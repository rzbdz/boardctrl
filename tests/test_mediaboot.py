"""Booting the media of a board whose FDL strap is held down, through steps its SoC profile declares."""
import hashlib
import re
import types

import pytest

from core.errors import HardwareTimeout, InvalidRequest
from core.dut.boot import BootRunner
from core.dut.mediaboot import MediaBoot

IMAGE = b"\x00a small image for one boot ROM\n"
SHA = hashlib.sha256(IMAGE).hexdigest()
BANNER = "ROM v2 strap=nor\r\nusb download\r\n"
STEPS = [{"do": "check", "serial": "strap=nor"}, {"do": "getvar", "name": "rom-version", "match": "^2$"},
         {"do": "stage", "image": "handoff"}, {"do": "continue"}, {"do": "wait", "serial": "SPL 20", "timeout": 5}]


def spec(strategy="strapped", **control):
    return types.SimpleNamespace(id="board-a", soc="soc-a",
                                 control={"backend": "gpio-relay", "fdl": {"strategy": strategy}, **control})


def profile(path=None, steps=STEPS, sha=SHA):
    return {"images": {"handoff": {"file": "handoff.bin", "sha256": sha, **({"path": str(path)} if path else {})}},
            "media_boot": {"name": "rom handoff", "steps": steps}}


class Board:
    """A strapped board: a reset lands in download mode; the staged image makes it boot from its media."""

    def __init__(self, banner=BANNER, rom="2", boots=True):
        self.banner, self.rom, self.boots = banner, rom, boots
        self.out, self.calls = "", []

    # fastboot
    usb, binary = "usb:1-1", "fastboot"

    def state(self):
        return {"product": "download", "usb": self.usb}

    def getvar(self, name):
        self.calls.append(f"getvar {name}")
        return self.rom

    def cmd(self, *args, timeout=0):
        self.calls.append(" ".join(a.rsplit("/", 1)[-1] for a in args))
        if args[0] == "continue" and self.boots:
            self.out += "SPL 2022.10\n"
        return "OKAY"

    # console: use SerialConsole's marker shape, with the log's byte stream as backing.
    alias = "s1"

    def mark(self):
        # The real SerialConsole returns a (Path, byte offset), never an integer usable as read_since's marker.
        from pathlib import Path
        return Path("console.log"), len(self.out)

    def retained_mark(self):
        """What the console kept before now: the log's start (SerialConsole's `retained_mark`)."""
        from pathlib import Path
        return Path("console.log"), 0

    def read_since(self, mark):
        path, offset = mark
        return self.out[offset:]

    def tail(self, n):
        return self.out.splitlines()[-n:]

    def wait(self, rx=None, fail=None, timeout=60, mark=None, stall=None, nudge=None):
        if not re.search(rx, self.out[mark[1]:]):
            raise HardwareTimeout(f"/{rx}/ not seen")

    # control
    cfg = {"fdl": {"strategy": "strapped"}}

    def enter_fdl(self, log, seen):
        self.calls.append("reset")
        self.out += self.banner

    def reset(self, log):
        self.calls.append("plain reset")


def runner(board, media=None):
    return BootRunner("board-a", board, board, board, store=None, download_mode={"fastboot": "download"}, media=media)


def image(tmp_path, data=IMAGE):
    p = tmp_path / "handoff.bin"
    p.write_bytes(data)
    return p


def test_an_already_waiting_download_port_uses_its_existing_rom_evidence_without_a_reset(tmp_path):
    from providers.smtty.serial import SerialConsole

    # Exercise the production marker: a retained ROM banner read through SerialConsole, not an integer offset.
    con = SerialConsole("board-a", log_dir=str(tmp_path))
    con.log_dir.mkdir()
    con._log_path().write_text(BANNER)
    b = Board()
    b.mark, b.read_since = con.mark, con.read_since
    b.retained_mark = con.retained_mark
    b.tail = lambda n=50: con.tail(n)
    b.wait = con.wait
    command = b.cmd

    def cmd(*args, **kwargs):
        result = command(*args, **kwargs)
        if args[0] == "continue":
            # The real serial backend appends the boot output to the console log, not to a fake string.
            with con._log_path().open("a") as f:
                f.write("SPL 2022.10\n")
        return result

    b.cmd = cmd
    lines = []
    r = MediaBoot(spec(), profile(image(tmp_path))).run(runner(b), lines.append)
    assert b.calls == ["getvar rom-version", "stage handoff.bin", "continue"]
    assert r["name"] == "rom handoff" and any("already present" in line for line in lines)


def test_a_human_strapped_board_with_an_existing_banner_does_not_need_another_request(tmp_path):
    # Human/relay is only how the action is performed; it must not change the media-boot decision.
    s = spec()
    s.control["backend"] = "human"
    b = Board()
    b.out = BANNER
    MediaBoot(s, profile(image(tmp_path))).run(runner(b), lambda _line: None)
    assert b.calls == ["getvar rom-version", "stage handoff.bin", "continue"]


def test_a_strapped_board_boots_its_media_through_the_steps_its_soc_declares(tmp_path):
    b = Board()
    m = MediaBoot(spec(), profile(image(tmp_path)))
    assert m.available() and m.name == "rom handoff"
    lines = []
    r = m.run(runner(b), lines.append)
    assert b.calls == ["reset", "getvar rom-version", "stage handoff.bin", "continue"]
    assert r["name"] == "rom handoff" and any("done" in l for l in lines)


