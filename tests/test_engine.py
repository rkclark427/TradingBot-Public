"""Tests for src/backtest/engine.py — simulation engine."""

from __future__ import annotations

import copy
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.backtest.cache_models import PriceBarRow
from src.backtest.data import BacktestDataLayer
from src.backtest.engine import (
    SimulationResult,
    TradeRecord,
    _fill_price,
    run_simulation,
)
from src.strategies.base import (
    MarketDataView,
    Position,
    Strategy,
    StrategyCapabilities,
    Target,
)
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------

_CAPS = StrategyCapabilities(
    asset_classes=frozenset({"us_equity"}),
    requires_shorting=False,
    requires_options=False,
    requires_fractional=True,
    min_cash_buffer_pct=0.0,
    rebalance_cadence="daily",
    typical_holding_period_days=10,
)

# Five trading days used across most tests
DATES = [
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
]


@pytest.fixture()
def layer(tmp_path: Path) -> BacktestDataLayer:
    return BacktestDataLayer(tmp_path / "test.db")


def _seed(
    layer: BacktestDataLayer,
    symbol: str,
    dates: list[date],
    closes: list[float],
    opens: list[float] | None = None,
) -> None:
    """Insert rows directly into the cache without calling yfinance."""
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    _opens = opens if opens is not None else closes[:]
    with Session(layer._engine) as session:
        for d, o, c in zip(dates, _opens, closes):
            session.add(PriceBarRow(
                symbol=symbol,
                trade_date=d,
                open=o,
                high=c + 1.0,
                low=c - 1.0,
                close=c,
                volume=1_000_000,
                fetched_at=now,
            ))
        session.commit()


class _NullStrategy(Strategy):
    """Never trades. Useful for testing cash-only paths."""
    name = "null"
    capabilities = _CAPS

    def generate_targets(self, params, nav, cash, positions, market_data, universe, as_of, position_state=None) -> list[Target]:
        return []


class _CapturingStrategy(Strategy):
    """Returns pre-scripted targets and records position_state on each call."""
    name = "capturing"
    capabilities = _CAPS

    def __init__(self, targets_per_day: list[list[Target]]) -> None:
        self._targets = targets_per_day
        self.captured_states: list[dict[str, Any]] = []
        self._n = 0

    def generate_targets(self, params, nav, cash, positions, market_data, universe, as_of, position_state=None) -> list[Target]:
        self.captured_states.append(copy.deepcopy(position_state) if position_state else {})
        result = self._targets[self._n] if self._n < len(self._targets) else []
        self._n += 1
        return result


def _run(
    strategy: Strategy,
    layer: BacktestDataLayer,
    dates: list[date] = DATES,
    slippage_bps: float = 0.0,
    cash_annualized_rate: float = 0.0,
    starting_capital: Decimal = Decimal("10000"),
    universe: list[str] | None = None,
) -> SimulationResult:
    return run_simulation(
        strategy=strategy,
        params={},
        universe=universe or ["SPY"],
        data_layer=layer,
        start_date=dates[0],
        end_date=dates[-1],
        starting_capital=starting_capital,
        slippage_bps=slippage_bps,
        cash_annualized_rate=cash_annualized_rate,
    )


# ---------------------------------------------------------------------------
# Unit tests — _fill_price (pure function)
# ---------------------------------------------------------------------------


def test_fill_price_buy_applies_slippage() -> None:
    """Buy fill price is open * (1 + bps/10000)."""
    result = _fill_price(Decimal("100"), "buy", 10)   # 10bps = 0.1%
    assert result == Decimal("100.1000")


def test_fill_price_sell_applies_slippage() -> None:
    """Sell fill price is open * (1 - bps/10000)."""
    result = _fill_price(Decimal("100"), "sell", 10)
    assert result == Decimal("99.9000")


