"""Portfolio sizing layer.

Converts strategy targets (weight-based) into concrete IntendedOrders by
comparing desired allocations against current positions and computing the
delta quantity needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from src.sleeves.types import Sleeve
from src.strategies.base import Target


# ---------------------------------------------------------------------------
# IntendedOrder dataclass
# ---------------------------------------------------------------------------


@dataclass
class IntendedOrder:
    """A single order intention from a sleeve before netting or routing.

    Attributes:
        sleeve_id: The UUID string of the originating sleeve.
        symbol: Ticker symbol.
        side: "buy" or "sell".
        qty: Absolute quantity, always positive.
        limit_price: Price to use as the order limit (or placeholder if no
            limit_prices dict was provided to the sizer).
    """

    sleeve_id: str
    symbol: str
    side: str          # "buy" or "sell"
    qty: Decimal
    limit_price: Decimal


# ---------------------------------------------------------------------------
# Precision helpers
# ---------------------------------------------------------------------------

_SIX_PLACES = Decimal("0.000001")
_ZERO_PLACES = Decimal("1")


def _round_qty(qty: Decimal, fractional: bool) -> Decimal:
    """Round *qty* using ROUND_DOWN to the appropriate precision.

    ``fractional=True``  → 6 decimal places (e.g. 1.234567)
    ``fractional=False`` → whole shares  (e.g. 5)
    """
    if fractional:
        return qty.quantize(_SIX_PLACES, rounding=ROUND_DOWN)
    return qty.quantize(_ZERO_PLACES, rounding=ROUND_DOWN)


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------


def targets_to_intended_orders(
    sleeve: Sleeve,
    targets: list[Target],
    current_prices: dict[str, Decimal],
    current_positions: dict[str, Decimal],
    *,
    limit_prices: Optional[dict[str, Decimal]] = None,
    round_fractional: bool = True,
    min_notional: Decimal = Decimal("1.00"),
) -> list[IntendedOrder]:
    """Convert weight-based targets into a list of IntendedOrders.

    Args:
        sleeve: The sleeve whose NAV is used as the sizing reference.
        targets: Target weights from the strategy (weights should sum to ≤ 1).
        current_prices: Latest market prices, keyed by symbol.
        current_positions: Current position quantities held, keyed by symbol.
            Symbols with no position need not be present (treated as zero).
        limit_prices: Optional explicit limit prices to attach to orders.
            If a symbol is absent (or the dict is None), ``current_prices``
            is used as a placeholder.
        round_fractional: When True, round quantities to 6 decimal places;
            when False, round to whole shares.  Both use ROUND_DOWN.
        min_notional: Orders whose absolute notional value (``|delta_qty| *
            price``) is below this threshold are skipped.  Defaults to $1.00.

    Returns:
        List of IntendedOrder objects, one per target that survives the
        min-notional filter.  Orders are always non-zero and have positive qty.
    """
    orders: list[IntendedOrder] = []
    sleeve_id = str(sleeve.id)

    for target in targets:
        symbol = target.symbol
        price = current_prices.get(symbol)
        if price is None or price <= Decimal("0"):
            # Cannot size without a valid price — skip silently.
            continue

        # 1. Desired notional and quantity.
        desired_notional = Decimal(str(target.target_weight)) * sleeve.current_nav
        desired_qty = desired_notional / price

        # 2. Current position.
        current_qty = current_positions.get(symbol, Decimal("0"))

        # 3. Delta.
        delta_qty = desired_qty - current_qty

        # 4. Round (always round toward zero so we never over-buy).
        if delta_qty >= Decimal("0"):
            rounded_delta = _round_qty(delta_qty, round_fractional)
        else:
            # For negative deltas, quantize the absolute value then negate.
            rounded_delta = -_round_qty(-delta_qty, round_fractional)

        # 5. Skip if below min_notional threshold.
        notional_value = abs(rounded_delta) * price
        if notional_value < min_notional:
            continue

        # 6. Skip zero-qty orders (can happen after rounding).
        if rounded_delta == Decimal("0"):
            continue

        # 7. Determine side.
        side = "buy" if rounded_delta > Decimal("0") else "sell"
        qty = abs(rounded_delta)

        # 8. Determine limit price.
        if limit_prices is not None and symbol in limit_prices:
            limit_price = limit_prices[symbol]
        else:
            limit_price = price

        orders.append(
            IntendedOrder(
                sleeve_id=sleeve_id,
                symbol=symbol,
                side=side,
                qty=qty,
                limit_price=limit_price,
            )
        )

    return orders
