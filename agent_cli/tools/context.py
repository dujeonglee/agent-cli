"""read_context tool — lets LLM browse previous session history."""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli.context.session import list_sessions
from agent_cli.tools.result import ToolResult

_SESSIONS_BASE = Path(".agent-cli") / "sessions"


def tool_read_context(args: dict) -> ToolResult:
    """Read context from previous sessions.

    Modes:
      - list: show all sessions (id, time, last query)
      - search: grep keyword across all sessions' history
    """
    mode = args.get("mode", "list")

    if mode == "list":
        return _mode_list()
    elif mode == "search":
        return _mode_search(args.get("keyword", ""))

    return ToolResult(
        False, error=f"unknown mode '{mode}'. Use 'list' or 'search'."
    )


def _mode_list() -> ToolResult:
    sessions = list_sessions()
    if not sessions:
        return ToolResult(True, output="No previous sessions found.")
    lines = []
    for s in sessions:
        lines.append(f"- {s.session_id} [{s.updated_at}] {s.query or '(no query)'}")
    return ToolResult(True, output="\n".join(lines))


def _mode_search(keyword: str) -> ToolResult:
    """Search keyword across all sessions' history.jsonl files."""
    if not keyword:
        return ToolResult(False, error="keyword is required for mode='search'.")

    keyword_lower = keyword.lower()
    matches = []

    if not _SESSIONS_BASE.is_dir():
        return ToolResult(True, output="No sessions found.")

    for session_dir in sorted(_SESSIONS_BASE.iterdir()):
        if not session_dir.is_dir():
            continue
        session_id = session_dir.name

        # Search all history.jsonl files (root + subdirs)
        for history_path in session_dir.rglob("history.jsonl"):
            rel_path = history_path.relative_to(session_dir)
            try:
                with open(history_path, encoding="utf-8") as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        if keyword_lower not in line.lower():
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        role = msg.get("role", "?")
                        thought = msg.get("thought", "")
                        action = msg.get("action", "")
                        content = msg.get("content", "")
                        artifact = msg.get("artifact", "")

                        # Build preview around keyword
                        if thought and keyword_lower in thought.lower():
                            preview = thought[:120]
                        elif content and keyword_lower in content.lower():
                            idx = content.lower().find(keyword_lower)
                            start = max(0, idx - 40)
                            end = min(len(content), idx + len(keyword) + 80)
                            preview = (
                                ("..." if start > 0 else "")
                                + content[start:end]
                                + ("..." if end < len(content) else "")
                            )
                        else:
                            preview = (thought or content)[:120]

                        loc = f"{session_id}/{rel_path}:{line_num}"
                        action_str = f" → {action}" if action else ""
                        artifact_str = f" [{artifact}]" if artifact else ""
                        matches.append(
                            f"- {loc} [{role}]{action_str}{artifact_str}\n  {preview}"
                        )
            except Exception:
                continue

        if len(matches) >= 50:
            matches.append(f"... (truncated, {len(matches)}+ matches)")
            break

    if not matches:
        return ToolResult(True, output=f"No matches found for '{keyword}'.")

    header = f"Search results for '{keyword}' ({len(matches)} matches):\n"
    return ToolResult(True, output=header + "\n".join(matches))
