"""상주 에이전트의 결정적 **시각 정체성** — key → 아이콘 + 별명.

프론트(``web/static/app.js`` 의 ``OV_AGENT_*`` / ``ovAgentIcon`` /
``ovAgentNickname``)와 **반드시 동일한 풀·해시**여야 한다 — 같은 key 가
서버(스윔레인·@agent 주체 배지·CLI 문답 창)와 웹 개요(채널 칩·배지·트레이)
양쪽에서 같은 얼굴로 보여야 하기 때문. 교차검증: ``tests/test_app_markdown.py``
가 app.js 의 풀/해시로 계산한 값과 여기 값을 대조한다. key 는
ASCII(``agt-<hex>``)라 ``ord`` == JS ``charCodeAt``.

**별명은 표시 전용이다.** 주소는 언제나 key(``@agt-<key>``, 도구의
``{"key": …}``)이고, 별명은 사람이 읽고 기억하기 위한 것이라 화면에서 늘
key 를 곁에 흐리게 달고 다닌다. 별명을 주소로 받기 시작하면 유일성(같은
이름 둘)·경로(``agents/<key>/``)·rename 이 전부 따라오는데, 지금 그 셋은
key 가 uuid 라서 **구조적으로** 해결돼 있다.

그래서 이 함수들은 **순수 함수**다 — 살아 있는 다른 에이전트를 보지 않는다.
24×16 이라 별명 충돌은 가능하지만, 곁의 key 가 언제나 구분해 주므로 모호해지지
않는다. 로스터를 봐야 하는 `#N` 중복 회피(사람 닉네임의 ``_assign_nickname_locked``
가 하는 일)를 넣으면 같은 key 가 접속 시점마다 다른 이름이 되어, resume·재접속
에서 얼굴이 바뀐다 — 결정성이 중복 회피보다 값지다.

별명은 v10.62.0 부터 영어다("Swift Fox" — 화면 문자열 영어 단일화). 종전엔
한국어 별명이 사람 닉네임 풀(``render/web.py`` 의 ``_NICKNAMES``, "Funky
Gecko" 류)과 언어로 갈렸는데, 그 2차 구분은 사라지고 **아이콘 접두 + 곁의
key** 가 사람과 에이전트를 가른다(사람 쪽은 묻기·회신 줄에 "(human)" 이 붙는다).
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

# 아이콘과 **같은 인덱스**의 이름 — "🦊 날쌘 여우" 가 어긋나지 않도록 1:1.
# (아이콘 풀을 고치면 여기도 같은 자리를 고쳐야 한다 — 길이 일치는 테스트가 고정.)
AGENT_ANIMALS: list[str] = [
    "Fox",
    "Octopus",
    "Owl",
    "Unicorn",
    "Whale",
    "Butterfly",
    "Turtle",
    "Bee",
    "Lion",
    "Penguin",
    "Flamingo",
    "Dolphin",
    "Bat",
    "Pufferfish",
    "Dinosaur",
    "Snail",
    "Hedgehog",
    "Otter",
    "Koala",
    "Panda",
    "Seal",
    "Badger",
    "Wolf",
    "Frog",
]

# 동물과 **독립적으로** 해시되는 수식어 — 24×16=384 조합.
AGENT_ADJECTIVES: list[str] = [
    "Swift",
    "Easygoing",
    "Meticulous",
    "Brave",
    "Quiet",
    "Quirky",
    "Diligent",
    "Clever",
    "Steady",
    "Nimble",
    "Careful",
    "Kind",
    "Reliable",
    "Busy",
    "Calm",
    "Cheerful",
]


def _hash(key: str) -> int:
    """문자 코드 합 — JS ``charCodeAt`` 합과 같은 값 (key 는 ASCII)."""
    return sum(ord(c) for c in key)


def agent_icon(key: str) -> str:
    """``key`` 를 문자 코드 합으로 해시해 풀 인덱스를 뽑는다 — 결정적·안정적.
    빈 key 는 첫 아이콘."""
    if not key:
        return AGENT_ICONS[0]
    return AGENT_ICONS[_hash(key) % len(AGENT_ICONS)]


def agent_nickname(key: str) -> str:
    """``key`` → "Swift Fox" — **표시 전용** 별명 (v9.9.0).

    동물은 ``agent_icon`` 과 같은 인덱스(아이콘과 짝이 맞는다), 수식어는 그
    나머지 자릿수로 독립 선택. 빈 key 는 첫 조합.
    """
    h = _hash(key)
    animal = AGENT_ANIMALS[h % len(AGENT_ANIMALS)]
    adjective = AGENT_ADJECTIVES[(h // len(AGENT_ANIMALS)) % len(AGENT_ADJECTIVES)]
    return f"{adjective} {animal}"


def agent_display_name(key: str, profile: str = "", name: str = "") -> str:
    """화면에 쓸 **이름** — key 는 붙이지 않는다 (호출부가 흐리게 병기).

    우선순위는 **구체적인 것부터**: 인스턴스 이름 > 프로파일 > 별명. 프로파일이
    있으면 "code-writer" 가 "Swift Fox" 보다 많은 것을 말해 주므로 별명은
    **폴백**이다 — 별명이 필요한 건 프로파일 없는 즉석 에이전트다.
    """
    if name and profile:
        return f"{profile} · {name}"
    return name or profile or agent_nickname(key)
