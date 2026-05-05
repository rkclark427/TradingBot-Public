"""SQLAlchemy models for the backtest price cache.

Separate from the live trading DB (trading_bot.db). This DB is purely derived
data — it can be deleted and rebuilt from yfinance at any time. Schema is
created via Base.metadata.create_all() on first access, not via Alembic.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PriceBarRow(Base):
    """One row per (symbol, trading_date). Stores split- and dividend-adjusted OHLCV."""

    __tablename__ = "price_bars"
    __table_args__ = (UniqueConstraint("symbol", "trade_date", name="uq_symbol_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # All OHLC values are split- and dividend-adjusted (yfinance auto_adjust=True).
    # Use adjusted open for fill simulation; adjusted close for signal computation.
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)

    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