def test_fill_price_zero_slippage() -> None:
    """Zero slippage returns the open price unchanged."""
    assert _fill_price(Decimal("123.45"), "buy", 0) == Decimal("123.4500")
    assert _fill_price(Decimal("123.45"), "sell", 0) == Decimal("123.4500")


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_empty_cache_raises(layer: BacktestDataLayer) -> None:
    """No data in cache for the requested range raises ValueError."""
    with pytest.raises(ValueError, match="No trading data"):
        _run(_NullStrategy(), layer)


def test_snapshots_count_matches_trading_days(layer: BacktestDataLayer) -> None:
    """One DaySnapshot per trading day."""
    _seed(layer, "SPY", DATES, [100.0] * 5)
    result = _run(_NullStrategy(), layer)
    assert len(result.snapshots) == len(DATES)
    assert result.snapshots[0].sim_date == DATES[0]
    assert result.snapshots[-1].sim_date == DATES[-1]


def test_no_op_strategy_produces_no_trades(layer: BacktestDataLayer) -> None:
    """A strategy that returns no targets should produce no trade records."""
    _seed(layer, "SPY", DATES, [100.0] * 5)
    result = _run(_NullStrategy(), layer)
    assert result.trades == []


def test_cash_interest_accrues(layer: BacktestDataLayer) -> None:
    """NAV grows above starting capital when cash_annualized_rate > 0."""
    _seed(layer, "SPY", DATES, [100.0] * 5)
    result = _run(_NullStrategy(), layer, cash_annualized_rate=0.04)
    final_nav = result.snapshots[-1].nav
    assert final_nav > Decimal("10000"), "Cash interest should grow NAV"


def test_cash_interest_one_day(layer: BacktestDataLayer) -> None:
    """After one day with no trades, cash equals starting_capital * (1 + 0.04/252)."""
    _seed(layer, "SPY", [DATES[0]], [100.0])
    result = _run(_NullStrategy(), layer, dates=[DATES[0]], cash_annualized_rate=0.04)
    expected = (Decimal("10000") * (Decimal("1") + Decimal("0.04") / Decimal("252"))).quantize(Decimal("0.01"))
    assert result.snapshots[0].cash == expected


def test_buy_fills_on_next_day_open(layer: BacktestDataLayer) -> None:
    """Order placed at close of day 1 fills at open of day 2; positions appear in day 2 snapshot."""
    # Constant prices, no interest, no slippage
    _seed(layer, "SPY", DATES, [100.0] * 5)

    # Buy 50% on day 1, hold thereafter
    strategy = _CapturingStrategy([[Target("SPY", 0.5)]] * 5)
    result = _run(strategy, layer)

    # Day 1 snapshot: no positions yet (order hasn't filled)
    assert result.snapshots[0].positions == {}
    # Day 2 snapshot: SPY now held
    assert "SPY" in result.snapshots[1].positions
    assert result.snapshots[1].positions["SPY"] > Decimal("0")


def test_nav_marks_to_market(layer: BacktestDataLayer) -> None:
    """NAV on day 2 equals cash + shares * close, not shares * avg_cost."""
    # Day 1 open/close = 100. Day 2 open = 100 (fill), close = 110 (mark-to-market).
    closes = [100.0, 110.0, 110.0, 110.0, 110.0]
    opens  = [100.0, 100.0, 100.0, 100.0, 100.0]
    _seed(layer, "SPY", DATES, closes, opens)

    # Buy 100% on day 1 (target_qty = 10000 / 100 = 100 shares)
    strategy = _CapturingStrategy([[Target("SPY", 1.0)]] * 5)
    result = _run(strategy, layer)

    snap = result.snapshots[1]  # day 2: after fill at 100, close at 110
    # cash = 0, position = 100 shares * 110 = 11000
    assert snap.nav == Decimal("11000")
    assert snap.cash == Decimal("0")


def test_position_state_empty_before_first_fill(layer: BacktestDataLayer) -> None:
    """Before any fill, position_state passed to strategy is empty."""
    _seed(layer, "SPY", DATES, [100.0] * 5)
    strategy = _CapturingStrategy([[Target("SPY", 0.5)]] * 5)
    _run(strategy, layer)
    # Day 1 (index 0): no fills yet
    assert strategy.captured_states[0] == {}


