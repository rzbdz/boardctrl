"""Running a program for the host: in a process group of its own, stopped with what it started.

`run` is what the kernel and its providers use for anything that may take long (fastboot, hooks, scp, ssh). The
program gets a new session, so a cancel or a timeout reaches its children too: SIGTERM to the whole group (it may let
go of a device), then SIGKILL after a grace period. A cancel comes from the running job's token
(`core.jobs.current_token`), so a caller honors it without being handed it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from core.jobs import CancelToken, current_token

GRACE_S = 3.0
STDERR_KEEP = 65536        # with `on_line`: how much of stderr is kept (its end)


@dataclass
class Result:
    """What `subprocess.run` returns, the parts callers use, and whether it was stopped."""
    args: list[str]
    returncode: int
    stdout: Any
    stderr: Any
    timed_out: bool = False
    elapsed_s: float = 0.0


class TimedOut(Exception):
    """The program ran past its timeout and was stopped (its group got SIGTERM, then SIGKILL)."""

    def __init__(self, argv: Sequence[str], timeout: float, result: Result):
        super().__init__(f"`{' '.join(list(argv)[:4])} ...` timed out after {timeout:g}s")
        self.argv, self.timeout, self.result = list(argv), timeout, result


def stop_group(proc: subprocess.Popen, grace: float = GRACE_S) -> None:
    """SIGTERM to the program and what it started, then SIGKILL to them if the program is still there after
    `grace` seconds. The group is killed either way: a child that ignores the program's exit is not left behind."""
    def signal_group(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
    signal_group(signal.SIGTERM)
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        pass
    signal_group(signal.SIGKILL)


class Watch:
    """Stops `proc` (and its group) when the token is cancelled or `timeout` passes. Use as a context manager
    around the code that reads the program's output; `cancelled` and `timed_out` say what happened."""

    def __init__(self, proc: subprocess.Popen, timeout: float | None, token: CancelToken | None = None,
                 grace: float = GRACE_S):
        self.proc, self.grace = proc, grace
        self.token = token or current_token()
        self.cancelled = False
        self.timed_out = False
        self._once = threading.Lock()
        self._timer = None
        if timeout is not None:
            self._timer = threading.Timer(timeout, self._expire)
            self._timer.daemon = True
        self._forget: Callable[[], None] = lambda: None

    def _stop(self) -> None:
        if self._once.acquire(blocking=False):
            threading.Thread(target=stop_group, args=(self.proc, self.grace), daemon=True).start()

    def _expire(self) -> None:
        self.timed_out = True
        self._stop()

    def _cancel(self) -> None:
        self.cancelled = True
        self._stop()

    def __enter__(self) -> "Watch":
        if self._timer is not None:
            self._timer.start()
        self._forget = self.token.on_cancel(self._cancel)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._forget()
        if self.proc.poll() is None and (self.cancelled or self.timed_out or exc[0] is not None):
            stop_group(self.proc, self.grace)


def run(argv: Sequence[str], *, timeout: float | None = None, input: str | bytes | None = None,
        cwd: str | os.PathLike | None = None, env: Mapping[str, str] | None = None,
        on_line: Callable[[str], None] | None = None, capture: bool = True, text: bool = True,
        stdin: Any = None, grace: float = GRACE_S, token: CancelToken | None = None) -> Result:
    """Run `argv` to its end, in its own process group. Raises `Cancelled` when the job is cancelled meanwhile
    (the group is stopped first), `TimedOut` past `timeout`, `FileNotFoundError` when the program is not there.
    `on_line` gets each stdout line as it comes (text mode); stdout is still returned whole."""
    tok = token or current_token()
    tok.check()
    pipe = subprocess.PIPE if capture else None
    t0 = time.monotonic()
    proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE if input is not None else stdin,
                            stdout=pipe, stderr=pipe, cwd=cwd, env=dict(env) if env is not None else None,
                            text=text, start_new_session=True, bufsize=1 if (text and on_line) else -1)
    with Watch(proc, timeout, tok, grace) as w:
        if on_line is not None and capture:
            out, err = _stream(proc, input, on_line)
        else:
            out, err = proc.communicate(input)
        proc.wait()
    res = Result(list(argv), proc.returncode, out, err, w.timed_out, round(time.monotonic() - t0, 2))
    if w.cancelled:
        raise tok.error()
    if w.timed_out:
        raise TimedOut(argv, timeout or 0, res)
    return res


def _stream(proc: subprocess.Popen, input: Any, on_line: Callable[[str], None]) -> tuple[str, str]:
    """Feed `input`, collect stderr in the background, and hand each stdout line to `on_line` as it comes."""
    err: list[str] = []

    def feed() -> None:
        try:
            if input is not None:
                proc.stdin.write(input)
            if proc.stdin:
                proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def drain() -> None:
        size = 0
        for chunk in iter(lambda: proc.stderr.read(8192), ""):
            err.append(chunk)
            size += len(chunk)
            while size > STDERR_KEEP and len(err) > 1:      # the end of a chatty stderr is what explains a failure
                size -= len(err.pop(0))

    helpers = [threading.Thread(target=f, daemon=True) for f in (feed, drain)]
    for t in helpers:
        t.start()
    out: list[str] = []
    for raw in proc.stdout:
        out.append(raw)
        on_line(raw.rstrip("\r\n"))
    for t in helpers:
        t.join(5)
    return "".join(out), "".join(err)
