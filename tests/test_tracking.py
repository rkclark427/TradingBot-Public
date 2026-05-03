"""Tests for the tracking layer: ORM models, repos, and schema creation.

Uses an in-memory SQLite database — no file I/O required.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from src.tracking.models import Base
from src.tracking.repos.events import CapitalEventRepo, RiskEventRepo, SignalRepo, SystemEventRepo
from src.tracking.repos.fills import FillRepo, SleeveFillRepo
from src.tracking.repos.heartbeat import HeartbeatRepo
from src.tracking.repos.market import AccountCapabilitiesRepo, AssetUniverseRepo
from src.tracking.repos.orders import IntendedOrderRepo, IntendedToNetRepo, NetOrderRepo
from src.tracking.repos.positions import PositionRepo
from src.tracking.repos.sleeves import SleeveRepo
from src.tracking.repos.snapshots import AccountSnapshotRepo, NavSnapshotRepo

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

EXPECTED_TABLES = {
    "sleeves",
    "sleeve_capital_events",
    "signals",
    "intended_orders",
    "net_orders",
    "intended_to_net",
    "fills",
    "sleeve_fills",
    "positions",
    "sleeve_nav_snapshots",
    "account_snapshots",
    "risk_events",
    "system_events",
    "asset_universe",
    "account_capabilities",
    "heartbeat",
}

NOW = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)
TODAY = date(2026, 5, 3)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine) as sess:
        yield sess


@pytest.fixture()
def sleeve_id() -> str:
    return "sleeve-abc-123"


@pytest.fixture()
def sleeve(session, sleeve_id):
    """Persist a minimal SleeveRow and return it."""
    repo = SleeveRepo(session)
    row = repo.create(
        id=sleeve_id,
        strategy_name="buy_and_hold",
        mode="paper",
        status="running",
        starting_capital=Decimal("500.00"),
        current_nav=Decimal("500.00"),
        high_water_mark=Decimal("500.00"),
        created_at=NOW,
        parameters_json=None,
    )
    session.commit()
    return row


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------


def test_all_tables_created(engine):
    insp = inspect(engine)
    actual = set(insp.get_table_names())
    assert EXPECTED_TABLES == actual, (
        f"Missing: {EXPECTED_TABLES - actual}, Extra: {actual - EXPECTED_TABLES}"
    )


# ---------------------------------------------------------------------------
# SleeveRepo
# ---------------------------------------------------------------------------


def test_sleeve_create_and_get(session, sleeve_id):
    repo = SleeveRepo(session)
    created = repo.create(
        id=sleeve_id,
        strategy_name="mean_reversion",
        mode="live",
        status="running",
        starting_capital=Decimal("1000.00"),
        current_nav=Decimal("1050.00"),
        high_water_mark=Decimal("1050.00"),
        created_at=NOW,
        parameters_json='{"lookback": 5}',
    )
    session.commit()

    fetched = repo.get(sleeve_id)
    assert fetched is not None
    assert fetched.id == sleeve_id
    assert fetched.strategy_name == "mean_reversion"
    assert fetched.mode == "live"
    assert fetched.status == "running"
    assert fetched.current_nav == Decimal("1050.00")
    assert fetched.parameters_json == '{"lookback": 5}'


def test_sleeve_update_status(session, sleeve):
    repo = SleeveRepo(session)
    updated = repo.update_status(sleeve.id, "paused")
    session.commit()

    assert updated is not None
    assert updated.status == "paused"
    refetched = repo.get(sleeve.id)
    assert refetched is not None
    assert refetched.status == "paused"


def test_sleeve_update_nav(session, sleeve):
    repo = SleeveRepo(session)
    updated = repo.update_nav(
        sleeve.id,
        current_nav=Decimal("550.00"),
        high_water_mark=Decimal("550.00"),
    )
    session.commit()

    assert updated is not None
    assert updated.current_nav == Decimal("550.00")
    assert updated.high_water_mark == Decimal("550.00")


def test_sleeve_list(session, sleeve):
    repo = SleeveRepo(session)
    all_sleeves = repo.list()
    assert len(all_sleeves) == 1
    assert all_sleeves[0].id == sleeve.id


def test_sleeve_get_nonexistent(session):
    repo = SleeveRepo(session)
    assert repo.get("does-not-exist") is None


def test_sleeve_update_status_nonexistent(session):
    repo = SleeveRepo(session)
    result = repo.update_status("does-not-exist", "paused")
    assert result is None


# ---------------------------------------------------------------------------
# IntendedOrderRepo
# ---------------------------------------------------------------------------


def test_intended_order_create_and_get(session, sleeve):
    repo = IntendedOrderRepo(session)
    order = repo.create(
        timestamp=NOW,
        sleeve_id=sleeve.id,
        symbol="SPY",
        side="buy",
        qty=Decimal("2.5"),
        limit_price=Decimal("450.00"),
        status="pending",
    )
    session.commit()

    assert order.id is not None
    fetched = repo.get(order.id)
    assert fetched is not None
    assert fetched.symbol == "SPY"
    assert fetched.side == "buy"
    assert fetched.qty == Decimal("2.5")
    assert fetched.limit_price == Decimal("450.00")
    assert fetched.status == "pending"


def test_intended_order_update_status(session, sleeve):
    repo = IntendedOrderRepo(session)
    order = repo.create(
        timestamp=NOW,
        sleeve_id=sleeve.id,
        symbol="AAPL",
        side="sell",
        qty=Decimal("1.0"),
        limit_price=None,
        status="pending",
    )
    session.commit()

    updated = repo.update_status(order.id, "filled")
    session.commit()
    assert updated is not None
    assert updated.status == "filled"


# ---------------------------------------------------------------------------
# PositionRepo
# ---------------------------------------------------------------------------


def test_position_upsert_create(session, sleeve):
    repo = PositionRepo(session)
    pos = repo.upsert(
        sleeve_id=sleeve.id,
        symbol="SPY",
        qty=Decimal("3.0"),
        avg_cost=Decimal("450.00"),
    )
    session.commit()

    assert pos.sleeve_id == sleeve.id
    assert pos.symbol == "SPY"
    assert pos.qty == Decimal("3.0")
    assert pos.avg_cost == Decimal("450.00")


def test_position_upsert_update(session, sleeve):
    repo = PositionRepo(session)
    repo.upsert(
        sleeve_id=sleeve.id,
        symbol="SPY",
        qty=Decimal("3.0"),
        avg_cost=Decimal("450.00"),
    )
    session.commit()

    repo.upsert(
        sleeve_id=sleeve.id,
        symbol="SPY",
        qty=Decimal("5.0"),
        avg_cost=Decimal("455.00"),
    )
    session.commit()

    fetched = repo.get(sleeve.id, "SPY")
    assert fetched is not None
    assert fetched.qty == Decimal("5.0")
    assert fetched.avg_cost == Decimal("455.00")


def test_position_list_by_sleeve(session, sleeve):
    repo = PositionRepo(session)
    repo.upsert(sleeve_id=sleeve.id, symbol="SPY", qty=Decimal("1.0"), avg_cost=Decimal("450.00"))
    repo.upsert(sleeve_id=sleeve.id, symbol="QQQ", qty=Decimal("2.0"), avg_cost=Decimal("380.00"))
    session.commit()

    positions = repo.list_by_sleeve(sleeve.id)
    symbols = {p.symbol for p in positions}
    assert symbols == {"SPY", "QQQ"}


def test_position_get_nonexistent(session, sleeve):
    repo = PositionRepo(session)
    assert repo.get(sleeve.id, "NONEXISTENT") is None


# ---------------------------------------------------------------------------
# HeartbeatRepo
# ---------------------------------------------------------------------------


def test_heartbeat_update_and_get_latest(session):
    repo = HeartbeatRepo(session)

    # get_latest before any update returns None
    assert repo.get_latest() is None

    hb = repo.update(NOW)
    session.commit()

    assert hb.timestamp == NOW

    fetched = repo.get_latest()
    assert fetched is not None
    assert fetched.timestamp == NOW


def test_heartbeat_update_idempotent(session):
    repo = HeartbeatRepo(session)
    repo.update(NOW)
    session.commit()

    later = datetime(2026, 5, 3, 13, 0, 0, tzinfo=timezone.utc)
    repo.update(later)
    session.commit()

    fetched = repo.get_latest()
    assert fetched is not None
    assert fetched.timestamp == later

    # Confirm only one row in the table
    count = session.execute(text("SELECT COUNT(*) FROM heartbeat")).scalar()
    assert count == 1


# ---------------------------------------------------------------------------
# NavSnapshotRepo
# ---------------------------------------------------------------------------


def test_nav_snapshot_write_and_get_latest(session, sleeve):
    repo = NavSnapshotRepo(session)
    snap = repo.write_snapshot(
        snapshot_date=TODAY,
        sleeve_id=sleeve.id,
        nav=Decimal("510.00"),
        cash=Decimal("100.00"),
        position_value=Decimal("410.00"),
        realized_pnl_today=Decimal("10.00"),
    )
    session.commit()

    assert snap.date == TODAY
    assert snap.nav == Decimal("510.00")

    latest = repo.get_latest(sleeve.id)
    assert latest is not None
    assert latest.nav == Decimal("510.00")
    assert latest.cash == Decimal("100.00")
    assert latest.position_value == Decimal("410.00")
    assert latest.realized_pnl_today == Decimal("10.00")


def test_nav_snapshot_upsert(session, sleeve):
    repo = NavSnapshotRepo(session)
    repo.write_snapshot(
        snapshot_date=TODAY,
        sleeve_id=sleeve.id,
        nav=Decimal("510.00"),
        cash=Decimal("100.00"),
        position_value=Decimal("410.00"),
        realized_pnl_today=Decimal("10.00"),
    )
    session.commit()

    # Overwrite same date
    repo.write_snapshot(
        snapshot_date=TODAY,
        sleeve_id=sleeve.id,
        nav=Decimal("520.00"),
        cash=Decimal("110.00"),
        position_value=Decimal("410.00"),
        realized_pnl_today=Decimal("20.00"),
    )
    session.commit()

    latest = repo.get_latest(sleeve.id)
    assert latest is not None
    assert latest.nav == Decimal("520.00")


def test_nav_snapshot_get_latest_none(session, sleeve):
    repo = NavSnapshotRepo(session)
    assert repo.get_latest(sleeve.id) is None


# ---------------------------------------------------------------------------
# NetOrderRepo
# ---------------------------------------------------------------------------


def test_net_order_create_and_get(session):
    repo = NetOrderRepo(session)
    order = repo.create(
        timestamp=NOW,
        mode="paper",
        symbol="SPY",
        side="buy",
        net_qty=Decimal("5.0"),
        limit_price=Decimal("451.00"),
        alpaca_id="alpaca-order-xyz",
        status="submitted",
    )
    session.commit()

    fetched = repo.get(order.id)
    assert fetched is not None
    assert fetched.mode == "paper"
    assert fetched.symbol == "SPY"
    assert fetched.net_qty == Decimal("5.0")
    assert fetched.alpaca_id == "alpaca-order-xyz"


# ---------------------------------------------------------------------------
# IntendedToNetRepo
# ---------------------------------------------------------------------------


def test_intended_to_net_create_and_get(session, sleeve):
    intended_repo = IntendedOrderRepo(session)
    net_repo = NetOrderRepo(session)
    mapping_repo = IntendedToNetRepo(session)

    intended = intended_repo.create(
        timestamp=NOW,
        sleeve_id=sleeve.id,
        symbol="SPY",
        side="buy",
        qty=Decimal("3.0"),
        limit_price=Decimal("450.00"),
        status="pending",
    )
    net = net_repo.create(
        timestamp=NOW,
        mode="paper",
        symbol="SPY",
        side="buy",
        net_qty=Decimal("3.0"),
        limit_price=Decimal("450.00"),
        alpaca_id=None,
        status="submitted",
    )
    session.commit()

    mapping = mapping_repo.create(
        intended_order_id=intended.id,
        net_order_id=net.id,
        allocated_qty=Decimal("3.0"),
    )
    session.commit()

    rows = mapping_repo.get_by_net_order(net.id)
    assert len(rows) == 1
    assert rows[0].intended_order_id == intended.id
    assert rows[0].allocated_qty == Decimal("3.0")


# ---------------------------------------------------------------------------
# FillRepo and SleeveFillRepo
# ---------------------------------------------------------------------------


def test_fill_create_and_get_by_net_order(session, sleeve):
    net_repo = NetOrderRepo(session)
    fill_repo = FillRepo(session)
    sleeve_fill_repo = SleeveFillRepo(session)

    net = net_repo.create(
        timestamp=NOW,
        mode="paper",
        symbol="AAPL",
        side="buy",
        net_qty=Decimal("2.0"),
        limit_price=Decimal("175.00"),
        alpaca_id=None,
        status="filled",
    )
    session.commit()

    fill = fill_repo.create(
        net_order_id=net.id,
        timestamp=NOW,
        qty=Decimal("2.0"),
        fill_price=Decimal("174.95"),
        fees=Decimal("0.00"),
    )
    session.commit()

    fills = fill_repo.get_by_net_order(net.id)
    assert len(fills) == 1
    assert fills[0].fill_price == Decimal("174.95")

    sf = sleeve_fill_repo.create(
        fill_id=fill.id,
        sleeve_id=sleeve.id,
        allocated_qty=Decimal("2.0"),
        allocated_price=Decimal("174.95"),
    )
    session.commit()

    sleeve_fills = sleeve_fill_repo.get_by_net_order(net.id)
    assert len(sleeve_fills) == 1
    assert sleeve_fills[0].sleeve_id == sleeve.id


# ---------------------------------------------------------------------------
# AssetUniverseRepo
# ---------------------------------------------------------------------------


def test_asset_universe_upsert_and_get_all(session):
    repo = AssetUniverseRepo(session)
    repo.upsert(
        symbol="SPY",
        tradable=True,
        fractionable=True,
        marginable=True,
        shortable=True,
        etb=True,
        last_refreshed=NOW,
    )
    repo.upsert(
        symbol="AAPL",
        tradable=True,
        fractionable=True,
        marginable=True,
        shortable=False,
        etb=True,
        last_refreshed=NOW,
    )
    session.commit()

    all_assets = repo.get_all()
    symbols = {a.symbol for a in all_assets}
    assert {"SPY", "AAPL"} == symbols


def test_asset_universe_upsert_update(session):
    repo = AssetUniverseRepo(session)
    repo.upsert(
        symbol="SPY",
        tradable=True,
        fractionable=True,
        marginable=True,
        shortable=True,
        etb=True,
        last_refreshed=NOW,
    )
    session.commit()

    later = datetime(2026, 5, 4, 0, 0, 0, tzinfo=timezone.utc)
    repo.upsert(
        symbol="SPY",
        tradable=False,
        fractionable=True,
        marginable=True,
        shortable=True,
        etb=True,
        last_refreshed=later,
    )
    session.commit()

    all_assets = repo.get_all()
    spy = next(a for a in all_assets if a.symbol == "SPY")
    assert spy.tradable is False
    assert spy.last_refreshed == later


# ---------------------------------------------------------------------------
# AccountCapabilitiesRepo
# ---------------------------------------------------------------------------


def test_account_capabilities_upsert_and_get_all(session):
    repo = AccountCapabilitiesRepo(session)
    repo.upsert(
        snapshot_date=TODAY,
        capability_key="fractional_shares_enabled",
        capability_value="true",
    )
    repo.upsert(
        snapshot_date=TODAY,
        capability_key="shorting_enabled",
        capability_value="false",
    )
    session.commit()

    rows = repo.get_all(TODAY)
    keys = {r.capability_key for r in rows}
    assert {"fractional_shares_enabled", "shorting_enabled"} == keys


# ---------------------------------------------------------------------------
# RiskEventRepo
# ---------------------------------------------------------------------------


def test_risk_event_create_and_list_recent(session, sleeve):
    repo = RiskEventRepo(session)
    repo.create(
        timestamp=NOW,
        level="sleeve",
        sleeve_id_nullable=sleeve.id,
        event_type="daily_loss_halt",
        severity="warning",
        details_json='{"loss_pct": 0.04}',
    )
    repo.create(
        timestamp=NOW,
        level="account",
        sleeve_id_nullable=None,
        event_type="account_drawdown_halt",
        severity="critical",
    )
    session.commit()

    recent = repo.list_recent(limit=10)
    assert len(recent) == 2


# ---------------------------------------------------------------------------
# SystemEventRepo
# ---------------------------------------------------------------------------


def test_system_event_create_and_list_recent(session):
    repo = SystemEventRepo(session)
    repo.create(
        timestamp=NOW,
        event_type="startup",
        message="Bot started successfully.",
        context_json='{"version": "0.1.0"}',
    )
    session.commit()

    recent = repo.list_recent(limit=5)
    assert len(recent) == 1
    assert recent[0].event_type == "startup"


# ---------------------------------------------------------------------------
# SignalRepo
# ---------------------------------------------------------------------------


def test_signal_create_and_list_recent(session, sleeve):
    repo = SignalRepo(session)
    repo.create(
        timestamp=NOW,
        sleeve_id=sleeve.id,
        symbol="SPY",
        target_weight=Decimal("1.0"),
        rationale_json='{"reason": "buy_and_hold"}',
    )
    session.commit()

    recent = repo.list_recent(sleeve.id, limit=10)
    assert len(recent) == 1
    assert recent[0].symbol == "SPY"
    assert recent[0].target_weight == Decimal("1.0")


# ---------------------------------------------------------------------------
# CapitalEventRepo
# ---------------------------------------------------------------------------


def test_capital_event_create_and_list_recent(session, sleeve):
    repo = CapitalEventRepo(session)
    repo.create(
        sleeve_id=sleeve.id,
        timestamp=NOW,
        event_type="deposit",
        amount=Decimal("100.00"),
        reason="Initial top-up",
    )
    session.commit()

    recent = repo.list_recent(sleeve.id, limit=10)
    assert len(recent) == 1
    assert recent[0].event_type == "deposit"
    assert recent[0].amount == Decimal("100.00")


# ---------------------------------------------------------------------------
# AccountSnapshotRepo
# ---------------------------------------------------------------------------


def test_account_snapshot_write_and_get_latest(session):
    repo = AccountSnapshotRepo(session)
    repo.write_snapshot(
        snapshot_date=TODAY,
        mode="paper",
        total_equity=Decimal("10000.00"),
        total_cash=Decimal("5000.00"),
        unallocated_cash=Decimal("4500.00"),
    )
    session.commit()

    latest = repo.get_latest("paper")
    assert latest is not None
    assert latest.total_equity == Decimal("10000.00")
    assert latest.unallocated_cash == Decimal("4500.00")


def test_account_snapshot_get_latest_none(session):
    repo = AccountSnapshotRepo(session)
    assert repo.get_latest("live") is None
