"""Write file tool."""

from __future__ import annotations

from pathlib import Path

from agent_cli.tools._diff import format_diff
from agent_cli.tools.result import ToolResult


def tool_write_file(args: dict) -> ToolResult:
    """Create or overwrite a file with raw content."""

    path = args.get("path", "")
    content = args.get("content", "")
    try:
        p = Path(path)
        # Capture old content for the diff before mutating. Missing
        # file = empty string so the diff shows every line as added.
        old = ""
        if p.is_file():
            try:
                old = p.read_text(encoding="utf-8")
            except Exception:
                # Binary or unreadable previous content — skip the
                # diff rather than crash; the write itself can still
                # proceed.
                old = ""
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        msg = f"File saved: {path} ({len(content)} bytes)"
        diff = format_diff(old, content, path)
        if diff:
            msg += "\n\n" + diff
        return ToolResult(True, output=msg)
    except Exception as e:
        return ToolResult(False, error=f"write_file failed: {e}")