def test_position_state_days_held_zero_on_entry_day(layer: BacktestDataLayer) -> None:
    """On the day a buy fills, the strategy is called with days_held=0."""
    _seed(layer, "SPY", DATES, [100.0] * 5)
    strategy = _CapturingStrategy([[Target("SPY", 0.5)]] * 5)
    _run(strategy, layer)
    # Day 2 (index 1): fill just happened, days_held should be 0
    assert strategy.captured_states[1]["SPY"]["days_held"] == 0


def test_position_state_days_held_increments(layer: BacktestDataLayer) -> None:
    """days_held increments by 1 each trading day after entry."""
    _seed(layer, "SPY", DATES, [100.0] * 5)
    strategy = _CapturingStrategy([[Target("SPY", 0.5)]] * 5)
    _run(strategy, layer)
    # Day 3 → days_held=1, Day 4 → 2, Day 5 → 3
    assert strategy.captured_states[2]["SPY"]["days_held"] == 1
    assert strategy.captured_states[3]["SPY"]["days_held"] == 2
    assert strategy.captured_states[4]["SPY"]["days_held"] == 3


def test_position_state_highest_close_tracks_max(layer: BacktestDataLayer) -> None:
    """highest_close_since_entry is the running maximum close since entry."""
    # Day 1=100, Day 2=108 (new high), Day 3=105 (pullback), Day 4=112 (new high), Day 5=109
    closes = [100.0, 108.0, 105.0, 112.0, 109.0]
    _seed(layer, "SPY", DATES, closes)

    strategy = _CapturingStrategy([[Target("SPY", 0.5)]] * 5)
    _run(strategy, layer)

    # Fill happens on day 2 at open=108. After step 4 on day 2: highest = max(108, 108) = 108.
    # Strategy on day 2 sees highest = 108.
    assert strategy.captured_states[1]["SPY"]["highest_close_since_entry"] == pytest.approx(108.0)
    # Day 3 close=105 < 108: no update
    assert strategy.captured_states[2]["SPY"]["highest_close_since_entry"] == pytest.approx(108.0)
    # Day 4 close=112 > 108: update to 112
    assert strategy.captured_states[3]["SPY"]["highest_close_since_entry"] == pytest.approx(112.0)
    # Day 5 close=109 < 112: no update
    assert strategy.captured_states[4]["SPY"]["highest_close_since_entry"] == pytest.approx(112.0)


def test_position_state_entry_price_is_fill_price(layer: BacktestDataLayer) -> None:
    """entry_price in position_state matches the actual fill price (open + slippage)."""
    # Day 1 close=100 (signal). Day 2 open=102 (fill). slippage=10bps → fill=102.1020
    closes = [100.0, 102.0, 102.0, 102.0, 102.0]
    opens = [100.0, 102.0, 102.0, 102.0, 102.0]
    _seed(layer, "SPY", DATES, closes, opens)

    strategy = _CapturingStrategy([[Target("SPY", 0.5)]] * 5)
    _run(strategy, layer, slippage_bps=10.0)  # 10bps

    fill_price = 102.0 * 1.001  # = 102.102
    assert strategy.captured_states[1]["SPY"]["entry_price"] == pytest.approx(fill_price, rel=1e-4)


