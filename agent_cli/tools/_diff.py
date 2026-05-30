"""Shared unified-diff formatter for write_file / edit_file output.

Produces a **plain** standard unified diff (the ``git diff`` text shape:
``--- a/…`` / ``+++ b/…`` / ``@@ … @@`` / ` ` / ``-`` / ``+`` lines). No
colour markup or line-number gutter — the diff that goes into the LLM
observation stays clean (no ``[green]`` tags to burn tokens or confuse
the model), and the renderers add colour themselves by reading each
line's leading character (CLI: Rich style; web: ``app.js`` diff
colorizer). Line position is conveyed by the ``@@ -A,B +C,D @@`` hunk
header, same as git.

Truncated past ``MAX_DIFF_LINES`` to keep the observation from
ballooning when an edit replaces an entire large file. The truncation
preserves the head of the diff and tells the caller how many lines
were dropped — full content is still on disk if needed.
"""

from __future__ import annotations

import difflib

# Cap the diff at ~100 visible lines. Beyond this the LLM rarely
# benefits from seeing more (it already authored the change) and the
# token cost grows. The user can still read the file directly if they
# want to verify the tail.
MAX_DIFF_LINES = 100

# Prefix of the truncation summary line. Renderers match on this to dim
# it; kept here so the producer and the colorizers agree on one string.
DIFF_TRUNCATION_PREFIX = "… diff truncated,"


def format_diff(old: str, new: str, path: str) -> str:
    """Return a plain standard unified diff between ``old`` and ``new``.

    Empty when identical (caller branches on truthiness). Colour is the
    renderer's job — see module docstring. Truncation appends a single
    ``DIFF_TRUNCATION_PREFIX`` summary line.
    """
    if old == new:
        return ""

    old_lines = old.splitlines(keepends=True) if old else []
    new_lines = new.splitlines(keepends=True) if new else []

    raw = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=2,  # 2 lines of context — enough to orient, not enough to bloat
        )
    )

    if not raw:
        return ""

    # ``unified_diff`` keeps each source line's trailing newline; strip
    # it so we control the joins (and blank lines don't double up).
    lines = [line.rstrip("\n") for line in raw]

    if len(lines) > MAX_DIFF_LINES:
        elided = len(lines) - MAX_DIFF_LINES
        lines = lines[:MAX_DIFF_LINES]
        lines.append(f"{DIFF_TRUNCATION_PREFIX} {elided} more line(s) omitted")

    return "\n".join(lines)
