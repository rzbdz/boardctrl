"""The provider contract, and the loader that finds providers in the layers a host reads.

A provider is a directory with a `provider.py`:

    <root>/<name>/
        provider.py  TITLE = "Serial console"            optional
                     def setup(ctx, api, config): ...    required; returns its manager object, if it has one
        ...          whatever else it ships (hooks, data files, an `external/` checkout)

`setup` registers everything the provider contributes through `api`: implementations for a role, commands, a
router, the services other providers read, and the callbacks the host calls as it starts, stops and reports.
Every `setup` runs before any provider's startup callback, so one may build what it needs from another's service
when it starts — in any order, because nothing has to be there while another sets up.

What a provider needs is declared, not discovered later: `REQUIRES = ("gpio", "smtty")` in its `provider.py` names
the services it reads. Every setup has already run when the next requirement is checked, so a provider whose
requirements are not all there is *degraded*: it stays loaded, keeps what it registered, and its status says what it
waits for. Without the declaration a missing service shows up as an error the first time a board is used, far from
the host configuration that caused it.

The providers this repository ships (`providers/<name>/`) and a site's own (`<site>/providers/<name>/`) are
loaded by the same loader, and a site's copy of a name replaces the shipped one. One that fails to load is
reported as degraded and never takes the host with it.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Callable


log = logging.getLogger(__name__)

# The names the kernel itself answers to in a host's provider map: their sections configure a service the host sets
# up (the DUT manager, the SDK catalogue, the host registry), not a provider directory.
KERNEL_SERVICES = ("dut", "sdk", "registry")


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
        """Add several commands at once (a provider that builds them as a list)."""
        self.commands.extend(commands)

    def router(self, router: Any) -> None:
        self.routers.append(router)

    def service(self, name: str, obj: Any) -> None:
        """Publish an object other providers read as `ctx.service(name)`."""
        self.ctx.services[name] = obj
        self.services.append(name)

    def open_path(self, prefix: str) -> None:
        """A path prefix whose writes need no token on a host that has one."""
        self.open_paths.append(prefix)

    # ------------------------------------------------------------- lifecycle
    def on_startup(self, fn: Callable[[], None]) -> None:
        """Run once the host starts, after every provider's setup."""
        self._startup.append(fn)

    def on_shutdown(self, fn: Callable[[], None]) -> None:
        self._shutdown.append(fn)

    def status(self, fn: Callable[[], dict[str, Any]]) -> None:
        """The provider's health, merged into the host's `/api/v1/status` under its name."""
        self._status.append(fn)


class Loaded:
    """One provider that set up: what it registered, and how the host drives it."""

    def __init__(self, name: str, path: Path, module: Any, api: ProviderApi, manager: Any = None):
        self.name = name
        self.path = path
        self.module = module
        self.api = api
        # what the provider's setup built for itself (its manager), for a helper or a test that drives it directly
        self.manager = manager
        self.title = str(getattr(module, "TITLE", "") or "")
        self.missing: list[str] = []

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
            except Exception as e:  # noqa: BLE001 - one provider's status never takes the page down
                out["error"] = f"{type(e).__name__}: {e}"
        return out

    def view(self) -> dict[str, Any]:
        """What `/api/v1/status` says about this provider: its title, what it registered, and its own health."""
        return {"title": self.title, "providers": list(self.api.providers),
                "commands": [c.spec.name for c in self.api.commands], "services": list(self.api.services),
                # A service it declared and does not have: the provider is degraded until the host offers it.
                **({"missing": list(self.missing),
                    "error": f"needs {', '.join(self.missing)}: enable the module that provides it"} if self.missing else {}),
                **self.status()}


class _Builtin(Loaded):
    """A provider the kernel carries itself: it has no directory, but registers like any other."""

    def __init__(self, name: str, title: str, api: ProviderApi, manager: Any):
        super().__init__(name, Path(), None, api, manager)
        self.title = title


