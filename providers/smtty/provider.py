"""`smtty` module: serial consoles on a board host, and the serial-mux tooling that serves them.

It publishes the host's `smtty` service — the console manager, the smtty/serial-mux client and the log directory —
and offers the `serial-mux` console provider, which is what a DUT record gets when it names no console provider.
The DUT module reads the same service for its slots, probes and ssh console logins, so a host that carries no
`smtty` module has no serial consoles anywhere and says so where it matters.

`external/serial-mux` is the checkout the host's `serial-mux` binary and its `python -m serial_mux` come from;
`deploy/` installs it from there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.dut.contracts import ConsoleProvider
from providers.smtty.consoles import ConsoleManager
from providers.smtty.serial import SerialConsole

TITLE = "Serial consoles"


class SerialMuxConsole(ConsoleProvider):
    """A serial-mux daemon on a USB-UART: the host's usual console for a board."""

    provider_name = "serial-mux"

    # A serial adapter is the host's slot: the slot table starts its daemon, not this provider.
    served_by_slots = True

    @classmethod
    def fields(cls) -> list[Any]:
        from core.providers import Field

        return [Field("alias", "serial-mux alias", required=True, note="the console's alias on this host"),
                Field("device", "tty device", note="where the alias attaches when it starts on its own")]

    def __init__(self, spec: Any, config: dict[str, Any] | None = None):
        super().__init__(spec, config)
        self.alias = self.config.get("alias") or spec.id
        self.manager = ConsoleManager(self.config.get("serial_mux", "serial-mux"),
                                      self.config.get("log_dir") or self.config.get("serial_log_dir")
                                      or "~/.serial-mux/logs")
        self.console = SerialConsole(self.alias, self.config.get("smtty", "smtty"),
                                     self.config.get("log_dir", "~/.serial-mux/logs"))

    def tail(self, lines: int = 50, grep: str | None = None) -> list[str]:
        return self.console.tail(lines, grep)

    def range(self, since: str | None, until: str | None, grep: str | None = None, regex: bool = False,
              limit: int = 20000, first: bool = False) -> dict[str, Any]:
        return self.console.range(since, until, grep, regex, limit, first)

    def send(self, text: str, pattern: str | None = None, fail: str | None = None, timeout: float = 60,
             mark: Any = None, stall: float | None = None, nudge: float | None = None) -> dict[str, Any]:
        return self.console.send(text, pattern, fail, timeout, stall=stall, nudge=nudge)

    def wait(self, pattern: str | None, fail: str | None = None, timeout: float = 60, mark: Any = None,
             stall: float | None = None, nudge: float | None = None) -> str:
        return self.console.wait(pattern, fail, timeout, mark=mark, stall=stall, nudge=nudge)

    def run(self, command: str, expect: str, timeout: float) -> dict[str, Any]:
        return self.console.run(command, expect, timeout)

    def mark(self) -> Any:
        return self.console.mark()

    def retained_mark(self) -> Any:
        """The start of today's log: what the console kept, however long ago it printed it."""
        return self.console.retained_mark()

    def read_since(self, mark: Any) -> str:
        return self.console.read_since(mark) if mark is not None else ""

    def nudge(self) -> None:
        self.console.nudge()

    def up(self) -> bool:
        return self.manager.up(self.alias)

    def socket(self) -> str | None:
        return str(self.manager.socket(self.alias))

    def attach(self) -> str:
        return f"{self.console.smtty} {self.alias}"

    def ensure(self) -> str:
        return self.manager.ensure(self.alias)

    def stop(self) -> None:
        self.manager.stop(self.alias)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider_name, "alias": self.alias, "up": self.up(), "socket": self.socket(),
                "attach": self.attach()}


class Smtty:
    """The host's serial tooling as a service: what the DUT module's slots, probe and logins read.

    `consoles` is the one every slot and this module's console providers share: a manager is stateless beyond the run
    directory it reads, so one serves the whole host.
    """

    def __init__(self, smtty: str, serial_mux: str, log_dir: str):
        self.smtty = smtty
        self.serial_mux = serial_mux
        self.log_dir = str(Path(log_dir).expanduser())
        self.consoles = ConsoleManager(serial_mux, self.log_dir)

    @property
    def run_dir(self) -> Path:
        return self.consoles.run_dir

    def console(self, spec: Any, cfg: dict[str, Any] | None = None) -> SerialMuxConsole:
        """A console provider for one board, configured from the host's tools and its record."""
        return SerialMuxConsole(spec, {"provider": "serial-mux", "smtty": self.smtty,
                                       "serial_mux": self.serial_mux, "log_dir": self.log_dir,
                                       **(cfg or {}), **(spec.console or {})})


def setup(ctx: Any, api: Any, config: dict[str, Any]) -> None:
    service = Smtty(config.get("smtty", "smtty"), config.get("serial_mux", "serial-mux"),
                    config.get("log_dir") or config.get("serial_log_dir") or "~/.serial-mux/logs")
    api.service("smtty", service)

    def serial(_ctx: Any, spec: Any, services: Any) -> SerialMuxConsole:
        # A host with this module serves its boards' consoles; a record's own block wins over the host's tools.
        return services("smtty").console(spec, (spec.raw.get("services") or {}).get("console"))

    api.provider("console", "serial-mux", serial, title="a serial-mux console",
                 about="A serial-mux daemon on a USB-UART, with history, one-shot commands and a log")
    return service
