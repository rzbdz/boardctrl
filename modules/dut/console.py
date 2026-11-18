"""The `console` role: a byte stream with history, one-shot commands, and its log.

The provider owns where the bytes come from (a serial-mux daemon on a USB-UART, a trace channel over a
debug adapter, a network console) and how a command is typed and its answer recognised. Everything else —
leases, busy guards, the log range API, the page — talks to this interface, so a board's console can be
anything that can say these things.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Callable


class ConsoleProvider(ABC):
    """One board's console. `config` is the role's block of the DUT record (`{"provider": ..., ...}`)."""

    provider_name = "abstract"

    def __init__(self, spec: Any, config: dict[str, Any] | None = None):
        self.spec = spec
        self.config = dict(config or {})

    # A marker for a mark() the provider cannot read back (a transient stream): reads since then are empty.
    def mark(self) -> Any:
        return None

    def read_since(self, mark: Any) -> str:
        return ""

    @abstractmethod
    def tail(self, lines: int = 50, grep: str | None = None) -> list[str]:
        """The last lines of the console (passive: no keystrokes)."""

    @abstractmethod
    def range(self, since: str | None, until: str | None, grep: str | None = None, regex: bool = False,
              limit: int = 20000, first: bool = False) -> dict[str, Any]:
        """Lines stamped from `since` to `until`, optionally searched (see `hostio.SerialConsole.range`)."""

    @abstractmethod
    def send(self, text: str, pattern: str | None = None, fail: str | None = None, timeout: float = 60,
             mark: Any = None) -> dict[str, Any]:
        """Type `text`, wait for `pattern`, return what came back (and whether it failed)."""

    @abstractmethod
    def wait(self, pattern: str | None, fail: str | None = None, timeout: float = 60, mark: Any = None) -> str:
        """Wait for `pattern` on what the console prints since `mark`."""

    @abstractmethod
    def run(self, command: str, expect: str, timeout: float) -> dict[str, Any]:
        """Run one shell command and return its output."""

    # ------------------------------------------------------------ liveness
    def up(self) -> bool:
        """Whether the console is reachable now (a daemon is running, a channel is open)."""
        return True

    def socket(self) -> str | None:
        """Where a protocol client (serial-mux) connects, when the provider serves one."""
        return None

    def attach(self) -> str | None:
        """A command a person runs on the host to type into it by hand."""
        return None

    def ensure(self) -> str:
        """Make sure it is reachable (start a daemon, open a channel); returns how it was served."""
        return "running"

    def stop(self) -> None:
        """Stop what `ensure` started."""

    def describe(self) -> dict[str, Any]:
        return {"provider": getattr(self, "provider_name", type(self).__name__)}
