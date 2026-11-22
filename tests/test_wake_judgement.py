""" "누가 이 주소를 깨우는가" 의 에이전트 몫 — `wake` 의 순수 함수를 손으로 만든 사본(`WakeFacts`)으로 (v10.146.0).

스레드도 레지스트리도 없다. 줄마다 한 사실. 잠든 채무자는 창구의 기다림 표시(``waiting``)로 본다(5b) — main 도
잠든 쪽이 되고, 깨우지 않는 봉투를 든 채 기다리는 쪽도 잠든 쪽이다. 죽은 채무자는 깨울 것이 아니다(5b 항목 8).
줄 선 요청(``queued`` — 장부의 줄 선 r 줄 ``(받는 쪽, 요청자)``)은 받는 쪽이 main 이든 에이전트든 "queued" 다(5c-2 C).
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


def agent(addr, *, active=False, dead=False, taken=()):
    return AgentFact(addr, active, dead, tuple(taken))


def facts(agents=(), owing=(), queued=(), awaited=(), waiting=()):
    return WakeFacts(
        tuple(agents),
        tuple(owing),
        tuple(queued),
        frozenset(awaited),
        frozenset(waiting),
    )


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
            [agent(A, active=True)],
            queued=[(A, "main")],
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
        "통지만 줄 섬 — 근거 아님(통지는 장부에 줄이 없다)",
        facts([agent(A)]),
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
        facts([agent(A)], owing=[(A, "main")], waiting=[A]),
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
        facts([agent(A, active=True)], queued=[(A, A)], awaited=[(A, "r-1")]),
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
        "죽은 채무자는 깨울 것이 아니다 (5b 항목 8)",
        facts([agent(A, dead=True)], owing=[(A, "main")]),
        "main",
        frozenset(),
        [],
    ),
    (
        "기다림 표시가 없는 main 은 빚진 쪽",
        facts([], owing=[("main", A)]),
        A,
        frozenset(),
        [("owes", "main")],
    ),
    (
        "기다리는 main 은 잠든 채무자 — 관계가 아니다 (5b N4)",
        facts([], owing=[("main", A)], waiting=["main"]),
        A,
        frozenset(),
        [],
    ),
    (
        "기다림 표시가 없는 한가한 채무자는 빚진 쪽",
        facts([agent(A)], owing=[(A, "main")], queued=[(A, B)]),
        "main",
        frozenset(),
        [("owes", A)],
    ),
    (
        "남의 줄 선 요청을 든 채 기다리는 채무자도 잠든 쪽 (5b D5)",
        facts([agent(A)], owing=[(A, "main")], queued=[(A, B)], waiting=[A]),
        "main",
        frozenset(),
        [],
    ),
    (
        "명단에 없는 채무자",
        facts([], owing=[(A, "main")]),
        "main",
        frozenset(),
        [("owes", A)],
    ),
    # ── 줄 선 요청은 장부가 말한다 (5c-2 C) ──
    (
        "main 앞에 줄 선 내 요청 — main 이 일하는 중(c1)",
        facts([agent(A, active=True)], queued=[("main", A)], awaited=[(A, "r-3")]),
        A,
        frozenset(),
        [("queued", "main")],
    ),
    (
        "main 앞에 줄 선 내 요청 — main 이 기다리는 중이어도(c2): 그 봉투가 main 을 깨운다",
        facts(
            [agent(A, active=True, taken=[("main", "r-2")])],
            owing=[(A, "main")],
            queued=[("main", A)],
            awaited=[("main", "r-2"), (A, "r-3")],
            waiting=["main"],
        ),
        A,
        frozenset(),
        [("queued", "main")],
    ),
    (
        "기다리는 에이전트 앞에 줄 선 내 요청(c3) — 한 번만 나온다",
        facts(
            [agent(A, active=True), agent(B, active=True)],
            queued=[(B, A)],
            awaited=[(A, "r-4")],
            waiting=[B],
        ),
        A,
        frozenset(),
        [("queued", B)],
    ),
    (
        "main 이 내 요청 하나를 집었고 하나는 줄 서 있다 — queued 하나(owes 보다 앞)",
        facts([], owing=[("main", A)], queued=[("main", A)]),
        A,
        frozenset(),
        [("queued", "main")],
    ),
    (
        "남이 main 에게 보낸 줄 선 요청은 세지 않는다",
        facts([agent(A), agent(B)], queued=[("main", B)]),
        A,
        frozenset(),
        [],
    ),
    (
        "명단의 에이전트는 명단 순서, 명단에 없는 main 은 그 뒤",
        facts(
            [agent(A), agent(B, active=True, taken=[(C, "r-1")]), agent(C)],
            queued=[("main", C), (A, C)],
            awaited=[(C, "r-1")],
        ),
        C,
        frozenset(),
        [("queued", A), ("working", B), ("queued", "main")],
    ),
    (
        "exclude 는 줄 선 main 도 뺀다",
        facts([], queued=[("main", A)]),
        A,
        frozenset({"main"}),
        [],
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
            agent(C),
        ],
        owing=[(A, "main")],
        queued=[(C, "main")],
        awaited=[("main", "r-1"), ("main", "r-2")],
    )
    assert relation_items(f, "main") == [
        (A, f"{A} owes you a reply"),
        (B, f"{B} working on your request"),
        (C, f"{C} has your request queued"),
    ]


PARKED = [
    (
        "잠든 채무자",
        facts([agent(A)], owing=[(A, "main")], waiting=[A]),
        "main",
        [A],
    ),
    (
        "기다림 표시가 없으면 한가해도 잠든 쪽이 아니다",
        facts([agent(A)], owing=[(A, "main")]),
        "main",
        [],
    ),
    ("일하는 채무자", facts([agent(A, active=True)], owing=[(A, "main")]), "main", []),
    (
        "받은편지함에 무엇이 있어도 기다리면 잠든 쪽 (5b D5)",
        facts([agent(A)], owing=[(A, "main")], queued=[(A, "x")], waiting=[A]),
        "main",
        [A],
    ),
    (
        "죽은 채무자",
        facts([agent(A, dead=True)], owing=[(A, "main")], waiting=[A]),
        "main",
        [],
    ),
    ("표시 없는 main", facts([], owing=[("main", A)]), A, []),
    (
        "줄 선 요청만 든 채 기다리는 main 은 잠든 채무자가 아니다 (c2 — 고리 거절이 아니다)",
        facts([], queued=[("main", A)], waiting=["main"]),
        A,
        [],
    ),
    (
        "줄 선 요청만 든 채 기다리는 에이전트도 잠든 채무자가 아니다 (c3)",
        facts([agent(B)], queued=[(B, A)], waiting=[B]),
        A,
        [],
    ),
    (
        "기다리는 main 은 잠든 채무자 (5b N4)",
        facts([], owing=[("main", A)], waiting=["main"]),
        A,
        ["main"],
    ),
    (
        "명단에 없는 채무자",
        facts([], owing=[(A, "main")], waiting=[A]),
        "main",
        [],
    ),
    (
        "서로 빚진 둘 — 각자 상대를 잠든 채무자로 본다",
        facts([agent(A), agent(B)], owing=[(A, B), (B, A)], waiting=[A, B]),
        A,
        [B],
    ),
]


@pytest.mark.parametrize("case", PARKED, ids=[c[0] for c in PARKED])
def test_parked_debtors(case):
    _name, f, addr, want = case
    assert parked_debtors(f, addr) == want
