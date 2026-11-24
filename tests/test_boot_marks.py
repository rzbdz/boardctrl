"""Boot marks: the console lines that start a boot stage on a SoC, from its facts, for a log reader to mark."""
import logging
import re
from pathlib import Path

import yaml

from core.dut.soc import SocFacts

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "tests" / "fixtures" / "site"


def _facts(tmp_path: Path, soc_yaml: str) -> SocFacts:
    d = tmp_path / "socs" / "soc-x"
    d.mkdir(parents=True)
    (d / "soc.yaml").write_text(soc_yaml)
    return SocFacts([tmp_path / "socs"])


def test_the_fingerprint_is_the_boot_rom_mark(tmp_path):
    f = _facts(tmp_path, "soc: soc-x\nfingerprint: ['ROMX: hello', 'sys: 0x12\\b']\n")
    assert f.boot_marks("soc-x") == [{"id": "rom", "title": "Boot ROM", "pattern": r"(?:ROMX: hello)|(?:sys: 0x12\b)",
                                      "flags": "", "stage": 0, "tone": "rom"}]
    rx = re.compile(f.boot_marks("soc-x")[0]["pattern"])
    assert rx.search("[2026-11-10 08:00:00] sys: 0x12 ok") and not rx.search("sys: 0x123")


def test_boot_marks_add_to_it_and_override_by_id_with_defaults(tmp_path):
    f = _facts(tmp_path, "soc: soc-x\nfingerprint: ['ROMX: hello']\n"
                         "boot_marks:\n"
                         "  - {id: rom, title: ROM, stage: 0}\n"
                         "  - {id: ddr, pattern: 'DDR train', flags: i, stage: 1, tone: firmware, title: DDR}\n"
                         "  - {id: extra, pattern: 'Hit any key'}\n")
    assert f.boot_marks("soc-x") == [
        {"id": "rom", "title": "ROM", "pattern": "(?:ROMX: hello)", "flags": "", "stage": 0, "tone": "rom"},
        {"id": "ddr", "title": "DDR", "pattern": "DDR train", "flags": "i", "stage": 1, "tone": "firmware"},
        {"id": "extra", "title": "extra", "pattern": "Hit any key", "flags": "", "stage": 3, "tone": "loader"}]
    no_print = _facts(tmp_path / "b", "soc: soc-x\nboot_marks: [{id: only, pattern: 'U-Boot 20'}]\n")
    assert [m["id"] for m in no_print.boot_marks("soc-x")] == ["only"]


def test_a_higher_layer_s_boot_marks_replace_the_lower_list(tmp_path):
    low, high = tmp_path / "low" / "soc-x", tmp_path / "high" / "soc-x"
    low.mkdir(parents=True)
    high.mkdir(parents=True)
    (low / "soc.yaml").write_text("fingerprint: ['ROMX']\nboot_marks: [{id: a, pattern: A}]\n")
    (high / "soc.yaml").write_text("boot_marks: [{id: b, pattern: B}]\n")
    f = SocFacts([tmp_path / "low", tmp_path / "high"])
    assert [m["id"] for m in f.boot_marks("soc-x")] == ["rom", "b"]


def test_a_mark_that_does_not_hold_up_is_left_out_with_a_warning(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="core.dut.soc")
    f = _facts(tmp_path, "soc: soc-x\nfingerprint: ['ROMX: ok', '(?<=x)ROM', 'bad(']\n"
                         "boot_marks:\n"
                         "  - {id: behind, pattern: '(?<=a)b'}\n"
                         "  - {id: named, pattern: '(?P<x>a)'}\n"
                         "  - {id: inline, pattern: '(?i)linux'}\n"
                         "  - {id: broken, pattern: 'a(b'}\n"
                         "  - {id: noflags, pattern: a, flags: g}\n"
                         "  - {id: tone, pattern: a, tone: bios}\n"
                         "  - {id: stage, pattern: a, stage: two}\n"
                         "  - {id: nopattern}\n"
                         "  - {pattern: no-id}\n"
                         "  - {id: fine, pattern: '(?:U-Boot)(?= 20)\\(?!x'}\n")
    assert f.boot_marks("soc-x") == [
        {"id": "rom", "title": "Boot ROM", "pattern": "(?:ROMX: ok)", "flags": "", "stage": 0, "tone": "rom"},
        {"id": "fine", "title": "fine", "pattern": "(?:U-Boot)(?= 20)\\(?!x", "flags": "", "stage": 3, "tone": "loader"}]
    said = caplog.text
    for name in ("behind", "named", "inline", "broken", "noflags", "tone", "stage", "nopattern", "needs an id",
                 "(?<=x)ROM", "bad("):
        assert name in said, name


