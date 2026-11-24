"""serial-mux's console log on disk: its day files, a line index over them, and reads by line number or position.

The log is one append-only file a day, `<dir>/YYYY-MM-DD.log`; purging removes whole days. Lines end at "\n" (a last
line without one is a line too). Every line has two names:

- its number: 0-based across the retained day files in date order. Numbers shift when an old day is purged, so they
  are for a reader scrolling a log it just counted (`total`).
- its position, `<day>:<byte offset of its first byte in that day's file>`: stable while that day is kept.

A long-lived host keeps a sparse index per day file (the offset of every 1024th line, and the line count), built once
and extended over what was appended since; a file that shrank or was replaced is indexed again. With it a line number
or a position is found by bisection and at most 1023 lines read forward, and a window costs that plus its own lines.
"""

from __future__ import annotations

import re
import threading
from bisect import bisect_left, bisect_right
from itertools import accumulate
from pathlib import Path
from typing import Any

CHECK = 1024                 # index every 1024th line
CHUNK = 8 << 20              # sequential reads: indexing and search
BLOCK = 64 << 10             # backward reads: the lines before a point
WINDOW_MAX = 5000            # lines on either side of a window
HITS_MAX = 1_000_000
HEAD = 64                    # the bytes that tell a replaced file from the one indexed

_DAY = re.compile(r"^\d{4}-\d\d-\d\d$")
_STAMP = re.compile(rb"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]")
POSITION = re.compile(r"^(\d{4}-\d\d-\d\d):(\d+)$")


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").rstrip("\r")


