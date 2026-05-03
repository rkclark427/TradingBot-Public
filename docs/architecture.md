# Architecture

> Multi-strategy sleeve-based trading platform. Version 0.2.

## Executive summary

This document specifies a multi-strategy algorithmic trading platform running on a Linux VM, executing trades through Alpaca's Trading API. Unlike a single-strategy bot, this platform hosts a library of pluggable strategies, each running as an isolated **sleeve** with its own capital allocation, mode (paper or live), and parameters. Multiple sleeves run concurrently — for example, $500 on Algo A live, $500 on Algo B live, and Algo C paper trading for evaluation.

Key design choices:

- **Sleeves are first-class.** Each is an isolated runtime instance of a strategy with its own NAV that compounds with its own performance.
- **Strategy library is extensible.** Adding a new strategy is a matter of writing one file conforming to the Strategy interface.
- **Capability layer enforces what's tradable.** The engine knows what the Alpaca account can do, what each strategy needs, and what each symbol's status is.
- **Order netting within an environment.** If two live sleeves want opposite-direction trades on the same symbol, they net before hitting Alpaca.
- **Two-level risk controls.** Account-level (master safety across all sleeves) and sleeve-level (per-strategy discipline).
- **Three strategies at launch:** mean reversion (the showcase), buy-and-hold SPY (the benchmark), sector rotation (interface stress test).
- **CLI control surface in v1; web dashboard deferred.**

## 1. Core concepts

### 1.1 Strategy

A class in the strategy library implementing the `Strategy` interface. Pure logic: given inputs (universe, market data, current sleeve positions, sleeve NAV), returns target positions as weights. A strategy does not know its capital allocation, does not know whether it is running paper or live, does not know whether other strategies are running, does not place orders. Stateless across runs except where explicitly persisted.

### 1.2 Sleeve

A runtime instance of a strategy with its own configuration and state. The same strategy class can be instantiated as multiple sleeves with different parameters or different modes. A sleeve has:

- A unique identifier
- A reference to a strategy class
- A parameter set (passed to the strategy at signal generation time)
- A mode: paper or live
- A starting bankroll (e.g., $500), set at creation
- A current NAV that compounds with realized + unrealized P&L
- Its own positions, attributed in the database
- A status: running, paused, stopping, stopped
- Per-sleeve risk parameters

Sleeves are the unit of management. You create, pause, resume, promote (paper → live), deposit into, withdraw from, and stop sleeves. The bot iterates through all running sleeves on each cycle.

### 1.3 Capital and NAV

The Alpaca live account holds total cash. That cash is divided internally into:

- **Sleeve NAVs** — cash + position market value belonging to each live sleeve
- **Unallocated pool** — cash not yet assigned to any sleeve. New deposits land here.

When a sleeve is created with a starting bankroll, that amount is moved from the unallocated pool into the sleeve. When a sleeve is stopped, its remaining NAV returns to the unallocated pool. When a sleeve is deposited into or withdrawn from, capital moves between the pool and the sleeve.

Sleeve NAV updates daily based on:
- Cash held by the sleeve (proportional to its share of the account)
- Market value of positions attributed to the sleeve
- Realized P&L from sleeve's closed positions

The paper environment uses Alpaca's separate paper account. Paper sleeves have their own NAV in the paper account, parallel to the live structure. There is no internal simulator in v1.

### 1.4 Capability layer

Three-tier capability check, all enforced before any order is submitted:

1. **Account capabilities.** On startup and refreshed daily, the bot queries Alpaca for: enabled asset classes, options trading level, fractional shares enabled, shorting enabled, day-trading status, buying power, account restrictions. Cached locally as the authoritative "what we can do" list.

2. **Strategy capability declarations.** Each strategy declares its requirements: asset classes, shorting, options, fractional shares, minimum cash buffer, etc. At sleeve creation time, the engine validates the strategy's needs against account capabilities. Mismatch refuses sleeve creation with a clear error.

