from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DataConfig(BaseModel):
    alpaca_feed: str = "iex"
    bars_cache_dir: str = "data/bars"


class ExecutionConfig(BaseModel):
    limit_offset_bps: int = 10
    min_order_notional: float = 1.0
    fill_poll_interval_seconds: int = 30


class RiskConfig(BaseModel):
    account_daily_loss_pct: float = 0.05
    account_drawdown_pct: float = 0.20
    account_max_orders_per_day: int = 100
    account_max_orders_per_minute: int = 20
    sleeve_daily_loss_pct: float = 0.03
    sleeve_drawdown_pct: float = 0.15
    sleeve_max_position_pct: float = 0.30
    sleeve_max_order_pct: float = 0.50
    # Default 1.01 (101%) accommodates the limit_offset_bps (default 10bps = 0.1%)
    # plus a small buffer. If you tune execution.limit_offset_bps, tune this too.
    max_order_nav_pct: float = 1.01


class OrchestratorConfig(BaseModel):
    cycle_interval_seconds: int = 60
    market_open_buffer_minutes: int = 5
    market_close_buffer_minutes: int = 15
    drain_timeout_seconds: int = 600  # max time to wait for in-flight orders on graceful halt


class LoggingConfig(BaseModel):
    level: str = "INFO"
    dir: str = "logs"


class SleeveRiskConfig(BaseModel):
    daily_loss_pct: float | None = None
    drawdown_halt_pct: float | None = None


class SleeveConfig(BaseModel):
    id: str
    strategy: str
    mode: Literal["paper", "live"]
    starting_capital: Decimal
    parameters_file: str
    parameter_overrides: dict[str, Any] = Field(default_factory=dict)
    sleeve_risk: SleeveRiskConfig = Field(default_factory=SleeveRiskConfig)
    managed: bool = True
    enabled: bool = True


class SleeveRegistry(BaseModel):
    sleeves: list[SleeveConfig] = Field(default_factory=list)


class StrategyParameters(BaseModel):
    model_config = {"extra": "allow"}

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    alpaca_paper_api_key: str = ""
    alpaca_paper_api_secret: str = ""
    alpaca_live_api_key: str = ""
    alpaca_live_api_secret: str = ""
    pushover_user_key: str = ""
    pushover_api_token: str = ""
    db_path: str = "trading_bot.db"


class BaseConfig(BaseModel):
    env: str = "development"
    data: DataConfig = Field(default_factory=DataConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    secrets: Secrets = Field(default_factory=Secrets)
    sleeves: SleeveRegistry = Field(default_factory=SleeveRegistry)
