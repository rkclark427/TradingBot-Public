from __future__ import annotations

from src.queries.kill_switch import KILL_SWITCH_PATH


def activate() -> None:
    """Create the kill switch file, halting order submission on the next cycle."""
    KILL_SWITCH_PATH.touch()


def deactivate() -> None:
    """Remove the kill switch file, allowing the orchestrator to resume."""
    KILL_SWITCH_PATH.unlink(missing_ok=True)
