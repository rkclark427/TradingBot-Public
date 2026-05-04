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

    def find_error_for_retry(
        self,
        date_str: str,
        symbol: str,
        mode: str,
    ) -> NetOrderRow | None:
        """Find the oldest error-status net_order for this symbol/mode/date.

        Used on retry to reuse the original row and its client_order_id, preventing
        double-submission when the previous attempt may have reached Alpaca but
        the response was lost to a network error.
        """
        from datetime import datetime, timedelta, timezone

        date = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
        return (
            self._session.query(NetOrderRow)
            .filter(
                NetOrderRow.symbol == symbol,
                NetOrderRow.mode == mode,
                NetOrderRow.status == "error",
                NetOrderRow.timestamp >= date,
                NetOrderRow.timestamp < date + timedelta(days=1),
            )
            .order_by(NetOrderRow.id.asc())
            .first()
        )

    def list_submitted(self) -> list[NetOrderRow]:
        """Return all net_orders currently in 'submitted' state."""
        return list(
            self._session.query(NetOrderRow)
            .filter(NetOrderRow.status == "submitted")
            .all()
        )


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

    def delete_by_net_order(self, net_order_id: int) -> int:
        """Delete all mappings for a net order. Returns count deleted."""
        return (
            self._session.query(IntendedToNetRow)
            .filter(IntendedToNetRow.net_order_id == net_order_id)
            .delete()
        )
