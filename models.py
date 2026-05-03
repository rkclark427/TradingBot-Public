"""SQLAlchemy ORM models for the trading bot database.

These are dumb data containers — no business logic. Reads happen through the
ORM; writes go through repository classes in src/tracking/repos/.

Design notes:
- SQLite + Alembic. All schema changes go through migrations.
- Decimal columns for money. Use Numeric type, not Float.
- Datetimes always stored as UTC; conversions happen at the application edge.
- Foreign keys enforced via SQLite PRAGMA in the engine setup.
- The split between intended_orders, net_orders, and intended_to_net is
  the bookkeeping that enables clean attribution after order netting.
  See architecture.md section 4.4.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.sleeves.types import Mode, SleeveStatus


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Declarative base for all ORM models.

    The type_annotation_map tells SQLAlchemy how to translate Python types
    to SQL column types. We map Decimal -> Numeric(20,8) for all money
    columns, and dict[str, Any] -> JSON for all JSON-blob columns. This
    means individual columns can use the bare Python type annotation and
    SQLAlchemy picks the right SQL type automatically.
    """

    type_annotation_map = {
        Decimal: Numeric(20, 8),
        dict[str, Any]: JSON,
    }


def _utcnow() -> datetime:
    """tz-aware UTC now. Default for created_at columns."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Sleeves
# ---------------------------------------------------------------------------


class SleeveModel(Base):
    """A sleeve: a runtime instance of a strategy."""

    __tablename__ = "sleeves"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    strategy_name: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[Mode] = mapped_column(SQLEnum(Mode), index=True)
    status: Mapped[SleeveStatus] = mapped_column(SQLEnum(SleeveStatus), index=True)

    starting_capital: Mapped[Decimal] = mapped_column()
    current_nav: Mapped[Decimal] = mapped_column()
    high_water_mark: Mapped[Decimal] = mapped_column()

    # Strategy parameters and risk config — JSON for flexibility
    parameters_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    risk_config_json: Mapped[dict[str, Any]] = mapped_column(default=dict)

    managed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_signaled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    capital_events: Mapped[list[SleeveCapitalEventModel]] = relationship(
        back_populates="sleeve", cascade="all, delete-orphan"
    )


class SleeveCapitalEventModel(Base):
    """A capital movement event for a sleeve.

    Records every deposit, withdrawal, and the initial creation event.
    Together these form the audit trail of how a sleeve's bankroll changed
    over time, separate from P&L.
    """

    __tablename__ = "sleeve_capital_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sleeve_id: Mapped[UUID] = mapped_column(ForeignKey("sleeves.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    event_type: Mapped[str] = mapped_column(String(32))
    # event_type: 'create', 'deposit', 'withdraw', 'stop_return'
    amount: Mapped[Decimal] = mapped_column()  # signed: deposits +, withdrawals -
    reason: Mapped[str | None] = mapped_column(Text)

    sleeve: Mapped[SleeveModel] = relationship(back_populates="capital_events")


# ---------------------------------------------------------------------------
# Signals & Orders
# ---------------------------------------------------------------------------


class SignalModel(Base):
    """A signal produced by a strategy at a point in time.

    Stored before the signal is translated to orders so we can analyze why
    the strategy made the decisions it did, independent of execution outcomes.
    """

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sleeve_id: Mapped[UUID] = mapped_column(ForeignKey("sleeves.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    target_weight: Mapped[float] = mapped_column()
    rationale_json: Mapped[dict[str, Any]] = mapped_column(default=dict)

    __table_args__ = (Index("ix_signals_sleeve_timestamp", "sleeve_id", "timestamp"),)


class IntendedOrderModel(Base):
    """A sleeve's pre-netting desired trade.

    Created by the portfolio layer from the strategy's targets. Each intended
    order is tagged with its originating sleeve so attribution survives netting.
    Multiple intended orders on the same symbol from different sleeves get
    netted into one NetOrderModel.
    """

    __tablename__ = "intended_orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sleeve_id: Mapped[UUID] = mapped_column(ForeignKey("sleeves.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(4))  # 'buy' or 'sell'
    qty: Mapped[Decimal] = mapped_column()  # always positive; side determines direction
    limit_price: Mapped[Decimal] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # status: 'pending', 'netted', 'rejected', 'filled', 'partial', 'canceled'
    rejection_reason: Mapped[str | None] = mapped_column(Text)


class NetOrderModel(Base):
    """A post-netting order actually submitted to Alpaca.

    One NetOrderModel can correspond to multiple IntendedOrderModels via the
    intended_to_net mapping table. The client_order_id is what we send to
    Alpaca; alpaca_id is what they return.
    """

    __tablename__ = "net_orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    mode: Mapped[Mode] = mapped_column(SQLEnum(Mode), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(4))
    net_qty: Mapped[Decimal] = mapped_column()  # always positive
    limit_price: Mapped[Decimal] = mapped_column()
    client_order_id: Mapped[str] = mapped_column(String(128), unique=True)
    alpaca_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # status: 'pending', 'submitted', 'filled', 'partial', 'canceled', 'rejected'
    rejection_reason: Mapped[str | None] = mapped_column(Text)


class IntendedToNetModel(Base):
    """Maps each intended order to the net order it was rolled into.

    `allocated_qty` is the constituent intended quantity that contributed to
    the net order (signed by side). For a single-sleeve symbol, allocated_qty
    equals the intended order's qty. For multi-sleeve netted symbols, the sum
    of allocated_qty (signed) equals the net_qty (signed).
    """

    __tablename__ = "intended_to_net"

    intended_order_id: Mapped[int] = mapped_column(
        ForeignKey("intended_orders.id"), primary_key=True
    )
    net_order_id: Mapped[int] = mapped_column(ForeignKey("net_orders.id"), primary_key=True)
    allocated_qty: Mapped[Decimal] = mapped_column()  # signed


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------


class FillModel(Base):
    """A fill received from Alpaca against a net order.

    A net order can have multiple fills (partial fills). Each fill is then
    allocated back to constituent sleeves via SleeveFillModel.
    """

    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    net_order_id: Mapped[int] = mapped_column(ForeignKey("net_orders.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    qty: Mapped[Decimal] = mapped_column()  # filled qty for this event (always positive)
    fill_price: Mapped[Decimal] = mapped_column()
    fees: Mapped[Decimal] = mapped_column(default=Decimal("0"))


class SleeveFillModel(Base):
    """Allocates a fill (or part of one) to a specific sleeve.

    For a single-sleeve net order, a fill produces one SleeveFillModel with
    allocated_qty == fill.qty. For a netted multi-sleeve order, a fill
    produces multiple SleeveFillModel rows summing (signed) to fill.qty.
    """

    __tablename__ = "sleeve_fills"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    fill_id: Mapped[int] = mapped_column(ForeignKey("fills.id"), index=True)
    sleeve_id: Mapped[UUID] = mapped_column(ForeignKey("sleeves.id"), index=True)
    allocated_qty: Mapped[Decimal] = mapped_column()  # signed: positive = bought, negative = sold
    allocated_price: Mapped[Decimal] = mapped_column()  # may differ from fill_price if
    # we distribute slippage savings pro-rata


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


class PositionModel(Base):
    """Current position of a sleeve in a symbol.

    Updated after each sleeve_fill is recorded. avg_cost is recomputed using
    standard cost-averaging when adding to a position; closing a position
    realizes P&L (recorded elsewhere) and zeros the row (or deletes it).

    Note: this is current state, not historical. History is reconstructable
    from sleeve_fills + sleeve_capital_events.
    """

    __tablename__ = "positions"

    sleeve_id: Mapped[UUID] = mapped_column(ForeignKey("sleeves.id"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    qty: Mapped[Decimal] = mapped_column()  # signed
    avg_cost: Mapped[Decimal] = mapped_column()
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ---------------------------------------------------------------------------
# Snapshots (daily P&L attribution)
# ---------------------------------------------------------------------------


class SleeveNavSnapshotModel(Base):
    """Daily snapshot of a sleeve's NAV and P&L."""

    __tablename__ = "sleeve_nav_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sleeve_id: Mapped[UUID] = mapped_column(ForeignKey("sleeves.id"), index=True)
    nav: Mapped[Decimal] = mapped_column()
    cash: Mapped[Decimal] = mapped_column()
    position_value: Mapped[Decimal] = mapped_column()
    realized_pnl_today: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    unrealized_pnl: Mapped[Decimal] = mapped_column(default=Decimal("0"))

    __table_args__ = (UniqueConstraint("date", "sleeve_id", name="uq_sleeve_nav_date"),)


