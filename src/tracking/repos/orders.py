"""Repositories for intended orders, net orders, and the mapping table."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from src.tracking.models import IntendedOrderRow, IntendedToNetRow, NetOrderRow


class IntendedOrderRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        timestamp: datetime,
        sleeve_id: str,
        symbol: str,
        side: str,
        qty: Decimal,
        limit_price: Decimal | None,
        status: str,
    ) -> IntendedOrderRow:
        row = IntendedOrderRow(
            timestamp=timestamp,
            sleeve_id=sleeve_id,
            symbol=symbol,
            side=side,
            qty=qty,
            limit_price=limit_price,
            status=status,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get(self, order_id: int) -> IntendedOrderRow | None:
        return self._session.get(IntendedOrderRow, order_id)

    def update_status(self, order_id: int, status: str) -> IntendedOrderRow | None:
        row = self.get(order_id)
        if row is None:
            return None
        row.status = status
        self._session.flush()
        return row


class NetOrderRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        timestamp: datetime,
        mode: str,
        symbol: str,
        side: str,
        net_qty: Decimal,
        limit_price: Decimal | None,
        alpaca_id: str | None,
        status: str,
    ) -> NetOrderRow:
        row = NetOrderRow(
            timestamp=timestamp,
            mode=mode,
            symbol=symbol,
            side=side,
            net_qty=net_qty,
            limit_price=limit_price,
            alpaca_id=alpaca_id,
            status=status,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get(self, order_id: int) -> NetOrderRow | None:
        return self._session.get(NetOrderRow, order_id)

    def update_status(self, order_id: int, status: str) -> NetOrderRow | None:
        row = self.get(order_id)
        if row is None:
            return None
        row.status = status
        self._session.flush()
        return row


class IntendedToNetRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        intended_order_id: int,
        net_order_id: int,
        allocated_qty: Decimal,
    ) -> IntendedToNetRow:
        row = IntendedToNetRow(
            intended_order_id=intended_order_id,
            net_order_id=net_order_id,
            allocated_qty=allocated_qty,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get_by_net_order(self, net_order_id: int) -> list[IntendedToNetRow]:
        return (
            self._session.query(IntendedToNetRow)
            .filter(IntendedToNetRow.net_order_id == net_order_id)
            .all()
        )
