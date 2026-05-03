"""Pydantic schemas for YAML configuration files.

Two configuration files at the engine level:
- config/base.yaml — engine defaults, account-level risk, monitoring, execution
- config/sleeves.yaml — declarative sleeve registry

Plus per-strategy parameter files in config/strategies/, which are validated
by the strategies themselves (each strategy defines its own parameter schema).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.sleeves.types import Mode


# ---------------------------------------------------------------------------
# base.yaml
# ---------------------------------------------------------------------------


class DataConfig(BaseModel):
    """Data layer configuration."""

    model_config = ConfigDict(extra="forbid")

    cache_dir: Path = Field(default=Path("data/cache"))
    universe_refresh_cadence_days: int = Field(default=7, ge=1)
    asset_list_refresh_cadence_hours: int = Field(default=24, ge=1)
    capabilities_refresh_cadence_hours: int = Field(default=24, ge=1)
    bars_provider: str = Field(default="alpaca")
    earnings_provider: str = Field(default="finnhub")


class AccountRiskConfig(BaseModel):
    """Account-level risk parameters. Apply across all sleeves combined.

    Sleeve-level risk is configured per-sleeve in sleeves.yaml.
    """

    model_config = ConfigDict(extra="forbid")

    daily_loss_pct: float = Field(default=0.05, ge=0.0, le=1.0)
    drawdown_halt_pct: float = Field(default=0.20, ge=0.0, le=1.0)
    max_orders_per_day: int = Field(default=100, ge=1)
    max_orders_per_minute: int = Field(default=20, ge=1)


class ExecutionConfig(BaseModel):
    """Execution layer defaults."""

    model_config = ConfigDict(extra="forbid")

    limit_offset_bps: float = Field(default=10.0, ge=0.0, le=500.0)
    """Limit price offset from previous close, in basis points.
    Buys: limit = prev_close * (1 + offset). Sells: limit = prev_close * (1 - offset).
    """

    cancel_unfilled_after_minutes: int = Field(default=30, ge=1)
    """Cancel limit orders not filled within N minutes after market open."""

    min_order_notional: Decimal = Field(default=Decimal("1.00"))
    """Skip intended orders smaller than this dollar amount (avoid dust)."""


class MonitoringConfig(BaseModel):
    """Monitoring and alerting configuration."""

    model_config = ConfigDict(extra="forbid")

    heartbeat_interval_seconds: int = Field(default=60, ge=10)
    heartbeat_stale_alert_seconds: int = Field(default=300, ge=30)
    daily_summary_email_enabled: bool = Field(default=True)
    daily_summary_email_to: str | None = None
    pushover_enabled: bool = Field(default=False)
    """Pushover keys come from environment variables, not config files."""


class CycleConfig(BaseModel):
    """Orchestrator cycle configuration."""

    model_config = ConfigDict(extra="forbid")

    interval_seconds: int = Field(default=60, ge=10)
    """How often the orchestrator runs through all sleeves during market hours."""

    market_open_buffer_minutes: int = Field(default=2, ge=0)
    """Wait this many minutes after market open before submitting orders.
    Avoids the chaotic first minute of trading."""


class BaseConfig(BaseModel):
    """Root config from base.yaml."""

    model_config = ConfigDict(extra="forbid")

    data: DataConfig = Field(default_factory=DataConfig)
    account_risk: AccountRiskConfig = Field(default_factory=AccountRiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    cycle: CycleConfig = Field(default_factory=CycleConfig)
    timezone: str = Field(default="America/New_York")


# ---------------------------------------------------------------------------
# sleeves.yaml
# ---------------------------------------------------------------------------


class SleeveRiskOverrides(BaseModel):
    """Per-sleeve risk parameter overrides. All optional."""

    model_config = ConfigDict(extra="forbid")

    daily_loss_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    drawdown_halt_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    max_position_concentration_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    max_orders_per_day: int | None = Field(default=None, ge=1)
    max_orders_per_minute: int | None = Field(default=None, ge=1)


class SleeveDeclaration(BaseModel):
    """One sleeve as declared in sleeves.yaml."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    """Stable identifier. Should be human-readable and snake_case.
    e.g., 'mr_live_main', 'spy_benchmark'.
    The persisted UUID in the database is separate; this is the operational
    handle used in CLI commands and YAML."""

    strategy: str = Field(min_length=1, max_length=64)
    """Name of the Strategy class. Must match a registered strategy."""

    mode: Mode

    starting_capital: Decimal = Field(gt=Decimal("0"))

    parameters_file: Path | None = None
    """Path to the strategy's parameter file. If None, strategy defaults are used."""

    parameter_overrides: dict = Field(default_factory=dict)
    """Inline parameter overrides applied on top of parameters_file."""

    sleeve_risk: SleeveRiskOverrides = Field(default_factory=SleeveRiskOverrides)
    """Per-sleeve risk overrides. Defaults from src.sleeves.types.SleeveRiskConfig."""

    managed: bool = Field(default=True)
    """If True, YAML is canonical for this sleeve (CLI changes are temporary,
    restored from YAML on restart). If False, DB is canonical.
    See architecture.md section 8.2."""

    enabled: bool = Field(default=True)
    """If False, the sleeve is loaded but not run. Useful for temporarily
    disabling a sleeve without removing its config."""

    @field_validator("id")
    @classmethod
    def _id_is_snake_case(cls, v: str) -> str:
        if not all(c.isalnum() or c == "_" for c in v):
            raise ValueError(
                f"Sleeve id {v!r} must contain only alphanumerics and underscores"
            )
        return v


class SleevesRegistry(BaseModel):
    """Root config from sleeves.yaml."""

    model_config = ConfigDict(extra="forbid")

    sleeves: list[SleeveDeclaration] = Field(default_factory=list)

    @field_validator("sleeves")
    @classmethod
    def _ids_are_unique(cls, v: list[SleeveDeclaration]) -> list[SleeveDeclaration]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            duplicates = {x for x in ids if ids.count(x) > 1}
            raise ValueError(f"Duplicate sleeve ids: {sorted(duplicates)}")
        return v
