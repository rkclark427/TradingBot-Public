from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.sleeves.manager import SleeveManager, SleeveNotFoundError
from src.sleeves.types import Sleeve


def create_sleeve(
    manager: SleeveManager,
    strategy: str,
    mode: str,
    capital: Decimal,
    params: dict[str, Any] | None = None,
    sleeve_id: str | None = None,
    managed: bool = False,
) -> Sleeve:
    return manager.create_sleeve(
        strategy_name=strategy,
        mode=mode,
        starting_capital=capital,
        params=params or {},
        sleeve_id=sleeve_id,
        managed=managed,
    )


def stop_sleeve(manager: SleeveManager, sleeve_id: str) -> Sleeve:
    return manager.stop_sleeve(sleeve_id)


def pause_sleeve(manager: SleeveManager, sleeve_id: str) -> Sleeve:
    return manager.pause_sleeve(sleeve_id)


def resume_sleeve(manager: SleeveManager, sleeve_id: str) -> Sleeve:
    return manager.resume_sleeve(sleeve_id)
