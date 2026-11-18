"""The provider contract, and the loader that finds providers in the layers a host reads.

A module is a directory with a `module.py`:

    <root>/<name>/
        module.py    TITLE = "Serial console"            optional
                     def setup(ctx, api, config): ...    required; returns its manager object, if it has one
        ...          whatever else it ships (hooks, data files, an `external/` checkout)

`setup` registers everything the module contributes through `api`: providers for a role, commands, a router, the
services other modules read, and the callbacks the host calls as it starts, stops and reports. Every module's
`setup` runs before any module's startup callback, so a module may build what it needs from another module's
service when it starts — in any order, because nothing has to be there while another sets up.

The modules this repository ships (`modules/<name>/`) and a site's own (`<site>/modules/<name>/`) are loaded by
the same loader, and a site's copy of a name replaces the shipped one. A module that fails to load is reported as
degraded and never takes the host with it.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Callable


log = logging.getLogger(__name__)


class ProviderApi:
    """What a provider may do: register providers and services, add commands and routes, and say how to start it."""

    def __init__(self, name: str, ctx: Any):
        self.name = name
        self.ctx = ctx
        self.commands: list[Any] = []
        self.routers: list[Any] = []
        self.providers: list[str] = []
        self.services: list[str] = []
        self._startup: list[Callable[[], None]] = []
        self._shutdown: list[Callable[[], None]] = []
        self._status: list[Callable[[], dict[str, Any]]] = []
        self.open_paths: list[str] = []

    # ------------------------------------------------------------ registration
    def provider(self, role: str, provider: str, factory: Callable[..., Any], *,
                 title: str = "", about: str = "") -> None:
        """Register a service provider for this host: `provider("debug", "my-swd", factory, title=…, about=…)`."""
        from core.providers import PROVIDERS, ROLES

        if role not in ROLES:
            raise ValueError(f"unknown service role {role!r}")
        if title or about:
            factory.title, factory.about = title or getattr(factory, "title", ""), about or getattr(factory, "about", "")
        PROVIDERS[role].register(provider, factory)
        self.providers.append(f"{role}/{provider}")

    def command(self, command: Any) -> None:
        """Add one `core.commands.Command` (a `CommandSpec` plus its handler)."""
        self.commands.append(command)

    def add_commands(self, commands: list[Any]) -> None:
        """Add several commands at once (a module that builds them as a list)."""
        self.commands.extend(commands)

    def router(self, router: Any) -> None:
        self.routers.append(router)

    def service(self, name: str, obj: Any) -> None:
        """Publish an object other modules read as `ctx.service(name)`."""
        self.ctx.services[name] = obj
        self.services.append(name)

    def open_path(self, prefix: str) -> None:
        """A path prefix whose writes need no token on a host that has one."""
        self.open_paths.append(prefix)

    # ------------------------------------------------------------- lifecycle
    def on_startup(self, fn: Callable[[], None]) -> None:
        """Run once the host starts, after every module's setup."""
        self._startup.append(fn)

    def on_shutdown(self, fn: Callable[[], None]) -> None:
        self._shutdown.append(fn)

    def status(self, fn: Callable[[], dict[str, Any]]) -> None:
        """The module's health, merged into the host's `/api/v1/status` under its name."""
        self._status.append(fn)


class Loaded:
    """One provider that set up: what it registered, and how the host drives it."""

    def __init__(self, name: str, path: Path, module: Any, api: ProviderApi, manager: Any = None):
        self.name = name
        self.path = path
        self.module = module
        self.api = api
        # what the module's setup built for itself (its manager), for a helper or a test that drives it directly
        self.manager = manager
        self.title = str(getattr(module, "TITLE", "") or "")

    def startup(self) -> None:
        for fn in self.api._startup:
            fn()

    def shutdown(self) -> None:
        for fn in self.api._shutdown:
            fn()

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for fn in self.api._status:
            try:
                out.update(fn() or {})
            except Exception as e:  # noqa: BLE001 - one module's status never takes the page down
                out["error"] = f"{type(e).__name__}: {e}"
        return out

    def view(self) -> dict[str, Any]:
        """What `/api/v1/status` says about this module: its title, what it registered, and its own health."""
        return {"title": self.title, "providers": list(self.api.providers),
                "commands": [c.spec.name for c in self.api.commands], "services": list(self.api.services),
                **self.status()}


