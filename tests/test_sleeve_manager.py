from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.sleeves.manager import (
    InvalidTransitionError,
    SleeveManager,
    SleeveNotFoundError,
)
from src.sleeves.types import (
    AccountCapabilities,
    CapabilityMismatchError,
    SleeveStatus,
    Mode,
)
from src.strategies.base import Strategy, StrategyCapabilities, Target
from src.tracking.models import Base
from src.tracking.repos.events import CapitalEventRepo
from src.tracking.repos.positions import PositionRepo


def _make_capabilities(**overrides: object) -> AccountCapabilities:
    defaults = dict(
        asset_classes=frozenset({"us_equity"}),
        options_trading_level=0,
        fractional_shares_enabled=True,
        shorting_enabled=True,
        is_pdt=False,
        buying_power=Decimal("100000"),
        cash=Decimal("100000"),
        as_of=datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return AccountCapabilities(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def caps() -> AccountCapabilities:
    return _make_capabilities()


@pytest.fixture
def manager(session: Session, caps: AccountCapabilities) -> SleeveManager:
    return SleeveManager(session=session, account_capabilities=caps)


# ------------------------------------------------------------------
# create_sleeve
# ------------------------------------------------------------------

def test_create_sleeve_returns_sleeve(manager: SleeveManager) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {"symbol": "SPY"})
    assert sleeve.strategy_name == "buy_and_hold"
    assert sleeve.mode == Mode.PAPER
    assert sleeve.status == SleeveStatus.RUNNING
    assert sleeve.starting_capital == Decimal("100")
    assert sleeve.current_nav == Decimal("100")


def test_create_sleeve_unknown_strategy_raises(manager: SleeveManager) -> None:
    with pytest.raises(ValueError, match="Unknown strategy"):
        manager.create_sleeve("nonexistent", "paper", Decimal("100"), {})


def test_create_sleeve_capability_mismatch_raises(session: Session) -> None:
    caps = _make_capabilities(fractional_shares_enabled=False)
    mgr = SleeveManager(session=session, account_capabilities=caps)
    with pytest.raises(CapabilityMismatchError):
        mgr.create_sleeve("buy_and_hold", "paper", Decimal("100"), {"symbol": "SPY"})


def test_create_sleeve_records_capital_event(manager: SleeveManager, session: Session) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    events = CapitalEventRepo(session).list_recent(sleeve_id=str(sleeve.id))
    assert len(events) == 1
    assert events[0].event_type == "deposit"
    assert Decimal(str(events[0].amount)) == Decimal("100")


def test_create_sleeve_persists(manager: SleeveManager) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    fetched = manager.get_sleeve(str(sleeve.id))
    assert fetched is not None
    assert fetched.id == sleeve.id


# ------------------------------------------------------------------
# get_sleeve / list_sleeves
# ------------------------------------------------------------------

def test_get_sleeve_returns_none_for_unknown(manager: SleeveManager) -> None:
    assert manager.get_sleeve("nonexistent-id") is None


def test_list_sleeves_empty(manager: SleeveManager) -> None:
    assert manager.list_sleeves() == []


def test_list_sleeves_returns_all(manager: SleeveManager) -> None:
    manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    manager.create_sleeve("buy_and_hold", "paper", Decimal("200"), {})
    assert len(manager.list_sleeves()) == 2


def test_list_sleeves_status_filter(manager: SleeveManager) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    manager.pause_sleeve(str(sleeve.id))
    running = manager.list_sleeves(status_filter="running")
    paused = manager.list_sleeves(status_filter="paused")
    assert len(running) == 0
    assert len(paused) == 1


# ------------------------------------------------------------------
# Lifecycle transitions
# ------------------------------------------------------------------

def test_pause_sleeve(manager: SleeveManager) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    paused = manager.pause_sleeve(str(sleeve.id))
    assert paused.status == SleeveStatus.PAUSED


def test_resume_sleeve(manager: SleeveManager) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    manager.pause_sleeve(str(sleeve.id))
    resumed = manager.resume_sleeve(str(sleeve.id))
    assert resumed.status == SleeveStatus.RUNNING


def test_stop_sleeve(manager: SleeveManager) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    stopped = manager.stop_sleeve(str(sleeve.id))
    assert stopped.status == SleeveStatus.STOPPING


def test_invalid_transition_raises(manager: SleeveManager) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    with pytest.raises(InvalidTransitionError):
        manager.resume_sleeve(str(sleeve.id))


def test_transition_nonexistent_raises(manager: SleeveManager) -> None:
    with pytest.raises(SleeveNotFoundError):
        manager.pause_sleeve("no-such-id")


# ------------------------------------------------------------------
# compute_nav
# ------------------------------------------------------------------

def test_compute_nav_no_positions(manager: SleeveManager) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    nav = manager.compute_nav(str(sleeve.id), {})
    assert nav == Decimal("100")


def test_compute_nav_with_position(manager: SleeveManager, session: Session) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    PositionRepo(session).upsert(
        sleeve_id=str(sleeve.id), symbol="SPY", qty=Decimal("0.5"), avg_cost=Decimal("200")
    )
    nav = manager.compute_nav(str(sleeve.id), {"SPY": Decimal("210")})
    assert nav == Decimal("100") + Decimal("0.5") * Decimal("210")


# ------------------------------------------------------------------
# attribute_fill
# ------------------------------------------------------------------

def test_attribute_fill_buy_creates_position(manager: SleeveManager, session: Session) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    manager.attribute_fill(str(sleeve.id), "SPY", "buy", Decimal("0.5"), Decimal("200"))
    pos = PositionRepo(session).get(str(sleeve.id), "SPY")
    assert pos is not None
    assert Decimal(str(pos.qty)) == Decimal("0.5")


def test_attribute_fill_buy_reduces_cash(manager: SleeveManager) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    manager.attribute_fill(str(sleeve.id), "SPY", "buy", Decimal("0.5"), Decimal("100"))
    updated = manager.get_sleeve(str(sleeve.id))
    assert updated is not None
    assert updated.current_nav == Decimal("100") - Decimal("0.5") * Decimal("100")


def test_attribute_fill_sell_increases_cash(manager: SleeveManager, session: Session) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    PositionRepo(session).upsert(
        sleeve_id=str(sleeve.id), symbol="SPY", qty=Decimal("1"), avg_cost=Decimal("100")
    )
    manager.attribute_fill(str(sleeve.id), "SPY", "sell", Decimal("0.5"), Decimal("110"))
    updated = manager.get_sleeve(str(sleeve.id))
    assert updated is not None
    assert updated.current_nav == Decimal("100") + Decimal("0.5") * Decimal("110")


# ------------------------------------------------------------------
# record_capital_event
# ------------------------------------------------------------------

def test_record_capital_event(manager: SleeveManager, session: Session) -> None:
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("100"), {})
    manager.record_capital_event(str(sleeve.id), "withdrawal", Decimal("25"), "test withdrawal")
    events = CapitalEventRepo(session).list_recent(sleeve_id=str(sleeve.id))
    types = [e.event_type for e in events]
    assert "withdrawal" in types


def test_record_capital_event_unknown_sleeve_raises(manager: SleeveManager) -> None:
    with pytest.raises(SleeveNotFoundError):
        manager.record_capital_event("bad-id", "deposit", Decimal("50"))
