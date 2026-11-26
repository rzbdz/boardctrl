"""`human` module: a person resets the board, in place of a relay.

The relay role's `human` provider (alias `manual`, its name before the role had one) asks for an action on the
board's page and in `dut status`, then waits for `dut confirm <dut>` or for the board to show the requested state on
its own. The whole request has one window (`human.timeout_s`, 5 minutes by default): a person has to see the
request, walk to the board and press it, so a relay's 30 seconds would fail them.

The request needs the host's `dut_attention` service (the DUT manager publishes it); the provider reads it lazily,
so the module loads before the DUT manager exists.
"""

from __future__ import annotations

import time
from typing import Any

from core.errors import HardwareTimeout
from core.jobs import current_token
from core.dut.contracts import ControlBackend, Log, Ready

TITLE = "A person's hands"
# What it reads: a request waits on the DUT manager's attention service. It is published by the kernel before any
# provider starts, so the declaration is satisfied on every host that has a DUT manager — which is every host.
REQUIRES = ("dut_attention",)

# How long a board reset by a person waits for them: long enough to see the request and walk to the board.
HUMAN_TIMEOUT_S = 300.0


def _span(seconds: float) -> str:
    return f"{seconds / 60:g} minutes" if seconds >= 120 else f"{seconds:g}s"


class HumanControl(ControlBackend):
    """`services.relay.provider: human`; its own settings under the record's `services.relay.human` block."""

    needs_human = True
    human_timeout_s = HUMAN_TIMEOUT_S

    def _ask(self, action: str, message: str, log: Log, ready: Ready | None) -> None:
        att = self._services("dut_attention")
        timeout = self.human_timeout()
        req = att.request(self.dut.id, action, message)
        ev = req["event"]
        # The request id is on the line: boardd and the page carry it back with the confirmation.
        log(f"ACTION NEEDED: {message} (request {req['id']})")
        log(f"waiting for `boardctrl dut confirm {self.dut.id}` (request {req['id']})"
            + (" or for the board to show it on its own" if ready else "") + f" (timeout {timeout:g}s)")
        deadline = time.monotonic() + timeout
        token = current_token()          # a cancelled job stops waiting for the person, and the request is cleared
        try:
            while True:
                if token.wait(ev, 0.5):
                    log(f"{action}: confirmed by a person")
                    return
                seen = ready() if ready else None
                if seen:
                    # Answered without a person: a click on 「我做好了」 for this request then resolves here too.
                    att.detected(self.dut.id, req, seen)
                    log(f"{action}: detected {seen}")
                    return
                if time.monotonic() > deadline:
                    att.expired(self.dut.id, req)
                    raise HardwareTimeout(
                        f"{self.dut.id} is reset by a person (no relay), and nobody did '{action}' within "
                        f"{_span(timeout)}: {message}",
                        hint=f"ask a person at the board to do it (it was asked for on the board's page), then "
                             f"`boardctrl dut confirm {self.dut.id}`; or retry when someone is there. "
                             f"services.relay.human.timeout_s sets the wait")
        finally:
            att.clear(self.dut.id, ev)

    def _text(self, key: str, default: str) -> str:
        return (self.cfg.get("human") or {}).get(key) or default

    def reset(self, log: Log, ready: Ready | None = None) -> None:
        self._ask("reset", self._text("reset", f"press RESET on {self.dut.id} ({self.dut.board or 'board'})"),
                  log, ready)

    def enter_fdl(self, log: Log, ready: Ready | None = None) -> None:
        strategy = (self.cfg.get("fdl") or {}).get("strategy", "strapped")
        default = (f"press RESET on {self.dut.id} ({self.dut.board or 'board'}); it boots into download mode"
                   if strategy == "strapped" else
                   f"put {self.dut.id} ({self.dut.board or 'board'}) into download mode: hold the "
                   f"download/BOOT button, press RESET, then release")
        self._ask("enter download mode", self._text("fdl", default), log, ready)

    def describe(self) -> dict[str, Any]:
        att = self._services("dut_attention")
        return {"backend": "human", "fdl_strategy": (self.cfg.get("fdl") or {}).get("strategy", "strapped"),
                "waiting_for": att.view(self.dut.id)}


def setup(ctx: Any, api: Any, config: dict[str, Any]) -> None:
    def factory(_ctx: Any, spec: Any, services: Any) -> HumanControl:
        return HumanControl(spec, services)

    api.provider("relay", "human", factory, title="a person's hands",
                 about="Asks a person at the board to press the buttons, then waits for them or for the board")
    api.provider("relay", "manual", factory)     # the name records used before the role had one
