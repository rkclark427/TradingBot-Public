"""Repository for per-sleeve, per-symbol strategy position state.

Stores the entry_price, days_held, and highest_close_since_entry that
stateful strategies (e.g. MomentumContinuation) need to evaluate stops.
The orchestrator reads this table before each generate_targets call and
writes to it after fills are attributed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from src.tracking.models import StrategyStateRow


class StrategyStateRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_sleeve(self, sleeve_id: str) -> list[StrategyStateRow]:
        return (
            self._session.query(StrategyStateRow)
            .filter(StrategyStateRow.sleeve_id == sleeve_id)
            .all()
        )

    def get(self, sleeve_id: str, symbol: str) -> StrategyStateRow | None:
        return self._session.get(StrategyStateRow, (sleeve_id, symbol))

    def upsert(
        self,
        sleeve_id: str,
        symbol: str,
        entry_date: date,
        entry_price: Decimal,
        days_held: int,
        highest_close_since_entry: Decimal,
        last_session_date: date | None = None,
    ) -> None:
        existing = self.get(sleeve_id, symbol)
        if existing is None:
            self._session.add(
                StrategyStateRow(
                    sleeve_id=sleeve_id,
                    symbol=symbol,
                    entry_date=entry_date,
                    entry_price=entry_price,
                    days_held=days_held,
                    highest_close_since_entry=highest_close_since_entry,
                    last_session_date=last_session_date,
                )
            )
        else:
            existing.entry_date = entry_date
            existing.entry_price = entry_price
            existing.days_held = days_held
            existing.highest_close_since_entry = highest_close_since_entry
            existing.last_session_date = last_session_date

    def delete(self, sleeve_id: str, symbol: str) -> None:
        existing = self.get(sleeve_id, symbol)
        if existing is not None:
            self._session.delete(existing)
