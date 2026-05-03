"""Repositories for fills and sleeve-level fill attribution."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from src.tracking.models import FillRow, SleeveFillRow


class FillRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        net_order_id: int,
        timestamp: datetime,
        qty: Decimal,
        fill_price: Decimal,
        fees: Decimal,
    ) -> FillRow:
        row = FillRow(
            net_order_id=net_order_id,
            timestamp=timestamp,
            qty=qty,
            fill_price=fill_price,
            fees=fees,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get_by_net_order(self, net_order_id: int) -> list[FillRow]:
        return (
            self._session.query(FillRow)
            .filter(FillRow.net_order_id == net_order_id)
            .all()
        )


class SleeveFillRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        fill_id: int,
        sleeve_id: str,
        allocated_qty: Decimal,
        allocated_price: Decimal,
    ) -> SleeveFillRow:
        row = SleeveFillRow(
            fill_id=fill_id,
            sleeve_id=sleeve_id,
            allocated_qty=allocated_qty,
            allocated_price=allocated_price,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get_by_net_order(self, net_order_id: int) -> list[SleeveFillRow]:
        return (
            self._session.query(SleeveFillRow)
            .join(FillRow, SleeveFillRow.fill_id == FillRow.id)
            .filter(FillRow.net_order_id == net_order_id)
            .all()
        )
