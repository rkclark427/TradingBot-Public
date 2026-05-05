# BacktestHarness Specification

**TradingBot Phase 2 — Infrastructure Design Document**
**Version 1.0 — May 2026**

---

## 1. Purpose and scope

This document specifies the BacktestHarness, a piece of platform infrastructure that simulates strategy execution over historical periods. It is intended as the authoritative reference for implementation by Claude Code.

The harness is a generic infrastructure component, not a strategy-specific tool. It accepts any class conforming to the Strategy ABC (Phase 1) and runs that strategy over a configured historical date range, simulating orders, fills, and NAV evolution. It produces standardized reports comparable across strategies.

Initial use case: validate the MomentumContinuation strategy (separate spec) on historical data before promoting it to a paper sleeve. Subsequent uses include validating future strategies as they are added to the platform.

### 1.1. In scope for v1

- Single-strategy backtest over a fixed date range with fixed starting capital.
- Simultaneous benchmark runs (BuyAndHoldSPY required; VOO_BND_60_40 optional) on identical date range and capital for comparison.
- Realistic fill simulation using bar-based open prices with explicit slippage.
- Cash earns a constant annualized rate during periods of partial deployment.
- Standardized HTML report with equity curve, drawdown curve, summary statistics, and trade log.
- CSV export of trade log and statistics for downstream analysis.
- Reproducibility via cached, versioned price data.

### 1.2. Out of scope for v1

- Parameter sweeps (grid search, random search, Bayesian optimization) — deferred to Phase 2.5.
- Walk-forward optimization — deferred to Phase 3.
- Monte Carlo bootstrapping or trade-shuffle analysis — deferred.
- Regime decomposition (returns conditional on VIX, market trend, etc.) — deferred.
- Live shadow mode (running a strategy on live data without trading, comparing to backtest predictions) — deferred.
- Intraday backtesting — daily bars only in v1.
- Multi-strategy portfolio backtests (running several strategies as a combined portfolio) — deferred.

---

## 2. Architectural overview

The harness is structured as four loosely-coupled layers:

### 2.1. Data layer

Fetches historical price data from yfinance, caches it locally, and serves it to the simulation engine. The data layer is the only component that knows about the data source. Strategies and the simulation engine receive data through a generic interface that could be swapped to a different source (Alpaca, Polygon, etc.) without strategy or engine changes.

### 2.2. Simulation engine

Walks forward through history one trading day at a time. On each simulated day, it: (a) presents the strategy with available market data through that date, (b) receives target weights from the strategy, (c) translates target weights into simulated orders for the next day, (d) on the following day, simulates fills using that day's open prices plus slippage, (e) updates simulated NAV and positions, (f) records all events for the trade log.

### 2.3. Reporting layer

Aggregates the simulation's recorded events into the report artifacts: equity curve, drawdown curve, statistics tables, trade log, and the consolidated HTML report. Reporting is fully separated from simulation — the same simulation output can produce multiple reports, and reports can be regenerated from saved simulation state without re-running the simulation.

### 2.4. Driver / CLI

Top-level entry point. Reads a backtest configuration (YAML), invokes the data layer to ensure data availability, runs the simulation engine for the strategy and benchmarks, and triggers the reporting layer. Exposed as a CLI command (e.g., `bot-ctl backtest <config.yaml>`).

---

## 3. Data layer

### 3.1. Data source: yfinance

Daily OHLC bars and dividend-adjusted close prices are sourced from yfinance. The library provides a sufficient combination of historical depth (back to the 1990s for most ETFs), dividend adjustment (essential for accurate ETF return calculations), and zero cost for paper-stage validation.

Acknowledged limitations of yfinance:

- **Unofficial** — Yahoo Finance is the source, scraped via a community library. Periodically breaks when Yahoo changes their site. Mitigated by caching.
- **Survivorship bias** — only includes currently-existing securities. For our 16 ETF universe this is not a concern (none have been delisted), but should be noted as a constraint for any future expansion to individual equities.
- **Historical accuracy** — generally reliable for liquid ETFs but occasionally has gaps or adjustments inconsistent with primary-source data.

### 3.2. Cache and versioning

Once price data is fetched, it MUST be cached locally on disk. The cache is the source of truth for backtest runs; live yfinance calls happen only on cache miss or explicit refresh.

Cache structure:

- **Storage:** SQLite database (separate from the live trading database; e.g., `backtest_data.db`) or a directory of Parquet files keyed by symbol.
- **Granularity:** one row per (symbol, date) tuple, storing open, high, low, close (raw), close (adjusted for dividends), volume, and the fetch timestamp.
- **Versioning:** each fetch records the date and time of the fetch. The cache is append-only by default; existing rows are not overwritten unless the user invokes an explicit refresh command.
- **Reproducibility marker:** each backtest run records the cache version (latest fetch timestamp seen across all symbols in the universe) in the report. Re-running with the same cache produces identical results.

