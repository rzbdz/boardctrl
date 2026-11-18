"""Requests for a person to act on a board, and their confirmations (one per DUT).

A module that waits for a person publishes an `Attention` as the host's `dut_attention` service; the DUT manager
asks and confirms. Every request has an id that travels with it (page, CLI, `attention` event). A confirmation names
the request it answers: the same id twice is idempotent — a person clicking 「我做好了」 after the host already saw the
board arrive is told it was confirmed, not that nothing was waiting. A confirmation without an id answers whatever
is waiting for that DUT now. Answered requests are kept (bounded) so a late or repeated confirmation still
resolves to what it answered.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

from core.errors import NotFound


class Attention:
    HISTORY = 32

    def __init__(self, publish: Callable[..., None] = lambda *a, **k: None):
        self._pending: dict[str, dict[str, Any]] = {}
        self._answered: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._publish = publish

    def request(self, dut: str, action: str, message: str) -> dict[str, Any]:
        ev = threading.Event()
        req = {"id": uuid.uuid4().hex[:12], "action": action, "message": message, "since": time.time(), "event": ev}
        with self._lock:
            older = self._pending.get(dut)
            if older and older["id"] != req["id"]:
                # Asked again before the first was answered: the older request answers as superseded, never NotFound.
                self._answered.setdefault(dut, {})[older["id"]] = {
                    "dut": dut, "request": older["id"], "confirmed": older["action"],
                    "waited_s": round(time.time() - older["since"], 1), "repeat": False, "how": "superseded"}
                self._trim(dut)
            self._pending[dut] = req
        self._publish("attention", dut=dut, id=req["id"], action=action, message=message)
        return req

    def confirm(self, dut: str, note: str = "", request: str | None = None) -> dict[str, Any]:
        with self._lock:
            p = self._pending.get(dut)
            if request:
                # The request id is the trace: an answered one (the board arrived first, or a second click)
                # resolves to what it answered instead of failing.
                done = self._answered.get(dut, {}).get(request)
                if done:
                    return {**done, "repeat": True}
                if not p or p["id"] != request:
                    raise NotFound(f"{dut}: request {request} is not waiting for a person",
                                   hint="the page's 「我做好了」 goes stale once the board was already seen; "
                                        "`bsp dut status` shows what is waiting now, if anything")
            if not p:
                raise NotFound(f"{dut}: nothing is waiting for a person",
                               hint="confirm only after bsp asked for an action (see `bsp dut status`)")
            result = {"dut": dut, "request": p["id"], "confirmed": p["action"],
                      "waited_s": round(time.time() - p["since"], 1), "repeat": False, "how": "by a person"}
            self._answered.setdefault(dut, {})[p["id"]] = result
            self._trim(dut)
        p["note"] = note
        p["event"].set()
        self._publish("attention.confirmed", dut=dut, id=p["id"], action=p["action"], note=note)
        return result

    def clear(self, dut: str, ev: threading.Event) -> None:
        with self._lock:
            if self._pending.get(dut, {}).get("event") is ev:
                del self._pending[dut]

    def detected(self, dut: str, req: dict[str, Any], evidence: str) -> dict[str, Any]:
        """The board showed the requested state on its own: the request is answered without a person."""
        return self._answer(dut, req, "detected", evidence)

    def expired(self, dut: str, req: dict[str, Any]) -> dict[str, Any]:
        """Nobody acted in time: kept so a late confirmation says the wait ended instead of 'nothing waiting'."""
        return self._answer(dut, req, "expired")

    def _answer(self, dut: str, req: dict[str, Any], how: str, evidence: str | None = None) -> dict[str, Any]:
        result = {"dut": dut, "request": req["id"], "confirmed": req["action"],
                  "waited_s": round(time.time() - req["since"], 1), "repeat": False, "how": how,
                  **({"evidence": evidence} if evidence else {})}
        with self._lock:
            self._answered.setdefault(dut, {})[req["id"]] = result
            self._trim(dut)
        return result

    def view(self, dut: str) -> dict[str, Any] | None:
        p = self._pending.get(dut)
        return {k: p[k] for k in ("id", "action", "message")} | {"waiting_s": round(time.time() - p["since"])} if p else None

    def _trim(self, dut: str) -> None:
        kept = self._answered.get(dut) or {}
        for stale in list(kept)[:-self.HISTORY]:
            del kept[stale]
