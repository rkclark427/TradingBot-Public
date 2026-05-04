from __future__ import annotations

from src.queries.kill_switch import KILL_SWITCH_PATH, _VALID_MODES


def activate(mode: str = "graceful") -> None:
    """Write the kill switch file with the requested halt mode.

    Args:
        mode: One of 'graceful' (default), 'cancel_open', or 'force'.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"Invalid kill switch mode: {mode!r}. Must be one of {_VALID_MODES}")
    KILL_SWITCH_PATH.write_text(mode)


def deactivate() -> None:
    """Remove the kill switch file, allowing the orchestrator to resume."""
    KILL_SWITCH_PATH.unlink(missing_ok=True)