### 3.3. Universe and symbol availability

The harness uses a static universe definition (the same 16-ETF universe defined in MomentumContinuation spec). The data layer handles symbols with limited history through availability masking:

- On any given simulation date D, the strategy receives close prices only for symbols where data exists for date D in the cache.
- XLRE has data only from October 2015 onward; XLC only from June 2018 onward. Backtests starting before these dates will see a smaller effective universe in the early periods.
- The simulation engine logs the effective universe size on each date for diagnostic purposes; the report includes a note if the universe size varied significantly during the test period.

### 3.4. Data layer interface

The data layer exposes (at minimum) the following methods:

```python
get_close_prices(symbols, start_date, end_date) -> DataFrame
get_ohlc_bars(symbols, start_date, end_date) -> DataFrame
get_available_symbols(date) -> List[str]
refresh_symbol(symbol, force=False) -> None
get_cache_version() -> datetime
```

Returned DataFrames use dividend-adjusted close prices by default. Raw OHLC is available for fill simulation (which uses next-day open prices).

---

## 4. Simulation engine

### 4.1. Time stepping

The simulation walks forward through trading days, one day per iteration. Trading days are derived from the data layer (specifically, the union of available trading dates across the universe). Weekends and US market holidays are excluded by virtue of having no data.

### 4.2. Per-day simulation cycle

On each simulated trading day D:

1. **Process pending orders from day D-1:** simulate fills using day D's open price, apply slippage, update positions, record fill events.
2. **Apply daily cash interest:** cash balance accrues at the configured annualized rate (default 4%), pro-rated to one day.
3. **Mark positions to market:** NAV is updated using day D's adjusted close price for each held position.
4. **Apply dividends:** if any held symbol paid a dividend on day D, credit the position's cash account with (shares held × dividend per share). yfinance's adjusted-close handling means this is implicitly captured in the price series, but explicit dividend modeling is preferred for trade-log accuracy.
5. **Invoke the strategy's cycle method:** pass it the data view (close prices through day D) and current positions. Receive target weights or order intents from the strategy.
6. **Translate target weights to orders for day D+1's open.** Orders are queued for execution at next open (consistent with the live platform's next-open execution behavior).
7. **Record end-of-day state:** NAV, positions, cash balance, pending orders. This becomes day D's row in the equity curve.

### 4.3. Fill simulation

Orders are queued at the close of day D for execution at the open of day D+1. On day D+1:

- Buy fill price = `open[D+1] * (1 + slippage_bps / 10000)`
- Sell fill price = `open[D+1] * (1 - slippage_bps / 10000)`

The default slippage is 5 basis points each way (configurable). All orders are treated as fully filled at this simulated price; partial fills are not modeled. This is appropriate for the universe (highly liquid ETFs) and the trade sizes contemplated in v1 ($125-$1,250 notional per position).

### 4.4. Cash and capital

- **Starting capital:** configurable, default $10,000 (chosen to be a realistic notional for evaluation; the live sleeve may run smaller).
- **Cash interest:** accrued daily at `(annualized_rate / 252)` per trading day. Default annualized rate: 4%.
- **No leverage:** total notional value of positions cannot exceed cash + position value. The strategy's `max_total_deployment_pct` parameter (typically 100%) caps this in practice.
- **No margin or short positions:** the harness rejects any negative target weight as an error.

### 4.5. Strategy interface contract

The harness invokes the strategy through the Phase 1 Strategy ABC. The strategy receives:

- A market data view object — supports queries for close prices and OHLC bars up through the current simulation date. The view enforces no-lookahead by raising an error if the strategy attempts to query data beyond the current date.
- Current positions for the strategy's sleeve.
- Current NAV.
- Current trading date.

The strategy returns target weights (or equivalent order intents). The simulation engine handles all execution logic; **the strategy itself does not know it is being backtested rather than run live.** This is essential for backtest validity — strategies that behave differently in backtest vs. live mode have a category of bugs that no testing can catch.

### 4.6. Lookback warmup

Most strategies require some history before they can generate signals (MomentumContinuation requires 21 days). The harness handles this transparently:

