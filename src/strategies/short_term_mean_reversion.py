from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from src.strategies.base import (
    MarketDataView,
    Position,
    Strategy,
    StrategyCapabilities,
    Target,
)

logger = logging.getLogger(__name__)

# Fixed S&P 100 constituents continuously traded 2018-2026.
# Excludes stocks that joined/left the S&P 100 mid-window to avoid survivorship bias.
UNIVERSE: list[str] = [
    # Technology
    "AAPL", "MSFT", "GOOGL", "GOOG", "META", "NVDA", "AVGO", "TXN", "QCOM", "IBM",
    "ORCL", "ACN", "CSCO", "INTC", "AMD",
    # Consumer Discretionary
    "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "TGT", "LOW", "BKNG", "F",
    # Consumer Staples
    "WMT", "PG", "KO", "PEP", "COST", "CL", "MO", "PM", "EL",
    # Healthcare
    "JNJ", "UNH", "PFE", "MRK", "ABBV", "TMO", "ABT", "MDT", "BMY", "AMGN",
    "GILD", "CVS",
    # Financials
    "BRK-B", "JPM", "BAC", "WFC", "GS", "MS", "BLK", "AXP", "USB", "C",
    "MMC", "CB",
    # Industrials
    "HON", "UPS", "BA", "CAT", "DE", "MMM", "GE", "LMT", "RTX", "FDX",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG",
    # Materials / Utilities / Real Estate
    "LIN", "APD", "NEE", "DUK", "SO", "AMT", "PLD",
    # Communication Services
    "VZ", "T", "DIS", "CMCSA", "NFLX",
]


def _compute_rsi(closes: pd.Series, period: int = 2) -> pd.Series:
    """Wilder RSI using EWM smoothing. Returns NaN for rows with insufficient history.

    When avg_loss = 0 (no losing periods), RSI = 100 by definition.
    Direct division is used rather than replacing 0 with inf, because dividing
    a finite avg_gain by inf would yield 0 (wrong) — pandas handles 0-denominator
    division as inf, which produces the correct RSI = 100 via 100 - 100/(1+inf).
    """
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    # Direct division: avg_gain>0, avg_loss=0 → inf → RSI=100 (overbought).
    # avg_gain=0, avg_loss=0 (flat) → NaN → RSI=NaN (undefined, treated as ineligible).
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_realized_vol(closes: pd.Series, lookback: int) -> float:
    """Annualized realized volatility over the trailing `lookback` log-return bars.

    Returns NaN if fewer than `lookback` returns are available.
    """
    log_returns = np.log(closes / closes.shift(1)).dropna()
    tail = log_returns.tail(lookback)
    if len(tail) < lookback:
        return math.nan
    return float(tail.std() * np.sqrt(252))


