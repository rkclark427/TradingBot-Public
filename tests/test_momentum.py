"""Tests for MomentumContinuation strategy — acceptance criteria from spec Section 11.1."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from src.strategies.momentum_continuation import MomentumContinuation, UNIVERSE
from src.strategies.base import Position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AS_OF = datetime(2024, 6, 1, tzinfo=timezone.utc)
DEFAULT_PARAMS: dict[str, Any] = {
    "lookback_days": 20,
    "hard_stop_pct": 0.04,
    "trailing_stop_pct": 0.05,
    "time_stop_days": 10,
    "risk_per_trade_pct": 0.01,
    "max_position_pct": 0.25,
    "max_concurrent_positions": 5,
    "max_total_deployment_pct": 1.00,
}

# All 16 universe ETFs as the "tradeable" universe list
ALL_SYMBOLS = sorted(UNIVERSE)


def _make_bars(closes: list[float]) -> pd.DataFrame:
    """Build a minimal DataFrame with a 'close' column."""
    return pd.DataFrame({"close": closes})


def _make_mock_market(
    prices: dict[str, float] | None = None,
    bars: dict[str, pd.DataFrame] | None = None,
) -> MagicMock:
    """Return a mock MarketDataView."""
    md = MagicMock()
    prices = prices or {}
    bars_map = bars or {}

    md.get_latest_price.side_effect = lambda sym: Decimal(str(prices.get(sym, 100.0)))
    md.get_bars.side_effect = lambda sym, lookback, as_of: bars_map.get(sym)
    return md


def _breakout_bars(lookback_days: int = 20, breakout: bool = True) -> pd.DataFrame:
    """21 bars where today either breaks out or doesn't.

    If breakout=True, today's close (101.0) > prior 20-day high (100.0).
    If breakout=False, today's close (99.0) <= prior 20-day high (100.0).
    """
    prior = [100.0] * lookback_days  # 20 bars, all at 100
    today = [101.0 if breakout else 99.0]
    return _make_bars(prior + today)


def _position(symbol: str, qty: float = 1.0, avg_cost: float = 100.0) -> Position:
    return Position(symbol=symbol, qty=Decimal(str(qty)), avg_cost=Decimal(str(avg_cost)))


STRATEGY = MomentumContinuation()


# ---------------------------------------------------------------------------
# Entry signal: 20-day high computation
# ---------------------------------------------------------------------------


def test_entry_fires_on_breakout() -> None:
    """Entry signal when today's close strictly exceeds the prior 20-day high."""
    md = _make_mock_market(
        prices={"EEM": 101.0},
        bars={"EEM": _breakout_bars(breakout=True)},
    )
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["EEM"], as_of=AS_OF,
    )
    assert any(t.symbol == "EEM" and t.target_weight > 0 for t in targets)


def test_entry_does_not_fire_below_prior_high() -> None:
    """No entry when today's close is at or below the 20-day high."""
    md = _make_mock_market(
        prices={"EEM": 99.0},
        bars={"EEM": _breakout_bars(breakout=False)},
    )
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["EEM"], as_of=AS_OF,
    )
    assert all(t.symbol != "EEM" for t in targets)


def test_entry_does_not_fire_for_existing_position() -> None:
    """No entry signal when we already hold the symbol."""
    md = _make_mock_market(
        prices={"EEM": 105.0},
        bars={"EEM": _breakout_bars(breakout=True)},
    )
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=9_000.0,
        positions={"EEM": _position("EEM", qty=10.0, avg_cost=100.0)},
        market_data=md, universe=["EEM"], as_of=AS_OF,
    )
    buy_targets = [t for t in targets if t.symbol == "EEM" and t.rationale.get("reason") == "breakout"]
    assert len(buy_targets) == 0


def test_entry_requires_sufficient_history() -> None:
    """Fewer than lookback_days+1 bars → no entry."""
    md = _make_mock_market(
        prices={"EEM": 101.0},
        bars={"EEM": _make_bars([100.0] * 15)},  # only 15 bars, need 21
    )
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["EEM"], as_of=AS_OF,
    )
    assert all(t.symbol != "EEM" for t in targets)