class AccountSnapshotModel(Base):
    """Daily snapshot of the overall Alpaca account, per environment."""

    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    mode: Mapped[Mode] = mapped_column(SQLEnum(Mode), index=True)
    total_equity: Mapped[Decimal] = mapped_column()
    total_cash: Mapped[Decimal] = mapped_column()
    unallocated_cash: Mapped[Decimal] = mapped_column()
    # unallocated_cash = total_cash - sum(sleeve cash) for this mode

    __table_args__ = (UniqueConstraint("date", "mode", name="uq_account_date_mode"),)


# ---------------------------------------------------------------------------
# Risk and System Events
# ---------------------------------------------------------------------------


class RiskEventModel(Base):
    """A risk event: trigger of a risk control or rejection of an order.

    `level` distinguishes account-level events (apply across all sleeves)
    from sleeve-level events (apply to one sleeve).
    """

    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    level: Mapped[str] = mapped_column(String(16))  # 'account' or 'sleeve'
    sleeve_id: Mapped[UUID | None] = mapped_column(ForeignKey("sleeves.id"))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16))  # 'info', 'warn', 'halt'
    details_json: Mapped[dict[str, Any]] = mapped_column(default=dict)


class SystemEventModel(Base):
    """A system-level event: startup, shutdown, errors, kill switch toggles."""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    context_json: Mapped[dict[str, Any]] = mapped_column(default=dict)


