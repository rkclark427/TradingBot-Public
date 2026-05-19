# TradingBot — Project State Document
*Last updated: 2026-05-17 | 301 tests passing | Phase 2 complete + STMR added*

---

## What this system does

A multi-strategy algorithmic trading platform that executes equity trades through Alpaca. The central abstraction is the **sleeve**: a runtime instance of a strategy with its own capital allocation, NAV, mode (paper/live), parameters, and lifecycle state. Multiple sleeves can run concurrently. The platform handles order sizing, netting, fill attribution, risk controls, and NAV accounting across all of them.

---

## Repository layout

```
trading-bot/
├── src/
│   ├── orchestrator.py          # main loop entry point
│   ├── logging_config.py        # structured JSON logging
│   ├── config/                  # config loading + Pydantic models
│   ├── data/                    # Alpaca client, asset universe, capabilities
│   ├── strategies/              # strategy library
│   ├── sleeves/                 # sleeve lifecycle, NAV, capital
│   ├── portfolio/               # weight → share quantity translation
│   ├── execution/               # order routing, netting, fill polling
│   ├── risk/                    # pre-trade checks
│   ├── tracking/                # SQLite database: models + repositories
│   ├── backtest/                # historical simulation engine + data layer
│   ├── queries/                 # read-only query functions (CLI wiring)
│   ├── commands/                # write/action functions (CLI wiring)
│   └── cli/                     # bot-ctl Typer app
├── backtest_configs/            # backtest YAML configs
├── config/
│   ├── base.yaml                # execution, risk, orchestrator settings
│   ├── sleeves.yaml             # managed sleeve registry
│   └── strategies/              # per-strategy default parameters
├── tests/                       # 270 tests, all passing
├── migrations/                  # Alembic schema migrations
└── docs/                        # architecture, decisions, specs
```

---

## Phase 1 — Foundation (complete, smoke-tested)

### 1. Configuration (`src/config/`)

**`config/models.py`** — Pydantic models for every config layer:
- `ExecutionConfig`: `limit_offset_bps`, `min_order_notional`, `fill_poll_interval_seconds`
- `RiskConfig`: `max_daily_loss_pct`, `drawdown_halt_pct`, `max_orders_per_day`, `max_order_nav_pct`
- `OrchestratorConfig`: `cycle_interval_seconds`, `market_open_buffer_minutes`, `drain_timeout_seconds`
- `SleeveRiskConfig`: per-sleeve overrides of the above
- `Secrets`: API keys loaded from environment (`.env` in dev, systemd in prod)
- `BaseConfig`: root config object composing all of the above

**`config/loader.py`** — loads `base.yaml`, `sleeves.yaml`, and per-strategy YAML files; injects secrets from environment.

### 2. Logging (`src/logging_config.py`)

- Structured JSON via structlog. One event per line.
- Required fields: `timestamp`, `level`, `event`, `module`.
- `sleeve_log_context()` context manager binds `sleeve_id` to all log events within a block.
- File rotation to `./logs/` (dev) or `/var/log/trading-bot/` (prod), kept 90 days.

### 3. Database (`src/tracking/`)

**Schema** — SQLAlchemy ORM models in `models.py`. All datetimes are UTC-aware via a custom `TZDateTime` type. All money columns use `Numeric(20, 10)`. Tables:

| Table | Purpose |
|-------|---------|
| `sleeves` | Sleeve registry: strategy, mode, status, NAV, cash, HWM, parameters |
| `sleeve_capital_events` | Deposits/withdrawals to sleeve capital |
| `signals` | Strategy signal audit log |
| `intended_orders` | Per-sleeve orders before netting |
| `net_orders` | Netted orders submitted to Alpaca |
| `intended_to_net` | Netting mapping table (attribution recovery) |
| `fills` | Alpaca fill records |
| `sleeve_fills` | Fill attribution back to individual sleeves |
| `positions` | Current sleeve positions (upserted on fill) |
| `strategy_state` | Per-sleeve per-symbol stop-loss state: entry_price, days_held, highest_close_since_entry |
| `sleeve_nav_snapshots` | Daily NAV snapshots per sleeve |
| `account_snapshots` | Daily account-level snapshots |
| `risk_events` | Risk check failures and circuit breaker fires |
| `system_events` | Bot lifecycle events (start, stop, errors) |
| `asset_universe` | Cached tradable asset list from Alpaca |
| `account_capabilities` | Cached account capabilities snapshot |
| `heartbeat` | Single-row liveness signal |

