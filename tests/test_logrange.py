"""Reading a console's log over a time range, with a search: what export and search in front ends are built on."""
import json
import time
from pathlib import Path

import pytest

from providers.smtty import logfile
from providers.smtty.serial import SerialConsole


def console(tmp_path: Path) -> SerialConsole:
    logs = tmp_path / "logs" / "s1"
    logs.mkdir(parents=True)
    (logs / "2026-11-09.log").write_text(
        "[2026-11-09 23:59:58] U-Boot SPL 2022.10\n"
        "[2026-11-09 23:59:59] DDR: 8 GiB\n")
    (logs / "2026-11-10.log").write_text(
        "[2026-11-10 00:00:01] Starting kernel ...\n"
        "a line printed without a stamp of its own\n"
        "[2026-11-10 00:00:05] ERROR: mmc0 timeout\n"
        "[2026-11-10 09:00:00] root@k1:~# \n")
    return SerialConsole("s1", log_dir=str(tmp_path / "logs"))


def test_a_range_spans_day_files_and_keeps_unstamped_lines_with_the_one_before(tmp_path):
    c = console(tmp_path)
    r = c.range("2026-11-09 23:59:59", "2026-11-10 00:00:05")
    assert r["lines"] == ["[2026-11-09 23:59:59] DDR: 8 GiB", "[2026-11-10 00:00:01] Starting kernel ...",
                          "a line printed without a stamp of its own", "[2026-11-10 00:00:05] ERROR: mmc0 timeout"]
    assert r["truncated"] is False and r["matched"] == 4


def test_a_search_matches_text_case_blind_or_a_regex(tmp_path):
    c = console(tmp_path)
    assert c.range(None, None, grep="error")["lines"] == ["[2026-11-10 00:00:05] ERROR: mmc0 timeout"]
    assert c.range(None, None, grep=r"^\[.*\] (U-Boot|DDR)", regex=True)["matched"] == 2


def test_a_range_is_capped_and_says_so(tmp_path):
    c = console(tmp_path)
    r = c.range(None, None, limit=2)
    assert r["lines"] == ["[2026-11-10 00:00:05] ERROR: mmc0 timeout", "[2026-11-10 09:00:00] root@k1:~# "]
    assert r["truncated"] is True and r["matched"] == 6
    assert c.range(None, None, limit=2, first=True)["lines"][0] == "[2026-11-09 23:59:58] U-Boot SPL 2022.10"


def test_hosts_serve_a_dut_s_log_range(tmp_path, monkeypatch):
    """`dut.logs` over the host API: the DUT's console log, ranged and searched."""
    from fastapi.testclient import TestClient

    from frontends.server.app import create_app
    from frontends.server.config import HostConfig

    root = Path(__file__).resolve().parents[1]
    hosts = tmp_path / "site" / "hosts"
    hosts.mkdir(parents=True)
    for d in ("sdk", "socs", "boards", "duts"):
        (tmp_path / "site" / d).symlink_to(root / "tests" / "fixtures" / "site" / d)
    (hosts / "h.yaml").write_text("host: {id: h}\nserver: {state_dir: state}\n"
                                  "modules: {gpio_relay: {board: example-40pin, backend: mock, pinmux: false}, dut: {serial_log_dir: %s}}\n" % (tmp_path / "logs"))
    console(tmp_path)
    with TestClient(create_app(HostConfig.load(hosts / "h.yaml"))) as c:
        mod = c.app.state.hub.modules["dut"]
        spec = {"id": "k1-a", "board": "k1", "soc": "soc-a", "console": {"alias": "s1"}, "fastboot": {"usb": "usb:0-5"},
                "control": {"backend": "human", "fdl": {"strategy": "strapped"}}}
        monkeypatch.setattr(mod.slots, "_list", lambda: [])
        assert c.post("/api/v1/tools/dut_add", json={"spec": json.dumps(spec)}).json()["ok"]
        r = c.post("/api/v1/tools/dut_logs", json={"dut": "k1-a", "grep": "mmc0"}).json()["result"]
        assert r["lines"] == ["[2026-11-10 00:00:05] ERROR: mmc0 timeout"] and r["truncated"] is False
        bad = c.post("/api/v1/tools/dut_logs", json={"dut": "k1-a", "grep": "(", "regex": True}).json()
        assert bad["error"]["code"] == "invalid_request"


