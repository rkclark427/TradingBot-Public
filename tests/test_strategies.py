from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.strategies.base import Position, StrategyCapabilities, Target
from src.strategies.buy_and_hold import BuyAndHoldSPY

AS_OF = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def strategy() -> BuyAndHoldSPY:
    return BuyAndHoldSPY()


@pytest.fixture
def market_data() -> MagicMock:
    return MagicMock()


def test_capabilities_declared(strategy: BuyAndHoldSPY) -> None:
    caps = strategy.capabilities
    assert isinstance(caps, StrategyCapabilities)
    assert "us_equity" in caps.asset_classes
    assert caps.requires_fractional is True
    assert caps.requires_shorting is False
    assert caps.requires_options is False


def test_initial_allocation_returns_spy_target(
    strategy: BuyAndHoldSPY, market_data: MagicMock
) -> None:
    targets = strategy.generate_targets(
        params={"symbol": "SPY"},
        nav=100.0,
        cash=100.0,
        positions={},
        market_data=market_data,
        universe=["SPY"],
        as_of=AS_OF,
    )
    assert len(targets) == 1
    assert targets[0].symbol == "SPY"
    assert targets[0].target_weight == 1.0
    assert targets[0].rationale["reason"] == "initial allocation"


def test_steady_state_returns_hold_target(
    strategy: BuyAndHoldSPY, market_data: MagicMock
) -> None:
    positions = {"SPY": Position(symbol="SPY", qty=Decimal("0.5"), avg_cost=Decimal("200"))}
    targets = strategy.generate_targets(
        params={"symbol": "SPY"},
        nav=100.0,
        cash=0.0,
        positions=positions,
        market_data=market_data,
        universe=["SPY"],
        as_of=AS_OF,
    )
    assert len(targets) == 1
    assert targets[0].symbol == "SPY"
    assert targets[0].target_weight == 1.0
    assert targets[0].rationale["reason"] == "hold"


def test_market_data_not_called(
    strategy: BuyAndHoldSPY, market_data: MagicMock
) -> None:
    strategy.generate_targets(
        params={"symbol": "SPY"},
        nav=100.0,
        cash=100.0,
        positions={},
        market_data=market_data,
        universe=["SPY"],
        as_of=AS_OF,
    )
    market_data.get_bars.assert_not_called()
    market_data.get_latest_price.assert_not_called()


def test_symbol_param_respected(
    strategy: BuyAndHoldSPY, market_data: MagicMock
) -> None:
    targets = strategy.generate_targets(
        params={"symbol": "QQQ"},
        nav=100.0,
        cash=100.0,
        positions={},
        market_data=market_data,
        universe=["QQQ"],
        as_of=AS_OF,
    )
    assert targets[0].symbol == "QQQ"


def test_target_is_dataclass(
    strategy: BuyAndHoldSPY, market_data: MagicMock
) -> None:
    targets = strategy.generate_targets(
        params={"symbol": "SPY"},
        nav=100.0,
        cash=100.0,
        positions={},
        market_data=market_data,
        universe=["SPY"],
        as_of=AS_OF,
    )
    assert isinstance(targets[0], Target)
