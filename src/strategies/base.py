from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class StrategyCapabilities:
    asset_classes: frozenset[str]
    requires_shorting: bool
    requires_options: bool
    requires_fractional: bool
    min_cash_buffer_pct: float
    rebalance_cadence: str
    typical_holding_period_days: int


@dataclass
class Target:
    symbol: str
    target_weight: float
    rationale: dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    symbol: str
    qty: Decimal
    avg_cost: Decimal

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    @property
    def is_short(self) -> bool:
        return self.qty < 0


@runtime_checkable
class MarketDataView(Protocol):
    def get_bars(
        self,
        symbol: str,
        lookback_days: int,
        as_of: datetime,
    ) -> Any:
        """Return a DataFrame of daily bars for *symbol* ending on *as_of*."""
        ...

    def get_latest_price(self, symbol: str) -> Decimal:
        """Return the most recent price for *symbol*."""
        ...


class Strategy(ABC):
    name: str
    capabilities: StrategyCapabilities

    @abstractmethod
    def generate_targets(  # noqa: B027
        self,
        params: dict[str, Any],
        nav: float,
        cash: float,
        positions: dict[str, Position],
        market_data: MarketDataView,
        universe: list[str],
        as_of: datetime,
        position_state: dict[str, dict[str, Any]] | None = None,
    ) -> list[Target]:
        """Generate target weights for the strategy's positions.

        position_state is keyed by symbol and carries per-position data the
        orchestrator computes from the fills/price history: entry_date,
        days_held, highest_close_since_entry. Strategies that don't need it
        (BuyAndHoldSPY) can ignore it. The orchestrator owns reading and
        writing this state; strategies treat it as read-only input.
        """
        ...
