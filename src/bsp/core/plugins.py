"""Site plugins: modules a site carries, loaded into a running board host.

A pack or a site may bring code, not only data: a `modules/<name>/` directory with a `module.py` that calls
`setup(ctx, api)` when the host starts. That is where hardware nobody bought a driver for gets its providers,
commands and routes — the kernel-style loadable module of a board host:

    site/modules/ec/
        module.py        def setup(ctx, api): api.provider("debug", "my-swd", factory, title=..., about=...)
        ...              whatever else the plugin ships (bundles, data files)

The host config selects them (``modules: {ec: {enabled: true}}``, or ``plugins: [ec]``); a plugin that fails to
load is reported as degraded and never takes the host with it. Code lives only in the site layers, never in this
repository: public packs carry public knowledge, private procedures stay in the site.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import traceback
from pathlib import Path
from typing import Any

from .registry import Registry

log = logging.getLogger(__name__)


class PluginApi:
    """What a plugin may do: register providers, publish services for other modules, add commands and routes."""

    def __init__(self, name: str, ctx: Any):
        self.name = name
        self.ctx = ctx
        self.commands: list[Any] = []
        self.routers: list[Any] = []
        self.providers: list[str] = []

    def provider(self, role: str, provider: str, factory: Any, *, title: str = "", about: str = "") -> None:
        """Register a service provider for this host: `provider("debug", "my-swd", factory, title=…, about=…)`."""
        from .providers import PROVIDERS, ROLES

        if role not in ROLES:
            raise ValueError(f"unknown service role {role!r}")
        if title or about:
            factory.title, factory.about = title or getattr(factory, "title", ""), about or getattr(factory, "about", "")
        PROVIDERS[role].register(provider, factory)
        self.providers.append(f"{role}/{provider}")

    def command(self, command: Any) -> None:
        """Add one `core.commands.Command` (a `CommandSpec` plus its handler)."""
        self.commands.append(command)

    def router(self, router: Any) -> None:
        self.routers.append(router)

    def service(self, name: str, obj: Any) -> None:
        self.ctx.services[name] = obj


class Plugins:
    """The plugins this host has loaded, and why any did not."""

    def __init__(self, dirs: list[Path]):
        self.dirs = [Path(d) for d in dirs]
        self.loaded: dict[str, PluginApi] = {}
        self.errors: dict[str, str] = {}

    def available(self) -> dict[str, Path]:
        """Every plugin directory in the layers, the highest layer's copy winning."""
        found: dict[str, Path] = {}
        for layer in self.dirs:                      # lowest first
            if not layer.is_dir():
                continue
            for p in sorted(layer.iterdir()):
                if (p / "module.py").is_file():
                    found[p.name] = p
        return found

    def load(self, wanted: list[str], ctx: Any, config: dict[str, dict[str, Any]] | None = None) -> list[Any]:
        """Load the selected plugins (all available ones when ``wanted`` is empty); returns their extra commands.

        Each plugin's `setup(ctx, api, config)` gets its own section of the host config (``{}`` when the host
        names it only in ``plugins:``); a plugin written as `setup(ctx, api)` takes two arguments.
        """
        available = self.available()
        names = [n for n in (wanted or list(available)) if n in available]
        for missing in [n for n in wanted if n not in available]:
            self.errors[missing] = f"no plugin directory with module.py (looked in {', '.join(map(str, self.dirs))})"
        commands: list[Any] = []
        for name in names:
            path = available[name]
            try:
                module = _import(name, path / "module.py")
                api = PluginApi(name, ctx)
                setup = getattr(module, "setup", None)
                if not callable(setup):
                    raise ValueError("module.py has no setup(ctx, api[, config])")
                if _takes(setup, 3):
                    section = (config or {}).get(name)
                    setup(ctx, api, dict(section) if isinstance(section, dict) else {})
                else:
                    setup(ctx, api)
                self.loaded[name] = api
                commands.extend(api.commands)
            except Exception as e:  # noqa: BLE001 - one plugin must not take the host down
                self.errors[name] = f"{type(e).__name__}: {e}"
                log.warning("plugin %s did not load: %s\n%s", name, e, traceback.format_exc())
        return commands

    def summary(self) -> dict[str, Any]:
        return {
            "loaded": {name: {"providers": api.providers, "commands": [c.spec.name for c in api.commands]}
                       for name, api in self.loaded.items()},
            "errors": dict(self.errors),
            "available": sorted(self.available()),
        }

    def routers(self) -> list[Any]:
        return [r for api in self.loaded.values() for r in api.routers]


def _takes(fn: Any, count: int) -> bool:
    import inspect

    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return any(p.kind is p.VAR_POSITIONAL for p in params) or len(positional) >= count


def _import(name: str, path: Path):
    """Import a plugin's module.py under a name of its own (two plugins may use the same file name)."""
    digest = __import__("hashlib").sha1(str(path.resolve()).encode()).hexdigest()[:10]
    modname = f"bsp_plugin_{name}_{digest}"      # the same site in two processes: one module; two sites: two
    if modname in sys.modules:
        return sys.modules[modname]
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


PLUGIN_REGISTRY: Registry[str] = Registry("plugin")   # names, for a pack to advertise what it brings
