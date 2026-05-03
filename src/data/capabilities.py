from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from src.data.alpaca_client import AccountInfo, PaperClient, LiveClient
from src.sleeves.types import AccountCapabilities
from src.tracking.repos.market import AccountCapabilitiesRepo


def discover_account_capabilities(
    client: PaperClient | LiveClient,
) -> AccountCapabilities:
    info: AccountInfo = client.get_account()
    return _info_to_capabilities(info)


def _info_to_capabilities(info: AccountInfo) -> AccountCapabilities:
    return AccountCapabilities(
        asset_classes=frozenset({"us_equity"}),
        options_trading_level=info.options_trading_level,
        fractional_shares_enabled=info.fractional_trading,
        shorting_enabled=info.shorting_enabled,
        is_pdt=info.pattern_day_trader,
        buying_power=info.buying_power,
        cash=info.cash,
        as_of=datetime.now(tz=timezone.utc),
    )


def persist_capabilities(
    caps: AccountCapabilities,
    session: Session,
    snapshot_date: date | None = None,
) -> None:
    """Write capability snapshot to account_capabilities table."""
    if snapshot_date is None:
        snapshot_date = caps.as_of.date()

    repo = AccountCapabilitiesRepo(session)
    rows = {
        "asset_classes": json.dumps(sorted(caps.asset_classes)),
        "options_trading_level": str(caps.options_trading_level),
        "fractional_shares_enabled": str(caps.fractional_shares_enabled),
        "shorting_enabled": str(caps.shorting_enabled),
        "is_pdt": str(caps.is_pdt),
        "buying_power": str(caps.buying_power),
        "cash": str(caps.cash),
    }
    for key, value in rows.items():
        repo.upsert(snapshot_date=snapshot_date, capability_key=key, capability_value=value)
    session.commit()


def load_capabilities_from_db(
    session: Session,
    snapshot_date: date | None = None,
) -> AccountCapabilities | None:
    """Reconstruct AccountCapabilities from the most recent DB snapshot."""
    repo = AccountCapabilitiesRepo(session)
    all_rows = repo.get_all()
    if not all_rows:
        return None

    if snapshot_date is not None:
        rows = [r for r in all_rows if r.snapshot_date == snapshot_date]
    else:
        latest = max(r.snapshot_date for r in all_rows)
        rows = [r for r in all_rows if r.snapshot_date == latest]

    if not rows:
        return None

    data = {r.capability_key: r.capability_value for r in rows}
    snapshot_dt = datetime.combine(rows[0].snapshot_date, datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    return AccountCapabilities(
        asset_classes=frozenset(json.loads(data.get("asset_classes", '["us_equity"]'))),
        options_trading_level=int(data.get("options_trading_level", "0")),
        fractional_shares_enabled=data.get("fractional_shares_enabled", "True") == "True",
        shorting_enabled=data.get("shorting_enabled", "False") == "True",
        is_pdt=data.get("is_pdt", "False") == "True",
        buying_power=Decimal(data.get("buying_power", "0")),
        cash=Decimal(data.get("cash", "0")),
        as_of=snapshot_dt,
    )
