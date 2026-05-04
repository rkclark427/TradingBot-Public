from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from src.data.alpaca_client import LiveClient, PaperClient
from src.queries.kill_switch import is_kill_switch_active
from src.sleeves.manager import SleeveManager
from src.tracking.repos.heartbeat import HeartbeatRepo


def get_status(
    session: Session,
    client: PaperClient | LiveClient | None,
    manager: SleeveManager,
) -> dict[str, Any]:
    heartbeat = HeartbeatRepo(session).get_latest()
    sleeves = manager.list_sleeves()

    account: dict[str, Any] | None = None
    if client is not None:
        try:
            info = client.get_account()
            account = {
                "buying_power": str(info.buying_power),
                "cash": str(info.cash),
                "portfolio_value": str(info.portfolio_value),
                "shorting_enabled": info.shorting_enabled,
                "fractional_trading": info.fractional_trading,
                "pattern_day_trader": info.pattern_day_trader,
                "trading_blocked": info.trading_blocked,
            }
        except Exception:
            account = {"error": "failed to fetch account info"}

    return {
        "kill_switch_active": is_kill_switch_active(),
        "last_heartbeat": heartbeat.timestamp.isoformat() if heartbeat else None,
        "sleeve_counts": {
            "total": len(sleeves),
            "running": sum(1 for s in sleeves if s.status.value == "running"),
            "paused": sum(1 for s in sleeves if s.status.value == "paused"),
            "stopping": sum(1 for s in sleeves if s.status.value == "stopping"),
            "stopped": sum(1 for s in sleeves if s.status.value == "stopped"),
        },
        "account": account,
    }
