"""Write file tool."""

from __future__ import annotations

import difflib
from pathlib import Path

from agent_cli.tools._diff import format_diff
from agent_cli.tools.base import Tool
from agent_cli.tools.read_file import format_hashlines
from agent_cli.tools.result import ToolResult

# A write_file that OVERWRITES an existing file but changes only a small
# fraction of its lines is almost always a case edit_file would do far cheaper
# — re-writing re-sends the whole file into context each turn. Below this
# changed-line fraction the write counts as a "small overwrite", which drives
# BOTH effects in one decision: (1) a one-line steering nudge, and (2) the
# observation echoes a DIFF (just the changed lines) instead of the whole
# file's hashlines, so the echo for the churn case shrinks to ~diff size.
# Both are observation-side (no mimicry); the write itself is unchanged. Tuned
# from a real session: small edits sat at 2-3% changed, full rewrites at 100%+.
_REWRITE_NUDGE_RATIO = 0.30


def _small_overwrite_analysis(path: str, new_content: str):
    """Judge whether writing ``new_content`` to ``path`` is a SMALL OVERWRITE,
    BEFORE the write (the old content is gone after). Returns
    ``(is_small, old_content, nudge_text)``:

    - ``is_small`` True iff ``path`` exists, is non-empty/readable, and <
      ``_REWRITE_NUDGE_RATIO`` of the new lines differ from the old.
    - ``old_content`` the prior file text (for the diff echo), or "".
    - ``nudge_text`` the steering note, or "".

    New file / empty / unreadable / genuine rewrite → ``(False, "", "")``: the
    single decision that gates both the nudge and the diff-vs-hashline echo."""
    p = Path(path)
    if not p.is_file():
        return (False, "", "")
    try:
        old = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return (False, "", "")
    if not old.strip():
        return (False, "", "")
    old_lines = old.splitlines()
    new_lines = new_content.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    changed = sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != "equal"
    )
    total = max(len(new_lines), 1)
    if changed / total >= _REWRITE_NUDGE_RATIO:
        return (False, "", "")
    pct = round(changed / total * 100)
    nudge = (
        f"[note] You rewrote an existing file but only ~{pct}% of lines changed "
        f"({changed}/{total}). For a change this small, edit_file costs only the "
        f"changed lines — re-writing the whole file re-sends every line into your "
        f"context each turn. Use edit_file next time."
    )
    return (True, old, nudge)


def tool_write_file(args: dict) -> ToolResult:
    """Create or overwrite a file with raw content.

    Returns the written content in hashline format (LINE#HASH:content) so
    the model can ``edit_file`` the file it just wrote WITHOUT a separate
    ``read_file`` round-trip. This removes the friction that pushes models
    to re-``write_file`` the whole file on every small change (observed:
    same file rewritten in full 4× in one session, edit_file used 0×).

    A SMALL overwrite (< 30% lines changed) is the exception: instead of the
    full hashline echo it shows a diff of just the changed lines (paired with
    the steering nudge) — the churn-case echo shrinks to ~diff size.
    """

    path = args.get("path", "")
    content = args.get("content", "")
    try:
        p = Path(path)
        # Judge small-overwrite BEFORE writing — the old content is gone after.
        is_small, old_content, nudge = _small_overwrite_analysis(path, content)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        header = f"File saved: {path} ({len(content)} bytes)"
        if nudge:
            header += f"\n{nudge}"
        if is_small:
            # Small overwrite: echo a diff of the changed lines (small, and it
            # shows the model exactly what it should have edit_file'd) instead
            # of dumping every line as a hashline.
            body = format_diff(old_content, content, path)
            msg = f"{header}\n{body}" if body else header
        else:
            # New file or genuine rewrite: full hashline echo — the same format
            # read_file emits, so it doubles as the edit_file ref source.
            msg = (
                f"{header}\n"
                f"To modify, call edit_file with the hashline refs below "
                f"(no need to read_file first):\n"
                f"{format_hashlines(content)}"
            )
        # Refresh code_index after a successful write. Best-effort —
        # post_hook swallows its own exceptions so an indexing hiccup
        # never poisons the user-facing write.
        from agent_cli.tools.code_index import post_hook

        post_hook(path)
        return ToolResult(True, output=msg)
    except Exception as e:
        return ToolResult(False, error=f"write_file failed: {e}")


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Create or overwrite a file with raw content. Returns hashline format "
        "(LINE#HASH:content) so you can edit_file immediately — no read_file "
        "needed. Use write_file ONLY for a NEW file or a genuine FULL rewrite. "
        "To change PART of an existing file, use edit_file instead: re-writing "
        "the whole file re-sends every line into your context each turn (the "
        "file appears twice — your write + its echo) and stays there, eating "
        "your context window; edit_file costs only the changed lines."
    )
    # Flat-native (consolidation roadmap Step 3): the schema is the plain
    # single-target shape — no `write_file_` wire-key prefix. `wrap_single_op`
    # is identity (no canonical re-wrap), so the flat op dispatches straight
    # through. `key_prefix` is left at its default; strip_prefix is then a
    # no-op on these unprefixed keys, and `claims` correctly returns False for
    # a flat `{path}` (no `write_file_` key) so it stays out of infer_action.
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to save"},
            "content": {"type": "string", "description": "File content"},
        },
        "required": ["path", "content"],
    }

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def touched_paths(self, action_input: dict) -> list[str]:
        p = self.strip_prefix(action_input).get("path")
        return [p] if isinstance(p, str) and p else []

    def summary_arg(self, action_input: dict) -> str:
        return self.strip_prefix(action_input).get("path", "")

    # NOTE: no ``render_action_input_for_context`` override — eliding the
    # written ``content`` on re-feed taught mimicry-prone models to emit the
    # marker AS the file body (real corruption; the model only recovered by
    # routing around write_file via ``shell`` heredocs). Reverted in v3.16.1;
    # the body stays verbatim on re-feed. See docs/ARCHITECTURE.md §5.4.

    def _run(self, args: dict, *, session_dir=None) -> ToolResult:
        return tool_write_file(args)
