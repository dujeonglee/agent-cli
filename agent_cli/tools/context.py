"""read_context tool — lets LLM browse previous session history."""

from __future__ import annotations

from agent_cli.context.session import (
    list_sessions,
    load_session,
    load_summary,
    read_log,
)
from agent_cli.tools.result import ToolResult


def tool_read_context(args: dict) -> ToolResult:
    """Read context from previous sessions."""

    mode = args.get("mode", "list")

    if mode == "list":
        sessions = list_sessions()
        if not sessions:
            return ToolResult(True, output="No previous sessions found.")
        lines = []
        for s in sessions:
            summary = load_summary(s)
            summary_preview = (
                summary[:200] + "..."
                if summary and len(summary) > 200
                else summary or "(no summary)"
            )
            lines.append(
                f"- {s.session_id} [{s.created_at}] {s.query or '(no query)'}\n  {summary_preview}"
            )
        return ToolResult(True, output="\n".join(lines))

    elif mode == "detail":
        session_id = args.get("session_id", "")
        if not session_id:
            return ToolResult(False, error="session_id is required for mode='detail'.")
        meta = load_session(session_id)
        if not meta:
            return ToolResult(False, error=f"session '{session_id}' not found.")
        entries = read_log(meta)
        if not entries:
            return ToolResult(
                True, output=f"Session '{session_id}' has no log entries."
            )
        parts = []
        for e in entries:
            if "_meta" in e:
                continue
            parts.append(
                f"[iter {e.get('iter', '?')}] {e.get('action', '?')}: "
                f"{e.get('thought', '')}\n  → {e.get('observation', '')[:300]}"
            )
        return ToolResult(
            True,
            output="\n\n".join(parts)
            if parts
            else "No tool executions in this session.",
        )

    return ToolResult(False, error=f"unknown mode '{mode}'. Use 'list' or 'detail'.")
