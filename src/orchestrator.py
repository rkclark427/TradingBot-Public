"""Orchestrator — main bot loop.

Each cycle:
  1. Kill switch check
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
from src.queries.kill_switch import is_kill_switch_active
from src.risk.checks import check_and_filter
from src.sleeves.manager import SleeveManager
from src.sleeves.types import Mode, Sleeve
from src.strategies.base import MarketDataView, Position, Strategy
from src.tracking.repos.heartbeat import HeartbeatRepo
from src.tracking.repos.market import AssetUniverseRepo
from src.tracking.repos.positions import PositionRepo

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
        from alpaca.data.timeframe import TimeFrame

        now = datetime.now(tz=timezone.utc)
        start = now - timedelta(days=7)
        df = self._client.get_bars(symbol, TimeFrame.Day, start, now)
        if df.empty:
            raise ValueError(f"No price data available for {symbol}")
        return Decimal(str(df["close"].iloc[-1]))


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

            self._strategies: dict[str, Strategy] = {"buy_and_hold": BuyAndHoldSPY()}
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

    def run(self) -> None:
        """Block indefinitely, running cycles until KeyboardInterrupt."""
        logger.info("Orchestrator starting")
        self.run_startup_checks()
        interval = self._config.orchestrator.cycle_interval_seconds
        try:
            while True:
                try:
                    self.run_once()
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
        for sleeve in self._sleeve_manager.list_sleeves(status_filter="running"):
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

        targets = strategy.generate_targets(
            params=sleeve.parameters,
            nav=float(sleeve.current_nav),
            cash=float(sleeve.current_nav),
            positions=positions,
            market_data=market_data,
            universe=list(asset_universe.keys()) or ["SPY"],
            as_of=as_of,
        )

        if not targets:
            logger.debug("No targets for sleeve %s", sleeve.id)
            return 0

        symbols_needed = {t.symbol for t in targets} | set(positions.keys())
        current_prices: dict[str, Decimal] = {}
        for symbol in symbols_needed:
            try:
                current_prices[symbol] = market_data.get_latest_price(symbol)
            except Exception:
                logger.warning("Could not get price for %s; skipping", symbol)

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

        passing, rejected = check_and_filter(
            orders, sleeve, asset_universe, kill_switch=is_kill_switch_active()
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

        alpaca_ids = submit_intended_orders(
            passing, client, self._session, self._sleeve_manager, as_of
        )
        logger.info("Submitted %d order(s) for sleeve %s", len(alpaca_ids), sleeve.id)
        return len(alpaca_ids)

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
