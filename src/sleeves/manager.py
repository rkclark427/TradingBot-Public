from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from src.sleeves.types import (
    AccountCapabilities,
    CapabilityMismatchError,
    Mode,
    Sleeve,
    SleeveRiskConfig,
    SleeveStatus,
    validate_strategy_against_account,
)
from src.strategies.base import Strategy
from src.strategies.buy_and_hold import BuyAndHoldSPY
from src.tracking.repos.events import CapitalEventRepo
from src.tracking.repos.positions import PositionRepo
from src.tracking.repos.sleeves import SleeveRepo
from src.tracking.models import SleeveRow

_DEFAULT_REGISTRY: dict[str, Strategy] = {
    "buy_and_hold": BuyAndHoldSPY(),
}


class SleeveNotFoundError(Exception):
    pass


class InvalidTransitionError(Exception):
    pass


class SleeveManager:
    def __init__(
        self,
        session: Session,
        account_capabilities: AccountCapabilities,
        strategy_registry: dict[str, Strategy] | None = None,
    ) -> None:
        self._session = session
        self._account_caps = account_capabilities
        self._strategies = strategy_registry if strategy_registry is not None else _DEFAULT_REGISTRY

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_sleeve(
        self,
        strategy_name: str,
        mode: str,
        starting_capital: Decimal,
        params: dict[str, Any],
        sleeve_id: str | None = None,
        managed: bool = False,
    ) -> Sleeve:
        strategy = self._strategies.get(strategy_name)
        if strategy is None:
            raise ValueError(f"Unknown strategy: {strategy_name!r}")

        validate_strategy_against_account(strategy.capabilities, self._account_caps)

        sid = sleeve_id or str(uuid4())
        now = datetime.now(tz=timezone.utc)

        sleeve_repo = SleeveRepo(self._session)
        row = sleeve_repo.create(
            id=sid,
            strategy_name=strategy_name,
            mode=mode,
            status=SleeveStatus.RUNNING.value,
            starting_capital=starting_capital,
            current_nav=starting_capital,
            current_cash=starting_capital,
            high_water_mark=starting_capital,
            created_at=now,
            parameters_json=json.dumps(params),
        )

        CapitalEventRepo(self._session).create(
            sleeve_id=sid,
            timestamp=now,
            event_type="deposit",
            amount=starting_capital,
            reason="initial allocation",
        )

        self._session.commit()
        return _row_to_sleeve(row, managed=managed)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get_sleeve(self, sleeve_id: str) -> Sleeve | None:
        row = SleeveRepo(self._session).get(sleeve_id)
        if row is None:
            return None
        return _row_to_sleeve(row)

    def list_sleeves(self, status_filter: str | None = None) -> list[Sleeve]:
        rows = SleeveRepo(self._session).list()
        sleeves = [_row_to_sleeve(r) for r in rows]
        if status_filter is not None:
            sleeves = [s for s in sleeves if s.status.value == status_filter]
        return sleeves

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def pause_sleeve(self, sleeve_id: str) -> Sleeve:
        return self._transition(sleeve_id, SleeveStatus.PAUSED, allowed_from={SleeveStatus.RUNNING})

    def resume_sleeve(self, sleeve_id: str) -> Sleeve:
        return self._transition(sleeve_id, SleeveStatus.RUNNING, allowed_from={SleeveStatus.PAUSED})

    def stop_sleeve(self, sleeve_id: str) -> Sleeve:
        return self._transition(
            sleeve_id,
            SleeveStatus.STOPPING,
            allowed_from={SleeveStatus.RUNNING, SleeveStatus.PAUSED},
        )

    def _transition(
        self,
        sleeve_id: str,
        new_status: SleeveStatus,
        allowed_from: set[SleeveStatus],
    ) -> Sleeve:
        row = SleeveRepo(self._session).get(sleeve_id)
        if row is None:
            raise SleeveNotFoundError(sleeve_id)
        current = SleeveStatus(row.status)
        if current not in allowed_from:
            raise InvalidTransitionError(
                f"Cannot transition sleeve {sleeve_id!r} from {current.value!r} to {new_status.value!r}"
            )
        SleeveRepo(self._session).update_status(sleeve_id, new_status.value)
        self._session.commit()
        updated = SleeveRepo(self._session).get(sleeve_id)
        assert updated is not None
        return _row_to_sleeve(updated)

    # ------------------------------------------------------------------
    # NAV computation
    # ------------------------------------------------------------------

    def compute_nav(self, sleeve_id: str, current_prices: dict[str, Decimal]) -> Decimal:
        """Compute current NAV: sleeve cash + mark-to-market position value."""
        row = SleeveRepo(self._session).get(sleeve_id)
        if row is None:
            raise SleeveNotFoundError(sleeve_id)

        positions = PositionRepo(self._session).list_by_sleeve(sleeve_id)
        position_value = sum(
            pos.qty * current_prices.get(pos.symbol, pos.avg_cost)
            for pos in positions
        )
        return Decimal(str(row.current_cash)) + Decimal(str(position_value))

    # ------------------------------------------------------------------
    # Fill attribution
    # ------------------------------------------------------------------

    def attribute_fill(
        self,
        sleeve_id: str,
        symbol: str,
        side: str,
        qty: Decimal,
        fill_price: Decimal,
        fees: Decimal = Decimal("0"),
    ) -> None:
        """Update sleeve position, cash, and NAV after a fill."""
        row = SleeveRepo(self._session).get(sleeve_id)
        if row is None:
            raise SleeveNotFoundError(sleeve_id)

        position_repo = PositionRepo(self._session)
        existing = position_repo.get(sleeve_id, symbol)

        if side == "buy":
            new_qty = (existing.qty if existing else Decimal("0")) + qty
            if existing:
                total_cost = existing.avg_cost * existing.qty + fill_price * qty
                new_avg_cost = total_cost / new_qty
            else:
                new_avg_cost = fill_price
            position_repo.upsert(sleeve_id=sleeve_id, symbol=symbol, qty=new_qty, avg_cost=new_avg_cost)
            cash_delta = -(qty * fill_price) - fees
        else:
            current_qty = existing.qty if existing else Decimal("0")
            new_qty = current_qty - qty
            avg_cost = existing.avg_cost if existing else fill_price
            if new_qty <= 0:
                position_repo.upsert(sleeve_id=sleeve_id, symbol=symbol, qty=Decimal("0"), avg_cost=avg_cost)
            else:
                position_repo.upsert(sleeve_id=sleeve_id, symbol=symbol, qty=new_qty, avg_cost=avg_cost)
            cash_delta = (qty * fill_price) - fees

        new_cash = Decimal(str(row.current_cash)) + cash_delta
        new_nav = Decimal(str(row.current_nav)) + cash_delta
        new_hwm = max(Decimal(str(row.high_water_mark)), new_nav)

        SleeveRepo(self._session).update_nav(
            sleeve_id=sleeve_id,
            current_nav=new_nav,
            current_cash=new_cash,
            high_water_mark=new_hwm,
        )
        self._session.commit()

    # ------------------------------------------------------------------
    # Capital events
    # ------------------------------------------------------------------

    def record_capital_event(
        self,
        sleeve_id: str,
        event_type: str,
        amount: Decimal,
        reason: str | None = None,
    ) -> None:
        if SleeveRepo(self._session).get(sleeve_id) is None:
            raise SleeveNotFoundError(sleeve_id)
        CapitalEventRepo(self._session).create(
            sleeve_id=sleeve_id,
            timestamp=datetime.now(tz=timezone.utc),
            event_type=event_type,
            amount=amount,
            reason=reason,
        )
        self._session.commit()


# ------------------------------------------------------------------
# Conversion helper
# ------------------------------------------------------------------

def _row_to_sleeve(row: SleeveRow, managed: bool = False) -> Sleeve:
    from uuid import UUID
    return Sleeve(
        id=UUID(row.id),
        strategy_name=row.strategy_name,
        mode=Mode(row.mode),
        status=SleeveStatus(row.status),
        starting_capital=Decimal(str(row.starting_capital)),
        current_nav=Decimal(str(row.current_nav)),
        high_water_mark=Decimal(str(row.high_water_mark)),
        parameters=json.loads(row.parameters_json) if row.parameters_json else {},
        risk=SleeveRiskConfig(),
        managed=managed,
        created_at=row.created_at,
        last_signaled_at=None,
    )