# ------------------------------------------------------- numbers and positions --

def _read_at(c: SerialConsole, pos: str) -> str:
    """The line a position names, read straight from its day file."""
    day, off = pos.split(":")
    with open(c.log_dir / f"{day}.log", "rb") as f:
        f.seek(int(off))
        return f.readline().decode().rstrip("\n").rstrip("\r")


def test_every_line_found_has_its_number_and_a_position_that_reads_it_back(tmp_path):
    c = console(tmp_path)
    r = c.range(None, None)
    assert r["numbers"] == list(range(6)) and r["total"] == 6
    assert len(r["positions"]) == len(r["lines"])
    assert [_read_at(c, p) for p in r["positions"]] == r["lines"]
    assert r["positions"][:3] == ["2026-11-09:0", "2026-11-09:41", "2026-11-10:0"]
    hit = c.range(None, None, grep="mmc0")
    assert hit["numbers"] == [4] and _read_at(c, hit["positions"][0]) == hit["lines"][0]


def test_a_window_holds_the_lines_around_one_and_crosses_days_both_ways(tmp_path):
    c = console(tmp_path)
    w = c.range(None, None, at="line:2", before=2, after=1)          # the first line of the second day
    assert w["numbers"] == [0, 1, 2, 3] and w["at"] == "2026-11-10:0"
    assert w["lines"][2] == "[2026-11-10 00:00:01] Starting kernel ..."
    assert (w["more_before"], w["more_after"], w["snapped"]) == (False, True, False)
    back = c.range(None, None, at="2026-11-09:41", after=3)          # forward from the first day into the second
    assert back["numbers"] == [1, 2, 3, 4] and back["more_after"] is True and back["more_before"] is True
    assert [_read_at(c, p) for p in back["positions"]] == back["lines"]


def test_a_window_from_either_end(tmp_path):
    c = console(tmp_path)
    end = c.range(None, None, at="end", before=2)
    assert end["lines"] == ["[2026-11-10 00:00:05] ERROR: mmc0 timeout", "[2026-11-10 09:00:00] root@k1:~# "]
    assert (end["at"], end["more_before"], end["more_after"]) == (None, True, False)
    start = c.range(None, None, at="start", after=2)
    assert start["numbers"] == [0, 1] and (start["more_before"], start["more_after"]) == (False, True)
    whole = c.range(None, None, at="end", before=5000)
    assert whole["numbers"] == list(range(6)) and not whole["more_before"] and not whole["more_after"]


def test_a_window_is_the_plain_log_whatever_the_search(tmp_path):
    c = console(tmp_path)
    w = c.range(None, None, grep="mmc0", at="line:3", before=1, after=1)
    assert w["numbers"] == [2, 3, 4]


def test_a_position_inside_a_line_or_on_a_purged_day_moves_to_a_line_there_is(tmp_path):
    c = console(tmp_path)
    inside = c.range(None, None, at="2026-11-10:50", after=0)
    assert inside["at"] == "2026-11-10:42" and inside["snapped"] is True and inside["numbers"] == [3]
    exact = c.range(None, None, at="2026-11-10:42")
    assert exact["snapped"] is False and exact["numbers"] == [3]
    gone = c.range(None, None, at="2026-11-01:7", after=1)            # a day purged: the next day there is
    assert gone["at"] == "2026-11-09:0" and gone["snapped"] is True and gone["numbers"] == [0, 1]
    past = c.range(None, None, at="2026-11-09:99999")                 # past a day's end: the next day's first line
    assert past["at"] == "2026-11-10:0" and past["snapped"] is True
    after_all = c.range(None, None, at="2026-12-01:0", before=1)      # past the last day: the last line
    assert after_all["numbers"] == [4, 5] and after_all["snapped"] is True and after_all["more_after"] is False
    beyond = c.range(None, None, at="line:600", before=1)
    assert beyond["numbers"] == [4, 5] and beyond["snapped"] is True


def test_numbers_from_a_search_name_the_same_line_in_a_window(tmp_path):
    c = console(tmp_path)
    for n, line in zip(c.range(None, None)["numbers"], c.range(None, None)["lines"]):
        assert c.range(None, None, at=f"line:{n}")["lines"] == [line]
    for n, pos in zip(c.range(None, None, hits=True)["hits"], c.range(None, None)["positions"]):
        assert c.range(None, None, at=pos)["numbers"] == [n]


