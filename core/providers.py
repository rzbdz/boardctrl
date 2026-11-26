"""Services a board host offers, and the providers that implement them.

A DUT is a composition: each of its *roles* — how it is reset, how its console is reached, how an image gets
onto it, how it is debugged — is filled by a provider. Providers are registered under ``<role>/<name>`` and are
selected per DUT in the DUT's record (``console: {provider: rtt}``); a role with no provider named uses its
default. A provider reports its own title, explanation and configuration fields, so the API, the CLI and the web
console can offer it without knowing it.

Roles:

    relay      reset, download mode, buttons
    console    a byte stream with history, and one-shot commands on it
    download   a download port for staging files into RAM
    flash      writing an image or a package to the board
    debug      halt/run/step/registers, live trace (RTT), a GDB endpoint, writing image regions

The registries are the extension point: a site plugin registers its providers from ``setup()``, so a new kind of
hardware needs no change here and no change in the modules that consume the roles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from core.registry import Registry

ROLES: dict[str, str] = {
    "relay": "reset the board, enter its download mode, press its buttons",
    "console": "the board's console: a byte stream with history, and one-shot commands",
    "download": "a download port to stage files into RAM",
    "flash": "write an image or a package to the board",
    "debug": "halt, run, step and inspect the board; live trace; a GDB endpoint",
}

# role -> name -> factory(ctx, spec, services) -> provider
PROVIDERS: dict[str, Registry[Callable[..., Any]]] = {role: Registry(f"{role} provider") for role in ROLES}


@dataclass
class Field:
    """One configuration value a provider takes, described so a form can be drawn from it."""

    """One configuration value a provider takes, described so a form can be drawn from it."""

    name: str
    label: str
    kind: str = "text"            # text | integer | number | boolean | choice
    required: bool = False
    default: Any = None
    options: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "kind": self.kind, "required": self.required,
                "default": self.default, "options": list(self.options), "note": self.note}


def provider(role: str, name: str):
    """Register a provider factory: ``@provider("console", "rtt")``."""
    if role not in PROVIDERS:
        raise ValueError(f"unknown service role {role!r}; roles are {', '.join(ROLES)}")
    return PROVIDERS[role].register(name)


def build(role: str, name: str, ctx: Any, spec: Any, services: Callable[[str], Any]) -> Any:
    """Instantiate ``<role>/<name>`` for one DUT, carrying the title and explanation it was registered with.

    A provider says what it is once, where it registers (`api.provider(..., title=..., about=...)`); the instance
    is what the API, the CLI and the page read, so it gets them too rather than every caller looking up the factory.
    """
    if role not in PROVIDERS:
        raise ValueError(f"unknown service role {role!r}; roles are {', '.join(ROLES)}")
    factory = PROVIDERS[role].get(name)
    made = factory(ctx, spec, services)
    for field in ("title", "about"):
        if not getattr(made, field, "") and getattr(factory, field, ""):
            setattr(made, field, getattr(factory, field))
    return made


def describe() -> dict[str, dict[str, Any]]:
    """What providers this host has, by role (for `boardctrl status`, the API and the page)."""
    return {role: {"about": ROLES[role], "names": PROVIDERS[role].names()} for role in ROLES}


class Service:
    """Base of a provider: one role of one DUT.

    ``config`` is the DUT's record for this role (``{"provider": "...", ...}``); ``fields()`` describes the
    values it reads from it, so `boardctrl dut set` and the page can offer them without knowing the provider.
    """

    role: str = ""
    name: str = ""
    title: str = ""
    about: str = ""

    def __init__(self, spec: Any, config: dict[str, Any] | None = None):
        self.spec = spec
        self.config = dict(config or {})

    @classmethod
    def fields(cls) -> list[Field]:
        return []

    def setup(self) -> None:
        """Prepare lines or connections when a DUT is (re)built."""

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name}

    def info(self) -> dict[str, Any]:
        return {"role": self.role, "provider": self.name, "title": self.title, "about": self.about}
