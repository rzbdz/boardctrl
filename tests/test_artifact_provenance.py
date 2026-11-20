"""Where a boot's artifacts come from: the build server's current file (by its sha256, computed there), whether the
host's copy was used, and which build job wrote it. The build server is this machine, reached through fake `ssh` and
`scp` on PATH that run the command locally."""

import hashlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.dut.artifacts import ArtifactStore

HELPER = Path(__file__).resolve().parents[1] / "core" / "sdk" / "helper.py"

SSH = """#!/bin/bash
# fake ssh: drop the options and the host, run the command here
while [ "$1" = "-o" ]; do shift 2; done
shift
exec bash -c "$*"
"""
SCP = """#!/bin/bash
# fake scp: `scp [-q] [-o x]... host:SRC DST` copies SRC to DST here
args=()
while [ $# -gt 0 ]; do case "$1" in -q) shift;; -o) shift 2;; *) args+=("$1"); shift;; esac; done
echo "${args[0]#*:}" >> "$(dirname "$0")/scp.log"
exec cp "${args[0]#*:}" "${args[1]}"
"""


@pytest.fixture
def server(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, text in (("ssh", SSH), ("scp", SCP)):
        (bin_ / name).write_text(text)
        (bin_ / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    sdk = tmp_path / "sdk"
    (sdk / "output").mkdir(parents=True)
    return SimpleNamespace(sdk=sdk, jobs=sdk / ".bsp" / "jobs", copies=lambda: _copies(bin_ / "scp.log"),
                           store=ArtifactStore(tmp_path / "cache"))


def _copies(log: Path) -> list[str]:
    return log.read_text().split() if log.exists() else []


def _job(server, jid: str, started: float, ended: float | None, rc: int | None = 0, component: str = "edk2"):
    d = server.jobs / jid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"component": component, "command": "make", "started": started}))
    (d / "script").write_text("#!/bin/bash\n")
    os.utime(d / "script", (started, started))
    (d / "log").write_text("build\n")
    os.utime(d / "log", (ended or time.time(),) * 2)
    if rc is not None:
        (d / "rc").write_text(f"{rc}\n")
        os.utime(d / "rc", (ended, ended))


def _artifact(server, name: str, data: bytes, mtime: float) -> Path:
    p = server.sdk / "output" / name
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))
    return p


def _pull(server, **files):
    lines: list[str] = []
    got = server.store.pull("builder@lab-1", {r: str(server.sdk / "output" / f) for r, f in files.items()},
                            lines.append, jobs=str(server.jobs))
    return got, lines


def test_a_rebuilt_file_is_copied_again_and_says_which_build_wrote_it(server):
    t = time.time() - 3600
    _job(server, "20261120-100000-aaaa", t, t + 60)
    _artifact(server, "edk2-opensbi.itb", b"old firmware", t + 30)
    first, lines = _pull(server, edk2="edk2-opensbi.itb")
    old = hashlib.sha256(b"old firmware").hexdigest()
    assert first["edk2"]["sha256"] == old and first["edk2"]["source"] == "build server"
    assert first["edk2"]["build"]["job"] == "20261120-100000-aaaa" and first["edk2"]["build"]["is_latest"]
    assert "fresh copy from the build server" in lines[0] and f"sha256 {old[:12]}" in lines[0]
    assert "job 20261120-100000-aaaa (edk2) (the latest successful build)" in lines[0]

    # the same file again: the host's copy, said as such, with the same build
    again, lines = _pull(server, edk2="edk2-opensbi.itb")
    assert again["edk2"]["source"] == "host cache" and "host cache hit" in lines[0]
    assert server.copies() == [str(server.sdk / "output" / "edk2-opensbi.itb")]

    # a new build rewrites it at the same path: the next boot takes the new content, never the cached one
    _job(server, "20261120-110000-bbbb", t + 600, t + 700)
    _artifact(server, "edk2-opensbi.itb", b"new firmware", t + 650)
    new, lines = _pull(server, edk2="edk2-opensbi.itb")
    assert new["edk2"]["sha256"] == hashlib.sha256(b"new firmware").hexdigest()
    assert new["edk2"]["source"] == "build server" and Path(new["edk2"]["path"]).read_bytes() == b"new firmware"
    assert new["edk2"]["build"]["job"] == new["edk2"]["build"]["latest"] == "20261120-110000-bbbb"
    assert len(server.copies()) == 2


