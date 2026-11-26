"""Long-lived sessions on a host: a provider's server process, kept out of boardctrld's cgroup.

Some services of a board are not one-shot commands: a console, a live trace, a debug adapter. Their server
(the serial-mux daemon today, a trace bridge or a debug server tomorrow) runs as a **transient systemd unit**
(`systemd-run --unit=… --collect -p Restart=always`), so it survives a boardctrld restart and is killed with it only
when nobody holds the board.

A session is the unit plus its run record: the socket or TCP port it serves, which DUT and role it belongs to,
and the lease it was started under. Releasing the lease stops it (':meth:`Sessions.reap`), which is the RAII
rule of the whole host: the lease is the lifetime root of everything that runs for a board.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.errors import Conflict, HardwareError, InvalidRequest, NotFound

UNIT = "boardctrl-session-"


@dataclass
class Session:
    """One running server: what serves a DUT's role, and how to reach it."""

    dut: str
    role: str
    provider: str
    unit: str
    pid: int | None = None
    # A Unix socket or a TCP address ("127.0.0.1:19022") its clients connect to.
    endpoint: str | None = None
    lease: str | None = None
    started: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["uptime_s"] = round(time.time() - self.started, 1) if self.started else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def unit_name(dut: str, role: str) -> str:
    return UNIT + re.sub(r"[^A-Za-z0-9_.-]", "_", f"{dut}-{role}")


class Sessions:
    """The sessions on one host, as run records under the host's state directory.

    ``systemd`` is where the units are made: a callable that starts a unit and returns after systemd accepted it
    (``systemd``, ``systemd-run``); tests pass a fake. ``alive`` says whether a record's process still runs.
    ``held(lease)`` says whether a lease a session was started under is still the live one: a lease that ran out is
    no longer a reason for a server to be running, so a session started under it is reaped rather than handed on.
    """

    def __init__(self, directory: Path, run: "Systemd", alive: Callable[[int | None], bool] | None = None,
                 held: Callable[[str | None], bool] | None = None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.systemd = run
        self._alive = alive or _pid_alive
        self._held = held
        # Starting a session and letting go of a board take the same lock: `debug start` validates its lease and
        # then makes a unit, while `dut release` deletes the lease and stops that board's sessions. Interleaved the
        # other way, a start that passed its check just before the release would launch a server under a lease that
        # no longer exists — the one thing the lease is supposed to prevent.
        self._lock = threading.RLock()

    # -------------------------------------------------------------- records
    def _path(self, dut: str, role: str) -> Path:
        return self.dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', dut)}.{re.sub(r'[^A-Za-z0-9_.-]', '_', role)}.json"

    def _live(self, s: Session) -> bool:
        """Whether the record's server still runs, and nobody else's lease has replaced the one it was started under."""
        if not self.systemd.active(s.unit) and not self._alive(s.pid):
            return False
        if s.lease and self._held and not self._held(s.lease):
            # Its lease expired (or was taken): the board belongs to someone else now, and a server pointed at the
            # old holder's breakpoint is not theirs to inherit. Taking the start lock keeps this cleanup from
            # swallowing a record between a release and its own reap: the release still gets to report what it
            # stopped, and a start cannot slip a unit in while this is being taken down.
            with self._lock:
                try:
                    self.systemd.stop(s.unit)
                finally:
                    self._path(s.dut, s.role).unlink(missing_ok=True)
            return False
        return True

    def _records(self, dut: str | None = None) -> list[Session]:
        """Every record on disk, live or not: what `reap` acts on, so it can report what it stopped."""
        import json

        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                s = Session.from_dict(json.loads(p.read_text()))
            except (OSError, ValueError, TypeError):
                continue
            if dut is None or s.dut == dut:
                out.append(s)
        return out

    def get(self, dut: str, role: str) -> Session | None:
        path = self._path(dut, role)
        if not path.exists():
            return None
        try:
            import json

            s = Session.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return None
        return s if self._live(s) else None

    def list(self, dut: str | None = None) -> list[Session]:
        return [s for s in self._records(dut) if self._live(s)]

    def _save(self, s: Session) -> Session:
        import json

        tmp = self._path(s.dut, s.role).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(s), indent=2))
        tmp.replace(self._path(s.dut, s.role))
        return s

    # ---------------------------------------------------------------- start
    def start(self, dut: str, role: str, provider: str, command: list[str], *, endpoint: str | None = None,
              lease: str | None = None, env: dict[str, str] | None = None, ready: Callable[[], str | None] | None = None,
              timeout: float = 30.0, meta: dict[str, Any] | None = None) -> Session:
        """Run ``command`` as a transient unit and wait until ``ready()`` names its endpoint.

        A unit already serving this DUT and role is kept (idempotent: `console ensure` is called by every reader).
        A session named with a lease re-checks that lease here, under the lock a release takes: a start whose lease
        ended while it was being prepared must not bring a server up for a board that now belongs to someone else.
        """
        with self._lock:
            if lease is not None and self._held is not None and not self._held(lease):
                raise Conflict(f"{dut}: the lease this session would run under has ended",
                               hint="take the board's lease and start the session again")
            s = self.get(dut, role)
            if s:
                return s
            return self._start_locked(dut, role, provider, command, endpoint=endpoint, lease=lease, env=env,
                                      ready=ready, timeout=timeout, meta=meta)

    def _start_locked(self, dut, role, provider, command, *, endpoint, lease, env, ready, timeout, meta) -> Session:
        if not command:
            raise InvalidRequest(f"{dut}: the {provider} provider needs no server", hint="open it directly")
        unit = unit_name(dut, role)
        # What is collected this way never starts twice: a stale unit from a crashed run is cleared first.
        self.systemd.reset(unit)
        self.systemd.start(unit, command, env=env)
        try:
            started = time.monotonic()
            last = None
            deadline = started + timeout
            while time.monotonic() < deadline:
                last = ready() if ready else endpoint
                if last:
                    return self._save(Session(dut, role, provider, unit, self.systemd.pid(unit), last, lease,
                                              time.time(), dict(meta or {})))
                if not self.systemd.active(unit) and time.monotonic() - started > 2:
                    raise HardwareError(f"{dut}: {provider} server for {role} exited at once",
                                        hint=f"journalctl -u {unit}")
                time.sleep(0.2)
            raise HardwareError(f"{dut}: {provider} server for {role} did not come up in {timeout:g}s",
                                hint=f"journalctl -u {unit}")
        except BaseException:
            # A server that is up but not answering (or died) must not be left running: it holds the port, the pins
            # and whatever lock its tool takes, and it has no record, so nothing could stop it later.
            try:
                self.systemd.stop(unit)
                self.systemd.reset(unit)
            except Exception:  # noqa: BLE001 - the start's own failure is what the caller needs to see
                pass
            raise

    # ----------------------------------------------------------------- stop
    def stop(self, dut: str, role: str, *, lease: str | None = None) -> dict[str, Any]:
        with self._lock:
            s = self.get(dut, role)
            if not s:
                raise NotFound(f"{dut}: no {role} session is running")
            if lease is not None and s.lease and lease != s.lease:
                raise Conflict(f"{dut}: the {role} session belongs to another lease",
                               hint="take the board's lease first (`boardctrl dut acquire`)")
            self.systemd.stop(s.unit)
            self._path(dut, role).unlink(missing_ok=True)
            return {"dut": dut, "role": role, "stopped": True, "unit": s.unit}

    def reap(self, dut: str, role: str | None = None) -> list[str]:
        """Stop the sessions of a DUT (all roles when ``role`` is None); used when its lease ends.

        Reads the records as they are, not through :meth:`list`: after a lease ends, that filter stops and forgets
        a session on its own, so going through it would report nothing stopped right after stopping everything. It
        holds the start lock, so a start racing the release cannot make a unit between the release and this stop.
        """
        with self._lock:
            stopped = []
            for s in self._records(dut):
                if role and s.role != role:
                    continue
                self.systemd.stop(s.unit)
                if self.systemd.active(s.unit):
                    # The stop did not take (a hung ExecStop, a systemd that refused): keep the record so it can be
                    # stopped again, and do not tell the caller it was.
                    continue
                self._path(s.dut, s.role).unlink(missing_ok=True)
                stopped.append(s.role)
            return stopped

    def reap_stale(self) -> list[str]:
        """Forget records whose unit and process are both gone (a crash, a reboot)."""
        gone = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                import json

                s = Session.from_dict(json.loads(p.read_text()))
            except (OSError, ValueError, TypeError):
                # A record nobody can read is what a crash leaves behind: forget it, as this method promises.
                p.unlink(missing_ok=True)
                continue
            if not isinstance(s.dut, str) or not isinstance(s.role, str):
                p.unlink(missing_ok=True)
                continue
            if not self.systemd.active(s.unit) and not self._alive(s.pid):
                p.unlink(missing_ok=True)
                gone.append(s.unit)
        return gone


