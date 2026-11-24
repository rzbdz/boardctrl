"""The `serial-mux` console: a serial-mux alias on a USB-UART, and the log it writes."""

from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from core import proc
from core.jobs import current_token
from core.errors import Cancelled, HardwareError, HardwareTimeout, InvalidRequest
from core.dut.contracts import ConsoleProvider
from providers.smtty import logfile

def _run(argv: list[str], timeout: float) -> proc.Result:
    try:
        return proc.run(argv, timeout=timeout)
    except proc.TimedOut:
        raise HardwareTimeout(f"`{' '.join(argv[:4])} ...` timed out after {timeout:g}s") from None
    except FileNotFoundError:
        raise HardwareError(f"{argv[0]} not found on this host", hint="install it or set its path in the host config") from None


# --------------------------------------------------------------- serial --
class SerialConsole(ConsoleProvider):
    """serial-mux alias: commands via `smtty --send/--wait`, passive reads from its
    per-day log (cumulative, append-only) without injecting keystrokes."""

    provider_name = "serial-mux"

    def __init__(self, alias: str, smtty: str = "smtty", log_dir: str = "~/.serial-mux/logs"):
        self.alias = alias
        self.smtty = smtty
        self.log_dir = Path(log_dir).expanduser() / alias

    def _log_path(self, day: date | None = None) -> Path:
        return self.log_dir / f"{(day or date.today()).isoformat()}.log"

    def mark(self) -> tuple[Path, int]:
        p = self._log_path()
        return p, (p.stat().st_size if p.exists() else 0)

    def retained_mark(self) -> tuple[Path, int]:
        """From the start of today's log: the console's history, as far back as the log goes."""
        return self._log_path(), 0

    def read_since(self, mark: tuple[Path, int]) -> str:
        path, off = mark
        chunks = []
        if path.exists():
            with open(path, "rb") as f:
                f.seek(off)
                chunks.append(f.read())
        today = self._log_path()
        if today != path and today.exists():          # day rollover
            chunks.append(today.read_bytes())
        return b"".join(chunks).decode("utf-8", errors="replace")

    def tail(self, lines: int = 50, grep: str | None = None) -> list[str]:
        files = sorted(self.log_dir.glob("*.log"))
        out: list[str] = []
        for p in reversed(files[-2:]):
            out = p.read_bytes().decode("utf-8", errors="replace").splitlines() + out
            if len(out) >= lines * 4:
                break
        if grep:
            rx = re.compile(grep)
            out = [l for l in out if rx.search(l)]
        return out[-lines:]

    def range(self, since: str | None, until: str | None, grep: str | None = None, regex: bool = False,
              limit: int = 20000, first: bool = False, at: str | None = None, before: int = 0, after: int = 0,
              hits: bool = False, max_hits: int = 200000, marks: list[dict[str, Any]] | None = None,
              max_marks: int = 100000, from_line: int = 0) -> dict[str, Any]:
        """The log across its day files, searched or as a window; every line comes with its number (0-based across
        the kept days, `numbers`) and its position (`<day>:<byte offset>`, `positions`), and `total` counts the log.

        Search (no `at`): lines stamped from `since` to `until` (`YYYY-MM-DD HH:MM:SS`, either may be None). A line
        without a stamp of its own goes with the one before it. `grep` keeps matching lines (case-blind text, or a
        regex); at most `limit` lines are returned, the last ones (or the first, with `first`), and `truncated` says
        when some were left out. `matched` counts every line that matched. With `hits` the answer is where they are,
        not their text: `hits`, the numbers of up to `max_hits` matching lines, and `hits_truncated`. `marks`
        ({id, pattern, flags: "" or "i"}) finds the lines a pattern marks (a boot's banners): `marks` in the answer is
        every such line in order, as {line, id, text cut to 200 characters}, at most `max_marks` (`marks_truncated`);
        a line matching several takes the first mark. Marks without a grep return no lines. `from_line` searches only
        from that line on (what a reader following the log has not seen yet).

        Window (`at`): `before` lines before the line `at` names, that line, and `after` lines after it (each at most
        5000), from the plain log: grep, since and until do not apply. `at` is a position, `line:<n>`, `start` or
        `end`. A position inside a line names that line; one on a day no longer kept, past its end or past the last
        line names the next line there is, and says `snapped`. `more_before`/`more_after` say whether the log goes on
        beyond the window; `at` is the position of the line it names (None for start and end)."""
        log = logfile.Log(self.log_dir)
        if at is not None:
            return logfile.window(log, at, before, after)
        try:
            found = logfile.Marks(marks) if marks else None
        except (re.error, KeyError, TypeError) as e:
            raise InvalidRequest(f"not a valid mark: {e}",
                                 hint="marks: [{id, pattern, flags: \"\" or \"i\"}], a regex JS and Python both read") from None
        return logfile.search(log, since, until, grep, regex, limit, first, hits, max_hits, found, max_marks,
                              max(0, from_line))

    def nudge(self) -> None:
        """Press Enter on the console (best effort). The log is line-based, so a prompt
        printed without a newline (`~ # `) only shows up once something follows it."""
        try:
            _run([self.smtty, self.alias, "--send", " "], timeout=10)
        except Cancelled:
            raise
        except Exception:  # noqa: BLE001 - a missed nudge only delays the wait
            pass

    def wait(self, pattern: str | None, fail: str | None = None, timeout: float = 60,
             mark: tuple[Path, int] | None = None, stall: float | None = None,
             nudge: float | None = None) -> str:
        """Wait for `pattern` in the log after `mark` (from now when it is None). Raises on `fail` or timeout.
        `nudge`: press Enter whenever the console has been quiet that many seconds."""
        return self._wait(mark or self.mark(), pattern, fail, timeout, stall, nudge)

    def _wait(self, mark: tuple[Path, int], pattern: str | None, fail: str | None,
              timeout: float, stall: float | None = None, nudge: float | None = None,
              after: Callable[[str], str] | None = None) -> str:
        """`after` trims the console text before matching (send uses it to skip the echoed line)."""
        rx = re.compile(pattern) if pattern else None
        frx = re.compile(fail) if fail else None
        deadline = time.monotonic() + timeout
        last_size, last_move = -1, time.monotonic()
        last_nudge = last_move
        token = current_token()          # a cancelled job stops waiting at once
        while True:
            token.check()
            text = self.read_since(mark)
            if after is not None:
                text = after(text)
            if frx and frx.search(text):
                m = frx.search(text)
                ctx = text[max(0, m.start() - 400): m.end() + 200]
                raise HardwareError(f"console reported failure /{fail}/", details={"console": ctx[-800:]})
            if rx is None or rx.search(text):
                return text
            if len(text) != last_size:
                last_size, last_move = len(text), time.monotonic()
            now = time.monotonic()
            if nudge and now - max(last_move, last_nudge) > nudge:
                self.nudge()
                last_nudge = time.monotonic()
            if stall and now - last_move > stall:
                raise HardwareTimeout(f"console went silent for {stall:g}s while waiting for /{pattern}/",
                                      details={"console_tail": text[-800:]})
            if now > deadline:
                raise HardwareTimeout(f"/{pattern}/ not seen on console within {timeout:g}s",
                                      hint="see `bsp dut log <dut>`; the board may be in another stage",
                                      details={"console_tail": text[-800:]})
            token.sleep(0.25)

    def send(self, text: str, pattern: str | None = None, fail: str | None = None, timeout: float = 60,
             mark: Any = None, stall: float | None = None, nudge: float | None = None) -> dict[str, Any]:
        """Send one line to the console and, with `pattern`, wait for it.

        The console log holds both the board's echo of the line and what the line made it
        print, and the line itself may match `pattern` (`send: "echo READY"`, wait: "READY").
        Matching therefore starts after the echoed line: an echo of the text is found in the
        log since the send and everything up to and including its newline is skipped. With
        echo disabled (no such line in the log) the text since the send is matched as it is.
        """
        if not text.strip():
            raise InvalidRequest("nothing to send", hint="`text` is the line to type on the console")
        if "\n" in text or "\r" in text:
            raise InvalidRequest("a send step types one line", hint="put each line in its own send step")
        mark = self.mark()
        r = _run([self.smtty, self.alias, "--send", text], timeout=max(15.0, min(timeout, 60.0)))
        if r.returncode != 0:
            raise HardwareError(f"smtty failed (rc={r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}",
                                hint=f"is serial-mux alias '{self.alias}' running? `serial-mux list` on the host")
        if pattern is None:
            return {"sent": text, "output": self.read_since(mark)}
        matched = self._wait(mark, pattern, fail, timeout, stall, nudge,
                             after=lambda raw: _after_echo(raw, text))
        return {"sent": text, "output": _after_echo(matched, text)}

    def up(self) -> bool:
        return self.log_dir.is_dir()

    def attach(self) -> str:
        return f"{self.smtty} {self.alias}"

    def run(self, command: str, expect: str, timeout: float) -> dict[str, Any]:
        """Send one line, wait for `expect`, return the console text it produced."""
        if not command.strip():
            raise InvalidRequest("empty console command", hint="pass a shell command such as `dmesg | tail`")
        mark = self.mark()
        r = _run([self.smtty, self.alias, "--send", command, "--wait", expect, "--timeout", str(int(timeout))],
                 timeout + 15)
        text = self.read_since(mark)
        if r.returncode == 2:
            raise HardwareTimeout(f"prompt /{expect}/ not seen within {timeout:g}s after `{command}`",
                                  hint="the board may not be booted to a shell: `bsp dut status`",
                                  details={"output": (r.stdout or text)[-2000:]})
        if r.returncode not in (0,):
            raise HardwareError(f"smtty failed (rc={r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}",
                                hint=f"is serial-mux alias '{self.alias}' running? `serial-mux list` on the host")
        return {"command": command, "output": _strip_console(r.stdout, command), "raw": r.stdout[-8000:]}


_TS = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ?")


def _after_echo(text: str, command: str) -> str:
    """The console text after the line that echoes `command` (all of it when no echo is in it)."""
    i = text.find(command)
    if i < 0:
        return text
    rest = text[i + len(command):]
    nl = rest.find("\n")
    return rest[nl + 1:] if nl >= 0 else rest


def _strip_console(out: str, command: str) -> str:
    lines = [_TS.sub("", l).rstrip("\r") for l in out.splitlines()]
    # drop the echoed command line and the trailing prompt
    if lines and command.strip() and command.strip() in lines[0]:
        lines = lines[1:]
    while lines and re.fullmatch(r"\s*(~ )?#\s*", lines[-1]):
        lines.pop()
    return "\n".join(lines)


