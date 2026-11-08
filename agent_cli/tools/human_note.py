"""확인 대화상자에서 사람이 남긴 말 — 모으는 곳 하나, 붙이는 곳 하나 (v10.120.2).

사람의 코멘트는 **확인에서 생기는 사건**이다. 종전(v10.120.1)엔 확인을 부른 도구
9곳이 각자 성공 결과에만 붙였다 — 승인 뒤 도구가 실패하면 말이 사라지고, 붙는
자리가 도구마다 달랐고, 거절 코멘트는 거절 문구 안이라는 또 다른
길로 갔다.

- **모으기**: 안전 확인 두 곳(셸 `confirm_dangerous`, `_confine.guard`)이 답을
  받은 직후 코멘트가 비어 있지 않으면 :func:`record` 한다. ⚡ 자동 승인은 코멘트가
  없어 기록도 없다. StaleGuard 의 확인은 기록하지 않는다(그 말은 Hint 로 사용자
  메시지가 된다).
- **붙이기**: `loop/tool_bridge.py` 의 실행 지점(`_dispatch_tool_with_hooks`·
  `dispatch_edit_batch`)이 실행 전에 :func:`clear`, 실행 뒤에 :func:`take` 한 줄들을
  결과 끝에 — 성공이면 output, 실패면 error 에.

보관함은 **스레드별**이다: 병렬 위임·상주 에이전트의 워커 스레드에서 뜬 확인은 그
스레드의 보관함에 쌓이고 그 스레드의 bridge 가 꺼낸다.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Literal

Stance = Literal["approving", "denying"]

_local = threading.local()


def _box() -> list[str]:
    box = getattr(_local, "notes", None)
    if box is None:
        box = _local.notes = []
    return box


def line(stance: Stance, comment: str) -> str:
    """결과 끝에 붙는 한 줄 — 승인은 v10.120.1 과 같은 글자."""
    return f"[User note when {stance}: {comment}]"


def record(stance: Stance, comment: str) -> None:
    """확인 답의 코멘트를 이 스레드의 보관함에. 빈 코멘트는 무시."""
    if comment:
        _box().append(line(stance, comment))


def take() -> list[str]:
    """쌓인 줄들(기록 순서)을 꺼내고 비운다."""
    box = _box()
    out = list(box)
    box.clear()
    return out


def clear() -> None:
    """앞선 호출에서 남은 것을 버린다 — 실행 전에 부른다."""
    _box().clear()


def attach(result, notes: list[str]):
    """``result``(ToolResult) 끝에 ``notes`` 를 줄마다 — 성공이면 output, 실패면
    error. 새 결과를 돌려준다(도구가 돌려준 객체는 건드리지 않는다). 줄이 없으면
    그대로 — 그 경우 모델 입력은 종전과 같다."""
    if not notes:
        return result
    if result.success:
        return replace(result, output="\n".join([result.output or "", *notes]))
    return replace(result, error="\n".join([result.error or "", *notes]))
