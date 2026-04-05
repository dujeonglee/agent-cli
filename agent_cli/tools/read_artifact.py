"""read_artifact tool — deprecated, redirects to read_file.

Artifacts are now stored as regular files in session directories.
Use read_file to access them directly.
"""

from __future__ import annotations

from pathlib import Path

from agent_cli.tools.result import ToolResult


def tool_read_artifact(args: dict, **kwargs) -> ToolResult:
    """Read artifact — delegates to read_file or lists session files."""
    path = args.get("path", "")
    mode = args.get("mode", "")

    if path:
        # Try to read the file directly
        ctx = kwargs.get("ctx")
        p = Path(path)

        if not p.is_absolute() and ctx and hasattr(ctx, "session_dir"):
            candidate = ctx.session_dir / path
            if candidate.is_file():
                p = candidate

        if p.is_file():
            try:
                content = p.read_text(encoding="utf-8")
                return ToolResult(True, output=content)
            except Exception as e:
                return ToolResult(False, error=f"Error reading: {e}")
        return ToolResult(False, error=f"File not found: {path}")

    if mode == "list":
        ctx = kwargs.get("ctx")
        if ctx and hasattr(ctx, "session_dir"):
            session_dir = ctx.session_dir
            files = sorted(session_dir.rglob("*.md")) + sorted(
                session_dir.rglob("*.jsonl")
            )
            if files:
                lines = [f"Session files ({len(files)}):"]
                for f in files:
                    rel = f.relative_to(session_dir)
                    lines.append(f"  {rel}")
                return ToolResult(True, output="\n".join(lines))
        return ToolResult(True, output="No session files found.")

    return ToolResult(
        False,
        error="Provide 'path' to read a file, or 'mode': 'list' to list session files.",
    )
