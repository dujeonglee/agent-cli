"""Readline-based input history for chat REPL.

Enables arrow key navigation, persistent history across sessions,
and standard readline editing shortcuts (Ctrl+A/E/W/K).
"""

from __future__ import annotations

import atexit
import readline
from pathlib import Path

_HISTORY_FILE = Path.home() / ".agent-cli" / "chat_history"
_MAX_HISTORY = 1000
_initialized = False
_is_libedit = "libedit" in (readline.__doc__ or "")


def setup() -> None:
    """Initialize readline and load persistent history."""
    global _initialized
    if _initialized:
        return

    # macOS ships libedit which needs different bind syntax
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")

    readline.set_history_length(_MAX_HISTORY)

    if _HISTORY_FILE.is_file():
        try:
            readline.read_history_file(str(_HISTORY_FILE))
        except OSError:
            pass  # corrupted or permission error — start fresh

    atexit.register(save)
    _initialized = True


def make_prompt(text: str, ansi_start: str, ansi_end: str = "\033[0m") -> str:
    """Build a readline-safe colored prompt.

    GNU readline uses \\001/\\002 to mark non-printing chars so it can
    calculate prompt width correctly.  macOS libedit does NOT support
    these markers — they corrupt the escape sequence and break colors.
    """
    if _is_libedit:
        return f"{ansi_start}{text}{ansi_end} "
    return f"\001{ansi_start}\002{text}\001{ansi_end}\002 "


def save() -> None:
    """Write current history to disk."""
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        readline.write_history_file(str(_HISTORY_FILE))
    except OSError:
        pass  # best-effort, don't crash on exit
