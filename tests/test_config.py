from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config.loader import load_config, load_strategy_parameters
from src.config.models import SleeveConfig, SleeveRegistry

CONFIG_DIR = Path(__file__).parent.parent / "config"


def test_load_base_config():
    config = load_config(CONFIG_DIR)
    assert config.env == "development"
    assert config.execution.limit_offset_bps == 10
    assert config.risk.account_daily_loss_pct == 0.05
    assert config.orchestrator.cycle_interval_seconds == 60


def test_load_empty_sleeve_registry():
    config = load_config(CONFIG_DIR)
    assert config.sleeves.sleeves == []


def test_sleeve_config_validates():
    sleeve = SleeveConfig(
        id="test_sleeve",
        strategy="buy_and_hold",
        mode="paper",
        starting_capital=Decimal("100"),
        parameters_file="config/strategies/buy_and_hold.yaml",
    )
    assert sleeve.managed is True
    assert sleeve.enabled is True
    assert sleeve.starting_capital == Decimal("100")


def test_sleeve_config_rejects_invalid_mode():
    with pytest.raises(ValidationError):
        SleeveConfig(
            id="bad",
            strategy="buy_and_hold",
            mode="invalid",
            starting_capital=Decimal("100"),
            parameters_file="config/strategies/buy_and_hold.yaml",
        )


def test_sleeve_config_requires_id():
    with pytest.raises(ValidationError):
        SleeveConfig(  # type: ignore[call-arg]
            strategy="buy_and_hold",
            mode="paper",
            starting_capital=Decimal("100"),
            parameters_file="config/strategies/buy_and_hold.yaml",
        )


def test_env_var_overrides_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "test_key_123")
    monkeypatch.setenv("ALPACA_PAPER_API_SECRET", "test_secret_456")
    config = load_config(CONFIG_DIR)
    assert config.secrets.alpaca_paper_api_key == "test_key_123"
    assert config.secrets.alpaca_paper_api_secret == "test_secret_456"


def test_strategy_parameters_buy_and_hold():
    params = load_strategy_parameters(CONFIG_DIR / "strategies" / "buy_and_hold.yaml")
    assert params.as_dict()["symbol"] == "SPY"


def test_strategy_parameters_mean_reversion():
    params = load_strategy_parameters(CONFIG_DIR / "strategies" / "mean_reversion.yaml")
    d = params.as_dict()
    assert d["lookback_days"] == 5
    assert d["entry_zscore_threshold"] == -1.5


def test_invalid_yaml_raises_clearly(tmp_path: Path) -> None:
    bad_config_dir = tmp_path
    (bad_config_dir / "base.yaml").write_text("env: development\nrisk:\n  account_daily_loss_pct: not_a_number\n")
    (bad_config_dir / "sleeves.yaml").write_text("sleeves: []\n")
    with pytest.raises(ValidationError):
        load_config(bad_config_dir)
