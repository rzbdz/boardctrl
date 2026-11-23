"""The artifact cache's policy: entries past the TTL go, then the least recently used until the store is under its
size and the disk has its floor; held entries and ones just used stay; old unfinished copies go; the URL cache
forgets what was removed; the host reports it in its status."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from core.dut import cachepolicy
from core.dut.artifacts import ArtifactStore
from core.dut.cachepolicy import CachePolicy
from core.errors import InvalidRequest

NOW = 1_800_000_000.0
H = 3600


def entry(root: Path, n: int, size: int, hours_ago: float) -> str:
    sha = f"{n:064x}"
    d = root / sha
    d.mkdir(parents=True)
    (d / "f.bin").write_bytes(b"x" * size)
    at = NOW - hours_ago * H
    os.utime(d / "f.bin", (at, at))
    os.utime(d, (at, at))
    return sha


def policy(root: Path, free: int = 10**12, **kw) -> CachePolicy:
    return CachePolicy(root, now=lambda: NOW, free=lambda p: free, **kw)


def test_entries_past_the_ttl_go_and_the_rest_stay(tmp_path):
    old, recent = entry(tmp_path, 1, 10, 200), entry(tmp_path, 2, 10, 5)
    assert policy(tmp_path, ttl_s=7 * 24 * H).tidy() == [old]
    assert [e["sha256"] for e in policy(tmp_path).entries()] == [recent]
    assert policy(tmp_path, ttl_s=0).tidy() == []          # 0: no age limit


def test_the_least_recently_used_go_first_until_the_store_is_under_its_size(tmp_path):
    a, b, c = entry(tmp_path, 1, 400, 30), entry(tmp_path, 2, 400, 20), entry(tmp_path, 3, 400, 10)
    assert policy(tmp_path, max_bytes=900, ttl_s=0).tidy() == [a]
    assert policy(tmp_path, max_bytes=100, ttl_s=0).tidy() == [b, c]


def test_the_disk_keeps_its_floor(tmp_path):
    a, b = entry(tmp_path, 1, 300, 30), entry(tmp_path, 2, 300, 20)
    # 1000 free, 1200 wanted: removing the oldest (300) is enough.
    assert policy(tmp_path, free=1000, min_free_bytes=1200, max_bytes=0, ttl_s=0).tidy() == [a]
    assert (tmp_path / b).is_dir()


def test_held_entries_and_ones_just_used_stay_whatever_the_settings(tmp_path):
    a, b, c = entry(tmp_path, 1, 100, 300), entry(tmp_path, 2, 100, 300), entry(tmp_path, 3, 100, 0.01)
    done = threading.Event()
    t = threading.Thread(target=lambda: (cachepolicy.hold(tmp_path, a), done.wait(5)))
    t.start()
    try:
        time.sleep(0.05)
        assert policy(tmp_path, max_bytes=1, ttl_s=H).tidy() == [b]
        assert (tmp_path / a).is_dir() and (tmp_path / c).is_dir()
        assert policy(tmp_path).usage()["held"] == 1
    finally:
        done.set()
        t.join()
    assert policy(tmp_path, max_bytes=1, ttl_s=H).tidy() == [a]     # the job ended: no longer held


def test_old_unfinished_copies_go(tmp_path):
    keep = entry(tmp_path, 1, 10, 1)
    part = tmp_path / keep / "f.bin.123.part"
    part.write_bytes(b"p")
    ticket = tmp_path / ".incoming" / ("a" * 32)
    ticket.mkdir(parents=True)
    (ticket / "pkg.zip").write_bytes(b"z")
    fresh = tmp_path / ".incoming" / ("b" * 32)
    fresh.mkdir()
    for p in (part, ticket):
        os.utime(p, (NOW - 30 * H, NOW - 30 * H))
    os.utime(fresh, (NOW - H, NOW - H))
    policy(tmp_path).tidy()
    assert not part.exists() and not ticket.exists() and fresh.exists() and (tmp_path / keep / "f.bin").exists()


def test_the_url_cache_forgets_what_was_removed(tmp_path):
    a, b = entry(tmp_path, 1, 10, 300), entry(tmp_path, 2, 10, 1)
    (tmp_path / ".url-cache.json").write_text(json.dumps({"version": 1, "owned": [a, b], "urls": {
        "https://x.example/a.zip": {"sha256": a, "size": 10, "fetched_at": NOW},
        "https://x.example/b.zip": {"sha256": b, "size": 10, "fetched_at": NOW}}}))
    assert policy(tmp_path, ttl_s=24 * H).tidy() == [a]
    index = json.loads((tmp_path / ".url-cache.json").read_text())
    assert list(index["urls"]) == ["https://x.example/b.zip"] and index["owned"] == [b]


def test_usage_and_settings(tmp_path):
    entry(tmp_path, 1, 10, 1), entry(tmp_path, 2, 30, 1)
    assert policy(tmp_path, free=999).usage() == {
        "path": str(tmp_path), "used": 40, "entries": 2, "held": 0, "max_bytes": 50 << 30, "ttl_s": 7 * 24 * H,
        "min_free_bytes": 5 << 30, "free": 999}
    got = CachePolicy.from_config(tmp_path, {"ttl_s": 60, "max_bytes": "1000", "min_free_bytes": 0})
    assert (got.ttl_s, got.max_bytes, got.min_free_bytes) == (60, 1000, 0)
    with pytest.raises(InvalidRequest) as e:
        CachePolicy.from_config(tmp_path, {"max_bytes": -1})
    assert "modules.dut.cache.max_bytes" in e.value.message


def test_a_store_applies_its_policy_after_each_file_that_arrives(tmp_path):
    import hashlib

    store = ArtifactStore(tmp_path)
    old = entry(tmp_path, 1, 100, 0)
    at = time.time() - 400 * H                       # the store runs on the real clock
    os.utime(tmp_path / old / "f.bin", (at, at))
    os.utime(tmp_path / old, (at, at))
    store.policy = CachePolicy(tmp_path, ttl_s=24 * H, free=lambda p: 10**12)
    new = hashlib.sha256(b"new").hexdigest()
    store.put(new, "n.bin", [b"new"])
    assert not (tmp_path / old).exists() and store.find(new) is not None


def test_the_host_reports_its_cache_in_its_status(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from frontends.server.app import create_app
    from frontends.server.config import HostConfig

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    with TestClient(create_app(HostConfig.load(root / "tests" / "fixtures" / "site" / "hosts" / "dev-mock.yaml"))) as c:
        body = c.get("/api/v1/status").json()
        cache = (body.get("result") or body)["modules"]["dut"]["cache"]
        assert set(cache) == {"path", "used", "entries", "held", "max_bytes", "ttl_s", "min_free_bytes", "free"}
        assert cache["path"].endswith("artifacts") and cache["max_bytes"] == 50 << 30
