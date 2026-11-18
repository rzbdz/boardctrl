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

    def __init__(self, spec: Any, config: dict[str, Any] | None = None):
        self.spec = spec
        self.config = dict(config or {})

    # A marker for a mark() the provider cannot read back (a transient stream): reads since then are empty.
    def mark(self) -> Any:
        return None

    def read_since(self, mark: Any) -> str:
        return ""

    @abstractmethod
    def tail(self, lines: int = 50, grep: str | None = None) -> list[str]:
        """The last lines of the console (passive: no keystrokes)."""

    @abstractmethod
    def range(self, since: str | None, until: str | None, grep: str | None = None, regex: bool = False,
              limit: int = 20000, first: bool = False) -> dict[str, Any]:
        """Lines stamped from `since` to `until`, optionally searched."""

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

    def __init__(self, dut: Any, services: Callable[[str], Any], cfg: dict[str, Any] | None = None):
        self.dut = dut
        # Its own settings: the record's `services.relay` block when it wrote one (the provider role's config),
        # else the older `control:` block, which is where these values lived before the role had a name.
        self.cfg = cfg if cfg is not None else getattr(dut, "control", {})
        self._services = services

    def setup(self) -> None:
        """Called at host start: prepare lines, register aliases."""

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
                                 hint=f"FDL strapped (a reset lands in download mode): bsp dut set {self.dut.id} --fdl strapped; "
                                      f"an FDL line: bsp dut set {self.dut.id} --fdl button --fdl-pin <pin>")
        raise InvalidRequest(f"{self.dut.id}: unknown fdl strategy {strategy!r}", hint="use strapped or button")

    def _fdl_button(self, log: Log) -> None:
        raise InvalidRequest(f"{self.dut.id}: backend {type(self).__name__} cannot drive an FDL button")

    def describe(self) -> dict[str, Any]:
        return {"backend": self.cfg.get("backend")}
