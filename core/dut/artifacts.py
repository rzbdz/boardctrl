"""The artifact cache: content-addressed build products a host takes from a caller, a URL or a path on it."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
import time
from pathlib import Path
from typing import Any, Callable

from core import proc
from core.jobs import current_token
from core.errors import Conflict, HardwareError, HardwareTimeout, InvalidRequest

Log = Callable[[str], None]
SHA256 = re.compile(r"[0-9a-f]{64}")


def _run(argv: list[str], timeout: float) -> proc.Result:
    """Run a local command for the cache; its failures are the host's, said as such. A cancelled job stops it."""
    try:
        return proc.run(argv, timeout=timeout)
    except proc.TimedOut:
        raise HardwareTimeout(f"`{' '.join(argv[:4])} ...` timed out after {timeout:g}s") from None
    except FileNotFoundError:
        raise HardwareError(f"{argv[0]} not found on this host", hint="install it or set its path in the host config") from None


# ------------------------------------------------------------ artifacts --
class ArtifactStore:
    """Content-addressed cache of build outputs pulled from build servers over SSH.

    The cache is local: bsp does not mount CIFS or translate Windows/UNC paths. Explicit board-host paths and
    host-fetched URLs are additional boot inputs; local-file upload is only the fallback."""

    SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]

    def __init__(self, root: Path, policy: Any = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # How much it keeps (core.dut.cachepolicy): applied after each file that arrives. None keeps everything.
        self.policy = policy

    def tidy(self, log: Log | None = None) -> list[str]:
        """Apply the cache's size and age policy, when it has one."""
        return self.policy.tidy(log) if self.policy is not None else []

    def hold(self, sha256: str) -> None:
        """Keep an entry while the calling job runs, whatever the policy says."""
        from core.dut import cachepolicy

        cachepolicy.hold(self.root, sha256)

    def pull(self, ssh: str, files: dict[str, str], log: Log, jobs: str | None = None) -> dict[str, dict[str, Any]]:
        """files: role -> absolute remote path. Returns role -> {path, remote, sha256, size, mtime, age_s, source,
        build}.

        The build server's file is the truth: its sha256 is computed there (`sha256sum` over ssh) on every pull, and
        the host's copy is used only when it has that content (same sha256 and size); otherwise the file is copied
        again. `jobs` is the SDK's build-job directory on the server (`<sdk>/.bsp/jobs`): with it, each artifact says
        whether the latest successful build wrote it, and a build still running is named."""
        globs = {r: p for r, p in files.items() if any(c in p for c in "*?[")}
        if globs:
            script = "; ".join(f"echo {shlex.quote(r)} $(ls -t {p} 2>/dev/null | head -1)" for r, p in globs.items())
            rg = _run(["ssh", *self.SSH_OPTS, ssh, script], 60)
            for line in rg.stdout.splitlines():
                role, _, found = line.partition(" ")
                if found.strip():
                    files = {**files, role: found.strip()}
        paths = list(files.values())
        script = (_jobs_script(jobs) + "; " if jobs else "") + \
            "for f in " + " ".join(shlex.quote(p) for p in paths) + \
            '; do if [ -f "$f" ]; then echo "$(sha256sum "$f" | cut -d" " -f1) $(stat -L -c "%s %Y" "$f") $f"; ' \
            'else echo "MISSING - - $f"; fi; done; echo "NOW $(date +%s)"'
        r = _run(["ssh", *self.SSH_OPTS, ssh, script], 120)
        if r.returncode != 0:
            raise HardwareError(f"cannot reach build server {ssh}: {r.stderr.strip()[-300:]}",
                                hint="this host needs an SSH key accepted by the build server")
        meta: dict[str, tuple[str, str, str]] = {}
        builds: list[dict[str, Any]] = []          # the SDK's build jobs, newest first
        now = None
        for line in r.stdout.splitlines():
            word = line.split(" ", 1)[0]
            if word == "JOB":
                job = _job_line(line)
                if job is not None:
                    builds.append(job)
                continue
            if word == "NOW":
                now = _int(line.split(" ", 1)[1])
                continue
            parts = line.split(" ", 3)
            if len(parts) == 4:
                meta[parts[3]] = (parts[0], parts[1], parts[2])
        now = now or int(time.time())
        for j in builds:
            if j["status"] == "running" and j["log_mtime"] is not None and now - j["log_mtime"] < RUNNING_FRESH_S:
                log(f"WARNING: build {j['id']} ({j['component'] or '?'}) is still running on {ssh}: the files below "
                    f"may be replaced by it; boot again once it has ended")
        out: dict[str, dict[str, Any]] = {}
        for role, path in files.items():
            current_token().check()                 # a cancelled boot stops before the next artifact
            sha, size, mtime = meta.get(path, ("MISSING", "-", "-"))
            if sha == "MISSING":
                raise InvalidRequest(f"artifact '{role}' missing on {ssh}: {path}",
                                     hint="build it first: `bsp sdk build <sdk> <component>`")
            if not SHA256.fullmatch(sha) or not size.isdigit() or not mtime.isdigit():
                raise HardwareError(f"build server {ssh} answered {sha!r} {size!r} {mtime!r} for {path}",
                                    hint="it needs sha256sum and GNU stat")
            size_i, mtime_i = int(size), int(mtime)
            local = self.root / sha / Path(path).name
            if local.is_file() and local.stat().st_size == size_i:
                source, took = "host cache", None
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
                tmp = local.with_suffix(local.suffix + ".part")
                t0 = time.monotonic()
                try:
                    rr = _run(["scp", "-q", *self.SSH_OPTS, f"{ssh}:{path}", str(tmp)], 600)
                except BaseException:
                    tmp.unlink(missing_ok=True)     # a cancelled or timed-out copy leaves no part behind
                    raise
                if rr.returncode != 0:
                    tmp.unlink(missing_ok=True)
                    raise HardwareError(f"scp {path} failed: {rr.stderr.strip()[-300:]}")
                got = _sha256(tmp)
                if got != sha:
                    tmp.unlink(missing_ok=True)
                    raise HardwareError(f"checksum mismatch pulling {path} (changed during copy?)",
                                        hint="retry once the build has finished writing it")
                os.replace(tmp, local)
                source, took = "build server", time.monotonic() - t0
            self.hold(sha)                          # the job that pulled it uses it: kept while it runs
            _touch(local.parent)
            build = build_of(mtime_i, builds, now) if jobs else None
            log(provenance_line(role, Path(path).name, sha, size_i, mtime_i, source, took, build))
            out[role] = {"path": str(local), "remote": path, "sha256": sha, "size": size_i,
                         "mtime": mtime_i, "built": when(mtime_i), "age_s": now - mtime_i, "source": source,
                         "build": build}
        self.tidy(log)
        return out

    # ------------------------------------------------- uploaded files --
    def find(self, sha256: str) -> Path | None:
        """The cached file with this content, whichever way it arrived (pulled or uploaded)."""
        if not SHA256.fullmatch(sha256 or ""):
            return None
        d = self.root / sha256
        if not d.is_dir():
            return None
        for p in sorted(d.iterdir()):
            if p.is_file() and not p.name.endswith(".part"):
                _touch(d)                           # used: the cache's policy keeps it longer
                return p
        return None

    def get(self, sha256: str) -> Path:
        p = self.find(sha256)
        if p is None:
            raise InvalidRequest(f"no file with sha256 {sha256[:16]}... on this host",
                                 hint="upload it first: `bsp dut stage DUT FILE` does that for you "
                                      "(PUT /api/v1/artifacts/<sha256>)")
        return p

    def upload(self, sha256: str, name: str) -> "Upload":
        """Start storing an uploaded file under its sha256: `write()` the body, then `commit()`."""
        if not SHA256.fullmatch(sha256 or ""):
            raise InvalidRequest(f"not a sha256: {sha256!r}", hint="64 lowercase hex digits")
        return Upload(self, sha256, _clean_name(name))

    def adopt_path(self, path: str) -> dict[str, Any]:
        """Copy a file already on this host into the cache (a Samba mount, a path boardctrld can read)."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            raise InvalidRequest(f"host file path must be absolute: {path}")
        if not p.is_file():
            raise InvalidRequest(f"no such file on this host: {path}")
        if not p.stat().st_size:
            raise InvalidRequest(f"refusing an empty file: {path}")
        sha = _sha256(p)
        if self.find(sha) is None:
            with open(p, "rb") as f:
                self.put(sha, p.name, iter(lambda: f.read(1 << 20), b""))
        final = self.get(sha)
        return {"sha256": sha, "path": str(final), "size": final.stat().st_size, "name": final.name}

    def adopt_path(self, path: str) -> dict[str, Any]:
        """Copy a file already on this host into the cache (a Samba mount, a path boardctrld can read)."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            raise InvalidRequest(f"host file path must be absolute: {path}")
        if not p.is_file():
            raise InvalidRequest(f"no such file on this host: {path}")
        if not p.stat().st_size:
            raise InvalidRequest(f"refusing an empty file: {path}")
        sha = _sha256(p)
        if self.find(sha) is None:
            with open(p, "rb") as f:
                self.put(sha, p.name, iter(lambda: f.read(1 << 20), b""))
        final = self.get(sha)
        return {"sha256": sha, "path": str(final), "size": final.stat().st_size, "name": final.name}

    def adopt(self, tmp: Path, sha256: str, name: str) -> Path:
        """Move a file whose content was checked to hash to `sha256` into the cache (same filesystem)."""
        have = self.find(sha256)
        if have is not None:
            tmp.unlink(missing_ok=True)
            return have
        if not SHA256.fullmatch(sha256 or ""):
            raise InvalidRequest(f"not a sha256: {sha256!r}", hint="64 lowercase hex digits")
        final = self.root / sha256 / _clean_name(name)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)
        self.hold(sha256)
        self.tidy()
        return final

    def put(self, sha256: str, name: str, chunks) -> dict[str, Any]:
        up = self.upload(sha256, name)
        try:
            for chunk in chunks:
                up.write(chunk)
            return up.commit()
        finally:
            up.abort()

    # ------------------------------------------------------ scp tickets --
    def ticket(self, sha256: str, name: str) -> dict[str, str]:
        """A place on this host for a file arriving over scp, and the id that adopts it.

        The caller copies the file there with scp (never through boardctrld's HTTP body), then `adopt_ticket`
        checks the content and moves it into the cache. The directory is under the cache, so the move is
        atomic and no large file is ever read into memory.
        """
        if not SHA256.fullmatch(sha256 or ""):
            raise InvalidRequest(f"not a sha256: {sha256!r}", hint="64 lowercase hex digits")
        import uuid

        incoming = self.root / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        ticket = uuid.uuid4().hex
        d = incoming / ticket
        d.mkdir(mode=0o700)
        path = d / _clean_name(name)
        return {"id": ticket, "path": str(path)}

    def adopt_ticket(self, ticket: str, sha256: str, name: str) -> dict[str, Any]:
        """Take a file copied to a `ticket` path into the cache, after checking it is that content."""
        if not re.fullmatch(r"[0-9a-f]{32}", ticket or ""):
            raise InvalidRequest(f"not an upload ticket: {ticket!r}")
        path = self.root / ".incoming" / ticket / _clean_name(name)
        try:
            st = path.lstat()
        except FileNotFoundError:
            raise InvalidRequest("the file did not arrive on this host",
                                 hint="copy it to the ticket's path with scp, then adopt it again") from None
        if not stat.S_ISREG(st.st_mode):
            raise InvalidRequest("the ticket path is not a regular file")
        if not st.st_size:
            raise InvalidRequest("refusing an empty file")
        got = _sha256(path)
        if got != sha256:
            raise InvalidRequest(f"the file copied to this host does not match its sha256 (got {got[:16]}...)")
        had = self.find(sha256) is not None
        final = self.adopt(path, sha256, name)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return {"sha256": sha256, "path": str(final), "size": final.stat().st_size, "stored": not had}


