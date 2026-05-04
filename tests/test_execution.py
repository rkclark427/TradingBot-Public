"""Tests for src/execution/router.py."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.execution.router import poll_fills, submit_intended_orders
from src.portfolio.sizer import IntendedOrder
from src.sleeves.types import AccountCapabilities, Mode, Sleeve, SleeveRiskConfig, SleeveStatus
from src.tracking.models import Base, NetOrderRow
from src.tracking.repos.fills import FillRepo
from src.tracking.repos.orders import IntendedOrderRepo, IntendedToNetRepo, NetOrderRepo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SLEEVE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_AS_OF = datetime(2026, 5, 3, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        # Create the sleeve row that intended_orders.sleeve_id FK references.
        from src.tracking.models import SleeveRow

        sleeve_row = SleeveRow(
            id=_SLEEVE_ID,
            strategy_name="buy_and_hold",
            mode="paper",
            status="running",
            starting_capital=Decimal("10000"),
            current_nav=Decimal("10000"),
            high_water_mark=Decimal("10000"),
            current_cash=Decimal("10000"),
            created_at=_AS_OF,
            parameters_json="{}",
        )
        s.add(sleeve_row)
        s.commit()
        yield s


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
        created_at=_AS_OF,
    )


def _make_sleeve_manager(session: Session, sleeve: Sleeve) -> MagicMock:
    mgr = MagicMock()
    mgr.get_sleeve.return_value = sleeve
    return mgr


def _make_order(
    symbol: str = "SPY",
    side: str = "buy",
    qty: Decimal = Decimal("10"),
    limit_price: Decimal = Decimal("500"),
) -> IntendedOrder:
    return IntendedOrder(
        sleeve_id=_SLEEVE_ID,
        symbol=symbol,
        side=side,
        qty=qty,
        limit_price=limit_price,
    )


def _make_client(alpaca_id: str = "alpaca-order-001") -> MagicMock:
    client = MagicMock()
    order_info = MagicMock()
    order_info.id = alpaca_id
    client.submit_order.return_value = order_info
    return client


# ---------------------------------------------------------------------------
# submit_intended_orders tests
# ---------------------------------------------------------------------------


def test_submit_persists_net_order_row(session: Session) -> None:
    """submit_intended_orders should create a net_orders row."""
    client = _make_client("alp-001")
    sleeve = _make_sleeve()
    mgr = _make_sleeve_manager(session, sleeve)
    order = _make_order()

    submit_intended_orders([order], client, session, mgr, _AS_OF)

    net_rows = session.query(NetOrderRow).all()
    assert len(net_rows) == 1
    net_row = net_rows[0]
    assert net_row.symbol == "SPY"
    assert net_row.side == "buy"
    assert Decimal(str(net_row.net_qty)) == Decimal("10")
    assert net_row.alpaca_id == "alp-001"
    assert net_row.status == "submitted"


def test_submit_persists_intended_to_net_row(session: Session) -> None:
    """submit_intended_orders should create an intended_to_net mapping row."""
    client = _make_client("alp-002")
    sleeve = _make_sleeve()
    mgr = _make_sleeve_manager(session, sleeve)
    order = _make_order()

    submit_intended_orders([order], client, session, mgr, _AS_OF)

    net_rows = session.query(NetOrderRow).all()
    assert len(net_rows) == 1
    net_id = net_rows[0].id

    mappings = IntendedToNetRepo(session).get_by_net_order(net_id)
    assert len(mappings) == 1
    assert Decimal(str(mappings[0].allocated_qty)) == Decimal("10")


def test_submit_calls_client_submit_order_with_correct_args(session: Session) -> None:
    """submit_intended_orders should call client.submit_order with the right args."""
    client = _make_client("alp-003")
    sleeve = _make_sleeve()
    mgr = _make_sleeve_manager(session, sleeve)
    order = _make_order(symbol="SPY", side="buy", qty=Decimal("5"), limit_price=Decimal("499"))

    submit_intended_orders([order], client, session, mgr, _AS_OF)

    client.submit_order.assert_called_once_with(
        symbol="SPY",
        qty=Decimal("5"),
        side="buy",
        limit_price=Decimal("499"),
        client_order_id="20260503-SPY-1",
    )


def test_submit_returns_alpaca_id(session: Session) -> None:
    """Return value should be a list containing the Alpaca order ID."""
    client = _make_client("alp-004")
    sleeve = _make_sleeve()
    mgr = _make_sleeve_manager(session, sleeve)
    order = _make_order()

    ids = submit_intended_orders([order], client, session, mgr, _AS_OF)

    assert ids == ["alp-004"]


def test_submit_empty_orders_returns_empty(session: Session) -> None:
    """Passing an empty list should return an empty list without touching the client."""
    client = _make_client()
    sleeve = _make_sleeve()
    mgr = _make_sleeve_manager(session, sleeve)

    ids = submit_intended_orders([], client, session, mgr, _AS_OF)

    assert ids == []
    client.submit_order.assert_not_called()


def test_submit_netting_buy_and_sell_same_symbol(session: Session) -> None:
    """Two orders for the same symbol (one buy, one sell) should net to zero → no submission."""
    # Need two different sleeve rows for this to work via FK, but for simplicity
    # we use the same sleeve ID for both — the netting logic only cares about symbol.
    client = _make_client("alp-005")
    sleeve = _make_sleeve()
    mgr = _make_sleeve_manager(session, sleeve)

    buy = _make_order(symbol="SPY", side="buy", qty=Decimal("5"), limit_price=Decimal("500"))
    sell = _make_order(symbol="SPY", side="sell", qty=Decimal("5"), limit_price=Decimal("500"))

    ids = submit_intended_orders([buy, sell], client, session, mgr, _AS_OF)

    # Fully netted → nothing submitted.
    assert ids == []
    client.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# poll_fills tests
# ---------------------------------------------------------------------------


def _create_net_order(session: Session, alpaca_id: str = "alp-fill-001") -> NetOrderRow:
    """Helper: persist a net_order row so poll_fills can find it."""
    row = NetOrderRow(
        timestamp=_AS_OF,
        mode="paper",
        symbol="SPY",
        side="buy",
        net_qty=Decimal("10"),
        limit_price=Decimal("500"),
        alpaca_id=alpaca_id,
        status="submitted",
    )
    session.add(row)
    session.flush()
    return row


def _make_filled_order_info(
    alpaca_id: str,
    symbol: str = "SPY",
    filled_qty: Decimal = Decimal("10"),
    fill_price: Decimal = Decimal("501"),
) -> MagicMock:
    info = MagicMock()
    info.id = alpaca_id
    info.status = "filled"
    info.symbol = symbol
    info.filled_qty = filled_qty
    info.filled_avg_price = fill_price
    info.limit_price = Decimal("500")
    info.filled_at = _AS_OF
    info.submitted_at = _AS_OF
    return info


def test_poll_fills_processes_new_fill(session: Session) -> None:
    """poll_fills should write fill rows and call sleeve_manager.attribute_fill."""
    alpaca_id = "alp-fill-001"
    net_row = _create_net_order(session, alpaca_id)

    # Create an intended_order and intended_to_net mapping so the attribution works.
    intended_row = IntendedOrderRepo(session).create(
        timestamp=_AS_OF,
        sleeve_id=_SLEEVE_ID,
        symbol="SPY",
        side="buy",
        qty=Decimal("10"),
        limit_price=Decimal("500"),
        status="pending",
    )
    IntendedToNetRepo(session).create(
        intended_order_id=intended_row.id,
        net_order_id=net_row.id,
        allocated_qty=Decimal("10"),
    )
    session.commit()

    client = MagicMock()
    client.get_orders.return_value = [_make_filled_order_info(alpaca_id)]

    mgr = MagicMock()

    count = poll_fills(client, session, mgr)

    assert count == 1
    mgr.attribute_fill.assert_called_once_with(
        sleeve_id=_SLEEVE_ID,
        symbol="SPY",
        side="buy",
        qty=Decimal("10"),
        fill_price=Decimal("501"),
        fees=Decimal("0"),
    )


def test_poll_fills_skips_already_processed(session: Session) -> None:
    """poll_fills is idempotent — re-processing a filled order is a no-op."""
    alpaca_id = "alp-fill-002"
    net_row = _create_net_order(session, alpaca_id)

    # Pre-populate fills table so the order looks already processed.
    FillRepo(session).create(
        net_order_id=net_row.id,
        timestamp=_AS_OF,
        qty=Decimal("10"),
        fill_price=Decimal("501"),
        fees=Decimal("0"),
    )
    session.commit()

    client = MagicMock()
    client.get_orders.return_value = [_make_filled_order_info(alpaca_id)]

    mgr = MagicMock()

    count = poll_fills(client, session, mgr)

    assert count == 0
    mgr.attribute_fill.assert_not_called()


def test_poll_fills_ignores_unknown_alpaca_id(session: Session) -> None:
    """Orders in Alpaca that have no matching net_order row are silently ignored."""
    client = MagicMock()
    client.get_orders.return_value = [_make_filled_order_info("unknown-id-xyz")]

    mgr = MagicMock()

    count = poll_fills(client, session, mgr)

    assert count == 0
    mgr.attribute_fill.assert_not_called()


# ---------------------------------------------------------------------------
# Retry idempotency tests
# ---------------------------------------------------------------------------


def test_retry_reuses_existing_error_net_row(session: Session) -> None:
    """On retry (same symbol/day), submit reuses the original error-status net_row."""
    # First attempt: submission raises a transient network error.
    client1 = MagicMock()
    client1.submit_order.side_effect = Exception("connection timeout")
    sleeve = _make_sleeve()
    mgr = _make_sleeve_manager(session, sleeve)
    order = _make_order(symbol="SPY", side="buy", qty=Decimal("5"), limit_price=Decimal("499"))

    ids1 = submit_intended_orders([order], client1, session, mgr, _AS_OF)
    assert ids1 == []

    net_rows = session.query(NetOrderRow).all()
    assert len(net_rows) == 1
    first_net_id = net_rows[0].id
    assert net_rows[0].status == "error"

    # Second attempt: succeeds. Should reuse the original net_row.
    client2 = _make_client("alp-retry-001")
    ids2 = submit_intended_orders([order], client2, session, mgr, _AS_OF)
    assert ids2 == ["alp-retry-001"]

    # Only one net_row should exist — the retry reused it, not created a new one.
    net_rows_after = session.query(NetOrderRow).all()
    assert len(net_rows_after) == 1
    assert net_rows_after[0].id == first_net_id

    # Both attempts must use the same client_order_id for Alpaca deduplication.
    expected_cid = f"20260503-SPY-{first_net_id}"
    client2.submit_order.assert_called_once()
    call_kwargs = client2.submit_order.call_args[1]
    assert call_kwargs["client_order_id"] == expected_cid


def test_two_sleeves_same_symbol_get_distinct_client_order_ids(session: Session) -> None:
    """Two sleeves independently buying the same symbol in the same cycle
    must produce distinct client_order_ids (no Alpaca uniqueness collision)."""
    from uuid import UUID

    from src.tracking.models import SleeveRow

    sleeve_id_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    session.add(
        SleeveRow(
            id=sleeve_id_b,
            strategy_name="buy_and_hold",
            mode="paper",
            status="running",
            starting_capital=Decimal("10000"),
            current_nav=Decimal("10000"),
            high_water_mark=Decimal("10000"),
            current_cash=Decimal("10000"),
            created_at=_AS_OF,
            parameters_json="{}",
        )
    )
    session.commit()

    sleeve_a = _make_sleeve()
    sleeve_b = Sleeve(
        id=UUID(sleeve_id_b),
        strategy_name="buy_and_hold",
        mode=Mode.PAPER,
        status=SleeveStatus.RUNNING,
        starting_capital=Decimal("10000"),
        current_nav=Decimal("10000"),
        high_water_mark=Decimal("10000"),
        parameters={},
        risk=SleeveRiskConfig(),
        managed=False,
        created_at=_AS_OF,
    )

    mgr = MagicMock()
    mgr.get_sleeve.side_effect = lambda sid: sleeve_a if sid == _SLEEVE_ID else sleeve_b

    client_a = _make_client("alp-a-001")
    client_b = _make_client("alp-b-001")

    order_a = IntendedOrder(
        sleeve_id=_SLEEVE_ID, symbol="SPY", side="buy",
        qty=Decimal("5"), limit_price=Decimal("499"),
    )
    order_b = IntendedOrder(
        sleeve_id=sleeve_id_b, symbol="SPY", side="buy",
        qty=Decimal("10"), limit_price=Decimal("499"),
    )

    ids_a = submit_intended_orders([order_a], client_a, session, mgr, _AS_OF)
    ids_b = submit_intended_orders([order_b], client_b, session, mgr, _AS_OF)

    assert ids_a == ["alp-a-001"]
    assert ids_b == ["alp-b-001"]

    cid_a = client_a.submit_order.call_args[1]["client_order_id"]
    cid_b = client_b.submit_order.call_args[1]["client_order_id"]
    assert cid_a != cid_b, f"Collision: both used client_order_id={cid_a!r}"
