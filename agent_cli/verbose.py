"""Verbose-mode flag and debug logging.

Factored out of loop.py so lower-level modules (e.g. providers) can emit
verbose-gated stderr lines without importing upward. The flag is toggled
once at loop entry via `set_verbose(...)`; every reader elsewhere just
calls `debug_log(...)` and it no-ops when verbose is off.
"""

from __future__ import annotations

import sys
import time

_verbose = False


def set_verbose(v: bool) -> None:
    """Enable/disable verbose stderr debug output."""
    global _verbose
    _verbose = v


def debug_log(msg: str) -> None:
    """Print a debug line to stderr when verbose mode is on."""
    if not _verbose:
        return
    print(f"[debug {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr)
