"""Host configuration (<site>/hosts/<host>.yaml)."""

from __future__ import annotations

from core import naming

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.env import resolve_refs
from core.errors import InvalidRequest


@dataclass
class HostConfig:
    path: Path
    host_id: str
    description: str = ""
    bind: str = "127.0.0.1"
    port: int = 8765
    state_dir: Path = Path("/var/lib/boardctrl")
    auth_token: str | None = None
    # The providers the host loads, by name: `modules:` (the key sites have always written) or `providers:`.
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def modules(self) -> dict[str, dict[str, Any]]:
        """The same map under the name sites have always written (`modules:` in the host YAML)."""
        return self.providers
    announce: dict[str, Any] | None = None   # registry this host announces itself to
    env: dict[str, str] = field(default_factory=dict)   # values hooks receive (top-level `env:`)

    @property
    def config_dir(self) -> Path:
        return self.path.parent

    # The key a host's provider map may be written under: `providers:` now, `modules:` in a host installed before the
    # name changed. One map is one map however it is spelled.
    PROVIDER_KEYS = ("providers", "modules")

    @classmethod
    def load(cls, path: str | Path) -> "HostConfig":
        import yaml

        p = Path(path).resolve()
        with open(p, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        # Any string written as `env:NAME` takes the value boardctrld runs with (top level, server,
        # modules, ...); an unset variable refuses to start with a message naming it.
        raw = resolve_refs(raw, f"host config {p}")
        # `plugins:` was the key before providers had a section map, and a file that still says it would load nothing
        # without a word: it is the same list of names, so it is read (a section under `providers:` wins).
        if isinstance(raw.get("plugins"), list):
            folded = dict(raw.get("modules") or {})
            for name in raw["plugins"]:
                folded.setdefault(str(name), {})
            raw["modules"] = folded
        host = raw.get("host") or {}
        srv = raw.get("server") or {}
        env = raw.get("env") or {}
        if not isinstance(env, dict):
            raise InvalidRequest(f"{p}: `env:` must be a mapping of NAME: value",
                                 hint="e.g. env: {BOARDCTRL_SERIAL_LOG_DIR: /var/log/serial}")
        state_dir = Path(naming.get("BOARDCTRL_STATE_DIR") or srv.get("state_dir") or "/var/lib/boardctrl")
        if not state_dir.is_absolute():
            state_dir = (p.parent / state_dir).resolve()
        return cls(
            path=p,
            host_id=host.get("id", p.stem),
            description=host.get("description", ""),
            bind=naming.get("BOARDCTRL_BIND") or srv.get("bind", "127.0.0.1"),
            port=int(naming.get("BOARDCTRL_PORT") or srv.get("port", 8765)),
            state_dir=state_dir,
            auth_token=naming.get("BOARDCTRL_TOKEN") or srv.get("auth_token") or None,
            # `providers:` is the key now; a host written before it may have both, and then both load (the new name's
            # section wins for a name in both), rather than one map being dropped without a word.
            providers={**{k: (v or {}) for k, v in (raw.get("modules") or {}).items()},
                       **{k: (v or {}) for k, v in (raw.get("providers") or {}).items()}},
            announce=_announce(srv.get("announce")),
            env={str(k): ("" if v is None else str(v)) for k, v in env.items()},
        )


def _announce(cfg: Any) -> dict[str, Any] | None:
    """server.announce: {url, interval_s, ssh_user, addresses, token}; $BSP_REGISTRY_URL overrides url."""
    url = naming.get("BOARDCTRL_REGISTRY_URL")
    if isinstance(cfg, str):
        cfg = {"url": cfg}
    cfg = dict(cfg or {})
    if url:
        cfg["url"] = url
    return cfg if cfg.get("url") else None
