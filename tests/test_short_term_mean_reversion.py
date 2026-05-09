"""Tests for ShortTermMeanReversion strategy — per spec Section 8."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from src.strategies.base import Position
from src.strategies.short_term_mean_reversion import (
    UNIVERSE,
    ShortTermMeanReversion,
    _compute_rsi,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AS_OF = datetime(2024, 6, 1, tzinfo=timezone.utc)

DEFAULT_PARAMS: dict[str, Any] = {
    "lookback_days": 210,
    "entry_rsi_threshold": 10.0,
    "exit_rsi_threshold": 70.0,
    "hard_stop_pct": 0.08,
    "time_stop_days": 15,
    "risk_per_trade_pct": 0.01,
    "max_position_pct": 0.20,
    "max_concurrent_positions": 8,
    "max_total_deployment_pct": 1.00,
}

# All universe symbols as the tradeable list for tests that need a full universe.
ALL_SYMBOLS = list(UNIVERSE)

STRATEGY = ShortTermMeanReversion()


def _make_bars(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def _make_mock_market(
    prices: dict[str, float] | None = None,
    bars: dict[str, pd.DataFrame] | None = None,
) -> MagicMock:
    md = MagicMock()
    prices = prices or {}
    bars_map = bars or {}
    md.get_latest_price.side_effect = lambda sym: Decimal(str(prices.get(sym, 100.0)))
    md.get_bars.side_effect = lambda sym, lookback, as_of: bars_map.get(sym)
    return md


def _position(symbol: str, qty: float = 10.0, avg_cost: float = 100.0) -> Position:
    return Position(symbol=symbol, qty=Decimal(str(qty)), avg_cost=Decimal(str(avg_cost)))


def _oversold_bars(n: int = 220) -> pd.DataFrame:
    """Bars where RSI(2) is near 0 and current price is above the 200-day MA.

    Structure: first 190 bars at 50, then 10 bars at 200, then 2 bars dropping
    (198, 196).  The 200-day MA of the last 200 bars ≈ 59, well below current
    price 196 — so the uptrend filter passes.  The two consecutive down bars
    drive RSI(2) near zero.
    """
    closes = [50.0] * 190 + [200.0] * 10 + [198.0, 196.0]
    return _make_bars(closes)


def _overbought_bars(n: int = 220) -> pd.DataFrame:
    """Bars where RSI(2) is near 100 (two consecutive up bars above a flat base)."""
    closes = [100.0] * 218 + [102.0, 104.0]
    return _make_bars(closes)


def _neutral_bars(price: float = 100.0, n: int = 220) -> pd.DataFrame:
    """Flat bars — RSI(2) is undefined/neutral, price equals MA200."""
    return _make_bars([price] * n)


# ---------------------------------------------------------------------------
# 8.1  RSI computation
# ---------------------------------------------------------------------------


def test_rsi_all_up_series_near_100() -> None:
    """Consistent daily gains produce RSI(2) close to 100."""
    closes = pd.Series([float(i) for i in range(100, 115)])
    rsi = _compute_rsi(closes)
    assert float(rsi.iloc[-1]) > 90.0


def test_rsi_all_down_series_near_zero() -> None:
    """Consistent daily losses produce RSI(2) close to 0."""
    closes = pd.Series([float(i) for i in range(114, 99, -1)])
    rsi = _compute_rsi(closes)
    assert float(rsi.iloc[-1]) < 10.0


def test_rsi_first_row_is_nan() -> None:
    """RSI(2) of the first row must be NaN — diff() produces no delta there."""
    closes = pd.Series([100.0, 101.0, 102.0, 103.0])
    rsi = _compute_rsi(closes)
    assert math.isnan(float(rsi.iloc[0]))


def test_rsi_below_10_identified_as_oversold() -> None:
    """A series ending with two sharp down bars produces RSI(2) < 10."""
    closes = pd.Series([100.0] * 20 + [95.0, 90.0])
    rsi = _compute_rsi(closes)
    assert float(rsi.iloc[-1]) < 10.0


def test_rsi_above_70_identified_as_overbought() -> None:
    """A series ending with two sharp up bars produces RSI(2) > 70."""
    closes = pd.Series([100.0] * 20 + [105.0, 110.0])
    rsi = _compute_rsi(closes)
    assert float(rsi.iloc[-1]) > 70.0


def test_rsi_stable_with_extra_history() -> None:
    """Adding extra leading bars does not change the final RSI value."""
    core = [100.0] * 20 + [95.0, 90.0]
    rsi_short = _compute_rsi(pd.Series(core))
    rsi_long = _compute_rsi(pd.Series([100.0] * 50 + core))
    # Final values should be equal (extra history converges to the same EWM state)
    assert abs(float(rsi_short.iloc[-1]) - float(rsi_long.iloc[-1])) < 1e-6


# ---------------------------------------------------------------------------
# 8.2  Entry signal
# ---------------------------------------------------------------------------


def test_entry_fires_when_rsi_oversold_and_above_ma200() -> None:
    """RSI(2) < 10 and close > 200-day MA → buy target generated."""
    bars = _oversold_bars()
    md = _make_mock_market(prices={"AAPL": 196.0}, bars={"AAPL": bars})
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["AAPL"], as_of=AS_OF,
    )
    assert any(t.symbol == "AAPL" and t.target_weight > 0 for t in targets)


def test_entry_blocked_when_price_below_ma200() -> None:
    """Close <= 200-day MA → no entry even if RSI is deeply oversold."""
    # Flat series: current price = MA200 = 50.0; RSI is also undefined/neutral
    bars = _make_bars([50.0] * 220)
    md = _make_mock_market(prices={"AAPL": 50.0}, bars={"AAPL": bars})
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["AAPL"], as_of=AS_OF,
    )
    assert all(t.symbol != "AAPL" or t.target_weight == 0 for t in targets)


def test_entry_blocked_when_rsi_not_oversold() -> None:
    """RSI(2) = ~50 (neutral) → no entry even if price > MA200."""
    bars = _overbought_bars()  # RSI near 100, not below 10
    md = _make_mock_market(prices={"AAPL": 104.0}, bars={"AAPL": bars})
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["AAPL"], as_of=AS_OF,
    )
    assert all(t.symbol != "AAPL" or t.target_weight == 0 for t in targets)


def test_entry_blocked_for_already_held_symbol() -> None:
    """No buy generated when symbol is already in positions."""
    bars = _oversold_bars()
    md = _make_mock_market(prices={"AAPL": 196.0}, bars={"AAPL": bars})
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=8_750.0,
        positions={"AAPL": _position("AAPL")},
        market_data=md, universe=["AAPL"], as_of=AS_OF,
    )
    buy_targets = [t for t in targets if t.symbol == "AAPL" and t.rationale.get("reason") == "rsi_oversold"]
    assert len(buy_targets) == 0


def test_entry_ranks_by_rsi_ascending() -> None:
    """When multiple signals fire and only 1 slot remains, lowest RSI is chosen."""
    params = {**DEFAULT_PARAMS, "max_concurrent_positions": 1}

    # AAPL: RSI very low (more oversold, should be chosen)
    aapl_bars = _make_bars([50.0] * 190 + [200.0] * 10 + [195.0, 190.0])
    # MSFT: RSI slightly higher (less oversold)
    msft_bars = _make_bars([50.0] * 190 + [200.0] * 10 + [199.0, 198.0])

    md = _make_mock_market(
        prices={"AAPL": 190.0, "MSFT": 198.0},
        bars={"AAPL": aapl_bars, "MSFT": msft_bars},
    )
    targets = STRATEGY.generate_targets(
        params=params, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["AAPL", "MSFT"], as_of=AS_OF,
    )
    entries = [t for t in targets if t.target_weight > 0]
    assert len(entries) == 1
    assert entries[0].symbol == "AAPL"


# ---------------------------------------------------------------------------
# 8.3  Exit signal
# ---------------------------------------------------------------------------


def test_rsi_exit_when_overbought() -> None:
    """RSI(2) > 70 on a held position → sell target with rationale rsi_exit."""
    bars = _overbought_bars()
    md = _make_mock_market(prices={"AAPL": 104.0}, bars={"AAPL": bars})
    ps = {"AAPL": {"entry_price": 100.0, "days_held": 3, "highest_close_since_entry": 104.0}}
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"AAPL": _position("AAPL", avg_cost=100.0)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    aapl = next(t for t in targets if t.symbol == "AAPL")
    assert aapl.target_weight == 0.0
    assert aapl.rationale["reason"] == "rsi_exit"


def test_time_stop_at_threshold() -> None:
    """days_held >= time_stop_days → sell target with rationale time_stop."""
    neutral = _neutral_bars(price=105.0)  # RSI undefined, price above entry
    md = _make_mock_market(prices={"AAPL": 105.0}, bars={"AAPL": neutral})
    ps = {"AAPL": {"entry_price": 100.0, "days_held": 15, "highest_close_since_entry": 105.0}}
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"AAPL": _position("AAPL", avg_cost=100.0)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    aapl = next(t for t in targets if t.symbol == "AAPL")
    assert aapl.target_weight == 0.0
    assert aapl.rationale["reason"] == "time_stop"


def test_hard_stop_triggers_below_threshold() -> None:
    """Price below entry × (1 - hard_stop_pct) → sell target with rationale hard_stop."""
    entry = 100.0
    current = entry * (1.0 - 0.085)  # 8.5% below entry — past 8% hard stop
    md = _make_mock_market(prices={"AAPL": current})
    ps = {"AAPL": {"entry_price": entry, "days_held": 3, "highest_close_since_entry": entry}}
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"AAPL": _position("AAPL", avg_cost=entry)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    aapl = next(t for t in targets if t.symbol == "AAPL")
    assert aapl.target_weight == 0.0
    assert aapl.rationale["reason"] == "hard_stop"


def test_hard_stop_overrides_rsi_exit() -> None:
    """Hard stop fires even when RSI < 70 — it checks before RSI, overrides all."""
    entry = 100.0
    current = entry * (1.0 - 0.09)  # past hard stop
    bars = _oversold_bars()  # RSI near 0 — would not trigger RSI exit anyway
    md = _make_mock_market(prices={"AAPL": current}, bars={"AAPL": bars})
    ps = {"AAPL": {"entry_price": entry, "days_held": 3, "highest_close_since_entry": entry}}
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"AAPL": _position("AAPL", avg_cost=entry)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    aapl = next(t for t in targets if t.symbol == "AAPL")
    assert aapl.rationale["reason"] == "hard_stop"


def test_hold_when_no_exit_condition() -> None:
    """Profitable position within all limits → hold with positive weight."""
    entry = 100.0
    current = 105.0
    neutral = _neutral_bars(price=current)
    md = _make_mock_market(prices={"AAPL": current}, bars={"AAPL": neutral})
    ps = {"AAPL": {"entry_price": entry, "days_held": 5, "highest_close_since_entry": current}}
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"AAPL": _position("AAPL", avg_cost=entry)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    aapl = next(t for t in targets if t.symbol == "AAPL")
    assert aapl.target_weight > 0.0
    assert aapl.rationale["reason"] == "hold"


# ---------------------------------------------------------------------------
# 8.4  Position sizing
# ---------------------------------------------------------------------------


def test_default_entry_weight_is_12_5_pct() -> None:
    """risk_per_trade_pct / hard_stop_pct = 0.01 / 0.08 = 0.125."""
    bars = _oversold_bars()
    md = _make_mock_market(prices={"AAPL": 196.0}, bars={"AAPL": bars})
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["AAPL"], as_of=AS_OF,
    )
    aapl = next(t for t in targets if t.symbol == "AAPL")
    assert abs(aapl.target_weight - 0.125) < 1e-9


def test_entry_weight_capped_by_max_position_pct() -> None:
    """When risk formula exceeds max_position_pct, weight is capped."""
    params = {**DEFAULT_PARAMS, "hard_stop_pct": 0.02, "risk_per_trade_pct": 0.01}
    # risk formula = 0.01 / 0.02 = 0.50, but max_position_pct = 0.20
    bars = _oversold_bars()
    md = _make_mock_market(prices={"AAPL": 196.0}, bars={"AAPL": bars})
    targets = STRATEGY.generate_targets(
        params=params, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["AAPL"], as_of=AS_OF,
    )
    aapl = next(t for t in targets if t.symbol == "AAPL")
    assert aapl.target_weight <= 0.20


def test_max_total_deployment_pct_respected() -> None:
    """Entries that would push total deployment past the cap are skipped."""
    params = {**DEFAULT_PARAMS, "max_total_deployment_pct": 0.25}
    # 0.125 entry weight × 2 = 0.25, 3rd entry would exceed cap
    bars = _oversold_bars()
    md = _make_mock_market(
        prices={s: 196.0 for s in ["AAPL", "MSFT", "GOOGL"]},
        bars={s: bars for s in ["AAPL", "MSFT", "GOOGL"]},
    )
    targets = STRATEGY.generate_targets(
        params=params, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["AAPL", "MSFT", "GOOGL"], as_of=AS_OF,
    )
    entries = [t for t in targets if t.target_weight > 0]
    total_weight = sum(t.target_weight for t in entries)
    assert total_weight <= 0.25 + 1e-9


def test_max_concurrent_positions_cap() -> None:
    """When 10 symbols signal simultaneously, no more than max_concurrent_positions entered."""
    params = {**DEFAULT_PARAMS, "max_concurrent_positions": 3}
    syms = ["AAPL", "MSFT", "GOOGL", "GOOG", "META", "NVDA", "AVGO", "TXN", "QCOM", "IBM"]
    bars = _oversold_bars()
    md = _make_mock_market(
        prices={s: 196.0 for s in syms},
        bars={s: bars for s in syms},
    )
    targets = STRATEGY.generate_targets(
        params=params, nav=50_000.0, cash=50_000.0,
        positions={}, market_data=md, universe=syms, as_of=AS_OF,
    )
    entries = [t for t in targets if t.target_weight > 0]
    assert len(entries) <= 3


# ---------------------------------------------------------------------------
# 8.5  Full generate_targets integration
# ---------------------------------------------------------------------------


def test_empty_portfolio_three_eligible_signals() -> None:
    """3 eligible symbols with no positions → 3 buy targets."""
    syms = ["AAPL", "MSFT", "GOOGL"]
    bars = _oversold_bars()
    md = _make_mock_market(
        prices={s: 196.0 for s in syms},
        bars={s: bars for s in syms},
    )
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=syms, as_of=AS_OF,
    )
    entries = [t for t in targets if t.target_weight > 0]
    assert len(entries) == 3


def test_full_portfolio_no_new_entries() -> None:
    """8 positions (at capacity) → no new entries even with signals."""
    held = [UNIVERSE[i] for i in range(8)]
    positions = {sym: _position(sym, avg_cost=100.0) for sym in held}
    prices = {sym: 105.0 for sym in held}
    prices["AAPL"] = 196.0  # would signal if there were room

    ps = {sym: {"entry_price": 100.0, "days_held": 3, "highest_close_since_entry": 105.0}
          for sym in held}
    bars = _oversold_bars()
    neutral = _neutral_bars(price=105.0)
    bars_map = {sym: neutral for sym in held}
    bars_map["AAPL"] = bars

    md = _make_mock_market(prices=prices, bars=bars_map)
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=80_000.0, cash=0.0,
        positions=positions, market_data=md, universe=ALL_SYMBOLS,
        as_of=AS_OF, position_state=ps,
    )
    new_entries = [t for t in targets if t.symbol not in held and t.target_weight > 0]
    assert len(new_entries) == 0


def test_rsi_exit_time_stop_and_hold_in_same_cycle() -> None:
    """RSI exit, time stop, and continuing position all appear in one target list."""
    overbought = _overbought_bars()
    neutral = _neutral_bars(price=105.0)

    sym_rsi = "AAPL"   # RSI exit
    sym_time = "MSFT"  # time stop
    sym_hold = "GOOGL"  # hold

    positions = {
        sym_rsi: _position(sym_rsi, avg_cost=100.0),
        sym_time: _position(sym_time, avg_cost=100.0),
        sym_hold: _position(sym_hold, avg_cost=100.0),
    }
    ps = {
        sym_rsi:  {"entry_price": 100.0, "days_held": 5,  "highest_close_since_entry": 104.0},
        sym_time: {"entry_price": 100.0, "days_held": 15, "highest_close_since_entry": 105.0},
        sym_hold: {"entry_price": 100.0, "days_held": 5,  "highest_close_since_entry": 105.0},
    }
    md = _make_mock_market(
        prices={sym_rsi: 104.0, sym_time: 105.0, sym_hold: 105.0},
        bars={sym_rsi: overbought, sym_time: neutral, sym_hold: neutral},
    )
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=30_000.0, cash=0.0,
        positions=positions, market_data=md, universe=ALL_SYMBOLS,
        as_of=AS_OF, position_state=ps,
    )
    by_sym = {t.symbol: t for t in targets}
    assert by_sym[sym_rsi].target_weight == 0.0
    assert by_sym[sym_rsi].rationale["reason"] == "rsi_exit"
    assert by_sym[sym_time].target_weight == 0.0
    assert by_sym[sym_time].rationale["reason"] == "time_stop"
    assert by_sym[sym_hold].target_weight > 0.0


def test_exits_free_slots_for_new_entries() -> None:
    """A position exiting on the same cycle creates room for a new entry."""
    params = {**DEFAULT_PARAMS, "max_concurrent_positions": 1}

    # AAPL is being exited via hard stop
    positions = {"AAPL": _position("AAPL", avg_cost=100.0)}
    ps = {"AAPL": {"entry_price": 100.0, "days_held": 3, "highest_close_since_entry": 100.0}}

    # MSFT has an entry signal
    msft_bars = _oversold_bars()

    md = _make_mock_market(
        prices={"AAPL": 91.0, "MSFT": 196.0},
        bars={"AAPL": msft_bars, "MSFT": msft_bars},
    )
    targets = STRATEGY.generate_targets(
        params=params, nav=10_000.0, cash=500.0,
        positions=positions, market_data=md, universe=["AAPL", "MSFT"],
        as_of=AS_OF, position_state=ps,
    )
    aapl = next(t for t in targets if t.symbol == "AAPL")
    msft = next((t for t in targets if t.symbol == "MSFT"), None)
    assert aapl.target_weight == 0.0
    assert aapl.rationale["reason"] == "hard_stop"
    assert msft is not None and msft.target_weight > 0


def test_no_eligible_symbols_returns_empty() -> None:
    """No signals in the universe → empty target list."""
    bars = _neutral_bars()  # flat bars — RSI undefined, price equals MA
    md = _make_mock_market(prices={"AAPL": 100.0}, bars={"AAPL": bars})
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["AAPL"], as_of=AS_OF,
    )
    entries = [t for t in targets if t.target_weight > 0]
    assert len(entries) == 0


def test_all_positions_hit_hard_stop() -> None:
    """All held positions below hard stop → all sell targets generated."""
    held = ["AAPL", "MSFT", "GOOGL"]
    positions = {sym: _position(sym, avg_cost=100.0) for sym in held}
    ps = {sym: {"entry_price": 100.0, "days_held": 3, "highest_close_since_entry": 100.0}
          for sym in held}
    md = _make_mock_market(prices={sym: 91.0 for sym in held})  # all below 8% hard stop

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=30_000.0, cash=0.0,
        positions=positions, market_data=md, universe=ALL_SYMBOLS,
        as_of=AS_OF, position_state=ps,
    )
    for sym in held:
        t = next(t for t in targets if t.symbol == sym)
        assert t.target_weight == 0.0
        assert t.rationale["reason"] == "hard_stop"


# ---------------------------------------------------------------------------
# 8.6  Edge cases
# ---------------------------------------------------------------------------


def test_insufficient_history_skipped_gracefully() -> None:
    """Symbol with < 202 bars of history is skipped; no crash."""
    short_bars = _make_bars([100.0] * 100)  # only 100 bars
    md = _make_mock_market(prices={"AAPL": 100.0}, bars={"AAPL": short_bars})
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["AAPL"], as_of=AS_OF,
    )
    # No crash, and no entry generated
    assert all(t.symbol != "AAPL" or t.target_weight == 0 for t in targets)


def test_nan_rsi_treated_as_ineligible() -> None:
    """A symbol whose RSI is NaN (exactly 2 bars) is skipped without error."""
    two_bars = _make_bars([100.0, 98.0])  # RSI(2) will be NaN with only 2 bars
    md = _make_mock_market(prices={"AAPL": 98.0}, bars={"AAPL": two_bars})
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["AAPL"], as_of=AS_OF,
    )
    assert all(t.symbol != "AAPL" or t.target_weight == 0 for t in targets)


def test_lookback_days_below_210_raises_value_error() -> None:
    """lookback_days < 210 → ValueError raised immediately."""
    params = {**DEFAULT_PARAMS, "lookback_days": 200}
    with pytest.raises(ValueError, match="lookback_days must be >= 210"):
        STRATEGY.generate_targets(
            params=params, nav=10_000.0, cash=10_000.0,
            positions={}, market_data=MagicMock(), universe=[], as_of=AS_OF,
        )


# ---------------------------------------------------------------------------
# Name and capabilities
# ---------------------------------------------------------------------------


def test_strategy_name_and_capabilities() -> None:
    assert STRATEGY.name == "ShortTermMeanReversion"
    assert STRATEGY.capabilities.requires_fractional is False
    assert STRATEGY.capabilities.requires_shorting is False
    assert "us_equity" in STRATEGY.capabilities.asset_classes


def test_zero_nav_returns_empty() -> None:
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=0.0, cash=0.0,
        positions={}, market_data=MagicMock(), universe=ALL_SYMBOLS, as_of=AS_OF,
    )
    assert targets == []
