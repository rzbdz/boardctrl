"""The `relay` role's built-in `gpio-relay` provider: a board's control lines on this host's gpio service.

The relay role is filled per board by a provider (`services.relay.provider`); the others are modules of their own
(`modules/human` for a person's buttons) or a site's. A record written before the role had a name says the same
thing as `control.backend`, which `build_relay` reads.

FDL strategies (control.fdl.strategy):
    strapped        the board always boots to BROM download mode: reset == enter FDL
    button          hold the FDL line, pulse reset, release (needs control.fdl.pin)
    unset           not known yet (a probe saw no download mode after a reset): entering FDL says what to set
"""

from __future__ import annotations

import time
from typing import Any, Callable

from core.errors import InvalidRequest, BspError
from core.providers import PROVIDERS
from core.dut.contracts import ControlBackend, Ready
from core.dut.records import DutSpec

__all__ = ["GpioRelayControl", "build_relay"]

Log = Callable[[str], None]


class GpioRelayControl(ControlBackend):
    """Uses the host's gpio service. Lines are registered as aliases
    `<dut>/reset` and `<dut>/fdl` (pass-through rule: callers may use either)."""

    def _gpio(self):
        return self._services("gpio")

    def _alias(self, role: str) -> str:
        return f"{self.dut.id}/{role}"

    def setup(self) -> None:
        gpio = self._gpio()
        for role in ("reset", "fdl"):
            line = self.cfg.get(role) or {}
            if not line.get("pin"):
                continue
            changes = {k: line[k] for k in ("mode", "active_low", "drive") if k in line}
            changes.setdefault("mode", "relay")
            gpio.configure(line["pin"], changes)
            gpio.alias_add(self._alias(role), line["pin"], tags=f"dut,{self.dut.id},{role}",
                           owner="dut", note=f"{self.dut.id} {role} line", replace=True)
        if self.cfg.get("reset", {}).get("pin"):
            # idle = released; for push-pull relay mode that drives the inactive level
            gpio.switch(self._alias("reset"), "off")

    def _roles(self) -> list[str]:
        """The lines `reset` and `enter_fdl` drive: the reset line, and the FDL line when a button strategy holds it."""
        fdl = self.cfg.get("fdl") or {}
        roles = ["reset"] if (self.cfg.get("reset") or {}).get("pin") else []
        if fdl.get("strategy", "strapped") == "button" and fdl.get("pin"):
            roles.append("fdl")
        return roles

    def preflight(self) -> None:
        """Each line this board's reset and download mode drive can be driven now (no pulse, no level change)."""
        gpio = self._gpio()
        for role in self._roles():
            pin = self.cfg[role]["pin"]
            try:
                gpio.check(self._alias(role))
            except BspError as e:
                raise type(e)(f"{self.dut.id}: its {role} line ({pin}) cannot be driven now: {e.message}",
                              hint=(e.hint + "; " if e.hint else "")
                                   + f"nothing was reset or fetched; retry once the line is free (`boardctrl gpio get {pin}`)",
                              details={"dut": self.dut.id, "role": role, "pin": pin, "cause": e.to_dict()}) from e

    def reset(self, log: Log, ready: Ready | None = None) -> None:
        line = self.cfg.get("reset") or {}
        if not line.get("pin"):
            raise InvalidRequest(f"{self.dut.id}: control.reset.pin is not configured")
        ms = int(line.get("pulse_ms", 1000))
        log(f"reset: pulse {self._alias('reset')} ({line['pin']}) active for {ms} ms")
        self._gpio().pulse(self._alias("reset"), ms, None, True, False)

    def _fdl_button(self, log: Log) -> None:
        if not (self.cfg.get("fdl") or {}).get("pin"):
            raise InvalidRequest(f"{self.dut.id}: fdl strategy `button` needs control.fdl.pin",
                                 hint=f"boardctrl dut set {self.dut.id} --fdl-pin <pin>, or --fdl strapped if a reset lands in download mode")
        gpio = self._gpio()
        log(f"FDL: hold {self._alias('fdl')}, reset, release")
        gpio.switch(self._alias("fdl"), "on")
        try:
            self.reset(log)
            time.sleep(float(self.cfg["fdl"].get("hold_after_reset_s", 1.0)))  # strap sampling time
        finally:
            gpio.switch(self._alias("fdl"), "off")

    def describe(self) -> dict[str, Any]:
        gpio = self._gpio()
        out = {"backend": "gpio-relay"}
        for role in ("reset", "fdl"):
            if (self.cfg.get(role) or {}).get("pin"):
                try:
                    snap = gpio.get(self._alias(role))
                    out[role] = {"alias": self._alias(role), "pin": snap["target"], "state": snap.get("state")}
                except BspError as e:
                    out[role] = {"error": e.message}
        out["fdl_strategy"] = (self.cfg.get("fdl") or {}).get("strategy", "strapped")
        return out


def build_relay(name: str, dut: DutSpec, services: Callable[[str], Any],
                cfg: dict[str, Any] | None = None) -> ControlBackend:
    """The relay role's provider: the built-in gpio relay, or one a module registered."""
    if name == "gpio-relay":
        return GpioRelayControl(dut, services, cfg)
    return PROVIDERS["relay"].get(name)(None, dut, services)
