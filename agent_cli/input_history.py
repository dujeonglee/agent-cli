"""Readline-based input history for interactive terminal prompts (the
``ask`` tool's questions and the setup wizard).

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
_decode_warning_shown = False


def _warn_decode_error_once(err: UnicodeDecodeError) -> None:
    """Print a one-shot hint when input() fails on non-UTF-8 bytes.

    Observed when a paste contains bytes from a non-UTF-8 source
    (e.g. CP949 clipboard) or an IME composition is interrupted
    mid-character. Returning empty lets the caller treat it like a
    missed input rather than crashing the CLI.
    """
    global _decode_warning_shown
    if _decode_warning_shown:
        return
    _decode_warning_shown = True
    import sys

    print(
        f"\n[warn] Input decode error ({err}). The terminal sent non-UTF-8 "
        "bytes — usually a paste from a non-UTF-8 source or an interrupted "
        "IME composition. Please retype.",
        file=sys.stderr,
    )


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


def read_rich_input(prompt: str, continuation: str = "... ") -> str:
    """Read user input with paste detection and explicit multiline support.

    Shared by the main REPL and in-skill ask prompts so both behave the
    same way:
      - Single line: Enter sends immediately.
      - Paste: drains extra lines buffered in stdin after the first line.
      - Explicit multiline: first line is `\"\"\"` (or `'''`) alone, then
        keep reading until a closing `\"\"\"`/`'''` line.

    `prompt` is used for the first input() call; `continuation` for the
    subsequent multiline lines (main.py uses the default, ask prompts
    may want a depth-prefixed continuation).
    """
    import select
    import sys

    try:
        first_line = input(prompt).strip()
    except UnicodeDecodeError as e:
        _warn_decode_error_once(e)
        return ""
    if not first_line:
        return ""

    # Explicit multiline: first line is """ or '''
    if first_line in ('"""', "'''"):
        close = first_line
        lines: list[str] = []
        while True:
            try:
                line = input(continuation)
            except EOFError:
                break
            except UnicodeDecodeError as e:
                _warn_decode_error_once(e)
                break
            if line.strip() == close:
                break
            lines.append(line)
            # Drain any pasted lines buffered in stdin
            try:
                while select.select([sys.stdin], [], [], 0.1)[0]:
                    buf_line = sys.stdin.readline()
                    if not buf_line:
                        break
                    stripped = buf_line.rstrip("\n")
                    if stripped.strip() == close:
                        return "\n".join(lines)
                    lines.append(stripped)
            except (OSError, ValueError):
                pass
        return "\n".join(lines)

    # Paste detection: check if stdin has more data immediately available
    lines = [first_line]
    try:
        while select.select([sys.stdin], [], [], 0.1)[0]:
            line = sys.stdin.readline()
            if not line:  # EOF
                break
            lines.append(line.rstrip("\n"))
    except (OSError, ValueError):
        pass  # select not supported (e.g. Windows) — single line only

    return "\n".join(lines)
