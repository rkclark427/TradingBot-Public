"""Tests for StrategyStateRow / StrategyStateRepo and attribute_fill integration."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.sleeves.manager import SleeveManager, SleeveNotFoundError
from src.sleeves.types import AccountCapabilities, Mode, SleeveStatus
from src.tracking.models import Base
from src.tracking.repos.positions import PositionRepo
from src.tracking.repos.sleeves import SleeveRepo
from src.tracking.repos.strategy_state import StrategyStateRepo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def caps() -> AccountCapabilities:
    return AccountCapabilities(
        asset_classes=frozenset({"us_equity"}),
        options_trading_level=0,
        fractional_shares_enabled=True,
        shorting_enabled=True,
        is_pdt=False,
        buying_power=Decimal("100000"),
        cash=Decimal("100000"),
        as_of=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def manager(session: Session, caps: AccountCapabilities) -> SleeveManager:
    return SleeveManager(session=session, account_capabilities=caps)


def _create_sleeve(manager: SleeveManager, session: Session) -> str:
    """Create a minimal sleeve and return its ID."""
    sleeve = manager.create_sleeve("buy_and_hold", "paper", Decimal("10000"), {})
    return str(sleeve.id)


# ---------------------------------------------------------------------------
# StrategyStateRepo unit tests
# ---------------------------------------------------------------------------


class TestStrategyStateRepo:
    def test_get_returns_none_for_missing(self, session: Session) -> None:
        repo = StrategyStateRepo(session)
        assert repo.get("sleeve-1", "SPY") is None

    def test_upsert_creates_new_entry(self, session: Session) -> None:
        repo = StrategyStateRepo(session)
        # Need a sleeve row for the FK; bypass manager and insert directly
        session.add(
            __import__("src.tracking.models", fromlist=["SleeveRow"]).SleeveRow(
                id="s1",
                strategy_name="test",
                mode="paper",
                status="running",
                starting_capital=Decimal("10000"),
                current_nav=Decimal("10000"),
                high_water_mark=Decimal("10000"),
                current_cash=Decimal("10000"),
                created_at=datetime(2026, 5, 7, 0, 0, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

        repo.upsert(
            sleeve_id="s1",
            symbol="SPY",
            entry_date=date(2026, 5, 1),
            entry_price=Decimal("500.00"),
            days_held=3,
            highest_close_since_entry=Decimal("510.00"),
            last_session_date=date(2026, 5, 6),
        )
        session.commit()

        row = repo.get("s1", "SPY")
        assert row is not None
        assert row.entry_date == date(2026, 5, 1)
        assert Decimal(str(row.entry_price)) == Decimal("500.00")
        assert row.days_held == 3
        assert Decimal(str(row.highest_close_since_entry)) == Decimal("510.00")
        assert row.last_session_date == date(2026, 5, 6)

    def test_upsert_updates_existing_entry(self, session: Session) -> None:
        from src.tracking.models import SleeveRow

        session.add(
            SleeveRow(
                id="s2",
                strategy_name="test",
                mode="paper",
                status="running",
                starting_capital=Decimal("10000"),
                current_nav=Decimal("10000"),
                high_water_mark=Decimal("10000"),
                current_cash=Decimal("10000"),
                created_at=datetime(2026, 5, 7, 0, 0, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

        repo = StrategyStateRepo(session)
        repo.upsert("s2", "QQQ", date(2026, 5, 1), Decimal("450"), 1, Decimal("455"))
        session.commit()

        repo.upsert("s2", "QQQ", date(2026, 5, 1), Decimal("450"), 5, Decimal("470"),
                    last_session_date=date(2026, 5, 7))
        session.commit()

        row = repo.get("s2", "QQQ")
        assert row is not None
        assert row.days_held == 5
        assert Decimal(str(row.highest_close_since_entry)) == Decimal("470")
        assert row.last_session_date == date(2026, 5, 7)

    def test_get_by_sleeve_returns_all(self, session: Session) -> None:
        from src.tracking.models import SleeveRow

        session.add(
            SleeveRow(
                id="s3",
                strategy_name="test",
                mode="paper",
                status="running",
                starting_capital=Decimal("10000"),
                current_nav=Decimal("10000"),
                high_water_mark=Decimal("10000"),
                current_cash=Decimal("10000"),
                created_at=datetime(2026, 5, 7, 0, 0, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

        repo = StrategyStateRepo(session)
        repo.upsert("s3", "SPY", date(2026, 5, 1), Decimal("500"), 0, Decimal("500"))
        repo.upsert("s3", "QQQ", date(2026, 5, 2), Decimal("450"), 0, Decimal("450"))
        session.commit()

        rows = repo.get_by_sleeve("s3")
        symbols = {r.symbol for r in rows}
        assert symbols == {"SPY", "QQQ"}

    def test_get_by_sleeve_empty_for_other_sleeve(self, session: Session) -> None:
        from src.tracking.models import SleeveRow

        session.add(
            SleeveRow(
                id="s4",
                strategy_name="test",
                mode="paper",
                status="running",
                starting_capital=Decimal("10000"),
                current_nav=Decimal("10000"),
                high_water_mark=Decimal("10000"),
                current_cash=Decimal("10000"),
                created_at=datetime(2026, 5, 7, 0, 0, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

        repo = StrategyStateRepo(session)
        repo.upsert("s4", "SPY", date(2026, 5, 1), Decimal("500"), 0, Decimal("500"))
        session.commit()

        assert repo.get_by_sleeve("other-sleeve") == []

    def test_delete_removes_entry(self, session: Session) -> None:
        from src.tracking.models import SleeveRow

        session.add(
            SleeveRow(
                id="s5",
                strategy_name="test",
                mode="paper",
                status="running",
                starting_capital=Decimal("10000"),
                current_nav=Decimal("10000"),
                high_water_mark=Decimal("10000"),
                current_cash=Decimal("10000"),
                created_at=datetime(2026, 5, 7, 0, 0, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

        repo = StrategyStateRepo(session)
        repo.upsert("s5", "SPY", date(2026, 5, 1), Decimal("500"), 0, Decimal("500"))
        session.commit()

        repo.delete("s5", "SPY")
        session.commit()

        assert repo.get("s5", "SPY") is None

    def test_delete_noop_for_missing(self, session: Session) -> None:
        repo = StrategyStateRepo(session)
        repo.delete("no-sleeve", "SPY")  # should not raise


# ---------------------------------------------------------------------------
# attribute_fill integration tests
# ---------------------------------------------------------------------------


class TestAttributeFillStrategyState:
    def test_buy_fill_creates_strategy_state(
        self, manager: SleeveManager, session: Session, caps: AccountCapabilities
    ) -> None:
        sleeve_id = _create_sleeve(manager, session)
        manager.attribute_fill(
            sleeve_id=sleeve_id,
            symbol="SPY",
            side="buy",
            qty=Decimal("10"),
            fill_price=Decimal("500.00"),
        )

        row = StrategyStateRepo(session).get(sleeve_id, "SPY")
        assert row is not None
        assert Decimal(str(row.entry_price)) == Decimal("500.00")
        assert Decimal(str(row.highest_close_since_entry)) == Decimal("500.00")
        assert row.days_held == 0
        assert row.entry_date == datetime.now(tz=timezone.utc).date()

    def test_buy_fill_add_to_position_does_not_overwrite_entry_price(
        self, manager: SleeveManager, session: Session
    ) -> None:
        sleeve_id = _create_sleeve(manager, session)

        # First buy at 500
        manager.attribute_fill(
            sleeve_id=sleeve_id, symbol="SPY", side="buy",
            qty=Decimal("10"), fill_price=Decimal("500.00"),
        )
        # Second buy (adding to position) at 520
        manager.attribute_fill(
            sleeve_id=sleeve_id, symbol="SPY", side="buy",
            qty=Decimal("5"), fill_price=Decimal("520.00"),
        )

        row = StrategyStateRepo(session).get(sleeve_id, "SPY")
        assert row is not None
        # Entry price must reflect the FIRST buy, not the addition
        assert Decimal(str(row.entry_price)) == Decimal("500.00")

    def test_full_sell_deletes_strategy_state(
        self, manager: SleeveManager, session: Session
    ) -> None:
        sleeve_id = _create_sleeve(manager, session)

        manager.attribute_fill(
            sleeve_id=sleeve_id, symbol="SPY", side="buy",
            qty=Decimal("10"), fill_price=Decimal("500.00"),
        )
        assert StrategyStateRepo(session).get(sleeve_id, "SPY") is not None

        manager.attribute_fill(
            sleeve_id=sleeve_id, symbol="SPY", side="sell",
            qty=Decimal("10"), fill_price=Decimal("510.00"),
        )
        assert StrategyStateRepo(session).get(sleeve_id, "SPY") is None

    def test_partial_sell_keeps_strategy_state(
        self, manager: SleeveManager, session: Session
    ) -> None:
        sleeve_id = _create_sleeve(manager, session)

        manager.attribute_fill(
            sleeve_id=sleeve_id, symbol="SPY", side="buy",
            qty=Decimal("10"), fill_price=Decimal("500.00"),
        )
        manager.attribute_fill(
            sleeve_id=sleeve_id, symbol="SPY", side="sell",
            qty=Decimal("4"), fill_price=Decimal("510.00"),
        )

        row = StrategyStateRepo(session).get(sleeve_id, "SPY")
        assert row is not None
        assert Decimal(str(row.entry_price)) == Decimal("500.00")

    def test_no_strategy_state_for_no_prior_position_sell(
        self, manager: SleeveManager, session: Session
    ) -> None:
        sleeve_id = _create_sleeve(manager, session)
        # Sell without a prior buy (edge case — short-selling or data error)
        manager.attribute_fill(
            sleeve_id=sleeve_id, symbol="SPY", side="sell",
            qty=Decimal("5"), fill_price=Decimal("500.00"),
        )
        # No strategy_state row should have been created or errored
        assert StrategyStateRepo(session).get(sleeve_id, "SPY") is None