def _touch(entry: Path) -> None:
    """Mark a cache entry used now (see core.dut.cachepolicy)."""
    try:
        os.utime(entry)
    except OSError:
        pass


def _clean_name(name: str) -> str:
    """A cached file's name: the base name, with only safe characters, never hidden."""
    name = re.sub(r"[^A-Za-z0-9._+-]", "_", Path(name or "file").name)[:120] or "file"
    return "_" + name if name.startswith(".") else name


class Upload:
    """One file being uploaded into an ArtifactStore. Its content is hashed as it arrives; a body
    that does not match the announced sha256 is discarded. A file already cached is not rewritten."""

    def __init__(self, store: ArtifactStore, sha256: str, name: str):
        self.store = store
        self.sha256, self.name, self.size = sha256, name, 0
        self.have = store.find(sha256)
        self._h = hashlib.sha256()
        self._f = None
        self.tmp: Path | None = None
        if self.have is None:
            d = store.root / sha256
            d.mkdir(parents=True, exist_ok=True)
            self.final = d / name
            self.tmp = d / f"{name}.{os.getpid()}.{time.monotonic_ns()}.part"
            self._f = open(self.tmp, "wb")

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self._f is not None:
            self._h.update(chunk)
            self._f.write(chunk)

    def commit(self) -> dict[str, Any]:
        if self.have is not None:
            return {"sha256": self.sha256, "path": str(self.have), "size": self.have.stat().st_size, "stored": False}
        self._f.close()
        got = self._h.hexdigest()
        if got != self.sha256:
            raise InvalidRequest(f"upload does not match its sha256 (got {got[:16]}..., {self.size:,} B)",
                                 hint="the file changed while it was sent, or the transfer was cut; send it again")
        if not self.size:
            raise InvalidRequest("refusing an empty file")
        os.replace(self.tmp, self.final)
        self.tmp = None
        self.store.tidy()
        return {"sha256": self.sha256, "path": str(self.final), "size": self.size, "stored": True}

    def abort(self) -> None:
        if self._f is not None and not self._f.closed:
            self._f.close()
        if self.tmp is not None:
            self.tmp.unlink(missing_ok=True)
            self.tmp = None


