from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.data.capabilities import (
    discover_account_capabilities,
    load_capabilities_from_db,
    persist_capabilities,
)
from src.data.alpaca_client import AccountInfo
from src.sleeves.types import AccountCapabilities
from src.tracking.models import Base


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _fake_account_info(**overrides: object) -> AccountInfo:
    defaults = dict(
        buying_power=Decimal("50000"),
        cash=Decimal("25000"),
        portfolio_value=Decimal("75000"),
        pattern_day_trader=False,
        trading_blocked=False,
        shorting_enabled=True,
        options_trading_level=0,
        fractional_trading=True,
    )
    defaults.update(overrides)
    return AccountInfo(**defaults)  # type: ignore[arg-type]


def test_discover_returns_account_capabilities():
    mock_client = MagicMock()
    mock_client.get_account.return_value = _fake_account_info()
    caps = discover_account_capabilities(mock_client)
    assert isinstance(caps, AccountCapabilities)


def test_discover_maps_fields_correctly():
    mock_client = MagicMock()
    mock_client.get_account.return_value = _fake_account_info(
        buying_power=Decimal("10000"),
        cash=Decimal("5000"),
        shorting_enabled=False,
        fractional_trading=True,
        options_trading_level=2,
        pattern_day_trader=True,
    )
    caps = discover_account_capabilities(mock_client)
    assert caps.buying_power == Decimal("10000")
    assert caps.cash == Decimal("5000")
    assert caps.shorting_enabled is False
    assert caps.fractional_shares_enabled is True
    assert caps.options_trading_level == 2
    assert caps.is_pdt is True
    assert "us_equity" in caps.asset_classes


def test_discover_as_of_is_utc_aware():
    mock_client = MagicMock()
    mock_client.get_account.return_value = _fake_account_info()
    caps = discover_account_capabilities(mock_client)
    assert caps.as_of.tzinfo is not None


def test_persist_and_reload(session: Session):
    mock_client = MagicMock()
    mock_client.get_account.return_value = _fake_account_info(
        buying_power=Decimal("99000"),
        shorting_enabled=True,
    )
    caps = discover_account_capabilities(mock_client)
    persist_capabilities(caps, session)

    reloaded = load_capabilities_from_db(session)
    assert reloaded is not None
    assert reloaded.buying_power == Decimal("99000")
    assert reloaded.shorting_enabled is True
    assert "us_equity" in reloaded.asset_classes


def test_load_returns_none_when_empty(session: Session):
    result = load_capabilities_from_db(session)
    assert result is None


def test_persist_overwrites_same_date(session: Session):
    d = date(2026, 5, 3)
    mock_client = MagicMock()

    mock_client.get_account.return_value = _fake_account_info(buying_power=Decimal("1000"))
    caps1 = discover_account_capabilities(mock_client)
    persist_capabilities(caps1, session, snapshot_date=d)

    mock_client.get_account.return_value = _fake_account_info(buying_power=Decimal("2000"))
    caps2 = discover_account_capabilities(mock_client)
    persist_capabilities(caps2, session, snapshot_date=d)

    reloaded = load_capabilities_from_db(session, snapshot_date=d)
    assert reloaded is not None
    assert reloaded.buying_power == Decimal("2000")


def test_load_uses_latest_date(session: Session):
    mock_client = MagicMock()

    mock_client.get_account.return_value = _fake_account_info(buying_power=Decimal("100"))
    caps_old = discover_account_capabilities(mock_client)
    persist_capabilities(caps_old, session, snapshot_date=date(2026, 5, 1))

    mock_client.get_account.return_value = _fake_account_info(buying_power=Decimal("999"))
    caps_new = discover_account_capabilities(mock_client)
    persist_capabilities(caps_new, session, snapshot_date=date(2026, 5, 3))

    reloaded = load_capabilities_from_db(session)
    assert reloaded is not None
    assert reloaded.buying_power == Decimal("999")