def test_entry_skips_missing_bars() -> None:
    """Symbols with no bars (None) are skipped without error."""
    md = _make_mock_market(prices={"EEM": 101.0}, bars={})  # no bars for EEM
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["EEM"], as_of=AS_OF,
    )
    assert all(t.symbol != "EEM" for t in targets)


def test_entry_prior_high_uses_only_last_lookback_days() -> None:
    """With 100 bars, the prior high is computed from the most recent 20, not all 100."""
    # Bars 0-79: close=200 (old high), bars 80-99: close=50 (low), bar 100: close=60 (breakout)
    closes = [200.0] * 80 + [50.0] * 20 + [60.0]  # 101 bars
    md = _make_mock_market(
        prices={"EEM": 60.0},
        bars={"EEM": _make_bars(closes)},
    )
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["EEM"], as_of=AS_OF,
    )
    # prior_high over last 20 bars (all 50.0) → today 60.0 > 50.0 → entry
    assert any(t.symbol == "EEM" and t.target_weight > 0 for t in targets)


# ---------------------------------------------------------------------------
# Exit signals
# ---------------------------------------------------------------------------


def test_hard_stop_triggers() -> None:
    """Close below entry_price * (1 - 0.04) → exit with reason 'hard_stop'."""
    entry = 100.0
    current = entry * (1.0 - 0.041)  # just below 4% stop
    md = _make_mock_market(prices={"EEM": current})
    ps = {"EEM": {"entry_price": entry, "days_held": 3, "highest_close_since_entry": entry}}

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"EEM": _position("EEM", qty=10.0, avg_cost=entry)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    assert eem.target_weight == 0.0
    assert eem.rationale["reason"] == "hard_stop"


def test_hard_stop_does_not_trigger_above_threshold() -> None:
    """Close at exactly 3.9% below entry → hard stop should NOT trigger."""
    entry = 100.0
    current = entry * (1.0 - 0.039)
    md = _make_mock_market(prices={"EEM": current})
    ps = {"EEM": {"entry_price": entry, "days_held": 3, "highest_close_since_entry": entry}}

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"EEM": _position("EEM", qty=10.0, avg_cost=entry)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    assert eem.target_weight > 0.0


def test_trailing_stop_triggers() -> None:
    """Close below highest_close * (1 - 0.05) → exit with reason 'trailing_stop'."""
    entry = 100.0
    highest = 110.0
    current = highest * (1.0 - 0.051)  # just below 5% trailing stop
    md = _make_mock_market(prices={"EEM": current})
    ps = {"EEM": {"entry_price": entry, "days_held": 5, "highest_close_since_entry": highest}}

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"EEM": _position("EEM", qty=10.0, avg_cost=entry)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    assert eem.target_weight == 0.0
    assert eem.rationale["reason"] == "trailing_stop"


def test_trailing_stop_not_triggered_above_threshold() -> None:
    """Close at 4.9% below highest → trailing stop should NOT trigger."""
    entry = 100.0
    highest = 110.0
    current = highest * (1.0 - 0.049)
    md = _make_mock_market(prices={"EEM": current})
    ps = {"EEM": {"entry_price": entry, "days_held": 5, "highest_close_since_entry": highest}}

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"EEM": _position("EEM", qty=10.0, avg_cost=entry)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    assert eem.target_weight > 0.0


def test_hard_stop_takes_precedence_over_trailing_stop() -> None:
    """Both stops triggered → hard_stop wins (checked first)."""
    entry = 100.0
    highest = 110.0
    current = entry * (1.0 - 0.05)  # below hard stop AND trailing stop
    md = _make_mock_market(prices={"EEM": current})
    ps = {"EEM": {"entry_price": entry, "days_held": 5, "highest_close_since_entry": highest}}

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"EEM": _position("EEM", qty=10.0, avg_cost=entry)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    assert eem.rationale["reason"] == "hard_stop"


def test_time_stop_triggers_at_threshold() -> None:
    """days_held >= time_stop_days → exit with reason 'time_stop'."""
    entry = 100.0
    current = 101.0  # profitable, no price-based stop
    md = _make_mock_market(prices={"EEM": current})
    ps = {"EEM": {"entry_price": entry, "days_held": 10, "highest_close_since_entry": 101.0}}

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"EEM": _position("EEM", qty=10.0, avg_cost=entry)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    assert eem.target_weight == 0.0
    assert eem.rationale["reason"] == "time_stop"


