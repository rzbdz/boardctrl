"""The names this software answers to: the command `boardctrl`, the daemon `boardctrld`, and `BOARDCTRL_*` settings.

A board host's program describes board control next to boardlab's lab coordination. This is one name in every place a
person or a script meets it: the command, the daemon, its unit, its state and site directories, and its environment.
"""

from __future__ import annotations

import os
import sys

CMD = "boardctrl"
DAEMON = "boardctrld"


def get(name: str, default: str | None = None) -> str | None:
    """A setting from the environment, by its one name."""
    return os.environ.get(name, default)


def setenv(name: str, value: str) -> None:
    """Export a setting for a child (a hook, a program) to read."""
    os.environ[name] = value


def note(old: str, new: str) -> None:
    """Say once, on stderr, that an old spelling was used (never for a program's stdout, which is its result)."""
    global _warned
    if _warned:
        return
    _warned = True
    if os.environ.get("BOARDCTRL_QUIET"):
        return
    print(f"{new} is the name now; {old} still works and will keep working. See `{new} --help`.", file=sys.stderr)
