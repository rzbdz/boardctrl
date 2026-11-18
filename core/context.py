"""What a provider is given: the host it runs in, its own state, and the services other providers published."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.errors import NotFound


class HostContext:
    """What a module is given: the host it runs in, its own state, and the services other modules published."""

    def __init__(self, host_id: str, bus: Any, state_dir: Path, config_dir: Path, jobs: Any = None,
                 port: int = 8765, auth_token: str | None = None, env: dict[str, str] | None = None,
                 bind: str = "127.0.0.1", announce: dict[str, Any] | None = None,
                 services: dict[str, Any] | None = None):
        self.host_id = host_id
        self.bus = bus
        self.state_dir = Path(state_dir)       # writable runtime state (overlays, caches)
        self.config_dir = Path(config_dir)     # directory of the host config file
        self.jobs = jobs
        self.port = port                       # where this host's API listens (for helpers that call back)
        self.auth_token = auth_token           # the token this host's API asks of writers (server.auth_token)
        self.env = dict(env or {})             # the host config's top-level `env:` map (already resolved)
        self.bind = bind                       # server.bind, for deriving this host's own addresses
        self.announce = announce               # server.announce, ditto
        self.services: dict[str, Any] = dict(services or {})

    def service(self, name: str) -> Any:
        try:
            return self.services[name]
        except KeyError:
            raise NotFound(
                f"service '{name}' is not available on this host",
                hint=f"enable the module that provides it in the host config (loaded: {sorted(self.services)})",
            ) from None

    def module_state_dir(self, name: str) -> Path:
        p = self.state_dir / name
        p.mkdir(parents=True, exist_ok=True)
        return p
