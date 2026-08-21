"""상주 에이전트의 결정적 이모지 아이콘 — key 를 안정적으로 하나의 아이콘에 매핑.

프론트(``web/static/app.js`` 의 ``OV_AGENT_ICONS`` / ``ovAgentIcon``)와 **반드시
동일한 풀·해시**여야 한다 — 같은 key 가 서버(스윔레인·@agent 주체 배지)와 웹
개요(채널 칩·배지·트레이) 양쪽에서 같은 아이콘으로 보여야 하기 때문.
교차검증: ``tests/test_app_markdown.py`` 가 app.js 의 풀/해시로 계산한 값과 여기
값을 대조한다. key 는 ASCII(``agt-<hex>``)라 ``ord`` == JS ``charCodeAt``.
"""

from __future__ import annotations

# 시각적으로 구분되는 "캐릭터/생물" 이모지 24종 (재미 + 식별성). 순서가 곧
# 해시 인덱스이므로 app.js 의 배열과 순서까지 동일해야 한다.
AGENT_ICONS: list[str] = [
    "🦊",
    "🐙",
    "🦉",
    "🦄",
    "🐳",
    "🦋",
    "🐢",
    "🐝",
    "🦁",
    "🐧",
    "🦩",
    "🐬",
    "🦇",
    "🐡",
    "🦕",
    "🐌",
    "🦔",
    "🦦",
    "🐨",
    "🐼",
    "🦭",
    "🦡",
    "🐺",
    "🐸",
]


def agent_icon(key: str) -> str:
    """``key`` 를 문자 코드 합으로 해시해 풀 인덱스를 뽑는다 — 결정적·안정적.
    빈 key 는 첫 아이콘."""
    if not key:
        return AGENT_ICONS[0]
    return AGENT_ICONS[sum(ord(c) for c in key) % len(AGENT_ICONS)]
