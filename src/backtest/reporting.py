"""Backtest reporting layer.

Stateless: given one or more SimulationResult objects, produces a report
directory containing report.html, CSVs, and metadata files. Can be called
after the fact to regenerate reports without re-running the simulation.

Usage::

    from src.backtest.reporting import compute_stats, generate_report

    strategy_result = run_simulation(...)
    benchmark_result = run_simulation(BuyAndHoldSPY(), ...)
    report_path = generate_report(
        strategy_result=strategy_result,
        benchmark_results=[benchmark_result],
        output_dir=Path("backtests/momentum_2018_baseline/"),
    )
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.backtest.engine import SimulationResult, TradeRecord


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------


@dataclass
class BacktestStats:
    """Summary performance metrics for one simulation run."""
    strategy_name: str
    total_return: float
    annualized_return: float
    annualized_volatility: float
    sharpe_ratio: float           # nan when vol == 0
    sortino_ratio: float          # nan when downside vol == 0
    max_drawdown: float           # <= 0, e.g. -0.15 means -15%
    calmar_ratio: float           # nan when max_drawdown == 0
    win_rate: float | None        # None when num_trades == 0
    profit_factor: float | None   # None when no trades; inf when no losing trades
    avg_winner_pct: float | None  # mean return of winning trades; None when no winners
    avg_loser_pct: float | None   # mean return of losing trades (negative); None when no losers
    num_trades: int
    avg_hold_days: float | None   # None when no trades
    time_underwater: float        # fraction of days below prior peak
    longest_losing_streak: int


def _drawdown_series(nav: pd.Series) -> pd.Series:
    """Return drawdown fraction from running peak (values <= 0)."""
    peak = nav.cummax()
    return (nav - peak) / peak


def compute_stats(result: SimulationResult, risk_free_rate: float = 0.04) -> BacktestStats:
    """Compute standard performance metrics from a SimulationResult.

    Args:
        result: Completed simulation output.
        risk_free_rate: Annualized risk-free rate used in Sharpe / Sortino.

    Returns:
        BacktestStats with all metrics computed.
    """
    nav = result.nav_series()

    # Edge case: single day or empty — return zeroed stats
    if len(nav) < 2:
        return BacktestStats(
            strategy_name=result.strategy_name,
            total_return=0.0, annualized_return=0.0, annualized_volatility=0.0,
            sharpe_ratio=float("nan"), sortino_ratio=float("nan"),
            max_drawdown=0.0, calmar_ratio=float("nan"),
            win_rate=None, profit_factor=None,
            avg_winner_pct=None, avg_loser_pct=None,
            num_trades=0, avg_hold_days=None,
            time_underwater=0.0, longest_losing_streak=0,
        )

    daily_returns = nav.pct_change().dropna()
    n_days = len(nav)

    # ---- Return metrics ----
    total_return = float(nav.iloc[-1] / nav.iloc[0] - 1)
    annualized_return = float((1.0 + total_return) ** (252.0 / n_days) - 1.0)

    # ---- Risk metrics ----
    ann_vol = float(daily_returns.std(ddof=1) * math.sqrt(252))
    sharpe = (annualized_return - risk_free_rate) / ann_vol if ann_vol > 1e-12 else float("nan")

    downside = daily_returns[daily_returns < 0]
    if len(downside) > 0:
        downside_vol = float(math.sqrt(float((downside ** 2).mean())) * math.sqrt(252))
        sortino = (annualized_return - risk_free_rate) / downside_vol if downside_vol > 1e-12 else float("nan")
    else:
        sortino = float("nan")

    # ---- Drawdown ----
    dd = _drawdown_series(nav)
    max_dd = float(dd.min())
    calmar = annualized_return / abs(max_dd) if max_dd < -1e-12 else float("nan")
    time_uw = float((dd < 0).sum() / len(dd))

    # ---- Trade metrics ----
    completed = [t for t in result.trades if t.exit_price is not None and t.pnl_pct is not None]
    num_trades = len(completed)

    if num_trades > 0:
        pnl_pcts = [t.pnl_pct for t in completed]          # type: ignore[misc]
        pnl_dollars = [float(t.pnl) for t in completed if t.pnl is not None]

        winners = [p for p in pnl_pcts if p > 0]
        losers  = [p for p in pnl_pcts if p <= 0]

        win_rate = len(winners) / num_trades
        gross_profit = sum(p for p in pnl_dollars if p > 0)
        gross_loss   = abs(sum(p for p in pnl_dollars if p <= 0))
        if gross_loss > 1e-12:
            profit_factor: float | None = gross_profit / gross_loss
        elif gross_profit > 1e-12:
            profit_factor = float("inf")
        else:
            profit_factor = float("nan")

        avg_winner = float(sum(winners) / len(winners)) if winners else None
        avg_loser  = float(sum(losers)  / len(losers))  if losers  else None

        hold_days_list = [(t.exit_date - t.entry_date).days for t in completed]
        avg_hold = float(sum(hold_days_list) / len(hold_days_list))

        # Longest consecutive losing streak
        streak = max_streak = 0
        for pct in pnl_pcts:
            if pct <= 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
    else:
        win_rate = profit_factor = avg_winner = avg_loser = avg_hold = None
        max_streak = 0

    return BacktestStats(
        strategy_name=result.strategy_name,
        total_return=total_return,
        annualized_return=annualized_return,
        annualized_volatility=ann_vol,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown=max_dd,
        calmar_ratio=calmar,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_winner_pct=avg_winner,
        avg_loser_pct=avg_loser,
        num_trades=num_trades,
        avg_hold_days=avg_hold,
        time_underwater=time_uw,
        longest_losing_streak=max_streak,
    )


# ---------------------------------------------------------------------------
# Plotly charts
# ---------------------------------------------------------------------------


def _equity_chart(nav_dict: dict[str, pd.Series], starting_capital: float) -> str:
    """Interactive equity curve chart (first chart — embeds Plotly JS)."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        return "<p><em>Install plotly to view charts.</em></p>"

    fig = go.Figure()
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]

    for i, (name, series) in enumerate(nav_dict.items()):
        dash = "dot" if name.startswith("Cash") else "solid"
        fig.add_trace(go.Scatter(
            x=list(series.index),
            y=list(series.values),
            name=name,
            mode="lines",
            line=dict(color=colors[i % len(colors)], dash=dash),
            hovertemplate="%{x|%Y-%m-%d}: $%{y:,.2f}<extra>" + name + "</extra>",
        ))

    fig.update_layout(
        title="Equity Curve",
        xaxis_title="Date",
        yaxis_title="NAV ($)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        height=450,
        margin=dict(l=60, r=20, t=60, b=40),
    )
    return fig.to_html(full_html=False, include_plotlyjs=True)


