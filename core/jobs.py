"""Background jobs: long operations (boot, flash) run in a thread and expose
progress lines + a final status, so CLI users and agents can follow them and
get an authoritative result instead of holding one HTTP request open.

A job can be cancelled. Its `CancelToken` is bound to the job's thread (`current_token()`), so whatever the job
runs honors it without being handed it: a wait returns at once (`token.sleep`, `token.wait`), a step boundary stops
(`token.check`), and a running program is killed with what it started (`core.proc.run` registers `on_cancel`).
"""

from __future__ import annotations

import contextvars
import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from core.errors import Cancelled, NotFound, BspError
from core.events import EventBus


class CancelToken:
    """Whether a job was asked to stop, and the means to stop promptly when it was."""

    def __init__(self, event: threading.Event | None = None, what: str = "job"):
        self.event = event if event is not None else threading.Event()
        self.what = what
        self.reason: str | None = None
        self.where: str | None = None          # the step the job is in, as the job said (`at`)
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def cancel(self, reason: str | None = None) -> bool:
        """Ask the job to stop. The `on_cancel` callbacks run once, here. False if it was asked already."""
        with self._lock:
            if self.event.is_set():
                return False
            self.reason = reason or None
            self.event.set()
            callbacks, self._callbacks = self._callbacks, []
        for cb in callbacks:
            try:
                cb()
            except Exception:  # noqa: BLE001 - one callback's failure must not keep the others from running
                pass
        return True

    def at(self, where: str) -> None:
        """Say which step the job is in: a cancelled job reports it as where it stopped."""
        self.where = where

    def error(self) -> Cancelled:
        msg = f"{self.what} cancelled" + (f" in {self.where}" if self.where else "") + \
              (f": {self.reason}" if self.reason else "")
        return Cancelled(msg, hint="the job was let go on request; the board keeps its lease and can be used again",
                         details={"step": self.where, "reason": self.reason})

    def check(self) -> None:
        """Raise `Cancelled` when the job was asked to stop."""
        if self.event.is_set():
            raise self.error()

    def sleep(self, seconds: float) -> None:
        """Sleep, unless the job is cancelled meanwhile: then raise `Cancelled` at once."""
        if self.event.wait(max(0.0, float(seconds))):
            raise self.error()

    def wait(self, until: "threading.Event | Callable[[], Any]", timeout: float | None = None,
             poll: float = 0.2) -> bool:
        """Wait for an event (or a predicate to become true) up to `timeout` seconds; True when it came, False on
        timeout. Raises `Cancelled` when the job is cancelled meanwhile."""
        deadline = None if timeout is None else time.monotonic() + timeout
        probe = until.is_set if isinstance(until, threading.Event) else until
        while True:
            self.check()
            if probe():
                return True
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return False
            step = poll if left is None else min(poll, left)
            if isinstance(until, threading.Event):
                if until.wait(step):
                    self.check()
                    return True
            else:
                self.sleep(step)

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Run `callback` when the job is cancelled (at once if it already is); returns what unregisters it."""
        with self._lock:
            if not self.event.is_set():
                self._callbacks.append(callback)

                def forget() -> None:
                    with self._lock:
                        if callback in self._callbacks:
                            self._callbacks.remove(callback)
                return forget
        callback()
        return lambda: None


class _NeverCancelled(CancelToken):
    """The token outside a job: nothing cancels it, and its sleep is a plain sleep."""

    def cancel(self, reason: str | None = None) -> bool:
        return False

    def at(self, where: str) -> None:
        pass

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, float(seconds)))

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        return lambda: None


NEVER = _NeverCancelled(what="call")
_current: contextvars.ContextVar[CancelToken] = contextvars.ContextVar("bsp_cancel_token", default=NEVER)


def current_token() -> CancelToken:
    """The running job's cancel token, or one that is never cancelled outside a job."""
    return _current.get()


def bind_token(token: CancelToken) -> contextvars.Token:
    """Make `token` the current one in this thread (a job's thread, or a thread working for it)."""
    return _current.set(token)


@dataclass
class Job:
    id: str
    kind: str
    title: str
    status: str = "running"            # running | succeeded | failed | cancelled
    created: float = field(default_factory=time.time)
    finished: float | None = None
    lines: list[str] = field(default_factory=list)
    result: Any = None
    error: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    progress: int | None = None        # percent done, when the job's body reports it
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # Who may cancel it: called with the caller's lease before a cancel (a board's job checks the board's lease).
    guard: Callable[[str | None], Any] | None = field(default=None, repr=False)
    token: CancelToken = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.token = CancelToken(self.cancel_event, self.title)

    def cancel(self, reason: str | None = None) -> bool:
        """Ask the job to stop: what it runs stops now, and it ends with `status: cancelled`."""
        return self.token.cancel(reason)

    def view(self, since: int = 0) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
            "created": self.created, "finished": self.finished,
            "elapsed_s": round((self.finished or time.time()) - self.created, 1),
            "progress": self.progress, "lines": self.lines[since:], "next": len(self.lines),
            "result": self.result, "error": self.error, "meta": self.meta,
        }


