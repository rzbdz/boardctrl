"""Character-device backend via libgpiod v2 python bindings (``pip install gpiod``).

Lines stay requested while the service runs; on release the kernel decides the
level, so prefer ``sysfs`` when a state must survive service restarts.
"""

from __future__ import annotations

import glob
import os
import threading
from pathlib import Path

from ...core.errors import HardwareError
from . import BACKENDS
from .base import GpioBackend, LineStatus


@BACKENDS.register("gpiod")
class GpiodBackend(GpioBackend):
    name = "gpiod"

    def __init__(self, consumer: str = "bsp", chips: dict[str, int] | None = None):
        try:
            import gpiod  # noqa: F401
        except ImportError as e:
            raise HardwareError("python 'gpiod' (libgpiod v2) is not installed", hint="pip install 'bsp-dev-loop[gpiod]'") from e
        self.consumer = consumer
        self._lock = threading.Lock()
        self._requests: dict[int, object] = {}
        # chip path -> base. Explicit config wins; else match chip labels against sysfs bases.
        self._chips = chips or self._discover()
        if not self._chips:
            raise HardwareError("no GPIO chips discovered")

    @staticmethod
    def _discover() -> dict[str, int]:
        import gpiod

        label_base: dict[str, int] = {}
        for d in glob.glob("/sys/class/gpio/gpiochip*"):
            try:
                label = Path(d, "label").read_text().strip()
                label_base[label] = int(Path(d, "base").read_text())
            except OSError:
                continue
        chips: dict[str, int] = {}
        for path in sorted(glob.glob("/dev/gpiochip*")):
            with gpiod.Chip(path) as c:
                label = c.get_info().label
            if label in label_base:
                chips[path] = label_base[label]
        return chips

    def _locate(self, gpio: int) -> tuple[str, int]:
        import gpiod

        for path, base in self._chips.items():
            with gpiod.Chip(path) as c:
                n = c.get_info().num_lines
            if base <= gpio < base + n:
                return path, gpio - base
        raise HardwareError(f"GPIO{gpio} not found on any chip", details={"chips": self._chips})

    def _settings(self, output: bool, value: int = 0):
        from gpiod.line import Direction, Value

        import gpiod

        if output:
            return gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE if value else Value.INACTIVE)
        return gpiod.LineSettings(direction=Direction.INPUT)

    def locate(self, gpio: int) -> tuple[str, int]:
        """The chip path and line offset a Linux GPIO number is on, for a service that drives it directly."""
        return self._locate(gpio)

    def chip_label(self, gpio: int) -> str:
        """The chip's device-tree label (`d4019000.gpio-bank3`), for configs that name chips rather than numbers."""
        import gpiod

        path, _ = self._locate(gpio)
        with gpiod.Chip(path) as c:
            return c.get_info().label

    def fast_line(self, gpio: int):
        """A line held as output and driven without the guard's thread hop (bit-banged buses)."""
        return _GpiodFastLine(self, gpio)

    def _apply(self, gpio: int, output: bool, value: int = 0) -> None:
        import gpiod

        with self._lock:
            req = self._requests.get(gpio)
            path, offset = self._locate(gpio)
            try:
                if req is None:
                    self._requests[gpio] = gpiod.request_lines(
                        path, consumer=self.consumer, config={offset: self._settings(output, value)}
                    )
                else:
                    req.reconfigure_lines({offset: self._settings(output, value)})
            except OSError as e:
                raise HardwareError(f"GPIO{gpio}: {os.strerror(e.errno) if e.errno else e}") from e

    def set_output(self, gpio: int, value: int) -> None:
        self._apply(gpio, True, value)

    def set_input(self, gpio: int) -> None:
        self._apply(gpio, False)

    def status(self, gpio: int) -> LineStatus:
        import gpiod
        from gpiod.line import Direction, Value

        path, offset = self._locate(gpio)
        req = self._requests.get(gpio)
        with gpiod.Chip(path) as c:
            li = c.get_line_info(offset)
        direction = "out" if li.direction == Direction.OUTPUT else "in"
        if req is not None:
            v = req.get_value(offset)
            return LineStatus(gpio, True, direction, 1 if v == Value.ACTIVE else 0, self.consumer)
        return LineStatus(gpio, li.used, direction if li.used else None, None, li.consumer or None)

    def free(self, gpio: int) -> None:
        with self._lock:
            req = self._requests.pop(gpio, None)
        if req is not None:
            path, offset = self._locate(gpio)
            req.reconfigure_lines({offset: self._settings(False)})
            req.release()

    def close(self) -> None:
        # dropping requests would let lines float; keep them until process exit
        pass

    def info(self) -> dict:
        return {"name": self.name, "chips": self._chips, "requested": sorted(self._requests)}


class _GpiodFastLine:
    """A line requested as output on its own, written straight through the request (no guard, no lock hop)."""

    def __init__(self, backend: "GpiodBackend", gpio: int):
        import gpiod

        self.gpio = gpio
        self.path, self.offset = backend._locate(gpio)
        self._req = gpiod.request_lines(self.path, consumer=backend.consumer,
                                        config={self.offset: backend._settings(True, 0)})

    def write(self, value: int) -> None:
        from gpiod.line import Value

        self._req.set_value(self.offset, Value.ACTIVE if value else Value.INACTIVE)

    def read(self) -> int:
        from gpiod.line import Value

        return 1 if self._req.get_value(self.offset) == Value.ACTIVE else 0

    def release(self) -> None:
        import gpiod
        from gpiod.line import Direction

        try:
            self._req.reconfigure_lines({self.offset: gpiod.LineSettings(direction=Direction.INPUT)})
        finally:
            self._req.release()
