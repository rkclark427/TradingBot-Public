"""Execution router — submits IntendedOrders to Alpaca and polls fills.

Phase 1 ships a single sleeve, so netting within a symbol group is a no-op
(there's always exactly one order per symbol).  The netting path is written
so it generalises cleanly to multi-sleeve netting in later phases.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from src.data.alpaca_client import LiveClient, PaperClient
from src.portfolio.sizer import IntendedOrder
from src.sleeves.manager import SleeveManager
from src.tracking.models import NetOrderRow
from src.tracking.repos.fills import FillRepo, SleeveFillRepo
from src.tracking.repos.orders import IntendedOrderRepo, IntendedToNetRepo, NetOrderRepo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def submit_intended_orders(
    orders: list[IntendedOrder],
    client: PaperClient | LiveClient,
    session: Session,
    sleeve_manager: SleeveManager,
    as_of: datetime,
) -> list[str]:
    """Submit a batch of IntendedOrders to Alpaca and persist audit rows.

    Phase 1 netting logic: group orders by symbol and net buys vs. sells.
    With a single sleeve there is at most one order per symbol, so netting
    is always a no-op here.

    Retry idempotency: before creating a new net_order row, we check for an
    existing error-status row for the same (date, symbol, mode).  If found, we
    reuse it — preserving the original client_order_id — so that Alpaca can
    deduplicate if the first attempt was received but the response was lost.

    Returns:
        List of Alpaca order IDs for orders that were successfully submitted.
    """
    if not orders:
        return []

    from src.queries.kill_switch import is_kill_switch_active

    intended_repo = IntendedOrderRepo(session)
    net_repo = NetOrderRepo(session)
    mapping_repo = IntendedToNetRepo(session)

    # Persist all intended orders first, collecting their DB rows.
    intended_rows = []
    for order in orders:
        row = intended_repo.create(
            timestamp=as_of,
            sleeve_id=order.sleeve_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            limit_price=order.limit_price,
            status="pending",
        )
        intended_rows.append((order, row))

    # Group by symbol for netting.
    grouped: dict[str, list[tuple[IntendedOrder, object]]] = {}
    for order, row in intended_rows:
        grouped.setdefault(order.symbol, []).append((order, row))

    alpaca_ids: list[str] = []
    date_str = as_of.strftime("%Y%m%d")

    for symbol, group in grouped.items():
        # Net buys against sells within the group.
        net_qty = Decimal("0")
        for order, _ in group:
            if order.side == "buy":
                net_qty += order.qty
            else:
                net_qty -= order.qty

        if net_qty == Decimal("0"):
            continue

        net_side = "buy" if net_qty > Decimal("0") else "sell"
        abs_net_qty = abs(net_qty)
        limit_price = group[0][0].limit_price

        sleeve = sleeve_manager.get_sleeve(group[0][0].sleeve_id)
        mode = sleeve.mode.value if sleeve is not None else "paper"

        # Check for an existing error-status row from a failed previous attempt.
        # Reusing it preserves the client_order_id for Alpaca-side deduplication.
        existing = net_repo.find_error_for_retry(date_str, symbol, mode)
        if existing is not None:
            net_row = existing
            net_row.status = "pending"
            net_row.net_qty = abs_net_qty
            net_row.side = net_side
            net_row.limit_price = limit_price
            session.flush()
            # Remove old mappings from the failed attempt to prevent double-attribution.
            mapping_repo.delete_by_net_order(net_row.id)
            session.flush()
            logger.info("Retrying failed net_order id=%s for %s", net_row.id, symbol)
        else:
            net_row = net_repo.create(
                timestamp=as_of,
                mode=mode,
                symbol=symbol,
                side=net_side,
                net_qty=abs_net_qty,
                limit_price=limit_price,
                alpaca_id=None,
                status="pending",
            )
            # net_repo.create() calls session.flush(), so net_row.id is assigned here.

        # Persist intended → net mappings before submission so they're never lost.
        for order, intended_row in group:
            mapping_repo.create(
                intended_order_id=intended_row.id,
                net_order_id=net_row.id,
                allocated_qty=order.qty,
            )

        # Re-check kill switch to handle the race where it was activated after
        # cycle start but before this submission point.
        if is_kill_switch_active():
            net_row.status = "cancelled_pre_submit"
            session.flush()
            logger.info(
                "Kill switch activated mid-cycle; cancelled pre-submit order for %s", symbol
            )
            session.commit()
            continue

        # client_order_id uses net_row.id — globally unique and stable for retry.
        client_order_id = f"{date_str}-{symbol}-{net_row.id}"

        try:
            order_info = client.submit_order(
                symbol=symbol,
                qty=abs_net_qty,
                side=net_side,
                limit_price=limit_price,
                client_order_id=client_order_id,
            )
            alpaca_id = order_info.id
            net_status = "submitted"
        except Exception as exc:
            # If Alpaca says the client_order_id already exists, the order was
            # received on a prior attempt whose response we lost. Look it up to
            # get the alpaca_id so fill polling can reconcile it.
            error_str = str(exc)
            if "client_order_id must be unique" in error_str or "42210000" in error_str:
                try:
                    existing_order = client.get_order_by_client_order_id(client_order_id)
                    alpaca_id = existing_order.id
                    net_status = "submitted"
                    logger.info(
                        "Order for %s already at Alpaca (client_order_id=%s alpaca_id=%s)",
                        symbol,
                        client_order_id,
                        alpaca_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to look up existing order by client_order_id for %s", symbol
                    )
                    alpaca_id = None
                    net_status = "error"
            else:
                logger.exception("Failed to submit order for %s", symbol)
                alpaca_id = None
                net_status = "error"

        net_row.alpaca_id = alpaca_id
        net_row.status = net_status

        session.commit()

        if alpaca_id is not None:
            alpaca_ids.append(alpaca_id)

    return alpaca_ids


# ---------------------------------------------------------------------------
# Poll fills
# ---------------------------------------------------------------------------


def poll_fills(
    client: PaperClient | LiveClient,
    session: Session,
    sleeve_manager: SleeveManager,
) -> int:
    """Fetch recent filled orders from Alpaca and process any new ones.

    Also marks expired and cancelled net_orders as such, allowing the graceful-
    halt drain loop to terminate cleanly even when orders don't fill.

    A fill is considered "already processed" if the ``net_orders`` row with
    the matching ``alpaca_id`` already has child rows in the ``fills`` table.

    Returns:
        Count of new fills processed.
    """
    all_closed = client.get_orders(status="closed")
    filled_orders = [o for o in all_closed if o.status == "filled"]

    net_repo = NetOrderRepo(session)
    fill_repo = FillRepo(session)
    sleeve_fill_repo = SleeveFillRepo(session)
    mapping_repo = IntendedToNetRepo(session)

    new_fill_count = 0

    for order_info in filled_orders:
        net_row: NetOrderRow | None = (
            session.query(NetOrderRow)
            .filter(NetOrderRow.alpaca_id == order_info.id)
            .first()
        )
        if net_row is None:
            logger.debug("No net_order row for alpaca_id=%s; skipping", order_info.id)
            continue

        existing_fills = fill_repo.get_by_net_order(net_row.id)
        if existing_fills:
            continue

        fill_price = order_info.filled_avg_price or order_info.limit_price or Decimal("0")
        fill_qty = order_info.filled_qty
        fill_timestamp = order_info.filled_at or order_info.submitted_at
        fees = Decimal("0")

        fill_row = fill_repo.create(
            net_order_id=net_row.id,
            timestamp=fill_timestamp,
            qty=fill_qty,
            fill_price=fill_price,
            fees=fees,
        )

        mappings = mapping_repo.get_by_net_order(net_row.id)

        for mapping in mappings:
            from src.tracking.repos.orders import IntendedOrderRepo as _IOR

            intended_row = _IOR(session).get(mapping.intended_order_id)
            if intended_row is None:
                continue

            sleeve_id = intended_row.sleeve_id

            sleeve_fill_repo.create(
                fill_id=fill_row.id,
                sleeve_id=sleeve_id,
                allocated_qty=mapping.allocated_qty,
                allocated_price=fill_price,
            )

            sleeve_manager.attribute_fill(
                sleeve_id=sleeve_id,
                symbol=net_row.symbol,
                side=net_row.side,
                qty=mapping.allocated_qty,
                fill_price=fill_price,
                fees=fees,
            )

        net_repo.update_status(net_row.id, "filled")
        session.commit()

        new_fill_count += 1

    # Mark expired / cancelled orders so the drain loop can see they're terminal.
    for order_info in all_closed:
        if order_info.status not in ("expired", "canceled"):
            continue
        net_row = (
            session.query(NetOrderRow)
            .filter(
                NetOrderRow.alpaca_id == order_info.id,
                NetOrderRow.status == "submitted",
            )
            .first()
        )
        if net_row is None:
            continue
        net_repo.update_status(net_row.id, order_info.status)
        logger.info(
            "Order %s %s; marking net_order %s as %s",
            order_info.id,
            order_info.status,
            net_row.id,
            order_info.status,
        )
        session.commit()

    return new_fill_count
