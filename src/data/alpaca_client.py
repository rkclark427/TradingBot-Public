"""Alpaca broker client wrappers for paper and live trading.

Thin wrappers around alpaca-py that:
- Return plain dataclasses rather than SDK types
- Enforce Decimal for money, timezone-aware datetimes
- Retry transient errors (429, 503, connection errors) with exponential backoff
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import pandas as pd

from alpaca.trading import TradingClient
from alpaca.trading.requests import (
    GetAssetsRequest,
    GetOrdersRequest,
    LimitOrderRequest,
)
from alpaca.trading.enums import (
    AssetClass,
    AssetStatus,
    OrderSide,
    TimeInForce,
)
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Return dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AccountInfo:
    buying_power: Decimal
    cash: Decimal
    portfolio_value: Decimal
    pattern_day_trader: bool
    trading_blocked: bool
    shorting_enabled: bool
    options_trading_level: int  # 0 = none
    fractional_trading: bool  # True if account supports fractional shares


@dataclass
class AssetInfo:
    symbol: str
    tradable: bool
    fractionable: bool
    marginable: bool
    shortable: bool
    easy_to_borrow: bool


@dataclass
class PositionInfo:
    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    market_value: Decimal
    unrealized_pl: Decimal
    side: str  # "long" or "short"


@dataclass
class OrderInfo:
    id: str
    client_order_id: str
    symbol: str
    qty: Decimal
    filled_qty: Decimal
    side: str
    order_type: str
    status: str
    limit_price: Decimal | None
    filled_avg_price: Decimal | None
    submitted_at: datetime
    filled_at: datetime | None


@dataclass
class ClockInfo:
    is_open: bool
    next_open: datetime
    next_close: datetime
    timestamp: datetime


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

#: Exception types considered transient — safe to retry.
_TRANSIENT_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)

#: HTTP status codes that warrant a retry.
_TRANSIENT_STATUS_CODES = {429, 503}


def _with_retry(fn: Any) -> Any:
    """Call *fn()* with exponential backoff on transient errors.

    Retries up to 3 times with 1s / 2s / 4s delays.
    Raises immediately on 4xx client errors (except 429).
    """
    delays = [1, 2, 4]
    last_exc: BaseException | None = None

    for attempt, delay in enumerate(delays, start=1):
        try:
            return fn()
        except _TRANSIENT_EXCEPTIONS as exc:
            last_exc = exc
            logger.warning(
                "Transient connection error (attempt %d/3), retrying in %ds: %s",
                attempt,
                delay,
                exc,
            )
            time.sleep(delay)
        except Exception as exc:  # noqa: BLE001
            # Check whether the SDK has embedded an HTTP status we should handle.
            status = _extract_status_code(exc)
            if status in _TRANSIENT_STATUS_CODES:
                last_exc = exc
                logger.warning(
                    "HTTP %d (attempt %d/3), retrying in %ds: %s",
                    status,
                    attempt,
                    delay,
                    exc,
                )
                time.sleep(delay)
            else:
                # Not transient — propagate immediately.
                raise

    # All retries exhausted.
    raise last_exc  # type: ignore[misc]


def _extract_status_code(exc: Exception) -> int | None:
    """Best-effort extraction of an HTTP status code from an alpaca-py exception."""
    # alpaca-py raises APIError with a status_code attribute on some versions.
    if hasattr(exc, "status_code"):
        return exc.status_code  # type: ignore[attr-defined]
    # Some versions embed it in the args or message; fall back to string scan.
    msg = str(exc)
    for code in _TRANSIENT_STATUS_CODES:
        if str(code) in msg:
            return code
    return None


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal:
    """Convert a value to Decimal, handling None → 0."""
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _to_utc_aware(dt: Any) -> datetime:
    """Ensure *dt* is a timezone-aware UTC datetime."""
    if dt is None:
        raise ValueError("Expected a datetime, got None")
    if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
        import zoneinfo

        return dt.astimezone(zoneinfo.ZoneInfo("UTC"))
    # Naïve datetime — assume UTC.
    from datetime import timezone

    return dt.replace(tzinfo=timezone.utc)


def _to_utc_aware_or_none(dt: Any) -> datetime | None:
    if dt is None:
        return None
    return _to_utc_aware(dt)


def _account_to_info(acct: Any) -> AccountInfo:
    # fractional_trading: prefer explicit attribute; fall back to True for paper.
    fractional = getattr(acct, "fractional_trading", None)
    if fractional is None:
        # Alpaca paper accounts support fractional by default.
        fractional = True
    else:
        fractional = bool(fractional)

    options_level_raw = getattr(acct, "options_approved_level", None)
    options_level = int(options_level_raw) if options_level_raw is not None else 0

    return AccountInfo(
        buying_power=_decimal(acct.buying_power),
        cash=_decimal(acct.cash),
        portfolio_value=_decimal(acct.portfolio_value),
        pattern_day_trader=bool(acct.pattern_day_trader),
        trading_blocked=bool(acct.trading_blocked),
        shorting_enabled=bool(acct.shorting_enabled),
        options_trading_level=options_level,
        fractional_trading=fractional,
    )


def _asset_to_info(asset: Any) -> AssetInfo:
    return AssetInfo(
        symbol=str(asset.symbol),
        tradable=bool(asset.tradable),
        fractionable=bool(asset.fractionable),
        marginable=bool(asset.marginable),
        shortable=bool(asset.shortable),
        easy_to_borrow=bool(asset.easy_to_borrow),
    )


def _position_to_info(pos: Any) -> PositionInfo:
    return PositionInfo(
        symbol=str(pos.symbol),
        qty=_decimal(pos.qty),
        avg_entry_price=_decimal(pos.avg_entry_price),
        market_value=_decimal(pos.market_value),
        unrealized_pl=_decimal(pos.unrealized_pl),
        side=str(pos.side.value) if hasattr(pos.side, "value") else str(pos.side),
    )


def _order_to_info(order: Any) -> OrderInfo:
    return OrderInfo(
        id=str(order.id),
        client_order_id=str(order.client_order_id),
        symbol=str(order.symbol),
        qty=_decimal(order.qty),
        filled_qty=_decimal(order.filled_qty),
        side=str(order.side.value) if hasattr(order.side, "value") else str(order.side),
        order_type=(
            str(order.order_type.value)
            if hasattr(order.order_type, "value")
            else str(order.order_type)
        ),
        status=(
            str(order.status.value) if hasattr(order.status, "value") else str(order.status)
        ),
        limit_price=_decimal_or_none(order.limit_price),
        filled_avg_price=_decimal_or_none(order.filled_avg_price),
        submitted_at=_to_utc_aware(order.submitted_at),
        filled_at=_to_utc_aware_or_none(order.filled_at),
    )


def _bars_to_dataframe(bars_response: Any, symbol: str) -> pd.DataFrame:
    """Convert alpaca-py bar data to a DataFrame indexed by timestamp."""
    if bars_response is None or not bars_response.data:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    symbol_bars = bars_response.data.get(symbol, [])
    if not symbol_bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    rows = []
    for bar in symbol_bars:
        rows.append(
            {
                "timestamp": _to_utc_aware(bar.timestamp),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
            }
        )

    df = pd.DataFrame(rows)
    df = df.set_index("timestamp")
    df.index = pd.DatetimeIndex(df.index, tz="UTC")
    return df


# ---------------------------------------------------------------------------
# Client base (shared logic)
# ---------------------------------------------------------------------------


class _BaseClient:
    """Shared implementation — don't instantiate directly."""

    def __init__(self, api_key: str, api_secret: str, paper: bool) -> None:
        self._trading = TradingClient(
            api_key=api_key,
            secret_key=api_secret,
            paper=paper,
        )
        # Market data doesn't have a paper/live split in alpaca-py.
        self._data = StockHistoricalDataClient(
            api_key=api_key,
            secret_key=api_secret,
        )

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account(self) -> AccountInfo:
        acct = _with_retry(lambda: self._trading.get_account())
        return _account_to_info(acct)

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    def get_assets(self, asset_class: str = "us_equity") -> list[AssetInfo]:
        asset_class_enum = AssetClass(asset_class)
        req = GetAssetsRequest(
            asset_class=asset_class_enum,
            status=AssetStatus.ACTIVE,
        )
        assets = _with_retry(lambda: self._trading.get_all_assets(req))
        return [_asset_to_info(a) for a in assets]

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> list[PositionInfo]:
        positions = _with_retry(lambda: self._trading.get_all_positions())
        return [_position_to_info(p) for p in positions]

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def get_orders(self, status: str = "all", limit: int = 100) -> list[OrderInfo]:
        from alpaca.trading.enums import QueryOrderStatus

        req = GetOrdersRequest(
            status=QueryOrderStatus(status),
            limit=limit,
        )
        orders = _with_retry(lambda: self._trading.get_orders(req))
        return [_order_to_info(o) for o in orders]

    # ------------------------------------------------------------------
    # Submit order
    # ------------------------------------------------------------------

    def submit_order(
        self,
        symbol: str,
        qty: Decimal,
        side: str,
        limit_price: Decimal,
        client_order_id: str,
    ) -> OrderInfo:
        order_side = OrderSide(side)
        req = LimitOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=order_side,
            time_in_force=TimeInForce.DAY,
            limit_price=float(limit_price),
            client_order_id=client_order_id,
        )
        order = _with_retry(lambda: self._trading.submit_order(req))
        return _order_to_info(order)

    # ------------------------------------------------------------------
    # Historical bars
    # ------------------------------------------------------------------

    def get_bars(
        self,
        symbol: str,
        timeframe: TimeFrame,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
        )
        bars = _with_retry(lambda: self._data.get_stock_bars(req))
        return _bars_to_dataframe(bars, symbol)

    # ------------------------------------------------------------------
    # Market clock
    # ------------------------------------------------------------------

    def get_clock(self) -> ClockInfo:
        clock = _with_retry(lambda: self._trading.get_clock())
        return ClockInfo(
            is_open=bool(clock.is_open),
            next_open=_to_utc_aware(clock.next_open),
            next_close=_to_utc_aware(clock.next_close),
            timestamp=_to_utc_aware(clock.timestamp),
        )


# ---------------------------------------------------------------------------
# Public client classes
# ---------------------------------------------------------------------------


class PaperClient(_BaseClient):
    """Alpaca paper-trading client (simulated fills, real market data)."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        super().__init__(api_key=api_key, api_secret=api_secret, paper=True)


class LiveClient(_BaseClient):
    """Alpaca live-trading client (real money)."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        super().__init__(api_key=api_key, api_secret=api_secret, paper=False)
