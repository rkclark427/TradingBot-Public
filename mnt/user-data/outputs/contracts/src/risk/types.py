"""Risk layer types and the RiskCheck protocol.

Risk checks are pure functions (well, methods on stateless objects) that
look at an intended order or net order plus relevant context, and return
a CheckResult indicating whether the order should proceed.

Risk operates at two levels:
- Account-level: applies across all sleeves combined (master safety)
- Sleeve-level: applies to one sleeve (per-strategy discipline)

Account-level checks: total daily loss, total drawdown, account order rate,
reconciliation mismatch, kill switch.

Sleeve-level checks: sleeve daily loss, sleeve drawdown, sleeve concentration,
sleeve order rate.

Both levels also include per-order sanity checks: order size sanity, symbol
tradability, capability match.

When any account-level check fails with severity HALT, all new orders across
all sleeves are blocked. When a sleeve-level check fails with severity HALT,
only that sleeve's new orders are blocked. INFO and WARN severities log but
do not block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol
from uuid import UUID


class RiskLevel(str, Enum):
    """Whether a risk check operates at account or sleeve level."""

    ACCOUNT = "account"
    SLEEVE = "sleeve"


class RiskSeverity(str, Enum):
    """How a failed risk check should be treated.

    INFO: log it, no action taken
    WARN: log it, alert if appropriate, allow the order
    HALT: log it, alert, block the order. If the check is account-level,
          block ALL new orders. If sleeve-level, block this sleeve's orders.
    """

    INFO = "info"
    WARN = "warn"
    HALT = "halt"


@dataclass(frozen=True)
class CheckResult:
    """The result of running a single risk check.

    Attributes:
        passed: True if the order may proceed.
        severity: How a failure should be treated. Ignored when passed=True.
        event_type: Short identifier for the check. Used as the risk_events.event_type
            value when persisted. e.g., "sleeve_daily_loss", "order_size_sanity".
        message: Human-readable explanation. Surfaced in alerts and logs.
        context: Additional structured data for logging and analysis.
    """

    passed: bool
    severity: RiskSeverity
    event_type: str
    message: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, event_type: str = "passed") -> CheckResult:
        """Convenience constructor for a passing check."""
        return cls(passed=True, severity=RiskSeverity.INFO, event_type=event_type)

    @classmethod
    def fail(
        cls,
        event_type: str,
        message: str,
        severity: RiskSeverity = RiskSeverity.HALT,
        context: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Convenience constructor for a failing check."""
        return cls(
            passed=False,
            severity=severity,
            event_type=event_type,
            message=message,
            context=context or {},
        )


class RiskCheck(Protocol):
    """Protocol for any risk check.

    Implementations live in src/risk/checks/. Each check is a callable that
    takes a context object and returns a CheckResult. Checks should be
    stateless and side-effect-free; persistence of risk events is handled
    by the risk layer's runner, not by individual checks.
    """

    @property
    def level(self) -> RiskLevel:
        """Whether this check operates at account or sleeve level."""
        ...

    @property
    def name(self) -> str:
        """Short name for the check, used in logs."""
        ...

    def __call__(self, context: RiskContext) -> CheckResult:
        """Evaluate the check against the given context."""
        ...


@dataclass
class RiskContext:
    """Bundle of state passed to risk checks.

    Not all checks use all fields. The bundle exists so checks have a uniform
    signature and we can add fields without breaking existing checks.

    The RiskContext is constructed by the risk layer runner before invoking
    checks. It snapshots the relevant state at the moment the order is being
    evaluated, so checks see consistent data even if reality changes mid-cycle.

    Attributes:
        kill_switch_set: True if the manual kill switch is active.
        account_daily_pnl_pct: Today's realized + unrealized P&L on the
            live account, as a fraction (e.g., -0.025 for -2.5%).
        account_drawdown_pct: Current account-level drawdown from peak.
        account_orders_today: Count of net orders submitted to live today.
        account_orders_last_minute: Count of net orders in the last 60 seconds.
        sleeve_id: ID of the sleeve being checked. None for purely
            account-level checks.
        sleeve_nav: Current NAV of the sleeve. None for account-level.
        sleeve_daily_pnl_pct: Sleeve's intraday P&L. None for account-level.
        sleeve_drawdown_pct: Sleeve's drawdown from HWM. None for account-level.
        sleeve_orders_today: Count of orders this sleeve has submitted today.
        sleeve_orders_last_minute: Count in last minute for this sleeve.
        order_symbol: The symbol being ordered (for per-order checks).
        order_notional: Notional value of the order in dollars.
        existing_position_qty: Existing position in the symbol for this sleeve
            (signed). 0 if no position.
        symbol_tradable: True if the symbol is currently tradable per the
            asset universe.
        symbol_fractionable: True if fractional orders are allowed for this symbol.
        symbol_shortable: True if the symbol is shortable today.
    """

    kill_switch_set: bool = False

    # Account-level state
    account_daily_pnl_pct: float = 0.0
    account_drawdown_pct: float = 0.0
    account_orders_today: int = 0
    account_orders_last_minute: int = 0

    # Sleeve-level state
    sleeve_id: UUID | None = None
    sleeve_nav: float | None = None
    sleeve_daily_pnl_pct: float | None = None
    sleeve_drawdown_pct: float | None = None
    sleeve_orders_today: int | None = None
    sleeve_orders_last_minute: int | None = None

    # Per-order state
    order_symbol: str | None = None
    order_notional: float | None = None
    existing_position_qty: float | None = None
    symbol_tradable: bool = True
    symbol_fractionable: bool = True
    symbol_shortable: bool = False