def test_time_stop_does_not_trigger_below_threshold() -> None:
    """days_held = 9 → time stop should not trigger."""
    entry = 100.0
    md = _make_mock_market(prices={"EEM": 101.0})
    ps = {"EEM": {"entry_price": entry, "days_held": 9, "highest_close_since_entry": 101.0}}

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"EEM": _position("EEM", qty=10.0, avg_cost=entry)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    assert eem.target_weight > 0.0


def test_hold_when_no_exit_conditions() -> None:
    """Position well above all stops → hold with positive weight."""
    entry = 100.0
    current = 105.0
    md = _make_mock_market(prices={"EEM": current})
    ps = {"EEM": {"entry_price": entry, "days_held": 3, "highest_close_since_entry": 105.0}}

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"EEM": _position("EEM", qty=10.0, avg_cost=entry)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    assert eem.target_weight > 0.0
    assert eem.rationale["reason"] == "hold"


def test_hold_weight_reflects_current_market_value() -> None:
    """Hold weight = (qty * current_price) / nav."""
    qty = 10.0
    current = 105.0
    nav = 10_000.0
    expected_weight = qty * current / nav

    md = _make_mock_market(prices={"EEM": current})
    ps = {"EEM": {"entry_price": 100.0, "days_held": 3, "highest_close_since_entry": 105.0}}

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=nav, cash=500.0,
        positions={"EEM": _position("EEM", qty=qty, avg_cost=100.0)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF, position_state=ps,
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    assert abs(eem.target_weight - expected_weight) < 1e-9


def test_hold_uses_avg_cost_when_no_position_state() -> None:
    """Without position_state, entry_price defaults to avg_cost → no spurious stop."""
    entry = 100.0
    current = 99.0  # below avg_cost but within hard-stop range (< 4%)
    md = _make_mock_market(prices={"EEM": current})

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions={"EEM": _position("EEM", qty=10.0, avg_cost=entry)},
        market_data=md, universe=ALL_SYMBOLS, as_of=AS_OF,
        position_state=None,  # no state provided
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    # 99 is not below hard stop (entry * 0.96 = 96), so we hold
    assert eem.target_weight > 0.0


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


def test_entry_weight_is_25pct_by_default() -> None:
    """risk_per_trade_pct / hard_stop_pct = 0.01 / 0.04 = 0.25."""
    md = _make_mock_market(
        prices={"EEM": 101.0},
        bars={"EEM": _breakout_bars()},
    )
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["EEM"], as_of=AS_OF,
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    assert abs(eem.target_weight - 0.25) < 1e-9


def test_concentration_cap_limits_entry_weight() -> None:
    """max_position_pct=0.10 caps entry weight below the risk formula's 0.25."""
    params = {**DEFAULT_PARAMS, "max_position_pct": 0.10}
    md = _make_mock_market(
        prices={"EEM": 101.0},
        bars={"EEM": _breakout_bars()},
    )
    targets = STRATEGY.generate_targets(
        params=params, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["EEM"], as_of=AS_OF,
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    assert eem.target_weight <= 0.10


def test_max_concurrent_positions_rejects_excess_entries() -> None:
    """With 5 existing positions, no new entries are generated."""
    existing_symbols = ["EEM", "EFA", "IWM", "QQQ", "SPY"]
    positions = {sym: _position(sym, qty=10.0, avg_cost=100.0) for sym in existing_symbols}

    # All positions are healthy (no exit conditions)
    prices = {sym: 105.0 for sym in existing_symbols}
    prices["XLE"] = 101.0  # XLE would signal breakout if we had room
    ps = {
        sym: {"entry_price": 100.0, "days_held": 3, "highest_close_since_entry": 105.0}
        for sym in existing_symbols
    }

    bars = {"XLE": _breakout_bars()}
    md = _make_mock_market(prices=prices, bars=bars)

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=50_000.0, cash=0.0,
        positions=positions, market_data=md, universe=ALL_SYMBOLS,
        as_of=AS_OF, position_state=ps,
    )
    new_entries = [t for t in targets if t.symbol not in existing_symbols and t.target_weight > 0]
    assert len(new_entries) == 0


def test_deployment_cap_rejects_entry_when_fully_deployed() -> None:
    """4 positions at 0.25 each = 1.0 deployed → no room for a 5th entry."""
    four_symbols = ["EEM", "EFA", "IWM", "QQQ"]
    positions = {sym: _position(sym, qty=10.0, avg_cost=100.0) for sym in four_symbols}
    prices = {sym: 100.0 for sym in four_symbols}
    prices["XLE"] = 101.0
    ps = {
        sym: {"entry_price": 100.0, "days_held": 3, "highest_close_since_entry": 100.0}
        for sym in four_symbols
    }
    bars = {"XLE": _breakout_bars()}
    md = _make_mock_market(prices=prices, bars=bars)

    # NAV = 4 * (10 shares * 100) = 4000; deployed = 4 * 0.25 = 1.0
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=4_000.0, cash=0.0,
        positions=positions, market_data=md, universe=ALL_SYMBOLS,
        as_of=AS_OF, position_state=ps,
    )
    new_entries = [t for t in targets if t.symbol not in four_symbols and t.target_weight > 0]
    assert len(new_entries) == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_zero_nav_returns_empty() -> None:
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=0.0, cash=0.0,
        positions={}, market_data=MagicMock(), universe=ALL_SYMBOLS, as_of=AS_OF,
    )
    assert targets == []


