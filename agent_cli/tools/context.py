"""read_context tool — lets LLM browse previous session history."""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli.context.session import (
    list_sessions,
    load_session,
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
            lines.append(f"- {s.session_id} [{s.updated_at}] {s.query or '(no query)'}")
        return ToolResult(True, output="\n".join(lines))

    elif mode == "detail":
        session_id = args.get("session_id", "")
        if not session_id:
            return ToolResult(False, error="session_id is required for mode='detail'.")
        meta = load_session(session_id)
        if not meta:
            return ToolResult(False, error=f"session '{session_id}' not found.")

        # Read from history.jsonl
        history_path = Path(".agent-cli") / "sessions" / session_id / "history.jsonl"
        if not history_path.is_file():
            return ToolResult(True, output=f"Session '{session_id}' has no history.")

        parts = []
        with open(history_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = msg.get("role", "?")
                thought = msg.get("thought", "")
                action = msg.get("action", "")
                content = msg.get("content", "")[:300]
                if thought:
                    parts.append(f"[{role}] {thought[:100]} → {action}")
                else:
                    parts.append(f"[{role}] {content}")

        return ToolResult(
            True,
            output="\n".join(parts) if parts else "No messages in this session.",
        )

    return ToolResult(False, error=f"unknown mode '{mode}'. Use 'list' or 'detail'.")
