"""Write file tool."""

from __future__ import annotations

import difflib
from pathlib import Path

from agent_cli.tools.base import Tool
from agent_cli.tools.read_file import format_hashlines
from agent_cli.tools.result import ToolResult

# Runtime nudge (B1): a write_file that OVERWRITES an existing file but changes
# only a small fraction of its lines is almost always a case that edit_file
# would do far cheaper — re-writing re-sends the whole file into context each
# turn (write content + hashline echo, twice). Below this changed-line fraction
# we append a one-line steering note to the observation (the write still
# happens; observation-side, so no mimicry risk). Tuned from a real session:
# small edits sat at 2-3% changed, genuine full rewrites at 100%+.
_REWRITE_NUDGE_RATIO = 0.30


def _rewrite_nudge(path: str, new_content: str) -> str:
    """One-line steering note when ``path`` exists, is non-empty, and the new
    content changes < ``_REWRITE_NUDGE_RATIO`` of its lines (small overwrite).
    Empty string otherwise (new file / empty file / genuine rewrite / unreadable
    — all default to no nudge). Computed BEFORE the write, while the old content
    is still on disk (render_observation runs after the overwrite, too late)."""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        old = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if not old.strip():
        return ""
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
        return ""
    pct = round(changed / total * 100)
    return (
        f"[note] You rewrote an existing file but only ~{pct}% of lines changed "
        f"({changed}/{total}). For a change this small, edit_file costs only the "
        f"changed lines — re-writing the whole file re-sends every line into your "
        f"context each turn. Use edit_file next time (hashline refs are below)."
    )


def tool_write_file(args: dict) -> ToolResult:
    """Create or overwrite a file with raw content.

    Returns the written content in hashline format (LINE#HASH:content) so
    the model can ``edit_file`` the file it just wrote WITHOUT a separate
    ``read_file`` round-trip. This removes the friction that pushes models
    to re-``write_file`` the whole file on every small change (observed:
    same file rewritten in full 4× in one session, edit_file used 0×).
    """

    path = args.get("path", "")
    content = args.get("content", "")
    try:
        p = Path(path)
        # Judge small-overwrite BEFORE writing — the old content is gone after.
        nudge = _rewrite_nudge(path, content)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        # Echo the written content as hashlines — the same format read_file
        # emits — so it doubles as the edit_file ref source.
        header = f"File saved: {path} ({len(content)} bytes)"
        if nudge:
            header += f"\n{nudge}"
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
