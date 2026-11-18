""" "누가 이 주소를 깨우는가" 의 에이전트 몫 — `wake` 의 순수 함수를 손으로 만든 사본(`WakeFacts`)으로 (v10.146.0).

스레드도 레지스트리도 없다. 줄마다 한 사실. ``[지금 동작]`` 표지는 알려진 결함을 지금 동작으로 적은 칸이다 —
고치는 단계에서 이 줄이 바뀐다(죽은 채무자 — 4c, main 의 제자리 대기·깨우지 않는 봉투 — 5단계).
"""

from __future__ import annotations

import pytest

from agent_cli.wake import (
    AgentFact,
    WakeFacts,
    agent_relations,
    parked_debtors,
    relation_items,
)

A, B, C = "agent:a", "agent:b", "agent:c"


def agent(addr, *, active=False, dead=False, queued=(), taken=()):
    return AgentFact(addr, active, dead, tuple(queued), tuple(taken))


def facts(agents=(), owing=(), awaited=()):
    return WakeFacts(tuple(agents), tuple(owing), frozenset(awaited))


RELATIONS = [
    # (이름, 사본, 주소, exclude, 관계)
    (
        "빚진 쪽이 일한다",
        facts([agent(A, active=True)], owing=[(A, "main")]),
        "main",
        frozenset(),
        [("owes", A)],
    ),
    (
        "빚지고 그 요청을 처리 중이면 working 하나",
        facts(
            [agent(A, active=True, taken=[("main", "r-1")])],
            owing=[(A, "main")],
            awaited=[("main", "r-1")],
        ),
        "main",
        frozenset(),
        [("working", A)],
    ),
    (
        "줄 선 요청",
        facts(
            [agent(A, active=True, queued=[("main", "r-2")])],
            awaited=[("main", "r-2")],
        ),
        "main",
        frozenset(),
        [("queued", A)],
    ),
    (
        "회신한 요청을 든 채 일함 — 근거 아님 (v10.143.0)",
        facts([agent(A, active=True, taken=[("main", "r-1")])]),
        "main",
        frozenset(),
        [],
    ),
    (
        "통지만 줄 섬 — 근거 아님",
        facts([agent(A, queued=[("main", "n-3")])]),
        "main",
        frozenset(),
        [],
    ),
    (
        "남이 보낸 요청은 세지 않는다",
        facts(
            [agent(A, active=True, taken=[(B, "r-1")])],
            awaited=[(B, "r-1")],
        ),
        "main",
        frozenset(),
        [],
    ),
    (
        "잠든 채무자는 관계가 아니다",
        facts([agent(A)], owing=[(A, "main")]),
        "main",
        frozenset(),
        [],
    ),
    (
        "exclude 는 빚진 쪽과 명단 둘 다에서 뺀다",
        facts(
            [agent(A, active=True, taken=[("main", "r-1")])],
            owing=[(A, "main")],
            awaited=[("main", "r-1")],
        ),
        "main",
        frozenset({A}),
        [],
    ),
    (
        "자기 자신은 명단에서 뺀다",
        facts([agent(A, active=True, queued=[(A, "r-1")])], awaited=[(A, "r-1")]),
        A,
        frozenset(),
        [],
    ),
    (
        "빚진 쪽 순서는 그 쪽의 첫 열린 줄 순서",
        facts(
            [agent(A, active=True), agent(B, active=True)],
            owing=[(B, "user"), (A, "main"), (B, "main")],
        ),
        "main",
        frozenset(),
        [("owes", B), ("owes", A)],
    ),
    (
        "[지금 동작] 죽은 채무자도 빚진 쪽으로 센다 (4c)",
        facts([agent(A, dead=True)], owing=[(A, "main")]),
        "main",
        frozenset(),
        [("owes", A)],
    ),
    (
        "[지금 동작] main 은 잠든 채무자가 아니다 — 언제나 빚진 쪽 (5단계)",
        facts([], owing=[("main", A)]),
        A,
        frozenset(),
        [("owes", "main")],
    ),
    (
        "[지금 동작] 깨우지 않는 봉투만 든 한가한 채무자도 잠들지 않은 것 (5단계)",
        facts([agent(A, queued=[(B, "n-9")])], owing=[(A, "main")]),
        "main",
        frozenset(),
        [("owes", A)],
    ),
    (
        "명단에 없는 채무자",
        facts([], owing=[(A, "main")]),
        "main",
        frozenset(),
        [("owes", A)],
    ),
]


@pytest.mark.parametrize("case", RELATIONS, ids=[c[0] for c in RELATIONS])
def test_agent_relations(case):
    _name, f, addr, exclude, want = case
    assert agent_relations(f, addr, exclude) == want


def test_relation_items_text():
    f = facts(
        [
            agent(A, active=True),
            agent(B, active=True, taken=[("main", "r-1")]),
            agent(C, queued=[("main", "r-2")]),
        ],
        owing=[(A, "main")],
        awaited=[("main", "r-1"), ("main", "r-2")],
    )
    assert relation_items(f, "main") == [
        (A, f"{A} owes you a reply"),
        (B, f"{B} working on your request"),
        (C, f"{C} has your request queued"),
    ]


PARKED = [
    ("잠든 채무자", facts([agent(A)], owing=[(A, "main")]), "main", [A]),
    ("일하는 채무자", facts([agent(A, active=True)], owing=[(A, "main")]), "main", []),
    (
        "받은편지함이 빈 것만",
        facts([agent(A, queued=[("x", "r-1")])], owing=[(A, "main")]),
        "main",
        [],
    ),
    ("죽은 채무자", facts([agent(A, dead=True)], owing=[(A, "main")]), "main", []),
    ("main 은 잠들지 않는다", facts([], owing=[("main", A)]), A, []),
    ("명단에 없는 채무자", facts([], owing=[(A, "main")]), "main", []),
    (
        "서로 빚진 둘 — 각자 상대를 잠든 채무자로 본다",
        facts([agent(A), agent(B)], owing=[(A, B), (B, A)]),
        A,
        [B],
    ),
]


@pytest.mark.parametrize("case", PARKED, ids=[c[0] for c in PARKED])
def test_parked_debtors(case):
    _name, f, addr, want = case
    assert parked_debtors(f, addr) == want
