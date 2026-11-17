"""Built-in providers of the `console` role.

    serial-mux   a serial-mux daemon on a USB-UART (the host's usual console)
    none         no console: commands that need one say so, the page shows none

A site plugin adds others (a trace channel over a debug adapter, a network console) by registering them.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import InvalidRequest
from ..core.providers import Field, PROVIDERS
from .console import ConsoleProvider
from .hostio import SerialConsole


class NoneConsole(ConsoleProvider):
    provider_name = "none"

    def _no(self) -> InvalidRequest:
        return InvalidRequest(f"{self.spec.id} has no console: its record names provider `none`",
                              hint=f"record one: bsp dut set {self.spec.id} --console-provider serial-mux")

    def tail(self, lines: int = 50, grep: str | None = None) -> list[str]:
        return []

    def range(self, since: str | None, until: str | None, grep: str | None = None, regex: bool = False,
              limit: int = 20000, first: bool = False) -> dict[str, Any]:
        return {"lines": [], "matched": 0, "truncated": False}

    def send(self, text: str, pattern: str | None = None, fail: str | None = None, timeout: float = 60,
             mark: Any = None) -> dict[str, Any]:
        raise self._no()

    def wait(self, pattern: str | None, fail: str | None = None, timeout: float = 60, mark: Any = None) -> str:
        raise self._no()

    def run(self, command: str, expect: str, timeout: float) -> dict[str, Any]:
        raise self._no()

    def up(self) -> bool:
        return False


class SerialMux(ConsoleProvider):
    provider_name = "serial-mux"

    @classmethod
    def fields(cls) -> list[Field]:
        return [Field("alias", "serial-mux alias", required=True, note="the console's alias on this host"),
                Field("device", "tty device", note="where the alias attaches when it starts on its own")]

    def __init__(self, spec: Any, config: dict[str, Any] | None = None):
        super().__init__(spec, config)
        from .consoles import ConsoleManager

        self.alias = self.config.get("alias") or spec.id
        self.manager = ConsoleManager(self.config.get("serial_mux", "serial-mux"),
                                      self.config.get("log_dir", "~/.serial-mux/logs"))
        self.console = SerialConsole(self.alias, self.config.get("smtty", "smtty"),
                                     self.config.get("log_dir", "~/.serial-mux/logs"))

    def tail(self, lines: int = 50, grep: str | None = None) -> list[str]:
        return self.console.tail(lines, grep)

    def range(self, since: str | None, until: str | None, grep: str | None = None, regex: bool = False,
              limit: int = 20000, first: bool = False) -> dict[str, Any]:
        return self.console.range(since, until, grep, regex, limit, first)

    def send(self, text: str, pattern: str | None = None, fail: str | None = None, timeout: float = 60,
             mark: Any = None) -> dict[str, Any]:
        return self.console.send(text, pattern, fail, timeout, mark)

    def wait(self, pattern: str | None, fail: str | None = None, timeout: float = 60, mark: Any = None) -> str:
        return self.console.wait(mark, pattern, fail, timeout)

    def run(self, command: str, expect: str, timeout: float) -> dict[str, Any]:
        return self.console.run(command, expect, timeout)

    def mark(self) -> Any:
        return self.console.mark()

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


def _serial(ctx: Any, spec: Any, services: Any) -> SerialMux:
    tools = services("dut_tools") if "dut_tools" in ctx.services else {}
    return SerialMux(spec, {"provider": "serial-mux", **tools, **(spec.console or {})})


def _none(ctx: Any, spec: Any, services: Any) -> NoneConsole:
    return NoneConsole(spec, {"provider": "none"})


PROVIDERS["console"].register("serial-mux", _serial)
PROVIDERS["console"].register("none", _none)