def _pid_alive(pid: int | None) -> bool:
    import os

    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


class Systemd:
    """Transient units, one call each. In tests, a subclass records the calls and fakes the process table."""

    prefix = "boardctrl-session-"

    def start(self, unit: str, command: list[str], env: dict[str, str] | None = None) -> None:
        import subprocess

        args = ["systemd-run", f"--unit={unit}", "--collect", "-p", "Restart=always", "-p", "RestartSec=2"]
        for key, value in (env or {}).items():
            args += ["-p", f"Environment={key}={value}"]
        r = subprocess.run([*args, *command], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise HardwareError(f"could not start {unit}: {(r.stderr or r.stdout).strip()[-300:]}",
                                hint=f"try by hand: {' '.join(command)}")

    def stop(self, unit: str) -> None:
        import subprocess

        subprocess.run(["systemctl", "stop", unit], capture_output=True)

    def reset(self, unit: str) -> None:
        import subprocess

        subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
        subprocess.run(["systemctl", "stop", unit], capture_output=True)

    def active(self, unit: str) -> bool:
        import subprocess

        r = subprocess.run(["systemctl", "is-active", "--quiet", unit], capture_output=True)
        return r.returncode == 0

    def pid(self, unit: str) -> int | None:
        import subprocess

        r = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", unit], capture_output=True, text=True)
        try:
            pid = int((r.stdout or "0").strip())
        except ValueError:
            return None
        return pid or None
