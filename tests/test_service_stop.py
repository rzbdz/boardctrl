"""boardctrld stops promptly on SIGTERM even while clients hold streams open, so systemd never has to kill it
(which would cut off the service's own shutdown). Fakes only: the made-up mock host, on a loopback port."""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from core.events import EventBus
from core.jobs import JobManager

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "tests" / "fixtures" / "site" / "hosts" / "dev-mock.yaml"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _open_stream(port: int, path: str) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n".encode())
    head = s.recv(4096)
    assert b"200" in head.split(b"\r\n", 1)[0], head
    return s


def test_sigterm_ends_open_event_streams_and_stops_within_seconds(tmp_path):
    port = _free_port()
    env = {**os.environ, "BOARDCTRL_STATE_DIR": str(tmp_path), "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen([sys.executable, "-m", "frontends.server", "--config", str(MOCK),
                             "--bind", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(200):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        streams = [_open_stream(port, "/api/v1/events") for _ in range(2)]
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15)
        took = time.monotonic() - t0
        out = proc.stdout.read().decode(errors="replace")
        assert rc in (0, -signal.SIGTERM), out     # uvicorn re-raises the SIGTERM it handled; systemd counts it clean
        # Without ending the streams, uvicorn waits for them until systemd kills it (TimeoutStopSec).
        assert took < 4, f"stop took {took:.1f}s\n{out}"
        assert "timeout graceful shutdown exceeded" not in out, out
        for s in streams:
            s.close()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_long_poll_on_a_job_returns_when_the_service_stops():
    jobs = JobManager(EventBus())
    release = threading.Event()
    job = jobs.start("flash", "flash board-1", lambda log: release.wait(30))
    stopping = threading.Event()
    threading.Timer(0.3, stopping.set).start()
    t0 = time.monotonic()
    got = jobs.wait(job.id, 30, stopping)
    assert time.monotonic() - t0 < 3
    assert got.status == "running"
    release.set()


def test_a_session_stream_ends_when_the_service_stops():
    """A console client reading a session's stream lets the service stop; the session goes on in its own unit."""
    import types

    import uvicorn
    from fastapi import FastAPI

    from frontends.server.streams import include_session_routes

    quiet = socket.socket()          # a session server that says nothing
    quiet.bind(("127.0.0.1", 0))
    quiet.listen()
    endpoint = f"127.0.0.1:{quiet.getsockname()[1]}"
    session = types.SimpleNamespace(endpoint=endpoint)
    sessions = types.SimpleNamespace(get=lambda dut, role: session, list=lambda: [])
    stopping = threading.Event()
    app = FastAPI()
    include_session_routes(app, lambda: sessions, stopping)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        s = _open_stream(port, "/api/v1/sessions/board-1/console/stream")
        s.settimeout(5)
        stopping.set()
        t0 = time.monotonic()
        data = b""
        while not data.endswith(b"0\r\n\r\n"):     # the chunked body's end: the server closed the stream
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        assert time.monotonic() - t0 < 3
        s.close()
    finally:
        server.should_exit = True
        quiet.close()
