"""Readline-based input history for chat REPL.

Enables arrow key navigation, persistent history across sessions,
and standard readline editing shortcuts (Ctrl+A/E/W/K).
"""

from __future__ import annotations

import atexit
from pathlib import Path

import os

# Prefer gnureadline over stdlib readline (macOS ships libedit which has
# known bugs with CJK character width, causing typed Korean/Chinese/Japanese
# input to render with extra spaces). Install: pip install gnureadline
# Disable readline entirely with AGENT_CLI_NO_READLINE=1.
_has_readline = False
_using_libedit = False
if not os.environ.get("AGENT_CLI_NO_READLINE"):
    try:
        import gnureadline as readline  # type: ignore[import-not-found]

        _has_readline = True
    except ImportError:
        try:
            import readline

            _has_readline = True
            _using_libedit = "libedit" in (readline.__doc__ or "")
        except Exception:
            pass

_HISTORY_FILE = Path.home() / ".agent-cli" / "chat_history"
_MAX_HISTORY = 1000
_initialized = False


def setup() -> None:
    """Initialize readline and load persistent history."""
    global _initialized
    if _initialized or not _has_readline:
        return

    # macOS ships libedit which has CJK width bugs. Warn once.
    if _using_libedit:
        import sys

        print(
            "[warn] Using libedit (macOS default). Korean/Chinese/Japanese input "
            "may render incorrectly. Fix: pip install gnureadline",
            file=sys.stderr,
        )
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


def make_prompt(text: str) -> str:
    """Build a plain-text prompt for readline input."""
    return f"{text} "


def save() -> None:
    """Write current history to disk."""
    if not _has_readline:
        return
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        readline.write_history_file(str(_HISTORY_FILE))
    except OSError:
        pass  # best-effort, don't crash on exit