# ---------------------------------------------------------------------------
# Reference data: assets and capabilities
# ---------------------------------------------------------------------------


class AssetUniverseModel(Base):
    """Cached snapshot of Alpaca's assets endpoint.

    Refreshed nightly. Strategies query this via the data layer to determine
    what's tradable.
    """

    __tablename__ = "asset_universe"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16))
    exchange: Mapped[str] = mapped_column(String(16))
    tradable: Mapped[bool] = mapped_column(Boolean, default=True)
    fractionable: Mapped[bool] = mapped_column(Boolean, default=False)
    marginable: Mapped[bool] = mapped_column(Boolean, default=False)
    shortable: Mapped[bool] = mapped_column(Boolean, default=False)
    easy_to_borrow: Mapped[bool] = mapped_column(Boolean, default=False)
    last_refreshed: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AccountCapabilitySnapshotModel(Base):
    """Versioned snapshots of account capabilities.

    One row per (date, mode) — captures what the account could do on that
    date. Useful for retrospective analysis ("on the day this rejection
    happened, did we have shorting enabled?").
    """

    __tablename__ = "account_capabilities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    snapshot_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    mode: Mapped[Mode] = mapped_column(SQLEnum(Mode), index=True)
    capabilities_json: Mapped[dict[str, Any]] = mapped_column()
    # We store the full capability blob as JSON for forward compatibility —
    # if Alpaca adds new capability fields, we don't need a migration.

    __table_args__ = (
        UniqueConstraint("snapshot_date", "mode", name="uq_account_caps_date_mode"),
    )


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class HeartbeatModel(Base):
    """Single-row table updated each orchestrator cycle.

    External monitoring reads this to detect stale heartbeats. We use a
    fixed id=1 so updates are always UPDATE not INSERT.
    """

    __tablename__ = "heartbeat"

    id: Mapped[int] = mapped_column(primary_key=True)  # always 1
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    cycle_number: Mapped[int] = mapped_column(Integer, default=0)
    last_status: Mapped[str] = mapped_column(String(64), default="ok")
