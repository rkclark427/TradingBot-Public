# TradingBot

An algorithmic trading platform built around a **multi-strategy sleeve architecture**, executing trades through Alpaca. Designed to host a library of pluggable strategies, each running as an isolated sleeve with its own capital allocation, mode (paper or live), and parameters.

This is a personal project. Not investment advice, not an offering, not seeking outside capital. Source is public for transparency and as a reference implementation; do not deploy with real money without understanding what it does.

## What this is

A bot that:

- Hosts multiple trading strategies simultaneously (e.g., $500 on a mean-reversion strategy live, $500 on sector rotation paper, $100 on buy-and-hold SPY as a benchmark)
- Tracks each "sleeve" (a runtime instance of a strategy) as a self-contained unit with its own NAV that compounds with its own performance
- Nets orders within each environment so multiple sleeves wanting opposite trades on the same symbol don't fight each other
- Enforces two-tier risk controls (account-level master safety, sleeve-level per-strategy discipline)
- Validates strategy requirements against actual Alpaca account capabilities at sleeve creation time
- Runs as a systemd service on a Linux VM with CLI-based control (`bot-ctl`) and Pushover alerts

## What this is not

- Not a high-frequency trading system. Daily-cycle decisions, limit orders only, multi-day holding periods.
- Not a discretionary trading platform. Strategies operate autonomously within configured rules.
- Not multi-broker or multi-account. Single Alpaca account, paper environment + live environment.
- Not a service for others. Personal use, single user.

## Status

Active development. Phase 1 (foundation + sleeve architecture) in progress. See `docs/phases/` for the build sequence.

## Documentation

- [Architecture](docs/architecture.md) — full system design, components, data model, build sequence

## Quick start (eventual)

Once Phase 1 is complete, bootstrapping a development environment will look something like:

```bash
git clone https://github.com/rkclark427/TradingBot.git
cd TradingBot
uv venv && source .venv/bin/activate
uv pip install -e .
cp .env.example .env  # add Alpaca paper API keys
python -m src.cli.bot_ctl status
```

This section will become accurate as Phase 1 lands.

## Design principles

1. **Fail safe, not fail open.** Any uncertainty halts trading rather than continuing on assumptions.
2. **Observability over cleverness.** Every signal, decision, and order is logged with full context.
3. **Hard limits as code, not policy.** Risk controls are enforced by the system.
4. **Modular strategy interface.** Strategies are pluggable; adding one is a single-file change.
5. **Backtest-execution parity.** Backtests use the same Strategy class as live execution.

## Tech

- Python 3.11+
- Alpaca Trading API (paper and live)
- SQLite + Alembic for state and history
- systemd for service management
- Pushover for critical alerts
- Eventually: OpenClaw for conversational ops (deferred)

## License

MIT, but see disclaimer above re: deployment.