class JobLog:
    """Handed to job bodies: ``log("...")`` appends a timestamped progress line."""

    def __init__(self, job: Job, bus: EventBus):
        self._job, self._bus = job, bus
        self.stop = job.cancel_event
        self.token = job.token

    def check(self) -> None:
        """Called between steps: a cancelled job ends here, saying so."""
        self._job.token.check()

    def __call__(self, msg: str) -> None:
        line = f"[{time.time() - self._job.created:6.1f}s] {msg}"
        self._job.lines.append(line)
        self._bus.publish("job.log", job=self._job.id, line=line)

    def progress(self, percent: float) -> None:
        """Set how far the job is (0-100); `job get` and `job list` show it."""
        pct = round(max(0.0, min(100.0, float(percent))))
        if pct != self._job.progress:
            self._job.progress = pct
            self._bus.publish("job.progress", job=self._job.id, progress=pct)


class JobManager:
    def __init__(self, bus: EventBus, keep: int = 200):
        self.bus = bus
        self.keep = keep
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._seq = itertools.count(1)

    def start(self, kind: str, title: str, body: Callable[[JobLog], Any],
              guard: Callable[[str | None], Any] | None = None, **meta: Any) -> Job:
        job = Job(f"{kind}-{int(time.time())}-{next(self._seq)}", kind, title, meta=meta, guard=guard)
        with self._lock:
            self._jobs[job.id] = job
            for old in sorted(self._jobs.values(), key=lambda j: j.created)[:-self.keep]:
                if old.status != "running":
                    del self._jobs[old.id]
        self.bus.publish("job.started", job=job.id, kind=kind, title=title, **meta)

        def run() -> None:
            bind_token(job.token)
            log = JobLog(job, self.bus)
            last_line = lambda: job.lines[-1] if job.lines else None  # noqa: E731
            try:
                job.result = body(log)
                if job.token.cancelled:
                    self._cancelled(job, log, job.token.error(), last_line())
                else:
                    job.status = "succeeded"
                    if job.progress is not None:
                        job.progress = 100
            except BspError as e:
                if isinstance(e, Cancelled) or job.token.cancelled:
                    self._cancelled(job, log, e if isinstance(e, Cancelled) else job.token.error(), last_line())
                else:
                    job.error, job.status = e.to_dict(), "failed"
                    log(f"FAILED: {e.message}" + (f" (hint: {e.hint})" if e.hint else ""))
            except Exception as e:  # pragma: no cover - defensive
                job.error = {"code": "internal_error", "message": str(e), "details": traceback.format_exc()[-2000:]}
                job.status = "failed"
                log(f"FAILED: {e}")
            finally:
                job.finished = time.time()
                self.bus.publish("job.finished", job=job.id, status=job.status, error=job.error)

        threading.Thread(target=run, name=job.id, daemon=True).start()
        return job

    @staticmethod
    def _cancelled(job: Job, log: JobLog, e: Cancelled, last: str | None) -> None:
        """A cancelled job says where it stopped: the step it was in, and the last line it printed."""
        err = e.to_dict()
        details = dict(err.get("details") or {})
        details.setdefault("step", job.token.where)
        details["last_line"] = last
        err["details"] = details
        job.error, job.status = err, "cancelled"
        log(f"cancelled: {e.message}")

    def get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise NotFound(f"no job {job_id!r}", hint="job ids are printed when a job starts; `bsp job list` shows recent ones") from None

    def cancel(self, job_id: str, lease: str | None = None, reason: str | None = None) -> dict[str, Any]:
        """Cancel a running job by id. A job a board runs needs that board's lease (its `guard` checks it)."""
        job = self.get(job_id)
        if job.status != "running":
            return {"id": job.id, "status": job.status, "cancelled": False,
                    "message": f"job {job.id} already ended ({job.status})"}
        if job.guard is not None:
            job.guard(lease)
        asked = job.cancel(reason)
        self.bus.publish("job.cancel", job=job.id, reason=reason)
        return {"id": job.id, "status": job.status, "cancelled": True,
                **({} if asked else {"message": "cancel was already asked for"})}

    def wait(self, job_id: str, timeout: float) -> Job:
        job = self.get(job_id)
        deadline = time.time() + timeout
        while job.status == "running" and time.time() < deadline:
            time.sleep(0.2)
        return job

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)