def _drawdown_chart(nav_dict: dict[str, pd.Series]) -> str:
    """Drawdown chart (reuses Plotly JS embedded in equity chart)."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        return "<p><em>Install plotly to view charts.</em></p>"

    fig = go.Figure()
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]

    for i, (name, series) in enumerate(nav_dict.items()):
        if name.startswith("Cash"):
            continue   # flat cash floor has no meaningful drawdown
        dd = _drawdown_series(series) * 100  # as percent
        fig.add_trace(go.Scatter(
            x=list(dd.index),
            y=list(dd.values),
            name=name,
            mode="lines",
            fill="tozeroy",
            line=dict(color=colors[i % len(colors)]),
            hovertemplate="%{x|%Y-%m-%d}: %{y:.1f}%<extra>" + name + "</extra>",
        ))

    fig.update_layout(
        title="Drawdown from Peak",
        xaxis_title="Date",
        yaxis_title="Drawdown (%)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        height=350,
        margin=dict(l=60, r=20, t=60, b=40),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _monthly_heatmap(result: SimulationResult) -> str:
    """Monthly returns heatmap (reuses Plotly JS)."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        return "<p><em>Install plotly to view charts.</em></p>"

    nav = result.nav_series()
    nav.index = pd.to_datetime(nav.index)
    monthly = nav.resample("ME").last()
    monthly_ret = monthly.pct_change() * 100   # in percent

    if monthly_ret.dropna().empty:
        return "<p><em>Not enough data for monthly heatmap.</em></p>"

    years = sorted(monthly_ret.index.year.unique())
    months = list(range(1, 13))
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    z: list[list[float | None]] = []
    text: list[list[str]] = []
    for yr in years:
        row_z: list[float | None] = []
        row_t: list[str] = []
        for mo in months:
            idx = (monthly_ret.index.year == yr) & (monthly_ret.index.month == mo)
            vals = monthly_ret[idx]
            if vals.empty or pd.isna(vals.iloc[0]):
                row_z.append(None)
                row_t.append("")
            else:
                v = float(vals.iloc[0])
                row_z.append(v)
                row_t.append(f"{v:+.1f}%")
        z.append(row_z)
        text.append(row_t)

    # symmetric colorscale around 0
    all_vals = [v for row in z for v in row if v is not None]
    zmax = max(abs(v) for v in all_vals) if all_vals else 5.0

    fig = go.Figure(data=go.Heatmap(
        z=z,
        x=month_names,
        y=[str(yr) for yr in years],
        text=text,
        texttemplate="%{text}",
        colorscale=[[0, "#c62828"], [0.5, "#f5f5f5"], [1, "#2e7d32"]],
        zmid=0,
        zmin=-zmax,
        zmax=zmax,
        hoverongaps=False,
        showscale=True,
    ))
    fig.update_layout(
        title="Monthly Returns",
        height=max(250, 50 + len(years) * 35),
        margin=dict(l=60, r=60, t=60, b=40),
        yaxis=dict(autorange="reversed"),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


# ---------------------------------------------------------------------------
# HTML table builders
# ---------------------------------------------------------------------------


def _fmt_pct(v: float | None, decimals: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v * 100:.{decimals}f}%"


def _fmt_ratio(v: float | None, decimals: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if math.isinf(v):
        return "∞"
    return f"{v:.{decimals}f}"


def _color(text: str, positive: bool) -> str:
    cls = "pos" if positive else "neg"
    return f'<span class="{cls}">{text}</span>'


def _stats_table_html(stats_list: list[BacktestStats]) -> str:
    """Build an HTML summary statistics table (metrics as rows, strategies as columns)."""
    cols = [s.strategy_name for s in stats_list]

    def _row(label: str, values: list[str]) -> str:
        cells = "".join(f"<td>{v}</td>" for v in values)
        return f"<tr><th>{label}</th>{cells}</tr>"

    def _pct_row(label: str, attr: str, flip: bool = False) -> str:
        vals = []
        for s in stats_list:
            v = getattr(s, attr)
            txt = _fmt_pct(v)
            if txt != "—":
                positive = (v > 0) != flip   # flip=True → positive is bad (e.g. drawdown)
                vals.append(_color(txt, positive))
            else:
                vals.append(txt)
        return _row(label, vals)

    def _ratio_row(label: str, attr: str) -> str:
        vals = []
        for s in stats_list:
            v = getattr(s, attr)
            txt = _fmt_ratio(v)
            if txt not in ("—", "∞"):
                vals.append(_color(txt, v > 0))
            else:
                vals.append(txt)
        return _row(label, vals)

    header = "<tr><th>Metric</th>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>"

    # Win rate / profit factor rows (None when not applicable)
    def _opt_pct_row(label: str, attr: str) -> str:
        vals = []
        for s in stats_list:
            v = getattr(s, attr)
            if v is None:
                vals.append("n/a")
            else:
                txt = _fmt_pct(v)
                vals.append(_color(txt, v > 0) if txt != "—" else txt)
        return _row(label, vals)

    def _opt_ratio_row(label: str, attr: str) -> str:
        vals = []
        for s in stats_list:
            v = getattr(s, attr)
            if v is None:
                vals.append("n/a")
            else:
                txt = _fmt_ratio(v)
                vals.append(_color(txt, v > 0) if txt not in ("—", "∞") else txt)
        return _row(label, vals)

    def _winner_loser_row() -> str:
        vals = []
        for s in stats_list:
            if s.avg_winner_pct is None and s.avg_loser_pct is None:
                vals.append("n/a")
            else:
                w = _fmt_pct(s.avg_winner_pct) if s.avg_winner_pct is not None else "—"
                l = _fmt_pct(s.avg_loser_pct) if s.avg_loser_pct is not None else "—"
                vals.append(f'<span class="pos">{w}</span> / <span class="neg">{l}</span>')
        return _row("Avg winner / loser", vals)

    def _hold_row() -> str:
        vals = []
        for s in stats_list:
            vals.append("n/a" if s.avg_hold_days is None else f"{s.avg_hold_days:.1f} days")
        return _row("Avg trade duration", vals)

    rows = [
        _pct_row("Total return", "total_return"),
        _pct_row("Annualized return", "annualized_return"),
        _pct_row("Annualized volatility", "annualized_volatility", flip=True),
        _ratio_row("Sharpe ratio", "sharpe_ratio"),
        _ratio_row("Sortino ratio", "sortino_ratio"),
        _pct_row("Maximum drawdown", "max_drawdown", flip=True),
        _ratio_row("Calmar ratio", "calmar_ratio"),
        _opt_pct_row("Win rate", "win_rate"),
        _opt_ratio_row("Profit factor", "profit_factor"),
        _winner_loser_row(),
        _row("Number of trades", [str(s.num_trades) for s in stats_list]),
        _hold_row(),
        _pct_row("Time underwater", "time_underwater", flip=True),
        _row("Longest losing streak", [str(s.longest_losing_streak) for s in stats_list]),
    ]

    body = "\n".join(rows)
    return f"<table><thead>{header}</thead><tbody>{body}</tbody></table>"


def _trade_log_table_html(trades: list[TradeRecord]) -> str:
    """HTML table for up to 50 trades."""
    if not trades:
        return "<p><em>No trades in this simulation.</em></p>"

    header = (
        "<tr><th>Entry date</th><th>Exit date</th><th>Symbol</th>"
        "<th>Entry price</th><th>Exit price</th><th>Shares</th>"
        "<th>P&amp;L ($)</th><th>P&amp;L (%)</th><th>Exit reason</th></tr>"
    )

    rows: list[str] = []
    for t in trades:
        pnl_d = f"${float(t.pnl):+,.2f}" if t.pnl is not None else "—"
        pnl_p = f"{t.pnl_pct * 100:+.2f}%" if t.pnl_pct is not None else "—"
        pnl_cls = "pos" if (t.pnl is not None and t.pnl > 0) else "neg"
        rows.append(
            f"<tr>"
            f"<td>{t.entry_date}</td>"
            f"<td>{t.exit_date or '—'}</td>"
            f"<td>{t.symbol}</td>"
            f"<td>${float(t.entry_price):.4f}</td>"
            f"<td>{'$' + f'{float(t.exit_price):.4f}' if t.exit_price else '—'}</td>"
            f"<td>{float(t.shares):.4f}</td>"
            f'<td class="{pnl_cls}">{pnl_d}</td>'
            f'<td class="{pnl_cls}">{pnl_p}</td>'
            f"<td>{t.exit_reason or '—'}</td>"
            f"</tr>"
        )

    body = "\n".join(rows)
    return f"<table><thead>{header}</thead><tbody>{body}</tbody></table>"


def _universe_diagnostics_html(result: SimulationResult) -> str:
    """Plain-text diagnostics about universe size variation during the simulation."""
    if not result.snapshots:
        return "<p>No snapshots.</p>"

    sizes = [s.universe_size for s in result.snapshots]
    max_size = max(sizes)
    min_size = min(sizes)
    reduced_days = sum(1 for s in sizes if s < max_size)
    first_date = result.snapshots[0].sim_date
    last_date = result.snapshots[-1].sim_date

    lines = [
        f"<p><strong>Simulation period:</strong> {first_date} → {last_date} ({len(result.snapshots)} trading days)</p>",
        f"<p><strong>Universe size:</strong> max {max_size} symbols, min {min_size} symbols</p>",
    ]
    if reduced_days > 0:
        lines.append(
            f"<p><strong>Days with reduced universe:</strong> {reduced_days} "
            f"({reduced_days / len(result.snapshots) * 100:.1f}% of trading days). "
            f"Likely caused by symbols with limited history (XLRE from Oct 2015, XLC from Jun 2018).</p>"
        )
    else:
        lines.append("<p>Universe size was constant throughout the simulation period.</p>")

    traded = sorted({t.symbol for t in result.trades})
    if traded:
        lines.append(f"<p><strong>Symbols traded:</strong> {', '.join(traded)}</p>")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Canonical caveats text (from spec §8 — do not modify at runtime)
# ---------------------------------------------------------------------------

_CAVEATS_HTML = """
<h3>What this backtest cannot tell you</h3>
<ul>
<li><strong>Past performance does not predict future returns.</strong>
A strategy that worked in 2018–2025 may not work in 2026–2030.
Markets change, factor premia decay, and strategies that have entered the public domain are arbitraged.</li>

<li><strong>Live execution will be worse than backtest execution.</strong>
Slippage, partial fills, exchange outages, broker errors, and timing discrepancies all degrade live performance
vs. the simulated execution. Plan for live results to be 1–3% annualized worse than backtest results,
even if everything works correctly.</li>

<li><strong>Single-period results are noisy.</strong>
An 8-year backtest is not enough data to confidently estimate the strategy's long-run characteristics.
The Sharpe ratio computed from this backtest has a standard error roughly equal to the Sharpe itself —
a Sharpe of 0.8 might really be 0.0 to 1.6.</li>

<li><strong>Multiple-testing risk.</strong>
If the strategy or its parameters were chosen with knowledge of historical performance, the backtest is
contaminated by selection bias. The first time a strategy is run on historical data, results are informative.
After that, every parameter tweak that produces "better" results on the same data is at risk of fitting noise.</li>

<li><strong>yfinance data has known imperfections.</strong>
Occasional gaps, late dividend adjustments, and rare price errors are present.
These are unlikely to materially change conclusions but are not zero.</li>

<li><strong>Survivorship bias is minimal but not zero.</strong>
Our 16-ETF universe is currently complete (none have been delisted), but the universe was chosen with
knowledge that these ETFs have all survived. A truly survivorship-bias-free backtest would also include
ETFs that were active at the start of the period but have since been delisted.</li>

<li><strong>Slippage and cash interest are simulated, not measured.</strong>
The default 5 bps slippage and 4% cash rate are reasonable for the current environment.
Real values vary over time and can change conclusions if the strategy is sensitive to either.</li>

<li><strong>This is not investment advice.</strong>
Backtests are a tool for understanding strategy behavior, not a basis for sizing real-money positions.
Strategy decisions are the user's responsibility.</li>
</ul>

<div class="caveat-best-practices">
<h3>Best practices</h3>
<ul>
<li>Run the backtest exactly once on out-of-sample data before any parameter tuning.
That result is the closest thing to ground truth the harness can provide.</li>
<li>If parameter tuning is performed, do it on a train subset (e.g., 2018–2022),
then re-validate on a held-out period (e.g., 2023–2025) without further tuning.</li>
<li>Treat backtest results as a "might work" signal, not a "will work" guarantee.
Promotion to paper trading is the next validation step; live is the step after that.</li>
<li>Compare to multiple benchmarks, not just buy-and-hold.
A strategy that beats SPY but loses to 60/40 may be taking on uncompensated risk.</li>
<li>Pay attention to drawdown and time underwater, not just total return.
A strategy with high return and brutal drawdowns may be psychologically untradable in real life.</li>
</ul>
</div>
"""


# ---------------------------------------------------------------------------
# Jinja2 HTML template
# ---------------------------------------------------------------------------

_REPORT_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest: {{ strategy_name }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;color:#1a1a1a;line-height:1.5}
.container{max-width:1400px;margin:0 auto;padding:24px}
h1{font-size:1.75rem;margin-bottom:4px}
.subtitle{color:#555;margin-bottom:24px;font-size:.95rem}
section{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.08);padding:24px;margin-bottom:24px}
h2{font-size:1.2rem;margin-bottom:16px;color:#111;border-bottom:2px solid #e8e8e8;padding-bottom:8px}
h3{font-size:1rem;margin:16px 0 8px;color:#333}
table{border-collapse:collapse;width:100%;font-size:.875rem}
th{background:#f7f7f7;font-weight:600;border:1px solid #e0e0e0;padding:8px 12px;text-align:left}
td{border:1px solid #e0e0e0;padding:8px 12px}
tr:hover td{background:#fafafa}
.pos{color:#2e7d32;font-weight:500}
.neg{color:#c62828;font-weight:500}
.info-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;margin-bottom:16px}
.info-card{background:#f8f9fa;border:1px solid #e8e8e8;border-radius:6px;padding:12px 16px}
.info-card .label{font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;color:#666;margin-bottom:4px}
.info-card .value{font-size:1rem;font-weight:600}
.params-table{max-width:480px;margin-top:12px}
.caveats{background:#fff8e1;border-left:4px solid #f9a825}
.caveats h2{color:#b45309;border-bottom-color:#f9a825}
.caveats h3{color:#78350f;margin-top:20px}
.caveats ul{padding-left:1.5em}
.caveats li{margin:8px 0;font-size:.9rem}
.caveat-best-practices{margin-top:20px}
small{color:#666;font-size:.85em;font-weight:normal}
p{margin:8px 0}
</style>
</head>
<body>
<div class="container">

<h1>Backtest Report: {{ strategy_name }}</h1>
<p class="subtitle">
  {{ start_date }} → {{ end_date }}
  &nbsp;|&nbsp; ${{ starting_capital }} starting capital
  &nbsp;|&nbsp; {{ slippage_bps }} bps slippage
  &nbsp;|&nbsp; {{ cash_rate_pct }} cash rate
</p>

<section>
<h2>Run Details</h2>
<div class="info-grid">
  <div class="info-card"><div class="label">Cache version</div><div class="value">{{ cache_version }}</div></div>
  <div class="info-card"><div class="label">Run timestamp</div><div class="value">{{ run_timestamp }}</div></div>
  <div class="info-card"><div class="label">Code version</div><div class="value">{{ code_version }}</div></div>
  <div class="info-card"><div class="label">Trading days</div><div class="value">{{ n_days }}</div></div>
</div>
{% if params %}
<h3>Strategy Parameters</h3>
<table class="params-table">
{% for k, v in params.items() %}
<tr><th>{{ k }}</th><td>{{ v }}</td></tr>
{% endfor %}
</table>
{% endif %}
</section>

<section>
<h2>Equity Curve</h2>
{{ equity_chart }}
</section>

<section>
<h2>Drawdown</h2>
{{ drawdown_chart }}
</section>

<section>
<h2>Summary Statistics</h2>
{{ stats_table }}
</section>

<section>
<h2>Monthly Returns ({{ strategy_name }})</h2>
{{ monthly_heatmap }}
</section>

<section>
<h2>Trade Log <small>(first {{ shown_trades }} of {{ total_trades }} — see trade_log.csv for all)</small></h2>
{{ trade_log_table }}
</section>

<section>
<h2>Universe Diagnostics</h2>
{{ universe_diagnostics }}
</section>

<section class="caveats">
<h2>⚠ Data Integrity Caveats</h2>
{{ caveats }}
</section>

</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------


def _write_trade_log_csv(trades: list[TradeRecord], path: Path) -> None:
    rows = []
    for t in trades:
        rows.append({
            "entry_date": t.entry_date,
            "exit_date": t.exit_date,
            "symbol": t.symbol,
            "entry_price": float(t.entry_price),
            "exit_price": float(t.exit_price) if t.exit_price else None,
            "shares": float(t.shares),
            "pnl_dollars": float(t.pnl) if t.pnl is not None else None,
            "pnl_pct": round(t.pnl_pct * 100, 4) if t.pnl_pct is not None else None,
            "exit_reason": t.exit_reason,
        })
    pd.DataFrame(rows).to_csv(path, index=False)


def _write_equity_curve_csv(nav_dict: dict[str, pd.Series], path: Path) -> None:
    df = pd.DataFrame(nav_dict)
    df.index.name = "date"
    df.to_csv(path)


def _write_statistics_csv(stats_list: list[BacktestStats], path: Path) -> None:
    rows = []
    for s in stats_list:
        rows.append({
            "strategy": s.strategy_name,
            "total_return_pct": round(s.total_return * 100, 4),
            "annualized_return_pct": round(s.annualized_return * 100, 4),
            "annualized_volatility_pct": round(s.annualized_volatility * 100, 4),
            "sharpe": round(s.sharpe_ratio, 4) if not math.isnan(s.sharpe_ratio) else None,
            "sortino": round(s.sortino_ratio, 4) if not math.isnan(s.sortino_ratio) else None,
            "max_drawdown_pct": round(s.max_drawdown * 100, 4),
            "calmar": round(s.calmar_ratio, 4) if not (math.isnan(s.calmar_ratio) or math.isinf(s.calmar_ratio)) else None,
            "win_rate_pct": round(s.win_rate * 100, 2) if s.win_rate is not None else None,
            "profit_factor": round(s.profit_factor, 4) if (s.profit_factor is not None and not math.isinf(s.profit_factor)) else None,
            "avg_winner_pct": round(s.avg_winner_pct * 100, 4) if s.avg_winner_pct is not None else None,
            "avg_loser_pct": round(s.avg_loser_pct * 100, 4) if s.avg_loser_pct is not None else None,
            "num_trades": s.num_trades,
            "avg_hold_days": round(s.avg_hold_days, 1) if s.avg_hold_days is not None else None,
            "time_underwater_pct": round(s.time_underwater * 100, 2),
            "longest_losing_streak": s.longest_losing_streak,
        })
    pd.DataFrame(rows).to_csv(path, index=False)


def _get_code_version() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _write_metadata_json(
    strategy_result: SimulationResult,
    benchmark_results: list[SimulationResult],
    path: Path,
) -> None:
    cache_v = strategy_result.cache_version
    meta = {
        "run_timestamp": strategy_result.run_timestamp.isoformat(),
        "strategy_name": strategy_result.strategy_name,
        "strategy_params": strategy_result.params,
        "start_date": strategy_result.start_date.isoformat(),
        "end_date": strategy_result.end_date.isoformat(),
        "starting_capital": str(strategy_result.starting_capital),
        "slippage_bps": strategy_result.slippage_bps,
        "cash_annualized_rate": strategy_result.cash_annualized_rate,
        "cache_version": cache_v.isoformat() if cache_v else None,
        "benchmarks": [r.strategy_name for r in benchmark_results],
        "code_version": _get_code_version(),
    }
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate_report(
    strategy_result: SimulationResult,
    benchmark_results: list[SimulationResult] | None = None,
    output_dir: Path = Path("backtests/latest"),
    config: dict[str, Any] | None = None,
    risk_free_rate: float = 0.04,
) -> Path:
    """Generate a full backtest report directory.

    Writes report.html, trade_log.csv, equity_curve.csv, statistics.csv,
    metadata.json, and optionally config.yaml.

    Args:
        strategy_result: Primary strategy simulation result.
        benchmark_results: Optional benchmark results (e.g. BuyAndHoldSPY).
        output_dir: Directory to write report artifacts into (created if needed).
        config: If provided, written as config.yaml for reproducibility.
        risk_free_rate: Annualized rate used in Sharpe / Sortino (default 4%).

    Returns:
        Path to the generated report.html file.
    """
    import jinja2

    benchmarks = benchmark_results or []
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ---- Build NAV series for all runs ----
    all_results = [strategy_result] + benchmarks
    nav_dict: dict[str, pd.Series] = {r.strategy_name: r.nav_series() for r in all_results}

    # Cash floor — analytical compound growth
    snap_dates = [s.sim_date for s in strategy_result.snapshots]
    daily_rate = strategy_result.cash_annualized_rate / 252.0
    cash_floor = pd.Series(
        {d: float(strategy_result.starting_capital) * (1.0 + daily_rate) ** i
         for i, d in enumerate(snap_dates)},
        name=f"Cash ({strategy_result.cash_annualized_rate * 100:.0f}% floor)",
    )
    nav_dict[cash_floor.name] = cash_floor

    # ---- Stats ----
    stats_list = [compute_stats(r, risk_free_rate=risk_free_rate) for r in all_results]

    # ---- Charts ----
    equity_html  = _equity_chart(nav_dict, float(strategy_result.starting_capital))
    drawdown_html = _drawdown_chart(nav_dict)
    monthly_html  = _monthly_heatmap(strategy_result)

    # ---- Table HTML ----
    stats_html       = _stats_table_html(stats_list)
    trade_log_html   = _trade_log_table_html(strategy_result.trades[:50])
    universe_html    = _universe_diagnostics_html(strategy_result)

    # ---- CSV / JSON output ----
    _write_trade_log_csv(strategy_result.trades, output_dir / "trade_log.csv")
    _write_equity_curve_csv(nav_dict, output_dir / "equity_curve.csv")
    _write_statistics_csv(stats_list, output_dir / "statistics.csv")
    _write_metadata_json(strategy_result, benchmarks, output_dir / "metadata.json")

    if config is not None:
        import yaml
        with open(output_dir / "config.yaml", "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False)

    # ---- Render HTML report ----
    cache_v = strategy_result.cache_version
    code_v = _get_code_version()

    env = jinja2.Environment(autoescape=False)
    tmpl = env.from_string(_REPORT_TEMPLATE)
    html = tmpl.render(
        strategy_name=strategy_result.strategy_name,
        start_date=strategy_result.start_date,
        end_date=strategy_result.end_date,
        starting_capital=f"{float(strategy_result.starting_capital):,.2f}",
        slippage_bps=strategy_result.slippage_bps,
        cash_rate_pct=f"{strategy_result.cash_annualized_rate * 100:.1f}%",
        cache_version=cache_v.strftime("%Y-%m-%d %H:%M UTC") if cache_v else "—",
        run_timestamp=strategy_result.run_timestamp.strftime("%Y-%m-%d %H:%M UTC"),
        code_version=code_v,
        n_days=len(strategy_result.snapshots),
        params=strategy_result.params if strategy_result.params else None,
        equity_chart=equity_html,
        drawdown_chart=drawdown_html,
        monthly_heatmap=monthly_html,
        stats_table=stats_html,
        trade_log_table=trade_log_html,
        shown_trades=min(50, len(strategy_result.trades)),
        total_trades=len(strategy_result.trades),
        universe_diagnostics=universe_html,
        caveats=_CAVEATS_HTML,
    )

    report_path = output_dir / "report.html"
    report_path.write_text(html, encoding="utf-8")
    return report_path
