"""Cancelling a job stops what it runs now: a program with what it started, a wait at once, and the job ends
`cancelled` saying where. A cancelled job is never reported as a success. Fakes only."""

import os
import stat
import threading
import time
from pathlib import Path

from core import proc
from core.events import EventBus
from core.jobs import JobManager, current_token


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



def test_a_hook_is_stopped_by_a_cancel(tmp_path, monkeypatch):
    """A hook (a flasher, an identify script) honors the job's cancel, with what it started."""
    from core.hooks import HookRunner

    hooks = tmp_path / "hooks" / "flash"
    hooks.mkdir(parents=True)
    pids = tmp_path / "hook.pid"
    (hooks / "stuck").write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {pids}\nwait\n")
    (hooks / "stuck").chmod(0o755)
    monkeypatch.setenv("BSP_HOOKS_PATH", str(tmp_path / "hooks"))
    jobs = JobManager(EventBus())
    job = jobs.start("flash", "b1: flash", lambda log: HookRunner().run("flash", "stuck", {}, timeout=300, log=log))
    assert _until(lambda: pids.exists() and pids.read_text().strip())
    grandchild = int(pids.read_text())
    t0 = time.monotonic()
    job.cancel()
    jobs.wait(job.id, 15)
    assert job.status == "cancelled" and time.monotonic() - t0 < 12
    assert _until(lambda: not _alive(grandchild), 5)
