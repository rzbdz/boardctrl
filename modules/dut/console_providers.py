"""The `console` role's `none` provider: a board with no console says so.

The `serial-mux` provider is the `smtty` module's (a host that carries it serves serial consoles). A site module
adds others (a trace channel over a debug adapter, a network console).
"""

from __future__ import annotations

from typing import Any

from core.errors import InvalidRequest
from core.providers import PROVIDERS
from modules.dut.console import ConsoleProvider


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
             mark: Any = None, stall: float | None = None, nudge: float | None = None) -> dict[str, Any]:
        raise self._no()

    def wait(self, pattern: str | None, fail: str | None = None, timeout: float = 60, mark: Any = None,
             stall: float | None = None, nudge: float | None = None) -> str:
        raise self._no()

    def run(self, command: str, expect: str, timeout: float) -> dict[str, Any]:
        raise self._no()

    def up(self) -> bool:
        return False


def _none(ctx: Any, spec: Any, services: Any) -> NoneConsole:
    return NoneConsole(spec, {"provider": "none"})


PROVIDERS["console"].register("none", _none)
