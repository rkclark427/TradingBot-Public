# Phase 1: Foundation & Sleeve Architecture

> Goal: a working sleeve manager that can create a paper BuyAndHoldSPY sleeve with $100, watch it submit a single buy order through the engine to Alpaca paper, see the fill come back, and compute correct NAV as SPY moves.

## Phase gate

The phase is complete when **all** of the following are true:

1. A paper sleeve can be created via CLI: `bot-ctl sleeves create --strategy buy_and_hold --mode paper --capital 100`
2. The sleeve persists across bot restarts
3. On orchestrator tick, the BuyAndHoldSPY strategy runs and produces a target of `[Target(symbol='SPY', target_weight=1.0, rationale={...})]`
4. The portfolio layer translates that to an intended order (buy fractional SPY shares totaling ~$100)
5. The risk layer passes the order
6. The execution layer submits a limit order to Alpaca paper, with proper `client_order_id`
7. When the fill arrives, position attribution and NAV update correctly
8. `bot-ctl sleeves show <id>` displays accurate state: NAV, position, recent activity
9. Capability discovery surfaces account state correctly: `bot-ctl status` shows account capabilities reflecting the actual paper account
10. Tests pass

The phase is **not** complete if mean reversion logic, order netting, or backtesting are partially built. Stay in scope.

## Tasks

### 1. Project setup
- [ ] `pyproject.toml` with dependencies: alpaca-py, sqlalchemy, alembic, pydantic, pyyaml, typer, structlog (or stdlib logging configured for JSON), httpx, pandas, pyarrow
- [ ] `.env.example` documenting required env vars: `ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_API_SECRET`, `ALPACA_LIVE_API_KEY` (later), `ALPACA_LIVE_API_SECRET` (later), `PUSHOVER_USER_KEY` (later), `PUSHOVER_API_TOKEN` (later)
- [ ] `.gitignore` covering `.env`, `data/`, `logs/`, `*.db`, `__pycache__`, `.venv`, `.pytest_cache`
- [ ] Pre-commit config with ruff (lint + format) and mypy
- [ ] Basic CI (GitHub Actions): run tests + linting on push

### 2. Configuration system
- [ ] Pydantic models for `BaseConfig`, `SleeveConfig`, `StrategyParameters` in `src/config/`
- [ ] YAML loader that reads `config/base.yaml` and `config/sleeves.yaml` and produces validated config objects
- [ ] Environment variable overrides for sensitive values
- [ ] Tests covering: valid config parses, invalid config raises clearly, env vars override file

### 3. Logging infrastructure
- [ ] Structured JSON logging configured at startup
- [ ] Required fields: `timestamp`, `level`, `event`, `module`. Plus context-specific fields.
- [ ] Helper to add sleeve_id context to logs within a sleeve's execution scope
- [ ] Logs written to `./logs/trading-bot.log` in dev (rotation deferred until prod deployment in Phase 3)

### 4. Database schema (Phase 1 subset)
- [ ] SQLAlchemy models in `src/tracking/models.py` for the tables needed in Phase 1:
  - `sleeves`
  - `sleeve_capital_events`
  - `signals`
  - `intended_orders`
  - `net_orders` (single-sleeve case in P1, but build the table now)
  - `intended_to_net` (single-row mapping in P1)
  - `fills`
  - `sleeve_fills`
  - `positions`
  - `sleeve_nav_snapshots`
  - `account_snapshots`
  - `system_events`
  - `asset_universe`
  - `account_capabilities`
  - `heartbeat`
- [ ] Initial Alembic migration creating these tables
- [ ] Repository classes for each (`src/tracking/repos/`) with type-hinted CRUD methods
- [ ] Tests covering schema creation, migration up/down, basic CRUD

### 5. Alpaca client wrappers
- [ ] `src/data/alpaca_client.py` with `LiveClient` and `PaperClient` classes
- [ ] Methods needed in Phase 1:
  - `get_account()` — account state including capabilities
  - `get_assets()` — full asset list with attributes
  - `get_positions()` — current positions
  - `get_orders(status, ...)` — recent orders
  - `submit_order(...)` — submit limit order
  - `get_bars(symbol, timeframe, start, end)` — historical bars
  - `get_clock()` — market hours / open status
- [ ] Retry logic with exponential backoff on transient errors
- [ ] Tests with mocked Alpaca responses (use VCR.py or mock at the HTTP layer)

### 6. Capability discovery layer
- [ ] `src/data/capabilities.py` with `discover_account_capabilities(client) -> AccountCapabilities`
- [ ] `AccountCapabilities` dataclass: asset classes enabled, options level, fractional shares, shorting, PDT status, buying power
- [ ] Daily refresh job (manual trigger in P1; scheduled in Phase 3)
- [ ] Persist snapshots to `account_capabilities` table
- [ ] Tests with mocked client

### 7. Asset universe
- [ ] `src/data/assets.py` with `refresh_asset_universe(client)` pulling Alpaca's `/v2/assets` endpoint and persisting to `asset_universe` table
- [ ] Query function: `get_tradable_symbols(constraints) -> list[str]` for strategies
- [ ] Tests

### 8. Strategy interface
- [ ] `src/strategies/base.py` with:
  - `Strategy` ABC
  - `StrategyCapabilities` dataclass (frozen)
  - `Target` dataclass
  - `Position` dataclass (current sleeve position)
  - `MarketDataView` protocol (interface for accessing historical bars from a strategy)
- [ ] `src/strategies/buy_and_hold.py` implementing BuyAndHoldSPY:
  - Capability declaration: `us_equity`, fractional, no shorting, no options, daily cadence (though it does almost nothing)
  - Logic: if no SPY position, return `[Target('SPY', 1.0, {'reason': 'initial allocation'})]`. Otherwise return current positions as targets (no-op).