def test_trade_record_entry_and_exit(layer: BacktestDataLayer) -> None:
    """Closed trade has correct entry_date, exit_date, entry_price, exit_price, pnl."""
    # Prices: day1_close=100(signal), day2_open=100(buy fill), day2_close=100,
    #         day3=100 (hold), day4=100 (exit signal), day5_open=105 (sell fill)
    closes = [100.0, 100.0, 100.0, 100.0, 105.0]
    opens  = [100.0, 100.0, 100.0, 100.0, 105.0]
    _seed(layer, "SPY", DATES, closes, opens)

    targets = [
        [Target("SPY", 0.5)],                                       # day 1: buy
        [Target("SPY", 0.5)],                                       # day 2: hold
        [Target("SPY", 0.5)],                                       # day 3: hold
        [Target("SPY", 0.0, rationale={"reason": "hard_stop"})],   # day 4: sell
        [],                                                          # day 5: sell fills
    ]
    strategy = _CapturingStrategy(targets)
    result = _run(strategy, layer)

    closed = [t for t in result.trades if t.exit_date is not None and t.exit_reason != "end_of_simulation"]
    assert len(closed) == 1
    trade = closed[0]
    assert trade.entry_date == DATES[1]           # fill on day 2
    assert trade.exit_date == DATES[4]            # fill on day 5
    assert trade.entry_price == Decimal("100.0000")
    assert trade.exit_price == Decimal("105.0000")
    assert trade.pnl is not None
    assert trade.pnl > Decimal("0")              # profitable exit


def test_trade_record_exit_reason_from_strategy(layer: BacktestDataLayer) -> None:
    """Exit reason from strategy rationale (hard_stop/trailing_stop/time_stop) is recorded."""
    _seed(layer, "SPY", DATES, [100.0] * 5)
    targets = [
        [Target("SPY", 0.5)],
        [Target("SPY", 0.5)],
        [Target("SPY", 0.5)],
        [Target("SPY", 0.0, rationale={"reason": "trailing_stop"})],
        [],
    ]
    strategy = _CapturingStrategy(targets)
    result = _run(strategy, layer)

    closed = [t for t in result.trades if t.exit_reason == "trailing_stop"]
    assert len(closed) == 1


def test_end_of_simulation_closes_open_trades(layer: BacktestDataLayer) -> None:
    """Positions still open on the last day are recorded as end_of_simulation exits."""
    _seed(layer, "SPY", DATES, [100.0] * 5)
    strategy = _CapturingStrategy([[Target("SPY", 0.5)]] * 5)
    result = _run(strategy, layer)

    eos = [t for t in result.trades if t.exit_reason == "end_of_simulation"]
    assert len(eos) == 1
    assert eos[0].exit_date == DATES[-1]


def test_nav_series_length_and_dates(layer: BacktestDataLayer) -> None:
    """nav_series() returns a float Series indexed by sim_date."""
    _seed(layer, "SPY", DATES, [100.0] * 5)
    result = _run(_NullStrategy(), layer)
    series = result.nav_series()
    assert len(series) == len(DATES)
    assert series.dtype == float
    assert list(series.index) == DATES


def test_sell_order_omitted_on_last_day(layer: BacktestDataLayer) -> None:
    """Pending orders on the last simulation day are not processed (no next open)."""
    # Strategy exits on the final day — the sell should NOT fill (no day+1 open)
    _seed(layer, "SPY", DATES, [100.0] * 5)
    targets = [
        [Target("SPY", 0.5)],                                         # day 1: buy
        [Target("SPY", 0.5)],                                         # day 2: hold
        [Target("SPY", 0.5)],                                         # day 3: hold
        [Target("SPY", 0.5)],                                         # day 4: hold
        [Target("SPY", 0.0, rationale={"reason": "time_stop"})],      # day 5 (last): exit
    ]
    strategy = _CapturingStrategy(targets)
    result = _run(strategy, layer)

    # The sell signal on the last day cannot fill — position closes as end_of_simulation
    eos = [t for t in result.trades if t.exit_reason == "end_of_simulation"]
    assert len(eos) == 1


def test_result_metadata(layer: BacktestDataLayer) -> None:
    """SimulationResult captures strategy name, dates, and capital correctly."""
    _seed(layer, "SPY", DATES, [100.0] * 5)
    strategy = _NullStrategy()
    result = _run(strategy, layer, starting_capital=Decimal("5000"))
    assert result.strategy_name == "null"
    assert result.start_date == DATES[0]
    assert result.end_date == DATES[-1]
    assert result.starting_capital == Decimal("5000")
    assert result.run_timestamp is not None
