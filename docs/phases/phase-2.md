# Phase 2: Backtest Harness + MomentumContinuation Strategy

> Goal: a complete backtest harness — historical data layer, simulation engine, reporting, and CLI — plus the MomentumContinuation strategy and its live-trading position state. Run the baseline backtest and review results before any parameter tuning.

## Phase gate

The phase is complete when **all** of the following are true:

1. `bot-ctl backtest refresh-data --all` populates the price cache from yfinance for all 16 strategy symbols without errors
2. `bot-ctl backtest run backtest_configs/momentum_2018_baseline.yaml` produces a `report.html` with equity curve, drawdown chart, monthly heatmap, and statistics table
3. The MomentumContinuation strategy runs in the live orchestrator with correct stop-loss behavior: hard stop, trailing stop, and time stop all wire up to persisted `strategy_state` (days_held increments once per session; highest_close updates with current price)
4. Backtests are reproducible: running the same config twice against the same cache produces identical results
5. All 270 tests pass

The phase is **not** complete without the baseline backtest having been run and reviewed. Do not tune parameters before that first run.

## Tasks

### 1. Backtest data layer ✅
- [x] `src/backtest/cache_models.py` — PriceBarRow SQLAlchemy model
- [x] `src/backtest/data.py` — BacktestDataLayer (yfinance fetch + SQLite cache)
- [x] `src/backtest/data.py` — BacktestMarketDataView (no-lookahead enforcement)
- [x] 17 tests in `tests/test_backtest_data.py`

### 2. Strategy ABC update ✅
- [x] `position_state: dict[str, dict[str, Any]] | None = None` added to `Strategy.generate_targets` signature
- [x] `BuyAndHoldSPY.generate_targets` updated to accept (and ignore) position_state

### 3. MomentumContinuation strategy ✅
- [x] `src/strategies/momentum_continuation.py`
- [x] Entry signal: 20-day closing-price breakout; exit: hard/trailing/time stops
- [x] Position sizing: `risk_per_trade_pct / hard_stop_pct = 0.25` NAV per position
- [x] 25 tests in `tests/test_momentum.py`

### 4. SleeveStatus.HALTED + circuit breaker ✅
- [x] `SleeveStatus.HALTED` added to types
- [x] `halt_sleeve()` / `resume_sleeve()` in SleeveManager
- [x] Orchestrator auto-transitions RUNNING→HALTED when drawdown ≥ threshold
- [x] HALTED sleeves: included in loop, buy orders filtered, sell orders pass through

### 5. Simulation engine ✅
- [x] `src/backtest/engine.py` — `run_simulation()` day-by-day loop
- [x] Fill at next-day open + slippage; cash interest accrual; MTM NAV
- [x] `days_held=0` on entry day; increments each subsequent session
- [x] TradeRecord with entry/exit price, exit_reason, pnl
- [x] End-of-simulation liquidation at last close
- [x] 21 tests in `tests/test_engine.py`

### 6. Reporting layer ✅
- [x] `src/backtest/reporting.py` — `compute_stats()` + `generate_report()`
- [x] 14 metrics: CAGR, Sharpe, Sortino, max_dd, Calmar, win rate, profit_factor, etc.
- [x] Plotly equity curve, drawdown chart, monthly heatmap — self-contained HTML
- [x] Artifacts: report.html, trade_log.csv, equity_curve.csv, statistics.csv, metadata.json, config.yaml
- [x] Canonical caveats text per spec §8 always included
- [x] 28 tests in `tests/test_reporting.py`

### 7. Backtest CLI + config model ✅
- [x] `src/backtest/config.py` — BacktestConfig (Pydantic v2), StrategyConfig with `class` alias, instantiate_strategy()
- [x] `src/cli/bot_ctl.py` — `backtest` sub-app: run, refresh-data, list, cache-status
- [x] `backtest_configs/momentum_2018_baseline.yaml` — baseline config

### 8. strategy_state DB table + orchestrator wiring ✅
- [x] `StrategyStateRow` in `src/tracking/models.py`
- [x] `migrations/versions/0003_add_strategy_state.py`
- [x] `src/tracking/repos/strategy_state.py` — StrategyStateRepo
- [x] Orchestrator `_run_sleeve_cycle`: loads, updates, and passes position_state each cycle
- [x] `SleeveManager.attribute_fill`: creates entry on new buy; deletes on full sell
- [x] 12 tests in `tests/test_strategy_state.py`

## Phase gate checklist (to complete before Phase 3)

- [x] Run `bot-ctl backtest refresh-data --all` on the VM; verify all 16 symbols cached — 16/16, 62196 rows (2026-05-08)
- [x] Run `bot-ctl backtest run backtest_configs/momentum_2018_baseline.yaml` — 2098 days, 539 trades, NAV $16,966.88 vs SPY $30,893.10 (2026-05-08)
- [x] Review `report.html`: equity curve shape, max drawdown, Sharpe, win rate — Sharpe 0.25, max DD -13.3%, win rate 57.7% (2026-05-08)
- [x] Confirm report is reproducible (second run produces identical statistics.csv) — confirmed identical (2026-05-08)
- [x] Record baseline metrics in decisions.md before any tuning — recorded (2026-05-08)

## Spec documents

- `docs/BacktestHarness_Specification_Phase2_v1.0.md`
- `docs/MomentumContinuation_Strategy_Spec_Phase2_v1.0.md`
