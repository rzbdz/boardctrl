"""The public board profiles, read the way a host reads them.

`packs/spacemit` is public hardware knowledge: the pin maps of boards anyone can buy. A profile that
stopped parsing, or that named a pin nothing resolves, would only show up on a board host at runtime,
so the example boards are checked here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.site import layered
from modules.gpio.board import BoardCatalog

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "examples" / "site"


@pytest.mark.parametrize("board_id, header, pins", [
    ("spacemit-k3-com260", "J40", 40),
    ("spacemit-k1x-deb1", "J26", 26),
    ("spacemit-bananapi-f3", "J26", 26),
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
    """The hello example sits on a board the public pack describes, not on a made-up one."""
    catalog = BoardCatalog(layered("boards", SITE))
    board = catalog.get("spacemit-bananapi-f3")
    assert board.id.startswith("spacemit-") and board.pinctrl.get("debugfs")
