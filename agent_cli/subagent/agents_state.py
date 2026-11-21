"""에이전트 상태의 어휘와 ``agents.json`` 의 모양 — "작업 중" 판정, 에이전트 하나 ↔ 저장 항목, 파일 한 장의
머리(버전)와 읽기.

레지스트리(`agents_live`)가 언제 저장하고 무엇을 되살릴지 정한다(락·명단·워커). 여기는 그 사이의 직렬화만
— 상태를 갖지 않는 잎이고 레지스트리를 import 하지 않는다. `agent_entry` 는 에이전트를 읽기만 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli.hooks.shell import hooks_from_json, hooks_to_json
from agent_cli.session.store import AGENTS_DIR

# agents.json 스키마 버전 — 비호환 변경 시 bump (구버전 파일은 무시=fresh).
#: v2 (v10.77.0): 순번(`seq`) 대신 id 가 키 — 옛 파일은 무시한다(호환층 없음).
AGENTS_STATE_VERSION = 2


def state_is_active(state: str) -> bool:
    """ "작업 중" 판정의 단일 소유 — worker 상태 어휘는
    starting|idle|busy|dead (AgentInstance.state). idle/dead 가 아니면
    mid-task 다(starting=곧 첫 요청 처리). ★v7.11.1: 표시/넛지/reap
    코드가 존재하지 않는 "working" 문자열을 비교하던 버그의 수리 —
    어휘는 여기서만."""
    return state not in ("idle", "dead")


def agent_entry(tm) -> dict:
    """에이전트 하나(`AgentInstance`) → ``agents`` 목록의 한 항목 — 명단 칸만(5c-0: 재시작은 컨텍스트와 명단만
    올린다 — 받은편지함·하던 일·빚·메일·질문은 남기지 않는다). 읽기만 한다."""
    return {
        "key": tm.key,
        "profile": tm.profile_name,
        "name": tm.instance_name,
        "description": tm.description,
        "instructions": tm.instructions,
        # role_prompt 를 통째로 저장 — resume 시 역할 md 파일이
        # 지워졌어도 teammate 는 갖고 있던 역할 그대로 살아난다.
        "role_prompt": tm.role_prompt,
        "allowed_tools": tm.allowed_tools,
        "model": tm.model,
        "hooks_config": hooks_to_json(tm.hooks_config),
        "context_mode": tm.context_mode,
        "created_at": tm.created_at,
        "handled": tm.handled,
        # 논리 생사: kill(revivable=False)·에러만 영구 dead —
        # 세션 종료로 멈춘 teammate 는 idle 로 남아 resume 대상.
        "state": "dead" if (not tm.revivable or tm.error) else "idle",
        "error": tm.error,
        # 띄운 쪽의 깊이·스택 — 복원된 에이전트가 같은 자리에서 다시 선다
        "depth": int((tm.caller_runtime or {}).get("depth", 0) or 0),
        "agent_stack": list(tm.agent_stack),
        # 띄운 쪽 — ✕ 가 그 run 을 함께 멈춘다 (v10.142.0, 더해진 키)
        **({"parent": tm.parent} if tm.parent else {}),
        # run 에이전트: 과제 하나 — 살아 있는 run 은 과제 도중이라 resume 이 dead 로 세운다(5c-0)
        **(
            {"ends_after_task": True, "task_id": tm.task_id}
            if tm.ends_after_task
            else {}
        ),
    }


def roster_document(tms) -> dict:
    """파일 한 장 — 명단에 버전을 단다. 옛 파일(v10.158 까지)의 ``pending``·``questions``·``inbox``·``in_flight`` 칸은
    쓰지도 읽지도 않는다(같은 버전 2 — 명단 칸의 모양이 같다)."""
    return {"version": AGENTS_STATE_VERSION, "agents": [agent_entry(tm) for tm in tms]}


def read_state(path: Path) -> dict | None:
    """``agents.json`` 을 읽는다 — 파일 부재·파손·버전 불일치는 None(fresh 세션과 같다)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != AGENTS_STATE_VERSION:
        return None
    return data


def instance_args(e: dict, *, session_dir: Path, runtime: dict) -> dict:
    """저장 항목 하나 → `AgentInstance` 의 키워드 인자(``key`` 밖). 숫자·상태 필드는 레지스트리가 따로 얹는다."""
    key = e.get("key")
    # 집 — 옛 파일(v10.140.0)의 run 에이전트는 세션 디렉터리의 ``run_…`` 에 살았다(``home``)
    home = str(e.get("home") or "")
    depth = int(e.get("depth", 0) or 0)
    return {
        "profile_name": e.get("profile", ""),
        "role_prompt": e.get("role_prompt", ""),
        "allowed_tools": e.get("allowed_tools"),
        "model": e.get("model", ""),
        "hooks_config": hooks_from_json(e.get("hooks_config")) or None,
        "context_mode": e.get("context_mode", "none"),
        "home_dir": (
            session_dir / Path(home).name if home else session_dir / AGENTS_DIR / key
        ),
        "instance_name": e.get("name", ""),
        "description": e.get("description", ""),
        "instructions": e.get("instructions", ""),
        "ends_after_task": bool(e.get("ends_after_task")),
        # 띄운 쪽이 main 이 아니었으면 그 깊이로 (배선은 지금 세션의 것)
        "caller_runtime": {**runtime, "depth": depth} if depth else None,
        "agent_stack": [str(x) for x in (e.get("agent_stack") or [])],
        "parent": str(e.get("parent") or ""),
    }
