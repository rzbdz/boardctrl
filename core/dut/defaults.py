"""The providers the kernel itself fills a role with: no external tool, no site's code.

    console/none    a board with no console says so, in the same words everywhere
    download/none   a board with no download port reports that it is not on the bus
    debug/none      a board with no debug path offers nothing
    flash/hook      the host's flash job, which runs `hooks/flash/<flasher>` on the unpacked package

They are registered when the DUT manager is built, so they are there whatever a host carries. A provider that binds
a tool — a serial console, fastboot, a GPIO relay, a person — is a directory under `providers/`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from core.dut.contracts import ConsoleProvider, ControlBackend, DebugProvider, DownloadPort, FlashProvider, Log, Ready
from core.errors import InvalidRequest
from core.providers import PROVIDERS


class NoneConsole(ConsoleProvider):
    provider_name = "none"

    def _no(self) -> InvalidRequest:
        return InvalidRequest(f"{self.spec.id} has no console: its record names provider `none`",
                              hint=f"record one: boardctrl dut set {self.spec.id} --console-provider serial-mux")

    def tail(self, lines: int = 50, grep: str | None = None) -> list[str]:
        return []

    def range(self, since: str | None, until: str | None, grep: str | None = None, regex: bool = False,
              limit: int = 20000, first: bool = False, at: str | None = None, before: int = 0, after: int = 0,
              hits: bool = False, max_hits: int = 200000, marks: list[dict[str, Any]] | None = None,
              max_marks: int = 100000, from_line: int = 0) -> dict[str, Any]:
        return {"lines": [], "positions": [], "numbers": [], "matched": 0, "truncated": False, "total": 0}

    def send(self, text: str, pattern: str | None = None, fail: str | None = None, timeout: float = 60,
             mark: Any = None, stall: float | None = None, nudge: float | None = None) -> dict[str, Any]:
        raise self._no()

    def wait(self, pattern: str | None, fail: str | None = None, timeout: float = 60, mark: Any = None,
             stall: float | None = None, nudge: float | None = None) -> str:
        raise self._no()

    def run(self, command: str, expect: str, timeout: float) -> dict[str, Any]:
        raise self._no()

    def up(self) -> bool:
        return False


class NoneDownload(DownloadPort):
    """No download port: the board is never on the bus, and nothing is staged or flashed through it."""

    provider_name = "none"

    def __init__(self, spec: Any, config: dict[str, Any] | None = None):
        super().__init__(spec, config)

    @property
    def usb(self) -> str:
        return ""

    @property
    def binary(self) -> str:
        return ""

    def state(self) -> None:
        return None

    def cmd(self, *args: str, timeout: float = 120) -> str:
        raise InvalidRequest(f"{getattr(self.spec, 'id', 'the board')} has no download port: its record names provider `none`",
                             hint="record one, or leave it to the board's own way in")

    def getvar(self, name: str) -> str:
        raise InvalidRequest("no download port")


class NoDebug(DebugProvider):
    """A board with no debug path (most of them): says so, and offers nothing."""

    provider_name = "none"

    def _no(self) -> InvalidRequest:
        return InvalidRequest(
            f"{self.dut_id_of(self.dut)} has no debug provider: it is not debugged through this host",
            hint=f"record one: boardctrl dut set {self.dut_id_of(self.dut)} --debug-provider <name> "
                 "(boardctrl status lists them)",
        )

    def start(self, lease: str | None = None) -> dict[str, Any]:
        raise self._no()

    def stop(self) -> dict[str, Any]:
        raise self._no()

    def action(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        raise self._no()

    def shelf(self) -> dict[str, Any]:
        return {"provider": "none", "kinds": []}


class NoRelay(ControlBackend):
    """A board nothing on this host can reset: it says so, rather than leaving the host unable to start.

    A host without a GPIO header (a USB-only machine) has no relay provider at all, and a record it did not write
    itself may still name the relay role. Like `NoneConsole`, it keeps the rest of the board usable.
    """

    provider_name = "none"

    def _no(self) -> InvalidRequest:
        return InvalidRequest(
            f"{self.dut.id} has no relay: no provider on this host resets it",
            hint=f"a host with a GPIO header serves the gpio-relay provider; "
                 f"`boardctrl dut set {self.dut.id} --relay-provider <name>` chooses another (boardctrl status "
                 "lists what this host has)",
        )

    def reset(self, log: Log, ready: Ready | None = None) -> None:
        raise self._no()

    def enter_fdl(self, log: Log, ready: Ready | None = None) -> None:
        raise self._no()

    def describe(self) -> dict[str, Any]:
        return {"backend": "none", "error": self._no().message}


class HookFlash(FlashProvider):
    """The built-in `hook` provider: the host's flash job, which runs `hooks/flash/<flasher>` on the package.

    Its work needs the whole flash job (download mode, unpacking, the hook's environment), so it is run by
    `DutManager.flash` itself rather than through `flash()`; the class exists so the role has a name.
    """

    provider_name = "hook"
    title = "a flash hook"
    about = "An executable in the site's hooks/flash, run on the unpacked package"

    def flash(self, package: Path, *, log: Callable[[str], None], timeout: float) -> dict[str, Any]:
        raise InvalidRequest("the hook provider is run by the host's flash job, not called directly",
                             hint="this is an internal dispatch error; report it")


def register() -> None:
    """Put the kernel's own providers in the role tables (the DUT manager does this as it is built)."""
    PROVIDERS["console"].register("none", lambda ctx, spec, services: NoneConsole(spec, {"provider": "none"}))
    PROVIDERS["download"].register("none", lambda ctx, spec, services: NoneDownload(spec, {"provider": "none"}))
    PROVIDERS["debug"].register("none", lambda ctx, spec, services: NoDebug(spec, {}, services))
    PROVIDERS["flash"].register("hook", lambda ctx, spec, services: HookFlash(spec, {"provider": "hook"}))
    # No role is left without a way to say "this host does not fill it": a board whose record names a provider this
    # host does not carry keeps the rest of its roles, instead of taking the host down with it.
    PROVIDERS["relay"].register("none", lambda ctx, spec, services: NoRelay(spec, services, {}))
