from __future__ import annotations

from sqlalchemy.orm import Session

from src.sleeves.manager import SleeveManager
from src.sleeves.types import Sleeve
from src.tracking.repos.events import CapitalEventRepo
from src.tracking.repos.positions import PositionRepo


def list_sleeves(manager: SleeveManager, status_filter: str | None = None) -> list[Sleeve]:
    return manager.list_sleeves(status_filter=status_filter)


def get_sleeve(manager: SleeveManager, sleeve_id: str) -> Sleeve | None:
    return manager.get_sleeve(sleeve_id)


def get_sleeve_positions(session: Session, sleeve_id: str) -> list[dict]:
    rows = PositionRepo(session).list_by_sleeve(sleeve_id)
    return [
        {"symbol": r.symbol, "qty": str(r.qty), "avg_cost": str(r.avg_cost)}
        for r in rows
    ]


def get_sleeve_capital_events(session: Session, sleeve_id: str, limit: int = 10) -> list[dict]:
    rows = CapitalEventRepo(session).list_recent(sleeve_id=sleeve_id, limit=limit)
    return [
        {
            "timestamp": r.timestamp.isoformat(),
            "event_type": r.event_type,
            "amount": str(r.amount),
            "reason": r.reason,
        }
        for r in rows
    ]