**Migrations** — Alembic manages all schema changes:
- `0001_initial_schema.py` — creates all original tables
- `0002_add_sleeve_cash.py` — adds `current_cash` column to sleeves
- `0003_add_strategy_state.py` — creates `strategy_state` table

**Repositories** (`src/tracking/repos/`) — one repo class per domain area:
- `SleeveRepo` — CRUD + status/NAV/cash updates
- `PositionRepo` — upsert positions, list by sleeve
- `StrategyStateRepo` — upsert/delete per-symbol stop-loss state; get_by_sleeve for bulk load
- `IntendedOrderRepo`, `NetOrderRepo`, `IntendedToNetRepo` — order lifecycle
- `FillRepo`, `SleeveFillRepo` — fill recording and attribution
- `RiskEventRepo`, `SystemEventRepo`, `SignalRepo`, `CapitalEventRepo` — event auditing
- `AssetUniverseRepo`, `AccountCapabilitiesRepo` — market data cache
- `HeartbeatRepo` — liveness
- `SleeveNavSnapshotRepo` — NAV history

### 4. Alpaca client (`src/data/alpaca_client.py`)

Thin wrapper around alpaca-py. Returns plain dataclasses (not SDK types), uses `Decimal` for all money, timezone-aware datetimes throughout.

Key methods:
- `get_account()` → `AccountInfo`
- `get_positions()` → `list[PositionInfo]`
- `submit_order(symbol, qty, side, limit_price, client_order_id)` → `OrderInfo`
- `get_order_by_client_order_id(id)` → `OrderInfo` (used for retry idempotency)
- `get_latest_trade_price(symbol)` → `Decimal` (current market price via `StockLatestTradeRequest`)
- `get_bars(symbol, timeframe, start, end)` → `pd.DataFrame` with OHLCV columns
- `cancel_order(order_id)`, `cancel_all_orders()`
- `get_clock()` → `ClockInfo` (is_open, next_open, next_close)

Two client classes: `PaperClient` (paper account) and `LiveClient` (live account). Data feeds are shared.

**Retry logic**: exponential backoff (1s/2s/4s) on `httpx.ConnectError`, `ReadTimeout`, `RemoteProtocolError`, HTTP 429, HTTP 503. All other errors propagate immediately.

### 5. Asset universe and capabilities (`src/data/`)

**`assets.py`** — `refresh_asset_universe(client, session)`: pulls all active US equity assets from Alpaca, persists tradability flags (fractionable, shortable, ETB) to `asset_universe` table.

**`capabilities.py`** — `discover_account_capabilities(client)`: snapshots what the Alpaca account can do (fractional shares, shorting, options level, buying power). Persisted to `account_capabilities` table and loaded at startup for capability validation.

### 6. Strategy interface (`src/strategies/base.py`)

```python
class Strategy(ABC):
    name: str
    capabilities: StrategyCapabilities  # static declaration

    @abstractmethod
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
    ) -> list[Target]: ...
```

- **Strategies return the full desired portfolio** as `Target` objects (weight fractions of NAV), not deltas.
- `position_state` is per-symbol state the orchestrator computes from the `strategy_state` DB table each cycle. Strategies treat it as read-only. Keys per symbol: `entry_price`, `days_held`, `highest_close_since_entry`, `entry_date`.
- `MarketDataView` protocol: `get_bars(symbol, lookback_days, as_of)` and `get_latest_price(symbol)`. Both the live orchestrator and the backtest harness implement this protocol — same strategy code runs both.

**`StrategyCapabilities`** — static declaration on each strategy class of what the account must support (asset classes, fractional, shorting, options). Validated against account capabilities at sleeve creation. Sleeve creation fails if requirements aren't met.

### 7. BuyAndHoldSPY strategy (`src/strategies/buy_and_hold.py`)

Returns `Target(symbol="SPY", target_weight=1.0)` every cycle. If already fully invested, the sizer computes a zero delta and no order is generated.

### 8. Sleeve types and lifecycle (`src/sleeves/`)

**`types.py`** — core value objects:

