"""도구를 바꾸는 층 (v10.77.0, docs/vision/DESIGN.md §12.2).

모델이 부른 도구를 결과가 **똑같다고 보장될 때만** 다른 도구의 호출로 바꾼다.
바뀐 호출이 history 레코드와 컨텍스트에 그대로 남고(모델이 이후 턴에 보는 자기
기록이 곧 올바른 도구의 예시가 된다), 원래 호출은 ``Op.rewritten_from`` 에만
남는다 — 레코드의 ``rewritten_from`` 은 모델에게 그리지 않는다. 결과에 알림 줄은
붙이지 않는다.

도구에서 도구로 바꾸는 것만 다룬다. 인자 모양 구제는 각 도구 안에 둔다 —
섞으면 이 층이 무엇이든 받아 주는 곳이 된다.
"""

from __future__ import annotations

from pathlib import Path

from agent_cli import images as _images


def _read_file_image(op, tools: frozenset[str]) -> bool:
    """read_file(이미지 파일) → view_image. 판정은 read_file 이 v10.59.0 부터 쓰던
    매직 바이트 그대로다. 줄 범위·search·stat 은 이미지에 뜻이 없어 버린다 —
    바뀐 호출이 view_image 스키마에 맞아야 한다(원래 인자는 rewritten_from 에)."""
    if op.action != "read_file" or "view_image" not in tools:
        return False
    ai = op.action_input
    path = ai.get("path") if isinstance(ai, dict) else None
    if not isinstance(path, str) or not path:
        return False
    if _images.sniff_file(Path(path)) is None:
        return False
    op.rewritten_from = {"action": op.action, "action_input": dict(ai)}
    op.action = "view_image"
    op.action_input = {"path": path}
    return True


#: (규칙 이름, 판정+변환) — 순서대로 처음 맞는 규칙 하나만 적용한다.
_RULES = (("read_file>view_image", _read_file_image),)


def reroute(ops, tools) -> list[str]:
    """``ops`` 를 제자리에서 바꾸고 적용한 규칙 이름을 op 마다 돌려준다
    (``turns.jsonl`` 의 primitives — "설명이 먹히는가" 의 계수)."""
    available = frozenset(tools)
    applied: list[str] = []
    for op in ops:
        for name, rule in _RULES:
            if rule(op, available):
                applied.append(f"reroute:{name}")
                break
    return applied
