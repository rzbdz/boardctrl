"""Cancelling a job stops what it runs now: a program with what it started, a wait at once, and the job ends
`cancelled` saying where. A cancelled job is never reported as a success. Fakes only."""

import os
import stat
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import proc
from core.events import EventBus
from core.jobs import JobManager, current_token
from frontends.server.app import create_app
from frontends.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "tests" / "fixtures" / "site" / "hosts" / "dev-mock.yaml"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie is gone for our purposes: it runs nothing
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def _until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _pidfile_program(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """An executable that starts a grandchild (`sleep 60 &`), writes its pid, and waits for it."""
    pids = tmp_path / f"{name}.pid"
    exe = tmp_path / "bin" / name
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {pids}\nwait\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe, pids


def test_a_cancelled_job_kills_its_program_and_the_grandchild(tmp_path):
    """`proc.run` in a job: a cancel stops the program and what it started within a couple of seconds."""
    exe, pids = _pidfile_program(tmp_path, "slow")
    jobs = JobManager(EventBus())
    job = jobs.start("boot", "b1: boot", lambda log: proc.run([str(exe)], timeout=120))
    assert _until(lambda: pids.exists() and pids.read_text().strip())
    grandchild = int(pids.read_text())
    assert _alive(grandchild)
    t0 = time.monotonic()
    jobs.cancel(job.id)
    jobs.wait(job.id, 10)
    assert job.status == "cancelled" and time.monotonic() - t0 < 5
    assert _until(lambda: not _alive(grandchild), 5), "the grandchild outlived the cancel"
    assert job.error["code"] == "cancelled"



def test_a_token_sleep_and_wait_end_at_once_and_outside_a_job_nothing_cancels(tmp_path):
    jobs = JobManager(EventBus())
    ev = threading.Event()
    job = jobs.start("x", "x", lambda log: current_token().wait(ev, 600))
    time.sleep(0.1)
    t0 = time.monotonic()
    job.cancel()
    jobs.wait(job.id, 5)
    assert job.status == "cancelled" and time.monotonic() - t0 < 1
    tok = current_token()                       # this thread runs no job
    assert not tok.cancel() and not tok.cancelled
    tok.check()
    assert tok.wait(lambda: True, 1)



def test_a_body_that_ignores_the_cancel_still_ends_cancelled_not_succeeded():
    jobs = JobManager(EventBus())
    gate = threading.Event()
    job = jobs.start("x", "x", lambda log: gate.wait(5) or "done")
    job.cancel()
    gate.set()
    jobs.wait(job.id, 5)
    assert job.status == "cancelled" and job.error["code"] == "cancelled"



def test_fastboot_and_a_hook_are_stopped_by_a_cancel(tmp_path, monkeypatch):
    """The fastboot CLI and a hook (the host's two long-running programs) honor the job's cancel."""
    from core.hooks import HookRunner
    from providers.fastboot.cli import Fastboot

    fb_exe, fb_pids = _pidfile_program(tmp_path, "fastboot")
    hooks = tmp_path / "hooks" / "flash"
    hooks.mkdir(parents=True)
    hook_pids = tmp_path / "hook.pid"
    (hooks / "stuck").write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {hook_pids}\nwait\n")
    (hooks / "stuck").chmod(0o755)
    monkeypatch.setenv("BSP_HOOKS_PATH", str(tmp_path / "hooks"))
    jobs = JobManager(EventBus())
    for start, pids in ((lambda log: Fastboot("usb:1-1", str(fb_exe)).cmd("flash", "rootfs", "x", timeout=300),
                         fb_pids),
                        (lambda log: HookRunner().run("flash", "stuck", {}, timeout=300, log=log), hook_pids)):
        job = jobs.start("flash", "b1: flash", start)
        assert _until(lambda: pids.exists() and pids.read_text().strip())
        grandchild = int(pids.read_text())
        t0 = time.monotonic()
        job.cancel()
        jobs.wait(job.id, 15)
        assert job.status == "cancelled" and time.monotonic() - t0 < 12
        assert _until(lambda: not _alive(grandchild), 5)



def test_a_serial_wait_returns_on_cancel_and_the_job_says_which_step(tmp_path):
    """A recipe waiting on the console for minutes stops at once, and its status names the step it was in."""
    from core.dut.boot import BootRunner
    from providers.smtty.serial import SerialConsole

    (tmp_path / "b1").mkdir()
    con = SerialConsole("b1", log_dir=str(tmp_path))
    con._log_path().write_text("booting\n")

    class Fb:
        usb, binary = "usb:1-1", "fastboot"

        def state(self):
            return None

    runner = BootRunner("b1", control=None, fastboot=Fb(), console=con, store=None)
    steps = [{"do": "settle", "seconds": 0}, {"do": "wait", "serial": "login:", "timeout": 600}]
    jobs = JobManager(EventBus())
    job = jobs.start("boot", "b1: boot", lambda log: runner.run_steps(steps, log))
    assert _until(lambda: any("1/2 settle" in line for line in job.lines))
    time.sleep(0.3)
    t0 = time.monotonic()
    job.cancel("enough")
    jobs.wait(job.id, 5)
    assert job.status == "cancelled" and time.monotonic() - t0 < 2
    assert job.error["details"]["step"] == "step 2/2 wait"
    assert "step 2/2 wait" in job.lines[-1] and "enough" in job.lines[-1]



def test_a_reset_waiting_for_a_person_ends_on_cancel_and_withdraws_the_request():
    """A person may never come: a cancel ends the wait at once and the board's page no longer asks."""
    from test_dut import _human

    ctl, att, _ = _human()
    jobs = JobManager(EventBus())
    job = jobs.start("reset", "slot-1: reset", lambda log: ctl.reset(log), dut="slot-1")
    assert _until(lambda: att.view("slot-1"))
    t0 = time.monotonic()
    job.cancel()
    jobs.wait(job.id, 5)
    assert job.status == "cancelled" and time.monotonic() - t0 < 2
    assert att.view("slot-1") is None


# ------------------------------------------------------------------ host --
@pytest.fixture
def host(tmp_path, monkeypatch):
    """The mock host; its fastboot is a fake that starts a grandchild and sleeps when asked to flash."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fb = bindir / "fastboot"
    fb.write_text("#!/bin/sh\n[ \"$1\" = devices ] && { printf 'dfu\\tDFU download\\n    usb:0-0\\n'; exit 0; }\n"
                  f"sleep 60 &\necho $! > {tmp_path}/fb.pid\nwait\n")
    fb.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    with TestClient(create_app(HostConfig.load(MOCK))) as c:
        c.tmp = tmp_path
        yield c


def _call(c, tool, **args):
    r = c.post(f"/api/v1/tools/{tool}", json=args).json()
    return r["result"] if r["ok"] else r["error"]


def test_a_probe_job_is_cancelled_by_id(host, monkeypatch):
    """Probe jobs have no board: `job.cancel` stops them by id, while they wait between rounds."""
    from core.dut import probe as probe_mod

    def slow_run(self, log):
        log("round 1")
        current_token().at("round 1")
        current_token().sleep(600)

    monkeypatch.setattr(probe_mod.Prober, "run", slow_run)
    r = _call(host, "dut_probe", pins=None, consoles=None, identify=False, sdks=None, dry_run=False)
    job = host.app.state.hub.jobs.get(r["job"])
    assert _until(lambda: job.lines)
    out = _call(host, "job_cancel", id=r["job"])
    assert out["cancelled"] is True
    v = _call(host, "job_get", id=r["job"], wait=5)
    assert v["status"] == "cancelled" and v["error"]["details"]["step"] == "round 1"
    again = _call(host, "job_cancel", id=r["job"])
    assert again["cancelled"] is False and "already ended" in again["message"]


def test_a_boards_job_needs_its_lease_to_be_cancelled_and_keeps_the_lease(host):
    """`job.cancel` of a board's job checks the board's lease; afterwards the board is free of the job and leased."""
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=600)["token"]
    hub = host.app.state.hub
    mod = hub.modules["dut"]
    job = mod._start_job("mock-board", "flash", lambda log: mod.board("mock-board").fastboot.cmd(
        "flash", "rootfs", "x", timeout=300), kind="flash")
    pids = host.tmp / "fb.pid"
    assert _until(lambda: pids.exists() and pids.read_text().strip())
    grandchild = int(pids.read_text())
    assert _call(host, "job_cancel", id=job.id)["code"] == "forbidden"
    assert _call(host, "job_cancel", id=job.id, lease=token)["cancelled"] is True
    hub.jobs.wait(job.id, 10)
    assert job.status == "cancelled"
    assert _until(lambda: not _alive(grandchild), 5)
    st = _call(host, "dut_status", dut="mock-board")
    assert st["lease"] and "mock-board" not in mod._busy
