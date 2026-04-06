"""Session persistence — project-local, session-scoped file management.

Stores session metadata in session.jsonl (single line).
Conversation history is managed by ContextManager (history.jsonl).

File layout:
  {project}/.agent-cli/sessions/{session_id}/
    session.jsonl          # single-line metadata (id, workspace, updated_at, query)
    history.jsonl          # conversation history (managed by ContextManager)
    skill_*/delegate_*/    # skill/delegate subdirectories
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
    updated_at: str
    query: str = ""


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
        updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


def save_meta(meta: SessionMeta) -> None:
    """Save session metadata (single line in session.jsonl)."""
    meta.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    d = _SESSIONS_BASE / "sessions" / meta.session_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / "session.jsonl"
    header = json.dumps(
        {
            "_meta": {
                "session_id": meta.session_id,
                "workspace": meta.workspace,
                "updated_at": meta.updated_at,
                "query": meta.query,
            }
        },
        ensure_ascii=False,
    )
    path.write_text(header + "\n", encoding="utf-8")


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
                    meta_data = data["_meta"]
                    # Backward compat: created_at → updated_at
                    if "created_at" in meta_data and "updated_at" not in meta_data:
                        meta_data["updated_at"] = meta_data.pop("created_at")
                    sessions.append(SessionMeta(**meta_data))
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
                meta_data = data["_meta"]
                # Backward compat: created_at → updated_at
                if "created_at" in meta_data and "updated_at" not in meta_data:
                    meta_data["updated_at"] = meta_data.pop("created_at")
                return SessionMeta(**meta_data)
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    return None


def finalize_session(meta, ctx=None) -> None:
    """Update session metadata on session end."""
    if meta:
        save_meta(meta)
