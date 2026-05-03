from __future__ import annotations

from pathlib import Path

import yaml

from src.config.models import BaseConfig, Secrets, SleeveRegistry, StrategyParameters


def load_config(config_dir: Path | str = "config") -> BaseConfig:
    config_dir = Path(config_dir)

    with open(config_dir / "base.yaml") as f:
        raw = yaml.safe_load(f) or {}

    sleeves = _load_sleeve_registry(config_dir)
    secrets = Secrets()

    return BaseConfig(**raw, secrets=secrets, sleeves=sleeves)


def _load_sleeve_registry(config_dir: Path) -> SleeveRegistry:
    path = config_dir / "sleeves.yaml"
    with open(path) as f:
        raw = yaml.safe_load(f)
    return SleeveRegistry(**(raw if isinstance(raw, dict) else {}))


def load_strategy_parameters(params_file: Path | str) -> StrategyParameters:
    with open(Path(params_file)) as f:
        raw = yaml.safe_load(f) or {}
    return StrategyParameters(**raw)
