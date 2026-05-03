"""Order types used between the portfolio, risk, and execution layers.

These are in-memory value types passed between layers. Persistence happens
via the SQLAlchemy models in src/tracking/models.py.

Flow:
    Strategy.generate_targets()  ->  list[Target]
                                          |
                                          v
    Portfolio.targets_to_intended_orders()  ->  list[IntendedOrder]
                                                     |
                                                     v
    Execution.net_intended_orders()  ->  list[NetOrder] + mapping
                                              |
                                              v
    Risk.check_each(net_orders)  ->  approved/rejected
                                              |
                                              v
    AlpacaClient.submit_order()  ->  fills come back  ->  attribution
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID


class Side(str, Enum):
    """Order side. Inherits from str for natural serialization."""

    BUY = "buy"
    SELL = "sell"

    @classmethod
    def from_qty_delta(cls, delta: Decimal) -> Side:
        """Side implied by a signed quantity delta. Convenience for portfolio layer."""
        return cls.BUY if delta > 0 else cls.SELL


@dataclass(frozen=True)
class IntendedOrder:
    """A sleeve's pre-netting desired trade.

    Produced by the portfolio layer from one strategy target. Each intended
    order is tagged with its originating sleeve_id so attribution survives
    netting.

    Attributes:
        sleeve_id: The sleeve that wants this trade.
        symbol: Ticker.
        side: BUY or SELL.
        qty: Always positive. Side determines direction.
        limit_price: Limit price for the order.
        rationale: Forwarded from the originating Target's rationale, plus
            portfolio-layer additions (e.g., the math used to size).
    """

    sleeve_id: UUID
    symbol: str
    side: Side
    qty: Decimal
    limit_price: Decimal
    rationale: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"IntendedOrder qty must be positive, got {self.qty}")
        if self.limit_price <= 0:
            raise ValueError(f"IntendedOrder limit_price must be positive, got {self.limit_price}")

    @property
    def signed_qty(self) -> Decimal:
        """Quantity with sign reflecting side. Positive = buy, negative = sell.
        Useful for netting math."""
        return self.qty if self.side == Side.BUY else -self.qty

    @property
    def notional(self) -> Decimal:
        """Notional value (qty * limit_price). Always positive."""
        return self.qty * self.limit_price


@dataclass(frozen=True)
class NetOrder:
    """A post-netting order to be submitted to Alpaca.

    Attributes:
        symbol: Ticker.
        side: BUY or SELL (determined by the sign of net_qty).
        net_qty: Always positive. Side determines direction.
        limit_price: Limit price for the order. The portfolio layer chooses
            this; netting does not change it (all constituent intended orders
            on the same symbol/side use the same limit price).
        client_order_id: Idempotent identifier sent to Alpaca.
            Format: f"{date}-{sleeve_id_or_net}-{symbol}-{seq}"
        constituent_intent_ids: IDs of the intended orders that were rolled
            into this net order. Empty if this is a single-sleeve order
            (still recorded for uniformity — the database mapping table
            always has at least one row).
    """

    symbol: str
    side: Side
    net_qty: Decimal
    limit_price: Decimal
    client_order_id: str
    constituent_intent_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.net_qty <= 0:
            raise ValueError(f"NetOrder net_qty must be positive, got {self.net_qty}")
        if self.limit_price <= 0:
            raise ValueError(f"NetOrder limit_price must be positive, got {self.limit_price}")
        if not self.client_order_id:
            raise ValueError("NetOrder requires a non-empty client_order_id")


@dataclass(frozen=True)
class FillEvent:
    """A fill received from Alpaca against a net order.

    Used internally before being persisted to the fills table and allocated
    to sleeves via SleeveFillModel.

    Attributes:
        net_order_db_id: The net_orders.id row this fill applies to.
        timestamp: When Alpaca reports the fill occurred.
        qty: Quantity filled in this event (always positive). Multiple
            FillEvents may apply to one net order in case of partial fills.
        fill_price: Average price for this fill event.
        fees: Fees for this fill (typically 0 on Alpaca, but recorded).
    """

    net_order_db_id: int
    timestamp: datetime
    qty: Decimal
    fill_price: Decimal
    fees: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"FillEvent qty must be positive, got {self.qty}")
        if self.fill_price <= 0:
            raise ValueError(f"FillEvent fill_price must be positive, got {self.fill_price}")