- The strategy receives the configured backtest start date as the first simulation date.
- On early dates, the strategy receives all available history from the cache, which extends before the backtest start date.
- If the strategy has insufficient history (e.g., XLC's data starts after the backtest start date), the strategy's own logic handles the missing-data case (skipping the symbol).
- NAV remains at starting capital plus accumulated cash interest until the strategy makes its first trade. This may be several days into the backtest period.

---

## 5. Benchmarks

Every backtest run includes one or more benchmark sleeves running on the same dates with identical starting capital. Benchmarks are themselves implemented as Strategy classes (not simulation shortcuts), ensuring their NAV evolution is calculated identically to the target strategy's.

### 5.1. BuyAndHoldSPY (required)

- On the first simulation date, deploy 100% of starting capital into SPY at that day's open (plus slippage).
- Hold for the full backtest period.
- Reinvest dividends: dividend payments on SPY are credited to cash, then deployed back into SPY at the next available open.
- This is implemented via the existing BuyAndHoldSPY strategy from Phase 1; no new code required.

### 5.2. VOO_BND_60_40 (optional)

- On the first simulation date, deploy 60% of starting capital into VOO and 40% into BND.
- Rebalance to exact 60/40 at the close of the first trading day of each calendar month (executes at next-day open).
- Reinvest dividends as cash, redeployed at the next rebalance.
- Implemented via the planned VOO_BND_60_40 strategy (separate Phase 2 deliverable). Backtest runs that include this benchmark require the strategy to exist.

### 5.3. Cash benchmark (always reported)

In addition to the above, the report includes a 'cash only' line showing what the starting capital would have grown to under the cash interest assumption (4% annualized, default). This serves as the floor — any real strategy must beat cash to justify its existence.

---

## 6. Reporting

### 6.1. Output structure

Each backtest run produces a directory containing:

- `report.html` — the consolidated human-readable report (charts + tables, single self-contained file)
- `trade_log.csv` — every entry and exit, full detail
- `equity_curve.csv` — NAV by date for the strategy and each benchmark
- `statistics.csv` — summary statistics for each sleeve
- `config.yaml` — copy of the configuration used for this run (for reproducibility)
- `metadata.json` — run metadata: timestamp, cache version, code version, parameter values

### 6.2. HTML report contents

The HTML report is generated from a template and includes:

#### 6.2.1. Header

- Strategy name and parameters
- Backtest period (start and end dates)
- Starting capital
- Cache version timestamp
- Run timestamp
- Code version (git SHA, if available)

#### 6.2.2. Equity curve chart

Line chart showing NAV vs. date for the target strategy, all benchmarks, and the cash floor. Logarithmic y-axis option for long periods. Annotations marking notable events (drawdown circuit breaker triggers, if any).

#### 6.2.3. Drawdown chart

Line chart showing drawdown from running peak vs. date, for the strategy and each benchmark. Useful for identifying the worst periods, which are usually more informative than the equity curve alone.

#### 6.2.4. Summary statistics table

Per sleeve (target strategy + each benchmark), the following metrics:

| Metric                  | Description                                                              |
|-------------------------|--------------------------------------------------------------------------|
| Total return            | (Final NAV / Starting capital) - 1                                       |
| Annualized return       | Geometric mean of daily returns × 252                                    |
| Annualized volatility   | Standard deviation of daily returns × sqrt(252)                          |
| Sharpe ratio            | (Annualized return - 4%) / Annualized volatility                         |
| Sortino ratio           | Like Sharpe but uses downside deviation only                             |
| Maximum drawdown        | Largest peak-to-trough NAV decline, expressed as %                       |
| Calmar ratio            | Annualized return / \|Maximum drawdown\|                                 |
| Win rate                | % of trades closed at profit (strategy only; not applicable to buy-and-hold) |
| Profit factor           | Gross profit / Gross loss                                                |
| Average winner / loser  | Mean P&L of winning vs. losing trades                                    |
| Number of trades        | Total entries (each entry/exit pair = one trade)                         |
| Average trade duration  | Mean days held                                                           |
| Time underwater         | % of days NAV is below its prior peak                                    |
| Longest losing streak   | Consecutive losing trades                                                |

#### 6.2.5. Monthly returns heatmap

Grid showing monthly returns by year (rows) and month (columns), color-coded green/red. Reveals seasonality, consistency, and clusters of bad months.

#### 6.2.6. Trade log table

First 50 trades displayed inline; full log available in `trade_log.csv`. Columns: entry date, exit date, symbol, entry price, exit price, shares, P&L (dollars and percent), exit reason (hard stop, trailing stop, time stop).

#### 6.2.7. Universe diagnostics

Notes on universe behavior during the backtest:

- Symbols included and their effective date ranges
- Days where the effective universe was reduced due to missing data
- Any data gaps or anomalies detected during the run

#### 6.2.8. Data integrity caveats (mandatory)

Every report includes a caveats section explicitly listing the limitations of the backtest. This is required, not optional, and must appear prominently. See section 8 of this spec for the canonical text.

### 6.3. Charting library

Plotly is the recommended charting library: produces interactive HTML charts inline, no external dependencies for viewing, hover tooltips, zoom/pan support. Matplotlib is acceptable as a fallback (static images embedded in HTML), but Plotly's interactivity makes report exploration substantially easier.

---

## 7. Configuration and CLI

### 7.1. Configuration schema

Backtest runs are defined by a YAML configuration file, validated by Pydantic. Example:

```yaml
# backtest_config.yaml
strategy:
  class: MomentumContinuation
  parameters:
    lookback_days: 20
    hard_stop_pct: 0.04
    trailing_stop_pct: 0.05
    time_stop_days: 10
    risk_per_trade_pct: 0.01
    max_position_pct: 0.25
    max_concurrent_positions: 5
    max_total_deployment_pct: 1.00
    drawdown_circuit_breaker_pct: 0.15

benchmarks:
  - BuyAndHoldSPY
  - VOO_BND_60_40    # optional

simulation:
  start_date: 2018-01-01
  end_date: null    # null = latest available
  starting_capital: 10000
  cash_annualized_rate: 0.04
  slippage_bps: 5

output:
  directory: ./backtests/momentum_2018_baseline/
  format: html
```

### 7.2. CLI

The harness exposes a CLI subcommand on the existing `bot-ctl` tool:

```
bot-ctl backtest run <config.yaml>
bot-ctl backtest refresh-data [--symbol SYMBOL] [--all]
bot-ctl backtest list           # show recent backtest runs
bot-ctl backtest cache-status   # show cache freshness
```

### 7.3. Default configuration for first run

A default configuration file `backtest_configs/momentum_2018_baseline.yaml` is provided. Running `bot-ctl backtest run backtest_configs/momentum_2018_baseline.yaml` produces a baseline result the user can review without writing a config from scratch.

Default configuration values:

- Strategy: MomentumContinuation with default parameters per the strategy spec
- Benchmarks: BuyAndHoldSPY (required), VOO_BND_60_40 (if available)
- Period: 2018-01-01 to latest available data
- Starting capital: $10,000
- Cash rate: 4% annualized
- Slippage: 5 bps each way

---

## 8. Data integrity caveats

Every backtest report includes the following caveats prominently. This text is canonical and SHOULD NOT be modified at runtime — it represents the structural limitations of any backtest, not a per-run disclosure.

### 8.1. What this backtest cannot tell you

- **Past performance does not predict future returns.** A strategy that worked in 2018-2025 may not work in 2026-2030. Markets change, factor premia decay, and strategies that have entered the public domain are arbitraged.

- **Live execution will be worse than backtest execution.** Slippage, partial fills, exchange outages, broker errors, and timing discrepancies all degrade live performance vs. the simulated execution. Plan for live results to be 1-3% annualized worse than backtest results, even if everything works correctly.

- **Single-period results are noisy.** An 8-year backtest is not enough data to confidently estimate the strategy's long-run characteristics. The Sharpe ratio computed from this backtest has a standard error roughly equal to the Sharpe itself — a Sharpe of 0.8 might really be 0.0 to 1.6.

- **Multiple-testing risk.** If the strategy or its parameters were chosen with knowledge of historical performance, the backtest is contaminated by selection bias. The first time a strategy is run on historical data, results are informative. After that, every parameter tweak that produces "better" results on the same data is at risk of fitting noise.

- **yfinance data has known imperfections.** Occasional gaps, late dividend adjustments, and rare price errors are present. These are unlikely to materially change conclusions but are not zero.

- **Survivorship bias is minimal but not zero.** Our 16-ETF universe is currently complete (none have been delisted), but the universe was chosen with knowledge that these ETFs have all survived. A truly survivorship-bias-free backtest would also include ETFs that were active at the start of the period but have since been delisted; this universe does not.

- **Slippage and cash interest are simulated, not measured.** The 5 bps slippage and 4% cash rate are reasonable defaults for the current environment. Real values vary over time and can change conclusions if the strategy is sensitive to either.

- **This is not investment advice.** Backtests are a tool for understanding strategy behavior, not a basis for sizing real-money positions. Strategy decisions are the user's responsibility.

### 8.2. Best practices to mitigate these limitations

- Run the backtest exactly once on out-of-sample data (e.g., 2018-2025) before any parameter tuning. That result is the closest thing to ground truth the harness can provide.
- If parameter tuning is performed, do it on a subset of the data (e.g., 2018-2022), then re-validate on a held-out period (e.g., 2023-2025) without further tuning.
- Treat backtest results as a 'might work' signal, not a 'will work' guarantee. Promotion to paper trading is the next validation step; promotion to live is the step after that.
- Compare to multiple benchmarks, not just buy-and-hold. A strategy that beats SPY but loses to 60/40 may be taking on uncompensated risk.
- Pay attention to drawdown and time underwater, not just total return. A strategy with high return and brutal drawdowns may be psychologically untradable in real life.

---

## 9. Acceptance criteria

Implementation is considered complete when ALL of the following are demonstrated:

### 9.1. Unit tests

- Data layer: cache hit/miss, refresh logic, symbol availability filtering.
- Simulation engine: time stepping produces correct dates, no lookahead in market data view, fill simulation matches expected open + slippage.
- Cash interest accrual: daily compounding produces correct annualized return on a static cash balance.
- NAV calculation: positions marked to market correctly with dividend handling.
- Statistics: each metric (Sharpe, Sortino, drawdown, etc.) computed correctly on synthetic equity curves with known properties.

### 9.2. Integration tests

- End-to-end test: run a trivial test strategy (always 100% in SPY) and verify the resulting equity curve matches BuyAndHoldSPY benchmark to within slippage tolerance.
- Run BuyAndHoldSPY as both the strategy and the benchmark; equity curves should be identical.
- Run a no-op strategy (always 0% deployed); resulting equity curve should match the cash floor exactly.
- MomentumContinuation strategy runs to completion on the default 2018+ config without errors.

### 9.3. Output validation

- HTML report renders correctly in a modern browser; charts are interactive.
- CSV files open correctly in Excel and pandas, with proper headers and date formatting.
- Two backtest runs with the same config and same cache version produce byte-identical `statistics.csv` files.
- Caveats section appears prominently in every report.

### 9.4. Documentation

- README in the backtest module explaining how to run a backtest from scratch.
- Default configuration file (`momentum_2018_baseline.yaml`) committed with sensible defaults.
- Inline docstrings on all public methods of the data layer and simulation engine.

---

## 10. Implementation notes for Claude Code

- The harness is a separate Python module within the existing repo, e.g., `src/backtest/`. It SHOULD NOT be coupled to the live trading code beyond shared use of the Strategy ABC.
- The strategy under test must run unmodified — the same strategy class powers both the live sleeve and the backtest. Any divergence between live and backtest behavior is a bug to be fixed, not a feature.
- Use pandas for tabular data manipulation. Use Plotly for HTML charts. Use Jinja2 for HTML report templating.
- yfinance: pin to a specific version; the API is unstable. Document the version in `pyproject.toml` and the `metadata.json` output.
- Cache database: separate from the live `tradingbot.db`. Use a clearly-named file like `backtest_data.db` with its own Alembic migrations or a simple schema-on-load approach.
- All monetary calculations: `Decimal`. Ratios and percentages: `float`. Consistent with Phase 1 conventions.
- Reporting layer: stateless. Given a saved simulation output (a directory of CSV files plus metadata), it can regenerate the HTML report. This separation enables iterating on the report format without re-running the simulation.
- CLI integration: extend the existing `bot-ctl` tool with the backtest subcommand; do not create a separate CLI binary.

---

## 11. Open questions and deferred decisions

- **Borrow rate and short-side execution.** v1 is long-only; if and when short strategies are added, the harness needs a short-borrow rate model and short-side fill simulation. Defer until shorts are in scope.

- **Tax-aware backtesting.** Real returns are after-tax. v1 ignores taxes, which is appropriate for paper-stage validation but understates the friction for taxable accounts. Defer until live taxable trading is contemplated.

- **Transaction costs beyond slippage.** Alpaca is commission-free, but other brokers charge commissions and SEC fees. v1 ignores commissions. Defer unless the platform is extended to non-Alpaca brokers.

- **Intraday backtesting.** If future strategies require intraday signals (e.g., opening range breakouts that we discussed but did not include), the harness must be extended to handle minute bars. Defer until needed.

- **Walk-forward optimization framework.** A natural Phase 2.5 addition: a wrapper that splits the backtest period into rolling train/test windows, optimizes parameters on each train window, and validates on the test window. Useful for parameter selection without overfitting. Significant additional code; defer.

- **Multi-strategy portfolio backtests.** Running several strategies as a combined portfolio with capital allocation rules. Useful for portfolio-of-strategies questions. Defer.

- **Live shadow mode.** Running a strategy on live market data without trading, comparing decisions to backtest expectations. Useful for catching live/backtest divergence. Defer to a later platform-level capability.

---

**END OF SPECIFICATION**