3. **Tradable symbol universe.** Alpaca's assets endpoint enumerates all supported symbols with attributes (tradable, fractionable, marginable, shortable, easy-to-borrow). Pulled nightly, cached locally. Strategies request symbols from this universe; symbols outside are silently excluded.

4. **Per-order check.** At order submission time, even after all the above, a final per-symbol check: is the symbol currently tradable, halted, or otherwise restricted right now? If any check fails, order rejected with a logged reason.

### 1.5 Modes

Two modes mapped to Alpaca's two account environments:

- **Live mode** — sleeve trades against the Alpaca live account. Real money.
- **Paper mode** — sleeve trades against the Alpaca paper account. Real market data, simulated fills, no money at risk.

Sleeves of the same strategy can run in both modes simultaneously (different sleeves, different environments). Order netting applies within an environment but not across — paper sleeve trades and live sleeve trades never see each other.

## 2. Repository structure

```
trading-bot/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── .env.example
├── config/
│   ├── base.yaml
│   ├── sleeves.yaml
│   └── strategies/
│       ├── mean_reversion.yaml
│       ├── buy_and_hold.yaml
│       └── sector_rotation.yaml
├── src/
│   ├── data/
│   ├── strategies/
│   │   ├── base.py
│   │   ├── mean_reversion.py
│   │   ├── buy_and_hold.py
│   │   └── sector_rotation.py
│   ├── sleeves/
│   ├── portfolio/
│   ├── risk/
│   ├── execution/
│   ├── tracking/
│   ├── monitoring/
│   ├── backtest/
│   ├── queries/
│   ├── commands/
│   ├── cli/
│   └── orchestrator.py
├── scripts/
├── tests/
├── data/
└── logs/
```

## 3. Strategy interface

The contract every strategy implements. Has to be general enough that mean reversion, buy-and-hold, and sector rotation all fit comfortably, with room for future strategies (event-driven, vol premium, congressional disclosure overlay).

### 3.1 Capability declaration

```python
@dataclass(frozen=True)
class StrategyCapabilities:
    asset_classes: set[str]            # {'us_equity'}, {'us_equity', 'us_option'}, etc.
    requires_shorting: bool
    requires_options: bool
    requires_fractional: bool
    min_cash_buffer_pct: float         # e.g. 0.05 for 5%
    rebalance_cadence: str             # 'daily', 'weekly', 'monthly', 'event'
    typical_holding_period_days: int   # informational
```

### 3.2 Signal generation

```python
class Strategy(ABC):
    name: str
    capabilities: StrategyCapabilities

    @abstractmethod
    def generate_targets(
        self,
        params: dict,
        nav: float,
        cash: float,
        positions: dict[str, Position],
        market_data: MarketDataView,
        universe: list[str],
        as_of: datetime,
    ) -> list[Target]:
        ...

@dataclass
class Target:
    symbol: str
    target_weight: float    # fraction of sleeve NAV; negative = short
    rationale: dict         # for logging and debugging
```

Notes:
- Targets are weights, not orders. The portfolio layer translates weights to share quantities.
- Strategy returns the FULL desired portfolio, not deltas. A no-op strategy returns its current positions as targets. A strategy that wants to fully exit returns an empty list.
- Strategy doesn't see other sleeves, doesn't see total account, doesn't see paper-vs-live. Pure isolation.

## 4. Core components

### 4.1 Orchestrator

The main loop. Runs continuously as a systemd service. On each tick:

1. Check global kill switch and account-level halts. If halted, skip cycle.
2. Check market hours and trading calendar.
3. For each running sleeve: ask its strategy for targets, route through portfolio layer, accumulate intended orders.
4. Net intended orders within each environment.
5. Run risk checks on each net order.
6. Submit surviving orders to the appropriate Alpaca environment.
7. Process fills, update sleeve attribution, update NAVs.
8. Write daily snapshot at end-of-day.
9. Heartbeat.

### 4.2 Sleeve manager

