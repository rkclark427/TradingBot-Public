from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from src.strategies.base import (
    MarketDataView,
    Position,
    Strategy,
    StrategyCapabilities,
    Target,
)

logger = logging.getLogger(__name__)

# Fixed universe per spec — changing this is a code change, not a config change.
UNIVERSE: frozenset[str] = frozenset({
    # Sector ETFs
    "XLE", "XLK", "XLF", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    # Broad market
    "SPY", "QQQ", "IWM",
    # International
    "EFA", "EEM",
})


class MomentumContinuation(Strategy):
    """20-day closing-price breakout strategy on a fixed 16-ETF universe.

    Long-only. Entry on new 20-day closing high. Exits via hard stop (4%),
    trailing stop (5% from highest close since entry), or time stop (10 days).
    Position sizing targets 1% NAV risk per trade at a 4% hard stop → 25% NAV
    per position. Max 5 concurrent positions; max 100% total deployment.

    The drawdown circuit breaker (HALTED status) is enforced by the orchestrator,
    not by this class. When halted, the orchestrator will only submit exit orders.
    """

    name = "momentum_continuation"

    capabilities = StrategyCapabilities(
        asset_classes=frozenset({"us_equity"}),
        requires_shorting=False,
        requires_options=False,
        requires_fractional=True,
        min_cash_buffer_pct=0.0,
        rebalance_cadence="daily",
        typical_holding_period_days=7,
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
        if nav <= 0.0:
            return []

        lookback_days: int = int(params.get("lookback_days", 20))
        hard_stop_pct: float = float(params.get("hard_stop_pct", 0.04))
        trailing_stop_pct: float = float(params.get("trailing_stop_pct", 0.05))
        time_stop_days: int = int(params.get("time_stop_days", 10))
        risk_per_trade_pct: float = float(params.get("risk_per_trade_pct", 0.01))
        max_position_pct: float = float(params.get("max_position_pct", 0.25))
        max_concurrent_positions: int = int(params.get("max_concurrent_positions", 5))
        max_total_deployment_pct: float = float(params.get("max_total_deployment_pct", 1.00))

        ps = position_state or {}
        tradeable = set(universe)
        targets: list[Target] = []

        # -----------------------------------------------------------------
        # 1. Evaluate existing positions — hold or exit?
        # -----------------------------------------------------------------
        kept_symbols: set[str] = set()

        for symbol, pos in positions.items():
            state = ps.get(symbol, {})
            entry_price = float(state.get("entry_price", pos.avg_cost))
            days_held = int(state.get("days_held", 0))
            highest_close = float(state.get("highest_close_since_entry", entry_price))

            try:
                current_price = float(market_data.get_latest_price(symbol))
            except Exception:
                logger.warning("Cannot get price for held position %s; holding", symbol)
                # Hold at cost-basis weight to avoid accidental liquidation
                weight = float(pos.qty * pos.avg_cost) / nav
                targets.append(Target(symbol=symbol, target_weight=weight,
                                      rationale={"reason": "hold_no_price"}))
                kept_symbols.add(symbol)
                continue

            hard_stop_level = entry_price * (1.0 - hard_stop_pct)
            trailing_stop_level = highest_close * (1.0 - trailing_stop_pct)

            exit_reason: str | None = None
            if current_price < hard_stop_level:
                exit_reason = "hard_stop"
            elif current_price < trailing_stop_level:
                exit_reason = "trailing_stop"
            elif days_held >= time_stop_days:
                exit_reason = "time_stop"

            if exit_reason:
                logger.info(
                    "Exit signal symbol=%s reason=%s days_held=%d "
                    "current=%.4f hard_stop=%.4f trailing_stop=%.4f",
                    symbol, exit_reason, days_held,
                    current_price, hard_stop_level, trailing_stop_level,
                )
                targets.append(Target(symbol=symbol, target_weight=0.0,
                                      rationale={"reason": exit_reason}))
            else:
                current_weight = float(pos.qty) * current_price / nav
                targets.append(Target(symbol=symbol, target_weight=current_weight,
                                      rationale={"reason": "hold"}))
                kept_symbols.add(symbol)

        # -----------------------------------------------------------------
        # 2. Entry signals — scan universe for 20-day closing-price breakouts
        # -----------------------------------------------------------------
        open_count = len(kept_symbols)
        deployed = sum(t.target_weight for t in targets if t.target_weight > 0.0)

        # Per-trade weight: risk_per_trade_pct / hard_stop_pct (= 0.01/0.04 = 0.25)
        entry_weight = min(risk_per_trade_pct / hard_stop_pct, max_position_pct)

        for symbol in sorted(UNIVERSE):  # sorted for deterministic signal ordering
            if open_count >= max_concurrent_positions:
                break
            if symbol in positions:
                continue
            if symbol not in tradeable:
                continue
            if deployed + entry_weight > max_total_deployment_pct + 1e-9:
                logger.debug("Deployment cap reached; skipping entry for %s", symbol)
                continue

            try:
                bars = market_data.get_bars(symbol, lookback_days + 1, as_of)
            except Exception:
                logger.warning("Failed to get bars for %s; skipping entry", symbol,
                               exc_info=True)
                continue

            if bars is None or len(bars) < lookback_days + 1:
                logger.debug("Insufficient history for %s (%s bars, need %d)",
                             symbol, len(bars) if bars is not None else 0, lookback_days + 1)
                continue

            closes = bars["close"].to_numpy()
            today_close = closes[-1]
            # Prior lookback_days closes, excluding today
            prior_high = float(closes[-(lookback_days + 1):-1].max())

            if today_close <= prior_high:
                continue

            logger.info(
                "Entry signal symbol=%s today_close=%.4f prior_%d_day_high=%.4f",
                symbol, today_close, lookback_days, prior_high,
            )
            targets.append(Target(
                symbol=symbol,
                target_weight=entry_weight,
                rationale={
                    "reason": "breakout",
                    "today_close": today_close,
                    "prior_high": prior_high,
                    "lookback_days": lookback_days,
                },
            ))
            deployed += entry_weight
            open_count += 1

        return targets
