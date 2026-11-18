"""The names this software answers to: `boardctrl` (and `boardctrld`), with `bsp` still working.

The command, the daemon and the environment were `bsp`/`bspd`; they describe board control next to boardlab's lab
coordination, so they are `boardctrl`/`boardctrld` now. Every old spelling keeps working and says once, on stderr,
what to use instead — a script, a unit file or a site written for `bsp` does not have to change before it works.
Environment variables are read as `BOARDCTRL_*` first, then `BSP_*`.
"""

from __future__ import annotations

import os
import sys

CMD = "boardctrl"
DAEMON = "boardctrld"
OLD_CMD = "bsp"
OLD_DAEMON = "bsp-hostd"
#: Environment variables, new name -> the `BSP_*` spellings still honoured.
ENV = {
    "BOARDCTRL_SITE": "BSP_SITE",
    "BOARDCTRL_TOKEN": "BSP_TOKEN",
    "BOARDCTRL_STATE_DIR": "BSP_STATE_DIR",
    "BOARDCTRL_URL": "BSP_URL",
    "BOARDCTRL_REMOTE": "BSP_REMOTE",
    "BOARDCTRL_REGISTRY": "BSP_REGISTRY",
    "BOARDCTRL_REGISTRY_URL": "BSP_REGISTRY_URL",
    "BOARDCTRL_HOOKS_PATH": "BSP_HOOKS_PATH",
    "BOARDCTRL_LEASE": "BSP_LEASE",
    "BOARDCTRL_HOLDER": "BSP_HOLDER",
    "BOARDCTRL_CONFIG": "BSP_CONFIG",
    "BOARDCTRL_STATELESS": "BSP_STATELESS",
    "BOARDCTRL_SSH_OPTIONS": "BSP_SSH_OPTIONS",
    "BOARDCTRL_BIND": "BSP_BIND",
    "BOARDCTRL_PORT": "BSP_PORT",
}
_warned = False


def get(new: str, default: str | None = None) -> str | None:
    """A setting by its new name, else its `BSP_*` spelling, else ``default``."""
    value = os.environ.get(new)
    if value is not None:
        return value
    old = ENV.get(new, "")
    return os.environ.get(old, default) if old else default


def setenv(new: str, value: str) -> None:
    """Export a setting under both names, so a hook or a child that still reads `BSP_*` sees it."""
    os.environ[new] = value
    old = ENV.get(new)
    if old:
        os.environ.setdefault(old, value)


def note(old: str, new: str) -> None:
    """Say once, on stderr, that an old spelling was used (never for a program's stdout, which is its result)."""
    global _warned
    if _warned:
        return
    _warned = True
    if os.environ.get("BOARDCTRL_QUIET"):
        return
    print(f"{new} is the name now; {old} still works and will keep working. See `{new} --help`.", file=sys.stderr)