Owns sleeve lifecycle and state:
- Sleeve CRUD: create, pause, resume, stop, deposit, withdraw, promote
- Capital movement between unallocated pool and sleeves
- NAV computation per sleeve
- Position attribution: when a fill comes back from Alpaca, assigns shares to the correct sleeve based on the originating order's sleeve tag
- Per-sleeve risk monitoring

### 4.3 Portfolio layer

Translates strategy targets (weights) into intended orders (share quantities). For each sleeve:

1. Compute desired notional per target = `target_weight × sleeve_NAV`
2. Compute desired share qty = `desired_notional / current_price`
3. Compute delta vs. current position = `desired_qty − current_qty`
4. If delta is non-trivial, create an intended order tagged with `sleeve_id`

### 4.4 Order netting

Within an environment, before submission to Alpaca:
- Group intended orders by symbol
- Net buys against sells. If Sleeve A wants +10 AAPL and Sleeve B wants −4 AAPL, the net order is +6.
- Submit one net order per symbol with a synthetic `client_order_id`
- When fill comes back, allocate fill proportionally to constituent intended orders. Each sleeve sees its intended fill (10 and −4 in the example).
- Slippage savings from netting distributed pro-rata to constituent sleeves
- Database records both constituent intents and the resulting net order, so we can always reconstruct what each sleeve actually intended

### 4.5 Risk layer (two-tier)

**Account-level (master safety across all sleeves):**
- Total account daily loss limit: −5% across all live sleeves combined → halt all new live entries
- Total account drawdown halt: −20% from live account peak → halt all live trading until manual reset
- Order rate limits: 100/day, 20/min across all sleeves
- Reconciliation halt: if DB sleeve attribution diverges from Alpaca account state
- Manual kill switch: halts all new orders across all sleeves immediately

**Sleeve-level (per-strategy discipline):**
- Sleeve daily loss limit: configurable per sleeve, default −3% of sleeve NAV
- Sleeve drawdown halt: configurable per sleeve, default −15% from sleeve high-water mark
- Sleeve concentration limit: no single position > 30% of sleeve NAV
- Sleeve order rate: per-sleeve limits to detect runaway logic

Risk events at either level are logged. Sleeve-level halts pause only that sleeve. Account-level halts pause everything.

### 4.6 Execution layer

- Two Alpaca clients: live (with live API keys) and paper (with paper API keys). Routed by sleeve mode.
- **Limit orders only.** Limit price = previous close ± offset (default 10 bps); cancel-and-retry-next-day if unfilled by some cutoff.
- Idempotent `client_order_id`s: derived from `(date, sleeve_id_or_net, symbol, sequence)`
- Reconciliation on startup: fetch positions and orders from both Alpaca environments, compare to DB state; halt if divergent
- Fractional shares enabled (required for $500 sleeves)

### 4.7 Data layer

- Alpaca Market Data API (IEX feed; sufficient at our scale)
- Historical bars cached locally as Parquet, keyed by symbol and date range
- Universe builder: weekly job, produces filtered tradable universe
- Asset list refresh: nightly, pulls Alpaca's full assets endpoint, caches with attributes
- Capability discovery: daily refresh of account capabilities
- Earnings calendar: secondary data source (Finnhub free tier or similar) with fallback

### 4.8 Tracking layer

SQLite. Schema in section 6.

### 4.9 Monitoring layer

- Heartbeat written to DB every minute; external cron job alerts if stale
- Pushover for critical alerts (account halt, drawdown halt, reconciliation failure, repeated order rejections)
- Daily email summary: per-sleeve NAV change, positions, risk events, account totals
- Logs: structured JSON, written to `/var/log/trading-bot/`, rotated daily

### 4.10 CLI control surface

Primary control interface for v1. SSH-accessible from headless VM. Implementation: Typer.

