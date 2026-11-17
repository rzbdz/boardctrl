"""`boardctrl debug ...` — a board's debug path, through the host that owns it.

A board's debug path is a service of the host (see `dut/debug.py`): which provider it uses, what that provider
offers, and the session its server runs under the board's lease. Nothing here knows a provider or an adapter —
the commands ask the host what it has and relay it, so the same commands work for a bit-banged SWD link, a
USB-JTAG probe or whatever a site adds next.

    boardctrl debug list                     every board's debug provider on the reachable hosts
    boardctrl debug status <board>           one board's provider, what it offers, how it stands
    boardctrl debug start <board>            bring it up (the provider's server as a session under the lease)
    boardctrl debug stop <board>             stop that session
    boardctrl debug action <board> <action>  run one action the provider offers (halt, run, step, regs, …)
    boardctrl debug pipe <board>             the session's bytes on stdin/stdout, for programs

`pipe` is how boardlab's debug page reads a trace: the session's endpoint is a loopback socket on the host, so a
client that cannot reach the host's loopback reaches it the same way a console is reached (`cli/pipe.py`).
"""

from __future__ import annotations

from typing import Any

from ..core.errors import InvalidRequest
from .dutcli import _bridge, _lease, _ok, _resolve, _table
from .fleet import Fleet, HostHandle, LeaseBook


def run(fleet: Fleet, args, book: LeaseBook) -> tuple[Any, str]:
    c = args.debug_cmd
    if c is None:
        raise InvalidRequest("missing debug action", hint="boardctrl debug --help")
    if c == "list":
        return _list(fleet)
    # A board is named like everywhere else: by its name, or by the slot it sits in.
    h, dut, _slot = _resolve(fleet, args.dut, "status")
    if c == "pipe":
        return _pipe(h, dut)
    if c == "status":
        r = _ok(h, h.call("debug_status", {"dut": dut}))
        return r, _status_text(r)
    if c == "start":
        r = _ok(h, h.call("debug_start", {"dut": dut, "lease": _lease(args, book, dut)}))
        where = r.get("endpoint") or r.get("gdb_endpoint") or "up"
        return r, f"{dut}: debug session up ({r.get('provider') or 'debug'}, {where})"
    if c == "stop":
        r = _ok(h, h.call("debug_stop", {"dut": dut, "lease": _lease(args, book, dut)}))
        return r, f"{dut}: debug session stopped"
    r = _ok(h, h.call("debug_action", {"dut": dut, "action": args.action,
                                       "lease": _lease(args, book, dut)}))
    return r, _action_text(r)


def _list(fleet: Fleet) -> tuple[Any, str]:
    items: list[dict[str, Any]] = []
    for h, r in fleet.fan("debug_list"):
        if r.get("ok"):
            items += [{**d, "host": d.get("host") or h.id} for d in r["result"]]
        else:
            fleet.errors[h.id] = (r.get("error") or {}).get("message", "error")
    rows = [("DUT", "HOST", "PROVIDER", "OFFERS", "WHERE")]
    for d in items:
        rows.append((d.get("dut"), d.get("host"), d.get("provider") or "-",
                     " ".join(d.get("kinds") or []) or "-", _where(d.get("state") or {})))
    text = _table(rows) if len(rows) > 1 else "(no board has a debug path)"
    if fleet.errors:
        text += "\nunreachable: " + ", ".join(f"{k} ({v[:50]})" for k, v in fleet.errors.items())
    return items, text


def _pipe(h: HostHandle, dut: str) -> tuple[Any, str]:
    """The debug session's bytes on stdin/stdout, for programs (boardlab's debug page's trace).

    The session serves its bytes on a loopback socket of the host; a debug provider that also serves a console
    reports it as the DUT's console socket, so this is the same bridge `dut pipe` uses, and a provider whose
    session is not a console (no `console_socket`) is reported as such rather than guessed at.
    """
    st = _ok(h, h.call("dut_status", {"dut": dut}))
    sock = st.get("console_socket")
    if not sock:
        raise InvalidRequest(f"{h.id} reports no stream for {dut}'s debug session",
                             hint=f"this board's debug path serves no byte stream (see `boardctrl debug status {dut}`)")
    if not st.get("console_up"):
        raise InvalidRequest(f"{dut}'s debug session is not running on {h.id}",
                             hint=f"start it under the board's lease: boardctrl debug start {dut}")
    # A trace is read, not typed into: the session stays open after whoever runs the pipe stops writing.
    return _bridge(h, sock, hold=True), None


def _where(state: dict[str, Any]) -> str:
    for key in ("endpoint", "gdb_endpoint", "gdb_port"):
        if state.get(key):
            return str(state[key])
    if state.get("error"):
        return "error"
    return "-"


def _status_text(r: dict[str, Any]) -> str:
    kinds = ", ".join(r.get("kinds") or []) or "nothing"
    head = f"{r.get('dut') or ''}: {r.get('provider') or '-'} ({kinds})"
    state = r.get("state") or {}
    if not state:
        return head
    lines = [head]
    for key, value in state.items():
        lines.append(f"  {key}: " + (value if isinstance(value, str) else str(value)))
    return "\n".join(lines)


def _action_text(r: dict[str, Any]) -> str:
    parts = [f"{k}: {v}" for k, v in r.items() if k not in ("dut", "action")]
    return "\n".join(parts) or f"{r.get('dut')}: {r.get('action')} done"


def add_parser(sub) -> None:
    d = sub.add_parser("debug", help="a board's debug path: list, status, start, stop, action, pipe")
    ds = d.add_subparsers(dest="debug_cmd", metavar="<action>")
    ds.add_parser("list", help="every board's debug provider on the reachable hosts")
    for name, hlp in (("status", "one board's provider, what it offers, how it stands"),
                      ("start", "bring it up (its server runs as a session under the board's lease)"),
                      ("stop", "stop that session")):
        x = ds.add_parser(name, help=hlp)
        x.add_argument("dut", help="board or slot")
        if name != "status":
            x.add_argument("--lease")
    a = ds.add_parser("action", help="run one action the provider offers (halt, run, step, regs, …)")
    a.add_argument("dut", help="board or slot")
    a.add_argument("action", help="an action of the provider (`boardctrl debug status`)")
    a.add_argument("--lease")
    p = ds.add_parser("pipe", help="the session's bytes on stdin/stdout, for programs (no lease)")
    p.add_argument("dut", help="board or slot")


__all__ = ["add_parser", "run"]
