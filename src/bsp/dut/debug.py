"""The `debug` role: halt, run, step, registers, a live trace channel and a GDB endpoint.

Debug is a service of a board like its console and its download path, because a board may be debugged through a
different path than it is flashed through (a GPIO bit-banged SWD link rather than a USB port). A provider reports
what it offers (`kinds`: `halt`, `regs`, `trace`, `gdb`, `write`), starts and stops a server as a session under
the board's lease, and answers the actions it declares — nothing here assumes a particular adapter or probe.
"""

from __future__ import annotations

from abc import ABC
from typing import Any

from ..core.errors import InvalidRequest


class DebugProvider(ABC):
    provider_name = "none"
    # What this provider can do, as names a page and a CLI render: halt, run, reset, step, regs, trace, gdb, write.
    kinds: tuple[str, ...] = ()

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
        return {"provider": self.provider_name, "kinds": list(self.kinds)}

    def info(self) -> dict[str, Any]:
        return {"provider": self.provider_name, "kinds": list(self.kinds), "available": bool(self.kinds)}


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
        """Its board's id: the provider is built for a Board (which has a spec), or given the spec itself."""
        return getattr(getattr(self.dut, "spec", self.dut), "id")

    def start(self, lease: str | None = None) -> dict[str, Any]:
        session = self.sessions.start(self.dut_id, "debug", self.provider_name, self.server_command(),
                                      lease=lease, ready=self.ready, timeout=40.0, meta=self.session_meta())
        return {"dut": self.dut_id, "provider": self.provider_name, **session.to_dict()}

    def stop(self) -> dict[str, Any]:
        return self.sessions.stop(self.dut_id, "debug")


class NoDebug(DebugProvider):
    """A board with no debug path (most of them): says so, and offers nothing."""

    provider_name = "none"

    def _no(self) -> InvalidRequest:
        return InvalidRequest(
            f"{self.dut.id} has no debug provider: it is not debugged through this host",
            hint=f"record one: bsp dut set {self.dut.id} --debug-provider <name> (bsp status lists them)",
        )

    def start(self, lease: str | None = None) -> dict[str, Any]:
        raise self._no()

    def stop(self) -> dict[str, Any]:
        raise self._no()

    def action(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        raise self._no()

    def shelf(self) -> dict[str, Any]:
        return {"provider": "none", "kinds": []}
