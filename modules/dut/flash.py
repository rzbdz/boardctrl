"""The `flash` role, and the built-in `hook` provider: writing an image or a package to a board.

The contract (`FlashProvider`) lives in `core/roles.py`; a site plugin implements it without importing a module.
The built-in `hook` provider is an executable in the site's `hooks/flash/`, run by the host's flash job on the
unpacked package — how an SDK's own flash tool is used.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from core.errors import InvalidRequest
from core.providers import PROVIDERS
from core.roles import FlashProvider

__all__ = ["FlashProvider", "HookFlash"]


class HookFlash(FlashProvider):
    """The built-in `hook` provider: the host's flash job, which runs `hooks/flash/<flasher>` on the package.

    Its work needs the whole flash job (download mode, unpacking, the hook's environment), so it is run by
    `DutModule.flash` itself rather than through `flash()`; the class exists so the role has a name.
    """

    provider_name = "hook"
    title = "a flash hook"
    about = "An executable in the site's hooks/flash, run on the unpacked package"

    def flash(self, package: Path, *, log: Callable[[str], None], timeout: float) -> dict[str, Any]:
        raise InvalidRequest("the hook provider is run by the host's flash job, not called directly",
                             hint="this is an internal dispatch error; report it")


PROVIDERS["flash"].register("hook", lambda ctx, spec, services: HookFlash(spec, {"provider": "hook"}))