# ------------------------------------------------------- provenance --
# A build job left without an `rc` whose log has not moved for this long is not running any more (a reboot, a kill).
RUNNING_FRESH_S = 1800
SLACK_S = 2                                         # mtimes are whole seconds; a job's own files bracket it


def _jobs_script(jobs: str) -> str:
    """Shell that prints the newest build jobs in `jobs` (an SDK's `.bsp/jobs`), newest first, one per line:
    `JOB <id> <rc|-> <started> <ended|-> <log mtime|-> <component>`. Started is the job script's mtime, ended its
    `rc` file's: what the build server's own clock said, like the artifacts' mtimes."""
    d = shlex.quote(jobs)
    return (f'd={d}; if [ -d "$d" ]; then for j in $(ls -1 "$d" 2>/dev/null | sort -r | head -20); do '
            'rc=$(tr -dc 0-9- 2>/dev/null < "$d/$j/rc"); '
            'echo "JOB $j ${rc:--} $(stat -c %Y "$d/$j/script" 2>/dev/null || echo -) '
            '$(stat -c %Y "$d/$j/rc" 2>/dev/null || echo -) $(stat -c %Y "$d/$j/log" 2>/dev/null || echo -) '
            '$(sed -n \'s/.*"component": *"\\([^"]*\\)".*/\\1/p\' "$d/$j/meta.json" 2>/dev/null)"; '
            'done; fi')


