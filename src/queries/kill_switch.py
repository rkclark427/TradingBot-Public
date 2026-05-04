from __future__ import annotations

from pathlib import Path

KILL_SWITCH_PATH = Path("kill_switch.flag")


def is_kill_switch_active() -> bool:
    return KILL_SWITCH_PATH.exists()