def test_a_file_the_latest_build_did_not_rewrite_is_named_older(server):
    t = time.time() - 3600
    _artifact(server, "edk2-opensbi.itb", b"firmware", t)
    _job(server, "20261120-110000-bbbb", t + 600, t + 700, component="kernel")
    got, lines = _pull(server, edk2="edk2-opensbi.itb")
    b = got["edk2"]["build"]
    assert b["job"] is None and b["latest"] == "20261120-110000-bbbb" and b["older_than_latest"]
    assert lines[0].startswith("WARNING: artifact edk2: edk2-opensbi.itb")
    assert "OLDER than the latest successful build, job 20261120-110000-bbbb (kernel)" in lines[0]


def test_a_failed_build_is_not_the_latest_successful_one(server):
    t = time.time() - 3600
    _job(server, "20261120-100000-aaaa", t, t + 60)
    _artifact(server, "Image", b"kernel", t + 30)
    _job(server, "20261120-110000-bbbb", t + 600, t + 610, rc=2)
    got, lines = _pull(server, kernel="Image")
    assert got["kernel"]["build"]["latest"] == "20261120-100000-aaaa" and got["kernel"]["build"]["is_latest"]


def test_a_build_still_running_is_named_before_its_files_are_used(server):
    t = time.time() - 600
    _job(server, "20261120-100000-aaaa", t - 3000, t - 2900)
    _artifact(server, "Image", b"kernel", t - 2950)
    _job(server, "20261120-110000-cccc", t, None, rc=None, component="kernel")
    got, lines = _pull(server, kernel="Image")
    assert lines[0].startswith("WARNING: build 20261120-110000-cccc (kernel) is still running")
    assert got["kernel"]["build"]["is_latest"]


def test_a_cached_copy_of_the_wrong_size_is_replaced(server):
    t = time.time() - 60
    _artifact(server, "Image", b"kernel", t)
    sha = hashlib.sha256(b"kernel").hexdigest()
    bad = server.store.root / sha / "Image"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"trunc")
    got, lines = _pull(server, kernel="Image")
    assert got["kernel"]["source"] == "build server" and bad.read_bytes() == b"kernel"


def test_without_build_jobs_the_line_still_names_source_and_time(server):
    _artifact(server, "Image", b"kernel", time.time() - 60)
    lines: list[str] = []
    got = server.store.pull("builder@lab-1", {"kernel": str(server.sdk / "output" / "Image")}, lines.append)
    assert got["kernel"]["build"] is None and got["kernel"]["built"]
    assert "fresh copy from the build server" in lines[0] and "built " in lines[0]
    lines.clear()
    got = server.store.pull("builder@lab-1", {"kernel": str(server.sdk / "output" / "Image")}, lines.append,
                            jobs=str(server.jobs))
    assert "no successful build job on record" in lines[0]


