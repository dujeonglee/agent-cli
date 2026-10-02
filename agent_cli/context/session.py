"""Session persistence — project-local, session-scoped file management.

Stores session metadata in session.jsonl (single line).
Conversation history is managed by ContextManager (history.jsonl).

File layout:
  {project}/.agent-cli/sessions/{session_id}/
    session.jsonl          # single-line metadata (id, workspace, updated_at)
    history.jsonl          # conversation history (managed by ContextManager)
    skill_*/delegate_*/    # skill/delegate subdirectories
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, fields
from pathlib import Path

from agent_cli.dialects import all_system_user_prefixes
from agent_cli.fsio import atomic_write_text
from agent_cli.paths import sessions_dir

_SESSIONS_DIR = sessions_dir()


@dataclass
class SessionMeta:
    session_id: str
    workspace: str
    updated_at: str


def get_session_dir(meta: SessionMeta) -> Path:
    """Return the session directory path, creating it if needed."""
    d = _SESSIONS_DIR / meta.session_id
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
    d = _SESSIONS_DIR / meta.session_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / "session.jsonl"
    header = json.dumps(
        {
            "_meta": {
                "session_id": meta.session_id,
                "workspace": meta.workspace,
                "updated_at": meta.updated_at,
            }
        },
        ensure_ascii=False,
    )
    # 단일-라인 meta rewrite — 다른 프로세스(보드 sessions 조회·resume
    # preview)가 읽는 상태 파일이라 원자 교체 (fsio 패턴).
    atomic_write_text(path, header + "\n")


def _meta_from_dict(meta_data: dict) -> SessionMeta:
    """session.jsonl 의 ``_meta`` → :class:`SessionMeta`.

    - ``created_at`` → ``updated_at`` (초기 세션).
    - 모르는 키는 버린다 — 메타는 읽는 쪽이 아는 필드만 의미가 있다
      (v10.3.0 전 세션의 ``dialect``/``response_format`` 은 더 이상 읽지
      않는다: 방언은 모델 바인딩이 정한다).
    """
    meta_data = dict(meta_data)
    if "created_at" in meta_data and "updated_at" not in meta_data:
        meta_data["updated_at"] = meta_data.pop("created_at")
    known = {f.name for f in fields(SessionMeta)}
    return SessionMeta(**{k: v for k, v in meta_data.items() if k in known})


def list_sessions(workspace: str | None = None) -> list[SessionMeta]:
    """List sessions, optionally filtered by workspace."""
    root = _SESSIONS_DIR
    if not root.is_dir():
        return []

    sessions = []
    for sdir in sorted(root.iterdir()):
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
                    sessions.append(_meta_from_dict(meta_data))
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    return sessions


def load_session(session_id: str) -> SessionMeta | None:
    """Load a session by ID."""
    sdir = _SESSIONS_DIR / session_id
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
                return _meta_from_dict(meta_data)
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
    :func:`agent_cli.dialects.all_system_user_prefixes` so any
    registered dialect plugin's framing strings are picked up
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
                if not isinstance(content, str):
                    content = ""
                if msg.get("tool") or content.startswith("Observation:"):
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
    hp = _SESSIONS_DIR / meta.session_id / "history.jsonl"
    pairs = recent_exchanges(hp, n=1)
    return pairs[-1] if pairs else ("", "")
