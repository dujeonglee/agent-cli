"""Per-session web instance file — ``.agent-cli/sessions/<id>/web.json``.

Written when ``agent-cli web`` starts and removed when it exits, so an external
orchestrator (the "board") can answer *"is this session's web up, and where?"*
by reading one file::

    {"session_id": ..., "host": ..., "port": ..., "token": ..., "pid": ...}

The board reads it to spawn-or-attach: present + pid alive → redirect/proxy to
``host:port`` with ``token``; missing or dead pid → (re)spawn
``agent-cli web --resume <id> --idle-timeout N`` (which rewrites the file). The
instance self-reaps on idle (``--idle-timeout``) and removes the file on exit,
so the board never tracks or kills processes itself.

Pure read/write/remove — no server dependency, no global state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent_cli.fsio import atomic_write_json

_NAME = "web.json"
_STATUS_NAME = "status.json"


def instance_file_path(session_dir: str | Path) -> Path:
    return Path(session_dir) / _NAME


def write_instance_file(
    session_dir: str | Path,
    *,
    session_id: str,
    host: str,
    port: int,
    token: str,
    pid: int | None = None,
) -> Path:
    """Write (overwrite) the instance file. ``pid`` defaults to this process.
    Creates the session dir if missing. Returns the path."""
    info = {
        "session_id": session_id,
        "host": host,
        "port": port,
        "token": token,
        "pid": os.getpid() if pid is None else pid,
    }
    path = instance_file_path(session_dir)
    # 보드가 읽는 상태 파일 — 원자 교체 (fsio 패턴; 이전엔 비원자라
    # 보드가 half-write 를 읽을 수 있었음).
    atomic_write_json(path, info)
    return path


def read_instance_file(session_dir: str | Path) -> dict | None:
    """Return the instance info, or ``None`` if absent / unreadable / corrupt."""
    try:
        return json.loads(instance_file_path(session_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def remove_instance_file(session_dir: str | Path) -> None:
    """Remove the instance file if present (idempotent, best-effort)."""
    try:
        instance_file_path(session_dir).unlink()
    except (FileNotFoundError, OSError):
        pass


# ── Live status sidecar ────────────────────────────────────────────────
# ``status.json`` holds the frequently-changing liveness the board used to poll
# via ``GET /api/health`` — ``{busy, awaiting_input, viewers}``. Kept SEPARATE
# from ``web.json`` (the quasi-static host/port/token/pid handshake) because it
# is rewritten on every viewer/busy/awaiting change; the board reads this file
# instead of an HTTP round-trip. Writes are atomic (temp + ``os.replace``) so a
# concurrent reader never sees a half-written file.


def status_file_path(session_dir: str | Path) -> Path:
    return Path(session_dir) / _STATUS_NAME


def write_status_file(
    session_dir: str | Path,
    *,
    busy: bool,
    awaiting_input: bool,
    viewers: int,
    agents: dict | None = None,
    active_turns: int = 0,
) -> Path:
    """Atomically (over)write the live status sidecar. Returns the path.

    ``agents`` (v7.10.0, additive): 상주 에이전트 요약
    ``{"alive", "working", "list": [{key, profile, name, state}, ...]}`` —
    board 가 행에 🤖 칩과 "에이전트 작업 중" 상태를 그리는 소스. ``None``
    이면 필드 생략(에이전트 미사용/구버전 소비자와 동일 shape 유지).

    ``active_turns`` (v7.29.0, additive): 동시 inflight 사용자 턴 수 —
    병렬 모드(A1)에서만 0 이 아니다. **0 이면 필드를 생략**해 직렬 세션의
    status.json 바이트가 종전과 동일하게 유지된다(보드 파서 무영향)."""
    info = {
        "busy": bool(busy),
        "awaiting_input": bool(awaiting_input),
        "viewers": int(viewers),
    }
    if agents is not None:
        info["agents"] = agents
    if active_turns:
        info["active_turns"] = int(active_turns)
    path = status_file_path(session_dir)
    # fsio.atomic_write_json 이 같은 의미론(유니크 tmp + replace + 부모
    # 소실 가드)을 소유 — 자체 mkstemp 구현을 수렴 (v4.27.1 레이스 교훈은
    # fsio 모듈 docstring 으로 이주).
    atomic_write_json(path, info)


def read_status_file(session_dir: str | Path) -> dict | None:
    """Return the live status, or ``None`` if absent / unreadable / corrupt."""
    try:
        return json.loads(status_file_path(session_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def remove_status_file(session_dir: str | Path) -> None:
    """Remove the status sidecar if present (idempotent, best-effort)."""
    try:
        status_file_path(session_dir).unlink()
    except (FileNotFoundError, OSError):
        pass
