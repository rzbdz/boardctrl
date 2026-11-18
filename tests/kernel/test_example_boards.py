"""The public board maps, read the way a host reads them.

`platforms/spacemit` is public hardware knowledge: the pin maps of boards anyone can buy. A map that
stopped parsing, or that named a pin nothing resolves, would only show up on a board host at runtime,
so the example boards are checked here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.site import layered
from providers.gpio.board import BoardCatalog

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "tests" / "fixtures" / "site"
SITE = SITE


@pytest.mark.parametrize("board_id, header, pins", [
    ("spacemit-k3-com260", "J40", 40),
    ("spacemit-k1x-deb1", "J26", 26),
])
def test_a_public_board_profile_parses_with_its_header(board_id, header, pins):
    catalog = BoardCatalog(layered("boards", SITE))
    board = catalog.get(board_id)
    assert [h.id for h in board.headers] == [header]
    assert len(board.headers[0].pins) == pins
    # Power and ground pins are named; a GPIO pin resolves to a line number or is honestly `other`.
    kinds = {p.kind for p in board.headers[0].pins.values()}
    assert kinds <= {"power", "gnd", "gpio", "other"}
    assert board.headers[0].pins[1].kind == "power" and board.headers[0].pins[6].kind == "gnd"


def test_the_example_board_is_a_real_k1_board():
    """The hello example sits on a board the shipped profile describes, not on a made-up one."""
    catalog = BoardCatalog(layered("boards", SITE))
    board = catalog.get("spacemit-k1x-deb1")
    assert board.id.startswith("spacemit-") and board.pinctrl.get("debugfs")


def test_a_site_overrides_a_shipped_profile_and_the_rest_still_applies(tmp_path):
    """One mechanism, two sources: the site is the last profile, so it wins file by file.

    A site that pins its own copy of one board profile must still see the shipped SoCs and the other
    boards — that is what makes the shipped profiles usable without copying them into the site.
    """
    import shutil

    from core.site import layered, layers

    site = tmp_path / "site"
    (site / "boards").mkdir(parents=True)
    # The site replaces one board with a two-pin stub of its own.
    (site / "boards" / "spacemit-k1x-deb1.yaml").write_text(
        "id: spacemit-k1x-deb1\nname: local pin map\nheaders:\n  - {id: J1, layout: single-row, pin_count: 2, "
        "pins: {1: {kind: power, label: 3.3V}, 2: {kind: gnd, label: GND}}}\n")
    # Lowest first: every shipped platform, then the site.
    assert layers(site)[-1] == site
    catalog = BoardCatalog(layered("boards", site))
    assert len(catalog.get("spacemit-k1x-deb1").headers[0].pins) == 2     # the site's copy won
    assert catalog.get("spacemit-k3-com260") is not None                     # the shipped ones still apply


def test_every_shipped_platform_is_read_and_its_socs_stay_distinct():
    """Each directory under `platforms/` is a platform; a host reads all of them."""
    from core.site import shipped
    from core.dut.soc import SocFacts

    for name in [p.name for p in (ROOT / "platforms").iterdir() if p.is_dir()]:
        assert (ROOT / "platforms" / name / "platform.yaml").is_file(), f"{name} has no platform.yaml"
    profiles = SocFacts([p / "socs" for p in shipped()])
    assert {"k1", "k3", "rk3568"} <= set(profiles.profiles)
    assert {p.name for p in shipped()} == {"rockchip", "spacemit"}
    rk = profiles.get("rk3568")
    assert rk["download_mode"] == {}
    assert profiles.detect("DDR Version V1.13 20220218\nIN\n")[0] == "rk3568"
    assert profiles.detect("usb2d_initialize : enter\nU-Boot SPL 2022.10\n")[0] == "k1"
    assert not profiles.looks_like_boot("IN\n")