def test_crlf_lines_and_a_last_line_without_a_newline(tmp_path):
    logs = tmp_path / "logs" / "s2"
    logs.mkdir(parents=True)
    (logs / "2026-11-10.log").write_bytes(b"[2026-11-10 00:00:01] one\r\n[2026-11-10 00:00:02] two\r\nprompt # ")
    c = SerialConsole("s2", log_dir=str(tmp_path / "logs"))
    r = c.range(None, None)
    assert r["lines"] == ["[2026-11-10 00:00:01] one", "[2026-11-10 00:00:02] two", "prompt # "] and r["total"] == 3
    assert r["positions"] == ["2026-11-10:0", "2026-11-10:27", "2026-11-10:54"]
    assert c.range(None, None, at="end", before=1)["lines"] == ["prompt # "]
    assert c.range(None, None, at="line:1", before=1, after=1)["lines"] == r["lines"]
    assert c.range(None, None, grep="two$", regex=True)["numbers"] == [1]


def test_the_index_follows_a_growing_file_and_starts_again_for_a_new_one(tmp_path, monkeypatch):
    monkeypatch.setattr(logfile, "CHECK", 4)                        # checkpoints every few lines, so they are used
    logs = tmp_path / "logs" / "s3"
    logs.mkdir(parents=True)
    p = logs / "2026-11-10.log"
    p.write_text("".join(f"line {i}\n" for i in range(10)) + "half")
    c = SerialConsole("s3", log_dir=str(tmp_path / "logs"))
    assert c.range(None, None, at="line:10")["lines"] == ["half"] and c.range(None, None)["total"] == 11
    idx = logfile._INDEXES[str(c.log_dir)]["2026-11-10"]
    with open(p, "a") as f:                                         # the last line ends; more come after it
        f.write(" done\n" + "".join(f"more {i}\n" for i in range(9)))
    assert c.range(None, None, at="line:10")["lines"] == ["half done"]
    assert logfile._INDEXES[str(c.log_dir)]["2026-11-10"] is idx    # extended, not built again
    r = c.range(None, None)
    assert r["total"] == 20 and r["lines"][-1] == "more 8"
    for n in range(20):
        assert c.range(None, None, at=f"line:{n}")["lines"] == [r["lines"][n]]
        assert c.range(None, None, at=r["positions"][n])["numbers"] == [n]
    assert idx.marks == [0, 28, 56, 87, 115, 143]                   # lines 0, 4, 8, 12, 16 and where 20 will be
    p.unlink()
    p.write_text("fresh 0\nfresh 1\n")                              # replaced (and shorter): indexed again
    assert c.range(None, None)["lines"] == ["fresh 0", "fresh 1"]
    p.write_text("other 0\nother 1\nother 2\n")                     # replaced in place, longer than before
    assert c.range(None, None, at="line:2")["lines"] == ["other 2"]


def test_total_counts_every_kept_day_and_drops_a_purged_one(tmp_path):
    c = console(tmp_path)
    assert c.range(None, None, at="start")["total"] == 6
    (c.log_dir / "2026-11-09.log").unlink()
    r = c.range(None, None, at="start", after=1)
    assert r["total"] == 4 and r["lines"] == ["[2026-11-10 00:00:01] Starting kernel ..."]


def test_hits_are_the_numbers_of_matching_lines_without_their_text(tmp_path):
    c = console(tmp_path)
    r = c.range(None, None, grep="2026-11-10", hits=True)
    assert r["hits"] == [2, 4, 5] and r["matched"] == 3 and r["hits_truncated"] is False and r["lines"] == []
    capped = c.range(None, None, grep="2026-11-10", hits=True, max_hits=2)
    assert capped["hits"] == [2, 4] and capped["hits_truncated"] is True and capped["matched"] == 3
    assert c.range(None, None, grep=r"^\[.*\] (U-Boot|DDR)", regex=True, hits=True)["hits"] == [0, 1]
    assert c.range(None, None, grep="ä", hits=True)["hits"] == []


BOOT = ["U-Boot SPL 2022.10-dirty (Nov 10 2026 - 08:00:00 +0800)", "DDR: 8 GiB", "OpenSBI v1.3", "Platform Name : k1",
        "U-Boot 2022.10-dirty (Nov 10 2026 - 08:00:00 +0800)", "Hit any key to stop autoboot",
        "[    0.000000] Linux version 6.6.36 (builder@host) (gcc 13.2) #1 SMP", "[    1.234567] mmc0: new HS200 card"]
