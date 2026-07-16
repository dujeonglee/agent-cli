"""Shared post-mutation change echo for write_file / edit_file.

A file-mutating tool's observation should carry the same two things whenever
it shows a change:

  1. a unified **diff** of WHAT changed (``_diff.format_diff``), and
  2. fresh ``LINE#HASH`` refs for the changed region (``_updated_region_echo``)
     at correct POST-edit line numbers, so the model can chain a follow-up
     ``edit_file`` on what it just touched WITHOUT a ``read_file`` round-trip.

``edit_file`` always emitted both. ``write_file``'s small-overwrite branch
emitted only the diff — the same diff, but no refs — so a model that took the
"use edit_file next time" nudge still had to re-read first. ``render_change_echo``
is the single composition both tools now call, so any diff-showing observation
carries the region refs consistently.

The region echo is capped at ``_MAX_REGION_LINES`` so an "edited the whole
file" case does not dump every line back into context (that would defeat
edit_file's cost model); it mirrors ``_diff.MAX_DIFF_LINES``.
"""

from __future__ import annotations

import difflib

from agent_cli.tools._diff import format_diff
from agent_cli.tools.read_file import format_hashlines_range

# Context lines shown on each side of a changed span in the post-edit hashline
# echo — just enough to anchor a follow-up edit ref, not enough to bloat.
_REGION_CONTEXT = 3
# Cap the windowed hashline echo so an "edited the whole file" case does not
# dump every line back into context. Past this, the model is told to read_file
# for the rest.
_MAX_REGION_LINES = 100
# Header for the fresh-hashline block; prefix kept as one string so renderers
# and tests can match it.
_REGION_HEADER = "Updated region (edit_file refs below):"
_REGION_TRUNCATION_PREFIX = "… region truncated,"


def _updated_region_echo(original_text: str, result_text: str) -> str:
    """Fresh ``LINE#HASH`` tags for the regions a mutation changed (± context).

    Lets the model chain a follow-up ``edit_file`` on what it just wrote WITHOUT
    a ``read_file`` round-trip: the diff shows WHAT changed, this shows the new
    hashline refs for the changed region at correct POST-mutation line numbers
    (via :func:`format_hashlines_range`). Overlapping/adjacent windows merge;
    total emitted lines are capped at ``_MAX_REGION_LINES``. Empty string when
    nothing changed."""
    orig_lines = original_text.split("\n")
    result_lines = result_text.split("\n")
    sm = difflib.SequenceMatcher(None, orig_lines, result_lines)
    windows: list[tuple[int, int]] = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        lo = max(0, j1 - _REGION_CONTEXT)
        # Pure deletion (j1 == j2) leaves nothing on the new side — still show
        # context around the seam so the model sees where the delete landed.
        hi = min(len(result_lines), max(j2, j1 + 1) + _REGION_CONTEXT)
        if hi > lo:
            windows.append((lo, hi))
    if not windows:
        return ""
    # Merge overlapping / adjacent [lo, hi) windows.
    windows.sort()
    merged: list[list[int]] = [list(windows[0])]
    for lo, hi in windows[1:]:
        if lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    total = sum(hi - lo for lo, hi in merged)
    blocks: list[str] = []
    emitted = 0
    for lo, hi in merged:
        if emitted >= _MAX_REGION_LINES:
            break
        hi = min(hi, lo + (_MAX_REGION_LINES - emitted))  # trim to cap
        blocks.append(format_hashlines_range(result_lines, lo, hi))
        emitted += hi - lo
    echo = "\n\n".join(blocks)
    if emitted < total:
        echo += (
            f"\n{_REGION_TRUNCATION_PREFIX} read_file for the rest "
            f"({total - emitted} more line(s))"
        )
    return echo


def render_change_echo(old: str, new: str, path: str) -> str:
    """Return the post-mutation echo body: a unified diff of the change plus a
    fresh-hashline region block (``diff\\n\\n{_REGION_HEADER}\\n{region}``).

    Empty string when ``old == new`` (no diff → no echo). When a diff exists but
    the region echo is empty (edge case), returns just the diff. Callers append
    this to their own header, so this function stays header-agnostic."""
    diff = format_diff(old, new, path)
    if not diff:
        return ""
    parts = [diff]
    region = _updated_region_echo(old, new)
    if region:
        parts.append(f"{_REGION_HEADER}\n{region}")
    return "\n\n".join(parts)
