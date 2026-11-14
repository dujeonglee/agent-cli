"""Session persistence — project-local, session-scoped file management.

Stores session metadata in session.jsonl (single line).
Conversation history is managed by ContextManager (history.jsonl).

File layout (declared ONCE here — v10.42.0; the makers and the one reader
import these names instead of spelling them):
  {project}/.agent-cli/sessions/{session_id}/
    session.jsonl          # single-line metadata (id, workspace, updated_at, title?)
    history.jsonl          # main conversation (managed by ContextManager)
    agents/<key>/          # resident teammates (AGENTS_DIR)
    run_*/  skill_*/       # one-shot delegates / skill runs, nestable
                           #   (RUN_PREFIX, SKILL_PREFIX)
    harness/<job>/<label>/ # harness jobs (HARNESS_DIR) — NOT the session's
                           #   conversation: the harness ran them, the model
                           #   did not, so ``conversation_history_files``
                           #   leaves the subtree out
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, fields
from pathlib import Path

from agent_cli.fsio import atomic_write_text
from agent_cli.paths import sessions_dir

_SESSIONS_DIR = sessions_dir()

# ── 세션 디렉토리 배치 (v10.42.0) ──────────────────────────────────────
#: 상주 팀원의 자리 ``agents/<key>/`` (subagent/agents_live 가 만든다).
AGENTS_DIR = "agents"
#: 일회성 delegate 런 ``run_<name>_<hash>_<ts>/`` (subagent/report 가 이름 짓는다).
RUN_PREFIX = "run"
#: 스킬 런 ``skill_<name>_<hash>_<ts>/`` (loop/skill_invoke 가 이름 짓는다).
SKILL_PREFIX = "skill"
#: 하니스 작업 ``harness/<job>/<label>/`` (harness/jobs 가 만든다).
HARNESS_DIR = "harness"
#: 모든 런이 자기 디렉토리에 남기는 대화 기록 파일 이름.
HISTORY_NAME = "history.jsonl"


def harness_run_dir(session_dir: Path, job: str, label: str) -> Path:
    """하니스 작업 한 번의 자리 — 세션 안이라 세션과 함께 지워지고 이전되지만,
    대화 테이블 밖이다(``conversation_history_files``)."""
    return Path(session_dir) / HARNESS_DIR / job / label


def conversation_history_files(session_dir: Path) -> list[Path]:
    """이 세션의 **모델이 한 대화**의 기록 파일 전부 — main, 팀원, delegate,
    스킬(중첩 포함). 하니스 작업(``HARNESS_DIR`` 아래)만 뺀다: 하니스가 돌린
    런이라 "내가 한 일" 질의에 사용자 요청 모양의 잡음으로 섞이기 때문이다.
    `history` 도구의 테이블이 이것을 읽는다 — 배치를 아는 모듈은 여기뿐이다."""
    root = Path(session_dir)
    harness = root / HARNESS_DIR
    return sorted(
        hp for hp in root.rglob(HISTORY_NAME) if not hp.is_relative_to(harness)
    )


@dataclass
class SessionMeta:
    session_id: str
    workspace: str
    updated_at: str
    #: 방 제목 (v10.71.0) — ``--title`` 로 받는다(보드가 글의 topic 을 넘긴다). 빈 문자열
    #: = 없음(메타에 키를 적지 않는다). 세션 도중 바꾸지 않는 정체 정보라 노브가 아니다;
    #: ``--resume`` 은 저장값을 되살리고, ``--title`` 이 같이 오면 그 값으로 갱신한다.
    title: str = ""


def get_session_dir(meta: SessionMeta) -> Path:
    """Return the session directory path, creating it if needed."""
    d = _SESSIONS_DIR / meta.session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


#: 새 세션 id 를 하나씩 올려 보는 횟수의 상한 (v10.127.4) — 같은 초에 이만큼
#: 붐비는 일은 없다; 넘으면 무언가 잘못된 것이라 조용히 돌지 않고 멈춘다.
_ID_CLAIM_ATTEMPTS = 1000


def create_session(workspace: str | None = None) -> SessionMeta:
    """Create a new session for the given workspace (defaults to CWD).

    새 세션은 디렉터리를 **배타적으로 차지하며** 태어난다 (v10.127.4). 종전엔 id 가
    ``int(time.time())`` 뿐이라 같은 초에 뜬 두 프로세스(스크립트의 연속·병렬 ``run``)가
    한 디렉터리에 history 를 섞어 썼다. ``mkdir(exist_ok=False)`` 가 원자적이므로
    이미 있으면 id 를 1 올려 다시 — id 는 숫자 문자열로 남는다(정렬·표시·
    ``--resume <id>`` 가 그것을 전제한다). 호출자(run·web)는 부트가 끝난 뒤 부르고
    곧바로 ``save_meta`` 하므로 빈 디렉터리가 남는 길은 없다.
    """
    ws = workspace or os.getcwd()
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    base = int(time.time())
    for n in range(_ID_CLAIM_ATTEMPTS):
        sid = str(base + n)
        try:
            (_SESSIONS_DIR / sid).mkdir()
        except FileExistsError:
            continue
        return SessionMeta(
            session_id=sid,
            workspace=ws,
            updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
    raise RuntimeError(
        f"no free session id in {_SESSIONS_DIR} after {_ID_CLAIM_ATTEMPTS} tries"
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
                # title 은 있을 때만 — 옛 리더(보드 sessions 조회)가 모르는 키를 보지 않게
                **({"title": meta.title} if meta.title else {}),
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
