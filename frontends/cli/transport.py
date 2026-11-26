"""Transports to a boardctrld: direct HTTP, or HTTP over SSH.

Networks often allow only SSH between subnets (build server -> board host:
port 22 open, 8765 filtered). SshTransport runs `curl` against the host's
loopback over one multiplexed SSH connection, so the CLI works unchanged.
Large files are copied with scp, never through an HTTP request body.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from core import naming, proc

# never route boardctrl traffic through an HTTP proxy from the environment
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Unreachable(Exception):
    def __init__(self, where: str, reason: Any):
        super().__init__(f"{where}: {reason}")
        self.where = where
        self.reason = reason


class HttpTransport:
    kind = "http"

    def __init__(self, url: str, token: str | None = None):
        self.url = url.rstrip("/")
        self.token = token

    @property
    def label(self) -> str:
        return self.url

    def reachable(self, timeout: float = 2.0) -> bool:
        sp = urlsplit(self.url)
        try:
            socket.create_connection((sp.hostname, sp.port or 80), timeout=timeout).close()
            return True
        except OSError:
            return False

    def request(self, method: str, path: str, body: Any = None, timeout: float = 90) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except ValueError:
                return {"ok": False, "error": {"code": f"http_{e.code}", "message": str(e.reason)}}
        except (urllib.error.URLError, OSError) as e:
            raise Unreachable(self.url, getattr(e, "reason", e)) from None

    def upload(self, path: str, file: str, timeout: float = 600, sha256: str | None = None,
               name: str | None = None) -> Any:
        """PUT a file as the request body, streamed from disk (kernel FITs are ~100 MB)."""
        size = os.path.getsize(file)
        with open(file, "rb") as f:
            req = urllib.request.Request(self.url + path, data=f, method="PUT")
            req.add_header("Accept", "application/json")
            req.add_header("Content-Type", "application/octet-stream")
            req.add_header("Content-Length", str(size))
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            try:
                with _OPENER.open(req, timeout=timeout) as r:
                    return json.loads(r.read() or b"null")
            except urllib.error.HTTPError as e:
                try:
                    return json.loads(e.read())
                except ValueError:
                    return {"ok": False, "error": {"code": f"http_{e.code}", "message": str(e.reason)}}
            except (urllib.error.URLError, OSError) as e:
                raise Unreachable(self.url, getattr(e, "reason", e)) from None

    def stream(self, path: str):
        req = urllib.request.Request(self.url + path, headers={"Accept": "text/event-stream"})
        try:
            with _OPENER.open(req, timeout=3600) as r:
                for raw in r:
                    yield raw.decode("utf-8", "replace").rstrip("\n")
        except (urllib.error.URLError, OSError) as e:
            raise Unreachable(self.url, getattr(e, "reason", e)) from None


def _ssh_options() -> list[str]:
    cdir = Path(os.environ.get("XDG_RUNTIME_DIR") or Path.home() / ".cache") / "bsp-ssh"
    cdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # BSP_SSH_OPTIONS: more ssh options, for a program driving boardctrl with ssh setup of its own (boardlab: its key).
    extra = shlex.split(naming.get("BOARDCTRL_SSH_OPTIONS", ""))
    return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ControlMaster=auto", "-o", f"ControlPath={cdir}/%r@%h:%p", "-o", "ControlPersist=300", *extra]


def _ssh_base(target: str) -> list[str]:
    return ["ssh", *_ssh_options(), target]


_REMOTE_SAFE = re.compile(r"/[A-Za-z0-9._/-]+")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_REMOTE_SAFE = re.compile(r"/[A-Za-z0-9._/-]+")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SshTransport:
    kind = "ssh"

    def __init__(self, target: str, port: int = 8765, token: str | None = None):
        self.target = target
        self.port = port
        self.token = token

    @property
    def label(self) -> str:
        return f"ssh://{self.target} -> 127.0.0.1:{self.port}"

    def reachable(self, timeout: float = 8.0) -> bool:
        try:
            r = subprocess.run(_ssh_base(self.target) + ["true"], capture_output=True, timeout=timeout + 4)
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def _curl(self, method: str, path: str, timeout: float) -> list[str]:
        cmd = ["curl", "-sS", "--noproxy", "*", "-m", str(int(timeout)), "-X", method,
               "-H", "Content-Type: application/json", "-H", "Accept: application/json"]
        if self.token:
            cmd += ["-H", f"Authorization: Bearer {self.token}"]
        cmd += ["--data-binary", "@-", f"http://127.0.0.1:{self.port}{path}"]
        return _ssh_base(self.target) + [" ".join(_q(c) for c in cmd)]

    def request(self, method: str, path: str, body: Any = None, timeout: float = 90) -> Any:
        data = json.dumps(body) if body is not None else ""
        if method == "GET":
            data = ""
        try:
            r = subprocess.run(self._curl(method, path, timeout), input=data, capture_output=True,
                               text=True, timeout=timeout + 15)
        except subprocess.TimeoutExpired:
            raise Unreachable(self.label, "timed out") from None
        if r.returncode == 255:
            raise Unreachable(self.label, (r.stderr.strip() or "ssh failed")[-200:])
        if r.returncode != 0:
            raise Unreachable(self.label, f"curl rc={r.returncode}: {r.stderr.strip()[-200:]}")
        try:
            return json.loads(r.stdout or "null")
        except ValueError:
            raise Unreachable(self.label, f"non-JSON reply: {r.stdout[:120]!r}") from None

    def upload(self, path: str, file: str, timeout: float = 600, sha256: str | None = None,
               name: str | None = None) -> Any:
        """Copy a file to the host over scp, never through an HTTP body.

        boardctrld gives a ticket path under its artifact cache; scp writes the bytes there; boardctrld then checks
        the sha256 and moves the file into the cache. Old boardctrld without tickets still gets the streamed
        PUT, so a mixed-version lab keeps working.
        """
        if not os.path.isfile(file):
            raise FileNotFoundError(file)
        size = os.path.getsize(file)
        sha = sha256 or _sha256(file)
        label = name or os.path.basename(file)
        query = f"sha256={quote(sha)}&name={quote(label)}"
        ticket = self.request("POST", f"/api/v1/artifacts/ticket?{query}", timeout=30)
        if not ticket.get("ok"):
            return self._upload_http(path, file, timeout)
        target = (ticket.get("result") or {}).get("path")
        if not isinstance(target, str) or not target.startswith("/") or not _REMOTE_SAFE.fullmatch(target):
            raise Unreachable(self.label, f"boardctrld gave an unusable upload path: {target!r}")
        t0 = os.times().elapsed
        try:
            r = _run(["scp", "-q", *_ssh_options(), file, f"{self.target}:{target}"], timeout=timeout + 15)
        except proc.TimedOut:
            raise Unreachable(self.label, "scp timed out") from None
        if r.returncode == 255:
            raise Unreachable(self.label, (r.stderr.strip() or "scp failed")[-200:])
        if r.returncode != 0:
            raise Unreachable(self.label, f"scp rc={r.returncode}: {r.stderr.strip()[-200:]}")
        adopted = self.request("POST", f"/api/v1/artifacts/adopt?ticket={quote((ticket['result'])['id'])}&{query}",
                               timeout=60)
        if not adopted.get("ok"):
            raise Unreachable(self.label, f"boardctrld did not adopt the file: {adopted}")
        if naming.get("BOARDCTRL_DEBUG"):
            print(f"scp {label} ({size:,} B) to {self.target} in {os.times().elapsed - t0:.1f}s", flush=True)
        return adopted

    def _upload_http(self, path: str, file: str, timeout: float) -> Any:
        """PUT a file over the ssh tunnel for a boardctrld that predates scp tickets."""
        cmd = ["curl", "-sS", "--noproxy", "*", "-m", str(int(timeout)), "-T", "-",
               "-H", "Content-Type: application/octet-stream", "-H", "Accept: application/json"]
        if self.token:
            cmd += ["-H", f"Authorization: Bearer {self.token}"]
        cmd.append(f"http://127.0.0.1:{self.port}{path}")
        try:
            with open(file, "rb") as f:
                r = _run(_ssh_base(self.target) + [" ".join(_q(c) for c in cmd)], stdin=f, text=False,
                         timeout=timeout + 15)
        except proc.TimedOut:
            raise Unreachable(self.label, "timed out") from None
        out, err = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
        if r.returncode == 255:
            raise Unreachable(self.label, (err.strip() or "ssh failed")[-200:])
        if r.returncode != 0:
            raise Unreachable(self.label, f"curl rc={r.returncode}: {err.strip()[-200:]}")
        try:
            return json.loads(out or "null")
        except ValueError:
            raise Unreachable(self.label, f"non-JSON reply: {out[:120]!r}") from None

    def stream(self, path: str):
        cmd = _ssh_base(self.target) + [f"curl -sS -N --noproxy '*' http://127.0.0.1:{self.port}{path}"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        try:
            for line in p.stdout:
                yield line.rstrip("\n")
        finally:
            p.kill()


def _run(argv: list[str], *, timeout: float, stdin: Any = None, text: bool = True) -> proc.Result:
    """A long transfer (scp, a streamed upload) in its own process group: an interrupt or a timeout stops it and
    the ssh it started, instead of leaving a copy running after the CLI is gone."""
    return proc.run(argv, timeout=timeout, stdin=stdin, text=text)


def _q(s: str) -> str:
    import shlex

    return shlex.quote(s)
