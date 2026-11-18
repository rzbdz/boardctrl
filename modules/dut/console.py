"""The `console` role, re-exported from the kernel: `from core.roles import ConsoleProvider`.

The contract lives in `core/roles.py` because a site plugin implements it without importing a module. Everything
else — leases, busy guards, the log range API, the page — talks to that interface.
"""

from __future__ import annotations

from core.roles import ConsoleProvider

__all__ = ["ConsoleProvider"]
