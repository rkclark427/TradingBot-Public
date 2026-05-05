"""Backtest data layer: yfinance fetch, local SQLite cache, no-lookahead view.

Usage pattern:
    layer = BacktestDataLayer(Path("data/backtest_cache.db"))
    layer.refresh_symbol("SPY", start_date=date(2018, 1, 1))
    closes = layer.get_close_prices(["SPY", "QQQ"], date(2020, 1, 1), date(2024, 12, 31))

The BacktestMarketDataView wraps the layer and enforces no-lookahead — any
attempt to query data beyond the view's as_of date raises LookaheadError.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from src.backtest.cache_models import Base, PriceBarRow

logger = logging.getLogger(__name__)


class LookaheadError(Exception):
    """Raised when a strategy attempts to access data beyond the current simulation date."""


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------


class BacktestDataLayer:
    """Manages a local SQLite cache of historical OHLC price data.

    The cache is the single source of truth during backtest runs — yfinance is
    only called on cache miss or explicit refresh. This ensures reproducibility:
    two runs against the same cache produce identical results.
    """

    def __init__(self, cache_path: Path) -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{cache_path}")
        Base.metadata.create_all(self._engine)

    # ------------------------------------------------------------------
    # Cache population
    # ------------------------------------------------------------------

    def refresh_symbol(
        self,
        symbol: str,
        start_date: date | None = None,
        force: bool = False,
    ) -> int:
        """Fetch price history for *symbol* from yfinance and write to cache.

        Args:
            symbol: Ticker symbol (e.g. "SPY").
            start_date: Earliest date to fetch. Defaults to 2010-01-01 to
                ensure sufficient lookback for any strategy using 20-60 day
                windows on data starting from 2018.
            force: If True, overwrite existing rows. If False (default), skip
                dates already present in the cache.

        Returns:
            Number of rows written.
        """
        import yfinance as yf

        fetch_start = start_date or date(2010, 1, 1)
        now = datetime.now(tz=timezone.utc)

        logger.info("Fetching %s from yfinance (start=%s, force=%s)", symbol, fetch_start, force)
        ticker = yf.Ticker(symbol)
        df = ticker.history(
            start=fetch_start.isoformat(),
            auto_adjust=True,
            actions=False,
        )

        if df.empty:
            logger.warning("yfinance returned no data for %s", symbol)
            return 0

        # yfinance 1.x returns a tz-aware DatetimeIndex. Strip tz, normalise to
        # midnight, then convert to plain date objects for SQLite storage.
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize().date  # type: ignore[assignment]

        rows_written = 0
        with Session(self._engine) as session:
            existing: set[date] = set()
            if not force:
                rows = session.execute(
                    select(PriceBarRow.trade_date).where(PriceBarRow.symbol == symbol)
                ).scalars().all()
                existing = set(rows)

            for trade_date, row in df.iterrows():
                if not force and trade_date in existing:
                    continue
                if force:
                    # Delete existing row before re-inserting
                    session.query(PriceBarRow).filter(
                        PriceBarRow.symbol == symbol,
                        PriceBarRow.trade_date == trade_date,
                    ).delete()

                bar = PriceBarRow(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row.get("Volume", 0)),
                    fetched_at=now,
                )
                session.add(bar)
                rows_written += 1

            session.commit()

        logger.info("Wrote %d rows for %s", rows_written, symbol)
        return rows_written

    def refresh_all(
        self,
        symbols: list[str],
        start_date: date | None = None,
        force: bool = False,
    ) -> dict[str, int]:
        """Refresh a list of symbols. Returns {symbol: rows_written}."""
        results: dict[str, int] = {}
        for symbol in symbols:
            try:
                results[symbol] = self.refresh_symbol(symbol, start_date=start_date, force=force)
            except Exception:
                logger.exception("Failed to refresh %s", symbol)
                results[symbol] = 0
        return results

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    def get_close_prices(
        self,
        symbols: list[str],
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Return a DataFrame of adjusted close prices.

        Returns:
            DataFrame indexed by trade_date, one column per symbol.
            Missing dates (holidays, before symbol existed) are NaN.
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(PriceBarRow.trade_date, PriceBarRow.symbol, PriceBarRow.close)
                .where(
                    PriceBarRow.symbol.in_(symbols),
                    PriceBarRow.trade_date >= start_date,
                    PriceBarRow.trade_date <= end_date,
                )
                .order_by(PriceBarRow.trade_date)
            ).all()

        if not rows:
            return pd.DataFrame(index=pd.DatetimeIndex([]), columns=symbols)

        df = pd.DataFrame(rows, columns=["trade_date", "symbol", "close"])
        pivot = df.pivot(index="trade_date", columns="symbol", values="close")
        pivot.index = pd.to_datetime(pivot.index)
        # Ensure all requested symbols are present as columns (NaN if no data)
        for sym in symbols:
            if sym not in pivot.columns:
                pivot[sym] = float("nan")
        return pivot[symbols]

    def get_ohlc_bars(
        self,
        symbols: list[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, pd.DataFrame]:
        """Return OHLCV bars as a dict of DataFrames, one per symbol.

        Each DataFrame is indexed by trade_date with columns: open, high, low,
        close, volume.
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(
                    PriceBarRow.trade_date,
                    PriceBarRow.symbol,
                    PriceBarRow.open,
                    PriceBarRow.high,
                    PriceBarRow.low,
                    PriceBarRow.close,
                    PriceBarRow.volume,
                )
                .where(
                    PriceBarRow.symbol.in_(symbols),
                    PriceBarRow.trade_date >= start_date,
                    PriceBarRow.trade_date <= end_date,
                )
                .order_by(PriceBarRow.trade_date)
            ).all()

        result: dict[str, pd.DataFrame] = {}
        if not rows:
            empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            return {sym: empty for sym in symbols}

        df = pd.DataFrame(rows, columns=["trade_date", "symbol", "open", "high", "low", "close", "volume"])
        for sym in symbols:
            sym_df = df[df["symbol"] == sym].drop(columns="symbol").set_index("trade_date")
            sym_df.index = pd.to_datetime(sym_df.index)
            result[sym] = sym_df

        return result

    def get_available_symbols(self, as_of: date) -> list[str]:
        """Return symbols that have at least one bar on or before *as_of*."""
        with Session(self._engine) as session:
            rows = session.execute(
                select(PriceBarRow.symbol)
                .where(PriceBarRow.trade_date <= as_of)
                .distinct()
            ).scalars().all()
        return sorted(rows)

    def get_cache_version(self) -> datetime | None:
        """Return the latest fetch timestamp across all rows, or None if cache is empty."""
        with Session(self._engine) as session:
            result = session.execute(
                select(func.max(PriceBarRow.fetched_at))
            ).scalar()
        return result


