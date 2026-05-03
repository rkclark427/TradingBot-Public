"""Sleeve types and capability validation.

A sleeve is a runtime instance of a strategy with its own mode, capital allocation,
parameters, and state. The Sleeve Manager (src/sleeves/manager.py) handles lifecycle
and persistence; this module defines the value types and pure-function validators
that the manager uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from src.strategies.base import StrategyCapabilities


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Mode(str, Enum):
    """Trading environment for a sleeve.

    PAPER routes orders to Alpaca's paper account.
    LIVE routes orders to Alpaca's live account.

    Inherits from str so it serializes naturally in JSON/YAML and can be
    compared to string literals.
    """

    PAPER = "paper"
    LIVE = "live"


class SleeveStatus(str, Enum):
    """Lifecycle status of a sleeve.

    State transitions:
      RUNNING <-> PAUSED  (manual pause/resume; sleeve-level halts also pause)
      RUNNING -> STOPPING -> STOPPED  (stop liquidates positions, then transitions)

    PAUSED sleeves do not generate signals or submit new entry orders, but
    existing positions can still be exited (e.g., to honor stop losses).
    STOPPING sleeves are in the process of liquidating; the orchestrator
    submits exit orders for all positions, transitioning to STOPPED when
    flat.
    STOPPED sleeves are permanently inactive; their capital has returned to
    the unallocated pool.
    """

    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"


# ---------------------------------------------------------------------------
# Configuration types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SleeveRiskConfig:
    """Per-sleeve risk parameters.

    Defaults are conservative; sleeves can override via config. Account-level
    risk controls are configured separately in base.yaml and apply across
    all sleeves.

    Attributes:
        daily_loss_pct: Sleeve pauses for the day if intraday P&L drops
            below -daily_loss_pct * sleeve_nav. Resets at midnight ET.
            Default 0.03 (3%).
        drawdown_halt_pct: Sleeve pauses (manual reset required) if NAV drops
            below (1 - drawdown_halt_pct) * sleeve_high_water_mark.
            Default 0.15 (15%).
        max_position_concentration_pct: No single position may exceed this
            fraction of sleeve NAV. Default 0.30 (30%).
        max_orders_per_day: Per-sleeve order rate limit. Default 50.
        max_orders_per_minute: Per-sleeve burst limit. Default 5.
    """

    daily_loss_pct: float = 0.03
    drawdown_halt_pct: float = 0.15
    max_position_concentration_pct: float = 0.30
    max_orders_per_day: int = 50
    max_orders_per_minute: int = 5


# ---------------------------------------------------------------------------
# Sleeve runtime state
# ---------------------------------------------------------------------------


@dataclass
class Sleeve:
    """A runtime instance of a strategy with its own state.

    This is a value object representing the sleeve at a point in time. It
    mirrors the `sleeves` row in the database (see src/tracking/models.py).
    The Sleeve Manager produces these from the database; mutations go back
    through the manager rather than mutating these instances directly.

    Attributes:
        id: Unique identifier. UUID generated at creation.
        strategy_name: Name of the strategy class (matches Strategy.name).
        mode: PAPER or LIVE — determines which Alpaca environment is used.
        status: Current lifecycle status.
        starting_capital: Bankroll allocated at sleeve creation. Immutable.
        current_nav: Current sleeve NAV. Updated as fills land and positions
            mark to market. This is what the strategy sees.
        high_water_mark: Peak NAV the sleeve has ever reached. Used to
            compute drawdown for the sleeve-level drawdown halt.
        parameters: Strategy-specific parameters. Passed to
            Strategy.generate_targets unmodified.
        risk: Per-sleeve risk configuration.
        managed: True if YAML is canonical (CLI changes are temporary,
            restored from YAML on restart). False if DB is canonical.
            See architecture.md section 8.2.
        created_at: When the sleeve was created. tz-aware.
        last_signaled_at: When the strategy last produced targets. None until
            first cycle.
    """

    id: UUID
    strategy_name: str
    mode: Mode
    status: SleeveStatus
    starting_capital: Decimal
    current_nav: Decimal
    high_water_mark: Decimal
    parameters: dict[str, Any] = field(default_factory=dict)
    risk: SleeveRiskConfig = field(default_factory=SleeveRiskConfig)
    managed: bool = False
    created_at: datetime | None = None
    last_signaled_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        """True if the sleeve should be evaluated by the orchestrator."""
        return self.status == SleeveStatus.RUNNING

    @property
    def can_enter(self) -> bool:
        """True if the sleeve may submit new entry orders.

        RUNNING sleeves can enter. PAUSED sleeves cannot enter but can exit.
        """
        return self.status == SleeveStatus.RUNNING

    @property
    def can_exit(self) -> bool:
        """True if the sleeve may submit exit orders.

        RUNNING, PAUSED, and STOPPING sleeves can exit. Only STOPPED cannot.
        """
        return self.status != SleeveStatus.STOPPED

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown from high-water mark, as a positive fraction.

        Returns 0.0 if NAV >= HWM. Returns e.g. 0.12 if NAV is 12% below HWM.
        """
        if self.high_water_mark <= 0:
            return 0.0
        if self.current_nav >= self.high_water_mark:
            return 0.0
        return float(
            (self.high_water_mark - self.current_nav) / self.high_water_mark
        )