| Command | Purpose |
|---|---|
| `bot-ctl status` | Overall bot health, account state, kill switch state |
| `bot-ctl sleeves list` | All sleeves with NAV, mode, status, today's P&L |
| `bot-ctl sleeves show <id>` | Detail: positions, recent trades, parameters, history |
| `bot-ctl sleeves create --strategy <name> --mode <m> --capital <amt>` | Create new sleeve |
| `bot-ctl sleeves pause <id>` | Pause sleeve (no new entries, exits still allowed) |
| `bot-ctl sleeves resume <id>` | Resume paused sleeve |
| `bot-ctl sleeves stop <id>` | Stop sleeve, liquidate positions, return capital |
| `bot-ctl sleeves promote <id>` | Paper → live (with prompt confirmation) |
| `bot-ctl sleeves deposit <id> --amount <amt>` | Move capital from pool into sleeve |
| `bot-ctl sleeves withdraw <id> --amount <amt>` | Move capital from sleeve to pool |
| `bot-ctl pnl --period <d/w/m/all>` | P&L summary by sleeve and total |
| `bot-ctl risk status` | Active risk limits, current usage, halts |
| `bot-ctl kill` | Emergency halt all trading |
| `bot-ctl resume` | Resume after kill (with confirmation) |
| `bot-ctl logs <id|all> [--tail]` | Tail logs filtered to one sleeve or all |
| `bot-ctl backtest <strategy> --params <yaml> --period <range>` | Run backtest |

Each command is also exposed as a callable Python function in `src/queries/` (read) or `src/commands/` (write). The CLI is just a thin wrapper. This makes future OpenClaw integration trivial.

## 5. Strategy library — launch set

### 5.1 Mean Reversion

The showcase strategy. The first one we'll deploy with real money beyond the SPY benchmark.

- **Asset class:** US equities, long-only
- **Cadence:** Daily evaluation after close, orders at next open
- **Holding period:** 1-5 trading days
- **Universe:** S&P 500 minus top 20 by market cap; ADV > $20M; price > $10; beta 0.5-1.5; not within 2 days of earnings. ~300-400 names.
- **Entry:** 5-day return z-score < -1.5 across universe; RSI(2) < 10; price > 200-day MA. Top 5 by signal strength, equal weighted.
- **Exit:** Profit target +4%, stop loss −6%, time stop 5 days, RSI(2) > 70, or earnings imminent.
- **Capabilities required:** us_equity, fractional, no shorting, no options.

### 5.2 Buy and Hold SPY

The benchmark. Simplest possible strategy. Useful because (a) any other sleeve's performance can be compared directly to a SPY sleeve running the same period, and (b) it's the smallest possible test of whether the engine works at all.

- **Asset class:** US equity (SPY ETF)
- **Cadence:** Once at sleeve creation; nothing thereafter except dividend handling
- **Logic:** On first run, allocate 100% of sleeve NAV to SPY. Every subsequent run, do nothing.
- **Capabilities required:** us_equity, fractional.

### 5.3 Sector Rotation

Slow-cadence momentum on sector ETFs. Tests whether the interface handles monthly rebalancing as cleanly as daily.

- **Asset class:** US equity (sector SPDR ETFs)
- **Universe:** XLF, XLE, XLI, XLV, XLP, XLU, XLB, XLRE, XLY, XLK, XLC (11 sectors)
- **Cadence:** Monthly rebalance, first trading day of month
- **Logic:** Rank ETFs by 3-month trailing return. Hold top 3, equal-weighted. Sell the rest.
- **Holding period:** Typically 1-3 months per name
- **Capabilities required:** us_equity, fractional.

### 5.4 Strategy stubs (future)

Strategies sketched in the strategy library as stubs (no implementation, just capability declarations) to validate the interface accommodates them:

- Earnings drift (event-driven): post-earnings announcement drift on positive surprises
- Volatility risk premium: cash-secured puts. Requires options.
- Congressional disclosure overlay: ingest disclosed congressional trades, signal weights based on filer track record
- Pairs trading: cointegrated stock pairs, mean-revert the spread

