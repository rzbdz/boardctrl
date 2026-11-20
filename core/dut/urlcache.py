"""The URL cache: which artifact an http(s) package URL gave last, so the same link is not downloaded every time.

An entry maps the exact URL to the sha256 of the file it gave (in the ArtifactStore), when it was fetched, its size
and the server's ETag / Last-Modified. Within the TTL the file is used again without asking the server; after it the
host asks again (a conditional request when the server gave a validator). The index is a small JSON file in the
artifact store, written atomically, so it outlives a boardctrld restart.

Files the cache downloaded itself are its own: once no entry within the TTL refers to one and no running job uses
it, `prune` removes it. A file that was in the store before (an upload) is never removed here.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from core.errors import InvalidRequest
from core.jobs import current_token
from core.dut.artifacts import SHA256, ArtifactStore

Log = Callable[[str], None]

DEFAULT_TTL_S = 24 * 3600
INDEX = ".url-cache.json"

# Shared by every UrlCache on the same index (one boardctrld process): the index lock, a lock per URL, and the
# files running jobs hold (sha256 -> the threads using it).
_guard = threading.Lock()
_index_locks: dict[str, threading.Lock] = {}
_url_locks: dict[tuple[str, str], threading.Lock] = {}
_holds: dict[str, list[tuple[str, threading.Thread]]] = {}


def ttl_from(v: Any) -> float:
    """`modules.dut.flash.url_cache_ttl_s` as configured: seconds, default (None) 24 h, 0 turns the cache off."""
    if v is None:
        return float(DEFAULT_TTL_S)
    try:
        ttl = float(v)
    except (TypeError, ValueError):
        ttl = -1.0
    if ttl < 0 or ttl != ttl:
        raise InvalidRequest(f"modules.dut.flash.url_cache_ttl_s must be a number of seconds >= 0, not {v!r}",
                             hint="set it in the host config (0 turns the URL cache off), or remove it for 24 h")
    return ttl


def shown(url: str) -> str:
    """The URL as a log line shows it: without its query (it may carry a signed token)."""
    base, q, _ = url.partition("?")
    return base.split("#", 1)[0] + ("?..." if q else "")


def _age(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


class UrlCache:
    def __init__(self, store: ArtifactStore, ttl_s: float = DEFAULT_TTL_S):
        self.store = store
        self.ttl_s = float(ttl_s)
        self.path = store.root / INDEX
        key = str(self.path.resolve())
        self._key = key
        with _guard:
            self._lock = _index_locks.setdefault(key, threading.Lock())

    @property
    def enabled(self) -> bool:
        return self.ttl_s > 0

    # ------------------------------------------------------------ index --
    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}                         # none yet, or unreadable: start again (only costs a download)
        if not isinstance(data, dict):
            data = {}
        urls = data.get("urls") if isinstance(data.get("urls"), dict) else {}
        owned = [s for s in data.get("owned") or [] if isinstance(s, str) and SHA256.fullmatch(s)]
        return {"version": 1,
                "urls": {u: e for u, e in urls.items() if isinstance(e, dict) and SHA256.fullmatch(str(e.get("sha256")))},
                "owned": owned}

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_name(f"{INDEX}.{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # URLs may carry tokens
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def fresh(self, entry: dict[str, Any], now: float | None = None) -> bool:
        age = (time.time() if now is None else now) - float(entry.get("fetched_at") or 0)
        return self.enabled and 0 <= age < self.ttl_s

    def lookup(self, url: str) -> dict[str, Any] | None:
        """The URL's entry, with `path`, when its file is still in the store as it was; else None."""
        with self._lock:
            entry = self._read()["urls"].get(url)
        if not entry:
            return None
        p = self.store.find(entry["sha256"])
        try:
            if p is None or p.stat().st_size != int(entry.get("size") or -1):
                return None
        except OSError:
            return None
        return {**entry, "path": str(p), "age_s": time.time() - float(entry.get("fetched_at") or 0)}

    def record(self, url: str, got: dict[str, Any], headers: dict[str, str | None]) -> None:
        """Remember what `url` gave: the downloaded file `got` ({sha256, size, name, stored}) and its validators."""
        with self._lock:
            data = self._read()
            data["urls"][url] = {"sha256": got["sha256"], "size": int(got["size"]), "name": got.get("name"),
                                 "fetched_at": time.time(), "etag": headers.get("etag"),
                                 "last_modified": headers.get("last_modified")}
            if got.get("stored") and got["sha256"] not in data["owned"]:
                data["owned"].append(got["sha256"])
            self._write(data)

    def touch(self, url: str) -> None:
        """The server said the URL's file did not change (304): it is fresh again."""
        with self._lock:
            data = self._read()
            if url in data["urls"]:
                data["urls"][url]["fetched_at"] = time.time()
                self._write(data)

    # ------------------------------------------------------ concurrency --
    @contextmanager
    def lock(self, url: str, log: Log) -> Iterator[None]:
        """One fetch of a URL at a time; a second waits (and can be cancelled while it waits)."""
        with _guard:
            lk = _url_locks.setdefault((self._key, url), threading.Lock())
        if not lk.acquire(blocking=False):
            log(f"waiting for another download of {shown(url)} on this host")
            token = current_token()
            while not lk.acquire(timeout=0.5):
                token.check()
        try:
            yield
        finally:
            lk.release()

    def hold(self, sha256: str, owner: threading.Thread | None = None) -> None:
        """Keep this file from `prune` while `owner` (the calling job's thread) runs."""
        with _guard:
            _holds.setdefault(self._key, []).append((sha256, owner or threading.current_thread()))

    def held(self) -> set[str]:
        with _guard:
            live = [(s, t) for s, t in _holds.get(self._key, []) if t.is_alive()]
            _holds[self._key] = live
            return {s for s, _ in live}

    # ------------------------------------------------------------ prune --
    def prune(self, log: Log | None = None) -> list[str]:
        """Remove the files this cache downloaded that no entry within the TTL refers to and no running job holds,
        and the entries that pointed at them. Returns the sha256s removed. Never raises for a file it cannot
        remove: that is tried again next time."""
        removed: list[str] = []
        with self._lock:
            data = self._read()
            now = time.time()
            keep = {e["sha256"] for e in data["urls"].values() if self.fresh(e, now)} | self.held()
            owned = []
            for sha in data["owned"]:
                d = self.store.root / sha
                if sha in keep:
                    owned.append(sha)
                    continue
                try:
                    if d.is_dir():
                        shutil.rmtree(d)
                except OSError:
                    owned.append(sha)
                    continue
                removed.append(sha)
            gone = set(removed)
            urls = {u: e for u, e in data["urls"].items()
                    if e["sha256"] not in gone and (self.fresh(e, now) or self.store.find(e["sha256"]) is not None)}
            if gone or owned != data["owned"] or urls != data["urls"]:
                data.update(owned=owned, urls=urls)
                self._write(data)
        if log:
            for sha in removed:
                log(f"URL cache: removed an expired package (sha256 {sha[:12]})")
        return removed
