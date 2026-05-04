from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.data.alpaca_client import PaperClient, LiveClient
from src.tracking.repos.market import AssetUniverseRepo


def refresh_asset_universe(
    client: PaperClient | LiveClient,
    session: Session,
    asset_class: str = "us_equity",
) -> int:
    """Pull the full asset list from Alpaca and persist to asset_universe table.

    Returns the number of assets upserted.
    """
    assets = client.get_assets(asset_class=asset_class)
    repo = AssetUniverseRepo(session)
    now = datetime.now(tz=timezone.utc)

    for asset in assets:
        repo.upsert(
            symbol=asset.symbol,
            tradable=asset.tradable,
            fractionable=asset.fractionable,
            marginable=asset.marginable,
            shortable=asset.shortable,
            etb=asset.easy_to_borrow,
            last_refreshed=now,
        )

    session.commit()
    return len(assets)


def get_tradable_symbols(
    session: Session,
    *,
    require_fractionable: bool = False,
    require_shortable: bool = False,
    require_etb: bool = False,
) -> list[str]:
    """Return symbols from the cached universe that meet the given constraints."""
    repo = AssetUniverseRepo(session)
    all_assets = repo.get_all()

    results: list[str] = []
    for row in all_assets:
        if not row.tradable:
            continue
        if require_fractionable and not row.fractionable:
            continue
        if require_shortable and not row.shortable:
            continue
        if require_etb and not row.etb:
            continue
        results.append(row.symbol)

    return sorted(results)
