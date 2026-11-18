"""`home-assistant` module: a home-automation instance's switches as a board's reset/BOOT lines.

The relay role's `home-assistant` provider is the extension point for that: a switch entity for reset, a button
entity for download mode. It is not wired up — an instance's URL, token and entity names belong to a site, so a site
carries the implementation as its own module of this name, which replaces this one.
"""

from __future__ import annotations

from typing import Any

from core.errors import InvalidRequest
from core.roles import ControlBackend, Log, Ready

TITLE = "Home Assistant"


class HomeAssistantControl(ControlBackend):
    def reset(self, log: Log, ready: Ready | None = None) -> None:
        raise InvalidRequest(
            f"{self.dut.id}: home-assistant control backend is not wired up on this host",
            hint="record a gpio-relay or human relay, or carry a site module that implements this one",
        )


def setup(ctx: Any, api: Any, config: dict[str, Any]) -> None:
    api.provider("relay", "home-assistant", lambda _ctx, spec, services: HomeAssistantControl(spec, services))
