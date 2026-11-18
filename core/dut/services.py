"""Which provider fills which role of a DUT, and how a DUT's record says it.

A DUT's record names its providers per role under `services:`; a record that predates that (or a site that never
writes it) is read through the old fields, so nothing has to be migrated:

    console: {alias: tty-a}              -> console: serial-mux
    fastboot: {usb: usb:2-1.4}           -> download: fastboot
    control: {backend: gpio-relay}       -> relay: gpio-relay
    (nothing)                            -> flash: hook, debug: none

New record:

    services:
      relay:    {provider: gpio-relay, reset: {pin: J40:11}}
      console:  {provider: rtt, port: 19022}
      download: {provider: none}
      flash:    {provider: my-flasher, expect_version: ...}
      debug:    {provider: my-swd, target: probe0}

The values a provider reads are its own; it describes them with `fields()` so `bsp dut set` and the page can offer
them. Role names and provider names are plain strings: a plugin adds providers without touching this file.
"""

from __future__ import annotations

from typing import Any

from core.providers import PROVIDERS
from core.dut.records import DutSpec

DEFAULTS = {"relay": "gpio-relay", "console": "serial-mux", "download": "fastboot", "flash": "hook", "debug": "none"}


def _legacy_console(spec: DutSpec) -> dict[str, Any]:
    return {"alias": spec.console.get("alias"), **({"device": spec.console["device"]} if spec.console.get("device") else {})}


def bindings(spec: DutSpec) -> dict[str, dict[str, Any]]:
    """Each role of one DUT: `{provider, ...its own values}`."""
    explicit = spec.raw.get("services") or {}
    out: dict[str, dict[str, Any]] = {}
    for role, default in DEFAULTS.items():
        value = explicit.get(role)
        if isinstance(value, str):
            value = {"provider": value}
        out[role] = dict(value or {})
        out[role].setdefault("provider", default)
    if "relay" not in explicit:
        # the old record's control block is the relay's own config
        out["relay"] = {"provider": "gpio-relay", **{k: v for k, v in spec.control.items() if k != "backend"}}
        if spec.control.get("backend"):
            out["relay"]["provider"] = spec.control["backend"]
    if "console" not in explicit:
        out["console"] = {"provider": "serial-mux", **_legacy_console(spec)}
    if "download" not in explicit:
        out["download"] = {"provider": "fastboot", "usb": spec.fastboot.get("usb")}
    return out


def provider_for(spec: DutSpec, role: str) -> str:
    return bindings(spec).get(role, {}).get("provider") or DEFAULTS[role]


def relay_settings(spec: DutSpec) -> dict[str, Any]:
    """How this record configures its relay role: the `services.relay` block when it wrote one, else the older
    `control:` block. The same rule `ControlBackend` applies to its own `cfg`, so the FDL topology a board reports
    and the one it is driven by are one answer — whichever way the record was written."""
    explicit = (getattr(spec, "raw", None) or {}).get("services", {}).get("relay")
    return dict(explicit if isinstance(explicit, dict) else getattr(spec, "control", None) or {})


def available(role: str) -> list[str]:
    return PROVIDERS[role].names() if role in PROVIDERS else []


def describe(spec: DutSpec) -> dict[str, Any]:
    """What fills each role, for `dut list/status` and the page: names only, never a provider's internals."""
    b = bindings(spec)
    return {role: {"provider": b[role]["provider"]} for role in DEFAULTS}


def services_from_fields(spec: DutSpec, **fields: Any) -> dict[str, Any] | None:
    """Turn `dut set --console-provider …` style fields into the record's `services:` block.

    A role that is named keeps the values it has today — including the ones a record written the old way states
    under `console:`, `fastboot:` or `control:` — so naming a provider never drops the alias, the pins or the USB
    port the role is configured with.
    """
    block = dict(spec.raw.get("services") or {})
    changed = False
    for role in DEFAULTS:
        value = fields.get(f"{role}_provider")
        if not value:
            continue
        current = dict(bindings(spec).get(role) or {})
        current["provider"] = value
        block[role] = current
        changed = True
    return block if changed else None


def unknown_providers(block: dict[str, Any]) -> dict[str, str]:
    """Each role of a `services:` block that names a provider no one registered: role -> the name given."""
    return {role: cfg["provider"] for role, cfg in block.items()
            if isinstance(cfg, dict) and cfg.get("provider") and cfg["provider"] not in available(role)}


def unknown_message(bad: dict[str, str]) -> str:
    parts = [f"{role} provider {name!r}" + (f" (known: {', '.join(available(role)) or 'none'})"
                                           if available(role) else " (none is registered on this host)")
             for role, name in bad.items()]
    return "; ".join(parts)
