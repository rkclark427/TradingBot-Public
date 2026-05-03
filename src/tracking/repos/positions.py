"""Repository for sleeve positions."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from src.tracking.models import PositionRow


class PositionRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        *,
        sleeve_id: str,
        symbol: str,
        qty: Decimal,
        avg_cost: Decimal,
    ) -> PositionRow:
        row = self._session.get(PositionRow, (sleeve_id, symbol))
        if row is None:
            row = PositionRow(
                sleeve_id=sleeve_id,
                symbol=symbol,
                qty=qty,
                avg_cost=avg_cost,
            )
            self._session.add(row)
        else:
            row.qty = qty
            row.avg_cost = avg_cost
        self._session.flush()
        return row

    def get(self, sleeve_id: str, symbol: str) -> PositionRow | None:
        return self._session.get(PositionRow, (sleeve_id, symbol))

    def list_by_sleeve(self, sleeve_id: str) -> list[PositionRow]:
        return (
            self._session.query(PositionRow)
            .filter(PositionRow.sleeve_id == sleeve_id)
            .all()
        )