@pytest.mark.parametrize("board,error", [
    (Board(banner="ROM v2 strap=sd\r\nusb download\r\n"), "does not match /strap=nor/"),
    (Board(rom="3"), "rom-version = '3' does not match"),
])
def test_a_board_the_steps_do_not_suit_is_refused_before_anything_is_staged(tmp_path, board, error):
    with pytest.raises(InvalidRequest, match=error):
        MediaBoot(spec(), profile(image(tmp_path))).run(runner(board), lambda l: None)
    assert not any(c.startswith("stage") for c in board.calls)


def test_an_image_that_does_not_hash_right_is_never_staged_and_a_silent_board_is_reported(tmp_path):
    b = Board()
    with pytest.raises(InvalidRequest, match="has sha256 .*, not "):
        MediaBoot(spec(), profile(image(tmp_path, b"something else"))).run(runner(b), lambda l: None)
    assert b.calls == []
    stuck = Board(boots=False)
    with pytest.raises(HardwareTimeout, match="SPL 20"):
        MediaBoot(spec(), profile(image(tmp_path))).run(runner(stuck), lambda l: None)


def test_who_needs_it_and_who_can_have_it(tmp_path):
    assert "not needed" in MediaBoot(spec("button"), profile(tmp_path)).unavailable()
    assert "switched off" in MediaBoot(spec(media_boot=False), profile(tmp_path)).unavailable()
    assert "declares no media_boot" in MediaBoot(spec(), {}).unavailable()
    assert "bsp images sync" in MediaBoot(spec(), profile()).unavailable()
    assert "may not use: flash" in MediaBoot(spec(), profile(tmp_path, steps=[{"do": "flash"}])).unavailable()
    # One DUT can bring its own steps, whatever its SoC profile says.
    own = MediaBoot(spec(media_boot={"name": "own", "steps": [{"do": "continue"}]}), {})
    assert own.available() and own.name == "own"


def test_recipes_cold_boot_the_flashed_media_of_a_strapped_board_instead_of_skipping_it(tmp_path):
    steps = [{"do": "reset", "when": "fdl-not-strapped", "why": "cold boot the flashed media"},
             {"do": "wait", "when": "fdl-not-strapped", "serial": "SPL 20", "timeout": 5}]
    b, lines = Board(), []
    runner(b, MediaBoot(spec(), profile(image(tmp_path)))).run_steps(steps, lines.append)
    assert b.calls[-2:] == ["stage handoff.bin", "continue"] and "plain reset" not in b.calls
    assert not any("skipped" in l for l in lines)
    # Without a way to boot its media, the steps are skipped and the log says why.
    b2, lines2 = Board(), []
    runner(b2, MediaBoot(spec(), {})).run_steps(steps, lines2.append)
    assert sum("skipped" in l and "declares no media_boot" in l for l in lines2) == 2 and b2.calls == []


def test_the_topology_a_record_reports_is_the_one_it_is_driven_by(tmp_path):
    """A record written the current way states its relay settings under `services.relay`; one written before the role
    had a name states them under `control:`. Media boot, the FDL policy and the board's status must read the same
    one whichever way the record was written — otherwise a board says it is strapped and is driven as a button."""
    from core.dut import services as bindings
    from core.dut.records import spec_from_dict

    written_ways = ({"console": {"alias": "tty-a"}, "fastboot": {"usb": "usb:1-1"},
                     "control": {"backend": "gpio-relay", "fdl": {"strategy": "strapped"}}},
                    {"services": {"relay": {"provider": "gpio-relay", "fdl": {"strategy": "strapped"}}}})
    for written in written_ways:
        spec = spec_from_dict({"id": "b", "host": "h", **written})
        assert (bindings.relay_settings(spec).get("fdl") or {}).get("strategy") == "strapped", written
        # The one answer every reader uses: the board can boot its media this way, and it is driven as strapped.
        assert MediaBoot(spec, profile(image(tmp_path))).available(), written
        driven = types.SimpleNamespace(**{**vars(spec)})
        assert (bindings.relay_settings(driven).get("fdl") or {}).get("strategy") == "strapped", written


def test_a_console_that_keeps_no_log_is_told_so_and_does_not_crash(tmp_path):
    """Media boot reads the console's retained output to see whether the board is already in download mode, and a
    recipe's `check` step reads what it printed. A provider whose mark is None (it keeps nothing) says "nothing
    before now" — the board is then driven by a reset — and a step that needs the log says why it cannot run rather
    than unpacking None."""
    class Ephemeral(Board):
        """A console with no log to read back: the contract's default marker."""

        def mark(self):
            return None

        def read_since(self, mark):
            return ""

    b = Ephemeral()
    b.out = BANNER                                # the board already prints a ROM banner
    with pytest.raises(InvalidRequest) as e:
        MediaBoot(spec(), profile(image(tmp_path))).run(runner(b), lambda _l: None)
    assert "keeps no log" in e.value.message and "check" in e.value.message
    # It did not trust a banner it could not read: it reset the board and began the steps there, stopping at the
    # first one that needs what the console printed.
    assert b.calls == ["reset"]