# ---------------------------------------------------------------------------
# No-lookahead market data view (for simulation engine)
# ---------------------------------------------------------------------------


class BacktestMarketDataView:
    """Implements the MarketDataView protocol backed by the local price cache.

    Enforces no-lookahead: any query for data beyond self._as_of raises
    LookaheadError. This guarantees that a strategy behaves identically
    whether it is running live or in backtest.
    """

    def __init__(
        self,
        data_layer: BacktestDataLayer,
        as_of: date,
    ) -> None:
        self._data = data_layer
        self._as_of = as_of

    def _check_lookahead(self, requested: date) -> None:
        if requested > self._as_of:
            raise LookaheadError(
                f"Strategy requested data for {requested}, but simulation date is {self._as_of}"
            )

    def get_bars(
        self,
        symbol: str,
        lookback_days: int,
        as_of: datetime,
    ) -> Any:
        """Return a DataFrame of daily bars for *symbol* ending on *as_of*.

        Raises LookaheadError if as_of is after the view's simulation date.
        """
        as_of_date = as_of.date() if hasattr(as_of, "date") else as_of  # type: ignore[union-attr]
        self._check_lookahead(as_of_date)

        # Fetch more calendar days than lookback_days to account for weekends/holidays
        from datetime import timedelta
        start = as_of_date - timedelta(days=lookback_days * 2)
        bars = self._data.get_ohlc_bars([symbol], start, as_of_date)
        df = bars.get(symbol, pd.DataFrame())
        # Return only the requested number of trading days
        return df.tail(lookback_days)

    def get_latest_price(self, symbol: str) -> Decimal:
        """Return the most recent adjusted close on or before the simulation date."""
        closes = self._data.get_close_prices([symbol], date(2000, 1, 1), self._as_of)
        series = closes[symbol].dropna()
        if series.empty:
            raise ValueError(f"No price data for {symbol} as of {self._as_of}")
        return Decimal(str(series.iloc[-1]))