class ShortTermMeanReversion(Strategy):
    """Mean reversion on S&P 100 large-caps using RSI(2) as entry/exit signal.

    Entry: RSI(2) < entry_rsi_threshold AND close > 200-day SMA (long-term uptrend filter).
    Exit: RSI(2) > exit_rsi_threshold (reversion complete) OR days_held >= time_stop_days.
    Hard stop: 8% below entry — wide by design; deeper oversold is more opportunity, not failure.
    Sizing: 1% NAV risk at 8% stop = 12.5% NAV per position; max 8 concurrent positions.
    """

    name = "ShortTermMeanReversion"

    capabilities = StrategyCapabilities(
        asset_classes=frozenset({"us_equity"}),
        requires_shorting=False,
        requires_options=False,
        requires_fractional=False,
        min_cash_buffer_pct=0.0,
        rebalance_cadence="daily",
        typical_holding_period_days=10,
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

        lookback_days: int = int(params.get("lookback_days", 210))
        if lookback_days < 210:
            raise ValueError(
                f"lookback_days must be >= 210 (got {lookback_days}); "
                "need at least 200 bars for the SMA filter plus RSI(2) warmup."
            )

        entry_rsi_threshold: float = float(params.get("entry_rsi_threshold", 10.0))
        exit_rsi_threshold: float = float(params.get("exit_rsi_threshold", 70.0))
        hard_stop_pct: float = float(params.get("hard_stop_pct", 0.08))
        time_stop_days: int = int(params.get("time_stop_days", 15))
        risk_per_trade_pct: float = float(params.get("risk_per_trade_pct", 0.01))
        max_position_pct: float = float(params.get("max_position_pct", 0.20))
        max_concurrent_positions: int = int(params.get("max_concurrent_positions", 8))
        max_total_deployment_pct: float = float(params.get("max_total_deployment_pct", 1.00))
        vol_filter_lookback: int = int(params.get("vol_filter_lookback", 10))
        vol_filter_multiplier: float = float(params.get("vol_filter_multiplier", 2.0))

        ps = position_state or {}
        tradeable = set(universe)
        targets: list[Target] = []
        bars_needed = max(200, lookback_days) + 10
        entry_weight = min(risk_per_trade_pct / hard_stop_pct, max_position_pct)

        # -----------------------------------------------------------------
        # 1. Evaluate existing positions — hold or exit?
        # -----------------------------------------------------------------
        kept_symbols: set[str] = set()

        for symbol, pos in positions.items():
            state = ps.get(symbol, {})
            entry_price = float(state.get("entry_price", pos.avg_cost))
            days_held = int(state.get("days_held", 0))

            try:
                current_price = float(market_data.get_latest_price(symbol))
            except Exception:
                logger.warning("Cannot get price for held position %s; holding", symbol)
                weight = float(pos.qty * pos.avg_cost) / nav
                targets.append(Target(symbol=symbol, target_weight=weight,
                                      rationale={"reason": "hold_no_price"}))
                kept_symbols.add(symbol)
                continue

            # Hard stop takes priority over all other exit logic.
            hard_stop_level = entry_price * (1.0 - hard_stop_pct)
            if current_price < hard_stop_level:
                logger.info(
                    "Hard stop symbol=%s days_held=%d current=%.4f threshold=%.4f",
                    symbol, days_held, current_price, hard_stop_level,
                )
                targets.append(Target(symbol=symbol, target_weight=0.0,
                                      rationale={"reason": "hard_stop"}))
                continue

            # RSI exit: mean reversion complete.
            rsi_exit = False
            try:
                bars = market_data.get_bars(symbol, bars_needed, as_of)
                if bars is not None and len(bars) >= 3:
                    rsi_series = _compute_rsi(bars["close"])
                    current_rsi = float(rsi_series.iloc[-1])
                    if not math.isnan(current_rsi) and current_rsi > exit_rsi_threshold:
                        rsi_exit = True
            except Exception:
                logger.warning(
                    "Failed to compute RSI for exit on %s; skipping RSI exit",
                    symbol, exc_info=True,
                )

            if rsi_exit:
                logger.info("RSI exit symbol=%s days_held=%d", symbol, days_held)
                targets.append(Target(symbol=symbol, target_weight=0.0,
                                      rationale={"reason": "rsi_exit"}))
                continue

            # Time stop: position has stagnated.
            if days_held >= time_stop_days:
                logger.info("Time stop symbol=%s days_held=%d", symbol, days_held)
                targets.append(Target(symbol=symbol, target_weight=0.0,
                                      rationale={"reason": "time_stop"}))
                continue

            # Hold at current market-value weight.
            current_weight = float(pos.qty) * current_price / nav
            targets.append(Target(symbol=symbol, target_weight=current_weight,
                                  rationale={"reason": "hold"}))
            kept_symbols.add(symbol)

        # -----------------------------------------------------------------
        # 1b. Pre-scan full universe for realized volatility (entry filter).
        # -----------------------------------------------------------------
        universe_vols: dict[str, float] = {}
        _vol_bars_needed = vol_filter_lookback + 1
        for _sym in UNIVERSE:
            try:
                _vb = market_data.get_bars(_sym, _vol_bars_needed, as_of)
                if _vb is not None and len(_vb) >= _vol_bars_needed:
                    _rv = _compute_realized_vol(_vb["close"], vol_filter_lookback)
                    if not math.isnan(_rv):
                        universe_vols[_sym] = _rv
            except Exception:
                pass
        _vol_values = list(universe_vols.values())
        universe_median_vol: float = float(np.median(_vol_values)) if _vol_values else math.nan
        vol_threshold: float = (
            universe_median_vol * vol_filter_multiplier
            if not math.isnan(universe_median_vol)
            else math.nan
        )

        # -----------------------------------------------------------------
        # 2. Entry signals — scan universe for RSI(2) oversold + uptrend
        # -----------------------------------------------------------------
        open_count = len(kept_symbols)
        deployed = sum(t.target_weight for t in targets if t.target_weight > 0.0)

        candidates: list[tuple[float, str]] = []

        for symbol in UNIVERSE:
            if symbol in positions:
                continue
            if symbol not in tradeable:
                continue

            try:
                bars = market_data.get_bars(symbol, bars_needed, as_of)
            except Exception:
                logger.warning("Failed to get bars for %s; skipping", symbol, exc_info=True)
                continue

            if bars is None or len(bars) < 202:
                logger.debug(
                    "Insufficient history for %s (%s bars, need 202)",
                    symbol, len(bars) if bars is not None else 0,
                )
                continue

            closes = bars["close"]
            current_close = float(closes.iloc[-1])

            # 200-day SMA uptrend filter.
            ma200 = float(closes.iloc[-200:].mean())
            if current_close <= ma200:
                continue

            # RSI(2) oversold filter.
            rsi_series = _compute_rsi(closes)
            current_rsi = float(rsi_series.iloc[-1])
            if math.isnan(current_rsi) or current_rsi >= entry_rsi_threshold:
                continue

            # Volatility filter: skip candidates whose realized vol exceeds the threshold.
            if not math.isnan(vol_threshold):
                sym_vol = universe_vols.get(symbol, math.nan)
                if not math.isnan(sym_vol) and sym_vol > vol_threshold:
                    continue

            candidates.append((current_rsi, symbol))

        # Most oversold (lowest RSI) takes priority when slots are limited.
        candidates.sort()

        for current_rsi, symbol in candidates:
            if open_count >= max_concurrent_positions:
                break
            if deployed + entry_weight > max_total_deployment_pct + 1e-9:
                logger.debug("Deployment cap reached; skipping %s", symbol)
                break

            logger.info(
                "Entry signal symbol=%s rsi=%.4f entry_weight=%.4f",
                symbol, current_rsi, entry_weight,
            )
            targets.append(Target(
                symbol=symbol,
                target_weight=entry_weight,
                rationale={"reason": "rsi_oversold", "rsi": current_rsi},
            ))
            deployed += entry_weight
            open_count += 1

        return targets
