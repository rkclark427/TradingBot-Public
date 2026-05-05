"""Tests for src/orchestrator.py."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.config.models import BaseConfig
from src.data.alpaca_client import ClockInfo
from src.orchestrator import Orchestrator
from src.sleeves.manager import SleeveManager
from src.sleeves.types import AccountCapabilities, SleeveStatus
from src.tracking.models import Base
from src.tracking.repos.market import AssetUniverseRepo
from src.tracking.repos.positions import PositionRepo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 5, 4, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def caps() -> AccountCapabilities:
    return AccountCapabilities(
        asset_classes=frozenset({"us_equity"}),
        options_trading_level=0,
        fractional_shares_enabled=True,
        shorting_enabled=True,
        is_pdt=False,
        buying_power=Decimal("100000"),
        cash=Decimal("100000"),
        as_of=_AS_OF,
    )


@pytest.fixture()
def manager(session: Session, caps: AccountCapabilities) -> SleeveManager:
    return SleeveManager(session=session, account_capabilities=caps)


@pytest.fixture()
def config() -> BaseConfig:
    return BaseConfig()


def _make_mock_clock(is_open: bool = True) -> MagicMock:
    clock = MagicMock(spec=ClockInfo)
    clock.is_open = is_open
    clock.next_open = datetime(2026, 5, 5, 13, 30, tzinfo=timezone.utc)
    clock.next_close = datetime(2026, 5, 4, 20, 0, tzinfo=timezone.utc)
    clock.timestamp = _AS_OF
    return clock


def _make_mock_client(is_open: bool = True, spy_price: float = 500.0) -> MagicMock:
    """Return a mock Alpaca client with sensible defaults."""
    client = MagicMock()
    client.get_clock.return_value = _make_mock_clock(is_open)
    client.get_latest_trade_price.return_value = Decimal(str(spy_price))
    client.get_positions.return_value = []

    order_info = MagicMock()
    order_info.id = "mock-alpaca-order-id"
    client.submit_order.return_value = order_info

    return client


def _add_spy_to_universe(session: Session) -> None:
    AssetUniverseRepo(session).upsert(
        symbol="SPY",
        tradable=True,
        fractionable=True,
        marginable=True,
        shortable=True,
        etb=True,
        last_refreshed=_AS_OF,
    )
    session.commit()


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_run_once_skips_when_kill_switch_active(
    tmp_path, monkeypatch, session: Session, manager: SleeveManager, config: BaseConfig
) -> None:
    """Kill switch flag → cycle returns skipped=True without touching the client."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "kill_switch.flag").touch()

    mock_client = _make_mock_client()
    orch = Orchestrator(config, session, mock_client, None, manager)

    result = orch.run_once()

    assert result["skipped"] is True
    assert result["skip_reason"] == "kill_switch"
    mock_client.get_clock.assert_not_called()


# ---------------------------------------------------------------------------
# Market closed
# ---------------------------------------------------------------------------


def test_run_once_skips_when_market_closed(
    tmp_path, monkeypatch, session: Session, manager: SleeveManager, config: BaseConfig
) -> None:
    """Market closed → cycle skips processing without submitting orders."""
    monkeypatch.chdir(tmp_path)

    mock_client = _make_mock_client(is_open=False)
    orch = Orchestrator(config, session, mock_client, None, manager)

    result = orch.run_once()

    assert result["skipped"] is True
    assert result["skip_reason"] == "market_closed"
    mock_client.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# No sleeves → no orders
# ---------------------------------------------------------------------------


def test_run_once_no_sleeves_produces_no_orders(
    tmp_path, monkeypatch, session: Session, manager: SleeveManager, config: BaseConfig
) -> None:
    """When no sleeves are running, the cycle completes cleanly with zero submissions."""
    monkeypatch.chdir(tmp_path)
    _add_spy_to_universe(session)

    mock_client = _make_mock_client()
    orch = Orchestrator(config, session, mock_client, None, manager)

    result = orch.run_once()

    assert result["skipped"] is False
    assert result["sleeves_processed"] == 0
    assert result["orders_submitted"] == 0
    mock_client.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# New sleeve → buy order submitted
# ---------------------------------------------------------------------------


