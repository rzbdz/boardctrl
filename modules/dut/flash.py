"""The `flash` role: writing an image or a package to a board.

Two kinds of flasher fill it. The built-in `hook` provider is an executable in the site's `hooks/flash/`, run by the
host's flash job on the unpacked package — how an SDK's own flash tool is used. A site plugin can register another
provider (a debug adapter's image writer, a vendor's programmer) that takes a local file and writes it itself;
the host checks the lease, resolves the package and reports the job the same way, so `boardctrl dut flash` and the
page do not know which kind they are talking to.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from core.errors import InvalidRequest
from core.providers import Field, PROVIDERS


class FlashProvider(ABC):
    """One board's flasher. `config` is the role's block of the DUT record (`{"provider": ..., ...}`)."""

    provider_name = "abstract"
    title = ""
    about = ""

    def __init__(self, spec: Any, config: dict[str, Any] | None = None):
        self.spec = spec
        self.config = dict(config or {})

    @classmethod
    def fields(cls) -> list[Field]:
        """The settings this provider reads from the DUT record (`dut set --flash-…` and the page offer them)."""
        return []

    @abstractmethod
    def flash(self, package: Path, *, log: Callable[[str], None], timeout: float) -> dict[str, Any]:
        """Write `package` (a local file) to the board and say what happened. Never retried by the host."""
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider_name, "title": self.title}


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
