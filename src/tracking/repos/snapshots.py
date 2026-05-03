"""Repositories for NAV and account snapshots."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from src.tracking.models import AccountSnapshotRow, SleeveNavSnapshotRow


class NavSnapshotRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def write_snapshot(
        self,
        *,
        snapshot_date: date,
        sleeve_id: str,
        nav: Decimal,
        cash: Decimal,
        position_value: Decimal,
        realized_pnl_today: Decimal,
    ) -> SleeveNavSnapshotRow:
        row = self._session.get(SleeveNavSnapshotRow, (snapshot_date, sleeve_id))
        if row is None:
            row = SleeveNavSnapshotRow(
                date=snapshot_date,
                sleeve_id=sleeve_id,
                nav=nav,
                cash=cash,
                position_value=position_value,
                realized_pnl_today=realized_pnl_today,
            )
            self._session.add(row)
        else:
            row.nav = nav
            row.cash = cash
            row.position_value = position_value
            row.realized_pnl_today = realized_pnl_today
        self._session.flush()
        return row

    def get_latest(self, sleeve_id: str) -> SleeveNavSnapshotRow | None:
        return (
            self._session.query(SleeveNavSnapshotRow)
            .filter(SleeveNavSnapshotRow.sleeve_id == sleeve_id)
            .order_by(SleeveNavSnapshotRow.date.desc())
            .first()
        )


class AccountSnapshotRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def write_snapshot(
        self,
        *,
        snapshot_date: date,
        mode: str,
        total_equity: Decimal,
        total_cash: Decimal,
        unallocated_cash: Decimal,
    ) -> AccountSnapshotRow:
        row = self._session.get(AccountSnapshotRow, (snapshot_date, mode))
        if row is None:
            row = AccountSnapshotRow(
                date=snapshot_date,
                mode=mode,
                total_equity=total_equity,
                total_cash=total_cash,
                unallocated_cash=unallocated_cash,
            )
            self._session.add(row)
        else:
            row.total_equity = total_equity
            row.total_cash = total_cash
            row.unallocated_cash = unallocated_cash
        self._session.flush()
        return row

    def get_latest(self, mode: str) -> AccountSnapshotRow | None:
        return (
            self._session.query(AccountSnapshotRow)
            .filter(AccountSnapshotRow.mode == mode)
            .order_by(AccountSnapshotRow.date.desc())
            .first()
        )
