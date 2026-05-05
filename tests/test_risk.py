"""Tests for src/risk/checks.py."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from src.portfolio.sizer import IntendedOrder
from src.risk.checks import check_and_filter, run_pre_trade_checks
from src.sleeves.types import Mode, Sleeve, SleeveRiskConfig, SleeveStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLEEVE_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _make_sleeve(nav: Decimal = Decimal("10000")) -> Sleeve:
    return Sleeve(
        id=UUID(_SLEEVE_ID),
        strategy_name="buy_and_hold",
        mode=Mode.PAPER,
        status=SleeveStatus.RUNNING,
        starting_capital=nav,
        current_nav=nav,
        current_cash=nav,
        high_water_mark=nav,
        parameters={},
        risk=SleeveRiskConfig(),
        managed=False,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _make_order(
    symbol: str = "SPY",
    qty: Decimal = Decimal("1"),
    limit_price: Decimal = Decimal("100"),
    side: str = "buy",
) -> IntendedOrder:
    return IntendedOrder(
        sleeve_id=_SLEEVE_ID,
        symbol=symbol,
        side=side,
        qty=qty,
        limit_price=limit_price,
    )


_UNIVERSE_SPY_TRADABLE: dict[str, bool] = {"SPY": True}


# ---------------------------------------------------------------------------
# run_pre_trade_checks
# ---------------------------------------------------------------------------


def test_clean_order_passes_all_checks() -> None:
    """An order that meets all criteria should return an empty reasons list."""
    sleeve = _make_sleeve(Decimal("10000"))
    order = _make_order(symbol="SPY", qty=Decimal("1"), limit_price=Decimal("100"))
    reasons = run_pre_trade_checks(order, sleeve, _UNIVERSE_SPY_TRADABLE, kill_switch=False)
    assert reasons == []


def test_kill_switch_rejects_order() -> None:
    """When kill_switch=True, the order is rejected with the kill switch reason."""
    sleeve = _make_sleeve()
    order = _make_order()
    reasons = run_pre_trade_checks(order, sleeve, _UNIVERSE_SPY_TRADABLE, kill_switch=True)
    assert "kill switch is active" in reasons


def test_kill_switch_returns_immediately_without_further_checks() -> None:
    """Kill switch is checked first; other violations are not reported when it fires."""
    sleeve = _make_sleeve(Decimal("10"))
    # This order would also fail the size check (100*100 >> 10), but we only
    # expect the kill-switch reason because we return early.
    order = _make_order(qty=Decimal("100"), limit_price=Decimal("100"))
    reasons = run_pre_trade_checks(order, sleeve, {}, kill_switch=True)
    assert reasons == ["kill switch is active"]


def test_untradable_symbol_rejected() -> None:
    """Symbol present in universe but marked not-tradable should be rejected."""
    sleeve = _make_sleeve()
    order = _make_order(symbol="XYZ")
    reasons = run_pre_trade_checks(order, sleeve, {"XYZ": False}, kill_switch=False)
    assert any("XYZ" in r and "not tradable" in r for r in reasons)


def test_symbol_not_in_universe_rejected() -> None:
    """Symbol absent from asset_universe is not tradable."""
    sleeve = _make_sleeve()
    order = _make_order(symbol="UNKWN")
    reasons = run_pre_trade_checks(order, sleeve, {}, kill_switch=False)
    assert any("UNKWN" in r and "not tradable" in r for r in reasons)


def test_oversized_order_rejected() -> None:
    """Order notional > 101% of sleeve NAV should be rejected."""
    sleeve = _make_sleeve(Decimal("10000"))
    # order notional = 103 * 100 = 10300 > 10100 (101% of NAV)
    order = _make_order(symbol="SPY", qty=Decimal("103"), limit_price=Decimal("100"))
    reasons = run_pre_trade_checks(order, sleeve, _UNIVERSE_SPY_TRADABLE, kill_switch=False)
    assert any("101%" in r for r in reasons)


def test_order_within_limit_offset_buffer_passes() -> None:
    """Order notional at 100.5% NAV passes — within the 1% bps-offset buffer."""
    sleeve = _make_sleeve(Decimal("10000"))
    # notional = 1005 * 10 = 10050 = 100.5% of 10000 — inside the 101% threshold
    order = _make_order(symbol="SPY", qty=Decimal("1005"), limit_price=Decimal("10"))
    reasons = run_pre_trade_checks(order, sleeve, _UNIVERSE_SPY_TRADABLE, kill_switch=False)
    assert not any("101%" in r for r in reasons)


def test_multiple_violations_reported() -> None:
    """Both tradability and size violations are reported together."""
    sleeve = _make_sleeve(Decimal("10"))
    # Not in universe AND enormous order (notional = 100*100 = 10000 >> NAV of $10).
    order = _make_order(symbol="NOPE", qty=Decimal("100"), limit_price=Decimal("100"))
    reasons = run_pre_trade_checks(order, sleeve, {}, kill_switch=False)
    assert len(reasons) >= 2
    assert any("not tradable" in r for r in reasons)
    assert any("101%" in r for r in reasons)


# ---------------------------------------------------------------------------
# check_and_filter
# ---------------------------------------------------------------------------


def test_check_and_filter_separates_passing_from_rejected() -> None:
    """check_and_filter partitions clean orders and rejected orders correctly."""
    sleeve = _make_sleeve(Decimal("10000"))
    good_order = _make_order(symbol="SPY", qty=Decimal("1"), limit_price=Decimal("100"))
    bad_order = _make_order(symbol="SPY", qty=Decimal("200"), limit_price=Decimal("100"))

    universe = {"SPY": True}
    passing, rejected = check_and_filter(
        [good_order, bad_order], sleeve, universe, kill_switch=False
    )

    assert good_order in passing
    assert len(rejected) == 1
    assert rejected[0]["order"] is bad_order
    assert rejected[0]["reasons"]


def test_check_and_filter_kill_switch_rejects_all() -> None:
    """With kill_switch=True, every order is rejected."""
    sleeve = _make_sleeve()
    orders = [_make_order(symbol="SPY"), _make_order(symbol="QQQ")]
    universe = {"SPY": True, "QQQ": True}

    passing, rejected = check_and_filter(orders, sleeve, universe, kill_switch=True)

    assert passing == []
    assert len(rejected) == 2


def test_check_and_filter_all_pass() -> None:
    """All clean orders end up in the passing list with no rejections."""
    sleeve = _make_sleeve(Decimal("10000"))
    orders = [
        _make_order(symbol="SPY", qty=Decimal("1"), limit_price=Decimal("50")),
        _make_order(symbol="QQQ", qty=Decimal("1"), limit_price=Decimal("50")),
    ]
    universe = {"SPY": True, "QQQ": True}

    passing, rejected = check_and_filter(orders, sleeve, universe, kill_switch=False)

    assert len(passing) == 2
    assert rejected == []


def test_check_and_filter_empty_input() -> None:
    """Empty order list returns two empty lists."""
    sleeve = _make_sleeve()
    passing, rejected = check_and_filter([], sleeve, {}, kill_switch=False)
    assert passing == []
    assert rejected == []


# ---------------------------------------------------------------------------
# Config-driven threshold tests
# ---------------------------------------------------------------------------


def test_tighter_threshold_rejects_order_default_would_pass() -> None:
    """A lower max_nav_pct rejects an order the default 101% threshold would pass."""
    sleeve = _make_sleeve(Decimal("10000"))
    # notional = 10050; default 101% → 10100 limit → passes; 100% → 10000 limit → rejected
    order = _make_order(symbol="SPY", qty=Decimal("1"), limit_price=Decimal("10050"))
    assert not run_pre_trade_checks(order, sleeve, _UNIVERSE_SPY_TRADABLE, kill_switch=False)
    reasons_tight = run_pre_trade_checks(
        order, sleeve, _UNIVERSE_SPY_TRADABLE, kill_switch=False,
        max_nav_pct=Decimal("1.0"),
    )
    assert any("100%" in r for r in reasons_tight)


def test_looser_threshold_passes_order_default_would_reject() -> None:
    """A higher max_nav_pct passes an order the default 101% threshold would reject."""
    sleeve = _make_sleeve(Decimal("10000"))
    # notional = 103 * 100 = 10300; default 101% → 10100 limit → rejected; 105% → 10500 → passes
    order = _make_order(symbol="SPY", qty=Decimal("103"), limit_price=Decimal("100"))
    assert run_pre_trade_checks(order, sleeve, _UNIVERSE_SPY_TRADABLE, kill_switch=False)
    reasons_loose = run_pre_trade_checks(
        order, sleeve, _UNIVERSE_SPY_TRADABLE, kill_switch=False,
        max_nav_pct=Decimal("1.05"),
    )
    assert not reasons_loose