class Providers:
    """The providers a host has loaded, and why any did not."""

    def __init__(self, roots: list[Path]):
        self.roots = [Path(r) for r in roots]
        self.loaded: dict[str, Loaded] = {}
        self.errors: dict[str, str] = {}

    def available(self) -> dict[str, Path]:
        """Every provider directory in the layers, the highest layer's copy winning."""
        found: dict[str, Path] = {}
        for root in self.roots:                      # lowest first
            if not root.is_dir():
                continue
            for p in sorted(root.iterdir()):
                if (p / "provider.py").is_file() or (p / "module.py").is_file():
                    found[p.name] = p
        return found

    def load(self, ctx: Any, config: dict[str, dict[str, Any]] | None = None) -> None:
        """Set up the providers the host names, in layer order (a site's copy of a name replaces the shipped one).

        Each provider gets its own section of the host's `providers:` map as its settings; `enabled: false` turns
        one off however else it would be loaded. A name with no provider directory is reported, not fatal; a site
        directory named after a kernel service (`dut`, `sdk`, `registry`) is that service's section, and a site that
        ships code under one of those names is told so rather than silently skipped.
        """
        available = self.available()
        config = config or {}
        off = {name for name, cfg in config.items() if isinstance(cfg, dict) and cfg.get("enabled", True) is False}
        for name in [n for n in config if n not in off]:
            if name in KERNEL_SERVICES and name not in available:
                continue                 # the kernel's own services: their sections configure them, not a provider
            if name not in available:
                self.errors[name] = f"no provider directory with provider.py (looked in {', '.join(map(str, self.roots))})"
                continue
            if name in KERNEL_SERVICES:
                self.errors[name] = (f"a site cannot ship code as {name!r}: that name is one of the kernel's own "
                                     f"services ({', '.join(sorted(KERNEL_SERVICES))}), which the host sets up itself")
                log.warning("provider %s is not loaded: %s", name, self.errors[name])
                continue
            self.setup(name, available[name], ctx, config.get(name) or {})

    def add(self, name: str, setup: Any, ctx: Any, section: dict[str, Any], title: str = "") -> None:
        """Set up a provider the kernel itself carries (no directory): `setup(ctx, api, config)` as for any other."""
        try:
            api = ProviderApi(name, ctx)
            manager = setup(ctx, api, dict(section))
            self.loaded[name] = _Builtin(name, title, api, manager)
        except Exception as e:  # noqa: BLE001 - one provider must not take the host down
            self.errors[name] = f"{type(e).__name__}: {e}"
            log.warning("provider %s did not load: %s\n%s", name, e, traceback.format_exc())

    def setup(self, name: str, path: Path, ctx: Any, section: dict[str, Any]) -> None:
        try:
            entry = path / "provider.py" if (path / "provider.py").is_file() else path / "module.py"
            module = _import(name, entry)
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise ValueError("provider.py has no setup(ctx, api, config)")
            api = ProviderApi(name, ctx)
            manager = setup(ctx, api, dict(section)) if _takes(setup, 3) else setup(ctx, api)
            self.loaded[name] = Loaded(name, path, module, api, manager)
        except Exception as e:  # noqa: BLE001 - one provider must not take the host down
            self.errors[name] = f"{type(e).__name__}: {e}"
            log.warning("provider %s did not load: %s\n%s", name, e, traceback.format_exc())

    def __getitem__(self, name: str) -> Any:
        """A loaded provider's manager object, by name."""
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

    def check(self) -> None:
        """Mark the providers whose declared services are not there (`REQUIRES`): loaded, degraded, and saying why.

        Every `setup` has run by now, so the services map is what the host offers: one provider may satisfy another's
        requirement in either order.
        """
        for provider in self.loaded.values():
            declared = [str(name) for name in (getattr(provider.module, "REQUIRES", None) or ())]
            provider.missing = [name for name in declared if name not in self.services()]

    def services(self) -> set[str]:
        """Every service the loaded providers published, by name."""
        return {name for provider in self.loaded.values() for name in provider.api.services}

    def startup(self) -> None:
        for m in self.loaded.values():
            m.startup()

    def shutdown(self) -> None:
        for m in self.loaded.values():
            m.shutdown()

    def view(self) -> dict[str, Any]:
        """The host's `providers:` status: one entry per provider, loaded or failed."""
        return {name: m.view() for name, m in self.loaded.items()} | {
            name: {"title": name, "error": why} for name, why in self.errors.items()}


def _takes(fn: Any, count: int) -> bool:
    """Whether `fn` accepts `count` positional arguments (a provider written `setup(ctx, api)` takes two)."""
    import inspect

    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return any(p.kind is p.VAR_POSITIONAL for p in positional) or len(positional) >= count


def _import(name: str, path: Path):
    """Import a provider's provider.py under a name of its own (two providers may use the same file name)."""
    digest = __import__("hashlib").sha1(str(path.resolve()).encode()).hexdigest()[:10]
    modname = f"boardctrl_provider_{name}_{digest}"      # the same site in two processes: one module; two sites: two
    if modname in sys.modules:
        return sys.modules[modname]
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module
