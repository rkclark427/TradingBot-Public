# Claude Code Primer

This file is read by Claude Code at the start of every session. It captures the architectural commitments, conventions, and current build state. **Treat what's here as binding decisions made in design conversations elsewhere.** If you think something here is wrong, ask before changing it.

## What this project is

A multi-strategy algorithmic trading platform executing equities trades through Alpaca. The central abstraction is the **sleeve**: a runtime instance of a strategy with its own mode (paper/live), capital allocation, parameters, and state. Multiple sleeves run concurrently. The platform handles capital allocation, order netting, attribution, and risk control across them.

For full architecture, see `docs/architecture.md`. For decision history, see `docs/decisions.md`. For current phase, see `docs/phases/`.

## The load-bearing abstractions

These are the contracts everything else depends on. Don't change them without explicit discussion.

### Strategy
A class in `src/strategies/` implementing the `Strategy` ABC. Pure logic: given inputs (universe, market data, current sleeve positions, sleeve NAV, parameters), returns target positions as weights. A strategy does not know its mode, does not know its capital allocation, does not know about other strategies, does not place orders. Stateless across runs except where explicitly persisted.

```python
class Strategy(ABC):
    name: str
    capabilities: StrategyCapabilities  # static declaration

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
    ) -> list[Target]: ...
```

Strategies return targets as weights (`target_weight: float`, fraction of NAV). The portfolio layer translates weights to share quantities. **Strategies return the FULL desired portfolio, not deltas.** A no-op strategy returns its current positions as targets.

### Sleeve
A runtime instance of a strategy. Owns its NAV, which compounds with its own P&L. Has a status (running/paused/stopping/stopped), a mode (paper/live), parameters, and per-sleeve risk limits. The Sleeve Manager (`src/sleeves/`) handles lifecycle.

### Capability layer
Three-tier validation, all enforced before any order goes out:
1. **Account capabilities** — what Alpaca account can do (asset classes, options level, fractional shares, shorting, PDT status). Discovered on startup, refreshed daily.
2. **Strategy capability declarations** — static metadata on each strategy class. Validated against account at sleeve creation.
3. **Per-symbol checks** — symbol tradability, halts, restrictions. Checked at order submission.

### Modes
Two modes, mapped to Alpaca's two account environments:
- `live` — Alpaca live account, real money
- `paper` — Alpaca paper account, real market data, simulated fills, no money

In v1 there is no internal simulator. Paper sleeves trade against Alpaca's paper environment.

### Order netting
Within an environment, before submission to Alpaca: group intended orders by symbol, net buys against sells, submit one net order. Allocate fills back to constituent sleeves proportionally. Slippage savings distributed pro-rata. Database records both intended orders and net orders, plus the mapping table, so attribution is always recoverable.

## Conventions

### Code style
- Python 3.11+
- Type hints required on all public functions
- `dataclasses` for value objects, regular classes for stateful things
- `attrs` is fine if there's a reason; `pydantic` for config parsing
- f-strings, not `.format()`, not `%`
- Pathlib, not `os.path`
- Logging via the standard `logging` module, configured to emit JSON

### Time
- All bot logic uses **explicit timezone-aware datetimes**
- Internal canonical timezone is **US/Eastern** (market hours)
- VM clock is UTC; conversion happens at the edges
- Never use `datetime.now()` without a timezone — use `datetime.now(ZoneInfo("America/New_York"))`
- This is the #1 source of subtle bugs in trading bots; we are paranoid about it

### Money
- Use `decimal.Decimal` for cash amounts, NAVs, prices when precision matters
- Floats are OK for percentages, ratios, signal strengths
- Always round share quantities according to Alpaca's rules (fractional shares to 6 decimals; whole shares for symbols that don't support fractional)

### Errors
- Trading actions must fail closed: any uncertainty halts the operation rather than guessing
- Distinguish between **expected operational errors** (order rejected, API rate limited) and **unexpected errors** (programming bugs)
- Expected errors: log + continue + accumulate metrics; if rate is too high, halt
- Unexpected errors: log + halt + alert

### Configuration
- YAML files in `config/`, parsed via Pydantic models for validation
- Secrets via environment variables (loaded from `.env` in dev, systemd Environment= in prod)
- `config/sleeves.yaml` is the sleeve registry (see Section 8.2 of architecture doc for semantics — we use Option C: hybrid model with `managed: true|false` flag per sleeve)
- Strategy default parameters in `config/strategies/<strategy_name>.yaml`

### Database
- SQLite, with **Alembic for migrations**
- All schema changes go through migrations; no ad-hoc ALTER TABLE
- Schema is in `src/tracking/models.py` (SQLAlchemy)
- Reads via SQLAlchemy ORM; writes through dedicated repository classes (`src/tracking/repos/`)
- Don't put business logic in the ORM models; they're dumb data containers

### Testing
- pytest, in `tests/`
- Unit tests for: strategy logic, portfolio math, risk checks, order netting math
- Integration tests for: full sleeve lifecycle in paper environment with mocked Alpaca, database operations
- Don't test against the real Alpaca API in CI; use VCR.py or mocks
- Hand-test against paper before any go-live

