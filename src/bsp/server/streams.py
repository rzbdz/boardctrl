"""HTTP access to sessions: what is running, and the bytes a session serves.

A session's server speaks its own protocol on a Unix socket or a loopback TCP port (serial-mux, a trace channel,
a debug adapter). A client that cannot reach the host's loopback — boardd, a browser, a person's laptop — reads
it here instead: the stream is the raw bytes, framed as SSE chunks, and input goes back the same way. This is
how a console or a trace becomes reachable without exposing a port.

    GET  /api/v1/sessions                what is running (DUT, role, provider, endpoint, since when)
    GET  /api/v1/sessions/<dut>/<role>/stream    the session's output (text/event-stream, base64 `data:`)
    POST /api/v1/sessions/<dut>/<role>/input     write bytes to it (base64 `data`)
"""

from __future__ import annotations

import base64
import queue
import socket
import threading
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI
from fastapi.responses import StreamingResponse

from ..core.errors import InvalidRequest, NotFound
from ..core.sessions import Sessions

CHUNK = 32768


def sessions_router(get_sessions) -> Any:
    """Routes over the host's sessions: read through `get_sessions()` so a host that swaps the service is
    followed (a test's fake process table, a service restarted in place)."""
    from fastapi import APIRouter

    router = APIRouter(tags=["sessions"])

    @router.get("/api/v1/sessions", summary="Sessions running on this host")
    async def listing() -> dict[str, Any]:
        return {"ok": True, "sessions": [s.to_dict() for s in get_sessions().list()]}

    def _session(dut: str, role: str):
        s = get_sessions().get(dut, role)
        if s is None:
            raise NotFound(f"{dut}: no {role} session is running",
                           hint=f"start one: POST /api/v1/tools/debug_start for role debug")
        return s

    @router.get("/api/v1/sessions/{dut}/{role}/stream", summary="A session's output as it comes")
    async def stream(dut: str, role: str):
        s = _session(dut, role)
        if not s.endpoint:
            raise InvalidRequest(f"{dut}: its {role} session serves no stream")

        def events():
            # The server is read on its own thread; this generator only forwards what it got, so a client
            # that stops reading does not hold the session's socket.
            q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=256)
            stop = threading.Event()

            def pump():
                try:
                    conn = _connect(s.endpoint or "")
                    try:
                        while not stop.is_set():
                            data = conn.recv(CHUNK)
                            if not data:
                                break
                            q.put(data)
                    finally:
                        conn.close()
                except OSError:
                    pass
                finally:
                    q.put(None)      # the server closed: this stream ends with it

            threading.Thread(target=pump, name=f"session-{dut}-{role}", daemon=True).start()
            yield "event: ready\ndata: {}\n\n"
            idle = 0.0
            while not stop.is_set():
                try:
                    data = q.get(timeout=0.2)
                    if data is None:
                        break
                except queue.Empty:
                    idle += 0.2
                    if idle >= 15:
                        yield ": ping\n\n"
                        idle = 0.0
                    continue
                idle = 0.0
                yield f"data: {base64.b64encode(data).decode()}\n\n"
            stop.set()

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @router.post("/api/v1/sessions/{dut}/{role}/input", summary="Write bytes to a session")
    async def input_(dut: str, role: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        s = _session(dut, role)
        data = payload.get("data")
        if not isinstance(data, str):
            raise InvalidRequest("send {\"data\": \"<base64>\"}")
        raw = base64.b64decode(data)
        try:
            conn = _connect(s.endpoint or "")
            try:
                conn.sendall(raw)
            finally:
                conn.close()
        except OSError as e:
            raise InvalidRequest(f"{dut}: could not write to its {role} session: {e}")
        return {"ok": True, "dut": dut, "role": role, "bytes": len(raw)}

    return router


def _connect(endpoint: str, timeout: float = 5.0) -> socket.socket:
    """A Unix socket path or `host:port` on loopback."""
    if endpoint.startswith("/") or Path(endpoint).exists():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(endpoint)
        return s
    host, _, port = endpoint.rpartition(":")
    s = socket.create_connection((host or "127.0.0.1", int(port)), timeout=timeout)
    s.settimeout(None)
    return s


def include_session_routes(app: FastAPI, get_sessions) -> None:
    app.include_router(sessions_router(get_sessions))
