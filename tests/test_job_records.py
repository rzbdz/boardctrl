"""A job is kept on disk while it runs and when it ends: after a restart, a job the old process was running is
reported as interrupted by the restart, by id, instead of being unknown. Fakes only."""

import json
import threading
import time

from core.events import EventBus
from core.jobs import INTERRUPTED, JobManager


def _until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_a_finished_job_is_still_known_after_a_restart(tmp_path):
    jobs = JobManager(EventBus(), record_dir=tmp_path)
    job = jobs.start("flash", "flash board-1", lambda log: (log("writing"), {"ok": True})[1], dut="board-1")
    assert _until(lambda: job.status == "succeeded" and (tmp_path / f"{job.id}.json").exists()
                  and json.loads((tmp_path / f"{job.id}.json").read_text())["status"] == "succeeded")

    again = JobManager(EventBus(), record_dir=tmp_path).get(job.id)
    assert again.status == "succeeded"
    assert again.meta == {"dut": "board-1"}
    assert again.result == {"ok": True}
    assert any("writing" in line for line in again.lines)


def test_a_job_running_when_the_service_died_reads_interrupted(tmp_path):
    release = threading.Event()
    jobs = JobManager(EventBus(), record_dir=tmp_path)

    def body(log):
        log("erasing partition boot")
        release.wait(10)

    job = jobs.start("flash", "flash board-1", body, dut="board-1")
    rec = tmp_path / f"{job.id}.json"
    assert _until(lambda: rec.exists() and "erasing" in rec.read_text())
    # The process "dies" here: a new manager reads what the old one left.
    after = JobManager(EventBus(), record_dir=tmp_path)
    release.set()
    j = after.get(job.id)
    assert j.status == "failed"
    assert j.error["code"] == "interrupted"
    assert j.error["message"] == INTERRUPTED
    assert "erasing partition boot" in j.error["details"]["last_line"]
    assert j.meta["dut"] == "board-1" and j.finished
    assert INTERRUPTED in j.lines[-1]
    view = j.view()
    assert view["status"] == "failed" and view["error"]["code"] == "interrupted"
    # The record now says so too, so a second restart keeps the same answer.
    assert json.loads((tmp_path / f"{job.id}.json").read_text())["error"]["code"] == "interrupted"
    assert JobManager(EventBus(), record_dir=tmp_path).get(job.id).error["code"] == "interrupted"


def test_records_are_bounded_and_a_broken_one_is_skipped(tmp_path):
    (tmp_path / "junk.json").write_text("{not json")
    jobs = JobManager(EventBus(), keep=3, record_dir=tmp_path)
    started = [jobs.start("probe", f"probe {i}", lambda log: None) for i in range(6)]
    assert _until(lambda: all(j.status == "succeeded" for j in started))
    kept = JobManager(EventBus(), keep=3, record_dir=tmp_path).list()
    assert len(kept) <= 3
    assert started[-1].id in {j.id for j in kept}


def test_without_a_record_dir_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jobs = JobManager(EventBus())
    job = jobs.start("probe", "probe", lambda log: log("x"))
    assert _until(lambda: job.status == "succeeded")
    assert list(tmp_path.iterdir()) == []
