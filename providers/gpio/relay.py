"""The `relay` role's built-in provider: a board's control lines switched by this host's gpio service."""

from __future__ import annotations

from typing import Any

from core.dut.control import GpioRelayControl

__all__ = ["GpioRelayControl"]
