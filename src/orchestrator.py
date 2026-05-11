"""Orchestrator — main bot loop.

Each cycle:
  1. Kill switch check (mode-aware: graceful / cancel_open / force)
  2. Market hours check
  3. For every running sleeve: generate targets → size → risk filter → submit
  4. Poll fills for both environments
  5. Write heartbeat
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from src.config.models import BaseConfig
from src.data.alpaca_client import LiveClient, PaperClient
from src.execution.router import poll_fills, submit_intended_orders
from src.portfolio.sizer import targets_to_intended_orders
from src.queries.kill_switch import get_kill_switch_mode, is_kill_switch_active
from src.risk.checks import check_and_filter
from src.sleeves.manager import SleeveManager
from src.sleeves.types import Mode, Sleeve, SleeveStatus
from src.strategies.base import MarketDataView, Position, Strategy
from src.tracking.models import NetOrderRow
from src.tracking.repos.heartbeat import HeartbeatRepo
from src.tracking.repos.market import AssetUniverseRepo
from src.tracking.repos.positions import PositionRepo
from src.tracking.repos.strategy_state import StrategyStateRepo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MarketDataView adapter
# ---------------------------------------------------------------------------


class AlpacaMarketDataView:
    """Adapts a _BaseClient to the MarketDataView protocol."""

    def __init__(self, client: PaperClient | LiveClient) -> None:
        self._client = client

    def get_bars(self, symbol: str, lookback_days: int, as_of: datetime) -> Any:
        from alpaca.data.timeframe import TimeFrame

        start = as_of - timedelta(days=lookback_days + 7)
        return self._client.get_bars(symbol, TimeFrame.Day, start, as_of)

    def get_latest_price(self, symbol: str) -> Decimal:
        return self._client.get_latest_trade_price(symbol)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    def __init__(
        self,
        config: BaseConfig,
        session: Session,
        paper_client: PaperClient | None,
        live_client: LiveClient | None,
        sleeve_manager: SleeveManager,
        strategy_registry: dict[str, Strategy] | None = None,
    ) -> None:
        self._config = config
        self._session = session
        self._paper_client = paper_client
        self._live_client = live_client
        self._sleeve_manager = sleeve_manager

        if strategy_registry is None:
            from src.strategies.buy_and_hold import BuyAndHoldSPY
            from src.strategies.momentum_continuation import MomentumContinuation
            from src.strategies.short_term_mean_reversion import ShortTermMeanReversion

            self._strategies: dict[str, Strategy] = {
                "buy_and_hold": BuyAndHoldSPY(),
                "momentum_continuation": MomentumContinuation(),
                "short_term_mean_reversion": ShortTermMeanReversion(),
            }
        else:
            self._strategies = strategy_registry

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def run_startup_checks(self) -> None:
        """Reconcile DB positions against Alpaca; log divergences and warn."""
        logger.info("Running startup reconciliation")
        sleeves = self._sleeve_manager.list_sleeves(status_filter="running")
        for sleeve in sleeves:
            try:
                self._reconcile_sleeve(sleeve)
            except Exception:
                logger.exception("Reconciliation failed for sleeve %s", sleeve.id)

    def _reconcile_sleeve(self, sleeve: Sleeve) -> None:
        client = self._client_for(sleeve)
        alpaca_map = {p.symbol: p.qty for p in client.get_positions()}
        db_rows = PositionRepo(self._session).list_by_sleeve(str(sleeve.id))
        for row in db_rows:
            db_qty = Decimal(str(row.qty))
            alpaca_qty = alpaca_map.get(row.symbol, Decimal("0"))
            if db_qty > Decimal("0"):
                divergence = abs(alpaca_qty - db_qty) / db_qty
                if divergence > Decimal("0.05"):
                    logger.warning(
                        "Position divergence sleeve=%s symbol=%s db_qty=%s alpaca_qty=%s",
                        sleeve.id,
                        row.symbol,
                        db_qty,
                        alpaca_qty,
                    )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, force: bool = False) -> None:
        """Block indefinitely, running cycles until KeyboardInterrupt or kill switch."""
        logger.info("Orchestrator starting")
        self.run_startup_checks()
        interval = self._config.orchestrator.cycle_interval_seconds
        try:
            while True:
                mode = get_kill_switch_mode()
                if mode == "force":
                    self._handle_force_exit()
                    return
                if mode in ("graceful", "cancel_open"):
                    self._drain_fills(cancel_open=(mode == "cancel_open"))
                    return

                try:
                    self.run_once(force=force)
                except Exception:
                    logger.exception("Unexpected error in orchestration cycle")
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Orchestrator stopping")

    # ------------------------------------------------------------------
    # Single cycle
    # ------------------------------------------------------------------

    def run_once(self, force: bool = False) -> dict[str, Any]:
        """Run one orchestration cycle. Returns a summary dict.

        Args:
            force: Skip the market-hours check. Useful for testing outside
                   market hours. Orders are still subject to all other checks.
        """
        now = datetime.now(tz=timezone.utc)
        result: dict[str, Any] = {
            "timestamp": now.isoformat(),
            "skipped": False,
            "skip_reason": None,
            "sleeves_processed": 0,
            "orders_submitted": 0,
            "fills_processed": 0,
        }

        if is_kill_switch_active():
            logger.info("Kill switch active; skipping cycle")
            result.update(skipped=True, skip_reason="kill_switch")
            return result

        clock_client = self._paper_client or self._live_client
        if clock_client is None:
            logger.error("No Alpaca client configured")
            result.update(skipped=True, skip_reason="no_client")
            return result

        if not force:
            try:
                clock = clock_client.get_clock()
            except Exception:
                logger.exception("Failed to get market clock; skipping cycle")
                result.update(skipped=True, skip_reason="clock_error")
                return result

            if not clock.is_open:
                logger.debug("Market closed; skipping cycle")
                result.update(skipped=True, skip_reason="market_closed")
                return result
        else:
            logger.info("Market hours check bypassed (--force)")

        asset_universe = {
            row.symbol: row.tradable
            for row in AssetUniverseRepo(self._session).get_all()
        }
        if not asset_universe:
            logger.warning(
                "Asset universe is empty; run 'bot-ctl refresh-universe' before trading"
            )

        total_submitted = 0
        sleeves_processed = 0
        for sleeve in self._sleeve_manager.list_sleeves(status_filter=["running", "halted"]):
            try:
                submitted = self._run_sleeve_cycle(sleeve, now, asset_universe)
                total_submitted += submitted
                sleeves_processed += 1
            except Exception:
                logger.exception("Sleeve cycle failed for sleeve_id=%s", sleeve.id)

        fills = 0
        for client in [self._paper_client, self._live_client]:
            if client is None:
                continue
            try:
                fills += poll_fills(client, self._session, self._sleeve_manager)
            except Exception:
                logger.exception("poll_fills failed")

        HeartbeatRepo(self._session).update(now)
        self._session.commit()

        result.update(
            sleeves_processed=sleeves_processed,
            orders_submitted=total_submitted,
            fills_processed=fills,
        )
        logger.info("Cycle complete: %s", result)
        return result

    # ------------------------------------------------------------------
    # Per-sleeve cycle
    # ------------------------------------------------------------------

    def _run_sleeve_cycle(
        self,
        sleeve: Sleeve,
        as_of: datetime,
        asset_universe: dict[str, bool],
    ) -> int:
        """Run one cycle for a single sleeve. Returns count of orders submitted."""
        # Circuit breaker: auto-transition RUNNING sleeve to HALTED if drawdown exceeded.
        if sleeve.status == SleeveStatus.RUNNING:
            breaker_pct = float(sleeve.parameters.get("drawdown_circuit_breaker_pct", 0.15))
            if sleeve.drawdown_pct >= breaker_pct:
                logger.warning(
                    "Circuit breaker triggered sleeve=%s drawdown=%.2f%% threshold=%.2f%%",
                    sleeve.id, sleeve.drawdown_pct * 100, breaker_pct * 100,
                )
                self._sleeve_manager.halt_sleeve(str(sleeve.id))
                refreshed = self._sleeve_manager.get_sleeve(str(sleeve.id))
                if refreshed is not None:
                    sleeve = refreshed

        strategy = self._strategies.get(sleeve.strategy_name)
        if strategy is None:
            logger.error(
                "No strategy for %r; skipping sleeve %s", sleeve.strategy_name, sleeve.id
            )
            return 0

        client = self._client_for(sleeve)
        market_data = AlpacaMarketDataView(client)

        pos_rows = PositionRepo(self._session).list_by_sleeve(str(sleeve.id))
        positions = {
            r.symbol: Position(
                symbol=r.symbol,
                qty=Decimal(str(r.qty)),
                avg_cost=Decimal(str(r.avg_cost)),
            )
            for r in pos_rows
        }

        # Load per-symbol strategy state and refresh days_held / highest_close.
        # Prices fetched here for held positions are re-used for order sizing below.
        today = as_of.date()
        prefetched_prices: dict[str, Decimal] = {}
        state_repo = StrategyStateRepo(self._session)
        position_state: dict[str, dict[str, Any]] = {}
        for row in state_repo.get_by_sleeve(str(sleeve.id)):
            if row.symbol not in positions:
                # Position closed without strategy_state being cleaned up.
                state_repo.delete(str(sleeve.id), row.symbol)
                continue
            new_days_held = row.days_held
            new_last_session = row.last_session_date
            if row.last_session_date is None or row.last_session_date < today:
                new_days_held += 1
                new_last_session = today
            new_highest = Decimal(str(row.highest_close_since_entry))
            try:
                current_px = market_data.get_latest_price(row.symbol)
                prefetched_prices[row.symbol] = current_px
                if current_px > new_highest:
                    new_highest = current_px
            except Exception:
                logger.warning(
                    "Could not get price for %s; using stored highest_close", row.symbol
                )
            state_repo.upsert(
                sleeve_id=str(sleeve.id),
                symbol=row.symbol,
                entry_date=row.entry_date,
                entry_price=Decimal(str(row.entry_price)),
                days_held=new_days_held,
                highest_close_since_entry=new_highest,
                last_session_date=new_last_session,
            )
            position_state[row.symbol] = {
                "entry_date": row.entry_date,
                "entry_price": float(row.entry_price),
                "days_held": new_days_held,
                "highest_close_since_entry": float(new_highest),
            }

        targets = strategy.generate_targets(
            params=sleeve.parameters,
            nav=float(sleeve.current_nav),
            cash=float(sleeve.current_nav),
            positions=positions,
            market_data=market_data,
            universe=list(asset_universe.keys()) or ["SPY"],
            as_of=as_of,
            position_state=position_state if position_state else None,
        )

        if not targets:
            logger.debug("No targets for sleeve %s", sleeve.id)
            return 0

        symbols_needed = {t.symbol for t in targets} | set(positions.keys())
        current_prices: dict[str, Decimal] = dict(prefetched_prices)
        for symbol in symbols_needed - set(prefetched_prices):
            try:
                current_prices[symbol] = market_data.get_latest_price(symbol)
            except Exception:
                logger.warning("Could not get price for %s; skipping", symbol, exc_info=True)

        if not current_prices:
            logger.warning("No prices available for sleeve %s; skipping", sleeve.id)
            return 0

        min_notional = Decimal(str(self._config.execution.min_order_notional))
        current_position_qtys = {sym: pos.qty for sym, pos in positions.items()}

        orders = targets_to_intended_orders(
            sleeve=sleeve,
            targets=targets,
            current_prices=current_prices,
            current_positions=current_position_qtys,
            min_notional=min_notional,
        )

        if not orders:
            logger.debug("No orders needed for sleeve %s", sleeve.id)
            return 0

        # Adjust limit prices: close ± limit_offset_bps by side
        bps = Decimal(str(self._config.execution.limit_offset_bps)) / Decimal("10000")
        for order in orders:
            price = current_prices.get(order.symbol, order.limit_price)
            if order.side == "buy":
                order.limit_price = (price * (1 + bps)).quantize(Decimal("0.01"))
            else:
                order.limit_price = (price * (1 - bps)).quantize(Decimal("0.01"))

        max_nav_pct = Decimal(str(self._config.risk.max_order_nav_pct))
        passing, rejected = check_and_filter(
            orders,
            sleeve,
            asset_universe,
            kill_switch=is_kill_switch_active(),
            max_nav_pct=max_nav_pct,
        )
        for record in rejected:
            logger.warning(
                "Order rejected sleeve=%s symbol=%s reasons=%s",
                sleeve.id,
                record["order"].symbol,
                record["reasons"],
            )

        if not passing:
            return 0

        # HALTED sleeves: only submit exits (sells of currently-held positions).
        if sleeve.status == SleeveStatus.HALTED:
            held_symbols = set(positions.keys())
            passing = [o for o in passing if o.side == "sell" and o.symbol in held_symbols]
            if not passing:
                logger.debug("No exit orders for halted sleeve %s", sleeve.id)
                return 0

        alpaca_ids = submit_intended_orders(
            passing, client, self._session, self._sleeve_manager, as_of
        )
        logger.info("Submitted %d order(s) for sleeve %s", len(alpaca_ids), sleeve.id)
        return len(alpaca_ids)

    # ------------------------------------------------------------------
    # Kill switch drain and force-exit
    # ------------------------------------------------------------------

    def _has_submitted_net_orders(self) -> bool:
        return (
            self._session.query(NetOrderRow)
            .filter(NetOrderRow.status == "submitted")
            .count()
            > 0
        )

    def _drain_fills(self, cancel_open: bool = False) -> None:
        """Poll fills until all submitted orders are terminal, then exit cleanly.

        If cancel_open=True, cancels all open Alpaca orders first so DAY orders
        don't need to wait until 4:00 PM ET to expire.
        """
        logger.info("Kill switch active — entering drain mode (cancel_open=%s)", cancel_open)

        if cancel_open:
            for client in [self._paper_client, self._live_client]:
                if client is None:
                    continue
                try:
                    client.cancel_all_orders()
                    logger.info(
                        "Cancelled all open orders (%s)",
                        "paper" if client is self._paper_client else "live",
                    )
                except Exception:
                    logger.exception("Failed to cancel all orders")

        timeout = self._config.orchestrator.drain_timeout_seconds
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=timeout)

        while datetime.now(tz=timezone.utc) < deadline:
            if not self._has_submitted_net_orders():
                logger.info("No submitted orders remaining — drain complete")
                break

            for client in [self._paper_client, self._live_client]:
                if client is None:
                    continue
                try:
                    poll_fills(client, self._session, self._sleeve_manager)
                except Exception:
                    logger.exception("poll_fills failed during drain")

            now = datetime.now(tz=timezone.utc)
            HeartbeatRepo(self._session).update(now)
            self._session.commit()
            time.sleep(30)
        else:
            logger.warning(
                "Drain timed out after %ds — some submitted orders may be unreconciled",
                timeout,
            )

        now = datetime.now(tz=timezone.utc)
        HeartbeatRepo(self._session).update(now)
        self._session.commit()
        logger.info("Graceful halt complete. Exiting.")

    def _handle_force_exit(self) -> None:
        logger.warning(
            "Kill switch FORCE mode. Exiting immediately. "
            "In-flight orders and state may be inconsistent."
        )
        now = datetime.now(tz=timezone.utc)
        try:
            HeartbeatRepo(self._session).update(now)
            self._session.commit()
        except Exception:
            logger.exception("Failed to write final heartbeat before force exit")
        raise SystemExit(1)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _client_for(self, sleeve: Sleeve) -> PaperClient | LiveClient:
        if sleeve.mode == Mode.PAPER:
            if self._paper_client is None:
                raise RuntimeError("Paper client not configured for paper sleeve")
            return self._paper_client
        if self._live_client is None:
            raise RuntimeError("Live client not configured for live sleeve")
        return self._live_client
