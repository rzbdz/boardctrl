"""The contracts a provider implements: one board's console, debug path, download port and flasher.

A role is filled per DUT by registering a provider under `<role>/<name>` (`core.providers`). The host, the CLI and
the page talk to these interfaces only, so a board's console can be a serial-mux alias, a trace channel or a
network port without anything above the role knowing which.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from core.errors import InvalidRequest
from core.providers import Field

Log = Callable[[str], None]
# returns a short description of the evidence once the requested state is visible, else None
Ready = Callable[[], "str | None"]


class ConsoleProvider(ABC):
    """One board's console: a byte stream with history, one-shot commands, and its log.

    The provider owns where the bytes come from (a serial-mux daemon on a USB-UART, a trace channel over a
    debug adapter, a network console) and how a command is typed and its answer recognised.
    """

    provider_name = "abstract"
    # Whether the host's slot table starts this board's console (every serial adapter on the machine is a slot),
    # or the provider starts it itself (a trace channel over a debug adapter, a network console).
    served_by_slots = False
    # The console's name on this host — what a slot, a log and a hook request call it. None when it has none.
    alias: str | None = None

    def __init__(self, spec: Any, config: dict[str, Any] | None = None):
        self.spec = spec
        self.config = dict(config or {})
        self.alias = self.config.get("alias")

    # A marker for a mark() the provider cannot read back (a transient stream): reads since then are empty.
    def mark(self) -> Any:
        return None

    def retained_mark(self) -> Any:
        """A marker from which `read_since` gives what the console kept before now, as far back as it keeps it —
        for a board that may already be where a step needs it before anything was done to it. The default is the
        same empty read as `mark()`."""
        return self.mark()

    def read_since(self, mark: Any) -> str:
        return ""

    @abstractmethod
    def tail(self, lines: int = 50, grep: str | None = None) -> list[str]:
        """The last lines of the console (passive: no keystrokes)."""

    @abstractmethod
    def range(self, since: str | None, until: str | None, grep: str | None = None, regex: bool = False,
              limit: int = 20000, first: bool = False, at: str | None = None, before: int = 0, after: int = 0,
              hits: bool = False, max_hits: int = 200000, marks: list[dict[str, Any]] | None = None,
              max_marks: int = 100000, from_line: int = 0) -> dict[str, Any]:
        """Lines stamped from `since` to `until`, optionally searched: `lines`, `matched`, `truncated`, and for a
        console that keeps a log, each line's number and position (`numbers`, `positions`) and the log's `total`.

        `at` asks for a window instead: `before` lines before the line it names, that line and `after` lines after
        it, with `more_before`, `more_after` and `snapped`. `hits` asks for the numbers of the matching lines, `marks`
        for the lines that match each mark, `from_line` for only the lines from that number on. A console without a
        log answers with nothing; the manager passes these only when a caller asked for them."""

    @abstractmethod
    def send(self, text: str, pattern: str | None = None, fail: str | None = None, timeout: float = 60,
             mark: Any = None, stall: float | None = None, nudge: float | None = None) -> dict[str, Any]:
        """Type `text`, wait for `pattern`, return what came back (and whether it failed).

        `stall` fails the wait when the console goes quiet that many seconds; `nudge` presses Enter on it when it has
        been quiet that long, so a prompt printed without a newline still arrives.
        """

    @abstractmethod
    def wait(self, pattern: str | None, fail: str | None = None, timeout: float = 60, mark: Any = None,
             stall: float | None = None, nudge: float | None = None) -> str:
        """Wait for `pattern` on what the console prints since `mark` (from now when it is None)."""

    @abstractmethod
    def run(self, command: str, expect: str, timeout: float) -> dict[str, Any]:
        """Run one shell command and return its output."""

    # ------------------------------------------------------------ liveness
    def up(self) -> bool:
        """Whether the console is reachable now (a daemon is running, a channel is open)."""
        return True

    def socket(self) -> str | None:
        """Where a protocol client (serial-mux) connects, when the provider serves one."""
        return None

    def attach(self) -> str | None:
        """A command a person runs on the host to type into it by hand."""
        return None

    def ensure(self) -> str:
        """Make sure it is reachable (start a daemon, open a channel); returns how it was served."""
        return "running"

    def stop(self) -> None:
        """Stop what `ensure` started."""

    def describe(self) -> dict[str, Any]:
        return {"provider": getattr(self, "provider_name", type(self).__name__)}


class DebugProvider(ABC):
    """Halt, run, step, registers, a live trace channel and a GDB endpoint.

    A provider reports what it offers (`kinds`: `halt`, `regs`, `trace`, `gdb`, `write`), starts and stops a server
    as a session under the board's lease, and answers the actions it declares — nothing here assumes a particular
    adapter or probe.
    """

    provider_name = "none"
    # What this provider can do, as names a page and a CLI render: halt, run, reset, step, regs, trace, gdb, write.
    kinds: tuple[str, ...] = ()
    # The words a page shows for this path; a provider inherits the ones it registered with.
    title: str = ""
    about: str = ""

    def __init__(self, dut: Any, config: dict[str, Any] | None = None, services: Any = None):
        self.dut = dut
        self.config = dict(config or {})
        self._services = services

    @property
    def sessions(self):
        """The host's session service: where a provider's server runs (a transient unit under the board's lease)."""
        if self._services is None:
            raise RuntimeError(f"{self.provider_name}: no session service on this host")
        return self._services("sessions")

    @property
    def gpio(self):
        """The host's GPIO service, for a provider whose path is bit-banged over header pins."""
        if self._services is None:
            raise RuntimeError(f"{self.provider_name}: no GPIO service on this host")
        return self._services("gpio")

    # ------------------------------------------------------------ lifecycle
    def start(self, lease: str | None = None) -> dict[str, Any]:
        """Bring the debug path up (start its server as a session); returns what it is and where."""
        raise NotImplementedError(f"{self.provider_name} cannot be started")

    def stop(self) -> dict[str, Any]:
        raise NotImplementedError(f"{self.provider_name} cannot be stopped")

    # ------------------------------------------------------------- actions
    def action(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one action of `kinds` (halt, run, reset, step, regs, …)."""
        raise NotImplementedError(f"{self.provider_name} has no action {name!r}")

    # --------------------------------------------------------------- views
    def shelf(self) -> dict[str, Any]:
        """What a board's page shows: provider, kinds, and the live state if it can tell."""
        return {"provider": self.provider_name, "kinds": list(self.kinds),
                **({"title": self.title} if self.title else {}), **({"about": self.about} if self.about else {})}

    def info(self) -> dict[str, Any]:
        return {"provider": self.provider_name, "kinds": list(self.kinds), "available": bool(self.kinds),
                **({"title": self.title} if self.title else {}), **({"about": self.about} if self.about else {})}

    @staticmethod
    def dut_id_of(dut: Any) -> str:
        """A provider's board id: it is built for a Board (which has a spec), or given the spec itself."""
        return getattr(getattr(dut, "spec", dut), "id")


class DownloadPort(ABC):
    """One board's way in: a USB download port that stages files into RAM, flashes a partition and reports itself.

    `boot.py` (stage, flash, getvar, continue, oem), `identify` and probing talk to this interface only, so a board
    can be reached by fastboot, a vendor's own downloader or a debug adapter's channel — nothing above the role
    knows which. `cmd` takes the verbs the recipes name (`boot.py`'s steps and an SDK recipe's), so a provider that
    is not fastboot implements the ones it can and says so for the rest.
    """

    provider_name = "abstract"

    def __init__(self, spec: Any, config: dict[str, Any] | None = None):
        self.spec = spec
        self.config = dict(config or {})

    @property
    def usb(self) -> str:
        """The board's path on this host's bus (`usb:2-1.4`), as `fastboot devices -l` shows it; "" when it has none."""
        return str(self.config.get("usb") or "")

    @property
    def binary(self) -> str:
        """The client a hook runs when it needs one itself, for its request (`hooks/probe/identify`); "" when the
        provider speaks the protocol rather than running a tool."""
        return ""

    @abstractmethod
    def state(self) -> dict[str, str] | None:
        """This board's download device when it is on the bus (its product name decides download mode), else None."""

    def enumeration(self) -> str | None:
        """A value that changes each time the device enumerates on the bus (Linux's USB devnum), or None when the
        provider cannot tell. It tells a device that came back after a reset from one that never went away."""
        return None

    @abstractmethod
    def cmd(self, *args: str, timeout: float = 120) -> str:
        """One verb on the port (`stage <file>`, `flash <part> <file>`, `continue`, `oem <x>`, `getvar <name>`)."""

    def getvar(self, name: str) -> str:
        """A variable the board reports (`blk-size`, `product`, …), without the command's decoration."""
        return self.cmd("getvar", name, timeout=15)


class FlashProvider(ABC):
    """One board's flasher. `config` is the role's block of the DUT record (`{"provider": ..., ...}`)."""

    provider_name = "abstract"
    title = ""
    about = ""

    def __init__(self, spec: Any, config: dict[str, Any] | None = None):
        self.spec = spec
        self.config = dict(config or {})

    @classmethod
    def fields(cls) -> list[Field]:
        """The settings this provider reads from the DUT record (`dut set --flash-…` and the page offer them)."""
        return []

    @abstractmethod
    def flash(self, package: Path, *, log: Log, timeout: float) -> dict[str, Any]:
        """Write `package` (a local file) to the board and say what happened. Never retried by the host."""
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider_name, "title": self.title}


class ControlBackend(ABC):
    """How a board is reset and put into download mode: the relay role's contract.

    `fdl.strategy` decides the topology; a role's GPIO or human actor only performs it.
    """

    needs_human = False      # reset/enter_fdl block until a person acts (run them as jobs)

    # How long a person has to act, when `needs_human`: a relay that asks one reports its own window.
    human_timeout_s = 300.0

    def human_timeout(self) -> float:
        """Seconds a person has to do what `reset`/`enter_fdl` asked: `services.relay.human.timeout_s`, else 300."""
        return float((self.cfg.get("human") or {}).get("timeout_s", self.human_timeout_s))

    def __init__(self, dut: Any, services: Callable[[str], Any], cfg: dict[str, Any] | None = None):
        self.dut = dut
        # Its own settings: the record's `services.relay` block when it wrote one (the provider role's config),
        # else the older `control:` block, which is where these values lived before the role had a name.
        self.cfg = cfg if cfg is not None else getattr(dut, "control", {})
        self._services = services

    def setup(self) -> None:
        """Called at host start: prepare lines, register aliases."""

    def preflight(self) -> None:
        """Check, without acting, that `reset` and `enter_fdl` can do their work now (their lines free, say).

        Called before a flash or a boot fetches anything, so a board that cannot be reset fails at once rather
        than after a long download. Raises a BspError saying what is in the way; a backend that cannot check
        anything has nothing to say, which is the default."""

    @abstractmethod
    def reset(self, log: Log, ready: Ready | None = None) -> None: ...

    def enter_fdl(self, log: Log, ready: Ready | None = None) -> None:
        strategy = (self.cfg.get("fdl") or {}).get("strategy", "strapped")
        if strategy == "strapped":
            log("FDL is strapped on this board: a reset lands in BROM download mode")
            self.reset(log, ready)
            return
        if strategy == "button":
            self._fdl_button(log)
            return
        if strategy == "unset":
            raise InvalidRequest(f"{self.dut.id}: how this board enters download mode is not set",
                                 hint=f"FDL strapped (a reset lands in download mode): boardctrl dut set {self.dut.id} --fdl strapped; "
                                      f"an FDL line: boardctrl dut set {self.dut.id} --fdl button --fdl-pin <pin>")
        raise InvalidRequest(f"{self.dut.id}: unknown fdl strategy {strategy!r}", hint="use strapped or button")

    def _fdl_button(self, log: Log) -> None:
        raise InvalidRequest(f"{self.dut.id}: backend {type(self).__name__} cannot drive an FDL button")

    def describe(self) -> dict[str, Any]:
        return {"backend": self.cfg.get("backend")}


class SessionDebug(DebugProvider):
    """A debug provider whose path is a server process, serving a protocol on loopback.

    A provider says how to start the server (`server_command`), when it is usable (`ready`: None until then, then
    the endpoint it serves) and what it wants remembered with the session (`session_meta`). The session runs as a
    transient unit under the board's lease, so releasing the board stops it; the provider keeps the verbs.
    """

    def server_command(self) -> list[str]:
        raise NotImplementedError

    def ready(self) -> str | None:
        raise NotImplementedError

    def session_meta(self) -> dict[str, Any]:
        return {}

    @property
    def dut_id(self) -> str:
        return self.dut_id_of(self.dut)

    def start(self, lease: str | None = None) -> dict[str, Any]:
        session = self.sessions.start(self.dut_id, "debug", self.provider_name, self.server_command(),
                                      lease=lease, ready=self.ready, timeout=40.0, meta=self.session_meta())
        return {"dut": self.dut_id, "provider": self.provider_name, **session.to_dict()}

    def stop(self) -> dict[str, Any]:
        return self.sessions.stop(self.dut_id, "debug")
