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
      flash:    {provider: ec-aprom, expect_version: ...}
      debug:    {provider: ec-swd, target: pico2}

The values a provider reads are its own; it describes them with `fields()` so `bsp dut set` and the page can offer
them. Role names and provider names are plain strings: a plugin adds providers without touching this file.
"""

from __future__ import annotations

from typing import Any

from ..core.providers import PROVIDERS
from .catalog import DutSpec

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


def available(role: str) -> list[str]:
    return PROVIDERS[role].names() if role in PROVIDERS else []


def describe(spec: DutSpec) -> dict[str, Any]:
    """What fills each role, for `dut list/status` and the page: names only, never a provider's internals."""
    b = bindings(spec)
    return {role: {"provider": b[role]["provider"]} for role in DEFAULTS}


def services_from_fields(spec: DutSpec, **fields: Any) -> dict[str, Any] | None:
    """Turn `dut set --console-provider …` style fields into the record's `services:` block."""
    block = dict(spec.raw.get("services") or {})
    changed = False
    for role in DEFAULTS:
        value = fields.get(f"{role}_provider")
        if not value:
            continue
        current = block.get(role)
        current = {"provider": current} if isinstance(current, str) else dict(current or {})
        current["provider"] = value
        block[role] = current
        changed = True
    return block if changed else None
