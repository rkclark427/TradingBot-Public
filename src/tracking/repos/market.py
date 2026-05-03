"""Repositories for asset universe and account capabilities."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from src.tracking.models import AccountCapabilitiesRow, AssetUniverseRow


class AssetUniverseRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        *,
        symbol: str,
        tradable: bool,
        fractionable: bool,
        marginable: bool,
        shortable: bool,
        etb: bool,
        last_refreshed: datetime,
    ) -> AssetUniverseRow:
        row = self._session.get(AssetUniverseRow, symbol)
        if row is None:
            row = AssetUniverseRow(
                symbol=symbol,
                tradable=tradable,
                fractionable=fractionable,
                marginable=marginable,
                shortable=shortable,
                etb=etb,
                last_refreshed=last_refreshed,
            )
            self._session.add(row)
        else:
            row.tradable = tradable
            row.fractionable = fractionable
            row.marginable = marginable
            row.shortable = shortable
            row.etb = etb
            row.last_refreshed = last_refreshed
        self._session.flush()
        return row

    def get_all(self) -> list[AssetUniverseRow]:
        return list(self._session.query(AssetUniverseRow).all())


class AccountCapabilitiesRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        *,
        snapshot_date: date,
        capability_key: str,
        capability_value: str,
    ) -> AccountCapabilitiesRow:
        row = self._session.get(AccountCapabilitiesRow, (snapshot_date, capability_key))
        if row is None:
            row = AccountCapabilitiesRow(
                snapshot_date=snapshot_date,
                capability_key=capability_key,
                capability_value=capability_value,
            )
            self._session.add(row)
        else:
            row.capability_value = capability_value
        self._session.flush()
        return row

    def get_all(self, snapshot_date: date) -> list[AccountCapabilitiesRow]:
        return (
            self._session.query(AccountCapabilitiesRow)
            .filter(AccountCapabilitiesRow.snapshot_date == snapshot_date)
            .all()
        )
