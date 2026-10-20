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
