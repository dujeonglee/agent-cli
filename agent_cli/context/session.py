"""Session persistence — project-local, session-scoped file management.

Stores per-session iteration logs (JSONL) and summaries (markdown)
alongside scratchpad and artifacts in the same session directory.

File layout:
  {project}/.agent-cli/sessions/{session_id}/
    session.jsonl          # append-only iteration log
    session.summary.md     # generated on session end
    scratchpad.md          # (managed by scratchpad.py)
    artifacts/             # (managed by scratchpad.py)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

_SESSIONS_BASE = Path(".agent-cli")


@dataclass
class SessionMeta:
    session_id: str
    workspace: str
    created_at: str
    query: str = ""  # first query (for identification)


def _ensure_sessions_dir() -> Path:
    d = _SESSIONS_BASE / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_session_dir(meta: SessionMeta) -> Path:
    """Return the session directory path, creating it if needed."""
    d = _SESSIONS_BASE / "sessions" / meta.session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_session(workspace: str | None = None) -> SessionMeta:
    """Create a new session for the given workspace (defaults to CWD)."""
    ws = workspace or os.getcwd()
    return SessionMeta(
        session_id=str(int(time.time())),
        workspace=ws,
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


def get_log_path(meta: SessionMeta) -> Path:
    """Path to the JSONL iteration log file."""
    return get_session_dir(meta) / "session.jsonl"


def get_summary_path(meta: SessionMeta) -> Path:
    """Path to the session summary markdown file."""
    return get_session_dir(meta) / "session.summary.md"


def append_log(meta: SessionMeta, entry: dict) -> None:
    """Append one iteration entry to the session log (JSONL)."""
    path = get_log_path(meta)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_log(meta: SessionMeta) -> list[dict]:
    """Read all entries from a session log."""
    path = get_log_path(meta)
    if not path.is_file():
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def save_summary(meta: SessionMeta, summary: str) -> None:
    """Save session summary to markdown file."""
    path = get_summary_path(meta)
    path.write_text(summary, encoding="utf-8")


def load_summary(meta: SessionMeta) -> str | None:
    """Load session summary from markdown file."""
    path = get_summary_path(meta)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def save_meta(meta: SessionMeta) -> None:
    """Save session metadata as first line of JSONL (if file is empty)."""
    path = get_log_path(meta)
    if path.is_file() and path.stat().st_size > 0:
        return  # already has content
    with open(path, "a", encoding="utf-8") as f:
        header = {
            "_meta": {
                "session_id": meta.session_id,
                "workspace": meta.workspace,
                "created_at": meta.created_at,
                "query": meta.query,
            }
        }
        f.write(json.dumps(header, ensure_ascii=False) + "\n")


def list_sessions(workspace: str | None = None) -> list[SessionMeta]:
    """List sessions, optionally filtered by workspace."""
    sessions_dir = _SESSIONS_BASE / "sessions"
    if not sessions_dir.is_dir():
        return []

    sessions = []
    for sdir in sorted(sessions_dir.iterdir()):
        if not sdir.is_dir():
            continue
        jsonl = sdir / "session.jsonl"
        if not jsonl.is_file():
            continue
        try:
            with open(jsonl, encoding="utf-8") as f:
                first_line = f.readline().strip()
            if first_line:
                data = json.loads(first_line)
                if "_meta" in data:
                    meta = SessionMeta(**data["_meta"])
                    if workspace and meta.workspace != workspace:
                        continue
                    sessions.append(meta)
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    return sessions


def load_session(session_id: str) -> SessionMeta | None:
    """Load a session by ID."""
    sdir = _SESSIONS_BASE / "sessions" / session_id
    jsonl = sdir / "session.jsonl"
    if not jsonl.is_file():
        return None
    try:
        with open(jsonl, encoding="utf-8") as f:
            first_line = f.readline().strip()
        if first_line:
            data = json.loads(first_line)
            if "_meta" in data:
                return SessionMeta(**data["_meta"])
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    return None


def _serialize_ctx_messages(messages: list[dict]) -> str:
    """Serialize context messages to text for summary storage."""
    parts = []
    for m in messages:
        role = m.get("role", "unknown").capitalize()
        content = m.get("content", "")
        if len(content) > 2000:
            content = (
                content[:2000]
                + f"\n[... {len(content) - 2000} more characters truncated]"
            )
        parts.append(f"[{role}]: {content}")
    return "\n\n".join(parts)


def finalize_session(meta, ctx=None) -> None:
    """Save context window as session summary (no LLM call)."""
    if ctx is None:
        return
    messages = ctx.get_messages()
    if not messages:
        return
    summary = _serialize_ctx_messages(messages)
    if summary:
        save_summary(meta, summary)
