"""The URL cache: a package URL fetched within the TTL is used again without a request; after it the host asks the
server again (conditionally), and what the cache downloaded is removed once nothing needs it."""

import hashlib
import http.server
import json
import threading
import time
from pathlib import Path

import pytest

from core.errors import InvalidRequest
from core.dut import flashpkg, urlcache
from core.dut.artifacts import ArtifactStore


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def srv():
    """An HTTP server with one mutable file per path, an ETag per content, and a count of the bodies it sent."""

    class S(http.server.ThreadingHTTPServer):
        files: dict[str, bytes] = {}
        gets: list[str] = []
        conditional: list[str] = []
        delay = 0.0

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = self.server.files.get(self.path)
            if data is None:
                self.send_error(404)
                return
            etag = f'"{_sha(data)[:16]}"'
            if self.headers.get("If-None-Match"):
                self.server.conditional.append(self.path)
                if self.headers["If-None-Match"] == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
            self.server.gets.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", etag)
            self.end_headers()
            half = len(data) // 2
            self.wfile.write(data[:half])
            self.wfile.flush()
            time.sleep(self.server.delay)
            self.wfile.write(data[half:])

        def log_message(self, *a):
            pass

    s = S(("127.0.0.1", 0), H)
    s.files, s.gets, s.conditional = {}, [], []
    threading.Thread(target=s.serve_forever, daemon=True).start()
    s.url = f"http://127.0.0.1:{s.server_address[1]}"
    yield s
    s.shutdown()


def _age_entries(store: ArtifactStore, by: float) -> None:
    """Make every entry of the store's URL index `by` seconds older."""
    p = store.root / urlcache.INDEX
    data = json.loads(p.read_text())
    for e in data["urls"].values():
        e["fetched_at"] -= by
    p.write_text(json.dumps(data))


def _in_thread(fn, *a, **k):
    """Run `fn` in a thread that then ends (its holds on files end with it, as a finished job's do)."""
    out: dict = {}

    def run():
        try:
            out["r"] = fn(*a, **k)
        except BaseException as e:  # noqa: BLE001
            out["e"] = e

    t = threading.Thread(target=run)
    t.start()
    t.join(30)
    if "e" in out:
        raise out["e"]
    return out["r"]


def test_a_url_fetched_within_the_ttl_is_used_again_without_a_request(tmp_path, srv):
    store, lines = ArtifactStore(tmp_path / "art"), []
    data = b"pkg-1" * 1000
    srv.files["/daily/pkg.tgz?build=7"] = data
    url = f"{srv.url}/daily/pkg.tgz?build=7"
    first = flashpkg.fetch(store, url, None, lines.append)
    assert first["fetched"] and first["sha256"] == _sha(data) and srv.gets == ["/daily/pkg.tgz?build=7"]
    again = flashpkg.fetch(store, url, None, lines.append)
    assert again == {"sha256": _sha(data), "path": first["path"], "size": len(data), "fetched": False, "cached": True}
    assert len(srv.gets) == 1 and not srv.conditional                     # no request at all
    hit = [l for l in lines if "from the URL cache" in l]
    assert len(hit) == 1 and _sha(data)[:12] in hit[0] and "/daily/pkg.tgz?..." in hit[0] and "ago" in hit[0]
    assert "build=7" not in hit[0]                                        # a query may carry a token: not logged
    # the query string is part of the key: another build is another URL
    srv.files["/daily/pkg.tgz?build=8"] = b"pkg-2"
    assert flashpkg.fetch(store, f"{srv.url}/daily/pkg.tgz?build=8", None, lines.append)["fetched"]


def test_the_index_survives_a_new_store_and_a_missing_file_is_fetched_again(tmp_path, srv):
    data = b"persist" * 100
    srv.files["/p.tgz"] = data
    url = f"{srv.url}/p.tgz"
    flashpkg.fetch(ArtifactStore(tmp_path / "art"), url, None, lambda m: None)
    index = json.loads((tmp_path / "art" / urlcache.INDEX).read_text())
    e = index["urls"][url]
    assert e["sha256"] == _sha(data) and e["size"] == len(data) and e["etag"] and e["fetched_at"] <= time.time()
    assert oct((tmp_path / "art" / urlcache.INDEX).stat().st_mode & 0o777) == "0o600"
    store = ArtifactStore(tmp_path / "art")                               # as after a boardctrld restart
    assert flashpkg.fetch(store, url, None, lambda m: None)["cached"] and len(srv.gets) == 1
    Path(store.find(_sha(data))).unlink()                                 # the file went: the entry is not used
    assert flashpkg.fetch(store, url, None, lambda m: None)["fetched"] and len(srv.gets) == 2


def test_after_the_ttl_the_server_is_asked_and_a_304_refreshes_the_entry(tmp_path, srv):
    store, lines = ArtifactStore(tmp_path / "art"), []
    srv.files["/p.tgz"] = b"v1" * 100
    url = f"{srv.url}/p.tgz"
    flashpkg.fetch(store, url, None, lines.append, ttl_s=60)
    _age_entries(store, 120)
    r = flashpkg.fetch(store, url, None, lines.append, ttl_s=60)
    assert r["cached"] and srv.conditional == ["/p.tgz"] and len(srv.gets) == 1   # 304: no body sent
    assert any("unchanged" in l for l in lines)
    assert flashpkg.fetch(store, url, None, lines.append, ttl_s=60)["cached"] and len(srv.conditional) == 1
    # changed at the server: after the TTL the new file is downloaded and replaces the entry
    srv.files["/p.tgz"] = b"v2" * 100
    _age_entries(store, 120)
    r = flashpkg.fetch(store, url, None, lines.append, ttl_s=60)
    assert r["fetched"] and r["sha256"] == _sha(b"v2" * 100) and len(srv.gets) == 2


