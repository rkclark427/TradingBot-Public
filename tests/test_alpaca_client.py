"""Tests for src/data/alpaca_client.py.

All tests mock at the SDK object layer — no real network calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pandas as pd
import pytest

from src.data.alpaca_client import (
    AccountInfo,
    AssetInfo,
    ClockInfo,
    OrderInfo,
    PaperClient,
    PositionInfo,
    _with_retry,
)

# ---------------------------------------------------------------------------
# Helpers to build fake SDK objects
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _dt(year: int = 2024, month: int = 1, day: int = 2) -> datetime:
    return datetime(year, month, day, 12, 0, 0, tzinfo=UTC)


def _fake_account(**overrides: object) -> SimpleNamespace:
    defaults: dict = {
        "buying_power": "50000.00",
        "cash": "25000.00",
        "portfolio_value": "75000.00",
        "pattern_day_trader": False,
        "trading_blocked": False,
        "shorting_enabled": True,
        "options_approved_level": 2,
        "fractional_trading": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _fake_asset(**overrides: object) -> SimpleNamespace:
    defaults: dict = {
        "symbol": "AAPL",
        "tradable": True,
        "fractionable": True,
        "marginable": True,
        "shortable": True,
        "easy_to_borrow": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _fake_position(**overrides: object) -> SimpleNamespace:
    side = SimpleNamespace(value="long")
    defaults: dict = {
        "symbol": "AAPL",
        "qty": "10.5",
        "avg_entry_price": "150.00",
        "market_value": "1600.00",
        "unrealized_pl": "25.00",
        "side": side,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _fake_order(**overrides: object) -> SimpleNamespace:
    side = SimpleNamespace(value="buy")
    order_type = SimpleNamespace(value="limit")
    status = SimpleNamespace(value="filled")
    defaults: dict = {
        "id": "order-uuid-1",
        "client_order_id": "client-order-1",
        "symbol": "AAPL",
        "qty": "5",
        "filled_qty": "5",
        "side": side,
        "order_type": order_type,
        "status": status,
        "limit_price": "152.00",
        "filled_avg_price": "151.80",
        "submitted_at": _dt(2024, 1, 2),
        "filled_at": _dt(2024, 1, 2),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _fake_clock(**overrides: object) -> SimpleNamespace:
    defaults: dict = {
        "is_open": True,
        "next_open": _dt(2024, 1, 3),
        "next_close": _dt(2024, 1, 2),
        "timestamp": _dt(2024, 1, 2),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _fake_bar(ts: datetime, o: float, h: float, lo: float, c: float, v: int) -> SimpleNamespace:
    return SimpleNamespace(timestamp=ts, open=o, high=h, low=lo, close=c, volume=v)


def _fake_bars_response(symbol: str, bars: list) -> SimpleNamespace:
    return SimpleNamespace(data={symbol: bars})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def paper_client() -> PaperClient:
    """Return a PaperClient with both underlying SDK clients mocked out."""
    with (
        patch("src.data.alpaca_client.TradingClient") as mock_trading_cls,
        patch("src.data.alpaca_client.StockHistoricalDataClient") as mock_data_cls,
    ):
        mock_trading = MagicMock()
        mock_data = MagicMock()
        mock_trading_cls.return_value = mock_trading
        mock_data_cls.return_value = mock_data

        client = PaperClient(api_key="test-key", api_secret="test-secret")
        # Expose mocks for test manipulation.
        client._trading = mock_trading  # type: ignore[attr-defined]
        client._data = mock_data  # type: ignore[attr-defined]
        yield client


# ---------------------------------------------------------------------------
# get_account
# ---------------------------------------------------------------------------


class TestGetAccount:
    def test_returns_account_info_dataclass(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_account.return_value = _fake_account()

        result = paper_client.get_account()

        assert isinstance(result, AccountInfo)

    def test_fields_match_sdk_response(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_account.return_value = _fake_account()

        result = paper_client.get_account()

        assert result.buying_power == Decimal("50000.00")
        assert result.cash == Decimal("25000.00")
        assert result.portfolio_value == Decimal("75000.00")
        assert result.pattern_day_trader is False
        assert result.trading_blocked is False
        assert result.shorting_enabled is True
        assert result.options_trading_level == 2
        assert result.fractional_trading is True

    def test_options_level_defaults_to_zero_when_none(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_account.return_value = _fake_account(
            options_approved_level=None
        )

        result = paper_client.get_account()

        assert result.options_trading_level == 0

    def test_fractional_trading_defaults_to_true_when_attribute_missing(
        self, paper_client: PaperClient
    ) -> None:
        acct = _fake_account()
        del acct.fractional_trading  # simulate attribute not present

        paper_client._trading.get_account.return_value = acct

        result = paper_client.get_account()

        assert result.fractional_trading is True

    def test_money_fields_are_decimal_not_float(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_account.return_value = _fake_account()

        result = paper_client.get_account()

        assert isinstance(result.buying_power, Decimal)
        assert isinstance(result.cash, Decimal)
        assert isinstance(result.portfolio_value, Decimal)


# ---------------------------------------------------------------------------
# get_assets
# ---------------------------------------------------------------------------


class TestGetAssets:
    def test_returns_list_of_asset_info(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_all_assets.return_value = [
            _fake_asset(symbol="AAPL"),
            _fake_asset(symbol="MSFT", fractionable=False),
        ]

        result = paper_client.get_assets()

        assert len(result) == 2
        assert all(isinstance(a, AssetInfo) for a in result)

    def test_asset_fields_are_correct(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_all_assets.return_value = [
            _fake_asset(
                symbol="TSLA",
                tradable=True,
                fractionable=True,
                marginable=False,
                shortable=True,
                easy_to_borrow=False,
            )
        ]

        result = paper_client.get_assets()

        a = result[0]
        assert a.symbol == "TSLA"
        assert a.tradable is True
        assert a.fractionable is True
        assert a.marginable is False
        assert a.shortable is True
        assert a.easy_to_borrow is False

    def test_empty_list_when_no_assets(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_all_assets.return_value = []

        result = paper_client.get_assets()

        assert result == []


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------


class TestGetPositions:
    def test_returns_list_of_position_info(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_all_positions.return_value = [
            _fake_position(symbol="AAPL"),
            _fake_position(symbol="SPY"),
        ]

        result = paper_client.get_positions()

        assert len(result) == 2
        assert all(isinstance(p, PositionInfo) for p in result)

    def test_position_fields_are_correct(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_all_positions.return_value = [
            _fake_position(
                symbol="AAPL",
                qty="10.5",
                avg_entry_price="150.00",
                market_value="1600.00",
                unrealized_pl="25.00",
            )
        ]

        result = paper_client.get_positions()

        p = result[0]
        assert p.symbol == "AAPL"
        assert p.qty == Decimal("10.5")
        assert p.avg_entry_price == Decimal("150.00")
        assert p.market_value == Decimal("1600.00")
        assert p.unrealized_pl == Decimal("25.00")
        assert p.side == "long"

    def test_money_fields_are_decimal(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_all_positions.return_value = [_fake_position()]

        result = paper_client.get_positions()

        p = result[0]
        assert isinstance(p.qty, Decimal)
        assert isinstance(p.avg_entry_price, Decimal)
        assert isinstance(p.market_value, Decimal)
        assert isinstance(p.unrealized_pl, Decimal)

    def test_empty_list_when_no_positions(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_all_positions.return_value = []

        result = paper_client.get_positions()

        assert result == []


# ---------------------------------------------------------------------------
# get_orders
# ---------------------------------------------------------------------------


class TestGetOrders:
    def test_returns_list_of_order_info(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_orders.return_value = [
            _fake_order(),
            _fake_order(id="order-uuid-2", client_order_id="client-order-2"),
        ]

        result = paper_client.get_orders()

        assert len(result) == 2
        assert all(isinstance(o, OrderInfo) for o in result)

    def test_order_fields_are_correct(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_orders.return_value = [_fake_order()]

        result = paper_client.get_orders()

        o = result[0]
        assert o.id == "order-uuid-1"
        assert o.client_order_id == "client-order-1"
        assert o.symbol == "AAPL"
        assert o.qty == Decimal("5")
        assert o.filled_qty == Decimal("5")
        assert o.side == "buy"
        assert o.order_type == "limit"
        assert o.status == "filled"
        assert o.limit_price == Decimal("152.00")
        assert o.filled_avg_price == Decimal("151.80")

    def test_order_datetimes_are_utc_aware(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_orders.return_value = [_fake_order()]

        result = paper_client.get_orders()

        o = result[0]
        assert o.submitted_at.tzinfo is not None
        assert o.filled_at is not None
        assert o.filled_at.tzinfo is not None

    def test_unfilled_order_has_none_fields(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_orders.return_value = [
            _fake_order(
                filled_qty="0",
                limit_price=None,
                filled_avg_price=None,
                filled_at=None,
                status=SimpleNamespace(value="new"),
            )
        ]

        result = paper_client.get_orders()

        o = result[0]
        assert o.limit_price is None
        assert o.filled_avg_price is None
        assert o.filled_at is None


# ---------------------------------------------------------------------------
# submit_order
# ---------------------------------------------------------------------------


class TestSubmitOrder:
    def test_calls_sdk_with_limit_order_request(self, paper_client: PaperClient) -> None:
        paper_client._trading.submit_order.return_value = _fake_order()

        result = paper_client.submit_order(
            symbol="AAPL",
            qty=Decimal("5"),
            side="buy",
            limit_price=Decimal("152.00"),
            client_order_id="client-order-1",
        )

        assert isinstance(result, OrderInfo)
        paper_client._trading.submit_order.assert_called_once()

    def test_returns_order_info(self, paper_client: PaperClient) -> None:
        paper_client._trading.submit_order.return_value = _fake_order(
            id="new-order-id",
            client_order_id="my-order",
            symbol="SPY",
        )

        result = paper_client.submit_order(
            symbol="SPY",
            qty=Decimal("2"),
            side="buy",
            limit_price=Decimal("450.00"),
            client_order_id="my-order",
        )

        assert result.id == "new-order-id"
        assert result.client_order_id == "my-order"
        assert result.symbol == "SPY"

    def test_sell_side_passes_through(self, paper_client: PaperClient) -> None:
        sell_order = _fake_order(side=SimpleNamespace(value="sell"))
        paper_client._trading.submit_order.return_value = sell_order

        result = paper_client.submit_order(
            symbol="AAPL",
            qty=Decimal("3"),
            side="sell",
            limit_price=Decimal("155.00"),
            client_order_id="sell-order-1",
        )

        assert result.side == "sell"


# ---------------------------------------------------------------------------
# get_bars
# ---------------------------------------------------------------------------


class TestGetBars:
    def test_returns_dataframe(self, paper_client: PaperClient) -> None:
        from alpaca.data.timeframe import TimeFrame

        bars = [
            _fake_bar(_dt(2024, 1, 2), 150.0, 155.0, 149.0, 153.0, 1_000_000),
            _fake_bar(_dt(2024, 1, 3), 153.0, 157.0, 152.0, 156.0, 1_200_000),
        ]
        paper_client._data.get_stock_bars.return_value = _fake_bars_response("AAPL", bars)

        result = paper_client.get_bars(
            symbol="AAPL",
            timeframe=TimeFrame.Day,
            start=_dt(2024, 1, 2),
            end=_dt(2024, 1, 3),
        )

        assert isinstance(result, pd.DataFrame)

    def test_dataframe_has_correct_columns(self, paper_client: PaperClient) -> None:
        from alpaca.data.timeframe import TimeFrame

        bars = [_fake_bar(_dt(2024, 1, 2), 150.0, 155.0, 149.0, 153.0, 1_000_000)]
        paper_client._data.get_stock_bars.return_value = _fake_bars_response("AAPL", bars)

        result = paper_client.get_bars(
            symbol="AAPL",
            timeframe=TimeFrame.Day,
            start=_dt(2024, 1, 2),
            end=_dt(2024, 1, 2),
        )

        for col in ("open", "high", "low", "close", "volume"):
            assert col in result.columns

    def test_dataframe_index_is_timestamp(self, paper_client: PaperClient) -> None:
        from alpaca.data.timeframe import TimeFrame

        bars = [_fake_bar(_dt(2024, 1, 2), 150.0, 155.0, 149.0, 153.0, 1_000_000)]
        paper_client._data.get_stock_bars.return_value = _fake_bars_response("AAPL", bars)

        result = paper_client.get_bars(
            symbol="AAPL",
            timeframe=TimeFrame.Day,
            start=_dt(2024, 1, 2),
            end=_dt(2024, 1, 2),
        )

        assert isinstance(result.index, pd.DatetimeIndex)

    def test_empty_dataframe_when_no_bars(self, paper_client: PaperClient) -> None:
        from alpaca.data.timeframe import TimeFrame

        paper_client._data.get_stock_bars.return_value = _fake_bars_response("AAPL", [])

        result = paper_client.get_bars(
            symbol="AAPL",
            timeframe=TimeFrame.Day,
            start=_dt(2024, 1, 2),
            end=_dt(2024, 1, 2),
        )

        assert len(result) == 0

    def test_bar_values_correct(self, paper_client: PaperClient) -> None:
        from alpaca.data.timeframe import TimeFrame

        bars = [_fake_bar(_dt(2024, 1, 2), 150.0, 155.0, 149.0, 153.0, 1_000_000)]
        paper_client._data.get_stock_bars.return_value = _fake_bars_response("AAPL", bars)

        result = paper_client.get_bars(
            symbol="AAPL",
            timeframe=TimeFrame.Day,
            start=_dt(2024, 1, 2),
            end=_dt(2024, 1, 2),
        )

        row = result.iloc[0]
        assert row["open"] == 150.0
        assert row["high"] == 155.0
        assert row["low"] == 149.0
        assert row["close"] == 153.0
        assert row["volume"] == 1_000_000


# ---------------------------------------------------------------------------
# get_clock
# ---------------------------------------------------------------------------


class TestGetClock:
    def test_returns_clock_info(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_clock.return_value = _fake_clock()

        result = paper_client.get_clock()

        assert isinstance(result, ClockInfo)

    def test_clock_fields_are_correct(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_clock.return_value = _fake_clock(is_open=True)

        result = paper_client.get_clock()

        assert result.is_open is True

    def test_clock_datetimes_are_utc_aware(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_clock.return_value = _fake_clock()

        result = paper_client.get_clock()

        assert result.next_open.tzinfo is not None
        assert result.next_close.tzinfo is not None
        assert result.timestamp.tzinfo is not None

    def test_clock_next_open_next_close_are_datetime(self, paper_client: PaperClient) -> None:
        paper_client._trading.get_clock.return_value = _fake_clock()

        result = paper_client.get_clock()

        assert isinstance(result.next_open, datetime)
        assert isinstance(result.next_close, datetime)


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


class TestRetryLogic:
    def test_retries_on_429_then_succeeds(self, paper_client: PaperClient) -> None:
        """A 429-style error on first call should trigger a retry; second call succeeds."""
        # Build a fake exception that looks like a 429.
        rate_limit_exc = Exception("429 Too Many Requests")
        rate_limit_exc.status_code = 429  # type: ignore[attr-defined]

        call_count = 0

        def flaky_call() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise rate_limit_exc
            return "success"

        with patch("src.data.alpaca_client.time.sleep"):
            result = _with_retry(flaky_call)

        assert result == "success"
        assert call_count == 2

    def test_retries_on_503_then_succeeds(self) -> None:
        service_unavail_exc = Exception("503 Service Unavailable")
        service_unavail_exc.status_code = 503  # type: ignore[attr-defined]

        call_count = 0

        def flaky_call() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise service_unavail_exc
            return "ok"

        with patch("src.data.alpaca_client.time.sleep"):
            result = _with_retry(flaky_call)

        assert result == "ok"
        assert call_count == 3

    def test_does_not_retry_4xx_client_error(self) -> None:
        """A 400 Bad Request should propagate immediately, no retries."""
        bad_request_exc = Exception("400 Bad Request")
        bad_request_exc.status_code = 400  # type: ignore[attr-defined]

        call_count = 0

        def bad_call() -> None:
            nonlocal call_count
            call_count += 1
            raise bad_request_exc

        with pytest.raises(Exception, match="400 Bad Request"):
            with patch("src.data.alpaca_client.time.sleep"):
                _with_retry(bad_call)

        assert call_count == 1

    def test_raises_after_all_retries_exhausted(self) -> None:
        """If all 3 attempts fail, the last exception propagates."""
        rate_limit_exc = Exception("429 Too Many Requests")
        rate_limit_exc.status_code = 429  # type: ignore[attr-defined]

        call_count = 0

        def always_fail() -> None:
            nonlocal call_count
            call_count += 1
            raise rate_limit_exc

        with pytest.raises(Exception, match="429"):
            with patch("src.data.alpaca_client.time.sleep"):
                _with_retry(always_fail)

        assert call_count == 3

    def test_retry_uses_exponential_backoff_delays(self) -> None:
        """Verify sleep is called with 1s, 2s on two failures then success."""
        rate_limit_exc = Exception("503 Service Unavailable")
        rate_limit_exc.status_code = 503  # type: ignore[attr-defined]

        call_count = 0

        def two_failures() -> str:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise rate_limit_exc
            return "done"

        with patch("src.data.alpaca_client.time.sleep") as mock_sleep:
            result = _with_retry(two_failures)

        assert result == "done"
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    def test_retries_on_connection_error(self) -> None:
        """httpx.ConnectError is a transient error and should trigger retry."""
        call_count = 0

        def flaky_call() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("Connection refused")
            return "connected"

        with patch("src.data.alpaca_client.time.sleep"):
            result = _with_retry(flaky_call)

        assert result == "connected"
        assert call_count == 2

    def test_get_account_retries_on_transient_error(self, paper_client: PaperClient) -> None:
        """End-to-end: get_account retries when the SDK raises a 429."""
        rate_limit_exc = Exception("429")
        rate_limit_exc.status_code = 429  # type: ignore[attr-defined]

        call_count = 0

        def side_effect() -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise rate_limit_exc
            return _fake_account()

        paper_client._trading.get_account.side_effect = side_effect

        with patch("src.data.alpaca_client.time.sleep"):
            result = paper_client.get_account()

        assert isinstance(result, AccountInfo)
        assert call_count == 2
