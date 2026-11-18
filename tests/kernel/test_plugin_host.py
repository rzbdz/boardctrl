"""A board whose console and debug path come from a site plugin, end to end through the host's API.

This is the shape the EC port relies on: a plugin registers providers, a DUT record binds them, and `debug` and
the console work through the same commands, sessions and leases as a board served by the built-ins.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.providers import PROVIDERS
from core.sessions import Session, Sessions
from frontends.server.app import create_app
from frontends.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[2]

from test_kernel import FakeSystemd  # noqa: E402

PLUGIN = '''
from modules.dut.debug import DebugProvider

class FakeDebug(DebugProvider):
    provider_name = "fake-swd"
    kinds = ("halt", "run", "regs")

    def __init__(self, spec, config, services=None):
        super().__init__(spec, config, services)
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
        s = self.sessions.get(self.spec.id, "debug")
        return {"halted": self.halted, **({"endpoint": s.endpoint} if s else {})}

class FakeConsole:
    provider_name = "fake-rtt"
    alias = None

    def __init__(self, spec, config):
        self.spec, self.config = spec, config
        self.lines = ["boot 1.2.3", "trace # "]

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

class FakeFlash:
    provider_name = "fake-writer"
    title = "fake image writer"
    about = "writes the image a plugin gave it"

    def __init__(self, spec, config):
        self.spec, self.config = spec, config
        # What the DUT's record says for this role: the provider's own settings.
        self.role = (spec.raw.get("services") or {}).get("flash") or {}

    def flash(self, package, *, log, timeout):
        # What a plugin's flasher does with the file the host resolved for it.
        log(f"writing {package.name}")
        return {"written": package.name, "expect_version": self.role.get("expect_version")}

def setup(ctx, api, config):
    api.provider("debug", "fake-swd", lambda c, s, sv: FakeDebug(s, config, sv), title="fake SWD")
    api.provider("console", "fake-rtt", lambda c, s, sv: FakeConsole(s, config), title="fake trace console")
    api.provider("flash", "fake-writer", lambda c, s, sv: FakeFlash(s, config), title="fake image writer")

    from core.cmdspec import CommandSpec, Param
    from core.commands import Command

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
    (site / "boards").mkdir()
    shutil.copy(ROOT / "examples" / "site" / "boards" / "example-40pin.yaml", site / "boards")
    shutil.copytree(ROOT / "profiles", site / "profiles")
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
    import yaml

    (site / "duts").mkdir(exist_ok=True)
    (site / "duts" / "k1-site.yaml").write_text(yaml.safe_dump(SITE_DUT, sort_keys=False))
    app = create_app(HostConfig.load(site / "hosts" / "ec-1.yaml"))
    # Sessions are transient units: in a test they are recorded, not started.
    app.state.systemd = FakeSystemd()
    app.state.hub.sessions = Sessions(tmp_path / "sessions", app.state.systemd)
    app.state.hub.ctx.services["sessions"] = app.state.hub.sessions
    app.state.hub.modules["dut"].sessions = app.state.hub.sessions
    with TestClient(app) as c:
        yield c


DUT = {"id": "k1-a", "board": "demo-board", "soc": "soc-a", "console": {"alias": "k1-a"},
       "fastboot": {"usb": "usb:UNKNOWN"},
       "services": {"console": {"provider": "fake-rtt"}, "download": {"provider": "none"},
                    "debug": {"provider": "fake-swd", "target": "probe0"}}}
# A site that ships the DUT record binds the plugin's providers from the first start: the plugin loads before the
# DUT module builds its boards.
SITE_DUT = {**DUT, "id": "k1-site", "host": "ec-1", "console": {"alias": "k1-site"},
            "fastboot": {"usb": "usb:UNKNOWN2"},
            "services": {**DUT["services"], "console": {"provider": "fake-rtt"},
                         "flash": {"provider": "fake-writer", "expect_version": "V1.0"}}}


def test_a_record_the_site_ships_is_bound_to_the_plugins_providers_at_start(client):
    status = client.post("/api/v1/tools/dut_status", json={"dut": "k1-site"}).json()["result"]
    assert status["services"]["debug"]["provider"] == "fake-swd"
    assert status["services"]["flash"] == "fake-writer"
    assert status["console_tail"] == ["boot 1.2.3", "trace # "]


def test_the_plugin_loaded_and_registered_its_providers(client):
    status = client.get("/api/v1/status").json()["result"]
    assert status["plugins"]["loaded"]["fake"]["providers"] == ["debug/fake-swd", "console/fake-rtt",
                                                              "flash/fake-writer"]
    assert "fake-swd" in status["providers"]["debug"]["names"]
    assert status["plugins"]["loaded"]["fake"]["commands"] == ["fake.hello"]


def test_the_plugin_commands_work_like_any_other(client):
    r = client.post("/api/v1/fake/hello", json={"name": "ec"}).json()
    assert r["ok"] and r["result"] == {"hello": "ec", "config": {"pins": "J40:11"}}
    assert any(t["tool_name"] == "fake_hello" for t in client.get("/api/v1/tools").json()["tools"])


def test_a_dut_binds_the_plugins_providers(client):
    r = client.post("/api/v1/tools/dut_add", json={"spec": json.dumps(DUT)}).json()
    assert r["ok"], r
    d = client.post("/api/v1/tools/dut_status", json={"dut": "k1-a"}).json()["result"]
    assert d["services"]["console"] == "fake-rtt"
    assert d["services"]["debug"]["provider"] == "fake-swd"
    assert d["services"]["download"] == "none"
    assert d["console_tail"] == ["boot 1.2.3", "trace # "]
    assert d["console_up"] is True


def test_debug_runs_under_the_lease_and_stops_with_it(client):
    client.post("/api/v1/tools/dut_add", json={"spec": json.dumps(DUT)})
    lease = client.post("/api/v1/tools/dut_acquire", json={"dut": "k1-a", "holder": "alice"}).json()["result"]
    token = lease["token"]
    # Without the lease nothing changes.
    assert client.post("/api/v1/tools/debug_start", json={"dut": "k1-a"}).status_code == 403
    started = client.post("/api/v1/tools/debug_start", json={"dut": "k1-a", "lease": token}).json()
    assert started.get("ok"), started
    started = started["result"]
    assert started["endpoint"] == "127.0.0.1:3334" and started["dut"] == "k1-a"
    acted = client.post("/api/v1/tools/debug_action", json={"dut": "k1-a", "action": "halt", "lease": token}).json()
    assert acted["result"]["halted"] is True
    bad = client.post("/api/v1/tools/debug_action", json={"dut": "k1-a", "action": "flash", "lease": token}).json()
    assert "offers" in bad["error"]["message"]
    shelf = client.post("/api/v1/tools/debug_list", json={}).json()["result"]
    assert shelf[0]["dut"] == "k1-a" and shelf[0]["provider"] == "fake-swd"
    # Releasing the board stops the session: the next holder does not inherit a debugger.
    released = client.post("/api/v1/tools/dut_release", json={"dut": "k1-a", "lease": token}).json()["result"]
    assert released["sessions_stopped"] == ["debug"]
    assert client.get("/api/v1/sessions").json()["sessions"] == []
    assert client.post("/api/v1/tools/debug_status", json={"dut": "k1-a"}).json()["result"]["provider"] == "fake-swd"


def test_a_board_without_a_debug_provider_says_so(client):
    plain = {**DUT, "id": "plain", "services": {"console": {"provider": "fake-rtt"}}}
    client.post("/api/v1/tools/dut_add", json={"spec": json.dumps(plain)})
    lease = client.post("/api/v1/tools/dut_acquire", json={"dut": "plain", "holder": "alice"}).json()["result"]
    r = client.post("/api/v1/tools/debug_start", json={"dut": "plain", "lease": lease["token"]}).json()
    assert "no debug provider" in r["error"]["message"]


def test_the_flash_role_can_be_a_plugins_writer(client, tmp_path):
    """A flash provider takes the file the host resolved and writes it; no download port or hook is involved."""
    import hashlib

    image = b":020000040800F2\n:00000001FF\n"
    sha = hashlib.sha256(image).hexdigest()
    assert client.put(f"/api/v1/artifacts/{sha}?name=ec.hex", content=image).json()["ok"]
    dut = {**DUT, "id": "ec-flash", "fastboot": {},
           "services": {**DUT["services"], "flash": {"provider": "fake-writer", "expect_version": "V1.0"}}}
    assert client.post("/api/v1/tools/dut_add", json={"spec": json.dumps(dut)}).json()["ok"]
    status = client.post("/api/v1/tools/dut_status", json={"dut": "ec-flash"}).json()["result"]
    assert status["services"]["flash"] == "fake-writer"
    # Without the board's lease a flash changes nothing.
    refused = client.post("/api/v1/tools/dut_flash", json={"dut": "ec-flash", "package": sha}).json()
    assert refused["error"]["code"] == "forbidden"
    lease = client.post("/api/v1/tools/dut_acquire", json={"dut": "ec-flash", "holder": "alice"}).json()["result"]
    started = client.post("/api/v1/tools/dut_flash", json={"dut": "ec-flash", "package": sha,
                                                          "lease": lease["token"]}).json()
    assert started.get("ok"), started
    assert started["result"]["flasher"] == "fake-writer"
    job = client.post("/api/v1/tools/job_get", json={"id": started["result"]["job"], "wait": 20}).json()["result"]
    assert job["status"] == "succeeded", job
    assert job["result"]["written"] == "ec.hex" and job["result"]["expect_version"] == "V1.0"
    assert any("writing ec.hex" in line for line in job["lines"])
    # The provider was handed the very bytes that were uploaded.
    assert client.get(f"/api/v1/artifacts/{sha}").json()["result"]["present"]
    assert client.get(f"/api/v1/artifacts/{sha}").json()["result"]["size"] == len(image)


def test_a_session_stream_carries_bytes_both_ways(client, tmp_path):
    """The stream is what a client that cannot reach the host's loopback reads instead of a port."""
    import base64
    import socket
    import threading
    import time

    sock_path = str(tmp_path / "session.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(4)
    received: list[bytes] = []

    def serve():
        for _ in range(2):
            conn, _ = server.accept()
            if len(received) == 0:
                conn.sendall(b"boot 1.2.3\n")
            else:
                received.append(conn.recv(64))
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    client.post("/api/v1/tools/dut_add", json={"spec": json.dumps(DUT)})
    hud = client.app.state.hub
    hud.sessions._save(Session("k1-a", "debug", "fake-swd", "unit-1", endpoint=sock_path, started=time.time()))
    client.app.state.systemd.units["unit-1"] = {"command": [], "active": True, "pid": 4242}

    listing = client.get("/api/v1/sessions").json()["sessions"]
    assert listing[0]["dut"] == "k1-a" and listing[0]["endpoint"] == sock_path
    body = ""
    with client.stream("GET", "/api/v1/sessions/k1-a/debug/stream") as r:
        assert r.status_code == 200
        for chunk in r.iter_text():
            body += chunk
            if "data: " in body and b"boot 1.2.3" in b"".join(
                    base64.b64decode(line[6:]) for line in body.splitlines() if line.startswith("data: ")):
                break
    assert b"boot 1.2.3\n" in b"".join(base64.b64decode(line[6:]) for line in body.splitlines()
                                           if line.startswith("data: "))
    received.append(b"")          # the first connection was the stream
    r = client.post("/api/v1/sessions/k1-a/debug/input", json={"data": base64.b64encode(b"help\r").decode()})
    assert r.json()["ok"] and r.json()["bytes"] == 5
    deadline = time.time() + 3
    while time.time() < deadline and not any(received):
        time.sleep(0.05)
    assert any(b"help\r" in item for item in received if item)


def test_a_provider_carries_the_title_and_explanation_it_registered_with(client):
    """What the page shows for a path is the provider's own words, not its registered name."""
    status = client.post("/api/v1/tools/dut_status", json={"dut": "k1-site"}).json()["result"]
    shelf = client.post("/api/v1/tools/debug_status", json={"dut": "k1-site"}).json()["result"]
    # The path reports its own words, though the plugin's `info()` names neither.
    assert shelf["title"] == "fake SWD" and shelf["provider"] == "fake-swd"
    listed = client.post("/api/v1/tools/debug_list", json={}).json()["result"]
    assert listed[0]["title"] == "fake SWD"
    # and a board's record still shows which provider fills the role, by name
    assert status["services"]["debug"]["provider"] == "fake-swd"


def test_a_plugin_switched_off_in_the_host_config_is_not_loaded(site, tmp_path, monkeypatch):
    """`enabled: false` is a switch, however the plugin would otherwise be selected.

    A site carries the code, and one host may still need it off — it drives pins, it needs a tool that host does not
    have. Reading the key as a comment and loading the plugin anyway is how a host starts doing something its
    operator says it should not.
    """
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BSP_SITE", str(site))
    import yaml

    (site / "hosts" / "off.yaml").write_text(yaml.safe_dump({
        "host": {"id": "off"},
        "server": {"bind": "127.0.0.1", "port": 8766, "state_dir": str(tmp_path / "state2")},
        "modules": {"gpio": {"board": "example-40pin", "backend": "mock", "pinmux": False},
                    "sdk": {}, "dut": {}, "fake": {"enabled": False}},
    }))
    app = create_app(HostConfig.load(site / "hosts" / "off.yaml"))
    with TestClient(app) as client:
        status = client.get("/api/v1/status").json()
        assert "fake" not in (status.get("plugins") or {}).get("loaded", {})
        assert "fake.hello" not in {tool["name"] for tool in client.get("/api/v1/tools").json()["tools"]}


def test_a_plugin_named_in_plugins_and_switched_off_is_not_loaded(site, tmp_path, monkeypatch):
    """The other selection route: an explicit `plugins: [fake]` does not override `enabled: false`."""
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BSP_SITE", str(site))
    import yaml

    (site / "hosts" / "named.yaml").write_text(yaml.safe_dump({
        "host": {"id": "named"},
        "server": {"bind": "127.0.0.1", "port": 8767, "state_dir": str(tmp_path / "state3")},
        "modules": {"gpio": {"board": "example-40pin", "backend": "mock", "pinmux": False},
                    "sdk": {}, "dut": {}, "fake": {"enabled": False}},
        "plugins": ["fake"],
    }))
    app = create_app(HostConfig.load(site / "hosts" / "named.yaml"))
    with TestClient(app) as client:
        status = client.get("/api/v1/status").json()
        assert "fake" not in (status.get("plugins") or {}).get("loaded", {})


RELAY_PLUGIN = '''
class FakeRelay:
    """A reset a vendor tool performs: registers under the relay role, which had no provider before."""

    needs_human = False

    def __init__(self, dut, cfg):
        self.dut, self.cfg = dut, cfg
        self.resets = 0

    def setup(self):
        pass

    def reset(self, log, ready=None):
        self.resets += 1
        log(f"fake reset of {self.dut.id}")

    def describe(self):
        return {"backend": "fake-relay", "cfg": self.cfg}

def setup(ctx, api, config):
    api.provider("relay", "fake-relay", lambda c, s, sv: FakeRelay(s, config), title="fake reset tool")
'''


def test_a_plugin_can_fill_the_relay_role(site, tmp_path, monkeypatch):
    """`relay` is a provider role like debug/console/flash: a site plugin registers one and a record binds it.

    This is what the role table promised and did not do — the built-ins only reached the old `control.backend`
    path, so a `services.relay.provider` a plugin registered was ignored.
    """
    (site / "modules" / "relayplug").mkdir(parents=True)
    (site / "modules" / "relayplug" / "module.py").write_text(RELAY_PLUGIN)
    (site / "duts").mkdir(exist_ok=True)
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BSP_SITE", str(site))
    import yaml

    (site / "hosts" / "relay.yaml").write_text(yaml.safe_dump({
        "host": {"id": "relay-host"},
        "server": {"bind": "127.0.0.1", "port": 8768, "state_dir": str(tmp_path / "state4")},
        "modules": {"gpio": {"board": "example-40pin", "backend": "mock", "pinmux": False},
                    "sdk": {}, "dut": {}, "relayplug": {}},
    }))
    (site / "duts" / "relay-board.yaml").write_text(yaml.safe_dump({
        "id": "relay-board", "host": "relay-host",
        "services": {"relay": {"provider": "fake-relay", "why": "vendor tool"},
                     "console": {"provider": "none"}, "download": {"provider": "none"},
                     "debug": {"provider": "none"}},
    }, sort_keys=False))
    app = create_app(HostConfig.load(site / "hosts" / "relay.yaml"))
    with TestClient(app) as client:
        status = client.post("/api/v1/tools/dut_status", json={"dut": "relay-board"}).json()["result"]
        # The record's own provider name comes back, not the default.
        assert status["services"]["relay"] == "fake-relay"
        board = app.state.hub.modules["dut"].boards["relay-board"]
        # ... and it is the plugin's object, built for this board (the factory sees the host's module config).
        assert board.control.describe()["backend"] == "fake-relay"
        assert board.control.dut.id == "relay-board"
        board.control.reset(lambda line: None)
        assert board.control.resets == 1


def test_the_built_in_roles_are_still_the_defaults_and_still_work(site, tmp_path, monkeypatch):
    """Filling the role table must not change what a record that names nothing gets."""
    from modules.dut.control import build_download, build_relay
    from modules.dut.catalog import spec_from_dict

    spec = spec_from_dict({"id": "plain", "host": "h1", "services": {"download": {"provider": "fastboot", "usb": "usb:1-1"}}})
    control = build_relay("gpio-relay", spec, lambda name: None)
    assert type(control).__name__ == "GpioRelayControl"
    download = build_download("fastboot", spec, "fastboot", lambda name: None,
                              cfg={"provider": "fastboot", "usb": "usb:1-1"})
    assert download.usb == "usb:1-1"
