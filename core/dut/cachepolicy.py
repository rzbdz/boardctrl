"""How much the host's artifact cache keeps: one policy over everything in it (pulled SDK builds, downloaded and
uploaded packages), so a lab host's disk does not fill with images nobody uses any more.

An entry is one `<sha256>/` directory of the store. It is *used* when a job takes it (a pull, a download, an
upload, a lookup by sha256 marks its directory), and kept while a running job holds it (`hold`). Past the
settings (`modules.dut.cache` in the host config) the entries used longest ago go first:

  ttl_s           an entry not used for this long is removed (default 7 days; 0 keeps entries regardless of age)
  max_bytes       the store is kept under this size (default 50 GiB; 0 = no bound)
  min_free_bytes  and the disk under it keeps at least this much free (default 5 GiB; 0 = no floor)

A held entry is never removed, whatever the settings say; nor is one used within the last `grace_s` (a job that
just found it and is about to hold it). Unfinished uploads and copies (`.incoming`, `.part` files) are removed
once they are a day old. The URL cache's own index (`.url-cache.json`) forgets the entries whose file is gone.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from core.errors import InvalidRequest

Log = Callable[[str], None]

GiB = 1 << 30
DEFAULTS = {"ttl_s": 7 * 24 * 3600, "max_bytes": 50 * GiB, "min_free_bytes": 5 * GiB}
GRACE_S = 600
PART_TTL_S = 24 * 3600

# The files running jobs hold, per store (its resolved root): sha256 -> the threads using it. Shared with the URL
# cache, so a file either one hands to a job stays while the job runs.
_guard = threading.Lock()
_holds: dict[str, list[tuple[str, threading.Thread]]] = {}
_tidying: dict[str, threading.Lock] = {}


def _key(root: Path) -> str:
    return str(Path(root).resolve())


def hold(root: Path, sha256: str, owner: threading.Thread | None = None) -> None:
    """Keep the store's entry `sha256` while `owner` (the calling job's thread) runs."""
    with _guard:
        _holds.setdefault(_key(root), []).append((sha256, owner or threading.current_thread()))


def held(root: Path) -> set[str]:
    """The entries running jobs hold now."""
    with _guard:
        key = _key(root)
        live = [(s, t) for s, t in _holds.get(key, []) if t.is_alive()]
        _holds[key] = live
        return {s for s, _ in live}


def touch(entry: Path) -> None:
    """Mark an entry used now (its directory's time), so the policy keeps what is used and removes what is not."""
    try:
        os.utime(entry)
    except OSError:
        pass


def _number(cfg: dict[str, Any], key: str) -> int:
    v = cfg.get(key, DEFAULTS[key])
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        n = -1
    if n < 0:
        raise InvalidRequest(f"modules.dut.cache.{key} must be a number >= 0, not {v!r}",
                             hint="set it in the host config (0 turns it off), or remove it for the default")
    return n


class CachePolicy:
    def __init__(self, root: Path, ttl_s: int = DEFAULTS["ttl_s"], max_bytes: int = DEFAULTS["max_bytes"],
                 min_free_bytes: int = DEFAULTS["min_free_bytes"], grace_s: int = GRACE_S,
                 now: Callable[[], float] = time.time, free: Callable[[Path], int] | None = None):
        self.root = Path(root)
        self.ttl_s, self.max_bytes, self.min_free_bytes, self.grace_s = ttl_s, max_bytes, min_free_bytes, grace_s
        self.now = now
        self._free = free or (lambda p: shutil.disk_usage(p).free)
        with _guard:
            self._lock = _tidying.setdefault(_key(self.root), threading.Lock())

    @classmethod
    def from_config(cls, root: Path, cfg: dict[str, Any] | None) -> "CachePolicy":
        cfg = dict(cfg or {})
        return cls(root, ttl_s=_number(cfg, "ttl_s"), max_bytes=_number(cfg, "max_bytes"),
                   min_free_bytes=_number(cfg, "min_free_bytes"))

    # ------------------------------------------------------------ what is there --
    def entries(self) -> list[dict[str, Any]]:
        """Each `<sha256>/` entry with its size and when it was last used, least recently used first."""
        out = []
        if not self.root.is_dir():
            return out
        for d in self.root.iterdir():
            if d.name.startswith(".") or not d.is_dir() or len(d.name) != 64:
                continue
            size, used = 0, 0.0
            try:
                used = d.stat().st_mtime
                for p in d.iterdir():
                    st = p.stat()
                    if p.is_file():
                        size += st.st_size
                        used = max(used, st.st_mtime)
            except OSError:
                continue
            out.append({"sha256": d.name, "path": d, "size": size, "used": used})
        return sorted(out, key=lambda e: e["used"])

    def free(self) -> int | None:
        try:
            return int(self._free(self.root))
        except OSError:
            return None

    def usage(self) -> dict[str, Any]:
        """What the cache holds now, for the host's status (`modules.dut.cache`)."""
        entries = self.entries()
        return {"path": str(self.root), "used": sum(e["size"] for e in entries), "entries": len(entries),
                "held": len(held(self.root)), "max_bytes": self.max_bytes, "ttl_s": self.ttl_s,
                "min_free_bytes": self.min_free_bytes, "free": self.free()}

    # ------------------------------------------------------------------- tidy --
    def tidy(self, log: Log | None = None) -> list[str]:
        """Apply the policy: old unfinished copies, then entries past the TTL, then the least recently used until
        the store is under `max_bytes` and the disk has `min_free_bytes`. Held entries, and ones used within the
        grace period, stay. Returns the sha256s removed; never raises for an entry it cannot remove."""
        if not self._lock.acquire(blocking=False):
            return []                       # another job is tidying this store right now
        try:
            return self._tidy(log)
        finally:
            self._lock.release()

    def _tidy(self, log: Log | None) -> list[str]:
        now = self.now()
        self._parts(now)
        keep = held(self.root)
        entries = self.entries()
        removed: list[str] = []
        used = sum(e["size"] for e in entries)
        free = self.free()

        def remove(e: dict[str, Any], why: str) -> bool:
            nonlocal used, free
            try:
                shutil.rmtree(e["path"])
            except OSError:
                return False
            used -= e["size"]
            if free is not None:
                free += e["size"]
            removed.append(e["sha256"])
            if log:
                log(f"artifact cache: removed {e['sha256'][:12]} ({e['size']:,} B, {why})")
            return True

        left = []
        for e in entries:
            if e["sha256"] in keep or now - e["used"] < self.grace_s:
                left.append(e)
            elif self.ttl_s and now - e["used"] > self.ttl_s:
                remove(e, f"unused for {int((now - e['used']) // 3600)} h")
            else:
                left.append(e)
        for e in left:
            over = self.max_bytes and used > self.max_bytes
            short = self.min_free_bytes and free is not None and free < self.min_free_bytes
            if not (over or short):
                break
            if e["sha256"] in keep or now - e["used"] < self.grace_s:
                continue
            remove(e, "the cache is over its size" if over else "the disk is short of space")
        if removed:
            self._forget_urls(set(removed))
        return removed

    def _parts(self, now: float) -> None:
        """Unfinished copies left by a cut transfer: an upload ticket, a `.part` file, older than a day."""
        incoming = self.root / ".incoming"
        candidates = list(incoming.iterdir()) if incoming.is_dir() else []
        if self.root.is_dir():
            for d in self.root.iterdir():
                if d.is_dir() and len(d.name) == 64:
                    candidates += [p for p in d.iterdir() if p.name.endswith(".part")]
        for p in candidates:
            try:
                if now - p.stat().st_mtime > PART_TTL_S:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
            except OSError:
                pass

    def _forget_urls(self, gone: set[str]) -> None:
        """The URL cache's index no longer points at files the policy removed."""
        from core.dut.urlcache import UrlCache

        try:
            UrlCache.forget(self.root, gone)
        except OSError:
            pass
