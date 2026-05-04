from __future__ import annotations

from pathlib import Path

KILL_SWITCH_PATH = Path("kill_switch.flag")

_VALID_MODES = ("graceful", "cancel_open", "force")


def is_kill_switch_active() -> bool:
    return KILL_SWITCH_PATH.exists()


def get_kill_switch_mode() -> str | None:
    """Return the halt mode if the kill switch is active, else None.

    Reads the flag file content to determine mode:
      'graceful'    — drain in-flight orders then exit cleanly (default)
      'cancel_open' — cancel all open Alpaca orders, then drain and exit
      'force'       — exit immediately with no cleanup

    If the file exists but contains an unrecognised value, defaults to 'graceful'.
    """
    if not KILL_SWITCH_PATH.exists():
        return None
    content = KILL_SWITCH_PATH.read_text().strip()
    return content if content in _VALID_MODES else "graceful"
