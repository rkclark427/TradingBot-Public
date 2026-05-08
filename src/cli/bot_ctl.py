"""bot-ctl — command-line control surface for the trading bot.

Every command is a thin wrapper over a function in src/queries/ or src/commands/.
No business logic lives here.

Usage:
  bot-ctl status
  bot-ctl sleeves list
  bot-ctl sleeves show <id>
  bot-ctl sleeves create --strategy <name> --mode paper --capital 100
  bot-ctl sleeves stop <id>
  bot-ctl kill
  bot-ctl resume
  bot-ctl run
  bot-ctl refresh-universe
  bot-ctl refresh-capabilities
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Generator

import typer
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

app = typer.Typer(help="Trading bot control surface.", no_args_is_help=True)
sleeves_app = typer.Typer(help="Sleeve management commands.", no_args_is_help=True)
app.add_typer(sleeves_app, name="sleeves")
backtest_app = typer.Typer(help="Backtest commands.", no_args_is_help=True)
app.add_typer(backtest_app, name="backtest")


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------


@contextmanager
def _app_context(*, need_client: bool = True) -> Generator:
    """Yield (config, session, paper_client, live_client, sleeve_manager)."""
    from src.config.loader import load_config
    from src.config.models import BaseConfig
    from src.data.alpaca_client import PaperClient, LiveClient
    from src.data.capabilities import discover_account_capabilities, load_capabilities_from_db
    from src.sleeves.manager import SleeveManager
    from datetime import date

    config_dir = Path("config")
    if not config_dir.exists():
        typer.echo("Error: config/ directory not found. Run from the project root.", err=True)
        raise typer.Exit(1)

    config = load_config(config_dir)
    db_url = f"sqlite:///{config.secrets.db_path}"

    # Use Alembic migrations to bootstrap/upgrade the schema.
    # create_all is intentionally not used here — keep it in test fixtures only.
    alembic_ini = Path("alembic.ini")
    if not alembic_ini.exists():
        typer.echo("Error: alembic.ini not found. Run from the project root.", err=True)
        raise typer.Exit(1)
    try:
        from alembic.config import Config as AlembicConfig
        from alembic import command as alembic_command

        alembic_cfg = AlembicConfig(str(alembic_ini))
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)
        alembic_command.upgrade(alembic_cfg, "head")
    except Exception as exc:
        typer.echo(f"Error running database migrations: {exc}", err=True)
        raise typer.Exit(1)

    engine = create_engine(db_url)

    paper_client: PaperClient | None = None
    live_client: LiveClient | None = None

    if need_client:
        if config.secrets.alpaca_paper_api_key:
            paper_client = PaperClient(
                api_key=config.secrets.alpaca_paper_api_key,
                api_secret=config.secrets.alpaca_paper_api_secret,
            )
        if config.secrets.alpaca_live_api_key:
            live_client = LiveClient(
                api_key=config.secrets.alpaca_live_api_key,
                api_secret=config.secrets.alpaca_live_api_secret,
            )

    with Session(engine) as session:
        primary_client = paper_client or live_client
        if need_client and primary_client is None:
            typer.echo("Error: no Alpaca API keys configured in .env.", err=True)
            raise typer.Exit(1)

        # Load or discover account capabilities for SleeveManager
        caps = None
        if primary_client is not None:
            caps = load_capabilities_from_db(session, date.today())
            if caps is None:
                try:
                    caps = discover_account_capabilities(primary_client)
                except Exception as exc:
                    typer.echo(f"Warning: could not fetch account capabilities: {exc}", err=True)

        if caps is None:
            # Fallback: construct minimal caps so the CLI can at least read data
            from src.sleeves.types import AccountCapabilities
            from datetime import datetime, timezone

            caps = AccountCapabilities(
                asset_classes=frozenset({"us_equity"}),
                options_trading_level=0,
                fractional_shares_enabled=True,
                shorting_enabled=True,
                is_pdt=False,
                buying_power=Decimal("0"),
                cash=Decimal("0"),
                as_of=datetime.now(tz=timezone.utc),
            )

        manager = SleeveManager(
            session=session,
            account_capabilities=caps,
        )
        yield config, session, paper_client, live_client, manager


# ---------------------------------------------------------------------------
# bot-ctl status
# ---------------------------------------------------------------------------


@app.command()
def status() -> None:
    """Show bot health, account state, and kill switch status."""
    with _app_context() as (config, session, paper_client, live_client, manager):
        from src.queries.status import get_status

        info = get_status(session, paper_client or live_client, manager)

        ks = "[ACTIVE]" if info["kill_switch_active"] else "inactive"
        typer.echo(f"Kill switch:   {ks}")
        typer.echo(f"Last heartbeat: {info['last_heartbeat'] or 'never'}")

        counts = info["sleeve_counts"]
        typer.echo(
            f"Sleeves: {counts['running']} running / {counts['paused']} paused / "
            f"{counts['stopping']} stopping / {counts['stopped']} stopped"
        )

        acct = info.get("account")
        if acct:
            if "error" in acct:
                typer.echo(f"Account: {acct['error']}")
            else:
                typer.echo(f"Account buying power: ${acct['buying_power']}")
                typer.echo(f"Account cash:         ${acct['cash']}")
                typer.echo(f"Portfolio value:      ${acct['portfolio_value']}")
                if acct["trading_blocked"]:
                    typer.echo("WARNING: account trading is blocked", err=True)


# ---------------------------------------------------------------------------
# bot-ctl kill / resume
# ---------------------------------------------------------------------------


@app.command()
def kill(
    cancel_open: bool = typer.Option(
        False, "--cancel-open",
        help="Cancel all open Alpaca orders before draining in-flight fills.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Immediate exit with no cleanup. Last resort — state may be inconsistent.",
    ),
) -> None:
    """Activate the kill switch. Default: graceful halt (drain in-flight fills then exit)."""
    from src.commands.kill_switch import activate

    if force:
        activate("force")
        typer.echo(
            "Kill switch activated (FORCE). Process exits on next cycle check.",
            err=True,
        )
        typer.echo("WARNING: in-flight orders will NOT be reconciled.", err=True)
    elif cancel_open:
        activate("cancel_open")
        typer.echo(
            "Kill switch activated (cancel-open). Open Alpaca orders will be cancelled "
            "before draining fills and exiting."
        )
    else:
        activate("graceful")
        typer.echo(
            "Kill switch activated (graceful). In-flight orders will complete, "
            "then the bot will exit cleanly."
        )


@app.command()
def resume() -> None:
    """Deactivate the kill switch — allows the orchestrator to resume."""
    from src.commands.kill_switch import deactivate

    deactivate()
    typer.echo("Kill switch deactivated. The bot will resume submitting orders.")


# ---------------------------------------------------------------------------
# bot-ctl sleeves list
# ---------------------------------------------------------------------------


@sleeves_app.command("list")
def sleeves_list(
    status_filter: str | None = typer.Option(None, "--status", help="Filter by status (running, paused, stopped)"),
) -> None:
    """List all sleeves."""
    with _app_context() as (config, session, paper_client, live_client, manager):
        from src.queries.sleeves import list_sleeves

        sleeves = list_sleeves(manager, status_filter=status_filter)
        if not sleeves:
            typer.echo("No sleeves found.")
            return

        typer.echo(f"{'ID':<38} {'STRATEGY':<20} {'MODE':<8} {'STATUS':<10} {'NAV':>12} {'START':>12}")
        typer.echo("-" * 110)
        for s in sleeves:
            typer.echo(
                f"{str(s.id):<38} {s.strategy_name:<20} {s.mode.value:<8} {s.status.value:<10} "
                f"${s.current_nav:>11.2f} ${s.starting_capital:>11.2f}"
            )


# ---------------------------------------------------------------------------
# bot-ctl sleeves show <id>
# ---------------------------------------------------------------------------


@sleeves_app.command("show")
def sleeves_show(sleeve_id: str = typer.Argument(..., help="Sleeve UUID")) -> None:
    """Show detailed state for a single sleeve."""
    with _app_context() as (config, session, paper_client, live_client, manager):
        from src.queries.sleeves import get_sleeve, get_sleeve_capital_events, get_sleeve_positions

        sleeve = get_sleeve(manager, sleeve_id)
        if sleeve is None:
            typer.echo(f"Sleeve {sleeve_id!r} not found.", err=True)
            raise typer.Exit(1)

        typer.echo(f"ID:             {sleeve.id}")
        typer.echo(f"Strategy:       {sleeve.strategy_name}")
        typer.echo(f"Mode:           {sleeve.mode.value}")
        typer.echo(f"Status:         {sleeve.status.value}")
        typer.echo(f"Starting capital: ${sleeve.starting_capital:.2f}")
        typer.echo(f"Current NAV:    ${sleeve.current_nav:.2f}")
        typer.echo(f"High-water mark: ${sleeve.high_water_mark:.2f}")
        typer.echo(f"Created:        {sleeve.created_at}")
        if sleeve.parameters:
            typer.echo(f"Parameters:     {sleeve.parameters}")

        positions = get_sleeve_positions(session, str(sleeve.id))
        if positions:
            typer.echo("\nPositions:")
            for p in positions:
                typer.echo(f"  {p['symbol']}: qty={p['qty']}  avg_cost=${p['avg_cost']}")
        else:
            typer.echo("\nPositions: none")

        events = get_sleeve_capital_events(session, str(sleeve.id))
        if events:
            typer.echo("\nRecent capital events:")
            for e in events[:5]:
                typer.echo(f"  {e['timestamp'][:19]}  {e['event_type']:12} ${e['amount']}  {e['reason'] or ''}")


# ---------------------------------------------------------------------------
# bot-ctl sleeves create
# ---------------------------------------------------------------------------


@sleeves_app.command("create")
def sleeves_create(
    strategy: str = typer.Option(..., "--strategy", help="Strategy name (e.g. buy_and_hold)"),
    mode: str = typer.Option(..., "--mode", help="Mode: paper or live"),
    capital: float = typer.Option(..., "--capital", help="Starting capital in dollars"),
    symbol: str | None = typer.Option(None, "--symbol", help="Symbol override (e.g. SPY)"),
    managed: bool = typer.Option(False, "--managed", help="If true, YAML is canonical source"),
) -> None:
    """Create a new sleeve."""
    with _app_context() as (config, session, paper_client, live_client, manager):
        from src.commands.sleeves import create_sleeve
        from src.sleeves.types import CapabilityMismatchError

        params: dict = {}
        if symbol:
            params["symbol"] = symbol

        try:
            sleeve = create_sleeve(
                manager,
                strategy=strategy,
                mode=mode,
                capital=Decimal(str(capital)),
                params=params,
                managed=managed,
            )
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)
        except CapabilityMismatchError as exc:
            typer.echo(f"Capability mismatch:\n{exc}", err=True)
            raise typer.Exit(1)

        typer.echo(f"Created sleeve {sleeve.id}")
        typer.echo(f"  strategy={sleeve.strategy_name}  mode={sleeve.mode.value}  "
                   f"status={sleeve.status.value}  nav=${sleeve.current_nav:.2f}")


# ---------------------------------------------------------------------------
# bot-ctl sleeves stop
# ---------------------------------------------------------------------------


@sleeves_app.command("stop")
def sleeves_stop(sleeve_id: str = typer.Argument(..., help="Sleeve UUID")) -> None:
    """Transition a sleeve to STOPPING status."""
    with _app_context() as (config, session, paper_client, live_client, manager):
        from src.commands.sleeves import stop_sleeve
        from src.sleeves.manager import InvalidTransitionError, SleeveNotFoundError

        try:
            sleeve = stop_sleeve(manager, sleeve_id)
        except SleeveNotFoundError:
            typer.echo(f"Sleeve {sleeve_id!r} not found.", err=True)
            raise typer.Exit(1)
        except InvalidTransitionError as exc:
            typer.echo(f"Cannot stop sleeve: {exc}", err=True)
            raise typer.Exit(1)

        typer.echo(f"Sleeve {sleeve.id} is now {sleeve.status.value}.")


# ---------------------------------------------------------------------------
# bot-ctl run
# ---------------------------------------------------------------------------


@app.command()
def run(
    force: bool = typer.Option(
        False, "--force", help="Bypass market-hours check (useful for testing outside hours)."
    ),
    once: bool = typer.Option(
        False, "--once", help="Run a single cycle then exit instead of looping."
    ),
) -> None:
    """Start the orchestrator loop (blocks until Ctrl-C)."""
    with _app_context() as (config, session, paper_client, live_client, manager):
        from src.orchestrator import Orchestrator

        orch = Orchestrator(
            config=config,
            session=session,
            paper_client=paper_client,
            live_client=live_client,
            sleeve_manager=manager,
        )
        if once:
            result = orch.run_once(force=force)
            typer.echo(f"Cycle complete: {result}")
        else:
            if force:
                typer.echo("WARNING: market-hours check bypassed (--force)", err=True)
            orch.run(force=force)
            typer.echo("Orchestrator stopped.")


# ---------------------------------------------------------------------------
# bot-ctl refresh-universe
# ---------------------------------------------------------------------------


@app.command("refresh-universe")
def refresh_universe() -> None:
    """Pull the full asset list from Alpaca and update the local universe cache."""
    with _app_context() as (config, session, paper_client, live_client, manager):
        from src.data.assets import refresh_asset_universe

        client = paper_client or live_client
        if client is None:
            typer.echo("No Alpaca client available.", err=True)
            raise typer.Exit(1)

        typer.echo("Fetching asset universe from Alpaca…")
        count = refresh_asset_universe(client, session)
        typer.echo(f"Done. {count} assets upserted.")


# ---------------------------------------------------------------------------
# bot-ctl refresh-capabilities
# ---------------------------------------------------------------------------


@app.command("refresh-capabilities")
def refresh_capabilities() -> None:
    """Fetch and persist current account capabilities from Alpaca."""
    with _app_context() as (config, session, paper_client, live_client, manager):
        from src.data.capabilities import discover_account_capabilities, persist_capabilities
        from datetime import date

        client = paper_client or live_client
        if client is None:
            typer.echo("No Alpaca client available.", err=True)
            raise typer.Exit(1)

        typer.echo("Fetching account capabilities from Alpaca…")
        caps = discover_account_capabilities(client)
        persist_capabilities(caps, session, date.today())
        typer.echo("Done.")
        typer.echo(f"  fractional_shares: {caps.fractional_shares_enabled}")
        typer.echo(f"  shorting_enabled:  {caps.shorting_enabled}")
        typer.echo(f"  buying_power:      ${caps.buying_power:.2f}")
        typer.echo(f"  cash:              ${caps.cash:.2f}")


# ---------------------------------------------------------------------------
# bot-ctl db-status
# ---------------------------------------------------------------------------


@app.command("db-status")
def db_status() -> None:
    """Show current Alembic migration revision and whether the DB is up to date."""
    from alembic.config import Config as AlembicConfig
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from src.config.loader import load_config

    alembic_ini = Path("alembic.ini")
    if not alembic_ini.exists():
        typer.echo("Error: alembic.ini not found. Run from the project root.", err=True)
        raise typer.Exit(1)

    config_dir = Path("config")
    if not config_dir.exists():
        typer.echo("Error: config/ directory not found. Run from the project root.", err=True)
        raise typer.Exit(1)

    config = load_config(config_dir)
    db_url = f"sqlite:///{config.secrets.db_path}"

    alembic_cfg = AlembicConfig(str(alembic_ini))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    script = ScriptDirectory.from_config(alembic_cfg)
    head = script.get_current_head()

    engine = create_engine(db_url)
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        current = context.get_current_revision()

    up_to_date = current == head
    typer.echo(f"Current revision: {current or '(none — DB not initialised)'}")
    typer.echo(f"Head revision:    {head or '(none)'}")
    typer.echo(f"Up to date:       {'yes' if up_to_date else 'NO — run bot-ctl (any command) to apply pending migrations'}")
    if not up_to_date:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# bot-ctl backtest run / refresh-data / list / cache-status
# ---------------------------------------------------------------------------


@backtest_app.command("run")
def backtest_run(
    config_path: Path = typer.Argument(..., help="Path to backtest config YAML"),
) -> None:
    """Run a backtest from a config file and generate an HTML report."""
    import yaml

    from src.backtest.config import BacktestConfig, UNIVERSE_MAP, instantiate_strategy
    from src.backtest.data import BacktestDataLayer
    from src.backtest.engine import run_simulation
    from src.backtest.reporting import generate_report

    if not config_path.exists():
        typer.echo(f"Config file not found: {config_path}", err=True)
        raise typer.Exit(1)

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    try:
        cfg = BacktestConfig.model_validate(raw)
    except Exception as exc:
        typer.echo(f"Invalid config: {exc}", err=True)
        raise typer.Exit(1)

    data_layer = BacktestDataLayer(cfg.cache_db)
    sim = cfg.simulation

    # Resolve null end_date to latest cached date
    end_date = sim.end_date
    if end_date is None:
        end_date = data_layer.get_latest_trade_date()
        if end_date is None:
            typer.echo(
                "Cache is empty — run `bot-ctl backtest refresh-data --all` first.", err=True
            )
            raise typer.Exit(1)
        typer.echo(f"end_date resolved to latest cached date: {end_date}")

    # Strategy
    try:
        strategy = instantiate_strategy(cfg.strategy.class_name)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    universe = UNIVERSE_MAP.get(cfg.strategy.class_name, ["SPY"])
    typer.echo(
        f"Running {cfg.strategy.class_name}  {sim.start_date} → {end_date}"
        f"  capital=${float(sim.starting_capital):,.0f}"
        f"  slippage={sim.slippage_bps}bps"
    )

    result = run_simulation(
        strategy=strategy,
        params=cfg.strategy.parameters,
        universe=universe,
        data_layer=data_layer,
        start_date=sim.start_date,
        end_date=end_date,
        starting_capital=sim.starting_capital,
        slippage_bps=sim.slippage_bps,
        cash_annualized_rate=sim.cash_annualized_rate,
    )
    final_nav = float(result.snapshots[-1].nav) if result.snapshots else 0.0
    typer.echo(
        f"  → {len(result.snapshots)} days  {len(result.trades)} trades  "
        f"final NAV=${final_nav:,.2f}"
    )

    # Benchmarks
    benchmark_results = []
    for bench_name in cfg.benchmarks:
        typer.echo(f"Running benchmark: {bench_name}")
        try:
            bench_strategy = instantiate_strategy(bench_name)
        except ValueError as exc:
            typer.echo(f"  Warning: skipping {bench_name}: {exc}", err=True)
            continue
        bench_universe = UNIVERSE_MAP.get(bench_name, ["SPY"])
        bench_result = run_simulation(
            strategy=bench_strategy,
            params={},
            universe=bench_universe,
            data_layer=data_layer,
            start_date=sim.start_date,
            end_date=end_date,
            starting_capital=sim.starting_capital,
            slippage_bps=sim.slippage_bps,
            cash_annualized_rate=sim.cash_annualized_rate,
        )
        bench_nav = float(bench_result.snapshots[-1].nav) if bench_result.snapshots else 0.0
        typer.echo(f"  → final NAV=${bench_nav:,.2f}")
        benchmark_results.append(bench_result)

    # Report
    output_dir = cfg.output.directory
    typer.echo(f"Generating report → {output_dir}")
    report_path = generate_report(
        strategy_result=result,
        benchmark_results=benchmark_results,
        output_dir=output_dir,
        config=raw,
    )
    typer.echo(f"Done. Report: {report_path}")


@backtest_app.command("refresh-data")
def backtest_refresh_data(
    symbol: list[str] = typer.Option([], "--symbol", "-s", help="Symbol(s) to refresh (repeatable)"),
    all_: bool = typer.Option(False, "--all", help="Refresh the full 16-ETF default universe"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing cache rows"),
    cache_db: Path = typer.Option(Path("data/backtest_cache.db"), "--cache-db"),
) -> None:
    """Fetch historical price data from yfinance into the local cache."""
    from datetime import date as _date

    from src.backtest.data import BacktestDataLayer

    if not all_ and not symbol:
        typer.echo("Specify at least one --symbol SYMBOL or use --all.", err=True)
        raise typer.Exit(1)

    if all_:
        from src.backtest.config import UNIVERSE_MAP
        all_syms: set[str] = set()
        for syms in UNIVERSE_MAP.values():
            all_syms.update(syms)
        symbols = sorted(all_syms)
    else:
        symbols = list(symbol)

    typer.echo(f"Refreshing {len(symbols)} symbol(s) into {cache_db} …")
    layer = BacktestDataLayer(cache_db)
    results = layer.refresh_all(symbols, start_date=_date(2010, 1, 1), force=force)

    ok = sum(1 for n in results.values() if n > 0)
    total = sum(results.values())
    for sym, n in sorted(results.items()):
        status = f"{n:>6} rows" if n > 0 else "  (no new rows)"
        typer.echo(f"  {sym:<6} {status}")

    typer.echo(f"Done. {ok}/{len(symbols)} symbols updated, {total} total rows written.")


@backtest_app.command("list")
def backtest_list(
    directory: Path = typer.Option(
        Path("backtests"), "--dir", help="Directory to scan for backtest runs"
    ),
) -> None:
    """List backtest runs found in the output directory."""
    import json

    if not directory.exists():
        typer.echo(f"No backtests directory at {directory}. Run a backtest first.")
        return

    runs: list[tuple[Path, dict]] = []
    for meta_path in sorted(directory.glob("*/metadata.json"), reverse=True):
        try:
            runs.append((meta_path.parent, json.loads(meta_path.read_text(encoding="utf-8"))))
        except Exception:
            pass

    if not runs:
        typer.echo(f"No backtest runs found in {directory}.")
        return

    typer.echo(f"{'Run directory':<36} {'Strategy':<24} {'Period':<24} {'Run timestamp'}")
    typer.echo("-" * 110)
    for run_dir, meta in runs:
        period = f"{meta.get('start_date', '?')} → {meta.get('end_date', '?')}"
        ts = (meta.get("run_timestamp") or "?")[:19].replace("T", " ")
        name = meta.get("strategy_name", "?")
        typer.echo(f"{str(run_dir.name):<36} {name:<24} {period:<24} {ts}")


@backtest_app.command("cache-status")
def backtest_cache_status(
    cache_db: Path = typer.Option(Path("data/backtest_cache.db"), "--cache-db"),
) -> None:
    """Show freshness of cached price data for each symbol."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from src.backtest.cache_models import PriceBarRow
    from src.backtest.data import BacktestDataLayer

    if not cache_db.exists():
        typer.echo(f"Cache not found at {cache_db}.")
        typer.echo("Run `bot-ctl backtest refresh-data --all` to populate it.")
        return

    layer = BacktestDataLayer(cache_db)
    cache_v = layer.get_cache_version()
    latest_date = layer.get_latest_trade_date()

    typer.echo(f"Cache:         {cache_db}")
    typer.echo(f"Last fetched:  {cache_v.strftime('%Y-%m-%d %H:%M UTC') if cache_v else '(empty)'}")
    typer.echo(f"Latest date:   {latest_date or '—'}")
    typer.echo()

    with Session(layer._engine) as session:
        rows = session.execute(
            select(
                PriceBarRow.symbol,
                func.count().label("n_rows"),
                func.min(PriceBarRow.trade_date).label("first_date"),
                func.max(PriceBarRow.trade_date).label("last_date"),
            )
            .group_by(PriceBarRow.symbol)
            .order_by(PriceBarRow.symbol)
        ).all()

    if not rows:
        typer.echo("Cache is empty.")
        return

    typer.echo(f"{'Symbol':<8} {'Rows':>6}  {'First date':<12}  {'Last date'}")
    typer.echo("-" * 44)
    for row in rows:
        typer.echo(
            f"{row.symbol:<8} {row.n_rows:>6}  {str(row.first_date):<12}  {row.last_date}"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app()


if __name__ == "__main__":
    main()