def _int(v: str) -> int | None:
    v = (v or "").strip()
    return int(v) if v.lstrip("-").isdigit() else None


def _job_line(line: str) -> dict[str, Any] | None:
    parts = line.split(" ", 6)
    if len(parts) < 6:
        return None
    rc = _int(parts[2])
    started = _int(parts[3])
    if started is None:
        return None
    return {"id": parts[1], "status": "running" if parts[2] == "-" else ("succeeded" if rc == 0 else "failed"),
            "started": started, "ended": _int(parts[4]), "log_mtime": _int(parts[5]),
            "component": (parts[6].strip() if len(parts) > 6 else "") or None}


def build_of(mtime: int, builds: list[dict[str, Any]], now: int) -> dict[str, Any]:
    """Which build job wrote a file with this mtime (the newest whose run brackets it), and how that relates to the
    SDK's latest successful build."""
    latest = next((j for j in builds if j["status"] == "succeeded"), None)
    by = next((j for j in builds
               if j["started"] - SLACK_S <= mtime <= (j["ended"] if j["ended"] is not None else now) + SLACK_S), None)
    return {"job": by["id"] if by else None, "job_status": by["status"] if by else None,
            "job_component": by["component"] if by else None,
            "latest": latest["id"] if latest else None, "latest_component": latest["component"] if latest else None,
            "latest_ended": when(latest["ended"]) if latest and latest["ended"] is not None else None,
            "is_latest": bool(by and latest and by["id"] == latest["id"]),
            "older_than_latest": bool(latest and mtime < latest["started"] - SLACK_S)}


def when(t: int | None) -> str | None:
    """A build-server mtime as local ISO time with its offset."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)) if t is not None else None


def _job(jid: str, component: str | None, status: str | None = None) -> str:
    what = ", ".join(x for x in (component, None if status in (None, "succeeded") else status) if x)
    return f"job {jid}" + (f" ({what})" if what else "")


def provenance_line(role: str, name: str, sha: str, size: int, mtime: int, source: str, took: float | None,
                     build: dict[str, Any] | None) -> str:
    """One line per artifact a boot uses: what it is, where it came from, when the build server wrote it, and which
    build that was."""
    where = f"fresh copy from the build server ({took:.1f}s)" if source == "build server" else "host cache hit"
    line = f"artifact {role}: {name} sha256 {sha[:12]} {size:,} B, {where}, built {when(mtime)} on the build server"
    if build is None:
        return line
    latest = _job(build["latest"], build["latest_component"]) if build["latest"] else None
    if build["is_latest"]:
        return f"{line}, {_job(build['job'], build['job_component'])} (the latest successful build)"
    if build["job"]:
        line += f", {_job(build['job'], build['job_component'], build['job_status'])}"
        return line + (f"; the latest successful build is {latest}" if latest else "")
    if build["older_than_latest"]:
        return (f"WARNING: {line}; OLDER than the latest successful build, {latest}, ended {build['latest_ended']}: "
                f"that build did not rewrite this file")
    if latest:
        return f"{line}; not written by a recorded build job (latest successful build: {latest})"
    return f"{line}; no successful build job on record"



def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure(cond: bool, msg: str, **kw: Any) -> None:
    if not cond:
        raise Conflict(msg, **kw)

