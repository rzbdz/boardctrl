"""The hello-platform example, loaded as a site plugin by a real host.

The example in `examples/site/modules/hello-platform` is the template a person copies to adapt a real
platform. A template that has rotted is worse than none, so this loads it exactly as a host does — through
the plugin loader, bound by a DUT record — and asks each role for its answer over the API.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frontends.server.app import create_app
from frontends.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "examples" / "site"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # The site is the example itself, so the config is read where it lives (a host file's parent is its site);
    # only the state directory moves, which the host reads from the environment.
    monkeypatch.setenv("BOARDCTRL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BSP_SITE", str(SITE))
    app = create_app(HostConfig.load(SITE / "hosts" / "hello.yaml"))
    with TestClient(app) as c:
        yield c


def call(client, tool, **args):
    body = client.post(f"/api/v1/tools/{tool}", json=args).json()
    assert body.get("ok"), body
    return body["result"]


def test_the_example_plugin_loads_and_fills_its_two_roles(client):
    status = call(client, "status")
    assert status["plugins"]["errors"] == {}
    assert "hello-platform" in status["plugins"]["loaded"]
    # It registered exactly the two roles the example claims, with the titles it gave them.
    providers = status["providers"]
    assert "hello-console" in providers["console"]["names"]
    assert "hello-flash" in providers["flash"]["names"]


def test_a_board_bound_to_the_plugin_answers_through_it(client):
    board = call(client, "dut_status", dut="hello-board")
    assert board["services"]["console"] == "hello-console"
    assert board["services"]["flash"] == "hello-flash"
    # The console role, in its passive and its command form.
    assert call(client, "dut_log", dut="hello-board", lines=5)["lines"] == ["hello from hello-board"]
    lease = call(client, "dut_acquire", dut="hello-board", holder="user:hello", purpose="example")["token"]
    ran = call(client, "dut_console", dut="hello-board", cmd="uptime", lease=lease)
    assert ran["output"] == "hello to `uptime`"


def test_the_example_flasher_reports_success_without_touching_anything(client, tmp_path):
    # A record without the version is refused: the example teaches that a flasher proves what it wrote.
    assert (SITE / "modules" / "hello-platform" / "module.py").is_file()
    image = tmp_path / "hello.hex"
    image.write_text(":00000001FF\n")
    import hashlib

    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    client.put(f"/api/v1/artifacts/{digest}", content=image.read_bytes())
    lease = call(client, "dut_acquire", dut="hello-board", holder="user:hello", purpose="example")["token"]
    job = call(client, "dut_flash", dut="hello-board", package=digest, lease=lease)
    done = call(client, "job_get", id=job["job"], wait=30)
    assert done["status"] == "succeeded", done
    # The host resolved the upload to a local file and handed it to the provider; the provider reports what it
    # did with that file, and that nothing was really written.
    assert done["result"]["written"] == "file"
    assert done["result"]["expect_version"] == "V1.2"
    assert done["result"]["runtime_verified"] is True
    assert "nothing was actually written" in done["result"]["message"]