## 6. Database schema

SQLite. Migrations via Alembic. Core tables:

| Table | Key columns |
|---|---|
| `sleeves` | `id, strategy_name, mode, status, starting_capital, current_nav, high_water_mark, created_at, parameters_json` |
| `sleeve_capital_events` | `id, sleeve_id, timestamp, event_type, amount, reason` |
| `signals` | `id, timestamp, sleeve_id, symbol, target_weight, rationale_json` |
| `intended_orders` | `id, timestamp, sleeve_id, symbol, side, qty, limit_price, status` |
| `net_orders` | `id, timestamp, mode, symbol, side, net_qty, limit_price, alpaca_id, status` |
| `intended_to_net` | `intended_order_id, net_order_id, allocated_qty` |
| `fills` | `id, net_order_id, timestamp, qty, fill_price, fees` |
| `sleeve_fills` | `id, fill_id, sleeve_id, allocated_qty, allocated_price` |
| `positions` | `sleeve_id, symbol, qty, avg_cost` |
| `sleeve_nav_snapshots` | `date, sleeve_id, nav, cash, position_value, realized_pnl_today` |
| `account_snapshots` | `date, total_equity, total_cash, unallocated_cash, mode` |
| `risk_events` | `id, timestamp, level, sleeve_id_nullable, event_type, severity, details_json` |
| `system_events` | `id, timestamp, event_type, message, context_json` |
| `asset_universe` | `symbol, tradable, fractionable, marginable, shortable, etb, last_refreshed` |
| `account_capabilities` | `snapshot_date, capability_key, capability_value` |
| `heartbeat` | `timestamp` (single-row table) |

The split between `intended_orders` / `net_orders` / `intended_to_net` is what enables clean attribution after netting. Each sleeve sees its own intended trade flow; the netted reality is also persisted; the mapping table reconciles them.

## 7. Backtesting framework

Backtests run per strategy, not per sleeve. A backtest is: given strategy code, parameters, capital, and a historical period, simulate the resulting NAV trajectory.

### 7.1 Required capabilities

- Realistic execution: fills at next-day open with slippage assumption (5-10 bps for liquid names)
- Survivorship-bias-aware universe (within the limits of available historical data)
- Walk-forward analysis: in-sample tuning + out-of-sample validation
- Per-strategy uses the same Strategy class as the live system, ensuring backtest-execution parity

### 7.2 Outputs

- Equity curve, drawdown chart, monthly returns heatmap
- Sharpe, Sortino, max drawdown, Calmar, win rate, profit factor, average holding period
- Comparison to SPY buy-and-hold over the same period
- Sensitivity analysis across the parameter neighborhood

### 7.3 Go/no-go deployment criteria

Out-of-sample thresholds for promoting a strategy from backtest to live:

| Metric | Threshold |
|---|---|
| Sharpe ratio | > 0.8 |
| Max drawdown | < 25% |
| Win rate | > 50% |
| Profit factor | > 1.3 |
| Beats SPY (risk-adjusted) | Yes |
| Stable across parameter neighborhood | Yes |

Buy-and-hold SPY is the obvious exception — it doesn't need to clear the gate to be useful as a benchmark.

## 8. Configuration

### 8.1 Engine config (`config/base.yaml`)

Defaults shared across all sleeves: data sources, account-level risk limits, monitoring config, execution defaults.

### 8.2 Sleeve registry (`config/sleeves.yaml`) — Hybrid model

We use **Option C: hybrid**. Each sleeve declares a `managed: true|false` flag.

- `managed: true` — YAML is canonical for that sleeve. CLI changes are temporary; on restart the YAML state is restored. Use for "production" sleeves you've committed to.
- `managed: false` (or sleeves created via CLI) — DB is canonical. CLI is the control surface. Use for experiments.

Example:

