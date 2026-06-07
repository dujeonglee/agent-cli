"""Session persistence — project-local, session-scoped file management.

Stores session metadata in session.jsonl (single line).
Conversation history is managed by ContextManager (history.jsonl).

File layout:
  {project}/.agent-cli/sessions/{session_id}/
    session.jsonl          # single-line metadata (id, workspace, updated_at, response_format)
    history.jsonl          # conversation history (managed by ContextManager)
    skill_*/delegate_*/    # skill/delegate subdirectories
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from agent_cli.wire_formats import DEFAULT_WIRE_FORMAT, all_system_user_prefixes

_SESSIONS_BASE = Path(".agent-cli")


@dataclass
class SessionMeta:
    session_id: str
    workspace: str
    updated_at: str
    # Wire format the session runs under. Recorded so a session's response
    # shape is recoverable for debugging / resume. Defaults to
    # DEFAULT_WIRE_FORMAT for sessions written before this field existed.
    response_format: str = DEFAULT_WIRE_FORMAT


def get_session_dir(meta: SessionMeta) -> Path:
    """Return the session directory path, creating it if needed."""
    d = _SESSIONS_BASE / "sessions" / meta.session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_session(
    workspace: str | None = None, response_format: str = DEFAULT_WIRE_FORMAT
) -> SessionMeta:
    """Create a new session for the given workspace (defaults to CWD)."""
    ws = workspace or os.getcwd()
    return SessionMeta(
        session_id=str(int(time.time())),
        workspace=ws,
        updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        response_format=response_format,
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
                "response_format": meta.response_format,
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


def recent_exchanges(history_path: Path, n: int = 10) -> list[tuple[str, str]]:
    """Return the last `n` (user_query, assistant_final) pairs from
    history.jsonl, in chronological order.

    A "user query" is a role=user message that is neither a tool
    observation (content starting with "Observation:" or carrying a
    `tool` field) nor a loop-emitted system notice (retry hints,
    interrupt notices). Those all share the role=user shape but are
    not real user input.

    The set of "system notice" prefixes comes from
    :func:`agent_cli.wire_formats.all_system_user_prefixes` so any
    registered wire-format plugin's framing strings are picked up
    automatically — no edit here when a new plugin is added.

    The paired final is the next role=assistant `complete` action's
    result. If a new user query arrives before the previous one
    completes, the previous pair is closed with "(no completion)" so
    interrupted runs still surface.
    """
    system_prefixes = all_system_user_prefixes()

    if not history_path.is_file():
        return []

    pairs: list[tuple[str, str]] = []
    pending: str | None = None

    with open(history_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            role = msg.get("role")
            if role == "user":
                content = msg.get("content", "")
                if content.startswith("Observation:") or msg.get("tool"):
                    continue
                if any(content.startswith(p) for p in system_prefixes):
                    continue
                if pending is not None:
                    pairs.append((pending, "(no completion)"))
                pending = content
            elif role == "assistant" and msg.get("action") == "complete":
                if pending is None:
                    continue
                action_input = msg.get("action_input", {})
                if isinstance(action_input, dict):
                    result = action_input.get("result", "")
                else:
                    result = str(action_input) if action_input else ""
                pairs.append((pending, result))
                pending = None

    if pending is not None:
        pairs.append((pending, "(no completion)"))

    return pairs[-n:] if n > 0 else pairs


def session_summary(meta: SessionMeta) -> tuple[str, str]:
    """``(last_user_request, last_result)`` for a session, read from its
    history.jsonl — the replacement for the removed ``query`` meta field.

    ``last_result`` is the last ``complete`` action's result, or
    "(no completion)" for a run still open / interrupted. Returns
    ``("", "")`` when the session has no history yet. Reads the file path
    directly (no mkdir side-effect, unlike ``get_session_dir``).
    """
    hp = _SESSIONS_BASE / "sessions" / meta.session_id / "history.jsonl"
    pairs = recent_exchanges(hp, n=1)
    return pairs[-1] if pairs else ("", "")