- [ ] Tests covering: strategy returns expected targets in initial state and steady state

### 9. Sleeve manager
- [ ] `src/sleeves/manager.py` with `SleeveManager` class
- [ ] Operations:
  - `create_sleeve(strategy_name, mode, starting_capital, params, ...) -> Sleeve`
  - `get_sleeve(id) -> Sleeve`
  - `list_sleeves(filter) -> list[Sleeve]`
  - `pause_sleeve(id)`, `resume_sleeve(id)`, `stop_sleeve(id)`
  - `compute_nav(sleeve_id) -> Decimal` — current NAV from positions + cash attribution
  - `attribute_fill(fill, intended_order)` — assigns fill shares to the right sleeve
  - `record_capital_event(sleeve_id, event_type, amount, reason)`
- [ ] Capability validation on sleeve creation: strategy capabilities checked against current account capabilities; clear error on mismatch
- [ ] Sleeve NAV computation logic — for Phase 1 this is straightforward (only one sleeve, no netting), but write it general
- [ ] Tests covering: sleeve lifecycle, capability validation rejection, NAV computation given a position and cash

### 10. Portfolio layer (Phase 1 subset)
- [ ] `src/portfolio/sizer.py` with `targets_to_intended_orders(sleeve, targets, current_prices) -> list[IntendedOrder]`
- [ ] Logic: for each target, compute desired shares = `target_weight * sleeve_nav / price`, round per Alpaca rules (fractional to 6 decimals or whole shares depending on symbol); compute delta vs current; create intended order if delta > min threshold
- [ ] Min order threshold: skip orders below $1 notional (avoid dust)
- [ ] Tests covering: full allocation from cash, no-op when already at target, partial rebalance

### 11. Execution layer (Phase 1 subset)
- [ ] `src/execution/router.py` with `submit_intended_orders(orders, env)` — Phase 1 has no netting (one sleeve, one symbol), but build the path as if netting will happen so Phase 3 expansion is clean
- [ ] Idempotent `client_order_id` generation: `f"{date}-{sleeve_id_or_net}-{symbol}-{seq}"`
- [ ] Submit limit orders with limit price = previous close ± 10 bps
- [ ] Persist net_orders and intended_to_net rows
- [ ] Fill listener (polling-based in P1; webhook later if it's worth it): on fill, write fills + sleeve_fills + update positions table + update sleeve NAV
- [ ] Tests with mocked Alpaca client

### 12. Risk layer (Phase 1 subset)
- [ ] `src/risk/checks.py` with pre-trade risk checks
- [ ] Phase 1 checks:
  - Order size > 50% of sleeve NAV → reject
  - Symbol not tradable per asset universe → reject
  - Manual kill switch set → reject
- [ ] Other checks (drawdown, daily loss, concentration) deferred to Phase 3
- [ ] Tests for each check passing and failing

### 13. Orchestrator (Phase 1 minimum)
- [ ] `src/orchestrator.py` with main loop
- [ ] Phase 1 cycle: for each running sleeve, generate targets → translate to intended orders → run risk checks → submit → persist
- [ ] Cycle cadence: configurable, default once per minute during market hours, idle at other times
- [ ] Heartbeat written each cycle
- [ ] Reconciliation on startup: compare DB sleeve positions to Alpaca positions, halt if divergent
- [ ] Tests with mocked Alpaca for full-cycle execution

### 14. CLI (Phase 1 subset)
- [ ] Typer app at `src/cli/bot_ctl.py`
- [ ] Phase 1 commands:
  - `bot-ctl status` — bot health, account state, kill switch state
  - `bot-ctl sleeves list`
  - `bot-ctl sleeves show <id>`
  - `bot-ctl sleeves create --strategy <name> --mode <m> --capital <amt>`
  - `bot-ctl sleeves stop <id>`
  - `bot-ctl kill` and `bot-ctl resume`
- [ ] Each command implemented as a thin wrapper over a function in `src/queries/` or `src/commands/`
- [ ] Tests covering CLI command outputs

### 15. End-to-end smoke test
- [ ] Manual or scripted: from a clean state, create a paper BuyAndHoldSPY sleeve with $100, run the orchestrator for one cycle, observe the SPY buy submitted to Alpaca paper, watch the fill come back, verify NAV reflects the fill
- [ ] Document the smoke test procedure in `docs/smoke-test.md`

## Definitely out of scope for Phase 1

- MeanReversion strategy (Phase 2)
- SectorRotation strategy (Phase 2)
- Backtesting framework (Phase 2)
- Order netting in the multi-sleeve sense (Phase 3 — but the code paths are built to be netting-ready)
- Drawdown halts, daily loss halts, concentration limits beyond order size (Phase 3)
- Pushover alerts (Phase 3)
- Daily summary email (Phase 3)
- systemd deployment (Phase 3)
- Promote / deposit / withdraw CLI commands (Phase 3)

## Notes for Claude Code

- Stick to the scope. The temptation will be to build forward into mean reversion or netting because they're "easy enough." Don't. Phase gates exist for a reason.
- Capability validation is critical. Test it. A sleeve created for a strategy the account can't support is one of the most common failure modes; we want it caught loudly.
- The split between `src/queries/` (read functions) and `src/commands/` (write functions), with the CLI as a thin wrapper, is intentional. Don't put logic in the CLI layer.
- Decimal for money. Float for ratios. Don't mix.
- When in doubt, halt and log. Better to have a stopped bot than a confused bot.
- Run tests as you go. Don't pile up uncommitted, untested code.
- Update `docs/decisions.md` if you make a non-trivial decision while implementing.
