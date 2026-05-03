"""Strategy interface and supporting types.

This module defines the contract that every strategy in the library must implement.
The `Strategy` abstract base class is the load-bearing abstraction of the platform —
it has to be general enough to express mean reversion, buy-and-hold, sector rotation,
and future strategies (event-driven, vol premium, congressional disclosure overlay)
without modification.

Design principles encoded here:

1. Strategies are stateless across runs except where explicitly persisted.
2. Strategies do not know their mode (paper/live), capital allocation, or whether
   other strategies are running. Pure isolation.
3. Strategies return the FULL desired portfolio as weighted targets, not deltas.
   A no-op strategy returns its current positions as targets.
4. Strategies do not place orders. The portfolio layer translates targets to
   intended orders; the execution layer places them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

import pandas as pd


# ---------------------------------------------------------------------------
# Capability declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyCapabilities:
    """Static metadata declaring what a strategy needs from the trading account.

    Validated against `AccountCapabilities` at sleeve creation time. A strategy
    whose declared needs exceed what the account supports cannot be instantiated
    as a sleeve — the engine refuses with a clear error rather than failing
    later at order time.

    Attributes:
        asset_classes: Set of Alpaca asset class identifiers needed.
            Examples: {"us_equity"}, {"us_equity", "us_option"}, {"crypto"}.
        requires_shorting: True if the strategy may submit short orders.
        requires_options: True if the strategy trades options contracts.
        requires_fractional: True if the strategy needs fractional share support
            (effectively required for any sleeve under ~$5K).
        min_cash_buffer_pct: Minimum fraction of NAV the strategy expects to
            keep as cash (e.g., 0.05 for 5%). The portfolio layer will enforce.
        rebalance_cadence: How often the strategy expects to be evaluated.
            One of: "daily", "weekly", "monthly", "event". Informational —
            the orchestrator runs every cycle regardless; this just helps
            the strategy know whether it's expected to act.
        typical_holding_period_days: Informational, used in reports.
    """

    asset_classes: frozenset[str]
    requires_shorting: bool
    requires_options: bool
    requires_fractional: bool
    min_cash_buffer_pct: float
    rebalance_cadence: str
    typical_holding_period_days: int

    def __post_init__(self) -> None:
        if self.rebalance_cadence not in {"daily", "weekly", "monthly", "event"}:
            raise ValueError(
                f"Invalid rebalance_cadence: {self.rebalance_cadence!r}. "
                "Must be one of: daily, weekly, monthly, event."
            )
        if not 0.0 <= self.min_cash_buffer_pct <= 1.0:
            raise ValueError(
                f"min_cash_buffer_pct must be in [0, 1], got {self.min_cash_buffer_pct}"
            )
        if self.typical_holding_period_days < 0:
            raise ValueError("typical_holding_period_days must be non-negative")


# ---------------------------------------------------------------------------
# Inputs to strategy.generate_targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    """A current position held by a sleeve.

    This is what the strategy sees when it runs. Cost basis is included for
    strategies that care about realized vs. unrealized P&L for exit decisions
    (e.g., mean reversion's profit target / stop loss logic).
    """

    symbol: str
    qty: Decimal  # signed: positive = long, negative = short
    avg_cost: Decimal  # per share
    market_price: Decimal  # most recent known price
    market_value: Decimal  # qty * market_price (signed)


class MarketDataView(Protocol):
    """Protocol for accessing historical market data from a strategy.

    Implementations live in src/data/. The protocol exists so strategies can
    be tested in isolation against a mock implementation without pulling in
    the full data layer.

    Strategies should not call out to the network from inside generate_targets;
    they should request data through this view, which is responsible for
    caching and batch-fetching.
    """

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Return OHLCV bars for `symbol` over [start, end].

        DataFrame columns: open, high, low, close, volume. Index: tz-aware
        datetime in US/Eastern.
        """
        ...

    def get_latest_price(self, symbol: str) -> Decimal:
        """Return the most recently known price for `symbol`."""
        ...

    def is_within_n_days_of_earnings(self, symbol: str, n: int, as_of: datetime) -> bool:
        """True if `symbol` has an earnings announcement within n trading days
        of `as_of` (in either direction).
        """
        ...


# ---------------------------------------------------------------------------
# Outputs from strategy.generate_targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A desired position expressed as a fraction of sleeve NAV.

    Strategies return a list of these representing the FULL desired portfolio.
    The portfolio layer translates them to share quantities, computes deltas
    against current positions, and produces intended orders.

    Attributes:
        symbol: The instrument identifier (e.g., "AAPL", "SPY").
        target_weight: Desired position as fraction of sleeve NAV.
            Range: typically [-1.0, 1.0]. Negative = short. Sum of absolute
            target_weights across all targets in a list should typically be
            <= 1.0 minus the strategy's min_cash_buffer_pct, but this is not
            enforced here — the portfolio layer handles sizing constraints.
        rationale: Free-form structured data explaining why this target was
            chosen. Logged for debugging and analysis. Conventional keys
            include "signal_type", "score", "z_score", "rsi", "trigger_price",
            but strategies are free to include whatever's useful.
    """

    symbol: str
    target_weight: float
    rationale: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not -2.0 <= self.target_weight <= 2.0:
            raise ValueError(
                f"target_weight {self.target_weight} is outside sane bounds [-2, 2]. "
                "If you intended leverage, use a more explicit mechanism."
            )


# ---------------------------------------------------------------------------
# Strategy ABC
# ---------------------------------------------------------------------------


class Strategy(ABC):
    """Abstract base class for all strategies in the library.

    Concrete strategies live in src/strategies/<name>.py. Each must:
    - Define a class-level `name` attribute (a unique identifier, snake_case)
    - Define a class-level `capabilities` attribute (StrategyCapabilities instance)
    - Implement `generate_targets`

    Strategies are instantiated once per sleeve and reused across cycles. They
    should not hold mutable state between calls to generate_targets unless that
    state is also persisted via the strategy's parameters.
    """

    name: str
    capabilities: StrategyCapabilities

    @abstractmethod
    def generate_targets(
        self,
        params: dict[str, Any],
        nav: Decimal,
        cash: Decimal,
        positions: dict[str, Position],
        market_data: MarketDataView,
        universe: list[str],
        as_of: datetime,
    ) -> list[Target]:
        """Return the desired portfolio for the sleeve as a list of weighted targets.

        Args:
            params: Strategy-specific parameters from the sleeve config.
                Strategies define their own parameter schema; the engine passes
                them through unmodified. A strategy should validate its own
                params and raise ValueError on malformed input.
            nav: Current sleeve NAV (cash + position market value).
            cash: Current sleeve cash. Distinct from NAV for strategies that
                care about how fully invested they are.
            positions: Current sleeve positions, keyed by symbol. Empty dict
                means flat. Use this to decide whether to enter, hold, or exit.
            market_data: Access to historical bars and prices. Implementations
                handle caching; strategies should not worry about efficiency
                of repeated calls.
            universe: List of symbols the strategy may trade. Already filtered
                for tradability and the strategy's capability requirements.
                Strategies may further filter this list internally.
            as_of: The decision timestamp. Strategies should not look at data
                after this time. Always tz-aware.

        Returns:
            A list of Target objects representing the FULL desired portfolio.
            - Empty list = exit everything
            - List containing current positions as targets = hold (no-op)
            - List with new symbols = enter those positions
            - Sum of |target_weight| typically <= 1.0 - min_cash_buffer_pct,
              but the portfolio layer is the final arbiter of sizing.

        Raises:
            ValueError: If params are malformed or required market data is
                missing. The orchestrator will halt the sleeve for this cycle
                and log the error.
        """
        ...
