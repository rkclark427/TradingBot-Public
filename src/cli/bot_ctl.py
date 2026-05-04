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
    from src.tracking.models import Base
    from datetime import date

    config_dir = Path("config")
    if not config_dir.exists():
        typer.echo("Error: config/ directory not found. Run from the project root.", err=True)
        raise typer.Exit(1)

    config = load_config(config_dir)
    db_url = f"sqlite:///{config.secrets.db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

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
def kill() -> None:
    """Activate the kill switch — halts order submission on the next cycle."""
    from src.commands.kill_switch import activate

    activate()
    typer.echo("Kill switch activated. The bot will stop submitting orders.")


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
def run() -> None:
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
        orch.run()


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
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app()


if __name__ == "__main__":
    main()
