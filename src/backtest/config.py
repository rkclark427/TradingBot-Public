"""Pydantic models for backtest configuration YAML.

Schema matches the format documented in BacktestHarness_Specification_Phase2_v1.0.md §7.1.

Usage::

    import yaml
    from src.backtest.config import BacktestConfig

    with open("backtest_configs/momentum_2018_baseline.yaml") as f:
        raw = yaml.safe_load(f)
    config = BacktestConfig.model_validate(raw)
"""

from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------


class StrategyConfig(BaseModel):
    """Strategy class name and parameter overrides."""

    model_config = ConfigDict(populate_by_name=True)

    # 'class' is a Python keyword, so we alias it
    class_name: str = Field(alias="class")
    parameters: dict[str, Any] = Field(default_factory=dict)


class SimulationConfig(BaseModel):
    """Simulation time range and cost assumptions."""

    start_date: date
    end_date: date | None = None         # None = use latest date available in cache
    starting_capital: Decimal = Decimal("10000")
    cash_annualized_rate: float = 0.04
    slippage_bps: float = 5.0


class OutputConfig(BaseModel):
    """Where and in what format to write report artifacts."""

    directory: Path = Path("backtests/latest")
    format: str = "html"


class BacktestConfig(BaseModel):
    """Top-level backtest configuration."""

    strategy: StrategyConfig
    benchmarks: list[str] = Field(default_factory=lambda: ["BuyAndHoldSPY"])
    simulation: SimulationConfig
    output: OutputConfig = Field(default_factory=OutputConfig)
    cache_db: Path = Path("data/backtest_cache.db")


# ---------------------------------------------------------------------------
# Strategy registry + instantiation
# ---------------------------------------------------------------------------

# Maps YAML class name → (module, class_attr).
# Add new strategies here as they are added to the platform.
_STRATEGY_REGISTRY: dict[str, tuple[str, str]] = {
    "MomentumContinuation": (
        "src.strategies.momentum_continuation",
        "MomentumContinuation",
    ),
    "BuyAndHoldSPY": (
        "src.strategies.buy_and_hold",
        "BuyAndHoldSPY",
    ),
    "ShortTermMeanReversion": (
        "src.strategies.short_term_mean_reversion",
        "ShortTermMeanReversion",
    ),
}

# Maps strategy class name → list of tradeable symbols.
# Used by the CLI to determine the universe for both the strategy and benchmarks.
UNIVERSE_MAP: dict[str, list[str]] = {
    "MomentumContinuation": sorted([
        "XLE", "XLK", "XLF", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
        "SPY", "QQQ", "IWM", "EFA", "EEM",
    ]),
    "BuyAndHoldSPY": ["SPY"],
    "ShortTermMeanReversion": sorted([
        "AAPL", "MSFT", "GOOGL", "GOOG", "META", "NVDA", "AVGO", "TXN", "QCOM", "IBM",
        "ORCL", "ACN", "CSCO", "INTC", "AMD",
        "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "TGT", "LOW", "BKNG", "F",
        "WMT", "PG", "KO", "PEP", "COST", "CL", "MO", "PM", "EL",
        "JNJ", "UNH", "PFE", "MRK", "ABBV", "TMO", "ABT", "MDT", "BMY", "AMGN",
        "GILD", "CVS",
        "BRK-B", "JPM", "BAC", "WFC", "GS", "MS", "BLK", "AXP", "USB", "C",
        "MMC", "CB",
        "HON", "UPS", "BA", "CAT", "DE", "MMM", "GE", "LMT", "RTX", "FDX",
        "XOM", "CVX", "COP", "SLB", "EOG",
        "LIN", "APD", "NEE", "DUK", "SO", "AMT", "PLD",
        "VZ", "T", "DIS", "CMCSA", "NFLX",
    ]),
}


def instantiate_strategy(class_name: str) -> Any:
    """Return a fresh instance of the named strategy class.

    Args:
        class_name: One of the keys in _STRATEGY_REGISTRY (e.g. "MomentumContinuation").

    Raises:
        ValueError: If class_name is not in the registry.
    """
    if class_name not in _STRATEGY_REGISTRY:
        known = list(_STRATEGY_REGISTRY)
        raise ValueError(f"Unknown strategy class {class_name!r}. Known: {known}")

    mod_name, cls_attr = _STRATEGY_REGISTRY[class_name]
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_attr)
    return cls()
