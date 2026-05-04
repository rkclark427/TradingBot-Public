"""Repository for sleeve rows."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from src.tracking.models import SleeveRow


class SleeveRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        id: str,
        strategy_name: str,
        mode: str,
        status: str,
        starting_capital: Decimal,
        current_nav: Decimal,
        current_cash: Decimal,
        high_water_mark: Decimal,
        created_at: datetime,
        parameters_json: str | None = None,
    ) -> SleeveRow:
        row = SleeveRow(
            id=id,
            strategy_name=strategy_name,
            mode=mode,
            status=status,
            starting_capital=starting_capital,
            current_nav=current_nav,
            current_cash=current_cash,
            high_water_mark=high_water_mark,
            created_at=created_at,
            parameters_json=parameters_json,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get(self, sleeve_id: str) -> SleeveRow | None:
        return self._session.get(SleeveRow, sleeve_id)

    def list(self) -> list[SleeveRow]:
        return list(self._session.query(SleeveRow).all())

    def update_status(self, sleeve_id: str, status: str) -> SleeveRow | None:
        row = self.get(sleeve_id)
        if row is None:
            return None
        row.status = status
        self._session.flush()
        return row

    def update_nav(
        self,
        sleeve_id: str,
        current_nav: Decimal,
        current_cash: Decimal,
        high_water_mark: Decimal,
    ) -> SleeveRow | None:
        row = self.get(sleeve_id)
        if row is None:
            return None
        row.current_nav = current_nav
        row.current_cash = current_cash
        row.high_water_mark = high_water_mark
        self._session.flush()
        return row
