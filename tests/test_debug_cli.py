"""`boardctrl debug …`: a board's debug path as the client asks its host, and the session's bytes as a pipe.

The client knows no provider: it relays what the host says a board has, and runs the actions the host lists. These
tests drive the real CLI against a live host whose site plugin registers a debug provider — the shape a real
adapter (a GPIO bit-banged SWD link, a USB-JTAG probe) arrives in.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from core.sessions import Session, Sessions
from frontends.server.app import create_app
from frontends.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "tests" / "fixtures" / "site"

PLUGIN = '''
from core.dut.contracts import SessionDebug


class FakeDebug(SessionDebug):
    """A debug path whose server is a loopback socket that greets whoever connects to it."""

    provider_name = "fake-swd"
    kinds = ("halt", "run", "regs", "trace")
    title = "fake SWD"

    def server_command(self):
        return ["fake-server", str(self.config["socket"])]

    def ready(self):
        # A server that is up: its socket exists (what a client connects to).
        import os

        return str(self.config["socket"]) if os.path.exists(str(self.config["socket"])) else None

    def action(self, name, args=None):
        return {"halted": name in ("halt", "step"), "regs": {"pc": "0x1000"}}

    def shelf(self):
        return {"provider": self.provider_name, "title": self.title, "kinds": list(self.kinds)}

    def info(self):
        return {"provider": self.provider_name, "title": self.title, "kinds": list(self.kinds), "available": True}

    def state(self):
        s = self.sessions.get(self.dut_id, "debug")
        return {"provider": self.provider_name, **({"endpoint": s.endpoint} if s else {})}


class FakeConsole:
    """The trace channel the debug server serves: a loopback socket, as a real one is (the EC's RTT port)."""

    provider_name = "fake-rtt"
    alias = None

    def __init__(self, config):
        self.config = config

    def tail(self, lines=50, grep=None):
        return []

    def range(self, **kw):
        return {"lines": [], "matched": 0, "truncated": False}

    def up(self):
        import os

        return os.path.exists(str(self.config["socket"]))

    def attach(self):
        return None

    def socket(self):
        return str(self.config["socket"])

    def describe(self):
        return {"provider": self.provider_name, "socket": self.socket()}


def setup(ctx, api, config):
    api.provider("debug", "fake-swd", lambda c, s, sv: FakeDebug(s, {"socket": config["socket"]}, sv),
                 title="fake SWD")
    api.provider("console", "fake-rtt", lambda c, s, sv: FakeConsole({"socket": config["socket"]}),
                 title="fake trace console")
'''


class FakeSystemd:
    """Sessions are transient units: a unit's server runs here as a socket that greets its clients.

    Nothing listens before a session starts and nothing listens after it stops — which is what makes
    `pipe` before `start` a real case rather than a test artifact.
    """

    def __init__(self):
        self.units: dict[str, dict] = {}
        self.listening: dict[str, socket.socket] = {}

    def start(self, unit, command, env=None):
        self.units[unit] = {"command": command, "active": True, "pid": 4242}
        if command and command[0] == "fake-server":
            self._serve(command[1])

    def _serve(self, path):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(4)
        self.listening[path] = server

        def accept():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:      # the session stopped: the server is gone with it
                    return
                try:
                    conn.sendall(b"pc 0x1000\n")
                except OSError:      # the reader left first: that is what a trace looks like
                    pass
                finally:
                    conn.close()

        threading.Thread(target=accept, daemon=True).start()

    def stop(self, unit):
        record = self.units.get(unit)
        if record:
            record["active"] = False
            path = record["command"][1] if record["command"] and record["command"][0] == "fake-server" else None
            if path and path in self.listening:
                self.listening.pop(path).close()
                Path(path).unlink(missing_ok=True)

    def reset(self, unit):
        self.stop(unit)

    def active(self, unit):
        return bool(self.units.get(unit, {}).get("active"))

    def pid(self, unit):
        return self.units.get(unit, {}).get("pid")


@pytest.fixture
def site(tmp_path):
    site = tmp_path / "site"
    (site / "modules" / "fake").mkdir(parents=True)
    (site / "modules" / "fake" / "module.py").write_text(PLUGIN)
    (site / "hosts").mkdir()
    (site / "duts").mkdir()
    import shutil

    (site / "boards").mkdir()
    shutil.copy(SITE / "boards" / "example-40pin.yaml", site / "boards")
    shutil.copytree(ROOT / "platforms", site / "platforms")
    return site


def _host_config(site, tmp_path):
    import yaml

    sock = str(tmp_path / "trace.sock")
    (site / "hosts" / "dbg-1.yaml").write_text(json.dumps({
        "host": {"id": "dbg-1"},
        "server": {"bind": "127.0.0.1", "port": 0, "state_dir": str(tmp_path / "state")},
        "modules": {"gpio": {"board": "example-40pin", "backend": "mock", "pinmux": False}, "sdk": {},
                    "dut": {"slots": {"auto_console": False}}, "fake": {"socket": sock}},
    }))
    dut = {"id": "dbg-a", "host": "dbg-1", "board": "demo-board", "soc": "soc-a", "console": {"alias": "dbg-a"},
           "fastboot": {"usb": "usb:NONE"},
           "services": {"console": {"provider": "fake-rtt"}, "debug": {"provider": "fake-swd"}}}
    (site / "duts" / "dbg-a.yaml").write_text(yaml.safe_dump(dut, sort_keys=False))
    return HostConfig.load(site / "hosts" / "dbg-1.yaml"), sock


def _live(host_config, tmp_path):
    """The host on a real port, as the client will reach it (its sessions start on demand)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(host_config)
    app.state.systemd = FakeSystemd()
    app.state.hub.sessions = Sessions(tmp_path / "sessions", app.state.systemd)
    app.state.hub.ctx.services["sessions"] = app.state.hub.sessions
    app.state.hub.modules["dut"].sessions = app.state.hub.sessions
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server, t, port


def test_the_client_asks_the_host_for_the_debug_path(site, tmp_path, monkeypatch, capsys):
    from frontends.cli.main import main

    monkeypatch.setenv("BOARDCTRL_STATE_DIR", str(tmp_path / "state"))
    cfg, _ = _host_config(site, tmp_path)
    server, t, port = _live(cfg, tmp_path)
    url = ["--url", f"http://127.0.0.1:{port}", "--stateless"]
    try:
        assert main([*url, "--json", "debug", "list"]) == 0
        listed = json.loads(capsys.readouterr().out)["result"]
        assert [(d["dut"], d["provider"], d["host"]) for d in listed] == [("dbg-a", "fake-swd", "dbg-1")]
        assert main([*url, "--json", "debug", "status", "dbg-a"]) == 0
        status = json.loads(capsys.readouterr().out)["result"]
        assert status["provider"] == "fake-swd" and status["kinds"] == ["halt", "run", "regs", "trace"]
        # Without the board's lease, nothing changes on it.
        assert main([*url, "--json", "debug", "start", "dbg-a"]) == 1
        assert "forbidden" in capsys.readouterr().out
        assert main([*url, "--json", "dut", "acquire", "dbg-a", "--holder", "t"]) == 0
        token = json.loads(capsys.readouterr().out)["result"]["token"]
        assert main([*url, "--json", "debug", "start", "dbg-a", "--lease", token]) == 0
        started = json.loads(capsys.readouterr().out)["result"]
        assert started["dut"] == "dbg-a" and started["endpoint"].endswith("trace.sock")
        assert main([*url, "--json", "debug", "action", "dbg-a", "halt", "--lease", token]) == 0
        acted = json.loads(capsys.readouterr().out)["result"]
        assert acted["halted"] is True and acted["action"] == "halt"
        # An action the provider does not offer is refused with what it does offer.
        assert main([*url, "--json", "debug", "action", "dbg-a", "flash", "--lease", token]) == 1
        assert "offers" in capsys.readouterr().out
        assert main([*url, "--json", "debug", "stop", "dbg-a", "--lease", token]) == 0
        assert json.loads(capsys.readouterr().out)["result"]["stopped"] is True
    finally:
        server.should_exit = True
        t.join(5)


def test_debug_pipe_is_the_session_stream_for_programs(site, tmp_path, monkeypatch, capsys):
    """`debug pipe` is what boardlab's debug page reads: the session's bytes as they arrive."""
    import subprocess
    import sys

    from frontends.cli.main import main

    monkeypatch.setenv("BOARDCTRL_STATE_DIR", str(tmp_path / "state"))
    cfg, _ = _host_config(site, tmp_path)
    server, t, port = _live(cfg, tmp_path)
    url = ["--url", f"http://127.0.0.1:{port}", "--stateless"]
    try:
        assert main([*url, "--json", "dut", "acquire", "dbg-a", "--holder", "t"]) == 0
        token = json.loads(capsys.readouterr().out)["result"]["token"]
        assert main([*url, "--json", "debug", "start", "dbg-a", "--lease", token]) == 0
        capsys.readouterr()
        # A trace console comes up with the session: `dut status` reports the socket the pipe bridges to.
        env = {**__import__("os").environ, "PYTHONPATH": str(ROOT)}
        out = subprocess.run([sys.executable, "-m", "frontends.cli.main", *url, "debug", "pipe", "dbg-a"],
                             capture_output=True, timeout=60, env=env)
        assert b"pc 0x1000" in out.stdout, out
    finally:
        server.should_exit = True
        t.join(5)


def test_a_session_that_is_not_up_says_so_instead_of_piping_nothing(site, tmp_path, monkeypatch, capsys):
    """`debug pipe` before a session exists is an error with the next step, not an empty stream."""
    from frontends.cli.main import main

    monkeypatch.setenv("BOARDCTRL_STATE_DIR", str(tmp_path / "state"))
    cfg, _ = _host_config(site, tmp_path)
    server, t, port = _live(cfg, tmp_path)
    url = ["--url", f"http://127.0.0.1:{port}", "--stateless"]
    try:
        assert main([*url, "debug", "pipe", "dbg-a"]) == 1
        cap = capsys.readouterr()
        assert "not running" in cap.err and "debug start dbg-a" in cap.err
    finally:
        server.should_exit = True
        t.join(5)


def test_the_debug_group_exists_before_any_host_is_reached():
    """The group is the client's own: `--help` works with no host, and names every action."""
    from frontends.cli import debugcli
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    debugcli.add_parser(sub)
    ns = parser.parse_args(["debug", "action", "board-a", "halt", "--lease", "tok"])
    assert (ns.cmd, ns.debug_cmd, ns.dut, ns.action, ns.lease) == ("debug", "action", "board-a", "halt", "tok")
    assert parser.parse_args(["debug", "list"]).debug_cmd == "list"


def test_a_stream_nobody_types_into_is_still_read(tmp_path):
    """`pipe` holds a trace open after its input ends; the old bridge returned as soon as stdin hit EOF."""
    import os

    from frontends.cli import pipe

    sock_path = str(tmp_path / "trace.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(2)

    def serve(lines):
        for _ in lines:
            conn, _ = server.accept()
            try:
                conn.sendall(b"pc 0x1000\n")
            except OSError:      # the reader left first (the case without `hold`)
                pass
            finally:
                conn.close()

    threading.Thread(target=serve, args=([1, 1],), daemon=True).start()

    def run(hold):
        fin_r, fin_w = os.pipe()
        os.close(fin_w)                     # stdin ends at once: nothing is typed into a trace
        out_r, out_w = os.pipe()
        try:
            rc = pipe.bridge(sock_path, fin_r, out_w, hold)
        finally:
            os.close(fin_r)
            os.close(out_w)
        data = os.read(out_r, 4096)
        os.close(out_r)
        return rc, data

    assert run(True) == (0, b"pc 0x1000\n")
    # Without `hold`, the end of stdin is the end of the pipe: what a console (typed into) needs.
    assert run(False) == (0, b"")
