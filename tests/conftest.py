from pathlib import Path

import pytest

from core.events import EventBus
from providers.gpio.backends import create_backend
from providers.gpio.board import BoardCatalog
from providers.gpio.service import ConfigStore, GpioService

ROOT = Path(__file__).resolve().parents[1]
# The site every test runs against: a complete, made-up site — hosts, DUT records, board maps, SoC facts, an SDK
# catalog and one site plugin — and the example a person copies to start their own (never a real one).
SITE = ROOT / "tests" / "fixtures" / "site"


@pytest.fixture
def board():
    return BoardCatalog(SITE / "boards").get("example-40pin")


@pytest.fixture
def backend():
    return create_backend("mock")


@pytest.fixture
def make_service(board, backend, tmp_path):
    def make(**kw):
        kw.setdefault("store", ConfigStore(tmp_path / "channels.json"))
        kw.setdefault("alias_path", tmp_path / "aliases.json")
        return GpioService(board, backend, EventBus(), **kw)

    return make


@pytest.fixture(autouse=True)
def example_site(monkeypatch):
    """Every test runs against the made-up example site (never a real one)."""
    monkeypatch.setenv("BSP_SITE", str(SITE))
