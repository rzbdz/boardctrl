"""`dut flash`: packages fetched or uploaded, unpacked safely, written by a flash hook with progress."""

import hashlib
import http.server
import io
import json
import os
import socket
import stat
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.errors import InvalidRequest
from core.dut import flashpkg
from core.dut.artifacts import ArtifactStore
from frontends.server.app import create_app
from frontends.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "tests" / "fixtures" / "site"
MOCK = SITE / "hosts" / "dev-mock.yaml"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tgz(files: dict[str, bytes], links: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        for name, target in (links or {}).items():
            ti = tarfile.TarInfo(name)
            ti.type, ti.linkname = tarfile.SYMTYPE, target
            t.addfile(ti)
    return buf.getvalue()


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def web(tmp_path):
    """A local HTTP server for files in tmp_path/www."""
    root = tmp_path / "www"
    root.mkdir()

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.root, srv.url = root, f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()


# ---------------------------------------------------------------- package --
def test_urls_other_than_http_are_refused():
    for bad in ("file:///etc/passwd", "ftp://h/p.tgz", "http:///p.tgz", "http://h/a b.tgz", "http://h/\n",
                "javascript:alert(1)", "/local/path"):
        with pytest.raises(InvalidRequest):
            flashpkg.check_url(bad)
    assert flashpkg.check_url("https://h.example/p.tgz?x=1") == "https://h.example/p.tgz?x=1"


def test_fetch_downloads_once_and_checks_the_hash(tmp_path, web):
    store, lines = ArtifactStore(tmp_path / "art"), []
    data = _tgz({"pkg/fastboot.yaml": b"actions: []\n"})
    (web.root / "pkg.tgz").write_bytes(data)
    with pytest.raises(InvalidRequest) as e:
        flashpkg.fetch(store, f"{web.url}/pkg.tgz", "0" * 64, lines.append)
    assert "hashes to" in e.value.message and store.find(_sha(data)) is None
    assert not list((tmp_path / "art" / ".incoming").iterdir())          # nothing half-written left
    r = flashpkg.fetch(store, f"{web.url}/pkg.tgz", _sha(data), lines.append)
    assert r["fetched"] and Path(r["path"]).read_bytes() == data and Path(r["path"]).name == "pkg.tgz"
    assert flashpkg.fetch(store, f"{web.url}/gone.tgz", _sha(data), lines.append)["fetched"] is False  # cached
    with pytest.raises(Exception) as e:
        flashpkg.fetch(store, f"{web.url}/missing.tgz", None, lines.append)
    assert "404" in str(e.value)
    with pytest.raises(InvalidRequest):
        flashpkg.fetch(store, f"{web.url}/pkg.tgz", None, lines.append, max_bytes=10)


@pytest.mark.parametrize("bad", [
    _tgz({"../evil": b"x"}),
    _tgz({"/abs/evil": b"x"}),
    _tgz({"pkg/a": b"x"}, links={"pkg/link": "/etc/passwd"}),
    _zip({"../../evil": b"x"}),
    _zip({"/abs": b"x"}),
])
def test_unpack_refuses_entries_outside_the_package(tmp_path, bad):
    p = tmp_path / "bad.pkg"
    p.write_bytes(bad)
    with pytest.raises(InvalidRequest):
        flashpkg.unpack(p, tmp_path / "out")
    assert not (tmp_path / "evil").exists()


def test_unpack_descends_into_one_top_level_directory(tmp_path):
    for i, data in enumerate((_tgz({"pkg/fastboot.yaml": b"a", "pkg/img/u-boot.itb": b"b"}),
                              _zip({"pkg/fastboot.yaml": b"a", "pkg/img/u-boot.itb": b"b"}))):
        p = tmp_path / f"p{i}"
        p.write_bytes(data)
        d = flashpkg.unpack(p, tmp_path / f"out{i}")
        assert d.name == "pkg" and (d / "fastboot.yaml").read_bytes() == b"a" and (d / "img" / "u-boot.itb").exists()
    p = tmp_path / "flat"
    p.write_bytes(_tgz({"fastboot.yaml": b"a", "u-boot.itb": b"b"}))
    assert flashpkg.unpack(p, tmp_path / "flat-out") == tmp_path / "flat-out"
    p.write_bytes(b"not an archive")
    with pytest.raises(InvalidRequest):
        flashpkg.unpack(p, tmp_path / "x")


LAYOUT = json.dumps({"format": "gpt", "partitions": [
    {"name": "bootfs", "image": "bootfs.ext4"}, {"name": "rootfs", "image": ["rootfs.in", "rootfs.ext4"]},
    {"name": "esp", "image": "esp.vfat"}]}).encode()


@pytest.mark.parametrize("pack", [_tgz, _zip])
def test_a_chosen_partition_unpacks_only_what_it_needs(tmp_path, pack):
    """`want` leaves the rest of the package on the wire: its layouts, `fastboot.yaml`, what its stage actions use and
    the chosen partitions' images — a list of inputs is kept whole, since which of them the flasher picks is its own
    business."""
    flow = b"actions:\n  - stage:\n      file: 'factory/FSBL.bin'\n  - multi_flash:\n      relate_partition: ['partition_universal.json']\n"
    files = {"pkg/fastboot.yaml": flow, "pkg/partition_universal.json": LAYOUT,
             "pkg/bootfs.ext4": b"b", "pkg/rootfs.in": b"i", "pkg/rootfs.ext4": b"r", "pkg/esp.vfat": b"e",
             "pkg/u-boot.itb": b"u", "pkg/factory/FSBL.bin": b"f"}
    p = tmp_path / "p.pkg"
    p.write_bytes(pack(files))
    d = flashpkg.unpack(p, tmp_path / "out", want=flashpkg.only_entries(p, ["rootfs"]))
    assert sorted(str(x.relative_to(d)) for x in d.rglob("*") if x.is_file()) == [
        "factory/FSBL.bin", "fastboot.yaml", "partition_universal.json", "rootfs.ext4", "rootfs.in"]
    # A name no layout has unpacks nothing of its images, so the flasher gets the whole package to report it against.
    d2 = flashpkg.unpack(p, tmp_path / "out2", want=flashpkg.only_entries(p, ["nothing"]))
    assert sorted(str(x.relative_to(d2)) for x in d2.rglob("*") if x.is_file()) == sorted(str(Path(n).relative_to("pkg"))
                                                                                          for n in files)


@pytest.mark.parametrize("pack", [_tgz, _zip])
def test_a_package_that_keeps_everything_in_one_directory_names_its_files_from_there(tmp_path, pack):
    """A vendor package is `pkg/fastboot.yaml` + `pkg/partition_universal.json` naming `bootfs.ext4`, not
    `pkg/bootfs.ext4`: `unpack` descends into `pkg`, so a layout's own name is relative to it."""
    flow = b"actions:\n  - stage:\n      file: 'u-boot.itb'\n  - multi_flash:\n      relate_partition: ['partition_universal.json']\n"
    files = {"vendor/fastboot.yaml": flow, "vendor/partition_universal.json": LAYOUT,
             "vendor/bootfs.ext4": b"b", "vendor/rootfs.ext4": b"r", "vendor/u-boot.itb": b"u", "vendor/esp.vfat": b"e"}
    p = tmp_path / "p.pkg"
    p.write_bytes(pack(files))
    d = flashpkg.unpack(p, tmp_path / "out", want=flashpkg.only_entries(p, ["bootfs"]))
    assert d.name == "vendor"
    assert sorted(x.name for x in d.iterdir()) == ["bootfs.ext4", "fastboot.yaml", "partition_universal.json", "u-boot.itb"]


def test_a_package_that_is_not_a_spacemit_one_is_handed_over_whole(tmp_path):
    """No layout names a partition asked for — a package this reads nothing of: every file is kept, so the flasher
    says what a chosen partition means here instead of the package arriving half empty."""
    p = tmp_path / "p.pkg"
    p.write_bytes(_tgz({"whatever.bin": b"x", "notes.txt": b"n"}))
    d = flashpkg.unpack(p, tmp_path / "out", want=flashpkg.only_entries(p, ["boot"]))
    assert sorted(x.name for x in d.iterdir()) == ["notes.txt", "whatever.bin"]


# ---------------------------------------------------------------- dut.flash --
FAKE_FLASHER = """
import os, time
pkg = req['package_dir']
with open(os.environ['FLASH_LOG'], 'w') as f:
    json.dump({**req, 'files': sorted(os.listdir(pkg))}, f)
print('flashing ' + req['usb'], flush=True)
for n in (10, 55, 100):
    print(json.dumps({'progress': n, 'msg': 'part %d' % n}), flush=True)
    time.sleep(float(os.environ.get('FLASH_SLEEP', '0')))
if os.environ.get('FLASH_FAIL'):
    print(json.dumps({'flashed': ['bootfs'], 'error': 'partition rootfs did not take', 'hint': 'check the cable'}))
    sys.exit(2)
print(json.dumps({'flashed': ['bootfs', 'rootfs']}))
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    """The mock host (mock-board in download mode on usb:0-0) with a fake flash hook `fake`."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fb = bindir / "fastboot"
    fb.write_text("#!/bin/sh\n[ \"$1\" = devices ] && printf 'dfu\\tDFU download\\n    usb:0-0\\n'\necho OKAY\n")
    fb.chmod(0o755)
    hooks = tmp_path / "hooks" / "flash"
    hooks.mkdir(parents=True)
    h = hooks / "fake"
    h.write_text("#!/usr/bin/env python3\nimport json,sys\nreq=json.load(sys.stdin)\n" + FAKE_FLASHER)
    h.chmod(h.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BOARDCTRL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BOARDCTRL_HOOKS_PATH", str(tmp_path / "hooks"))
    monkeypatch.setenv("FLASH_LOG", str(tmp_path / "flash.json"))
    with TestClient(create_app(HostConfig.load(MOCK))) as c:
        c.tmp = tmp_path
        yield c


def _call(c, tool, **args):
    r = c.post(f"/api/v1/tools/{tool}", json=args).json()
    return r["result"] if r["ok"] else r["error"]


def test_flasher_choice_order(host):
    mod = host.app.state.hub.modules["dut"]
    b = mod.board("mock-board")
    assert mod.flasher_for(b) == ("spacemit-pkg", "the default")
    mod.socs.profiles.setdefault("soc-b", {})["flash"] = {"flasher": "soc-one"}
    assert mod.flasher_for(b)[0] == "soc-one"
    b.spec.raw["flash"] = {"flasher": "board-one"}
    assert mod.flasher_for(b)[0] == "board-one"
    assert mod.flasher_for(b, "asked") == ("asked", "asked for")


def test_flash_uploaded_package_runs_the_hook_with_progress(host):
    # Host env, network and a product directory are in the hook's request; args expand from the env.
    mod = host.app.state.hub.modules["dut"]
    mod.network_cfg = {"board_subnet": "10.0.0.0/24", "host_address": "10.0.0.2",
                       "export_address": "203.0.113.5"}
    mod._network = None
    mod.ctx.env.update({"BOARDCTRL_TFTP_SERVER": "10.0.0.9", "BOARDCTRL_SDK_OUTPUT": "/srv/sdk"})
    mod.flash_cfg["args"] = {"tftp": "${BOARDCTRL_TFTP_SERVER}/pkg", "fallback": "${NOPE:-none}"}
    data = _tgz({"pkg/fastboot.yaml": b"actions: []\n", "pkg/partition_universal.json": b"{}",
                 "pkg/rootfs.ext4": b"r" * 1000})
    sha = _sha(data)
    assert host.put(f"/api/v1/artifacts/{sha}?name=pkg.tgz", content=data).json()["ok"]
    assert _call(host, "dut_flash", dut="mock-board", package=sha, flasher="fake")["code"] == "forbidden"
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    e = _call(host, "dut_flash", dut="mock-board", package=sha, flasher="nope", lease=token)
    assert e["code"] == "not_found" and "fake" in e["hint"]
    assert _call(host, "dut_flash", dut="mock-board", package="ftp://h/p.tgz", lease=token)["code"] == "invalid_request"
    assert _call(host, "dut_flash", dut="mock-board", package="1" * 64, lease=token)["code"] == "invalid_request"

    r = _call(host, "dut_flash", dut="mock-board", package=sha, flasher="fake", lease=token, reset=False)
    assert r["flasher"] == "fake" and r["package"] == "pkg.tgz"
    job = _call(host, "job_get", id=r["job"], wait=20)
    assert job["status"] == "succeeded", job
    assert job["kind"] == "flash" and job["progress"] == 100
    assert job["result"]["flashed"] == ["bootfs", "rootfs"] and job["result"]["flasher"] == "fake"
    assert any("55%" in l for l in job["lines"]) and any("flashing usb:0-0" in l for l in job["lines"])
    req = json.loads((host.tmp / "flash.json").read_text())
    assert req["files"] == ["fastboot.yaml", "partition_universal.json", "rootfs.ext4"]
    assert req["timeout"] == 3600
    assert req["usb"] == "usb:0-0" and req["dut"] == "mock-board" and req["artifacts"]["package"].endswith("pkg.tgz")
    assert req["product_dir"] == req["package_dir"]              # the unpacked package is the product
    assert req["env"] == {"BOARDCTRL_TFTP_SERVER": "10.0.0.9", "BOARDCTRL_SDK_OUTPUT": "/srv/sdk"}
    assert req["network"] == {"board_subnet": "10.0.0.0/24", "host_address": "10.0.0.2",
                              "export_address": "203.0.113.5", "console": "mock-board"}
    assert req["args"] == {"tftp": "10.0.0.9/pkg", "fallback": "none"}
    assert not Path(req["package_dir"]).exists()                  # the unpacked copy is gone afterwards
    assert any(j["id"] == r["job"] and j["progress"] == 100 for j in _call(host, "job_list"))


def test_flash_downloads_a_url_and_reports_the_hooks_error(host, web, monkeypatch):
    data = _zip({"fastboot.yaml": b"actions: []\n", "u-boot.itb": b"u"})
    (web.root / "p.zip").write_bytes(data)
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    r = _call(host, "dut_flash", dut="mock-board", package=f"{web.url}/p.zip", sha256="0" * 64, flasher="fake",
              lease=token, reset=False)
    job = _call(host, "job_get", id=r["job"], wait=20)
    assert job["status"] == "failed" and "hashes to" in job["error"]["message"]

    monkeypatch.setenv("FLASH_FAIL", "1")
    r = _call(host, "dut_flash", dut="mock-board", package=f"{web.url}/p.zip", sha256=_sha(data), flasher="fake",
              lease=token, reset=False)
    job = _call(host, "job_get", id=r["job"], wait=20)
    assert job["status"] == "failed" and "rootfs did not take" in job["error"]["message"]
    assert job["error"]["hint"] == "check the cable"
    req = json.loads((host.tmp / "flash.json").read_text())
    assert req["files"] == ["fastboot.yaml", "u-boot.itb"] and req["artifacts"]["flash-zip"].endswith("p.zip")


def test_cli_flash_sends_a_directory_and_prints_progress(tmp_path, monkeypatch, capsys):
    import uvicorn

    from frontends.cli.main import main

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fb = bindir / "fastboot"
    fb.write_text("#!/bin/sh\n[ \"$1\" = devices ] && printf 'dfu\\tDFU download\\n    usb:0-0\\n'\necho OKAY\n")
    fb.chmod(0o755)
    hooks = tmp_path / "hooks" / "flash"
    hooks.mkdir(parents=True)
    (hooks / "fake").write_text("#!/usr/bin/env python3\nimport json,sys\nreq=json.load(sys.stdin)\n" + FAKE_FLASHER)
    (hooks / "fake").chmod(0o755)
    (hooks / "planner").write_text(PLANNING_FLASHER)
    (hooks / "planner").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BOARDCTRL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BOARDCTRL_HOOKS_PATH", str(tmp_path / "hooks"))
    monkeypatch.setenv("FLASH_LOG", str(tmp_path / "flash.json"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(HostConfig.load(MOCK)), host="127.0.0.1", port=port,
                                           log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        url = ["--url", f"http://127.0.0.1:{port}", "--stateless"]
        assert main([*url, "--json", "dut", "acquire", "mock-board", "--holder", "t"]) == 0
        token = json.loads(capsys.readouterr().out)["result"]["token"]
        pkg = tmp_path / "my-image"
        (pkg / "img").mkdir(parents=True)
        (pkg / "fastboot.yaml").write_text("actions: []\n")
        (pkg / "img" / "rootfs.ext4").write_bytes(b"r" * 4096)
        assert main([*url, "--json", "dut", "flash", "mock-board", "--package", str(pkg), "--flasher", "fake",
                     "--lease", token, "--no-reset"]) == 0
        cap = capsys.readouterr()
        assert "sent my-image.tar" in cap.err and "55%" in cap.err
        res = json.loads(cap.out)["result"]
        assert res["flasher"] == "fake" and res["flashed"] == ["bootfs", "rootfs"] and res["job"]
        assert any("100%" in l for l in res["lines"]) and isinstance(res["elapsed_s"], (int, float))
        req = json.loads((tmp_path / "flash.json").read_text())
        assert req["files"] == ["fastboot.yaml", "img"]
        assert main([*url, "dut", "flash", "mock-board", "--package", str(pkg), "--flasher", "fake",
                     "--lease", token, "--no-reset"]) == 0
        assert "my-image.tar already on dev-mock" in capsys.readouterr().out     # same tree, same tar
        # --plan: the flasher's reading of the package, as JSON and as a table; --partitions reaches the flasher.
        assert main([*url, "--json", "dut", "flash", "mock-board", "--package", str(pkg), "--flasher", "planner",
                     "--lease", token, "--plan"]) == 0
        res = json.loads(capsys.readouterr().out)["result"]
        assert res["plan"]["package"] == "my-image.tar" and res["plan"]["partial"] is True
        assert [p["name"] for p in res["plan"]["media"][0]["partitions"]] == ["fastboot.yaml", "img"]
        assert main([*url, "dut", "flash", "mock-board", "--package", str(pkg), "--flasher", "planner",
                     "--lease", token, "--plan"]) == 0
        assert "my-image.tar: partitions can be chosen (--partitions a,b)\ndisk\n  fastboot.yaml" in capsys.readouterr().out
        assert main([*url, "--json", "dut", "flash", "mock-board", "--package", str(pkg), "--flasher", "planner",
                     "--lease", token, "--no-reset", "--partitions", "img"]) == 0
        assert json.loads(capsys.readouterr().out)["result"]["flashed"] == ["img"]
        assert main([*url, "dut", "flash", "mock-board", "--package", "file:///etc/x.tgz", "--lease", token]) == 1
        assert "not allowed" in capsys.readouterr().err
    finally:
        server.should_exit = True
        t.join(5)


def test_a_running_job_can_be_cancelled(host):
    """`dut cancel` asks the running job to stop: it ends before its next step, with status cancelled."""
    import time

    from core.jobs import JobManager
    from core.events import EventBus

    bus = EventBus()
    jobs = JobManager(bus)
    step = {"n": 0}

    def body(log):
        for _ in range(100):
            log.check()
            step["n"] += 1
            time.sleep(0.02)

    job = jobs.start("boot", "d1: boot", body, dut="d1")
    for _ in range(100):
        if step["n"] >= 2:
            break
        time.sleep(0.02)
    job.cancel()
    jobs.wait(job.id, 5)
    assert job.status == "cancelled" and step["n"] < 100


NOT_READY = """#!/usr/bin/env python3
# bsp-hook: check
import json, os, sys
req = json.load(sys.stdin)
if req.get('check'):
    open(os.environ['CHECK_LOG'], 'a').write(json.dumps(req) + '\\n')
    print(json.dumps({'ready': False, 'reason': 'no vendortool for riscv64 on this host', 'hint': 'install vendortool'}))
    sys.exit(2)
open(os.environ['CHECK_LOG'], 'a').write('RAN\\n')
sys.exit(1)
"""


def _not_ready(host, name="vendortool"):
    h = host.tmp / "hooks" / "flash" / name
    h.write_text(NOT_READY)
    h.chmod(0o755)
    os.environ["CHECK_LOG"] = str(host.tmp / "check.log")
    return host.tmp / "check.log"


def test_an_unready_flasher_is_passed_over_before_anything_is_fetched(host, web, monkeypatch):
    # The SoC prefers a flasher whose tool this host lacks; its fallback is ready: the package is written by that,
    # the job says which was passed over and why, and the unready hook is only ever asked, never run.
    log = _not_ready(host)
    mod = host.app.state.hub.modules["dut"]
    mod.socs.profiles.setdefault("soc-b", {})["flash"] = {"flasher": "vendortool", "fallback": ["fake"]}
    data = _zip({"fastboot.yaml": b"actions: []\n", "u-boot.itb": b"u"})
    (web.root / "p.zip").write_bytes(data)
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    r = _call(host, "dut_flash", dut="mock-board", package=f"{web.url}/p.zip", lease=token, reset=False)
    assert r["flasher"] == "fake", r
    job = _call(host, "job_get", id=r["job"], wait=20)
    assert job["status"] == "succeeded", job
    assert any("passed over vendortool" in l and "no vendortool for riscv64" in l for l in job["lines"])
    calls = log.read_text().splitlines()
    assert calls and "RAN" not in calls and json.loads(calls[0])["check"] is True


def test_no_ready_flasher_fails_at_once_and_says_what_is_there(host, web):
    # Nothing ready: the call fails before the job starts (no download), naming each candidate's reason.
    _not_ready(host)
    mod = host.app.state.hub.modules["dut"]
    mod.socs.profiles.setdefault("soc-b", {})["flash"] = {"flasher": "vendortool"}
    mod.hooks.dirs = [d for d in mod.hooks.dirs if (d / "flash" / "vendortool").exists()]   # only that hook here
    (web.root / "big.tgz").write_bytes(b"x" * 10)
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    e = _call(host, "dut_flash", dut="mock-board", package=f"{web.url}/big.tgz", lease=token, reset=False)
    assert e["code"] == "hardware_error", e
    assert "no vendortool for riscv64" in e["hint"] and "spacemit-pkg (the default): not on this host" in e["hint"]
    assert not list((host.tmp / "state").rglob("big.tgz"))          # nothing was fetched


def test_a_flasher_asked_for_by_name_is_checked_and_the_ready_ones_listed(host):
    _not_ready(host)
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    sha = "a" * 64
    e = _call(host, "dut_flash", dut="mock-board", package=sha, flasher="bogus", lease=token)
    assert e["code"] == "not_found" and "可用 flasher" in e["hint"] and "fake" in e["hint"]
    e = _call(host, "dut_flash", dut="mock-board", package=sha, flasher="vendortool", lease=token)
    assert e["code"] == "hardware_error" and "no vendortool" in e["message"]
    assert "install vendortool" in e["hint"] and "fake" in e["hint"] and "vendortool" not in e["hint"].split("ready flashers here:")[1]


def test_hooks_declare_check_explicitly(tmp_path):
    from core.hooks import HookRunner
    d = tmp_path / "flash"
    d.mkdir()
    (d / "plain").write_text("#!/bin/sh\nexit 1\n")
    (d / "plain").chmod(0o755)
    hr = HookRunner((str(tmp_path),))
    assert not hr.declares("flash", "plain", "check")
    assert hr.check("flash", "plain", {})["ready"] is True              # never run in a mode it does not know
    assert hr.check("flash", "missing", {})["ready"] is False
    shipped = HookRunner((str(ROOT / "platforms" / "spacemit" / "hooks"),))
    assert shipped.declares("flash", "spacemit-pkg", "check")
    assert shipped.check("flash", "spacemit-pkg", {"fastboot": "no-such-fastboot-binary"})["reason"].startswith("no no-such")


def test_flashing_one_url_twice_downloads_it_once_unless_refreshed(host, web):
    data = _zip({"fastboot.yaml": b"actions: []\n", "u-boot.itb": b"u"})
    (web.root / "daily.zip").write_bytes(data)
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    url = f"{web.url}/daily.zip?build=1"

    def flash(**kw):
        r = _call(host, "dut_flash", dut="mock-board", package=url, flasher="fake", lease=token, reset=False, **kw)
        job = _call(host, "job_get", id=r["job"], wait=20)
        assert job["status"] == "succeeded", job
        return job["lines"]

    assert any("downloading daily.zip" in l for l in flash())
    lines = flash()
    assert not any("downloading" in l for l in lines)
    assert any("from the URL cache" in l and _sha(data)[:12] in l for l in lines)
    assert any("downloading daily.zip" in l for l in flash(refresh=True))


# ------------------------------------------------------- control preflight --
def _hold(host, pin="P15", holder="another-program"):
    """Make the mock line behind `pin` look held by something else, as a stale export or another program looks."""
    gpio = host.app.state.hub.modules["dut"].ctx.service("gpio")
    gpio.backend.inner.held[gpio.resolve(pin).gpio] = holder
    return gpio


def test_a_busy_reset_line_fails_the_flash_before_anything_is_fetched(host, web):
    """The board's reset line cannot be driven: the flash fails at once, naming the line and why, and nothing is
    downloaded, reset or pulsed."""
    gpio = _hold(host)
    (web.root / "big.tgz").write_bytes(b"x" * 10)
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    e = _call(host, "dut_flash", dut="mock-board", package=f"{web.url}/big.tgz", flasher="fake", lease=token)
    assert e["code"] == "hardware_error", e
    assert "mock-board: its reset line (P15) cannot be driven now" in e["message"]
    assert "held by 'another-program'" in e["message"]
    assert "nothing was reset or fetched" in e["hint"] and "boardctrl gpio get P15" in e["hint"]
    assert e["details"]["role"] == "reset" and e["details"]["cause"]["code"] == "hardware_error"
    assert not list((host.tmp / "state").rglob("big.tgz"))          # nothing was fetched
    assert not any(h[0] == gpio.resolve("P15").gpio and h[1] == "out" for h in gpio.backend.inner.history)
    assert not [j for j in _call(host, "job_list") if j["kind"] == "flash"]

    # reset and boot-from-files say the same before they start a job
    e = _call(host, "dut_reset", dut="mock-board", lease=token, fdl=True)
    assert e["code"] == "hardware_error" and "reset line (P15) cannot be driven now" in e["message"]
    e = _call(host, "dut_boot_files", dut="mock-board", files="a" * 64, lease=token, reset=True)
    assert e["code"] == "hardware_error" and "reset line (P15) cannot be driven now" in e["message"]

    gpio.backend.inner.held.clear()                                    # line free: the flash starts
    (web.root / "p.zip").write_bytes(_zip({"fastboot.yaml": b"actions: []\n"}))
    r = _call(host, "dut_flash", dut="mock-board", package=f"{web.url}/p.zip", flasher="fake", lease=token,
              reset=False)
    assert r["job"], r


def test_no_reset_flash_fails_before_the_job_when_the_board_is_in_another_stage(host, web, monkeypatch):
    """--no-reset: the device on the bus is not the SoC's download mode: refused before anything is fetched, naming
    what is there."""
    fb = host.tmp / "bin" / "fastboot"
    fb.write_text("#!/bin/sh\n[ \"$1\" = devices ] && printf 'abc\\tAndroid Fastboot\\n    usb:0-0\\n'\necho OKAY\n")
    (web.root / "big.tgz").write_bytes(b"x" * 10)
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    e = _call(host, "dut_flash", dut="mock-board", package=f"{web.url}/big.tgz", flasher="fake", lease=token,
              reset=False)
    assert e["code"] == "hardware_error", e
    assert "'Android Fastboot'" in e["message"] and "not the download mode" in e["message"]
    assert "drop --no-reset" in e["hint"]
    assert not list((host.tmp / "state").rglob("big.tgz"))


def test_cancel_with_nothing_running_names_the_boards_last_job(host):
    """Nothing to cancel: the answer says so and what the board's last job did, so a caller knows it already ended."""
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    e = _call(host, "dut_cancel", dut="mock-board", lease=token)
    assert e["code"] == "invalid_request" and "no job running, and the host keeps no record of one run on it" in e["message"]
    assert e["details"] == {"running": False, "last_job": None}

    from core.errors import HardwareError

    mod = host.app.state.hub.modules["dut"]

    def body(log):
        raise HardwareError("GPIO127: Device or resource busy", hint="free the line")

    job = mod._start_job("mock-board", "flash", body, kind="flash")
    host.app.state.hub.jobs.wait(job.id, 10)
    e = _call(host, "dut_cancel", dut="mock-board", lease=token)
    assert e["code"] == "invalid_request", e
    assert e["message"] == f"mock-board: no job running; its last job {job.id} (flash) failed: GPIO127: Device or resource busy"
    assert f"boardctrl job get {job.id}" in e["hint"]
    last = e["details"]["last_job"]
    assert last["id"] == job.id and last["status"] == "failed" and last["error"]["hint"] == "free the line"


PLANNING_FLASHER = """#!/usr/bin/env python3
# bsp-hook: plan only
import json, os, sys
req = json.load(sys.stdin)
if req.get('plan'):
    files = sorted(os.listdir(req['package_dir']))
    print('reading ' + ', '.join(files), flush=True)
    print(json.dumps({'plan': {'package': 'x', 'partial': True, 'notes': [], 'media': [{'id': 'disk', 'label': 'disk',
          'size': None, 'partitions': [{'name': n, 'offset': None, 'size': None, 'image': n, 'payload': 1,
          'selectable': True} for n in files]}]}}))
    sys.exit(0)
with open(os.environ['FLASH_LOG'], 'w') as f:
    json.dump({**req, 'files': sorted(os.listdir(req['package_dir']))}, f)
print(json.dumps({'flashed': req['args'].get('only') or ['all']}))
"""


def _planning(host):
    h = host.tmp / "hooks" / "flash" / "planner"
    h.write_text(PLANNING_FLASHER)
    h.chmod(h.stat().st_mode | stat.S_IEXEC)


def test_a_plan_reads_the_package_and_touches_no_board(host, web, monkeypatch):
    """`plan`: fetched and unpacked as a flash would be, read by the flasher; no reset, no download port, the
    board not busy, and a flasher that cannot plan refused before anything is fetched."""
    _planning(host)
    data = _zip({"fastboot.yaml": b"actions: []\n", "u-boot.itb": b"u"})
    (web.root / "p.zip").write_bytes(data)
    mod = host.app.state.hub.modules["dut"]
    board = mod.board("mock-board")
    monkeypatch.setattr(board.control, "preflight", lambda: (_ for _ in ()).throw(AssertionError("no reset")))
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    assert _call(host, "dut_flash", dut="mock-board", package=f"{web.url}/p.zip", plan=True, flasher="fake",
                 lease=token)["code"] == "invalid_request"          # `fake` does not declare plan
    r = _call(host, "dut_flash", dut="mock-board", package=f"{web.url}/p.zip", plan=True, flasher="planner", lease=token)
    assert r["plan"] is True and r["job"].startswith("flash-plan-")
    assert "mock-board" not in mod._busy
    job = _call(host, "job_get", id=r["job"], wait=20)
    assert job["status"] == "succeeded", job
    plan = job["result"]["plan"]
    assert plan["package"] == "p.zip" and [p["name"] for p in plan["media"][0]["partitions"]] == ["fastboot.yaml", "u-boot.itb"]
    assert any("nothing was written" in l for l in job["lines"])
    assert not (host.tmp / "flash.json").exists()


def test_chosen_partitions_reach_the_flasher_and_a_flasher_without_them_refuses(host):
    _planning(host)
    data = _tgz({"pkg/a.img": b"a", "pkg/b.img": b"b"})
    sha = _sha(data)
    assert host.put(f"/api/v1/artifacts/{sha}?name=pkg.tgz", content=data).json()["ok"]
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    e = _call(host, "dut_flash", dut="mock-board", package=sha, flasher="fake", lease=token, reset=False, partitions="a")
    assert e["code"] == "invalid_request" and "only some partitions" in e["message"]
    e = _call(host, "dut_flash", dut="mock-board", package=sha, flasher="planner", lease=token, reset=False, partitions="a;b")
    assert e["code"] == "invalid_request" and "not a partition name" in e["message"]
    r = _call(host, "dut_flash", dut="mock-board", package=sha, flasher="planner", lease=token, reset=False,
              partitions="boot, rootfs,boot")
    assert r["partitions"] == ["boot", "rootfs"]
    job = _call(host, "job_get", id=r["job"], wait=20)
    assert job["status"] == "succeeded", job
    assert json.loads((host.tmp / "flash.json").read_text())["args"]["only"] == ["boot", "rootfs"]
    assert job["result"]["flashed"] == ["boot", "rootfs"]


def test_a_chosen_partition_is_the_only_image_a_package_hands_the_flasher(host):
    """The package is unpacked for the parts chosen: a 4 GB image of a partition nobody asked for never lands on the
    host's disk. What the flasher is given is still the package, with its own layout and the images chosen."""
    _planning(host)
    layout = json.dumps({"format": "gpt", "partitions": [
        {"name": "boot", "image": "boot.img"}, {"name": "rootfs", "image": "rootfs.ext4"}]}).encode()
    data = _tgz({"pkg/fastboot.yaml": b"actions: []\n", "pkg/partition_4M.json": layout,
                 "pkg/boot.img": b"b", "pkg/rootfs.ext4": b"r" * 4096})
    sha = _sha(data)
    assert host.put(f"/api/v1/artifacts/{sha}?name=pkg.tgz", content=data).json()["ok"]
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    r = _call(host, "dut_flash", dut="mock-board", package=sha, flasher="planner", lease=token, reset=False, partitions="boot")
    job = _call(host, "job_get", id=r["job"], wait=20)
    assert job["status"] == "succeeded", job
    seen = json.loads((host.tmp / "flash.json").read_text())
    assert seen["args"]["only"] == ["boot"]
    assert seen["files"] == ["boot.img", "fastboot.yaml", "partition_4M.json"]


FAKE_SSH = """#!/bin/bash
# fake ssh: drop the options, note the target, run the command here
while [ "$1" = "-o" ]; do shift 2; done
echo "$1" >> "$(dirname "$0")/ssh.log"
shift
exec bash -c "$*"
"""
FAKE_SCP = """#!/bin/bash
args=()
while [ $# -gt 0 ]; do case "$1" in -q) shift;; -o) shift 2;; *) args+=("$1"); shift;; esac; done
exec cp "${args[0]#*:}" "${args[1]}"
"""


def test_a_package_on_a_build_server_is_pulled_over_ssh_and_only_from_one_the_site_declares(host, monkeypatch):
    """ssh://user@host/path: from a build server in the site's sdk/servers (as its user), hashed there and checked
    here; another host, another user, a relative or `..` path are refused before anything is fetched."""
    from core.dut import flashpkg

    bindir = host.tmp / "bin"
    for name, text in (("ssh", FAKE_SSH), ("scp", FAKE_SCP)):
        (bindir / name).write_text(text)
        (bindir / name).chmod(0o755)
    out = host.tmp / "build-out"
    out.mkdir()
    data = _zip({"fastboot.yaml": b"actions: []\n", "u-boot.itb": b"u" * 50})
    (out / "flash_image.zip").write_bytes(data)
    servers = host.app.state.hub.modules["dut"].sdks.servers
    assert flashpkg.check_ssh(f"ssh://dev@192.0.2.10{out}/flash_image.zip", servers) == ("dev@192.0.2.10", f"{out}/flash_image.zip")
    assert flashpkg.check_ssh(f"ssh://build-b{out}/x.zip", servers)[0] == "dev@192.0.2.11"      # by its id, as its user
    for bad, why in ((f"ssh://dev@203.0.113.9{out}/x.zip", "not one of the site's build servers"),
                     (f"ssh://root@192.0.2.10{out}/x.zip", "not one of the site's build servers"),
                     ("ssh://dev@192.0.2.10/out/../etc/shadow", "not a plain absolute file path"),
                     ("ssh://dev@192.0.2.10/out/x;reboot", "not a plain absolute file path"),
                     ("ssh://dev:pw@192.0.2.10/out/x.zip", "not a usable ssh package link")):
        with pytest.raises(InvalidRequest) as e:
            flashpkg.check_ssh(bad, servers)
        assert why in e.value.message, bad

    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    e = _call(host, "dut_flash", dut="mock-board", package=f"ssh://dev@203.0.113.9{out}/flash_image.zip", flasher="fake",
              lease=token, reset=False)
    assert e["code"] == "invalid_request" and "dev@192.0.2.10" in e["hint"]
    assert not (bindir / "ssh.log").exists()                     # refused before anything was asked of a server
    url = f"ssh://dev@192.0.2.10{out}/flash_image.zip"
    job = _call(host, "job_get", id=_call(host, "dut_flash", dut="mock-board", package=url, sha256="0" * 64,
                                          flasher="fake", lease=token, reset=False)["job"], wait=20)
    assert job["status"] == "failed" and "hashes to" in job["error"]["message"]
    job = _call(host, "job_get", id=_call(host, "dut_flash", dut="mock-board", package=url, sha256=_sha(data),
                                          flasher="fake", lease=token, reset=False)["job"], wait=20)
    assert job["status"] == "succeeded", job
    assert json.loads((host.tmp / "flash.json").read_text())["files"] == ["fastboot.yaml", "u-boot.itb"]
    assert (bindir / "ssh.log").read_text().split() == ["dev@192.0.2.10"] * 2


def test_a_hook_is_told_the_hook_search_path(host, tmp_path):
    """$BOARDCTRL_HOOK_DIRS: a flasher can find another one (to read a package the way the platform's does)."""
    h = host.tmp / "hooks" / "flash" / "telling"
    h.write_text("#!/bin/sh\nprintf '{\"dirs\": \"%s\"}\\n' \"$BOARDCTRL_HOOK_DIRS\"\n")
    h.chmod(0o755)
    mod = host.app.state.hub.modules["dut"]
    got = mod.hooks.run("flash", "telling", {})
    assert str(host.tmp / "hooks") in got.data["dirs"].split(":")
