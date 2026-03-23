"""Session persistence — file-based context management.

Stores per-session iteration logs (JSONL) and summaries (markdown).
Harness writes automatically; LLM reads via read_context tool.

File layout:
  ~/.agent-cli/context/
    {workspace_hash}-{session_id}.jsonl       # append-only iteration log
    {workspace_hash}-{session_id}.summary.md  # generated on session end
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_CONTEXT_DIR = Path.home() / ".agent-cli" / "context"


@dataclass
class SessionMeta:
    session_id: str
    workspace: str
    workspace_hash: str
    created_at: str
    query: str = ""  # first query (for identification)


def _workspace_hash(workspace: str) -> str:
    """Short hash of workspace path for filename prefix."""
    return hashlib.sha256(workspace.encode()).hexdigest()[:12]


def _ensure_context_dir() -> Path:
    _CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    return _CONTEXT_DIR


def create_session(workspace: str | None = None) -> SessionMeta:
    """Create a new session for the given workspace (defaults to CWD)."""
    ws = workspace or os.getcwd()
    return SessionMeta(
        session_id=str(int(time.time())),
        workspace=ws,
        workspace_hash=_workspace_hash(ws),
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _file_prefix(meta: SessionMeta) -> str:
    return f"{meta.workspace_hash}-{meta.session_id}"


def get_log_path(meta: SessionMeta) -> Path:
    """Path to the JSONL iteration log file."""
    return _ensure_context_dir() / f"{_file_prefix(meta)}.jsonl"


def get_summary_path(meta: SessionMeta) -> Path:
    """Path to the session summary markdown file."""
    return _ensure_context_dir() / f"{_file_prefix(meta)}.summary.md"


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
        header = {"_meta": asdict(meta)}
        f.write(json.dumps(header, ensure_ascii=False) + "\n")


def list_sessions(workspace: str | None = None) -> list[SessionMeta]:
    """List sessions, optionally filtered by workspace."""
    if not _CONTEXT_DIR.is_dir():
        return []

    ws_hash = _workspace_hash(workspace) if workspace else None
    sessions = []

    for path in sorted(_CONTEXT_DIR.glob("*.jsonl")):
        name = path.stem  # {ws_hash}-{session_id}
        if ws_hash and not name.startswith(ws_hash):
            continue
        # Read first line for metadata
        try:
            with open(path, encoding="utf-8") as f:
                first_line = f.readline().strip()
            if first_line:
                data = json.loads(first_line)
                if "_meta" in data:
                    meta_dict = data["_meta"]
                    sessions.append(SessionMeta(**meta_dict))
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    return sessions


def load_session(session_id: str) -> SessionMeta | None:
    """Load a session by ID."""
    if not _CONTEXT_DIR.is_dir():
        return None
    for path in _CONTEXT_DIR.glob(f"*-{session_id}.jsonl"):
        try:
            with open(path, encoding="utf-8") as f:
                first_line = f.readline().strip()
            if first_line:
                data = json.loads(first_line)
                if "_meta" in data:
                    return SessionMeta(**data["_meta"])
        except (json.JSONDecodeError, TypeError, KeyError):
            pass
    return None


def _serialize_ctx_messages(messages: list[dict]) -> str:
    """Serialize context messages to text for summary storage.

    Reuses the pattern from ContextManager._serialize_messages().
    """
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


def find_latest_summary(workspace: str | None = None) -> str | None:
    """Find the most recent session summary for a workspace."""
    sessions = list_sessions(workspace or os.getcwd())
    if not sessions:
        return None
    # Sessions are sorted by file name (which includes timestamp)
    latest = sessions[-1]
    return load_summary(latest)
