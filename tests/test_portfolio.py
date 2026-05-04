"""Tests for src/portfolio/sizer.py."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from src.portfolio.sizer import IntendedOrder, targets_to_intended_orders
from src.sleeves.types import Mode, Sleeve, SleeveRiskConfig, SleeveStatus
from src.strategies.base import Target


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLEEVE_ID = "11111111-1111-1111-1111-111111111111"


def _make_sleeve(nav: Decimal = Decimal("10000")) -> Sleeve:
    return Sleeve(
        id=UUID(_SLEEVE_ID),
        strategy_name="buy_and_hold",
        mode=Mode.PAPER,
        status=SleeveStatus.RUNNING,
        starting_capital=nav,
        current_nav=nav,
        high_water_mark=nav,
        parameters={},
        risk=SleeveRiskConfig(),
        managed=False,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_full_allocation_from_cash_produces_buy_order() -> None:
    """No existing position → full buy to reach target weight."""
    sleeve = _make_sleeve(Decimal("10000"))
    targets = [Target(symbol="SPY", target_weight=1.0)]
    prices = {"SPY": Decimal("500.00")}
    positions: dict[str, Decimal] = {}

    orders = targets_to_intended_orders(sleeve, targets, prices, positions)

    assert len(orders) == 1
    order = orders[0]
    assert order.sleeve_id == _SLEEVE_ID
    assert order.symbol == "SPY"
    assert order.side == "buy"
    # desired_qty = 10000 / 500 = 20.000000
    assert order.qty == Decimal("20.000000")
    assert order.limit_price == Decimal("500.00")


def test_noop_when_already_at_target_weight() -> None:
    """Already holding the exact target weight → delta is zero → no order."""
    sleeve = _make_sleeve(Decimal("10000"))
    # At $500/share, 20 shares = $10000 = 100% of NAV.
    targets = [Target(symbol="SPY", target_weight=1.0)]
    prices = {"SPY": Decimal("500.00")}
    positions = {"SPY": Decimal("20")}

    orders = targets_to_intended_orders(sleeve, targets, prices, positions)

    assert orders == []


def test_partial_rebalance_existing_position() -> None:
    """Holding half the target → should generate a buy for the remainder."""
    sleeve = _make_sleeve(Decimal("10000"))
    # desired = 10 shares, current = 5 → delta = 5
    targets = [Target(symbol="SPY", target_weight=0.5)]
    prices = {"SPY": Decimal("500.00")}
    positions = {"SPY": Decimal("0")}  # 0 current, target is 5000/500=10 shares

    # With current=0, desired=10 → buy 10
    orders = targets_to_intended_orders(sleeve, targets, prices, positions)
    assert len(orders) == 1
    assert orders[0].side == "buy"
    assert orders[0].qty == Decimal("10.000000")

    # Now with current=5, desired=10 → buy 5
    positions2 = {"SPY": Decimal("5")}
    orders2 = targets_to_intended_orders(sleeve, targets, prices, positions2)
    assert len(orders2) == 1
    assert orders2[0].side == "buy"
    assert orders2[0].qty == Decimal("5.000000")


def test_skip_order_below_min_notional() -> None:
    """Order whose notional is below min_notional threshold is skipped."""
    sleeve = _make_sleeve(Decimal("10000"))
    # delta = 0.001 shares * $500 = $0.50 < $1.00 min_notional
    # desired = 10000 * 0.5 / 500 = 10 shares; current = 9.999
    targets = [Target(symbol="SPY", target_weight=0.5)]
    prices = {"SPY": Decimal("500.00")}
    # desired = 10, current = 9.999 → delta = 0.001 shares → $0.50 notional
    positions = {"SPY": Decimal("9.999")}

    orders = targets_to_intended_orders(
        sleeve, targets, prices, positions, min_notional=Decimal("1.00")
    )
    assert orders == []


def test_sell_side_when_target_weight_zero_and_position_exists() -> None:
    """target_weight=0 with existing position should produce a sell order."""
    sleeve = _make_sleeve(Decimal("10000"))
    targets = [Target(symbol="SPY", target_weight=0.0)]
    prices = {"SPY": Decimal("500.00")}
    positions = {"SPY": Decimal("5")}

    orders = targets_to_intended_orders(sleeve, targets, prices, positions)

    assert len(orders) == 1
    order = orders[0]
    assert order.side == "sell"
    assert order.qty == Decimal("5.000000")


def test_fractional_rounding_to_six_decimal_places() -> None:
    """Quantities are rounded DOWN to 6 decimal places when fractional=True."""
    sleeve = _make_sleeve(Decimal("100"))
    # desired_qty = 100 / 3 = 33.333333... → should round DOWN to 33.333333
    targets = [Target(symbol="XYZ", target_weight=1.0)]
    prices = {"XYZ": Decimal("3")}
    positions: dict[str, Decimal] = {}

    orders = targets_to_intended_orders(
        sleeve, targets, prices, positions, round_fractional=True
    )

    assert len(orders) == 1
    qty = orders[0].qty
    # 100 / 3 = 33.33333... → ROUND_DOWN to 6dp = 33.333333
    assert qty == Decimal("33.333333")
    # Ensure exactly 6 decimal places (no more).
    assert qty == qty.quantize(Decimal("0.000001"))


def test_multiple_targets_produce_multiple_orders() -> None:
    """A list of targets each generates its own order."""
    sleeve = _make_sleeve(Decimal("10000"))
    targets = [
        Target(symbol="SPY", target_weight=0.6),
        Target(symbol="QQQ", target_weight=0.4),
    ]
    prices = {"SPY": Decimal("500.00"), "QQQ": Decimal("400.00")}
    positions: dict[str, Decimal] = {}

    orders = targets_to_intended_orders(sleeve, targets, prices, positions)

    assert len(orders) == 2
    symbols = {o.symbol for o in orders}
    assert symbols == {"SPY", "QQQ"}
    # SPY: 10000*0.6/500 = 12 shares
    spy_order = next(o for o in orders if o.symbol == "SPY")
    assert spy_order.qty == Decimal("12.000000")
    assert spy_order.side == "buy"
    # QQQ: 10000*0.4/400 = 10 shares
    qqq_order = next(o for o in orders if o.symbol == "QQQ")
    assert qqq_order.qty == Decimal("10.000000")
    assert qqq_order.side == "buy"


def test_limit_prices_override_current_prices() -> None:
    """If limit_prices dict is provided, order uses that price, not current."""
    sleeve = _make_sleeve(Decimal("10000"))
    targets = [Target(symbol="SPY", target_weight=1.0)]
    prices = {"SPY": Decimal("500.00")}
    limit_prices = {"SPY": Decimal("499.50")}
    positions: dict[str, Decimal] = {}

    orders = targets_to_intended_orders(
        sleeve, targets, prices, positions, limit_prices=limit_prices
    )

    assert len(orders) == 1
    assert orders[0].limit_price == Decimal("499.50")


def test_whole_share_rounding() -> None:
    """When round_fractional=False, quantities round DOWN to whole shares."""
    sleeve = _make_sleeve(Decimal("1000"))
    # desired = 1000/3 = 333.33... → rounds down to 333 whole shares
    targets = [Target(symbol="XYZ", target_weight=1.0)]
    prices = {"XYZ": Decimal("3")}
    positions: dict[str, Decimal] = {}

    orders = targets_to_intended_orders(
        sleeve, targets, prices, positions, round_fractional=False
    )

    assert len(orders) == 1
    assert orders[0].qty == Decimal("333")


def test_intended_order_is_dataclass() -> None:
    """IntendedOrder instances are plain dataclass instances."""
    order = IntendedOrder(
        sleeve_id="abc",
        symbol="SPY",
        side="buy",
        qty=Decimal("1"),
        limit_price=Decimal("500"),
    )
    assert order.sleeve_id == "abc"
    assert order.symbol == "SPY"
    assert order.side == "buy"
