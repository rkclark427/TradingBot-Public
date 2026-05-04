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
) -> list[str]:
    """Evaluate pre-trade risk rules against a single order.

    Returns a list of human-readable rejection reasons.  An empty list means
    the order passed all checks.

    Phase 1 checks (in evaluation order):
        1. Kill switch — immediate blanket rejection.
        2. Symbol tradability — symbol must be present and tradable.
        3. Order size — net notional must not exceed 50 % of sleeve NAV.

    Args:
        order: The order to evaluate.
        sleeve: Current sleeve state (used for NAV-based sizing limits).
        asset_universe: Mapping of symbol → is_tradable.
        kill_switch: When True, all orders are rejected regardless of other
            conditions.
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
    # Guard against orders larger than the sleeve itself (bug protection).
    # Full-allocation strategies (e.g. BuyAndHoldSPY at 100% weight) are valid;
    # the real risk is accidentally ordering 200%+ of NAV.
    order_notional = order.qty * order.limit_price
    if order_notional > sleeve.current_nav * Decimal("1.0"):
        reasons.append("order exceeds 100% of sleeve NAV")

    return reasons


# ---------------------------------------------------------------------------
# Batch convenience wrapper
# ---------------------------------------------------------------------------


def check_and_filter(
    orders: list[IntendedOrder],
    sleeve: Sleeve,
    asset_universe: dict[str, bool],
    kill_switch: bool,
) -> tuple[list[IntendedOrder], list[dict]]:
    """Run pre-trade checks on a list of orders and partition the results.

    Args:
        orders: Orders to evaluate.
        sleeve: Current sleeve state.
        asset_universe: Tradability mapping.
        kill_switch: Global halt flag.

    Returns:
        A 2-tuple ``(passing_orders, rejection_records)`` where each
        rejection record is a dict ``{"order": <IntendedOrder>,
        "reasons": [<str>, ...]}``.
    """
    passing: list[IntendedOrder] = []
    rejected: list[dict] = []

    for order in orders:
        reasons = run_pre_trade_checks(order, sleeve, asset_universe, kill_switch)
        if reasons:
            rejected.append({"order": order, "reasons": reasons})
        else:
            passing.append(order)

    return passing, rejected