def test_a_sha256_other_than_the_cached_one_downloads_and_checks(tmp_path, srv):
    store, lines = ArtifactStore(tmp_path / "art"), []
    old, new = b"old" * 100, b"new" * 100
    srv.files["/p.tgz"] = old
    url = f"{srv.url}/p.tgz"
    flashpkg.fetch(store, url, None, lines.append)
    srv.files["/p.tgz"] = new
    r = flashpkg.fetch(store, url, _sha(new), lines.append)
    assert r["fetched"] and r["sha256"] == _sha(new) and len(srv.gets) == 2
    with pytest.raises(InvalidRequest) as e:                              # a wrong --sha256 still fails
        flashpkg.fetch(store, url, "1" * 64, lines.append)
    assert "hashes to" in e.value.message and len(srv.gets) == 3
    assert flashpkg.fetch(store, url, _sha(new), lines.append)["fetched"] is False   # matching: no request


def test_refresh_downloads_again_and_a_ttl_of_zero_turns_the_cache_off(tmp_path, srv):
    store = ArtifactStore(tmp_path / "art")
    srv.files["/p.tgz"] = b"x" * 100
    url = f"{srv.url}/p.tgz"
    flashpkg.fetch(store, url, None, lambda m: None)
    assert flashpkg.fetch(store, url, None, lambda m: None, refresh=True)["fetched"] and len(srv.gets) == 2
    assert not srv.conditional                                            # refresh asks unconditionally
    assert flashpkg.fetch(store, url, None, lambda m: None, ttl_s=0)["fetched"] and len(srv.gets) == 3
    assert flashpkg.fetch(store, url, None, lambda m: None, ttl_s=0)["fetched"] and len(srv.gets) == 4
    with pytest.raises(InvalidRequest) as e:
        flashpkg.fetch(store, url, None, lambda m: None, ttl_s="soon")
    assert "url_cache_ttl_s" in e.value.message


def test_two_fetches_of_one_url_at_once_download_it_once(tmp_path, srv):
    store = ArtifactStore(tmp_path / "art")
    data = b"slow" * 50000
    srv.files["/big.tgz"] = data
    srv.delay = 0.5
    url = f"{srv.url}/big.tgz"
    results, lines, errors = [], [], []

    def go():
        try:
            results.append(flashpkg.fetch(store, url, None, lines.append))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=go) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert not errors and len(results) == 3 and srv.gets == ["/big.tgz"]
    assert {r["sha256"] for r in results} == {_sha(data)} and sum(1 for r in results if r["fetched"]) == 1
    assert any("waiting for another download" in l for l in lines)
    assert json.loads((store.root / urlcache.INDEX).read_text())["urls"][url]["sha256"] == _sha(data)
    assert not list((store.root / ".incoming").iterdir())


def test_prune_removes_only_expired_files_the_cache_downloaded_and_nothing_in_use(tmp_path, srv):
    store = ArtifactStore(tmp_path / "art")
    srv.files["/a.tgz"], srv.files["/b.tgz"] = b"a" * 100, b"b" * 100
    uploaded = b"uploaded" * 10
    store.put(_sha(uploaded), "up.tgz", [uploaded])
    srv.files["/up.tgz"] = uploaded                                       # a URL giving a file uploaded before
    for p in ("/a.tgz", "/b.tgz", "/up.tgz"):
        _in_thread(flashpkg.fetch, store, f"{srv.url}{p}", None, lambda m: None, ttl_s=60)
    _age_entries(store, 120)
    cache = urlcache.UrlCache(store, 60)
    busy = threading.Event()
    done = threading.Event()

    def job():                                                            # a running job uses b's file
        cache.hold(_sha(b"b" * 100))
        busy.set()
        done.wait(10)

    t = threading.Thread(target=job)
    t.start()
    busy.wait(5)
    removed = cache.prune()
    assert removed == [_sha(b"a" * 100)] and store.find(_sha(b"a" * 100)) is None
    assert store.find(_sha(b"b" * 100)) and store.find(_sha(uploaded))   # in use; not the cache's own
    urls = json.loads((store.root / urlcache.INDEX).read_text())["urls"]
    assert f"{srv.url}/a.tgz" not in urls and f"{srv.url}/b.tgz" in urls
    done.set()
    t.join(5)
    assert cache.prune() == [_sha(b"b" * 100)] and store.find(_sha(uploaded))
    # a fresh entry keeps its file however many other entries expire
    _in_thread(flashpkg.fetch, store, f"{srv.url}/a.tgz", None, lambda m: None, ttl_s=60)
    assert cache.prune() == [] and store.find(_sha(b"a" * 100))


def test_an_unreadable_index_only_costs_a_download(tmp_path, srv):
    store = ArtifactStore(tmp_path / "art")
    (store.root / urlcache.INDEX).write_text("{not json")
    srv.files["/p.tgz"] = b"p" * 10
    assert flashpkg.fetch(store, f"{srv.url}/p.tgz", None, lambda m: None)["fetched"]
    assert flashpkg.fetch(store, f"{srv.url}/p.tgz", None, lambda m: None)["cached"]