MARKS = [{"id": "spl", "pattern": r"U-Boot SPL \d", "flags": ""}, {"id": "opensbi", "pattern": "^OpenSBI v", "flags": ""},
         {"id": "uboot", "pattern": r"u-boot \d{4}\.", "flags": "i"}, {"id": "linux", "pattern": r"Linux version \S+", "flags": ""}]


def test_marks_find_each_boot_s_banners_across_days(tmp_path):
    logs = tmp_path / "logs" / "s4"
    logs.mkdir(parents=True)
    (logs / "2026-11-09.log").write_text("noise\n" + "\n".join(BOOT) + "\n" + "\n".join(BOOT[:3]) + "\n")
    (logs / "2026-11-10.log").write_text("\r\n".join(BOOT[3:]) + "\r\n" + "\n".join(BOOT) + "\n")
    c = SerialConsole("s4", log_dir=str(tmp_path / "logs"))
    r = c.range(None, None, marks=MARKS)
    got = [(m["line"], m["id"]) for m in r["marks"]]
    assert got == [(1, "spl"), (3, "opensbi"), (5, "uboot"), (7, "linux"), (9, "spl"), (11, "opensbi"),
                   (13, "uboot"), (15, "linux"), (17, "spl"), (19, "opensbi"), (21, "uboot"), (23, "linux")]
    assert r["lines"] == [] and r["matched"] == 0 and r["marks_truncated"] is False and r["total"] == 25
    for m in r["marks"]:                                            # a mark's number is the line a window shows
        assert c.range(None, None, at=f"line:{m['line']}")["lines"] == [m["text"]]
    assert all(not m["text"].endswith("\r") for m in r["marks"])
    later = c.range(None, None, marks=MARKS, from_line=13)
    assert [m["line"] for m in later["marks"]] == [13, 15, 17, 19, 21, 23]
    both = c.range(None, None, grep="ddr", marks=MARKS, from_line=10, hits=True)
    assert both["hits"] == [10, 18] and [m["line"] for m in both["marks"]][:2] == [11, 13]
    assert c.range(None, None, marks=MARKS, max_marks=2)["marks_truncated"] is True
    first_wins = c.range(None, None, marks=[{"id": "a", "pattern": "U-Boot", "flags": ""}, *MARKS])
    assert {m["id"] for m in first_wins["marks"]} == {"a", "opensbi", "linux"}
    assert c.range(None, None, grep="noise", from_line=1)["matched"] == 0
    assert c.range(None, None, from_line=24)["lines"] == ["[    1.234567] mmc0: new HS200 card"]
    with pytest.raises(Exception, match="not a valid mark"):
        c.range(None, None, marks=[{"id": "bad", "pattern": "(", "flags": ""}])


# ------------------------------------------------------------------- big files --
def _big(tmp_path: Path, lines: int, name: str = "big") -> SerialConsole:
    logs = tmp_path / "logs" / name
    logs.mkdir(parents=True)
    with open(logs / "2026-11-10.log", "w") as f:
        for i in range(0, lines, 10000):
            f.write("".join(f"[2026-11-10 08:{(j // 60) % 60:02d}:{j % 60:02d}] line {j} "
                            f"{'U-Boot SPL 2022.10-dirty' if j % 50000 == 0 else 'payload'}\n"
                            for j in range(i, min(lines, i + 10000))))
    return SerialConsole(name, log_dir=str(tmp_path / "logs"))


def test_a_window_on_a_large_log_is_read_around_its_line_not_from_the_start(tmp_path, monkeypatch):
    c = _big(tmp_path, 200_000)
    assert c.range(None, None, at="start")["total"] == 200_000          # the index, built once
    reads: list[int] = []
    real = open

    class Counting:
        def __init__(self, *a, **k):
            self.f = real(*a, **k)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

        def __getattr__(self, name):
            return getattr(self.f, name)

        def read(self, n=-1):
            b = self.f.read(n)
            reads.append(len(b))
            return b

        def readline(self, *a):
            b = self.f.readline(*a)
            reads.append(len(b))
            return b

    monkeypatch.setattr(logfile, "open", Counting, raising=False)
    w = c.range(None, None, at="line:150000", before=100, after=100)
    assert w["numbers"] == list(range(149_900, 150_101))
    assert w["lines"][100].startswith("[") and " line 150000 " in w["lines"][100]
    assert sum(reads) < 2 << 20                                         # well under the file's ~9 MB
    for p, l in zip(w["positions"][::50], w["lines"][::50]):
        assert _read_at(c, p) == l