| Type | Description |
|------|-------------|
| `Mode` | `PAPER` or `LIVE` — determines which Alpaca account |
| `SleeveStatus` | `RUNNING`, `PAUSED`, `HALTED`, `STOPPING`, `STOPPED` |
| `Sleeve` | Runtime snapshot: id, strategy_name, mode, status, starting_capital, current_nav, current_cash, high_water_mark, parameters, risk config |
| `AccountCapabilities` | What the Alpaca account can do |
| `SleeveRiskConfig` | Per-sleeve risk parameters with defaults |

Status semantics:
- `RUNNING`: normal operation, can enter and exit
- `PAUSED`: operator-triggered, no new entries, exits continue
- `HALTED`: circuit-breaker-triggered, no new entries, exits continue, requires manual `resume`
- `STOPPING`: liquidating all positions
- `STOPPED`: permanently inactive, capital returned to pool

**`manager.py`** — `SleeveManager`:
- `create_sleeve(strategy_name, mode, starting_capital, params)` — validates capabilities, creates DB row, records capital event
- `pause_sleeve`, `resume_sleeve`, `halt_sleeve`, `stop_sleeve` — lifecycle transitions with allowed-from validation
- `attribute_fill(fill)` — NAV accounting on fill:
  - **Buy**: cash → position at cost, NAV unchanged minus fees; creates `strategy_state` entry on first buy of a symbol (entry_price = fill_price, days_held = 0)
  - **Sell**: NAV += realized P&L minus fees; deletes `strategy_state` entry on full close
- `list_sleeves(status_filter)` — accepts single status string, list of statuses, or None

### 9. Portfolio sizer (`src/portfolio/sizer.py`)

`targets_to_intended_orders(sleeve, targets, current_prices, current_positions)`:
1. For each target: `desired_notional = weight × nav` → `desired_qty = desired_notional / price`
2. Delta = desired_qty − current_qty
3. Round down (fractional: 6 decimal places; whole shares: integer)
4. Skip if `|delta| × price < min_notional`
5. Return `IntendedOrder(sleeve_id, symbol, side, qty, limit_price)`

### 10. Execution router (`src/execution/router.py`)

**`submit_intended_orders(orders, client, session, manager, as_of)`**:
1. Groups orders by symbol, nets buys vs. sells into a single `NetOrder` per symbol
2. Persists `IntendedOrderRow`, `NetOrderRow`, and `IntendedToNetRow` mapping
3. Submits to Alpaca with `client_order_id = {YYYYMMDD}-{symbol}-{net_row.id}`
4. On HTTP error: re-queries by `client_order_id` before failing (idempotency for retries)

**`poll_fills(client, session, manager)`**:
1. Queries all `net_orders` with status `"submitted"`
2. Fetches each from Alpaca by `client_order_id`
3. On fill: calls `manager.attribute_fill()` to update sleeve NAV/cash/positions and strategy_state
4. On `"canceled"` or `"expired"`: marks terminal without fill attribution
5. Returns count of fills processed

### 11. Risk checks (`src/risk/checks.py`)

`check_and_filter(orders, sleeve, asset_universe, kill_switch, max_nav_pct)`:
- Kill switch active → reject all
- Symbol not in asset_universe or not tradable → reject
- Order notional > `max_nav_pct × sleeve.current_nav` → reject
- Returns `(passing, rejected_with_reasons)`

### 12. Orchestrator (`src/orchestrator.py`)

**`AlpacaMarketDataView`** — adapts `PaperClient`/`LiveClient` to the `MarketDataView` protocol:
- `get_bars`: fetches daily OHLCV from Alpaca
- `get_latest_price`: uses `StockLatestTradeRequest` for current market price

**Main loop** (`run()` → `run_once()`):
1. Kill switch check (force/graceful/cancel_open modes)
2. Market hours check via Alpaca clock
3. For each RUNNING and HALTED sleeve: `_run_sleeve_cycle()`
4. `poll_fills()` for all configured environments
5. Write heartbeat

**`_run_sleeve_cycle(sleeve, as_of, asset_universe)`**:
1. Circuit breaker check: if RUNNING and `drawdown_pct ≥ drawdown_circuit_breaker_pct` → `halt_sleeve()` and continue as HALTED
2. Look up strategy from registry (`buy_and_hold`, `momentum_continuation`, `short_term_mean_reversion`)
3. Load current positions from DB
4. Load `strategy_state` from DB; update `days_held` (once per trading session via `last_session_date` gate) and `highest_close_since_entry` (current market price); save; build `position_state` dict; prefetch prices for held symbols
5. Call `strategy.generate_targets(position_state=position_state)`
6. Fetch current prices for any remaining symbols (re-uses prefetched prices for held positions)
7. `targets_to_intended_orders()`
8. Apply limit price offset (close ± `limit_offset_bps`)
9. `check_and_filter()`
10. If HALTED: drop all buy orders; only pass sell orders for currently-held positions
11. `submit_intended_orders()`

