"""The artifact cache: content-addressed build products a host takes from a caller, a URL or a path on it."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from core.errors import Conflict, HardwareError, HardwareTimeout, InvalidRequest

Log = Callable[[str], None]
SHA256 = re.compile(r"[0-9a-f]{64}")


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run a local command for the cache; its failures are the host's, said as such."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HardwareTimeout(f"`{' '.join(argv[:4])} ...` timed out after {timeout:g}s") from None
    except FileNotFoundError:
        raise HardwareError(f"{argv[0]} not found on this host", hint="install it or set its path in the host config") from None


# ------------------------------------------------------------ artifacts --
class ArtifactStore:
    """Content-addressed cache of build outputs pulled from build servers over SSH.

    The cache is local: bsp does not mount CIFS or translate Windows/UNC paths. Explicit board-host paths and
    host-fetched URLs are additional boot inputs; local-file upload is only the fallback."""

    SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def pull(self, ssh: str, files: dict[str, str], log: Log) -> dict[str, dict[str, Any]]:
        """files: role -> absolute remote path. Returns role -> {path, sha256, size, mtime}."""
        globs = {r: p for r, p in files.items() if any(c in p for c in "*?[")}
        if globs:
            script = "; ".join(f"echo {shlex.quote(r)} $(ls -t {p} 2>/dev/null | head -1)" for r, p in globs.items())
            rg = _run(["ssh", *self.SSH_OPTS, ssh, script], 60)
            for line in rg.stdout.splitlines():
                role, _, found = line.partition(" ")
                if found.strip():
                    files = {**files, role: found.strip()}
        paths = list(files.values())
        script = "for f in " + " ".join(shlex.quote(p) for p in paths) + \
                 '; do if [ -f "$f" ]; then echo "$(sha256sum "$f" | cut -d" " -f1) $(stat -c "%s %Y" "$f") $f"; else echo "MISSING - - $f"; fi; done'
        r = _run(["ssh", *self.SSH_OPTS, ssh, script], 120)
        if r.returncode != 0:
            raise HardwareError(f"cannot reach build server {ssh}: {r.stderr.strip()[-300:]}",
                                hint="this host needs an SSH key accepted by the build server")
        meta = {}
        for line in r.stdout.splitlines():
            sha, size, mtime, path = line.split(" ", 3)
            meta[path] = (sha, size, mtime)
        out: dict[str, dict[str, Any]] = {}
        for role, path in files.items():
            sha, size, mtime = meta.get(path, ("MISSING", "-", "-"))
            if sha == "MISSING":
                raise InvalidRequest(f"artifact '{role}' missing on {ssh}: {path}",
                                     hint="build it first: `bsp sdk build <sdk> <component>`")
            local = self.root / sha / Path(path).name
            if local.exists():
                log(f"artifact {role}: {Path(path).name} cached ({int(size):,} B, sha {sha[:12]})")
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
                tmp = local.with_suffix(local.suffix + ".part")
                t0 = time.monotonic()
                rr = _run(["scp", "-q", *self.SSH_OPTS, f"{ssh}:{path}", str(tmp)], 600)
                if rr.returncode != 0:
                    raise HardwareError(f"scp {path} failed: {rr.stderr.strip()[-300:]}")
                got = _sha256(tmp)
                if got != sha:
                    tmp.unlink(missing_ok=True)
                    raise HardwareError(f"checksum mismatch pulling {path} (changed during copy?)",
                                        hint="retry once the build has finished writing it")
                os.replace(tmp, local)
                log(f"artifact {role}: pulled {Path(path).name} ({int(size):,} B in {time.monotonic() - t0:.1f}s, sha {sha[:12]})")
            out[role] = {"path": str(local), "remote": path, "sha256": sha, "size": int(size),
                         "mtime": int(mtime), "age_s": int(time.time()) - int(mtime)}
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


def _clean_name(name: str) -> str:
    """A cached file's name: the base name, with only safe characters, never hidden."""
    name = re.sub(r"[^A-Za-z0-9._+-]", "_", Path(name or "file").name)[:120] or "file"
    return "_" + name if name.startswith(".") else name


class Upload:
    """One file being uploaded into an ArtifactStore. Its content is hashed as it arrives; a body
    that does not match the announced sha256 is discarded. A file already cached is not rewritten."""

    def __init__(self, store: ArtifactStore, sha256: str, name: str):
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
        return {"sha256": self.sha256, "path": str(self.final), "size": self.size, "stored": True}

    def abort(self) -> None:
        if self._f is not None and not self._f.closed:
            self._f.close()
        if self.tmp is not None:
            self.tmp.unlink(missing_ok=True)
            self.tmp = None



def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure(cond: bool, msg: str, **kw: Any) -> None:
    if not cond:
        raise Conflict(msg, **kw)

