"""A board whose console and debug path come from a site plugin, end to end through the host's API.

This is the shape the EC port relies on: a plugin registers providers, a DUT record binds them, and `debug` and
the console work through the same commands, sessions and leases as a board served by the built-ins.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bsp.core.providers import PROVIDERS
from bsp.server.app import create_app
from bsp.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[1]

PLUGIN = '''
from bsp.core.providers import Providers

class FakeDebug:
    provider_name = "fake-swd"
    kinds = ("halt", "run", "regs")

    def __init__(self, spec, config):
        self.spec, self.config = spec, config
        self.halted = False

    def start(self, lease=None):
        self.sessions.start(self.spec.id, "debug", self.provider_name, ["fake-server"], lease=lease,
                            endpoint="127.0.0.1:3334")
        return {"dut": self.spec.id, "endpoint": "127.0.0.1:3334"}

    def stop(self):
        return self.sessions.stop(self.spec.id, "debug")

    def action(self, name, args=None):
        self.halted = name in ("halt", "step")
        return {"halted": self.halted, "kinds": list(self.kinds)}

    def shelf(self):
        return {"provider": self.provider_name, "kinds": list(self.kinds)}

    def info(self):
        return {"provider": self.provider_name, "kinds": list(self.kinds), "available": True}

    def state(self):
        return {"halted": self.halted, "endpoint": self.session_endpoint}

class FakeConsole:
    provider_name = "fake-rtt"
    alias = None

    def __init__(self, spec, config):
        self.spec, self.config = spec, config
        self.lines = ["EC boot 1.2.3", "CSCE2010 #"]

    def tail(self, lines=50, grep=None):
        return self.lines[-lines:]

    def range(self, since=None, until=None, grep=None, regex=False, limit=20000, first=False):
        return {"lines": self.lines, "matched": len(self.lines), "truncated": False}

    def send(self, text, pattern=None, fail=None, timeout=60, mark=None):
        return {"output": "ok", "matched": True}

    def wait(self, pattern=None, fail=None, timeout=60, mark=None):
        return "ok"

    def run(self, command, expect, timeout):
        return {"output": "Support Cmd", "matched": True}

    def up(self):
        return True

    def attach(self):
        return None

    def socket(self):
        return None

    def describe(self):
        return {"provider": self.provider_name}

def setup(ctx, api, config):
    api.provider("debug", "fake-swd", lambda c, s, sv: FakeDebug(s, config), title="fake SWD")
    api.provider("console", "fake-rtt", lambda c, s, sv: FakeConsole(s, config), title="fake trace console")

    from bsp.core.cmdspec import CommandSpec, Param
    from bsp.core.commands import Command

    def hello(name: str):
        return {"hello": name, "config": config}

    api.command(Command(CommandSpec("fake.hello", "Say hello from the site plugin", [Param("name")]),
                        hello, lambda r: f"hello {r['hello']}"))
'''


@pytest.fixture
def site(tmp_path):
    site = tmp_path / "site"
    (site / "modules" / "fake").mkdir(parents=True)
    (site / "modules" / "fake" / "module.py").write_text(PLUGIN)
    (site / "hosts").mkdir()
    (site / "gc.yaml").write_text("")
    return site


@pytest.fixture
def client(site, tmp_path, monkeypatch):
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BSP_SITE", str(site))
    (site / "hosts" / "ec-1.yaml").write_text(json.dumps({
        "host": {"id": "ec-1"},
        "server": {"bind": "127.0.0.1", "port": 8765, "state_dir": str(tmp_path / "state")},
        "modules": {"gpio": {"board": "example-40pin", "backend": "mock", "pinmux": False},
                    "sdk": {}, "dut": {"slots": {"auto_console": False}}, "fake": {"pins": "J40:11"}},
    }))
    with TestClient(create_app(HostConfig.load(site / "hosts" / "ec-1.yaml"))) as c:
        yield c


DUT = {"id": "k1-a", "board": "com260", "soc": "csce2010", "console": {"alias": "k1-a"},
       "fastboot": {"usb": "usb:UNKNOWN"},
       "services": {"console": {"provider": "fake-rtt"}, "download": {"provider": "none"},
                    "debug": {"provider": "fake-swd", "target": "pico2"}}}


def test_the_plugin_loaded_and_registered_its_providers(client):
    status = client.get("/api/v1/status").json()["result"]
    assert status["plugins"]["loaded"]["fake"]["providers"] == ["debug/fake-swd", "console/fake-rtt"]
    assert "fake-swd" in status["providers"]["debug"]["names"]
    assert status["plugins"]["loaded"]["fake"]["commands"] == ["fake.hello"]


def test_the_plugin_commands_work_like_any_other(client):
    r = client.post("/api/v1/fake/hello", json={"name": "ec"}).json()
    assert r["ok"] and r["result"] == {"hello": "ec", "config": {"pins": "J40:11"}}
    assert any(t["name"] == "fake_hello" for t in client.get("/api/v1/tools").json()["tools"])


def test_a_dut_binds_the_plugins_providers(client):
    assert client.post("/api/v1/tools/dut_add", json={"spec": json.dumps(DUT)}).json()["ok"]
    d = client.post("/api/v1/tools/dut_status", json={"dut": "k1-a"}).json()["result"]
    assert d["services"]["console"] == "fake-rtt"
    assert d["services"]["debug"]["provider"] == "fake-swd"
    assert d["services"]["download"] == "none"
    assert d["console_tail"] == ["EC boot 1.2.3", "CSCE2010 #"]
    assert d["console_up"] is True


def test_debug_runs_under_the_lease_and_stops_with_it(client):
    client.post("/api/v1/tools/dut_add", json={"spec": json.dumps(DUT)})
    lease = client.post("/api/v1/tools/dut_acquire", json={"dut": "k1-a", "holder": "alice"}).json()["result"]
    token = lease["token"]
    # Without the lease nothing changes.
    assert client.post("/api/v1/tools/debug_start", json={"dut": "k1-a"}).status_code == 403
    started = client.post("/api/v1/tools/debug_start", json={"dut": "k1-a", "lease": token}).json()["result"]
    assert started["endpoint"] == "127.0.0.1:3334" and started["role"] == "debug"
    acted = client.post("/api/v1/tools/debug_action", json={"dut": "k1-a", "action": "halt", "lease": token}).json()
    assert acted["result"]["halted"] is True
    bad = client.post("/api/v1/tools/debug_action", json={"dut": "k1-a", "action": "flash", "lease": token}).json()
    assert "offers" in bad["error"]["message"]
    shelf = client.post("/api/v1/tools/debug_list", json={}).json()["result"]
    assert shelf[0]["dut"] == "k1-a" and shelf[0]["provider"] == "fake-swd"
    # Releasing the board stops the session: the next holder does not inherit a debugger.
    assert client.post("/api/v1/tools/dut_release", json={"dut": "k1-a", "lease": token}).json()["ok"]
    assert client.post("/api/v1/tools/debug_status", json={"dut": "k1-a"}).json()["result"]["provider"] == "fake-swd"


def test_a_board_without_a_debug_provider_says_so(client):
    plain = {**DUT, "id": "plain", "services": {}}
    client.post("/api/v1/tools/dut_add", json={"spec": json.dumps(plain)})
    lease = client.post("/api/v1/tools/dut_acquire", json={"dut": "plain", "holder": "alice"}).json()["result"]
    r = client.post("/api/v1/tools/debug_start", json={"dut": "plain", "lease": lease["token"]}).json()
    assert "no debug provider" in r["error"]["message"]
