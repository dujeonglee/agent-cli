"""구제 패키지 — 드리프트한 출력을 ops 로 되살리는 2단계 기계 (Phase 5, PHASE5.md §4.4).

역할은 하나(stage-2 구제), 기계는 둘이고 코드 공유는 0 이다:
- ``recovery.json`` — JSON 수리·추출 (json_fc, hermes_json)
- ``recovery.tagged`` — 태그 변종·closer·값 끝 (xml_fc, glm_argkey)

여기 ``__init__`` 는 둘의 **앞단**만 둔다: 인용 영역(펜스·인라인 코드) 계산.
S1 은 이동만 — 공용 앞단의 확장(세그먼트 자격·Recovered 결과 타입)은 S2 에서.
"""

from __future__ import annotations

import re

_FENCE_TICKS = re.compile(r"```")
# 한 줄 안의 단일 백틱 쌍 — ``` 의 일부인 백틱은 제외.
_INLINE_CODE = re.compile(r"(?<!`)`(?!`)[^`\n]+`(?!`)")


def quote_spans(text: str) -> list[tuple[int, int]]:
    """Quoted spans: balanced ``` pairs, then inline code spans outside them.
    An unpaired trailing fence masks nothing."""
    ticks = [m.start() for m in _FENCE_TICKS.finditer(text)]
    spans = [(ticks[i], ticks[i + 1] + 3) for i in range(0, len(ticks) - 1, 2)]
    for m in _INLINE_CODE.finditer(text):
        if not any(a <= m.start() < b for a, b in spans[: len(ticks) // 2]):
            spans.append((m.start(), m.end()))
    return spans