def test_run_once_new_sleeve_submits_buy_order(
    tmp_path, monkeypatch, session: Session, manager: SleeveManager, config: BaseConfig
) -> None:
    """First cycle for a BuyAndHoldSPY sleeve with no position → submit a buy."""
    monkeypatch.chdir(tmp_path)
    _add_spy_to_universe(session)

    sleeve = manager.create_sleeve(
        "buy_and_hold", "paper", Decimal("500"), {"symbol": "SPY"}
    )

    mock_client = _make_mock_client(spy_price=500.0)
    orch = Orchestrator(config, session, mock_client, None, manager)

    result = orch.run_once()

    assert result["skipped"] is False
    assert result["sleeves_processed"] == 1
    assert result["orders_submitted"] == 1
    mock_client.submit_order.assert_called_once()
    call_kwargs = mock_client.submit_order.call_args[1]
    assert call_kwargs["symbol"] == "SPY"
    assert call_kwargs["side"] == "buy"
    # qty = 500 / 500 = 1.000000 shares
    assert call_kwargs["qty"] == Decimal("1.000000")


# ---------------------------------------------------------------------------
# Sleeve already at target → no orders
# ---------------------------------------------------------------------------


def test_run_once_noop_when_position_at_target(
    tmp_path, monkeypatch, session: Session, manager: SleeveManager, config: BaseConfig
) -> None:
    """BuyAndHoldSPY with SPY position at 100% target → portfolio sizer produces zero delta."""
    monkeypatch.chdir(tmp_path)
    _add_spy_to_universe(session)

    sleeve = manager.create_sleeve(
        "buy_and_hold", "paper", Decimal("500"), {"symbol": "SPY"}
    )
    # Add position directly: 1 share @ $500 = $500 = 100% of NAV
    PositionRepo(session).upsert(
        sleeve_id=str(sleeve.id),
        symbol="SPY",
        qty=Decimal("1"),
        avg_cost=Decimal("500"),
    )
    session.commit()

    mock_client = _make_mock_client(spy_price=500.0)
    orch = Orchestrator(config, session, mock_client, None, manager)

    result = orch.run_once()

    assert result["skipped"] is False
    assert result["sleeves_processed"] == 1
    assert result["orders_submitted"] == 0
    mock_client.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# Empty asset universe → orders blocked by risk layer
# ---------------------------------------------------------------------------


def test_run_once_empty_universe_blocks_orders(
    tmp_path, monkeypatch, session: Session, manager: SleeveManager, config: BaseConfig
) -> None:
    """With no symbols in the asset universe, risk checks reject all orders."""
    monkeypatch.chdir(tmp_path)
    # Do NOT add SPY to universe

    manager.create_sleeve("buy_and_hold", "paper", Decimal("500"), {"symbol": "SPY"})

    mock_client = _make_mock_client(spy_price=500.0)
    orch = Orchestrator(config, session, mock_client, None, manager)

    result = orch.run_once()

    assert result["orders_submitted"] == 0
    mock_client.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# Paused sleeve is not processed
# ---------------------------------------------------------------------------


def test_run_once_paused_sleeve_is_skipped(
    tmp_path, monkeypatch, session: Session, manager: SleeveManager, config: BaseConfig
) -> None:
    """A paused sleeve should not generate orders."""
    monkeypatch.chdir(tmp_path)
    _add_spy_to_universe(session)

    sleeve = manager.create_sleeve(
        "buy_and_hold", "paper", Decimal("500"), {"symbol": "SPY"}
    )
    manager.pause_sleeve(str(sleeve.id))

    mock_client = _make_mock_client()
    orch = Orchestrator(config, session, mock_client, None, manager)

    result = orch.run_once()

    assert result["sleeves_processed"] == 0
    assert result["orders_submitted"] == 0
    mock_client.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# Heartbeat is written each cycle
# ---------------------------------------------------------------------------


def test_run_once_writes_heartbeat(
    tmp_path, monkeypatch, session: Session, manager: SleeveManager, config: BaseConfig
) -> None:
    """Each successful cycle (even with no sleeves) writes a heartbeat row."""
    monkeypatch.chdir(tmp_path)

    mock_client = _make_mock_client()
    orch = Orchestrator(config, session, mock_client, None, manager)

    from src.tracking.repos.heartbeat import HeartbeatRepo

    assert HeartbeatRepo(session).get_latest() is None
    orch.run_once()
    assert HeartbeatRepo(session).get_latest() is not None