```yaml
sleeves:
  - id: spy_benchmark
    strategy: buy_and_hold
    mode: live
    starting_capital: 100
    parameters_file: config/strategies/buy_and_hold.yaml
    managed: true
    enabled: true

  - id: mr_live_main
    strategy: mean_reversion
    mode: live
    starting_capital: 500
    parameters_file: config/strategies/mean_reversion.yaml
    parameter_overrides:
      lookback_days: 5
      entry_zscore_threshold: -1.5
    sleeve_risk:
      daily_loss_pct: 0.03
      drawdown_halt_pct: 0.15
    managed: true
    enabled: true

  - id: sector_rotation_paper
    strategy: sector_rotation
    mode: paper
    starting_capital: 500
    parameters_file: config/strategies/sector_rotation.yaml
    managed: true
    enabled: true
```

### 8.3 Strategy parameters

Per-strategy parameter files in `config/strategies/`. Each strategy's defaults live in its own file. Sleeve-level overrides go in `sleeves.yaml` or the CLI.

## 9. Build sequence

Six phases. Each gated. Don't proceed past a gate without confirming the gate criteria are met. Detailed task lists in `docs/phases/`.

| Phase | Focus | Gate |
|---|---|---|
| 1 | Foundation & sleeve architecture | BuyAndHoldSPY paper sleeve runs end-to-end |
| 2 | Backtesting & strategies | MeanReversion clears deployment criteria on out-of-sample data |
| 3 | Execution & multi-sleeve | Two paper sleeves run concurrently with all risk controls verified |
| 4 | Paper validation week | Multi-sleeve operation, edge cases, no critical bugs |
| 5 | Live deployment (one sleeve) | First live sleeve runs cleanly; live ≈ paper performance |
| 6 | Library expansion | Additional strategies, dashboard considered |

## 10. Risk controls — consolidated

| Control | Trigger | Action | Reset |
|---|---|---|---|
| Account daily loss | −5% across all live sleeves | Halt new live entries | Midnight ET |
| Account drawdown | −20% from peak | Halt all live trading | Manual |
| Account order rate | >100/day or >20/min | Reject excess | Per window |
| Reconciliation mismatch | DB ≠ Alpaca | Halt all trading | Manual |
| Heartbeat stale | >5 min | External alert; systemd restart | Auto |
| Manual kill switch | Operator sets flag | Halt all new orders | Operator clears |
| Sleeve daily loss | −3% of NAV | Pause sleeve for day | Midnight ET |
| Sleeve drawdown | −15% from HWM | Pause sleeve | Manual |
| Sleeve concentration | >30% of NAV in one symbol | Reject order | Per-order |
| Order size sanity | >50% of NAV in one order | Reject order | Per-order |
| Capability mismatch | Strategy needs > account supports | Refuse sleeve creation | Fix config |
| Symbol untradable | Halted, delisted, restricted | Skip + reject | Resolves with symbol |

## 11. Open questions

- Historical data source for backtests (Alpaca + yfinance hybrid leaning, but not finalized)
- Earnings calendar source (Finnhub free tier likely)
- Backup strategy (SQLite to Azure Blob daily likely)
- VM size — investigate why D2 is the smallest available; try other regions or check subscription policy

## Glossary

| Term | Definition |
|---|---|
| Strategy | A class in the strategy library implementing the Strategy interface. Pure logic. |
| Sleeve | A runtime instance of a strategy with mode, capital, parameters, and state. |
| NAV | Net Asset Value. A sleeve's current value: cash + position market value. |
| HWM | High-Water Mark. The peak NAV a sleeve has achieved. Used for drawdown computation. |
| Unallocated pool | Cash in the Alpaca account not currently assigned to any sleeve. |
| Capability | An account or strategy attribute (e.g., "options enabled"). |
| Intended order | A sleeve's pre-netting desired trade. |
| Net order | A post-netting order actually submitted to Alpaca. |
| Mode | paper or live. Determines which Alpaca environment a sleeve uses. |
| Promote | Convert a paper sleeve to live (with fresh capital from the unallocated pool). |