class Providers:
    """The providers a host has loaded, and why any did not."""

    def __init__(self, roots: list[Path]):
        self.roots = [Path(r) for r in roots]
        self.loaded: dict[str, Loaded] = {}
        self.errors: dict[str, str] = {}

    def available(self) -> dict[str, Path]:
        """Every module directory in the layers, the highest layer's copy winning."""
        found: dict[str, Path] = {}
        for root in self.roots:                      # lowest first
            if not root.is_dir():
                continue
            for p in sorted(root.iterdir()):
                if (p / "module.py").is_file():
                    found[p.name] = p
        return found

    def load(self, ctx: Any, config: dict[str, dict[str, Any]] | None = None) -> None:
        """Set up the modules the host names, in layer order (a site's copy of a name replaces the shipped one).

        Each module gets its own section of the host's `modules:` map as its settings; `enabled: false` turns a
        module off however else it would be loaded. A name with no module directory is reported, not fatal.
        """
        available = self.available()
        config = config or {}
        off = {name for name, cfg in config.items() if isinstance(cfg, dict) and cfg.get("enabled", True) is False}
        for name in [n for n in config if n not in off]:
            if name not in available:
                self.errors[name] = f"no module directory with module.py (looked in {', '.join(map(str, self.roots))})"
                continue
            self.setup(name, available[name], ctx, config.get(name) or {})

    def setup(self, name: str, path: Path, ctx: Any, section: dict[str, Any]) -> None:
        try:
            module = _import(name, path / "module.py")
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise ValueError("module.py has no setup(ctx, api, config)")
            api = ProviderApi(name, ctx)
            manager = setup(ctx, api, dict(section)) if _takes(setup, 3) else setup(ctx, api)
            self.loaded[name] = Loaded(name, path, module, api, manager)
        except Exception as e:  # noqa: BLE001 - one module must not take the host down
            self.errors[name] = f"{type(e).__name__}: {e}"
            log.warning("module %s did not load: %s\n%s", name, e, traceback.format_exc())

    def __getitem__(self, name: str) -> Any:
        """A loaded provider's manager object, by name (`host.modules["dut"]`)."""
        try:
            return self.loaded[name].manager
        except KeyError:
            raise KeyError(name) from None

    def values(self):
        """The manager objects, in load order (a helper that scans them by name)."""
        return [m.manager for m in self.loaded.values()]

    def __contains__(self, name: str) -> bool:
        return name in self.loaded

    def commands(self) -> list[Any]:
        return [c for m in self.loaded.values() for c in m.api.commands]

    def routers(self) -> list[Any]:
        return [r for m in self.loaded.values() for r in m.api.routers]

    def open_paths(self) -> list[str]:
        return [p for m in self.loaded.values() for p in m.api.open_paths]

    def startup(self) -> None:
        for m in self.loaded.values():
            m.startup()

    def shutdown(self) -> None:
        for m in self.loaded.values():
            m.shutdown()

    def view(self) -> dict[str, Any]:
        """The host's `modules:` status: one entry per module, loaded or failed."""
        return {name: m.view() for name, m in self.loaded.items()} | {
            name: {"title": name, "error": why} for name, why in self.errors.items()}


def _takes(fn: Any, count: int) -> bool:
    """Whether `fn` accepts `count` positional arguments (a module written `setup(ctx, api)` takes two)."""
    import inspect

    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return any(p.kind is p.VAR_POSITIONAL for p in positional) or len(positional) >= count


def _import(name: str, path: Path):
    """Import a module's module.py under a name of its own (two modules may use the same file name)."""
    digest = __import__("hashlib").sha1(str(path.resolve()).encode()).hexdigest()[:10]
    modname = f"boardctrl_module_{name}_{digest}"      # the same site in two processes: one module; two sites: two
    if modname in sys.modules:
        return sys.modules[modname]
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module
