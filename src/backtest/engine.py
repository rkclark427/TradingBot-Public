"""Backtest simulation engine.

Walks forward through history one trading day at a time. On each day:
  1. Fill pending orders from the prior day at today's open + slippage.
  2. Accrue cash interest.
  3. Mark positions to market at today's close.
  4. Update position_state (days_held, highest_close_since_entry).
  5. Invoke the strategy's generate_targets.
  6. Translate targets to pending orders for tomorrow's open.
  7. Record a DaySnapshot.

Usage::

    layer = BacktestDataLayer(Path("data/backtest_cache.db"))
    result = run_simulation(
        strategy=MomentumContinuation(),
        params={"lookback_days": 20, ...},
        universe=list(UNIVERSE),
        data_layer=layer,
        start_date=date(2018, 1, 1),
        end_date=date(2024, 12, 31),
        starting_capital=Decimal("10000"),
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.backtest.data import BacktestDataLayer, BacktestMarketDataView
from src.strategies.base import Position, Strategy, Target

logger = logging.getLogger(__name__)

_EASTERN = ZoneInfo("America/New_York")
_SHARE_PRECISION = Decimal("0.000001")   # 6 decimal places for fractional shares
_PRICE_PRECISION = Decimal("0.0001")


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass
class TradeRecord:
    """One completed (or still-open at simulation end) position."""
    symbol: str
    entry_date: date
    entry_price: Decimal
    shares: Decimal
    exit_date: date | None = None
    exit_price: Decimal | None = None
    exit_reason: str | None = None  # hard_stop / trailing_stop / time_stop / strategy / end_of_simulation

    @property
    def pnl(self) -> Decimal | None:
        if self.exit_price is None:
            return None
        return (self.exit_price - self.entry_price) * self.shares

    @property
    def pnl_pct(self) -> float | None:
        if self.exit_price is None or self.entry_price == 0:
            return None
        return float((self.exit_price - self.entry_price) / self.entry_price)


@dataclass
class DaySnapshot:
    """End-of-day state for one simulation date."""
    sim_date: date
    nav: Decimal
    cash: Decimal
    positions: dict[str, Decimal]   # symbol -> shares held
    universe_size: int               # symbols with price data on this date


@dataclass
class SimulationResult:
    """Complete output of one strategy simulation run."""
    strategy_name: str
    params: dict[str, Any]
    start_date: date
    end_date: date
    starting_capital: Decimal
    slippage_bps: float
    cash_annualized_rate: float
    snapshots: list[DaySnapshot]
    trades: list[TradeRecord]
    cache_version: datetime | None
    run_timestamp: datetime

    def nav_series(self) -> pd.Series:
        """NAV by date as a float Series, suitable for equity curve calculations."""
        return pd.Series(
            {snap.sim_date: float(snap.nav) for snap in self.snapshots},
            name=self.strategy_name,
            dtype=float,
        )

    def cash_series(self) -> pd.Series:
        return pd.Series(
            {snap.sim_date: float(snap.cash) for snap in self.snapshots},
            name=f"{self.strategy_name}_cash",
            dtype=float,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass
class _SimPosition:
    qty: Decimal       # shares held (positive)
    avg_cost: Decimal  # cost basis per share


@dataclass
class _PendingOrder:
    symbol: str
    side: str           # "buy" | "sell"
    qty: Decimal        # always positive
    exit_reason: str | None  # set on sells from strategy rationale


def _fill_price(open_price: Decimal, side: str, slippage_bps: float) -> Decimal:
    bps = Decimal(str(slippage_bps)) / Decimal("10000")
    factor = (Decimal("1") + bps) if side == "buy" else (Decimal("1") - bps)
    return (open_price * factor).quantize(_PRICE_PRECISION)


def _targets_to_orders(
    targets: list[Target],
    positions: dict[str, _SimPosition],
    nav: Decimal,
    closes: pd.Series,
) -> list[_PendingOrder]:
    """Translate strategy target weights to pending orders.

    Symbols held but absent from targets are fully liquidated (weight 0 implied).
    """
    orders: list[_PendingOrder] = []
    mentioned: set[str] = set()

    for target in targets:
        symbol = target.symbol
        mentioned.add(symbol)

        raw_price = closes.get(symbol)
        if raw_price is None or pd.isna(raw_price) or float(raw_price) <= 0:
            logger.warning("No close price for %s; cannot size order", symbol)
            continue

        price = Decimal(str(raw_price))
        target_notional = nav * Decimal(str(max(target.target_weight, 0.0)))
        target_qty = (target_notional / price).quantize(_SHARE_PRECISION)

        current_qty = positions[symbol].qty if symbol in positions else Decimal("0")
        delta = target_qty - current_qty

        if abs(delta) < _SHARE_PRECISION:
            continue

        exit_reason: str | None = None
        if delta < 0:
            raw_reason = target.rationale.get("reason", "strategy")
            exit_reason = (
                raw_reason
                if raw_reason in ("hard_stop", "trailing_stop", "time_stop")
                else "strategy"
            )

        orders.append(_PendingOrder(
            symbol=symbol,
            side="buy" if delta > 0 else "sell",
            qty=abs(delta),
            exit_reason=exit_reason,
        ))

    # Fully liquidate positions the strategy omitted entirely
    for symbol, pos in positions.items():
        if symbol not in mentioned and pos.qty > _SHARE_PRECISION:
            orders.append(_PendingOrder(symbol=symbol, side="sell", qty=pos.qty, exit_reason="strategy"))

    return orders


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_simulation(
    strategy: Strategy,
    params: dict[str, Any],
    universe: list[str],
    data_layer: BacktestDataLayer,
    start_date: date,
    end_date: date,
    starting_capital: Decimal,
    slippage_bps: float = 5.0,
    cash_annualized_rate: float = 0.04,
) -> SimulationResult:
    """Run a single strategy through a historical simulation.

    The strategy is invoked identically to live mode — same ABC, same params,
    same position_state contract. Only the data source and execution venue differ.

    Orders are queued at the close of day D for execution at the open of day D+1.
    The final day's pending orders are not filled; open positions are recorded as
    ``exit_reason="end_of_simulation"`` in the trade log at the last available close.

    Args:
        strategy: Strategy instance to simulate.
        params: Strategy parameter dict (passed through to generate_targets).
        universe: Symbols the strategy may trade.
        data_layer: Populated BacktestDataLayer.
        start_date: First simulation date (inclusive).
        end_date: Last simulation date (inclusive).
        starting_capital: Starting cash in dollars.
        slippage_bps: Slippage applied to fill prices (buy higher, sell lower).
        cash_annualized_rate: Annualized interest on uninvested cash.

    Returns:
        SimulationResult with full equity curve, snapshots, and trade log.
    """
    run_ts = datetime.now(tz=timezone.utc)
    cache_version = data_layer.get_cache_version()
    daily_rate = Decimal(str(cash_annualized_rate)) / Decimal("252")

    # Pre-fetch price data for the full simulation window.
    # Extra calendar-day buffer ensures the strategy has lookback history on day 1.
    from datetime import timedelta
    data_start = start_date - timedelta(days=180)  # ~6 months of extra lookback

    all_closes = data_layer.get_close_prices(universe, data_start, end_date)
    ohlc_by_sym = data_layer.get_ohlc_bars(universe, start_date, end_date)
    all_opens: pd.DataFrame = pd.DataFrame(
        {sym: df["open"] for sym, df in ohlc_by_sym.items() if not df.empty}
    )

    # Derive the ordered list of trading days within the simulation window
    sim_mask = (all_closes.index >= pd.Timestamp(start_date)) & (all_closes.index <= pd.Timestamp(end_date))
    sim_closes = all_closes[sim_mask]
    trading_days: list[date] = [ts.date() for ts in sim_closes.index]

    if not trading_days:
        raise ValueError(f"No trading data found in cache for {start_date} … {end_date}. "
                         "Run `bot-ctl backtest refresh-data` first.")

    logger.info(
        "Starting simulation: strategy=%s days=%d %s → %s capital=$%.2f",
        strategy.name, len(trading_days), trading_days[0], trading_days[-1],
        float(starting_capital),
    )

    # Mutable simulation state
    cash: Decimal = starting_capital
    positions: dict[str, _SimPosition] = {}
    position_state: dict[str, dict[str, Any]] = {}
    pending_orders: list[_PendingOrder] = []

    # Outputs accumulated during the loop
    snapshots: list[DaySnapshot] = []
    open_trades: dict[str, TradeRecord] = {}
    closed_trades: list[TradeRecord] = []

    for i, sim_date in enumerate(trading_days):
        ts_date = pd.Timestamp(sim_date)
        sim_dt = datetime(sim_date.year, sim_date.month, sim_date.day,
                          tzinfo=_EASTERN)

        # ----------------------------------------------------------------
        # Step 1 — Fill pending orders at today's open price
        # ----------------------------------------------------------------
        newly_opened: set[str] = set()

        for order in pending_orders:
            # Look up today's open price for this symbol
            if ts_date not in all_opens.index or order.symbol not in all_opens.columns:
                logger.warning("No open price for %s on %s; order skipped", order.symbol, sim_date)
                continue
            raw_open = all_opens.at[ts_date, order.symbol]
            if pd.isna(raw_open) or float(raw_open) <= 0:
                logger.warning("Bad open price for %s on %s; order skipped", order.symbol, sim_date)
                continue

            fp = _fill_price(Decimal(str(raw_open)), order.side, slippage_bps)

            if order.side == "buy":
                cost = fp * order.qty
                # Trim to available cash if rounding pushes us over
                if cost > cash + Decimal("0.01"):
                    affordable = (cash / fp).quantize(_SHARE_PRECISION)
                    if affordable <= 0:
                        logger.warning("Insufficient cash for %s buy; skipped", order.symbol)
                        continue
                    order.qty = affordable
                    cost = fp * affordable

                cash -= cost

                if order.symbol in positions:
                    # Add to existing position: update avg cost basis
                    existing = positions[order.symbol]
                    new_qty = existing.qty + order.qty
                    new_avg = ((existing.qty * existing.avg_cost + cost) / new_qty).quantize(_SHARE_PRECISION)
                    positions[order.symbol] = _SimPosition(qty=new_qty, avg_cost=new_avg)
                else:
                    positions[order.symbol] = _SimPosition(qty=order.qty, avg_cost=fp)
                    position_state[order.symbol] = {
                        "entry_date": sim_date,
                        "entry_price": float(fp),
                        "days_held": 0,
                        "highest_close_since_entry": float(fp),
                    }
                    open_trades[order.symbol] = TradeRecord(
                        symbol=order.symbol,
                        entry_date=sim_date,
                        entry_price=fp,
                        shares=order.qty,
                    )
                    newly_opened.add(order.symbol)

                logger.debug("FILL BUY  %s qty=%.6f @ $%.4f  cash=$%.2f",
                             order.symbol, float(order.qty), float(fp), float(cash))

            else:  # sell
                if order.symbol not in positions:
                    logger.warning("Sell for unheld symbol %s; skipped", order.symbol)
                    continue

                pos = positions[order.symbol]
                sell_qty = min(order.qty, pos.qty)
                proceeds = fp * sell_qty
                cash += proceeds

                remaining = pos.qty - sell_qty
                if remaining < _SHARE_PRECISION:
                    del positions[order.symbol]
                    if order.symbol in position_state:
                        del position_state[order.symbol]
                    if order.symbol in open_trades:
                        trade = open_trades.pop(order.symbol)
                        trade.exit_date = sim_date
                        trade.exit_price = fp
                        trade.exit_reason = order.exit_reason or "strategy"
                        closed_trades.append(trade)
                else:
                    positions[order.symbol] = _SimPosition(qty=remaining, avg_cost=pos.avg_cost)

                logger.debug("FILL SELL %s qty=%.6f @ $%.4f  cash=$%.2f",
                             order.symbol, float(sell_qty), float(fp), float(cash))

        pending_orders = []

        # ----------------------------------------------------------------
        # Step 2 — Accrue daily cash interest
        # ----------------------------------------------------------------
        cash = (cash * (Decimal("1") + daily_rate)).quantize(Decimal("0.01"))

        # ----------------------------------------------------------------
        # Step 3 — Mark positions to market; compute NAV
        # ----------------------------------------------------------------
        today_closes: pd.Series = (
            all_closes.loc[ts_date] if ts_date in all_closes.index
            else pd.Series(dtype=float)
        )

        position_value = Decimal("0")
        for sym, pos in positions.items():
            raw_close = today_closes.get(sym)
            if raw_close is None or pd.isna(raw_close):
                position_value += pos.qty * pos.avg_cost  # fall back to cost basis
            else:
                position_value += pos.qty * Decimal(str(raw_close))

        nav = cash + position_value

        # ----------------------------------------------------------------
        # Step 4 — Update position_state
        # ----------------------------------------------------------------
        for sym, state in position_state.items():
            raw_close = today_closes.get(sym)
            if sym not in newly_opened:
                state["days_held"] += 1
            if raw_close is not None and not pd.isna(raw_close):
                state["highest_close_since_entry"] = max(
                    state["highest_close_since_entry"],
                    float(raw_close),
                )

        # ----------------------------------------------------------------
        # Step 5 — Invoke strategy
        # ----------------------------------------------------------------
        available_universe = [
            s for s in universe
            if today_closes.get(s) is not None and not pd.isna(today_closes.get(s))
        ]

        view = BacktestMarketDataView(data_layer, sim_date)
        strategy_positions = {
            sym: Position(sym, pos.qty, pos.avg_cost)
            for sym, pos in positions.items()
        }

        try:
            targets = strategy.generate_targets(
                params=params,
                nav=float(nav),
                cash=float(cash),
                positions=strategy_positions,
                market_data=view,
                universe=available_universe,
                as_of=sim_dt,
                position_state=position_state,
            )
        except Exception:
            logger.exception("Strategy raised on %s; holding current positions", sim_date)
            targets = [
                Target(
                    symbol=sym,
                    target_weight=float(pos.qty * Decimal(str(today_closes.get(sym) or pos.avg_cost))) / float(nav),
                    rationale={"reason": "error_hold"},
                )
                for sym, pos in positions.items()
            ]

        # ----------------------------------------------------------------
        # Step 6 — Translate targets to orders (skipped on last day)
        # ----------------------------------------------------------------
        if i < len(trading_days) - 1:
            pending_orders = _targets_to_orders(targets, positions, nav, today_closes)

        # ----------------------------------------------------------------
        # Step 7 — Record end-of-day snapshot
        # ----------------------------------------------------------------
        snapshots.append(DaySnapshot(
            sim_date=sim_date,
            nav=nav,
            cash=cash,
            positions={sym: pos.qty for sym, pos in positions.items()},
            universe_size=len(available_universe),
        ))

        if (i + 1) % 100 == 0 or i == len(trading_days) - 1:
            logger.info("Simulation %s: day %d/%d  NAV=$%.2f  positions=%d",
                        strategy.name, i + 1, len(trading_days), float(nav), len(positions))

    # ----------------------------------------------------------------
    # End of simulation — record open positions as end_of_simulation exits
    # ----------------------------------------------------------------
    if trading_days:
        last_ts = pd.Timestamp(trading_days[-1])
        last_closes = (
            all_closes.loc[last_ts] if last_ts in all_closes.index
            else pd.Series(dtype=float)
        )
        for sym in list(open_trades):
            raw_close = last_closes.get(sym)
            exit_price = Decimal(str(raw_close)) if (raw_close is not None and not pd.isna(raw_close)) else (
                positions[sym].avg_cost if sym in positions else Decimal("0")
            )
            trade = open_trades.pop(sym)
            trade.exit_date = trading_days[-1]
            trade.exit_price = exit_price
            trade.exit_reason = "end_of_simulation"
            closed_trades.append(trade)

    all_trades = closed_trades  # open_trades is now empty
    logger.info(
        "Simulation complete: strategy=%s  days=%d  trades=%d  final_nav=$%.2f",
        strategy.name, len(snapshots), len(all_trades),
        float(snapshots[-1].nav) if snapshots else 0.0,
    )

    return SimulationResult(
        strategy_name=strategy.name,
        params=params,
        start_date=start_date,
        end_date=end_date,
        starting_capital=starting_capital,
        slippage_bps=slippage_bps,
        cash_annualized_rate=cash_annualized_rate,
        snapshots=snapshots,
        trades=all_trades,
        cache_version=cache_version,
        run_timestamp=run_ts,
    )
