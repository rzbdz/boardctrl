"""The `debug` role: halt, run, step, registers, a live trace channel and a GDB endpoint.

Debug is a service of a board like its console and its download path, because a board may be debugged through a
different path than it is flashed through (a GPIO bit-banged SWD link rather than a USB port). A provider reports
what it offers (`kinds`: `halt`, `regs`, `trace`, `gdb`, `write`), starts and stops a server as a session under
the board's lease, and answers the actions it declares — nothing here assumes a particular adapter or probe.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DebugProvider(ABC):
    provider_name = "none"
    # What this provider can do, as names a page and a CLI render: halt, run, reset, step, regs, trace, gdb, write.
    kinds: tuple[str, ...] = ()

    def __init__(self, dut: Any, config: dict[str, Any] | None = None):
        self.dut = dut
        self.config = dict(config or {})

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
        return {"provider": self.provider_name, "kinds": list(self.kinds)}

    def info(self) -> dict[str, Any]:
        return {"provider": self.provider_name, "kinds": list(self.kinds), "available": bool(self.kinds)}


class NoDebug(DebugProvider):
    """A board with no debug path (most of them): says so, and offers nothing."""

    provider_name = "none"

    def start(self, lease: str | None = None) -> dict[str, Any]:
        raise NotImplementedError("this board has no debug provider: record one with `bsp dut set --debug-provider`")

    def stop(self) -> dict[str, Any]:
        raise NotImplementedError("this board has no debug provider")

    def action(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError("this board has no debug provider")

    def shelf(self) -> dict[str, Any]:
        return {"provider": "none", "kinds": []}