class DayIndex:
    """The sparse line index of one day file, as far as it has been read."""

    def __init__(self, path: Path, ino: int, head: bytes):
        self.path, self.ino, self.head = path, ino, head
        self.size = 0                # bytes indexed
        self.complete = 0            # "\n"-terminated lines in them
        self.last_start = 0          # where the line after the last "\n" starts
        self.marks = [0]             # marks[k] = offset of line k * CHECK
        self.lock = threading.Lock()

    @property
    def lines(self) -> int:
        return self.complete + (1 if self.size > self.last_start else 0)

    def extend(self, size: int) -> None:
        """Index what was appended up to `size`: one pass over the new bytes."""
        with open(self.path, "rb") as f:
            f.seek(self.size)
            pos = self.size
            while pos < size:
                buf = f.read(min(CHUNK, size - pos))
                if not buf:
                    break
                n = buf.count(b"\n")
                first = CHECK - 1 - self.complete % CHECK      # the newline (in buf) that ends a mark's previous line
                if n > first:
                    # the j-th newline of buf is at sum(len(part) for the first j + 1 parts) + j
                    ends = list(accumulate(map(len, buf.split(b"\n"))))
                    self.marks.extend(pos + ends[j] + j + 1 for j in range(first, n, CHECK))
                if n:
                    self.last_start = pos + buf.rfind(b"\n") + 1
                self.complete += n
                pos += len(buf)
            self.size = pos

    def offset_of(self, k: int) -> int:
        """The offset of line `k` of this file (k < lines)."""
        pos = self.marks[k // CHECK]
        skip = k % CHECK
        if skip:
            with open(self.path, "rb") as f:
                f.seek(pos)
                for _ in range(skip):
                    pos += len(f.readline())
        return pos

    def line_at(self, off: int) -> tuple[int, int]:
        """The line holding byte `off` (< size): its number in this file and its offset."""
        i = bisect_right(self.marks, off) - 1
        k, pos = i * CHECK, self.marks[i]
        with open(self.path, "rb") as f:
            f.seek(pos)
            while True:
                n = len(f.readline())
                if not n or pos + n > off:
                    return k, pos
                pos += n
                k += 1


# One index per day file for the life of the process, by directory and day.
_INDEXES: dict[str, dict[str, DayIndex]] = {}
_LOCK = threading.Lock()


def _index(directory: Path, day: str, path: Path) -> tuple[DayIndex, int, int] | None:
    """The day file's index brought up to date, with the size and line count it was read at."""
    try:
        st = path.stat()
        with open(path, "rb") as f:
            head = f.read(HEAD)
    except FileNotFoundError:
        return None
    with _LOCK:
        known = _INDEXES.setdefault(str(directory), {})
        idx = known.get(day)
        # new, replaced or shrunk: read it again. A file written anew under the same inode shows in its first bytes.
        if (idx is None or idx.ino != st.st_ino or st.st_size < idx.size
                or head[: len(idx.head)] != idx.head[: len(head)]):
            idx = known[day] = DayIndex(path, st.st_ino, head)
    with idx.lock:
        if st.st_size > idx.size:
            idx.extend(st.st_size)
        if len(idx.head) < len(head):
            idx.head = head
        return idx, idx.size, idx.lines


def _prune(directory: Path, days: set[str]) -> None:
    with _LOCK:
        known = _INDEXES.get(str(directory)) or {}
        for day in [d for d in known if d not in days]:
            del known[day]


class Log:
    """The day files of one console as they are now: line counts, and reads by number or position."""

    def __init__(self, directory: Path):
        found = sorted((p.stem, p) for p in directory.glob("*.log") if _DAY.match(p.stem)) if directory.is_dir() else []
        self.names: list[str] = []
        self.idx: list[DayIndex] = []
        self.sizes: list[int] = []
        self.starts: list[int] = []          # the number of each day's first line
        total = 0
        for day, path in found:
            got = _index(directory, day, path)
            if got is None:
                continue
            idx, size, lines = got
            self.names.append(day)
            self.idx.append(idx)
            self.sizes.append(size)
            self.starts.append(total)
            total += lines
        self.total = total
        _prune(directory, set(self.names))

    # ---------------------------------------------------------- naming lines
    def _lines(self, i: int) -> int:
        return (self.starts[i + 1] if i + 1 < len(self.starts) else self.total) - self.starts[i]

    def locate(self, n: int) -> tuple[int, int]:
        """Line `n` (< total) as (day file, offset); `total` is the end of the last file."""
        if n >= self.total:
            return len(self.names) - 1, self.sizes[-1] if self.sizes else 0
        i = bisect_right(self.starts, n) - 1
        return i, self.idx[i].offset_of(n - self.starts[i])

    def position(self, i: int, off: int) -> str:
        return f"{self.names[i]}:{off}"

    def resolve(self, pos: str) -> tuple[int, bool]:
        """A position as a line number, and whether it had to move to name a line: an offset inside a line names that
        line; a day no longer kept, or an offset past its end, names the next line there is (the last at the end)."""
        m = POSITION.match(pos)
        day, off = m.group(1), int(m.group(2))
        i = bisect_left(self.names, day)
        if i < len(self.names) and self.names[i] == day:
            if off < self.sizes[i]:
                k, start = self.idx[i].line_at(off)
                return self.starts[i] + k, start != off
            n = self.starts[i] + self._lines(i)
        else:
            n = self.starts[i] if i < len(self.names) else self.total
        return min(n, self.total - 1), True

    def number(self, i: int, off: int) -> int:
        """The number of the line holding `off` in day file `i`; the end of the file is the next day's first line."""
        if off >= self.sizes[i]:
            return self.starts[i] + self._lines(i)
        return self.starts[i] + self.idx[i].line_at(off)[0]

    # --------------------------------------------------------------- reading
    def forward(self, n: int, count: int) -> list[tuple[int, str, str]]:
        """Up to `count` lines from line `n` on, as (number, position, text)."""
        out: list[tuple[int, str, str]] = []
        if count <= 0 or n >= self.total:
            return out
        i, off = self.locate(n)
        while len(out) < count and i < len(self.names):
            size = self.sizes[i]
            with open(self.idx[i].path, "rb") as f:
                f.seek(off)
                while len(out) < count and off < size:
                    raw = f.readline()[: size - off]
                    if not raw:
                        break
                    out.append((n, self.position(i, off), _text(raw.rstrip(b"\n"))))
                    off += len(raw)
                    n += 1
            i, off = i + 1, 0
        return out

    def backward(self, n: int, count: int) -> list[tuple[int, str, str]]:
        """Up to `count` lines before line `n` (or before the end, `total`), oldest first."""
        got: list[tuple[int, str, str]] = []
        if count <= 0 or n <= 0 or not self.names:
            return got
        i, end = self.locate(min(n, self.total))
        n = min(n, self.total)
        while len(got) < count and i >= 0:
            for off, raw in _before(self.idx[i].path, end, count - len(got)):
                n -= 1
                got.append((n, self.position(i, off), _text(raw)))
            i -= 1
            end = self.sizes[i] if i >= 0 else 0
        got.reverse()
        return got

    # ---------------------------------------------------------------- search
    def bounds(self, since: str | None, until: str | None) -> list[tuple[int, int, int]]:
        """The bytes of each day file stamped from `since` to `until`, as (day file, start, end). A line without a stamp
        goes with the one before it; stamps rise through a day's file, so each bound is found by bisection."""
        lo, hi = (since or "")[:19], (until or "9999")[:19]
        day_lo, day_hi = lo[:10] or "0000", hi[:10]
        out = []
        started = not since                 # whether a line stamped `since` or later has been seen
        for i, day in enumerate(self.names):
            if not (day_lo <= day <= day_hi):
                continue
            size = self.sizes[i]
            # Before the range starts, the unstamped lines heading a day go with a stamp before `since`.
            a = 0 if started else _first_stamp(self.idx[i].path, size, lo.encode(), False)
            started = started or a < size
            b = _first_stamp(self.idx[i].path, size, hi.encode(), True) if until and day == day_hi else size
            out.append((i, a, max(a, b)))
        return out


def _before(path: Path, end: int, count: int) -> list[tuple[int, bytes]]:
    """Up to `count` lines of `path` ending before `end` (a line boundary, or the end of the file), newest first, as
    (offset, raw line without "\n"). Reads backwards in blocks, never from the start of the file."""
    got: list[tuple[int, bytes]] = []
    carry = b""
    pos = end
    first = True
    with open(path, "rb") as f:
        while len(got) < count and pos > 0:
            step = min(BLOCK, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + carry
            e = pos + len(buf)
            parts = buf.split(b"\n")
            if first and buf.endswith(b"\n"):
                parts.pop()
                e -= 1
            first = False
            body = parts[1:] if pos > 0 else parts
            carry = parts[0] if pos > 0 else b""
            for part in reversed(body):
                s = e - len(part)
                got.append((s, part))
                e = s - 1
                if len(got) >= count:
                    break
    return got


def _first_stamp(path: Path, size: int, key: bytes, strict: bool) -> int:
    """The offset of the first stamped line whose stamp is >= `key` (> with `strict`), or `size`."""
    def after(x: int, limit: int) -> tuple[int, bytes | None]:
        # the first stamped line starting at or after x (and before limit)
        f.seek(max(0, x - 1))
        pos = max(0, x - 1)
        if x > 0:
            pos += len(f.readline())          # to the start of the next line
        while pos < limit:
            raw = f.readline()
            if not raw:
                break
            m = _STAMP.match(raw)
            if m:
                return pos, m.group(1)
            pos += len(raw)
        return limit, None

    def below(stamp: bytes) -> bool:
        return stamp <= key if strict else stamp < key

    with open(path, "rb") as f:
        lo, hi = 0, size
        while lo < hi:
            mid = (lo + hi) // 2
            s, stamp = after(mid, hi)
            if stamp is not None and below(stamp):
                lo = s + 1
            else:
                hi = mid
        return after(lo, size)[0]


def _matches(buf: bytes, grep: str, regex: bool):
    """The lines of `buf` (whole lines) that match, as (line index in buf, start, end) byte offsets in buf.

    One pass with no per-line work between matches: the needle is found in the whole chunk and its line worked out
    from the newlines before it. An ASCII needle is matched case-blind on the bytes; any other needle, and a regex,
    on the decoded text, mapped back to bytes through the chunk's line starts."""
    if not regex and grep.isascii():
        hay, needle = buf.lower(), grep.lower().encode()
        i = hay.find(needle)
        k, counted = 0, 0
        while i >= 0:
            s = buf.rfind(b"\n", 0, i) + 1
            e = buf.find(b"\n", i)
            e = len(buf) if e < 0 else e
            k += buf.count(b"\n", counted, s)
            counted = s
            yield k, s, e
            i = hay.find(needle, e + 1)
        return
    text = buf.decode("utf-8", errors="replace")
    starts: list[int] | None = None
    if regex:
        multi, rx = re.compile(grep, re.MULTILINE), re.compile(grep)
        hay = text.replace("\r\n", "\n")      # `$` at the end of a CRLF line; the lines stay where they were
    else:
        hay, needle = text.casefold(), grep.casefold()
    i, k, counted = 0, 0, 0
    while i <= len(hay):
        if regex:
            m = multi.search(hay, i)
            if m is None:
                return
            at = m.start()
        else:
            at = hay.find(needle, i)
            if at < 0:
                return
        s = hay.rfind("\n", 0, at) + 1
        if s >= len(hay):
            return                          # past the last line
        e = hay.find("\n", at)
        e = len(hay) if e < 0 else e
        k += hay.count("\n", counted, s)
        counted = s
        if not regex or rx.search(hay[s:e]):
            if starts is None:           # byte offsets of the chunk's lines, once it has a match
                starts = [0, *(e + j + 1 for j, e in enumerate(accumulate(map(len, buf.split(b"\n")))))]
            bs = starts[k]
            be = starts[k + 1] - 1 if k + 1 < len(starts) else len(buf)
            yield k, bs, be
        i = e + 1


def _chunks(path: Path, a: int, b: int):
    """Bytes a..b of `path` in large pieces that end at a line boundary, with their offsets."""
    with open(path, "rb") as f:
        f.seek(a)
        pos = a
        carry = b""
        while pos < b:
            more = f.read(min(CHUNK, b - pos - len(carry)))
            if not more:                    # the file is shorter than it was
                if carry:
                    yield pos, carry
                return
            buf = carry + more
            if pos + len(buf) < b:
                cut = buf.rfind(b"\n")
                if cut < 0:                 # one line longer than a chunk: read on
                    carry = buf
                    continue
                buf, carry = buf[: cut + 1], buf[cut + 1:]
            else:
                carry = b""
            yield pos, buf
            pos += len(buf)


def window(log: Log, at: str, before: int, after: int) -> dict[str, Any]:
    """`before` lines before the line `at` names, that line, and `after` lines after it. `at` is a position,
    `line:<n>`, `start` (the window starts at the first line) or `end` (it ends at the last)."""
    before, after = min(max(0, before), WINDOW_MAX), min(max(0, after), WINDOW_MAX)
    snapped = False
    at_pos = None
    if at == "start":
        rows = log.forward(0, after)
    elif at == "end":
        rows = log.backward(log.total, before)
    else:
        if at.startswith("line:"):
            n = int(at[5:])
            snapped = n >= log.total
            n = min(n, log.total - 1)
        else:
            n, snapped = log.resolve(at)
        if n < 0:                       # an empty log
            rows = []
            snapped = True
        else:
            here = log.forward(n, after + 1)
            at_pos = here[0][1] if here else None
            rows = log.backward(n, before) + here
    first = rows[0][0] if rows else (log.total if at == "end" else 0)
    last = rows[-1][0] if rows else first - 1
    return {"lines": [r[2] for r in rows], "positions": [r[1] for r in rows], "numbers": [r[0] for r in rows],
            "at": at_pos, "snapped": snapped, "more_before": first > 0, "more_after": last < log.total - 1,
            "total": log.total}


class Marks:
    """Patterns that mark lines (a boot's banners): one combined regex run over the decoded chunk, so a search finds
    them in one pass; a line matching several takes the first mark in list order."""

    def __init__(self, marks: list[dict[str, Any]]):
        self.ids: list[str] = []
        self.rx: list[re.Pattern] = []
        parts = []
        for m in marks:
            flags = m.get("flags") or ""
            one = re.compile(m["pattern"], re.IGNORECASE if flags == "i" else 0)
            self.ids.append(str(m["id"]))
            self.rx.append(one)
            parts.append(f"(?i:{m['pattern']})" if flags == "i" else f"(?:{m['pattern']})")
        self.any = re.compile("|".join(parts), re.MULTILINE)

    def find(self, text: str):
        """The lines of `text` (whole lines, "\n" between them) a mark matches, as (line index, mark id, line)."""
        i, k, counted = 0, 0, 0
        while True:
            m = self.any.search(text, i)
            if m is None:
                return
            s = text.rfind("\n", 0, m.start()) + 1
            if s >= len(text):
                return
            e = text.find("\n", m.start())
            e = len(text) if e < 0 else e
            line = text[s:e].rstrip("\r")
            for j, rx in enumerate(self.rx):    # the first mark in list order, on the line alone
                if rx.search(line):
                    k += text.count("\n", counted, s)
                    counted = s
                    yield k, self.ids[j], line
                    break
            i = e + 1


def search(log: Log, since: str | None, until: str | None, grep: str | None, regex: bool, limit: int, first: bool,
           hits: bool = False, max_hits: int = 200000, marks: Marks | None = None, max_marks: int = 100000,
           from_line: int = 0) -> dict[str, Any]:
    """Lines stamped from `since` to `until`, from line `from_line` on, searched; see SerialConsole.range."""
    from collections import deque

    spans = log.bounds(since, until) if (since or until) else [(i, 0, s) for i, s in enumerate(log.sizes)]
    if from_line > 0:                   # only from this line on: where it is, by bisection
        fi, foff = log.locate(from_line) if from_line < log.total else (len(log.names), 0)
        spans = [(i, max(a, foff) if i == fi else a, b) for i, a, b in spans if i >= fi]
    spans = [(i, a, b) for i, a, b in spans if b > a]
    max_hits = min(max(1, max_hits), HITS_MAX)
    found: list[int] = []
    rows: list[tuple[int, str, str]] = []
    marked: list[dict[str, Any]] = []
    marked_all = 0
    matched = 0
    if not grep and not marks:          # everything in the range: one run of line numbers, read only where shown
        n0 = log.number(spans[0][0], spans[0][1]) if spans else 0
        n1 = log.number(spans[-1][0], spans[-1][2]) if spans else 0
        matched = max(0, n1 - n0)
        if hits:
            found = list(range(n0, n0 + min(matched, max_hits)))
        else:
            rows = log.forward(n0, min(limit, matched)) if first else log.backward(n1, min(limit, matched))
    else:
        keep: deque | list = [] if first else deque(maxlen=limit)
        for i, a, b in spans:
            n = log.number(i, a)
            for off, buf in _chunks(log.idx[i].path, a, b):
                if grep:
                    for k, s, e in _matches(buf, grep, regex):
                        matched += 1
                        if hits:
                            if len(found) < max_hits:
                                found.append(n + k)
                        elif not first or len(keep) < limit:
                            keep.append((n + k, log.position(i, off + s), _text(buf[s:e])))
                if marks:
                    text = buf.decode("utf-8", errors="replace").replace("\r\n", "\n")
                    for k, mid, line in marks.find(text):
                        marked_all += 1
                        if len(marked) < max_marks:
                            marked.append({"line": n + k, "id": mid, "text": line[:200]})
                n += buf.count(b"\n") + (0 if buf.endswith(b"\n") else 1)
        rows = list(keep)
    out: dict[str, Any] = {"lines": [r[2] for r in rows], "positions": [r[1] for r in rows],
                           "numbers": [r[0] for r in rows], "matched": matched, "total": log.total}
    if hits:
        out.update(hits=found, hits_truncated=matched > len(found), truncated=matched > len(found))
    else:
        out["truncated"] = matched > len(rows)
    if marks:
        out.update(marks=marked, marks_truncated=marked_all > len(marked))
    return out