def test_symbol_not_in_tradeable_universe_skipped() -> None:
    """Symbol in UNIVERSE but not in tradeable list → no entry."""
    md = _make_mock_market(
        prices={"EEM": 101.0},
        bars={"EEM": _breakout_bars()},
    )
    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md,
        universe=[],  # EEM not tradeable
        as_of=AS_OF,
    )
    assert all(t.symbol != "EEM" for t in targets)


def test_multiple_entries_respect_max_concurrent() -> None:
    """With max_concurrent_positions=2, at most 2 new entries are generated."""
    params = {**DEFAULT_PARAMS, "max_concurrent_positions": 2}
    # 3 symbols all showing breakouts
    bars_map = {sym: _breakout_bars() for sym in ["EEM", "EFA", "IWM"]}
    md = _make_mock_market(prices={s: 101.0 for s in ["EEM", "EFA", "IWM"]}, bars=bars_map)

    targets = STRATEGY.generate_targets(
        params=params, nav=10_000.0, cash=10_000.0,
        positions={}, market_data=md, universe=["EEM", "EFA", "IWM"], as_of=AS_OF,
    )
    entries = [t for t in targets if t.target_weight > 0]
    assert len(entries) <= 2


def test_exits_and_entries_on_same_cycle() -> None:
    """Position exits and new entries can both appear in the same target list."""
    # EEM is being exited (hard stop); XLE is a new entry
    positions = {"EEM": _position("EEM", qty=10.0, avg_cost=100.0)}
    ps = {"EEM": {"entry_price": 100.0, "days_held": 3, "highest_close_since_entry": 100.0}}
    prices = {"EEM": 95.0, "XLE": 101.0}  # EEM hits hard stop
    bars_map = {"XLE": _breakout_bars()}
    md = _make_mock_market(prices=prices, bars=bars_map)

    targets = STRATEGY.generate_targets(
        params=DEFAULT_PARAMS, nav=10_000.0, cash=500.0,
        positions=positions, market_data=md, universe=ALL_SYMBOLS,
        as_of=AS_OF, position_state=ps,
    )
    eem = next(t for t in targets if t.symbol == "EEM")
    xle = next((t for t in targets if t.symbol == "XLE"), None)

    assert eem.target_weight == 0.0
    assert eem.rationale["reason"] == "hard_stop"
    assert xle is not None and xle.target_weight > 0


def test_strategy_name_and_capabilities() -> None:
    assert STRATEGY.name == "momentum_continuation"
    assert STRATEGY.capabilities.requires_fractional is True
    assert STRATEGY.capabilities.requires_shorting is False
    assert "us_equity" in STRATEGY.capabilities.asset_classes