### CLI
- `bot-ctl` is the canonical control surface in v1 (no web dashboard yet)
- Implementation: Click or Typer (slight preference for Typer for type-hinted ergonomics)
- Commands listed in architecture doc Section 4.10
- **Important**: every CLI command is also exposed as a callable Python function in `src/queries/` (read) or `src/commands/` (write). The CLI is just a thin wrapper. This makes future OpenClaw integration trivial.

### Logging
- Structured JSON, one event per line
- Required fields: `timestamp`, `level`, `event`, `module`. Plus event-specific context.
- Sleeve-related events include `sleeve_id`
- Order-related events include `order_id`, `symbol`, `side`
- Risk events include `risk_event_type`, `severity`
- Logs go to `/var/log/trading-bot/` in prod, `./logs/` in dev
- Rotated daily, kept 90 days

## What lives where

```
trading-bot/
├── pyproject.toml
├── README.md           # public-facing
├── CLAUDE.md           # this file
├── .env.example
├── config/
│   ├── base.yaml
│   ├── sleeves.yaml
│   └── strategies/
├── src/
│   ├── data/           # Alpaca data, universe, asset list, capability discovery
│   ├── strategies/     # strategy library (one file per strategy)
│   │   └── base.py     # Strategy ABC + Target/StrategyCapabilities dataclasses
│   ├── sleeves/        # sleeve lifecycle, NAV, capital movement
│   ├── portfolio/      # weight-to-quantity translation, position sizing
│   ├── risk/           # account-level + sleeve-level risk controls
│   ├── execution/      # Alpaca clients, order netting, fill handling
│   ├── tracking/       # database, models, repositories
│   ├── monitoring/     # heartbeat, alerts, daily summary
│   ├── backtest/       # historical simulation
│   ├── queries/        # read-only query functions (CLI + future OpenClaw)
│   ├── commands/       # write/action functions (CLI + future OpenClaw)
│   ├── cli/            # bot-ctl Typer app
│   └── orchestrator.py # main loop entry point
├── scripts/
├── tests/
├── data/               # local cache (gitignored)
└── logs/               # application logs (gitignored)
```

## Current phase

**Phase 1: Foundation & Sleeve Architecture**

See `docs/phases/phase-1.md` for the task list and acceptance criteria. Don't work on Phase 2+ tasks until Phase 1 is complete and the gate is passed.

The Phase 1 deliverable is: a working sleeve manager that can create a paper BuyAndHoldSPY sleeve with $100, watch it submit a single buy order through the engine to Alpaca paper, see the fill come back, and compute correct NAV as SPY moves. Capability discovery surfaces account state correctly.

## What to build vs. what to ask first

**Build without asking:**
- Anything specified in `docs/architecture.md` or `docs/phases/phase-1.md`
- Standard glue (logging setup, config parsing, Pydantic models for declared schemas)
- Tests for things you've built
- Documentation updates that reflect what you built

**Ask first:**
- Anything that changes one of the load-bearing abstractions above
- New strategies not on the launch list (MeanReversion, BuyAndHoldSPY, SectorRotation)
- New external dependencies beyond what's in `pyproject.toml`
- Risk control changes
- Anything that touches money flow logic (capital movement, NAV calculation, position attribution)

## What to never do

- Never use market orders. Limits only.
- Never use `datetime.now()` without a timezone.
- Never enter sensitive values (API keys, account numbers) into source files. Environment only.
- Never write code that bypasses the risk layer. Every order goes through risk checks.
- Never let a strategy directly call Alpaca. Strategies produce targets; only execution layer talks to brokers.
- Never silently catch exceptions in trading paths. Log them, halt if uncertain, surface them.
- Never let a backtest and live execution diverge in their use of strategy logic. Same Strategy class, same parameters, same logic — only the data source and execution venue differ.

## Open design questions

These are unresolved as of the latest design conversation. If you encounter them while building, ask before deciding.

- Historical data source for backtests (Alpaca + yfinance hybrid leaning, but not finalized)
- Earnings calendar source (Finnhub free tier likely)
- Backup destination for SQLite (Azure Blob most likely)
- First live sleeve choice (BuyAndHoldSPY at $100 first, then add MeanReversion at $500 — this is the current intent)

## When you finish a task

1. Run the tests
2. Update `docs/decisions.md` if you made a decision worth recording
3. Update `docs/phases/phase-1.md` to mark the task complete
4. Commit with a message that says what changed and why
5. If you hit a question that needed asking, surface it in the chat — don't paper over it

## Working with Kent

- Kent has strong financial literacy; don't over-explain basics
- Surface tradeoffs and recommend; don't hand-wave
- Be honest about uncertainty — "I don't know" is acceptable, "I think but haven't verified" is acceptable
- Provide ongoing updates as you work; don't disappear into a long task and emerge with a wall of code
- Push back if you disagree; he won't take offense and will engage substantively
