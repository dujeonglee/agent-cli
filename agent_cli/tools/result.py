"""Standardized return type for all tool functions."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolResult:
    """Standardized return type for all tool functions."""

    success: bool
    output: str = ""
    error: str = ""
    artifact: str = ""  # artifact path (delegate subdir, etc.)
    #: 모델에게 보여 줄 이미지 참조 (v10.59.0, ``images.ImageRef.to_dict()``
    #: 모양). ``output`` 은 여전히 글(자리표시 줄 포함) — 이미지는 관찰
    #: 레코드의 ``images`` 로 실려 공급자 경계에서 조각이 된다.
    images: list[dict] = field(default_factory=list)
    #: 런이 세션 종료로 끝났다 (5e1) — 일하던 작업 구간이 main 상자가 닫혀 멈췄다. 답이 아니다(받을 이가 없다).
    session_ended: bool = False
    #: 그 작업 구간이 든 사람의 요청 번호(``u-``) — 연 것이든 도중에 받은 것이든, 닫았든 아니든. 감사와 `run` 의
    #: 결과 파일이 이것으로 판정한다(`AgentLoop.run` 이 장부에서 싣는다).
    human_requests: tuple[str, ...] = ()
