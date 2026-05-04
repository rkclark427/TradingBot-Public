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

### 1. Project setup ✅
- [x] `pyproject.toml` with dependencies
- [x] `.env.example` documenting required env vars
- [x] `.gitignore`
- [ ] Pre-commit config with ruff + mypy — deferred to Phase 3 hardening
- [ ] Basic CI (GitHub Actions) — deferred to Phase 3 hardening

### 2. Configuration system ✅
- [x] Pydantic models for `BaseConfig`, `SleeveConfig`, `StrategyParameters` in `src/config/`
- [x] YAML loader (`src/config/loader.py`)
- [x] Environment variable overrides via pydantic-settings
- [x] Tests passing

### 3. Logging infrastructure ✅
- [x] Structured JSON logging (`src/logging_config.py`)
- [x] Required fields and sleeve context helper
- [x] Tests passing

### 4. Database schema ✅
- [x] All 16 SQLAlchemy models in `src/tracking/models.py` including `TZDateTime` for SQLite tz compatibility
- [x] Initial Alembic migration (`0001_initial_schema.py`)
- [x] Migration `0002_add_sleeve_cash.py` adds `current_cash` to sleeves
- [x] Repository classes in `src/tracking/repos/`
- [x] Tests passing (30 tests)

### 5. Alpaca client wrappers ✅
- [x] `src/data/alpaca_client.py` with `PaperClient` and `LiveClient`
- [x] All required methods implemented
- [x] Retry logic with exponential backoff
- [x] Tests passing (35 tests)

### 6. Capability discovery layer ✅
- [x] `src/data/capabilities.py` with `discover_account_capabilities`, `persist_capabilities`, `load_capabilities_from_db`
- [x] Tests passing

### 7. Asset universe ✅
- [x] `src/data/assets.py` with `refresh_asset_universe` and `get_tradable_symbols`
- [x] Tests passing

### 8. Strategy interface ✅
- [x] `src/strategies/base.py` — `Strategy` ABC, `StrategyCapabilities`, `Target`, `Position`, `MarketDataView`
- [x] `src/strategies/buy_and_hold.py` — BuyAndHoldSPY
- [x] Tests passing

### 9. Sleeve manager ✅
- [x] `src/sleeves/manager.py` with all operations
- [x] Capability validation at sleeve creation
- [x] NAV computation, fill attribution, capital events
- [x] Tests passing (26 tests)

### 10. Portfolio layer ✅
- [x] `src/portfolio/sizer.py` — `targets_to_intended_orders` with ROUND_DOWN and min_notional filter
- [x] Tests passing (10 tests)

### 11. Execution layer ✅
- [x] `src/execution/router.py` — `submit_intended_orders` (with netting path) + `poll_fills` (idempotent)
- [x] Tests passing (9 tests)

### 12. Risk layer ✅
- [x] `src/risk/checks.py` — kill switch, symbol tradability, order size (101% NAV threshold)
- [x] Tests passing (11 tests)
- [x] Decision recorded: threshold is 101% (not 100%) to accommodate the 10bps limit price offset

### 13. Orchestrator ✅
- [x] `src/orchestrator.py` — main loop, kill switch, market hours, per-sleeve cycle, fill polling, heartbeat
- [x] Startup reconciliation (logs position divergences)
- [x] Tests passing (8 tests)

### 14. CLI ✅
- [x] `src/cli/bot_ctl.py` — `status`, `sleeves list/show/create/stop`, `kill`, `resume`, `run`, `refresh-universe`, `refresh-capabilities`
- [x] Thin wrappers over `src/queries/` and `src/commands/`

### 15. End-to-end smoke test
- [ ] Smoke test procedure documented in `docs/smoke-test.md` ✅
- [ ] Manual smoke test run against Alpaca paper — **pending execution**

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