**Kill switch drain** (`_drain_fills(cancel_open=False)`): polls fills every 30 seconds until all `"submitted"` net orders are terminal, with a configurable timeout.

### 13. CLI (`src/cli/bot_ctl.py`)

`bot-ctl` Typer application. Every command is a thin wrapper — business logic lives in `src/queries/` (reads) or `src/commands/` (writes).

| Command | Action |
|---------|--------|
| `bot-ctl status` | Show kill switch, heartbeat, sleeve summary, account info |
| `bot-ctl sleeves list` | List all sleeves with status and NAV |
| `bot-ctl sleeves show <id>` | Full sleeve detail |
| `bot-ctl sleeves create` | Create a new sleeve (validates capabilities) |
| `bot-ctl sleeves stop <id>` | Stop a sleeve |
| `bot-ctl kill` | Activate kill switch (graceful mode) |
| `bot-ctl kill --cancel-open` | Activate kill switch + cancel open orders |
| `bot-ctl resume` | Deactivate kill switch |
| `bot-ctl run` | Start the main orchestrator loop |
| `bot-ctl refresh-universe` | Pull asset list from Alpaca |
| `bot-ctl refresh-capabilities` | Snapshot account capabilities |

---

## Phase 2 — Backtest + MomentumContinuation + ShortTermMeanReversion (complete)

### 1. Backtest data layer (`src/backtest/`)

**`cache_models.py`** — `PriceBarRow`: SQLAlchemy model for a local SQLite cache of historical price data. Columns: `symbol`, `trade_date`, `open`, `high`, `low`, `close` (all split+dividend adjusted via yfinance `auto_adjust=True`), `volume`, `fetched_at`. Unique constraint on `(symbol, trade_date)`. Uses `create_all` (not Alembic) since it's purely derived and can be regenerated at any time.

**`data.py`** — two classes:

`BacktestDataLayer(cache_path: Path)`:
- `refresh_symbol(symbol, start_date, force)` → int rows written. Fetches from yfinance; skips existing dates unless `force=True`.
- `refresh_all(symbols, start_date, force)` → dict[str, int]
- `get_close_prices(symbols, start_date, end_date)` → DataFrame (date index, symbol columns, adjusted close values)
- `get_ohlc_bars(symbols, start_date, end_date)` → dict[str, DataFrame]
- `get_available_symbols(as_of)` → list of symbols with data on or before that date
- `get_cache_version()` → most recent `fetched_at` timestamp, or None if empty
- `get_latest_trade_date()` → most recent `trade_date` across all cached symbols, or None

`BacktestMarketDataView(layer, as_of: date, bar_cache, close_cache)`:
- Implements `MarketDataView` protocol (same interface as live `AlpacaMarketDataView`)
- Raises `LookaheadError` if any query requests data beyond `as_of`
- `get_bars(symbol, lookback_days, as_of)` → DataFrame — served from `bar_cache` (in-memory), no SQL
- `get_latest_price(symbol)` → `Decimal` (adjusted close on or before simulation date) — served from `close_cache`
- `advance_to(sim_date)` — moves the view forward to a new simulation date (replaces per-day constructor calls)
- Bar/close caches are pre-fetched once by the engine before the simulation loop, not rebuilt each day

**Performance**: this design eliminates per-day SQL queries. A full 2018–2026 STMR run (88 symbols × ~2100 trading days) previously issued ~21K SQL queries in the hot path; now it issues one bulk fetch at startup. Runtime dropped from ~48 min to ~2 min.

**Data warmup**: engine extends start by 500 calendar days (not 180) so OHLC bars include enough history for a 200-bar SMA on the first simulation day.

**Note on yfinance**: version 1.x (1.3.0 installed) returns a tz-aware `DatetimeIndex`. The data layer normalizes with `.tz_localize(None).normalize()` before the `.date` cast.

### 2. MomentumContinuation strategy (`src/strategies/momentum_continuation.py`)

