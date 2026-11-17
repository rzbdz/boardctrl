"""`site.reload`: a host reads its site again after a tool that manages it shipped a new copy."""

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from bsp.server.app import create_app
from bsp.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[1]

# A board a site declares for this host: the site's own console and download port, no hardware here.
BOARD = """id: board-1
host: h
board: board-x
soc: soc-a
console: {alias: board-1}
fastboot: {usb: "usb:1-1"}
control: {backend: human}
"""


def _sdks(c) -> list[str]:
    return [s["id"] for s in c.post("/api/v1/tools/sdk_list", json={}).json()["result"]]


def _duts(c) -> list[str]:
    return [d["id"] for d in c.post("/api/v1/tools/dut_list", json={}).json()["result"]]


def test_site_reload_takes_a_site_shipped_after_start(tmp_path, monkeypatch):
    """A host started with an empty site picks up the SDK catalog and SoC profiles shipped later; a catalog that does
    not load is refused and the running one kept."""
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    site = tmp_path / "site"
    p = site / "hosts" / "h.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("host: {id: h}\nserver: {state_dir: ../state}\nmodules:\n  sdk: {}\n"
                 "  dut: {probe: {pulse_ms: 200}, slots: {auto_console: false}}\n")
    with TestClient(create_app(HostConfig.load(p))) as c:
        assert _sdks(c) == []
        shutil.copytree(ROOT / "examples" / "site" / "sdk", site / "sdk")
        shutil.copytree(ROOT / "examples" / "site" / "socs", site / "socs")
        r = c.post("/api/v1/tools/site_reload", json={}).json()
        assert r["ok"] and "bsp-a" in r["result"]["sdks"] and {"soc-a", "soc-b"} <= set(r["result"]["socs"])
        assert "bsp-a" in _sdks(c)
        (site / "sdk" / "instances" / "broken.yaml").write_text("id: broken\nfamily: make-bsp\n")
        r = c.post("/api/v1/tools/site_reload", json={})
        assert r.status_code == 400 and "boot" in r.json()["error"]["message"]
        assert "bsp-a" in _sdks(c) and "broken" not in _sdks(c)


def test_site_reload_takes_the_boards_the_site_declares(tmp_path, monkeypatch):
    """The site's `duts/` is swapped with the rest: boards it declares after the host started become its boards on
    `site.reload`, and a record that does not load refuses the whole site (the running one is kept), as a catalog that
    does not load does — a host never takes half a site and reports it fine."""
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    site = tmp_path / "site"
    p = site / "hosts" / "h.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("host: {id: h}\nserver: {state_dir: ../state}\nmodules:\n  sdk: {}\n"
                 "  dut: {probe: {pulse_ms: 200}, slots: {auto_console: false}}\n")
    with TestClient(create_app(HostConfig.load(p))) as c:
        assert _duts(c) == []
        (site / "duts").mkdir()
        (site / "duts" / "board-1.yaml").write_text(BOARD)
        r = c.post("/api/v1/tools/site_reload", json={}).json()
        assert r["ok"] and r["result"]["duts"] == ["board-1"] and _duts(c) == ["board-1"]
        # A second board and a record that is not one: the site is refused and every board stays as it was.
        (site / "duts" / "board-2.yaml").write_text(BOARD.replace("board-1", "board-2"))
        (site / "duts" / "typo.yaml").write_text("id: typo\n")            # no host
        r = c.post("/api/v1/tools/site_reload", json={})
        assert r.status_code == 400 and "typo.yaml" in r.json()["error"]["message"] and "host" in r.json()["error"]["message"]
        assert _duts(c) == ["board-1"]
        # Fixed: both are read.
        (site / "duts" / "typo.yaml").write_text(BOARD.replace("board-1", "board-3"))
        r = c.post("/api/v1/tools/site_reload", json={}).json()
        assert sorted(r["result"]["duts"]) == ["board-1", "board-2", "board-3"]
        # A record the site no longer has is no longer a board (the site's catalog is the site's).
        (site / "duts" / "board-2.yaml").unlink()
        c.post("/api/v1/tools/site_reload", json={})
        assert sorted(_duts(c)) == ["board-1", "board-3"]
