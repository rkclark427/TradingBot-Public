"""SQLAlchemy ORM models for the tracking layer.

These are dumb data containers — no business logic.
All timestamps are timezone-aware UTC.
Money amounts use Numeric(20, 10) for fractional share / price precision.
JSON fields are Text; serialization is handled by the repo layer.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Sleeves
# ---------------------------------------------------------------------------


class SleeveRow(Base):
    __tablename__ = "sleeves"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    starting_capital: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    current_nav: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    high_water_mark: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parameters_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class SleeveCapitalEventRow(Base):
    __tablename__ = "sleeve_capital_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sleeve_id: Mapped[str] = mapped_column(String, ForeignKey("sleeves.id"), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


class SignalRow(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sleeve_id: Mapped[str] = mapped_column(String, ForeignKey("sleeves.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    target_weight: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    rationale_json: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class IntendedOrderRow(Base):
    __tablename__ = "intended_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sleeve_id: Mapped[str] = mapped_column(String, ForeignKey("sleeves.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)


class NetOrderRow(Base):
    __tablename__ = "net_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    net_qty: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    alpaca_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)


class IntendedToNetRow(Base):
    __tablename__ = "intended_to_net"

    intended_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("intended_orders.id"), primary_key=True
    )
    net_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("net_orders.id"), primary_key=True
    )
    allocated_qty: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    net_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("net_orders.id"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    fees: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)


class SleeveFillRow(Base):
    __tablename__ = "sleeve_fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fill_id: Mapped[int] = mapped_column(Integer, ForeignKey("fills.id"), nullable=False)
    sleeve_id: Mapped[str] = mapped_column(String, ForeignKey("sleeves.id"), nullable=False)
    allocated_qty: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    allocated_price: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


class PositionRow(Base):
    __tablename__ = "positions"

    sleeve_id: Mapped[str] = mapped_column(
        String, ForeignKey("sleeves.id"), primary_key=True
    )
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


class SleeveNavSnapshotRow(Base):
    __tablename__ = "sleeve_nav_snapshots"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    sleeve_id: Mapped[str] = mapped_column(
        String, ForeignKey("sleeves.id"), primary_key=True
    )
    nav: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    position_value: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    realized_pnl_today: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)


class AccountSnapshotRow(Base):
    __tablename__ = "account_snapshots"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    mode: Mapped[str] = mapped_column(String, primary_key=True)
    total_equity: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    total_cash: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    unallocated_cash: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)


# ---------------------------------------------------------------------------
# Risk and system events
# ---------------------------------------------------------------------------


class RiskEventRow(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    level: Mapped[str] = mapped_column(String, nullable=False)
    sleeve_id_nullable: Mapped[str | None] = mapped_column(
        String, ForeignKey("sleeves.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class SystemEventRow(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Asset universe and account capabilities
# ---------------------------------------------------------------------------


class AssetUniverseRow(Base):
    __tablename__ = "asset_universe"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    tradable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fractionable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    marginable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    shortable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    etb: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_refreshed: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AccountCapabilitiesRow(Base):
    __tablename__ = "account_capabilities"

    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)
    capability_key: Mapped[str] = mapped_column(String, primary_key=True)
    capability_value: Mapped[str] = mapped_column(Text, nullable=False)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class HeartbeatRow(Base):
    __tablename__ = "heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