**Universe** (fixed in code, not config — changes require a code edit):
- 11 sector ETFs: XLE, XLK, XLF, XLV, XLI, XLY, XLP, XLU, XLB, XLRE, XLC
- 3 broad market: SPY, QQQ, IWM
- 2 international: EFA, EEM

**Entry signal**: `close[T] > max(close[T-20], ..., close[T-1])`. Today's close strictly greater than the prior 20-day high. The current day is excluded from the lookback window. Configurable via `lookback_days` (default 20).

**Exit signals** (first trigger wins):
1. **Hard stop**: `current_price < entry_price × (1 − 0.04)`
2. **Trailing stop**: `current_price < highest_close_since_entry × (1 − 0.05)`
3. **Time stop**: `days_held ≥ 10`

**Position sizing**: `entry_weight = risk_per_trade_pct / hard_stop_pct = 0.01 / 0.04 = 0.25`. Each position targets 25% of NAV, bounded by `max_position_pct`. At a 4% stop, a stop-out loses ~1% of NAV.

**Constraints**:
- Max 5 concurrent positions
- Max 100% total deployment
- No short selling, no options

**Parameters** (all in `config/strategies/momentum_continuation.yaml`, overridable per sleeve):

| Parameter | Default |
|-----------|---------|
| `lookback_days` | 20 |
| `hard_stop_pct` | 0.04 |
| `trailing_stop_pct` | 0.05 |
| `time_stop_days` | 10 |
| `risk_per_trade_pct` | 0.01 |
| `max_position_pct` | 0.25 |
| `max_concurrent_positions` | 5 |
| `max_total_deployment_pct` | 1.00 |
| `drawdown_circuit_breaker_pct` | 0.15 |

### 3. Drawdown circuit breaker

- `SleeveStatus.HALTED` added: circuit-breaker-triggered halt, distinct from operator `PAUSE`
- Trigger: `current_nav < high_water_mark × (1 − drawdown_circuit_breaker_pct)`
- Orchestrator checks this at the top of every cycle for RUNNING sleeves; auto-transitions to HALTED
- HALTED sleeves remain in the orchestrator loop; exits (sells) still process; entries (buys) are filtered out before submission
- Recovery: manual `bot-ctl sleeves resume <id>`

### 4. Simulation engine (`src/backtest/engine.py`)

`run_simulation(strategy, params, universe, data_layer, start_date, end_date, starting_capital, slippage_bps, cash_annualized_rate)` → `SimulationResult`

Day-by-day loop:
1. Advance `BacktestMarketDataView` to current date
2. Call `strategy.generate_targets(position_state=...)` with in-memory state dict
3. Translate targets to orders (buys of new entries, sells of exits)
4. Fill pending orders from the prior day at today's open + slippage
5. Accrue daily cash interest (`cash × rate / 252`)
6. Mark-to-market NAV against today's close
7. Generate pending orders for tomorrow (skipped on final day)

Key data structures:
- `TradeRecord`: entry_date, entry_price, shares, exit_date, exit_price, exit_reason, pnl/pnl_pct properties
- `DaySnapshot`: sim_date, nav, cash, positions dict, universe_size
- `SimulationResult`: snapshots list, trades list, `nav_series()` / `cash_series()` helpers

Details:
- `days_held = 0` on the entry day; increments each subsequent session
- Exit reasons from `Target.rationale["reason"]`: hard_stop/trailing_stop/time_stop passed through; everything else → "strategy"
- Cash trimming: if slippage causes fill cost > available cash, order qty is reduced rather than rejected
- End-of-simulation: all open positions closed at last close with `exit_reason = "end_of_simulation"`

### 5. Reporting layer (`src/backtest/reporting.py`)

`generate_report(strategy_result, benchmark_results, output_dir, config)` → Path

**Artifacts written**:
- `report.html` — self-contained HTML with inline Plotly charts
- `trade_log.csv` — one row per completed trade
- `equity_curve.csv` — daily NAV series for strategy and benchmarks
- `statistics.csv` — all computed metrics
- `metadata.json` — run metadata (timestamp, git hash, cache version, config hash)
- `config.yaml` — copy of the run config (if config object provided)

**Computed metrics** (`compute_stats(result, risk_free_rate=0.04)` → `BacktestStats`):

