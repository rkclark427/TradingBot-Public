from __future__ import annotations

from datetime import datetime
from typing import Any

from src.strategies.base import (
    MarketDataView,
    Position,
    Strategy,
    StrategyCapabilities,
    Target,
)


class BuyAndHoldSPY(Strategy):
    name = "buy_and_hold"

    capabilities = StrategyCapabilities(
        asset_classes=frozenset({"us_equity"}),
        requires_shorting=False,
        requires_options=False,
        requires_fractional=True,
        min_cash_buffer_pct=0.0,
        rebalance_cadence="daily",
        typical_holding_period_days=3650,
    )

    def generate_targets(
        self,
        params: dict[str, Any],
        nav: float,
        cash: float,
        positions: dict[str, Position],
        market_data: MarketDataView,
        universe: list[str],
        as_of: datetime,
        position_state: dict[str, dict[str, Any]] | None = None,
    ) -> list[Target]:
        symbol = params.get("symbol", "SPY")

        if symbol not in positions:
            return [Target(symbol=symbol, target_weight=1.0, rationale={"reason": "initial allocation"})]

        return [
            Target(
                symbol=symbol,
                target_weight=1.0,
                rationale={"reason": "hold"},
            )
        ]
