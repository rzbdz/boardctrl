"""Built-in providers of the `debug` role.

Only `none` is built in: a board with no debug path says so in the same words everywhere. Real providers (a
GPIO bit-banged SWD link, a USB-JTAG adapter talking to a debug server) come from site plugins, which register
`debug/<name>` themselves — see `bsp/core/providers.py` and `docs/porting.md`.
"""

from __future__ import annotations

from typing import Any

from ..core.providers import PROVIDERS
from .debug import NoDebug


def _none(ctx: Any, spec: Any, services: Any) -> NoDebug:
    return NoDebug(spec, {}, services)


PROVIDERS["debug"].register("none", _none)
