"""Repositories for risk events, system events, signals, and capital events."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from src.tracking.models import (
    RiskEventRow,
    SignalRow,
    SleeveCapitalEventRow,
    SystemEventRow,
)


class RiskEventRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        timestamp: datetime,
        level: str,
        sleeve_id_nullable: str | None,
        event_type: str,
        severity: str,
        details_json: str | None = None,
    ) -> RiskEventRow:
        row = RiskEventRow(
            timestamp=timestamp,
            level=level,
            sleeve_id_nullable=sleeve_id_nullable,
            event_type=event_type,
            severity=severity,
            details_json=details_json,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_recent(self, limit: int = 100) -> list[RiskEventRow]:
        return (
            self._session.query(RiskEventRow)
            .order_by(RiskEventRow.timestamp.desc())
            .limit(limit)
            .all()
        )


class SystemEventRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        timestamp: datetime,
        event_type: str,
        message: str,
        context_json: str | None = None,
    ) -> SystemEventRow:
        row = SystemEventRow(
            timestamp=timestamp,
            event_type=event_type,
            message=message,
            context_json=context_json,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_recent(self, limit: int = 100) -> list[SystemEventRow]:
        return (
            self._session.query(SystemEventRow)
            .order_by(SystemEventRow.timestamp.desc())
            .limit(limit)
            .all()
        )


class SignalRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        timestamp: datetime,
        sleeve_id: str,
        symbol: str,
        target_weight: Decimal,
        rationale_json: str | None = None,
    ) -> SignalRow:
        row = SignalRow(
            timestamp=timestamp,
            sleeve_id=sleeve_id,
            symbol=symbol,
            target_weight=target_weight,
            rationale_json=rationale_json,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_recent(self, sleeve_id: str, limit: int = 100) -> list[SignalRow]:
        return (
            self._session.query(SignalRow)
            .filter(SignalRow.sleeve_id == sleeve_id)
            .order_by(SignalRow.timestamp.desc())
            .limit(limit)
            .all()
        )


class CapitalEventRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        sleeve_id: str,
        timestamp: datetime,
        event_type: str,
        amount: Decimal,
        reason: str | None = None,
    ) -> SleeveCapitalEventRow:
        row = SleeveCapitalEventRow(
            sleeve_id=sleeve_id,
            timestamp=timestamp,
            event_type=event_type,
            amount=amount,
            reason=reason,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_recent(self, sleeve_id: str, limit: int = 100) -> list[SleeveCapitalEventRow]:
        return (
            self._session.query(SleeveCapitalEventRow)
            .filter(SleeveCapitalEventRow.sleeve_id == sleeve_id)
            .order_by(SleeveCapitalEventRow.timestamp.desc())
            .limit(limit)
            .all()
        )
