"""Tests for src/backtest/data.py — BacktestDataLayer and BacktestMarketDataView."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.backtest.data import BacktestDataLayer, BacktestMarketDataView, LookaheadError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_yfinance_df(
    symbol: str,
    dates: list[date],
    closes: list[float],
    opens: list[float] | None = None,
) -> pd.DataFrame:
    """Build a DataFrame shaped like yfinance Ticker.history() output."""
    n = len(dates)
    opens = opens or [c - 1.0 for c in closes]
    # yfinance 1.x returns tz-aware DatetimeIndex
    index = pd.to_datetime([d.isoformat() for d in dates]).tz_localize("America/New_York")
    return pd.DataFrame(
        {
            "Open": opens,
            "High": [c + 1.0 for c in closes],
            "Low": [c - 2.0 for c in closes],
            "Close": closes,
            "Volume": [1_000_000] * n,
        },
        index=index,
    )


@pytest.fixture()
def tmp_layer(tmp_path: Path) -> BacktestDataLayer:
    return BacktestDataLayer(tmp_path / "test_cache.db")


def _seed_layer(layer: BacktestDataLayer, symbol: str, dates: list[date], closes: list[float]) -> None:
    """Directly insert rows into the cache without calling yfinance."""
    from sqlalchemy.orm import Session
    from src.backtest.cache_models import PriceBarRow

    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    with Session(layer._engine) as session:
        for d, c in zip(dates, closes):
            session.add(PriceBarRow(
                symbol=symbol,
                trade_date=d,
                open=c - 1.0,
                high=c + 1.0,
                low=c - 2.0,
                close=c,
                volume=1_000_000,
                fetched_at=now,
            ))
        session.commit()


# ---------------------------------------------------------------------------
# refresh_symbol
# ---------------------------------------------------------------------------


def test_refresh_symbol_writes_rows(tmp_layer: BacktestDataLayer) -> None:
    """refresh_symbol should fetch from yfinance and write rows to the cache."""
    dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    mock_df = _make_yfinance_df("SPY", dates, [470.0, 472.0, 471.0])

    mock_ticker = MagicMock()
    mock_ticker.history.return_value = mock_df

    with patch("yfinance.Ticker", return_value=mock_ticker):
        rows = tmp_layer.refresh_symbol("SPY", start_date=date(2024, 1, 1))

    assert rows == 3
    closes = tmp_layer.get_close_prices(["SPY"], date(2024, 1, 1), date(2024, 1, 31))
    assert list(closes["SPY"].dropna()) == [470.0, 472.0, 471.0]


def test_refresh_symbol_skips_existing_rows(tmp_layer: BacktestDataLayer) -> None:
    """Without force=True, existing dates are not overwritten."""
    dates = [date(2024, 1, 2), date(2024, 1, 3)]
    _seed_layer(tmp_layer, "SPY", dates, [470.0, 472.0])

    mock_df = _make_yfinance_df("SPY", dates, [999.0, 999.0])
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = mock_df

    with patch("yfinance.Ticker", return_value=mock_ticker):
        rows = tmp_layer.refresh_symbol("SPY")

    assert rows == 0  # nothing new written
    closes = tmp_layer.get_close_prices(["SPY"], date(2024, 1, 1), date(2024, 1, 31))
    assert list(closes["SPY"].dropna()) == [470.0, 472.0]  # originals unchanged


def test_refresh_symbol_force_overwrites(tmp_layer: BacktestDataLayer) -> None:
    """force=True should overwrite existing rows with fresh data."""
    _seed_layer(tmp_layer, "SPY", [date(2024, 1, 2)], [470.0])

    mock_df = _make_yfinance_df("SPY", [date(2024, 1, 2)], [999.0])
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = mock_df

    with patch("yfinance.Ticker", return_value=mock_ticker):
        rows = tmp_layer.refresh_symbol("SPY", force=True)

    assert rows == 1
    closes = tmp_layer.get_close_prices(["SPY"], date(2024, 1, 1), date(2024, 1, 31))
    assert list(closes["SPY"].dropna()) == [999.0]


def test_refresh_symbol_empty_response_returns_zero(tmp_layer: BacktestDataLayer) -> None:
    """An empty yfinance response writes nothing and returns 0."""
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()

    with patch("yfinance.Ticker", return_value=mock_ticker):
        rows = tmp_layer.refresh_symbol("FAKE")

    assert rows == 0


# ---------------------------------------------------------------------------
# get_close_prices
# ---------------------------------------------------------------------------


def test_get_close_prices_returns_correct_range(tmp_layer: BacktestDataLayer) -> None:
    """get_close_prices filters to the requested date range."""
    all_dates = [date(2024, 1, d) for d in range(2, 10)]
    all_closes = [float(400 + i) for i in range(len(all_dates))]
    _seed_layer(tmp_layer, "SPY", all_dates, all_closes)

    result = tmp_layer.get_close_prices(["SPY"], date(2024, 1, 4), date(2024, 1, 6))
    assert list(result["SPY"].dropna()) == [402.0, 403.0, 404.0]


def test_get_close_prices_missing_symbol_is_nan(tmp_layer: BacktestDataLayer) -> None:
    """A symbol not in the cache returns NaN for all dates."""
    _seed_layer(tmp_layer, "SPY", [date(2024, 1, 2)], [470.0])

    result = tmp_layer.get_close_prices(["SPY", "QQQ"], date(2024, 1, 1), date(2024, 1, 31))
    assert "QQQ" in result.columns
    assert result["QQQ"].isna().all()


def test_get_close_prices_multiple_symbols(tmp_layer: BacktestDataLayer) -> None:
    """Returns correct values for multiple symbols simultaneously."""
    _seed_layer(tmp_layer, "SPY", [date(2024, 1, 2)], [470.0])
    _seed_layer(tmp_layer, "QQQ", [date(2024, 1, 2)], [390.0])

    result = tmp_layer.get_close_prices(["SPY", "QQQ"], date(2024, 1, 1), date(2024, 1, 31))
    assert result.loc[result.index[0], "SPY"] == 470.0
    assert result.loc[result.index[0], "QQQ"] == 390.0


# ---------------------------------------------------------------------------
# get_ohlc_bars
# ---------------------------------------------------------------------------


def test_get_ohlc_bars_returns_expected_columns(tmp_layer: BacktestDataLayer) -> None:
    """get_ohlc_bars returns DataFrames with open/high/low/close/volume."""
    _seed_layer(tmp_layer, "SPY", [date(2024, 1, 2), date(2024, 1, 3)], [470.0, 472.0])

    result = tmp_layer.get_ohlc_bars(["SPY"], date(2024, 1, 1), date(2024, 1, 31))
    assert "SPY" in result
    assert set(result["SPY"].columns) >= {"open", "high", "low", "close", "volume"}
    assert len(result["SPY"]) == 2


def test_get_ohlc_bars_empty_for_unknown_symbol(tmp_layer: BacktestDataLayer) -> None:
    """An unknown symbol returns an empty DataFrame, not an error."""
    result = tmp_layer.get_ohlc_bars(["FAKE"], date(2024, 1, 1), date(2024, 1, 31))
    assert "FAKE" in result
    assert result["FAKE"].empty


# ---------------------------------------------------------------------------
# get_available_symbols
# ---------------------------------------------------------------------------


def test_get_available_symbols_filters_by_date(tmp_layer: BacktestDataLayer) -> None:
    """Only symbols with data on or before as_of are returned."""
    _seed_layer(tmp_layer, "SPY", [date(2018, 1, 2)], [270.0])
    _seed_layer(tmp_layer, "XLC", [date(2018, 6, 19)], [50.0])  # XLC launched June 2018

    # Before XLC existed
    before_xlc = tmp_layer.get_available_symbols(date(2018, 3, 1))
    assert "SPY" in before_xlc
    assert "XLC" not in before_xlc

    # After XLC existed
    after_xlc = tmp_layer.get_available_symbols(date(2018, 7, 1))
    assert "SPY" in after_xlc
    assert "XLC" in after_xlc


# ---------------------------------------------------------------------------
# get_cache_version
# ---------------------------------------------------------------------------


def test_get_cache_version_returns_none_when_empty(tmp_layer: BacktestDataLayer) -> None:
    assert tmp_layer.get_cache_version() is None


def test_get_cache_version_returns_latest_fetch(tmp_layer: BacktestDataLayer) -> None:
    """get_cache_version returns the most recent fetched_at timestamp."""
    from sqlalchemy.orm import Session
    from src.backtest.cache_models import PriceBarRow

    early = datetime(2026, 5, 1, 10, 0, 0)
    late = datetime(2026, 5, 5, 15, 0, 0)

    with Session(tmp_layer._engine) as session:
        session.add(PriceBarRow(
            symbol="SPY", trade_date=date(2024, 1, 2),
            open=469.0, high=471.0, low=468.0, close=470.0, volume=1000000,
            fetched_at=early,
        ))
        session.add(PriceBarRow(
            symbol="QQQ", trade_date=date(2024, 1, 2),
            open=389.0, high=391.0, low=388.0, close=390.0, volume=800000,
            fetched_at=late,
        ))
        session.commit()

    assert tmp_layer.get_cache_version() == late


# ---------------------------------------------------------------------------
# BacktestMarketDataView — no-lookahead enforcement
# ---------------------------------------------------------------------------


def test_view_raises_on_lookahead(tmp_layer: BacktestDataLayer) -> None:
    """Querying data beyond the simulation date raises LookaheadError."""
    from datetime import datetime, timezone
    view = BacktestMarketDataView(tmp_layer, as_of=date(2024, 6, 1))

    with pytest.raises(LookaheadError):
        view.get_bars("SPY", 20, datetime(2024, 6, 2, tzinfo=timezone.utc))


def test_view_allows_query_on_simulation_date(tmp_layer: BacktestDataLayer) -> None:
    """Querying exactly the simulation date is allowed."""
    from datetime import datetime, timezone
    dates = [date(2024, 5, d) for d in range(1, 22)]
    _seed_layer(tmp_layer, "SPY", dates, [float(470 + i) for i in range(len(dates))])

    view = BacktestMarketDataView(tmp_layer, as_of=date(2024, 5, 21))
    df = view.get_bars("SPY", 20, datetime(2024, 5, 21, tzinfo=timezone.utc))
    assert not df.empty
    assert len(df) <= 20


def test_view_get_latest_price_returns_decimal(tmp_layer: BacktestDataLayer) -> None:
    """get_latest_price returns a Decimal of the most recent adjusted close."""
    _seed_layer(tmp_layer, "SPY", [date(2024, 5, 20), date(2024, 5, 21)], [470.0, 473.5])

    view = BacktestMarketDataView(tmp_layer, as_of=date(2024, 5, 21))
    price = view.get_latest_price("SPY")
    assert isinstance(price, Decimal)
    assert price == Decimal("473.5")


def test_view_get_latest_price_uses_most_recent_before_simulation_date(
    tmp_layer: BacktestDataLayer,
) -> None:
    """get_latest_price returns the last available close, not a future one."""
    _seed_layer(tmp_layer, "SPY", [date(2024, 5, 20), date(2024, 5, 22)], [470.0, 480.0])

    # Simulation date is the 21st — only the 20th bar should be visible
    view = BacktestMarketDataView(tmp_layer, as_of=date(2024, 5, 21))
    price = view.get_latest_price("SPY")
    assert price == Decimal("470.0")


def test_view_get_latest_price_raises_when_no_data(tmp_layer: BacktestDataLayer) -> None:
    """get_latest_price raises ValueError if no data exists for the symbol."""
    view = BacktestMarketDataView(tmp_layer, as_of=date(2024, 5, 21))
    with pytest.raises(ValueError, match="No price data"):
        view.get_latest_price("FAKE")
