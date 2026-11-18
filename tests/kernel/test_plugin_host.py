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
SITE = ROOT / "tests" / "fixtures" / "site"

from test_kernel import FakeSystemd  # noqa: E402

PLUGIN = '''
from core.dut.contracts import DebugProvider

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
        self.alias = config.get("alias")
        self.lines = ["boot 1.2.3", "trace # "]

    def tail(self, lines=50, grep=None):
        return self.lines[-lines:]

    def range(self, since=None, until=None, grep=None, regex=False, limit=20000, first=False):
        return {"lines": self.lines, "matched": len(self.lines), "truncated": False}

    def send(self, text, pattern=None, fail=None, timeout=60, mark=None, stall=None, nudge=None):
        return {"output": "ok", "matched": True}

    def wait(self, pattern=None, fail=None, timeout=60, mark=None, stall=None, nudge=None):
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
    shutil.copy(SITE / "boards" / "example-40pin.yaml", site / "boards")
    shutil.copytree(ROOT / "platforms", site / "platforms")
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
    assert status["modules"]["fake"]["providers"] == ["debug/fake-swd", "console/fake-rtt",
                                                              "flash/fake-writer"]
    assert "fake-swd" in status["providers"]["debug"]["names"]
    assert status["modules"]["fake"]["commands"] == ["fake.hello"]


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
        assert "fake" not in (status.get("modules") or {})
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
        assert "fake" not in (status.get("modules") or {})


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


def test_a_host_file_written_with_the_old_sections_still_loads_them(site, tmp_path):
    """`modules:` is the key sites wrote before providers, and `gpio_relay:` a service's name before GPIO had a role:
    the same settings, under the names the host loads now, and the relay role works through them."""
    from frontends.server.app import create_app
    from frontends.server.config import HostConfig

    host = tmp_path / "legacy-site" / "hosts" / "legacy.yaml"
    host.parent.mkdir(parents=True)
    for name in ("boards", "socs", "sdk", "duts"):
        (host.parent.parent / name).symlink_to(site / name)
    host.write_text("host: {id: legacy}\nserver: {state_dir: state, port: 8771}\n"
                    "modules:\n  gpio_relay: {board: example-40pin, backend: mock, pinmux: false}\n  sdk: {}\n  dut: {}\n")
    with TestClient(create_app(HostConfig.load(host))) as client:
        status = client.post("/api/v1/tools/status", json={}).json()["result"]
        assert "error" not in status["modules"]["gpio"]
        assert "gpio-relay" in status["providers"]["relay"]["names"]
        assert client.post("/api/v1/tools/gpio_list", json={}).json()["ok"]


def test_a_host_built_before_its_providers_still_has_their_services():
    """The DUT manager is built before any provider sets up (a record may bind any provider), and the serial tooling
    and the fastboot client it uses are read when used: a host whose smtty provider loaded afterwards still has real
    consoles, its log directory and its binaries, not the empty fallbacks."""
    from fastapi.testclient import TestClient

    from frontends.server.app import create_app
    from frontends.server.config import HostConfig

    app = create_app(HostConfig.load(SITE / "hosts" / "dev-mock.yaml"))
    with TestClient(app):
        dut = app.state.hub.dut
        assert type(dut.smtty_service).__name__ == "Smtty"
        assert type(dut.consoles).__name__ == "ConsoleManager", "a host's serial consoles, not the empty fallback"
        assert dut.serial_log_dir.endswith(".serial-mux/logs")
        assert dut.fastboot_bin


def test_identify_builds_its_request_with_the_hosts_fastboot_client():
    """`dut probe` and `dut refresh` identify a board through the identify hook, and the request names the host's
    fastboot client. The hook is faked here: what is checked is that the request is built (a manager with no such
    attribute raises before the hook runs) and that it carries the configured binary."""
    import tempfile

    from fastapi.testclient import TestClient

    from core.dut.probe import identify_board
    from frontends.server.app import create_app
    from frontends.server.config import HostConfig

    app = create_app(HostConfig.load(SITE / "hosts" / "dev-mock.yaml"))
    with TestClient(app):
        dut = app.state.hub.dut
        seen: dict = {}
        dut.hooks.run = lambda kind, name, req, timeout=0, log=None: (
            seen.update(req) or type("R", (), {"applicable": False, "data": {}})())
        assert identify_board(dut, "usb:1-1", True, "soc-a", None, lambda line: None) == {}
        assert seen["fastboot"] == (dut.config.get("fastboot") or "fastboot")


def test_a_host_written_with_plugins_and_modules_loads_all_of_them(site, tmp_path, monkeypatch):
    """`plugins: [name]` was how a host named a plugin before providers had a section map. A file that still says it
    (with or without `modules:`) loads those plugins, and a name under `providers:` wins over the same name there."""
    from fastapi.testclient import TestClient

    from frontends.server.app import create_app
    from frontends.server.config import HostConfig

    host = tmp_path / "plugins-site" / "hosts" / "plugins.yaml"
    host.parent.mkdir(parents=True)
    for name in ("boards", "socs", "sdk", "duts", "modules"):
        (host.parent.parent / name).symlink_to(site / name)
    host.write_text("host: {id: plugins}\nserver: {state_dir: state, port: 8772}\n"
                    "plugins: [fake]\n")
    with TestClient(create_app(HostConfig.load(host))) as client:
        status = client.post("/api/v1/tools/status", json={}).json()["result"]
        assert "error" not in status["modules"].get("fake", {"error": "not loaded"}), status["modules"]


def test_the_artifact_cache_can_pull_from_a_build_server(tmp_path):
    """The cache's pull runs local commands (`ssh`/`scp`) through one helper. A store that lost it raises
    `NameError` on the host after a boot recipe has started, before the board is touched: a pull that cannot reach
    its server has to say so as a host failure instead."""
    from core.dut.artifacts import ArtifactStore
    from core.errors import BspError

    try:
        ArtifactStore(tmp_path).pull("nobody@192.0.2.1", {"kernel": "/tmp/nope"}, lambda line: None)
    except BspError as e:
        assert "nobody@192.0.2.1" in e.message
    else:
        raise AssertionError("an unreachable build server is a failure the caller must see")


def test_a_download_provider_that_is_none_is_not_replaced_by_fastboot(site, tmp_path, monkeypatch):
    """`download: {provider: none}` means the board has no download port; the fastboot provider must not register over
    the kernel's `none`, or every such record would get a fastboot device that is not there."""
    from core.dut.defaults import NoneDownload
    from core.providers import build
    from core.dut.records import spec_from_dict
    from core.loader import Providers

    class Ctx:
        services: dict = {}

    Providers([Path(__file__).resolve().parents[2] / "providers"]).load(
        Ctx(), {"gpio": {}, "smtty": {}, "fastboot": {}, "human": {}})
    spec = spec_from_dict({"id": "no-port", "host": "h1", "services": {"download": {"provider": "none"}}})
    made = build("download", "none", Ctx(), spec, lambda name: None)
    assert isinstance(made, NoneDownload), f"download/none became {type(made).__name__}"


def test_the_built_in_roles_are_still_the_defaults_and_still_work(site, tmp_path, monkeypatch):
    """Filling the role table must not change what a record that names nothing gets."""
    from core.providers import build
    from core.dut.records import spec_from_dict

    spec = spec_from_dict({"id": "plain", "host": "h1", "services": {"download": {"provider": "fastboot", "usb": "usb:1-1"}}})
    # The relay and download providers come from the modules a host with DUTs carries (loaded by its Hub).
    from frontends.server.app import ROLE_PROVIDERS
    from core.loader import Providers

    class Ctx:
        services: dict = {}

    Providers([Path(__file__).resolve().parents[2] / "providers"]).load(
        Ctx(), {name: {} for name in ROLE_PROVIDERS})
    ctx = Ctx()
    ctx.services = {"smtty": None}
    control = build("relay", "gpio-relay", ctx, spec, lambda name: None)
    assert type(control).__name__ == "GpioRelayControl"
    download = build("download", "fastboot", ctx, spec, lambda name: None)
    assert download.usb == "usb:1-1"


# --------------------------------------------------- a host that lacks a role
def _bare_host(tmp_path, monkeypatch, providers: dict, duts: dict[str, dict] | None = None) -> TestClient:
    """A host with exactly the providers given, and the DUT records it ships: no fixture, no platform boards."""
    import yaml

    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    site = tmp_path / "bare"
    (site / "hosts").mkdir(parents=True)
    (site / "duts").mkdir()
    for name, raw in (duts or {}).items():
        (site / "duts" / f"{name}.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    host = site / "hosts" / "bare.yaml"
    host.write_text(json.dumps({"host": {"id": "bare"},
                                "server": {"state_dir": str(tmp_path / "state"), "port": 8766},
                                "providers": providers}))
    monkeypatch.setenv("BSP_SITE", str(site))
    return TestClient(create_app(HostConfig.load(host)))


def test_a_host_without_a_relay_provider_still_starts_and_says_which_role_is_missing(tmp_path, monkeypatch):
    """A USB-only machine (no GPIO header, so no `gpio` provider, and so no `gpio-relay` either) may still carry a
    record that names the relay role — its own from before, or the site's. It starts, the other roles work, and the
    missing one says what is missing instead of taking the whole host down.

    The role tables are process-wide, so the providers another test's fixture registered are removed here: this is
    what a host that loads only `fake` and `dut` sees.
    """
    from core.providers import PROVIDERS

    for name in ("gpio-relay", "gpio_relay"):
        if name in PROVIDERS["relay"]:
            monkeypatch.delitem(PROVIDERS["relay"]._items, name)
    record = {"id": "usb-only", "host": "bare", "board": "b", "soc": "soc-a",
              "console": {"alias": "usb-only"}, "fastboot": {"usb": "usb:1-2"},
              "control": {"backend": "gpio-relay", "reset": {"pin": "P11"}}}
    with _bare_host(tmp_path, monkeypatch, {"fake": {}, "dut": {}}, {"usb-only": record}) as client:
        status = client.post("/api/v1/tools/dut_status", json={"dut": "usb-only"}).json()
        assert status.get("ok"), status                       # the host started, and the board is there
        assert status["result"]["services"]["relay"] == "gpio-relay"
        assert "no relay" in status["result"]["control_state"].get("error", "")
        lease = client.post("/api/v1/tools/dut_acquire", json={"dut": "usb-only", "holder": "alice"}).json()["result"]
        reset = client.post("/api/v1/tools/dut_reset", json={"dut": "usb-only", "lease": lease["token"]}).json()
        assert not reset["ok"] and "no relay" in reset["error"]["message"]


def test_a_mistyped_provider_is_refused_before_it_is_written(tmp_path, monkeypatch):
    """`dut set --download-provider typo` must not save the name: a record naming a provider nobody registered is a
    host that cannot build that board next start, with the person who typed it long gone."""
    with _bare_host(tmp_path, monkeypatch, {"fake": {}, "dut": {}, "human": {}}) as client:
        added = client.post("/api/v1/tools/dut_add", json={"spec": json.dumps({
            "id": "b1", "host": "bare", "board": "b", "soc": "soc-a", "console": {"alias": "b1"},
            "fastboot": {"usb": "usb:1-2"}, "control": {"backend": "human"}})}).json()
        assert added.get("ok"), added
        lease = client.post("/api/v1/tools/dut_acquire", json={"dut": "b1", "holder": "alice"}).json()["result"]
        bad = client.post("/api/v1/tools/dut_set", json={"dut": "b1", "lease": lease["token"],
                                                        "download_provider": "typo"}).json()
        assert not bad["ok"] and bad["error"]["code"] == "invalid_request"
        assert "download provider 'typo'" in bad["error"]["message"] and "none" in bad["error"]["message"]
        # The record on disk is untouched, so the next start builds the board exactly as this one did.
        after = client.post("/api/v1/tools/dut_status", json={"dut": "b1"}).json()["result"]
        assert after["services"]["download"] == "fastboot"
