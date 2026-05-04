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

    Returns:
        List of Alpaca order IDs for orders that were successfully submitted.
    """
    if not orders:
        return []

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
    # key: symbol → list of (IntendedOrder, IntendedOrderRow)
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
            # Fully netted — no order to submit.
            continue

        net_side = "buy" if net_qty > Decimal("0") else "sell"
        abs_net_qty = abs(net_qty)

        # Use the limit price from the first contributing order.
        # In Phase 1 there's only one order per symbol so this is unambiguous.
        limit_price = group[0][0].limit_price

        # Determine mode from the first order's sleeve.
        sleeve = sleeve_manager.get_sleeve(group[0][0].sleeve_id)
        mode = sleeve.mode.value if sleeve is not None else "paper"

        # Persist net order first (status=pending) so we get a DB-assigned id
        # before submission. This id is used in client_order_id to guarantee
        # global uniqueness even when two sleeves trade the same symbol on the same day.
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
            allocated = order.qty
            mapping_repo.create(
                intended_order_id=intended_row.id,
                net_order_id=net_row.id,
                allocated_qty=allocated,
            )

        # client_order_id uses net_row.id — globally unique and stable for retry.
        client_order_id = f"{date_str}-{symbol}-{net_row.id}"

        # Submit to Alpaca.
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
        except Exception:
            logger.exception("Failed to submit order for %s", symbol)
            alpaca_id = None
            net_status = "error"

        # Update net order with Alpaca result.
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

    A fill is considered "already processed" if the ``net_orders`` row with
    the matching ``alpaca_id`` already has child rows in the ``fills`` table.

    Returns:
        Count of new fills processed.
    """
    # "filled" is an order status, not a valid QueryOrderStatus filter value.
    # Alpaca's query API accepts "open", "closed", or "all"; filter client-side.
    all_closed = client.get_orders(status="closed")
    filled_orders = [o for o in all_closed if o.status == "filled"]

    net_repo = NetOrderRepo(session)
    fill_repo = FillRepo(session)
    sleeve_fill_repo = SleeveFillRepo(session)
    mapping_repo = IntendedToNetRepo(session)

    new_fill_count = 0

    for order_info in filled_orders:
        # Find the matching net_order row by alpaca_id.
        net_row: NetOrderRow | None = (
            session.query(NetOrderRow)
            .filter(NetOrderRow.alpaca_id == order_info.id)
            .first()
        )
        if net_row is None:
            # No matching net order — not submitted through this system; skip.
            logger.debug("No net_order row for alpaca_id=%s; skipping", order_info.id)
            continue

        # Check idempotency: if fills already exist for this net_order, skip.
        existing_fills = fill_repo.get_by_net_order(net_row.id)
        if existing_fills:
            continue

        # Determine fill metadata.
        fill_price = order_info.filled_avg_price or order_info.limit_price or Decimal("0")
        fill_qty = order_info.filled_qty
        fill_timestamp = order_info.filled_at or order_info.submitted_at
        fees = Decimal("0")

        # Write to fills table.
        fill_row = fill_repo.create(
            net_order_id=net_row.id,
            timestamp=fill_timestamp,
            qty=fill_qty,
            fill_price=fill_price,
            fees=fees,
        )

        # Look up intended orders for this net order.
        mappings = mapping_repo.get_by_net_order(net_row.id)

        for mapping in mappings:
            from src.tracking.repos.orders import IntendedOrderRepo as _IOR

            intended_row = _IOR(session).get(mapping.intended_order_id)
            if intended_row is None:
                continue

            sleeve_id = intended_row.sleeve_id

            # Write to sleeve_fills table.
            sleeve_fill_repo.create(
                fill_id=fill_row.id,
                sleeve_id=sleeve_id,
                allocated_qty=mapping.allocated_qty,
                allocated_price=fill_price,
            )

            # Update sleeve position and NAV.
            sleeve_manager.attribute_fill(
                sleeve_id=sleeve_id,
                symbol=net_row.symbol,
                side=net_row.side,
                qty=mapping.allocated_qty,
                fill_price=fill_price,
                fees=fees,
            )

        # Mark net order as filled.
        net_repo.update_status(net_row.id, "filled")
        session.commit()

        new_fill_count += 1

    return new_fill_count
