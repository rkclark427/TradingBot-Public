from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.data.alpaca_client import AssetInfo
from src.data.assets import get_tradable_symbols, refresh_asset_universe
from src.tracking.models import Base


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _make_asset(**overrides: object) -> AssetInfo:
    defaults = dict(
        symbol="AAPL",
        tradable=True,
        fractionable=True,
        marginable=True,
        shortable=True,
        easy_to_borrow=True,
    )
    defaults.update(overrides)
    return AssetInfo(**defaults)  # type: ignore[arg-type]


def test_refresh_returns_count(session: Session) -> None:
    client = MagicMock()
    client.get_assets.return_value = [_make_asset(symbol="AAPL"), _make_asset(symbol="MSFT")]
    count = refresh_asset_universe(client, session)
    assert count == 2


def test_refresh_persists_to_db(session: Session) -> None:
    client = MagicMock()
    client.get_assets.return_value = [_make_asset(symbol="SPY", fractionable=True)]
    refresh_asset_universe(client, session)

    symbols = get_tradable_symbols(session)
    assert "SPY" in symbols


def test_refresh_upserts_on_second_call(session: Session) -> None:
    client = MagicMock()
    client.get_assets.return_value = [_make_asset(symbol="SPY", tradable=True)]
    refresh_asset_universe(client, session)

    client.get_assets.return_value = [_make_asset(symbol="SPY", tradable=False)]
    refresh_asset_universe(client, session)

    symbols = get_tradable_symbols(session)
    assert "SPY" not in symbols


def test_get_tradable_symbols_excludes_untradable(session: Session) -> None:
    client = MagicMock()
    client.get_assets.return_value = [
        _make_asset(symbol="AAPL", tradable=True),
        _make_asset(symbol="DELIST", tradable=False),
    ]
    refresh_asset_universe(client, session)

    symbols = get_tradable_symbols(session)
    assert "AAPL" in symbols
    assert "DELIST" not in symbols


def test_get_tradable_symbols_require_fractionable(session: Session) -> None:
    client = MagicMock()
    client.get_assets.return_value = [
        _make_asset(symbol="AAPL", fractionable=True),
        _make_asset(symbol="WHOLE", fractionable=False),
    ]
    refresh_asset_universe(client, session)

    symbols = get_tradable_symbols(session, require_fractionable=True)
    assert "AAPL" in symbols
    assert "WHOLE" not in symbols


def test_get_tradable_symbols_require_shortable(session: Session) -> None:
    client = MagicMock()
    client.get_assets.return_value = [
        _make_asset(symbol="AAPL", shortable=True),
        _make_asset(symbol="NOSHORT", shortable=False),
    ]
    refresh_asset_universe(client, session)

    symbols = get_tradable_symbols(session, require_shortable=True)
    assert "AAPL" in symbols
    assert "NOSHORT" not in symbols


def test_get_tradable_symbols_empty_universe(session: Session) -> None:
    assert get_tradable_symbols(session) == []


def test_get_tradable_symbols_sorted(session: Session) -> None:
    client = MagicMock()
    client.get_assets.return_value = [
        _make_asset(symbol="MSFT"),
        _make_asset(symbol="AAPL"),
        _make_asset(symbol="SPY"),
    ]
    refresh_asset_universe(client, session)

    symbols = get_tradable_symbols(session)
    assert symbols == sorted(symbols)