def test_an_unknown_soc_has_no_marks():
    f = SocFacts([SITE / "socs"])
    assert f.boot_marks("no-such-soc") == [] and f.boot_marks(None) == []
    assert f.boot_marks("soc-b")[0]["id"] == "rom" and len(f.boot_marks("soc-b")) == 1


def test_every_shipped_soc_s_fingerprint_makes_a_mark_both_runtimes_read(caplog):
    caplog.set_level(logging.WARNING, logger="core.dut.soc")
    files = sorted(ROOT.glob("platforms/*/socs/*/soc.yaml"))
    assert files
    f = SocFacts(sorted({p.parent.parent for p in files}))
    for p in files:
        prof = yaml.safe_load(p.read_text())
        soc = prof.get("soc") or p.parent.name
        marks = {m["id"]: m for m in f.boot_marks(soc)}
        if prof.get("fingerprint"):
            rom = marks["rom"]
            assert rom["pattern"] == "|".join(f"(?:{rx})" for rx in prof["fingerprint"]), soc
            re.compile(rom["pattern"])
            assert "(?<" not in rom["pattern"] and "(?P" not in rom["pattern"]
        for m in marks.values():
            re.compile(m["pattern"], re.IGNORECASE if m["flags"] == "i" else 0)
    assert "left out" not in caplog.text


def test_hosts_serve_a_board_s_boot_marks_and_the_cli_prints_them(tmp_path, monkeypatch, capsys):
    import json

    from frontends.cli.main import main
    from tests.test_logrange import _add, _host

    with _host(tmp_path, monkeypatch) as c:
        _add(c, monkeypatch)                                            # k1-a, on soc-a
        r = c.post("/api/v1/tools/dut_boot_marks", json={"dut": "k1-a"}).json()
        assert r["ok"] and r["result"]["soc"] == "soc-a"
        assert [m["id"] for m in r["result"]["marks"]] == ["rom", "soc-a-ddr"]
        assert r["result"]["marks"][0]["pattern"] == "(?:ROMA: boot start)|(?:ROMA: usb init)|(?:ROMA: setup=)"
        spec = {"id": "odd-1", "board": "k1", "soc": "soc-unknown", "console": {"alias": "s9"},
                "fastboot": {"usb": "usb:0-9"}, "control": {"backend": "human", "fdl": {"strategy": "strapped"}}}
        assert c.post("/api/v1/tools/dut_add", json={"spec": json.dumps(spec)}).json()["ok"]
        none = c.post("/api/v1/tools/dut_boot_marks", json={"dut": "odd-1"}).json()["result"]
        assert none == {"dut": "odd-1", "soc": "soc-unknown", "marks": []}

        class Host:                                                     # the CLI, talking to this host
            id = "h"

            def call(self, tool, args, timeout=120):
                return c.post(f"/api/v1/tools/{tool}", json=args).json()

        monkeypatch.setattr("frontends.cli.fleet.Fleet.find_dut", lambda self, d: Host())
        assert main(["--stateless", "--json", "dut", "boot-marks", "k1-a"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] and out["result"] == r["result"]
        assert main(["--stateless", "dut", "boot-marks", "k1-a"]) == 0
        text = capsys.readouterr().out.splitlines()
        assert text[0].split() == ["ID", "TONE", "STAGE", "PATTERN"]
        assert text[2].split()[:3] == ["soc-a-ddr", "firmware", "1"] and text[2].endswith("(case-blind)")
        assert main(["--stateless", "dut", "boot-marks", "odd-1"]) == 0
        assert "no boot marks (soc: soc-unknown)" in capsys.readouterr().out
