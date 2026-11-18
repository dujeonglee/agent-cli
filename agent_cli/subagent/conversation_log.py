"""🤝 대화 창의 줄 — ``agents/<key>/conversation.jsonl``(창의 진짜 소스) 쓰기·읽기와 창에 내기.

규칙(v10.146.5): **기록이 원본이고 화면은 그 순서를 따른다 — 막히지 않는 표면만 배달 구간 안에서 낸다.** 언제 부르는지
(레지스트리의 ``_cv`` 구간 안·밖)는 `AgentRegistry._lines_in_section`·`_lines_after_section` 이 정한다. 여기는 그 재료다.
줄은 ``(owner, payload)`` — ``owner`` 는 ``home_dir`` 를 가진 에이전트, ``payload`` 는 `Renderer.agent_message` 의 인자.
"""

from __future__ import annotations

import json
from pathlib import Path


def path_of(home_dir: Path) -> Path:
    return home_dir / "conversation.jsonl"


def append(lines) -> None:
    """줄들을 각 에이전트의 파일에 append — best-effort(디스크 오류는 삼킨다)."""
    for owner, payload in lines:
        try:
            owner.home_dir.mkdir(parents=True, exist_ok=True)
            with path_of(owner.home_dir).open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass


def read(home_dir: Path) -> list[dict]:
    """파일의 창 줄(``key`` 가 있는 dict)을 순서대로 — 없거나 못 읽으면 빈 목록, 깨진 줄은 건너뛴다."""
    try:
        with path_of(home_dir).open(encoding="utf-8") as f:
            raw = list(f)
    except OSError:
        return []
    out = []
    for line in raw:
        try:
            rec = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("key"):
            out.append(rec)
    return out


def never_blocks() -> bool:
    """지금 렌더러의 `agent_message` 가 막히지 않는가 — `Renderer.agent_message_never_blocks` 가 바로 ``True`` 일 때만
    (속성이 없거나 다른 값이면 막힐 수 있는 표면으로 본다 — 무엇이든 받아 주는 대리 객체 포함)."""
    from agent_cli.render import get_renderer

    return getattr(get_renderer(), "agent_message_never_blocks", False) is True


def show(payloads) -> None:
    """창에 낸다(`agent_message`) — 표시용이라 실패를 삼킨다."""
    from agent_cli.render import get_renderer

    renderer = get_renderer()
    for payload in payloads:
        try:
            renderer.agent_message(**payload)
        except Exception:
            pass  # 표시용 — 전송 경로를 막지 않는다
