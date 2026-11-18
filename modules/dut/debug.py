"""The `debug` role: halt, run, step, registers, a live trace channel and a GDB endpoint.

The contract (`DebugProvider`) and the server-backed base (`SessionDebug`) live in `core/roles.py` and here, because
a site plugin implements them without importing a module; the built-in `none` provider says a board has no debug
path, in the same words everywhere.
"""

from __future__ import annotations

from typing import Any

from core.errors import InvalidRequest
from core.roles import DebugProvider

__all__ = ["DebugProvider", "SessionDebug", "NoDebug"]


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


class NoDebug(DebugProvider):
    """A board with no debug path (most of them): says so, and offers nothing."""

    provider_name = "none"

    def _no(self) -> InvalidRequest:
        return InvalidRequest(
            f"{self.dut_id_of(self.dut)} has no debug provider: it is not debugged through this host",
            hint=f"record one: bsp dut set {self.dut_id_of(self.dut)} --debug-provider <name> (bsp status lists them)",
        )

    def start(self, lease: str | None = None) -> dict[str, Any]:
        raise self._no()

    def stop(self) -> dict[str, Any]:
        raise self._no()

    def action(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        raise self._no()

    def shelf(self) -> dict[str, Any]:
        return {"provider": "none", "kinds": []}