# ------------------------------------------------------- build end --
def test_the_helper_reports_the_artifacts_and_the_job(server):
    t = time.time() - 3600
    _job(server, "20261120-100000-aaaa", t, t + 60)
    _artifact(server, "Image", b"kernel", t + 30)
    req = {"op": "artifacts", "path": str(server.sdk), "job": "20261120-100000-aaaa",
           "files": {"kernel": str(server.sdk / "output" / "Image"), "gone": str(server.sdk / "output" / "nope"),
                     "zip": str(server.sdk / "output" / "*.zip")}}
    out = subprocess.run([sys.executable, str(HELPER), json.dumps(req)], capture_output=True, text=True).stdout
    r = json.loads(out.strip().splitlines()[-1])["result"]
    assert set(r["artifacts"]) == {"kernel"}
    assert r["artifacts"]["kernel"]["sha256"] == hashlib.sha256(b"kernel").hexdigest()
    assert r["artifacts"]["kernel"]["mtime"] == int(t + 30) and r["artifacts"]["kernel"]["size"] == 6
    assert r["job"]["started"] == int(t) and r["job"]["ended"] == int(t + 60)


def test_a_build_ends_with_one_line_per_artifact(server, monkeypatch):
    from frontends.cli import sdkcli

    t = time.time() - 3600
    _job(server, "20261120-100000-aaaa", t, t + 60)
    _artifact(server, "Image", b"kernel", t + 30)
    _artifact(server, "FSBL.bin", b"fsbl", t - 9000)
    files = {"kernel": str(server.sdk / "output" / "Image"), "fsbl": str(server.sdk / "output" / "FSBL.bin")}
    inst = SimpleNamespace(path=str(server.sdk), artifacts=lambda: files, server=SimpleNamespace(ssh="builder@lab-1"))
    monkeypatch.setenv("BSP_SDK_LOCAL", "1")
    monkeypatch.setattr(sdkcli, "is_local", lambda s: True)
    out = io.StringIO()
    res = sdkcli.built_artifacts(inst, "20261120-100000-aaaa", out)
    lines = out.getvalue().splitlines()
    sha = hashlib.sha256(b"kernel").hexdigest()[:12]
    assert any(l.strip().startswith(f"artifact Image sha256 {sha} 6 B built ") and
               l.endswith("job 20261120-100000-aaaa") for l in lines), lines
    assert any("artifact FSBL.bin" in l and "(not rewritten by job 20261120-100000-aaaa)" in l for l in lines), lines
    assert res["kernel"]["job"] == "20261120-100000-aaaa" and res["fsbl"]["job"] is None


def test_a_build_server_that_cannot_list_artifacts_leaves_the_build_alone(monkeypatch):
    from core.errors import BspError
    from frontends.cli import sdkcli

    def boom(*a, **k):
        raise BspError("no python3")
    monkeypatch.setattr(sdkcli, "helper", boom)
    out = io.StringIO()
    assert sdkcli.built_artifacts(SimpleNamespace(artifacts=lambda: {}), "j", out) == {}
    assert "artifact checksums unavailable: no python3" in out.getvalue()


def test_a_boot_result_carries_each_artifacts_provenance(tmp_path):
    """The boot job's result has the same facts as its log lines, for a frontend to show."""
    from test_dut import SITE, FakeConsole, FakeControl, FakeFastboot
    from core.dut.boot import BootRunner
    from core.sdk.catalog import SdkCatalog

    sdk = SdkCatalog(SITE / "sdk").get("bsp-a")
    seen = {}

    class Store:
        def pull(self, ssh, files, log, jobs=None):
            seen["jobs"] = jobs
            build = {"job": "20261120-100000-aaaa", "latest": "20261120-100000-aaaa", "is_latest": True}
            return {r: {"path": p, "remote": p, "sha256": "a" * 64, "size": 1, "age_s": 5, "mtime": 100,
                        "built": "2026-11-20T10:00:30+0000", "source": "host cache", "build": build}
                    for r, p in files.items()}

    con = FakeConsole(tmp_path)
    res = BootRunner("b1", FakeControl(con), FakeFastboot(con), con, Store()).run(sdk, "load", None, lambda m: None)
    assert seen["jobs"] == f"{sdk.path.rstrip('/')}/.bsp/jobs"
    a = next(iter(res["artifacts"].values()))
    assert a["source"] == "host cache" and a["built"] and a["build"]["job"] == "20261120-100000-aaaa"