# ---------------------------------------------------------------------------
# Account capabilities (what the Alpaca account can do)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountCapabilities:
    """What the Alpaca account is permitted to do.

    Discovered on bot startup and refreshed daily. Used to validate strategy
    capability requirements at sleeve creation time.

    Attributes:
        asset_classes: Asset classes enabled on the account.
        options_trading_level: 0 (none), 1, 2, 3 — Alpaca's options approval
            tiers. 0 means no options trading.
        fractional_shares_enabled: True if fractional share orders are
            supported on the account.
        shorting_enabled: True if the account can sell short.
        is_pdt: True if the account is flagged as Pattern Day Trader.
            Affects day-trade rules.
        buying_power: Total buying power available right now. For attribution
            purposes — sleeves operate against their own NAV, but this is the
            ceiling.
        cash: Cash balance. Distinct from buying power, which can include
            margin.
        as_of: When this snapshot was taken. tz-aware.
    """

    asset_classes: frozenset[str]
    options_trading_level: int
    fractional_shares_enabled: bool
    shorting_enabled: bool
    is_pdt: bool
    buying_power: Decimal
    cash: Decimal
    as_of: datetime


# ---------------------------------------------------------------------------
# Capability validation (pure function — no side effects)
# ---------------------------------------------------------------------------


class CapabilityMismatchError(Exception):
    """Raised when a strategy's capability requirements exceed account capabilities."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__(
            "Strategy capabilities not satisfied by account:\n"
            + "\n".join(f"  - {r}" for r in reasons)
        )


def validate_strategy_against_account(
    strategy_caps: StrategyCapabilities,
    account_caps: AccountCapabilities,
) -> None:
    """Validate that an account can support a strategy's requirements.

    Raises CapabilityMismatchError with a list of all mismatch reasons (not
    just the first) so the operator sees the full picture.

    Called at sleeve creation, before any state is persisted. The sleeve is
    not created if validation fails.
    """
    reasons: list[str] = []

    missing_asset_classes = strategy_caps.asset_classes - account_caps.asset_classes
    if missing_asset_classes:
        reasons.append(
            f"Strategy requires asset classes {sorted(missing_asset_classes)}, "
            f"account only supports {sorted(account_caps.asset_classes)}"
        )

    if strategy_caps.requires_shorting and not account_caps.shorting_enabled:
        reasons.append("Strategy requires shorting; account does not have shorting enabled")

    if strategy_caps.requires_options and account_caps.options_trading_level < 1:
        reasons.append(
            "Strategy requires options trading; account has options_trading_level=0 "
            "(no options approval)"
        )

    if strategy_caps.requires_fractional and not account_caps.fractional_shares_enabled:
        reasons.append(
            "Strategy requires fractional shares; account does not have fractional enabled"
        )

    if reasons:
        raise CapabilityMismatchError(reasons)
