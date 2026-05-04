"""Pre-trade risk checks for Phase 1.

All checks are pure functions — no I/O, no side effects.  The canonical
entry-points are:

* ``run_pre_trade_checks`` — evaluate a single order, return reasons list.
* ``check_and_filter``     — evaluate a batch, partition into passing / rejected.
"""

from __future__ import annotations

from decimal import Decimal

from src.portfolio.sizer import IntendedOrder
from src.sleeves.types import Sleeve


# ---------------------------------------------------------------------------
# Single-order check
# ---------------------------------------------------------------------------


def run_pre_trade_checks(
    order: IntendedOrder,
    sleeve: Sleeve,
    asset_universe: dict[str, bool],
    kill_switch: bool,
    max_nav_pct: Decimal = Decimal("1.01"),
) -> list[str]:
    """Evaluate pre-trade risk rules against a single order.

    Returns a list of human-readable rejection reasons.  An empty list means
    the order passed all checks.

    Phase 1 checks (in evaluation order):
        1. Kill switch — immediate blanket rejection.
        2. Symbol tradability — symbol must be present and tradable.
        3. Order size — net notional must not exceed max_nav_pct of sleeve NAV.

    Args:
        order: The order to evaluate.
        sleeve: Current sleeve state (used for NAV-based sizing limits).
        asset_universe: Mapping of symbol → is_tradable.
        kill_switch: When True, all orders are rejected regardless of other
            conditions.
        max_nav_pct: Maximum allowed order notional as a fraction of sleeve NAV.
            Default 1.01 (101%) accommodates the limit_offset_bps premium
            (default 10bps = 0.1%) applied by the execution layer. Configurable
            via risk.max_order_nav_pct. If you tune execution.limit_offset_bps,
            tune this threshold accordingly.
    """
    reasons: list[str] = []

    # 1. Kill switch.
    if kill_switch:
        reasons.append("kill switch is active")
        return reasons  # no need to evaluate further

    # 2. Symbol tradability.
    if order.symbol not in asset_universe or not asset_universe[order.symbol]:
        reasons.append(f"symbol {order.symbol} is not tradable")

    # 3. Order size relative to sleeve NAV.
    order_notional = order.qty * order.limit_price
    threshold = sleeve.current_nav * max_nav_pct
    if order_notional > threshold:
        pct = int(max_nav_pct * 100)
        reasons.append(f"order exceeds {pct}% of sleeve NAV")

    return reasons


# ---------------------------------------------------------------------------
# Batch convenience wrapper
# ---------------------------------------------------------------------------


def check_and_filter(
    orders: list[IntendedOrder],
    sleeve: Sleeve,
    asset_universe: dict[str, bool],
    kill_switch: bool,
    max_nav_pct: Decimal = Decimal("1.01"),
) -> tuple[list[IntendedOrder], list[dict]]:
    """Run pre-trade checks on a list of orders and partition the results.

    Args:
        orders: Orders to evaluate.
        sleeve: Current sleeve state.
        asset_universe: Tradability mapping.
        kill_switch: Global halt flag.
        max_nav_pct: Max order notional as fraction of NAV. See run_pre_trade_checks.

    Returns:
        A 2-tuple ``(passing_orders, rejection_records)`` where each
        rejection record is a dict ``{"order": <IntendedOrder>,
        "reasons": [<str>, ...]}``.
    """
    passing: list[IntendedOrder] = []
    rejected: list[dict] = []

    for order in orders:
        reasons = run_pre_trade_checks(order, sleeve, asset_universe, kill_switch, max_nav_pct)
        if reasons:
            rejected.append({"order": order, "reasons": reasons})
        else:
            passing.append(order)

    return passing, rejected
