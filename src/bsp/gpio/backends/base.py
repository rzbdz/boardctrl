"""GPIO backend contract (strategy). Backends speak Linux-global GPIO numbers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ...core.errors import HardwareError


@dataclass
class LineStatus:
    gpio: int
    claimed: bool                 # requested/exported (by us or someone else)
    direction: str | None = None  # "in" | "out" | None (unknown)
    value: int | None = None      # physical level 0/1, None if not readable without claiming
    owner: str | None = None      # consumer / "sysfs" when known

    def to_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "direction": self.direction,
            "value": self.value,
            "owner": self.owner,
        }


class FastLine:
    """One line driven directly, for services whose timing matters (a bit-banged bus).

    These calls are not wrapped by the timeout guard: they are the ones a caller needs to issue back to back
    (a clock edge), so a thread hop would change what is on the wire. A fast usage claims its lines, keeps them
    for its lifetime, and gives them back as inputs (`release`).
    """

    def write(self, value: int) -> None: raise NotImplementedError
    def read(self) -> int: raise NotImplementedError
    def release(self) -> None: raise NotImplementedError


class _PlainFastLine(FastLine):
    """The fallback: a backend with no direct handle drives and reads through its own methods."""

    def __init__(self, backend: "GpioBackend", gpio: int):
        self.backend = backend
        self.gpio = gpio

    def write(self, value: int) -> None:
        self.backend.set_output(self.gpio, 1 if value else 0)

    def read(self) -> int:
        line = self.backend.status(self.gpio)
        return int(line.value or 0)

    def release(self) -> None:
        self.backend.set_input(self.gpio)


class GpioBackend(ABC):
    """Physical-level line access. Polarity, drive emulation and semantics
    (relay/button) live in channels, not here."""

    name: str = "abstract"

    def fast_line(self, gpio: int) -> FastLine:
        """A direct handle on one line; backends that can offer better override this."""
        return _PlainFastLine(self, gpio)

    def locate(self, gpio: int) -> tuple[str, int]:
        """The chip path and line offset a Linux GPIO number is on, for a caller that drives it without this service
        (a site plugin's own tool, say). Only a character-device backend can say; the others refuse."""
        raise HardwareError(f"the GPIO backend ({self.name}) cannot map GPIO{gpio} to a gpiochip line",
                            hint="drive it through this service, or configure the chip and line directly")

    @abstractmethod
    def set_output(self, gpio: int, value: int) -> None:
        """Claim if needed and drive the line as output at ``value`` (glitch-free)."""

    @abstractmethod
    def set_input(self, gpio: int) -> None:
        """Claim if needed and switch the line to input (high impedance)."""

    @abstractmethod
    def status(self, gpio: int) -> LineStatus:
        """Inspect the line without claiming it or changing anything."""

    @abstractmethod
    def free(self, gpio: int) -> None:
        """Give the line back (input first, then unexport/release)."""

    def close(self) -> None:
        """Service shutdown. Default keeps lines as they are so DUT state survives restarts."""

    def info(self) -> dict[str, Any]:
        return {"name": self.name}
