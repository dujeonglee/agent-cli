"""Write file tool."""

from __future__ import annotations

from pathlib import Path

from agent_cli.tools.base import Tool
from agent_cli.tools.read_file import format_hashlines
from agent_cli.tools.result import ToolResult


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
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        # Echo the written content as hashlines — the same format read_file
        # emits — so it doubles as the edit_file ref source.
        msg = (
            f"File saved: {path} ({len(content)} bytes)\n"
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
        "Create or overwrite a file with raw content. Returns the written "
        "content in hashline format (LINE#HASH:content) so you can edit_file "
        "it immediately — no separate read_file needed. For NEW files use "
        "write_file; to make a SMALL change to a file you already wrote or "
        "read, prefer edit_file with the returned hashline refs rather than "
        "rewriting the whole file."
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

    def render_action_input_for_context(self, action_input: dict) -> dict:
        """Elide the written ``content`` body on re-feed: keep the op shape but
        replace the body with a marker. The file is on disk and the write was
        already confirmed by the observation — re-feeding the whole body every
        turn only crowds out context (the model reads_file to view)."""
        body = action_input.get("content")
        if isinstance(body, str) and body:
            path = action_input.get("path", "")
            n = body.count("\n") + 1
            return {
                **action_input,
                "content": f"<{n} lines / {len(body)}B written to {path} — read_file to view>",
            }
        return action_input

    def _run(self, args: dict, *, session_dir=None) -> ToolResult:
        return tool_write_file(args)