| Metric | Notes |
|--------|-------|
| total_return | (final_nav - starting) / starting |
| annualized_return | (1 + total_return)^(252/n_days) - 1 |
| sharpe_ratio | (ann_return - rf) / (daily_std × √252) |
| sortino_ratio | (ann_return - rf) / (downside_std × √252) |
| max_drawdown | max of (nav - cummax) / cummax |
| calmar_ratio | ann_return / |max_drawdown| |
| num_trades, win_rate | |
| avg_win_pct, avg_loss_pct | |
| profit_factor | sum(wins) / sum(|losses|); inf when no losing trades |
| avg_hold_days | |
| max_concurrent_positions | |

**Charts** (Plotly, inline JS — self-contained HTML):
- Equity curve vs. benchmarks (log scale)
- Drawdown curve
- Monthly returns heatmap

Canonical caveats text per spec §8 (look-ahead risk, data-snooping warning, no transaction tax, etc.) is embedded as a constant and always included in the report footer.

### 6. Backtest CLI (`src/cli/bot_ctl.py` — `bot-ctl backtest` sub-app)

| Command | Action |
|---------|--------|
| `bot-ctl backtest run <config.yaml>` | Run simulation + generate report |
| `bot-ctl backtest refresh-data [--symbol S] [--all] [--force]` | Populate yfinance cache |
| `bot-ctl backtest list [--dir]` | List completed backtest runs |
| `bot-ctl backtest cache-status [--cache-db]` | Per-symbol row count and date range |

**Config model** (`src/backtest/config.py`): `BacktestConfig` (Pydantic v2) with `StrategyConfig` (alias for `class` field — Python keyword workaround), `SimulationConfig`, `OutputConfig`. `end_date: null` resolves to `get_latest_trade_date()` at runtime.

**Strategy registry** (`_STRATEGY_REGISTRY`): maps YAML class names to `(module, class_attr)`. **`UNIVERSE_MAP`**: maps class names to the symbol list used for both strategy universe and benchmark data refresh. Both `MomentumContinuation` and `ShortTermMeanReversion` are registered.

**`refresh-data --all`**: derives the symbol list from `UNIVERSE_MAP` dynamically, so all registered strategies are covered. Previously hardcoded to the 16-ETF momentum universe.

**Backtest configs**:
- `backtest_configs/momentum_2018_baseline.yaml` — MomentumContinuation baseline, 2018-01-01 to latest cache, $10k capital, 5bps slippage
- `backtest_configs/momentum_2018_trailing8.yaml` — MomentumContinuation tuning variant (trailing stop at 8%)
- `backtest_configs/stmr_2018_baseline.yaml` — ShortTermMeanReversion baseline, same date range and capital

### 7. ShortTermMeanReversion strategy (`src/strategies/short_term_mean_reversion.py`)

**Universe** (fixed in code — 88 S&P 100 large-caps continuously traded 2018–2026, survivorship-bias-constrained):
- Technology: AAPL, MSFT, GOOGL, GOOG, META, NVDA, AVGO, TXN, QCOM, IBM, ORCL, ACN, CSCO, INTC, AMD
- Consumer Discretionary: AMZN, TSLA, HD, MCD, NKE, SBUX, TGT, LOW, BKNG, F
- Consumer Staples: WMT, PG, KO, PEP, COST, CL, MO, PM, EL
- Healthcare: JNJ, UNH, PFE, MRK, ABBV, TMO, ABT, MDT, BMY, AMGN, GILD, CVS
- Financials: BRK-B, JPM, BAC, WFC, GS, MS, BLK, AXP, USB, C, MMC, CB
- Industrials: HON, UPS, BA, CAT, DE, MMM, GE, LMT, RTX, FDX
- Energy: XOM, CVX, COP, SLB, EOG
- Materials/Utilities/Real Estate: LIN, APD, NEE, DUK, SO, AMT, PLD
- Communication Services: VZ, T, DIS, CMCSA, NFLX

**Entry signal**: `RSI(2) < entry_rsi_threshold` AND `close > 200-day SMA`. Candidates ranked by RSI ascending (most oversold first) when position slots are limited.

**Exit signals** (first trigger wins):
1. **Hard stop**: `current_price < entry_price × (1 − 0.08)` — wide by design; deeper oversold is more opportunity, not failure
2. **RSI exit**: `RSI(2) > exit_rsi_threshold` (mean reversion complete)
3. **Time stop**: `days_held ≥ time_stop_days`

**Position sizing**: `entry_weight = risk_per_trade_pct / hard_stop_pct = 0.01 / 0.08 = 0.125`. Each position targets 12.5% of NAV, bounded by `max_position_pct`. At an 8% stop, a stop-out loses ~1% of NAV.