def test_window_reads_cost_the_same_at_the_start_middle_and_end_of_a_million_lines(tmp_path):
    c = _big(tmp_path, 1_000_000)
    assert c.range(None, None, at="end", before=1)["total"] == 1_000_000
    took = {}
    for where in ("line:0", "line:500000", "line:999999", "end", "start"):
        best = 1.0
        for _ in range(3):
            t = time.perf_counter()
            w = c.range(None, None, at=where, before=200, after=200)
            best = min(best, time.perf_counter() - t)
        took[where] = best
        assert w["lines"]
    assert max(took.values()) < 0.05, took
    assert took["line:999999"] < 5 * took["line:0"] + 0.005, took
    t = time.perf_counter()
    hits = c.range(None, None, grep="u-boot spl", hits=True)          # one pass over ~42 MB
    assert time.perf_counter() - t < 1.0
    t = time.perf_counter()
    marks = c.range(None, None, marks=MARKS)
    assert time.perf_counter() - t < 2.0
    assert hits["hits"] == list(range(0, 1_000_000, 50_000)) and hits["total"] == 1_000_000
    assert [m["line"] for m in marks["marks"]] == hits["hits"]


def test_unstamped_lines_heading_a_day_go_with_the_stamp_before_them(tmp_path):
    c = console(tmp_path)
    (c.log_dir / "2026-11-11.log").write_text("still booting\n[2026-11-11 00:00:02] up\n")
    assert c.range(None, None, at="end", before=2)["lines"] == ["still booting", "[2026-11-11 00:00:02] up"]
    assert c.range(None, None, grep="still")["numbers"] == [6]                  # no range: every line counts
    assert c.range("2026-11-10 09:00:00", None)["lines"][1] == "still booting"  # with 09:00:00, which is inside
    assert c.range("2026-11-11 00:00:01", None)["lines"] == ["[2026-11-11 00:00:02] up"]


def test_reads_agree_whatever_the_chunk_and_block_sizes(tmp_path, monkeypatch):
    """Search and windows read in pieces; pieces that end anywhere (mid-line, mid-character) change nothing."""
    import random

    rnd = random.Random(7)
    logs = tmp_path / "logs" / "s5"
    logs.mkdir(parents=True)
    every: list[str] = []
    for day in ("2026-11-08", "2026-11-09", "2026-11-10"):
        lines = [f"[{day} 00:00:{i % 60:02d}] " + rnd.choice(["ok", "Ärger", "U-Boot SPL 1", "x" * rnd.randint(0, 90), ""])
                 for i in range(rnd.randint(1, 120))]
        (logs / f"{day}.log").write_text("\n".join(lines) + "\n")
        every += lines
    c = SerialConsole("s5", log_dir=str(tmp_path / "logs"))
    want = c.range(None, None, limit=100000)
    assert want["lines"] == every
    for chunk, block, check in ((7, 5, 2), (64, 3, 3), (1, 1, 1)):
        monkeypatch.setattr(logfile, "CHUNK", chunk)
        monkeypatch.setattr(logfile, "BLOCK", block)
        monkeypatch.setattr(logfile, "CHECK", check)
        logfile._INDEXES.clear()
        got = c.range(None, None, limit=100000)
        assert (got["lines"], got["positions"], got["numbers"]) == (want["lines"], want["positions"], want["numbers"])
        assert c.range(None, None, grep="ärger", hits=True)["hits"] == [n for n, l in enumerate(every) if "Ärger" in l]
        assert [m["line"] for m in c.range(None, None, marks=MARKS[:1])["marks"]] == \
               [n for n, l in enumerate(every) if "U-Boot SPL 1" in l]
        for _ in range(30):
            n, b, a = rnd.randrange(len(every)), rnd.randint(0, 40), rnd.randint(0, 40)
            w = c.range(None, None, at=f"line:{n}", before=b, after=a)
            lo = max(0, n - b)
            assert w["lines"] == every[lo:n + a + 1] and w["numbers"][0] == lo
            assert w["positions"] == want["positions"][lo:n + a + 1]
    logfile._INDEXES.clear()

