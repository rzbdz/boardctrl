"""Reinstalling a host never cuts off a job: right before the service is stopped, the installer asks it which jobs
run, waits for them (naming them), and refuses when they outlast the wait, unless `--force`. The service is a fake
HTTP server on a loopback port; nothing here touches systemd or a real host."""

import json
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "deploy" / "bspd-install.sh").read_text()
FRAGMENT = SCRIPT[SCRIPT.index("# --- running jobs"):SCRIPT.index("# --- end running jobs")]


class FakeService:
    """GET /api/v1/job/list answers with `jobs` (a list, or a callable returning one), or with `status`."""

    def __init__(self, jobs=(), status=200):
        self.jobs, self.status, self.asked = jobs, status, 0
        svc = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                svc.asked += 1
                if self.path != "/api/v1/job/list" or svc.status != 200:
                    self.send_response(404 if self.path != "/api/v1/job/list" else svc.status)
                    self.end_headers()
                    return
                jobs = svc.jobs() if callable(svc.jobs) else svc.jobs
                body = json.dumps({"ok": True, "command": "job.list", "result": list(jobs)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _job(id="flash-1-1", status="running", **kw):
    return {"id": id, "kind": "flash", "title": "board-1: flash", "status": status, "created": time.time() - 30,
            "progress": 40, "meta": {"dut": "board-1"}, **kw}


def _run(port, tmp_path, force=False, wait=None, timeout=30):
    """Run the installer's job check against `port`, as the install would right before a restart."""
    site = tmp_path / "site"
    (site / "hosts").mkdir(parents=True, exist_ok=True)
    (site / "hosts" / "lab-1.yaml").write_text(f"host: {{id: lab-1}}\nserver:\n  bind: 0.0.0.0\n  port: {port}\n")
    env = {"PATH": "/usr/bin:/bin", "SITE": str(site), "ID": "lab-1", "PORT": "1", "FORCE": "1" if force else "",
           **({"BOARDCTRL_UPGRADE_WAIT": str(wait)} if wait is not None else {})}
    body = FRAGMENT + '\nwait_for_jobs "$RUNNING_PORT" "restarting boardctrld"\necho PROCEEDS\n'
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + body], env=env, capture_output=True, text=True,
                          timeout=timeout)


def test_no_running_job_proceeds_at_once(tmp_path):
    svc = FakeService([_job(status="succeeded")])
    try:
        r = _run(svc.port, tmp_path)
    finally:
        svc.close()
    assert r.returncode == 0 and "PROCEEDS" in r.stdout, r
    assert "waiting" not in r.stdout


def test_nothing_serving_the_port_proceeds(tmp_path):
    """A first install: no service runs yet, so there is nothing to wait for."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    r = _run(port, tmp_path)
    assert r.returncode == 0 and "PROCEEDS" in r.stdout, r


def test_a_running_job_is_waited_for_and_named(tmp_path):
    done_at = time.time() + 3
    svc = FakeService(lambda: [_job(status="running" if time.time() < done_at else "succeeded")])
    try:
        r = _run(svc.port, tmp_path, wait=60)
    finally:
        svc.close()
    assert r.returncode == 0 and "PROCEEDS" in r.stdout, r
    assert "waiting for running jobs before restarting boardctrld" in r.stdout
    assert "job flash-1-1 (flash, board board-1): board-1: flash" in r.stdout and "40%" in r.stdout


def test_a_job_that_outlasts_the_wait_refuses_and_says_what_to_do(tmp_path):
    svc = FakeService([_job()])
    try:
        r = _run(svc.port, tmp_path, wait=2)
    finally:
        svc.close()
    assert r.returncode == 1 and "PROCEEDS" not in r.stdout, r
    assert "not restarting boardctrld" in r.stderr
    assert "job flash-1-1 (flash, board board-1)" in r.stderr
    assert "boardctrl job cancel <id>" in r.stderr and "--force" in r.stderr


def test_force_goes_ahead_and_says_what_it_cuts_off(tmp_path):
    svc = FakeService([_job()])
    try:
        r = _run(svc.port, tmp_path, force=True, wait=600, timeout=10)
    finally:
        svc.close()
    assert r.returncode == 0 and "PROCEEDS" in r.stdout, r
    assert "--force" in r.stdout and "interrupted by a service restart" in r.stdout
    assert "job flash-1-1" in r.stdout


def test_a_service_that_cannot_say_refuses_unless_forced(tmp_path):
    svc = FakeService(status=500)
    try:
        r = _run(svc.port, tmp_path)
        forced = _run(svc.port, tmp_path, force=True)
    finally:
        svc.close()
    assert r.returncode == 1 and "could not tell" in r.stderr and "--force" in r.stderr, r
    assert forced.returncode == 0 and "PROCEEDS" in forced.stdout, forced


def test_the_check_runs_right_before_each_stop():
    """Before the restart (after the upload and the venv build, so a job started meanwhile counts), before an
    uninstall, and before the takeover stops the old unit."""
    restart = SCRIPT.index("systemctl restart boardctrld.service")
    check = SCRIPT.rindex('wait_for_jobs "$RUNNING_PORT" "restarting boardctrld"', 0, restart)
    assert "pip_install" not in SCRIPT[check:restart] and "venv" not in SCRIPT[check:restart]
    assert SCRIPT.index('wait_for_jobs "$RUNNING_PORT" "removing boardctrld"') < \
        SCRIPT.index('systemctl disable --now "$u"')
    assert SCRIPT.index('wait_for_jobs "$RUNNING_PORT" "stopping bsp-hostd"') < \
        SCRIPT.index("systemctl disable --now bsp-hostd.service")
    assert "--force) FORCE=1" in SCRIPT


def test_the_check_reads_the_running_service_the_way_it_answers(tmp_path, monkeypatch):
    """The real service answers GET /api/v1/job/list without a token (read-only), in the shape the check parses."""
    from fastapi.testclient import TestClient

    from frontends.server.app import create_app
    from frontends.server.config import HostConfig

    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BSP_TOKEN", "made-up-token")
    app = create_app(HostConfig.load(ROOT / "tests" / "fixtures" / "site" / "hosts" / "dev-mock.yaml"))
    with TestClient(app) as c:
        r = c.get("/api/v1/job/list")
    assert r.status_code == 200 and isinstance(r.json()["result"], list)
