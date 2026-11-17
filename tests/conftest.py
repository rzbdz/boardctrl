from pathlib import Path

import pytest

from bsp.core.events import EventBus
from bsp.gpio.backends import create_backend
from bsp.gpio.board import BoardCatalog
from bsp.gpio.service import ConfigStore, GpioService

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def board():
    return BoardCatalog(ROOT / "examples" / "site" / "boards").get("example-40pin")


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
    monkeypatch.setenv("BSP_SITE", str(ROOT / "examples" / "site"))
