"""Repository for the heartbeat single-row table."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from src.tracking.models import HeartbeatRow

_HEARTBEAT_ID = 1


class HeartbeatRepo:
    def __init__(self, session: Session) -> None:
        self._session = session

    def update(self, timestamp: datetime) -> HeartbeatRow:
        row = self._session.get(HeartbeatRow, _HEARTBEAT_ID)
        if row is None:
            row = HeartbeatRow(id=_HEARTBEAT_ID, timestamp=timestamp)
            self._session.add(row)
        else:
            row.timestamp = timestamp
        self._session.flush()
        return row

    def get_latest(self) -> HeartbeatRow | None:
        return self._session.get(HeartbeatRow, _HEARTBEAT_ID)
