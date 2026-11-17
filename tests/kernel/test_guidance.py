"""The leveled guidance: the general hints, one SoC's notes, one SDK's skill — and `--dut`, which asks a host."""

from __future__ import annotations

from pathlib import Path

import pytest

from bsp.core import guidance
from bsp.core.site import layers


@pytest.fixture
def site(tmp_path, monkeypatch):
    site = tmp_path / "site"
    (site / "hints").mkdir(parents=True)
    (site / "skills").mkdir()
    (site / "socs" / "soc-b").mkdir(parents=True)
    (site / "hints" / "general.md").write_text("general notes")
    (site / "skills" / "sdk-a.md").write_text("# sdk-a\nuse the u-boot variant")
    (site / "socs" / "soc-b" / "soc.yaml").write_text("soc: soc-b\n")
    (site / "socs" / "soc-b" / "notes.md").write_text("1.8V UART")
    monkeypatch.setenv("BSP_SITE", str(site))
    return site


def test_the_listing_says_what_there_is_to_ask_for(site):
    listing = guidance.level_listing(site)
    assert "soc-b" in listing["socs"]
    assert listing["skills"] == ["sdk-a"]
    assert any(name.endswith("general.md") for name in listing["hints"])
    assert listing["site"] == str(site)


def test_a_soc_level_carries_its_notes_and_where_its_profile_is(site):
    blocks, text = guidance.resolve(type("A", (), {"soc": "soc-b", "sdk": None, "hints_cmd": None})(), site)
    assert [block["name"] for block in blocks] == ["soc-b/notes.md", "soc-b/soc.yaml"]
    assert "1.8V UART" in text and "soc.yaml" in text


def test_an_sdk_level_is_its_skill(site):
    blocks, text = guidance.resolve(type("A", (), {"soc": None, "sdk": "sdk-a", "hints_cmd": None})(), site)
    assert blocks[0]["name"] == "sdk-a" and "u-boot variant" in text
    blocks, text = guidance.resolve(type("A", (), {"soc": None, "sdk": "nope", "hints_cmd": None})(), site)
    assert blocks == [] and "nothing configured" in text


def test_the_general_level_is_all_the_hints(site):
    blocks, text = guidance.resolve(type("A", (), {"soc": None, "sdk": None, "hints_cmd": None})(), site)
    assert any(block["name"].endswith("general.md") for block in blocks) and "general notes" in text


def test_the_site_wins_over_a_pack_for_the_same_file(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    (pack / "socs" / "soc-b").mkdir(parents=True)
    (pack / "socs" / "soc-b" / "notes.md").write_text("pack note")
    (pack / "socs" / "soc-b" / "soc.yaml").write_text("soc: soc-b\n")
    site = tmp_path / "site"
    (site / "socs" / "soc-b").mkdir(parents=True)
    (site / "socs" / "soc-b" / "notes.md").write_text("site note")
    monkeypatch.setattr("bsp.core.guidance.layers", lambda s=None: [pack, site])
    blocks, text = guidance.resolve(type("A", (), {"soc": "soc-b", "sdk": None, "hints_cmd": None})(), site)
    assert "site note" in text and "pack note" not in text
