"""Tests for src/backtest/reporting.py — BacktestStats computation and report generation."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.backtest.engine import DaySnapshot, SimulationResult, TradeRecord
from src.backtest.reporting import (
    BacktestStats,
    _drawdown_series,
    compute_stats,
    generate_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    nav_values: list[float],
    trades: list[TradeRecord] | None = None,
    strategy_name: str = "test",
    cash_rate: float = 0.04,
    starting_capital: float | None = None,
) -> SimulationResult:
    """Build a minimal SimulationResult from a list of NAV values."""
    # Use weekday-only dates starting from a Monday
    base = date(2020, 1, 6)  # Monday
    calendar_dates: list[date] = []
    d = base
    while len(calendar_dates) < len(nav_values):
        if d.weekday() < 5:
            calendar_dates.append(d)
        d += timedelta(days=1)

    cap = Decimal(str(starting_capital if starting_capital is not None else nav_values[0]))
    snapshots = [
        DaySnapshot(
            sim_date=dt,
            nav=Decimal(str(v)),
            cash=Decimal(str(v)),
            positions={},
            universe_size=5,
        )
        for dt, v in zip(calendar_dates, nav_values)
    ]
    return SimulationResult(
        strategy_name=strategy_name,
        params={},
        start_date=calendar_dates[0],
        end_date=calendar_dates[-1],
        starting_capital=cap,
        slippage_bps=5.0,
        cash_annualized_rate=cash_rate,
        snapshots=snapshots,
        trades=trades or [],
        cache_version=None,
        run_timestamp=datetime.now(tz=timezone.utc),
    )


def _make_trade(
    entry_price: float,
    exit_price: float,
    shares: float = 10.0,
    symbol: str = "SPY",
    exit_reason: str = "strategy",
) -> TradeRecord:
    return TradeRecord(
        symbol=symbol,
        entry_date=date(2020, 1, 6),
        entry_price=Decimal(str(entry_price)),
        shares=Decimal(str(shares)),
        exit_date=date(2020, 1, 16),
        exit_price=Decimal(str(exit_price)),
        exit_reason=exit_reason,
    )


# ---------------------------------------------------------------------------
# _drawdown_series
# ---------------------------------------------------------------------------


def test_drawdown_series_flat_nav() -> None:
    """Flat NAV has zero drawdown throughout."""
    nav = pd.Series([100.0, 100.0, 100.0])
    dd = _drawdown_series(nav)
    assert (dd == 0.0).all()


def test_drawdown_series_peak_to_trough() -> None:
    """Nav goes 100 → 125 → 100: drawdown at last point is -20%."""
    nav = pd.Series([100.0, 125.0, 100.0])
    dd = _drawdown_series(nav)
    assert dd.iloc[0] == pytest.approx(0.0)
    assert dd.iloc[1] == pytest.approx(0.0)
    assert dd.iloc[2] == pytest.approx(-0.20, rel=1e-6)


def test_drawdown_series_monotone_up() -> None:
    """Monotonically rising NAV has zero drawdown."""
    nav = pd.Series([100.0, 110.0, 120.0, 130.0])
    dd = _drawdown_series(nav)
    assert (dd == 0.0).all()


# ---------------------------------------------------------------------------
# compute_stats — return metrics
# ---------------------------------------------------------------------------


def test_total_return_positive() -> None:
    """Total return = (final / initial) - 1."""
    result = _make_result([10000.0, 11000.0])
    stats = compute_stats(result)
    assert stats.total_return == pytest.approx(0.10, rel=1e-6)


def test_total_return_negative() -> None:
    result = _make_result([10000.0, 9000.0])
    stats = compute_stats(result)
    assert stats.total_return == pytest.approx(-0.10, rel=1e-6)


def test_annualized_return_direction() -> None:
    """A 10% gain over 252 days should annualize close to 10%."""
    # 253 nav points → 252 daily returns
    daily = (1.10) ** (1 / 252)
    nav = [10000.0 * (daily ** i) for i in range(253)]
    result = _make_result(nav)
    stats = compute_stats(result)
    # Formula is (1+total)^(252/n_days) where n_days=253, so slightly under 10%
    assert stats.annualized_return == pytest.approx(
        (1.10) ** (252 / 253) - 1, rel=1e-4
    )


def test_annualized_volatility_zero_for_flat_nav() -> None:
    """Flat NAV → vol = 0 → Sharpe = nan."""
    result = _make_result([10000.0] * 10)
    stats = compute_stats(result)
    assert stats.annualized_volatility == pytest.approx(0.0, abs=1e-9)
    assert math.isnan(stats.sharpe_ratio)


def test_sharpe_positive_for_steady_gain() -> None:
    """Steadily rising NAV with low variance → positive Sharpe."""
    nav = [10000.0 + i * 5 for i in range(252)]  # linear gain of ~$1250
    result = _make_result(nav)
    stats = compute_stats(result)
    assert stats.sharpe_ratio > 0


def test_sortino_nan_when_no_down_days() -> None:
    """No negative daily returns → downside vol = 0 → Sortino = nan."""
    nav = [100.0 + i for i in range(20)]  # strictly increasing
    result = _make_result(nav)
    stats = compute_stats(result)
    assert math.isnan(stats.sortino_ratio)


# ---------------------------------------------------------------------------
# compute_stats — drawdown / risk
# ---------------------------------------------------------------------------


def test_max_drawdown_computed_correctly() -> None:
    """Nav 100 → 125 → 100: max drawdown = (100-125)/125 = -20%."""
    result = _make_result([100.0, 125.0, 100.0, 125.0, 100.0])
    stats = compute_stats(result)
    assert stats.max_drawdown == pytest.approx(-0.20, rel=1e-6)


def test_max_drawdown_zero_for_monotone_up() -> None:
    result = _make_result([100.0, 110.0, 120.0, 130.0])
    stats = compute_stats(result)
    assert stats.max_drawdown == pytest.approx(0.0, abs=1e-9)
    assert math.isnan(stats.calmar_ratio)


def test_time_underwater() -> None:
    """Nav 100 → 125 → 100 → 125 → 100: 2 of 5 points are below peak → 40%."""
    result = _make_result([100.0, 125.0, 100.0, 125.0, 100.0])
    stats = compute_stats(result)
    # Points below prior peak: index 2 and 4
    assert stats.time_underwater == pytest.approx(2 / 5, rel=1e-6)


def test_calmar_positive_for_profitable_strategy() -> None:
    """Positive return + negative drawdown → positive Calmar."""
    # Mostly up, one dip
    nav = [100.0 + i * 0.5 for i in range(50)] + [120.0 - i * 0.3 for i in range(10)] + [125.0 + i * 0.5 for i in range(50)]
    result = _make_result(nav)
    stats = compute_stats(result)
    assert stats.calmar_ratio > 0


# ---------------------------------------------------------------------------
# compute_stats — trade metrics
# ---------------------------------------------------------------------------


def test_no_trades_returns_none_metrics() -> None:
    """Zero trades → win_rate, profit_factor, etc. are all None."""
    result = _make_result([10000.0, 10100.0])
    stats = compute_stats(result)
    assert stats.win_rate is None
    assert stats.profit_factor is None
    assert stats.avg_winner_pct is None
    assert stats.avg_loser_pct is None
    assert stats.avg_hold_days is None
    assert stats.num_trades == 0
    assert stats.longest_losing_streak == 0


def test_win_rate_two_wins_one_loss() -> None:
    trades = [
        _make_trade(100, 110),  # +10%
        _make_trade(100, 105),  # +5%
        _make_trade(100, 95),   # -5%
    ]
    result = _make_result([10000.0, 11000.0], trades=trades)
    stats = compute_stats(result)
    assert stats.win_rate == pytest.approx(2 / 3, rel=1e-6)
    assert stats.num_trades == 3


def test_profit_factor() -> None:
    """Gross profit / gross loss."""
    # Winner: $100 profit. Loser: $50 loss. Profit factor = 100/50 = 2.0
    trades = [
        _make_trade(100, 110, shares=10),   # pnl = (110-100)*10 = +$100
        _make_trade(100, 95,  shares=10),   # pnl = (95-100)*10  = -$50
    ]
    result = _make_result([10000.0, 10000.0], trades=trades)
    stats = compute_stats(result)
    assert stats.profit_factor == pytest.approx(2.0, rel=1e-6)


def test_profit_factor_inf_when_no_losers() -> None:
    trades = [_make_trade(100, 110)]
    result = _make_result([10000.0, 10000.0], trades=trades)
    stats = compute_stats(result)
    assert math.isinf(stats.profit_factor)  # type: ignore[arg-type]


def test_avg_winner_loser_pct() -> None:
    """Average winner = mean of winning pnl_pcts; loser = mean of losing."""
    trades = [
        _make_trade(100, 120),  # +20%
        _make_trade(100, 110),  # +10%
        _make_trade(100, 90),   # -10%
    ]
    result = _make_result([10000.0, 10000.0], trades=trades)
    stats = compute_stats(result)
    assert stats.avg_winner_pct == pytest.approx(0.15, rel=1e-6)  # mean of 20% and 10%
    assert stats.avg_loser_pct == pytest.approx(-0.10, rel=1e-6)


def test_longest_losing_streak() -> None:
    trades = [
        _make_trade(100, 90),   # loss
        _make_trade(100, 90),   # loss
        _make_trade(100, 90),   # loss
        _make_trade(100, 110),  # win
        _make_trade(100, 90),   # loss
    ]
    result = _make_result([10000.0, 10000.0], trades=trades)
    stats = compute_stats(result)
    assert stats.longest_losing_streak == 3


def test_avg_hold_days() -> None:
    """Avg hold = mean calendar days between entry and exit."""
    t = TradeRecord(
        symbol="SPY",
        entry_date=date(2020, 1, 6),
        entry_price=Decimal("100"),
        shares=Decimal("10"),
        exit_date=date(2020, 1, 16),   # 10 calendar days later
        exit_price=Decimal("110"),
        exit_reason="strategy",
    )
    result = _make_result([10000.0, 10000.0], trades=[t])
    stats = compute_stats(result)
    assert stats.avg_hold_days == pytest.approx(10.0, rel=1e-6)


# ---------------------------------------------------------------------------
# generate_report — integration (file output)
# ---------------------------------------------------------------------------


def test_generate_report_creates_required_files(tmp_path: Path) -> None:
    """generate_report must write report.html, three CSVs, and metadata.json."""
    result = _make_result([10000.0, 10500.0, 10300.0, 10800.0])
    report_path = generate_report(result, output_dir=tmp_path / "rpt")

    assert report_path.exists()
    assert (tmp_path / "rpt" / "trade_log.csv").exists()
    assert (tmp_path / "rpt" / "equity_curve.csv").exists()
    assert (tmp_path / "rpt" / "statistics.csv").exists()
    assert (tmp_path / "rpt" / "metadata.json").exists()


def test_report_html_contains_required_sections(tmp_path: Path) -> None:
    """HTML report must contain the mandatory caveats and section headers."""
    result = _make_result([10000.0, 10100.0, 10050.0])
    report_path = generate_report(result, output_dir=tmp_path / "rpt")
    html = report_path.read_text(encoding="utf-8")

    assert "Data Integrity Caveats" in html
    assert "Past performance does not predict future returns" in html
    assert "Equity Curve" in html
    assert "Drawdown" in html
    assert "Summary Statistics" in html
    assert "Trade Log" in html


def test_trade_log_csv_columns(tmp_path: Path) -> None:
    """trade_log.csv must have the expected column headers."""
    trades = [_make_trade(100, 110)]
    result = _make_result([10000.0, 10100.0], trades=trades)
    generate_report(result, output_dir=tmp_path / "rpt")

    df = pd.read_csv(tmp_path / "rpt" / "trade_log.csv")
    required = {"entry_date", "exit_date", "symbol", "entry_price",
                "exit_price", "shares", "pnl_dollars", "pnl_pct", "exit_reason"}
    assert required.issubset(set(df.columns))


def test_equity_curve_csv_contains_strategy_column(tmp_path: Path) -> None:
    """equity_curve.csv must have a column for the primary strategy."""
    result = _make_result([10000.0, 10100.0], strategy_name="my_strategy")
    generate_report(result, output_dir=tmp_path / "rpt")

    df = pd.read_csv(tmp_path / "rpt" / "equity_curve.csv", index_col="date")
    assert "my_strategy" in df.columns


def test_statistics_csv_one_row_per_strategy(tmp_path: Path) -> None:
    """statistics.csv has one row per strategy (primary + benchmarks)."""
    strategy = _make_result([10000.0, 10100.0], strategy_name="main")
    benchmark = _make_result([10000.0, 10050.0], strategy_name="bench")
    generate_report(strategy, benchmark_results=[benchmark], output_dir=tmp_path / "rpt")

    df = pd.read_csv(tmp_path / "rpt" / "statistics.csv")
    assert len(df) == 2
    assert set(df["strategy"]) == {"main", "bench"}


def test_generate_report_with_config_writes_yaml(tmp_path: Path) -> None:
    """When a config dict is passed, config.yaml should be written."""
    result = _make_result([10000.0, 10100.0])
    cfg = {"strategy": {"class": "Test"}, "simulation": {"start_date": "2020-01-01"}}
    generate_report(result, output_dir=tmp_path / "rpt", config=cfg)

    assert (tmp_path / "rpt" / "config.yaml").exists()


def test_generate_report_benchmark_appears_in_equity_csv(tmp_path: Path) -> None:
    """Benchmark NAV series appears as a column in equity_curve.csv."""
    strategy = _make_result([10000.0, 10100.0, 10200.0], strategy_name="strat")
    bench = _make_result([10000.0, 10050.0, 10080.0], strategy_name="spy_bench")
    generate_report(strategy, benchmark_results=[bench], output_dir=tmp_path / "rpt")

    df = pd.read_csv(tmp_path / "rpt" / "equity_curve.csv", index_col="date")
    assert "strat" in df.columns
    assert "spy_bench" in df.columns


def test_metadata_json_contains_key_fields(tmp_path: Path) -> None:
    """metadata.json must contain strategy name, dates, and capital."""
    import json
    result = _make_result([10000.0, 10100.0], strategy_name="momentum")
    generate_report(result, output_dir=tmp_path / "rpt")

    meta = json.loads((tmp_path / "rpt" / "metadata.json").read_text())
    assert meta["strategy_name"] == "momentum"
    assert "start_date" in meta
    assert "end_date" in meta
    assert "starting_capital" in meta
    assert "run_timestamp" in meta