**Constraints**:
- Max 8 concurrent positions
- Max 100% total deployment
- No short selling, no options

**RSI implementation**: Wilder RSI using EWM smoothing. When `avg_loss = 0` (no losing periods), the direct-division path returns `inf`, which produces `RSI = 100` (overbought) correctly via `100 - 100/(1+inf)`. When `avg_gain = avg_loss = 0` (flat price), the result is `NaN`, treated as ineligible for entry/exit.

**Parameters** (all in `config/strategies/short_term_mean_reversion.yaml`, overridable per sleeve):

| Parameter | Default |
|-----------|---------|
| `lookback_days` | 210 |
| `entry_rsi_threshold` | 10 |
| `exit_rsi_threshold` | 70 |
| `hard_stop_pct` | 0.08 |
| `time_stop_days` | 15 |
| `risk_per_trade_pct` | 0.01 |
| `max_position_pct` | 0.20 |
| `max_concurrent_positions` | 8 |
| `max_total_deployment_pct` | 1.00 |
| `drawdown_circuit_breaker_pct` | 0.15 |

Minimum `lookback_days` is enforced at 210 (200 bars for SMA + RSI(2) warmup). Raises `ValueError` if set lower.

**Baseline backtest config**: `backtest_configs/stmr_2018_baseline.yaml` — 2018-01-01 to latest cache date, $10k capital, 5bps slippage.

### 8. strategy_state — live position tracking (`src/tracking/`)

`strategy_state` table: `(sleeve_id, symbol)` composite PK, plus `entry_date`, `entry_price`, `days_held`, `highest_close_since_entry`, `last_session_date`.

**`last_session_date`** is the gate that prevents `days_held` from incrementing more than once per trading session. When `last_session_date < today`, the orchestrator increments `days_held` and sets `last_session_date = today`. This ensures the live counter behaves the same as the backtest engine's day-by-day loop.

**Lifecycle** (managed by `SleeveManager.attribute_fill`):
- New buy fill (no existing position): creates entry with `days_held=0`, `highest_close_since_entry=fill_price`
- Adding to an existing position: entry untouched (original entry_price preserved)
- Full sell (qty → 0): deletes entry
- Partial sell: entry untouched

---

## Production smoke test results (2026-05-05)

Two paper BuyAndHoldSPY sleeves ran against Alpaca paper during market hours. Both submitted and filled SPY limit orders. Three bugs were found and fixed:

1. **Stale price** (`orchestrator.py`): `get_latest_price` used yesterday's close → limit orders ~$4 below market. Fixed with `StockLatestTradeRequest`.
2. **Drain loop spelling** (`router.py`): `"cancelled"` vs `"canceled"` caused fill-polling loop to hang. Fixed.
3. **NAV math** (`manager.py`): `attribute_fill` subtracted full cash cost from NAV on buy → $0 NAV. Fixed with correct accounting (buy: cash → position at cost, NAV unchanged minus fees; sell: NAV += realized P&L minus fees).

---

## Known gaps / what isn't built yet

| Gap | Impact |
|-----|--------|
| Mark-to-market NAV | NAV updated only on fills (cost basis). No intraday MTM. |
| `bot-ctl sleeves resume` | Method exists in manager; not yet wired into CLI |
| Kill switch live validation | Mechanically tested; not validated end-to-end under time pressure |

---

## Test coverage

| Module | Tests |
|--------|-------|
| Config loading and validation | ✓ |
| Logging configuration | ✓ |
| Alpaca client (mocked) | ✓ |
| Account capabilities | ✓ |
| Asset universe | ✓ |
| Strategy interface + BuyAndHoldSPY | ✓ |
| MomentumContinuation (25 tests) | ✓ |
| ShortTermMeanReversion (31 tests) | ✓ |
| Sleeve manager (lifecycle, NAV, fills) | ✓ |
| Portfolio sizer | ✓ |
| Execution router (submit, net, poll) | ✓ |
| Risk checks | ✓ |
| Orchestrator (loop, kill switch) | ✓ |
| Backtest data layer (17 tests) | ✓ |
| Backtest simulation engine (21 tests) | ✓ |
| Backtest reporting (28 tests) | ✓ |
| Backtest config model | ✓ |
| strategy_state repo + attribute_fill (12 tests) | ✓ |
| **Total** | **301** |
