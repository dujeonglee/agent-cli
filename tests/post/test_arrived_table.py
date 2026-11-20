"""창구의 도착 조회 — 상자 모양 × (깨우는 것만, 사람의 말 포함) 두 인자의 네 칸 (단일 구조 5a).

표의 수가 정본이다. 네 칸은 판정 자리마다 다르게 쓰인다(`tests/test_wake_queries_table.py` 가 자리와 칸을
잇는다): ``wait`` 의 도착분은 (False, 사람의 말을 세는가), 런 열기·할 일은 (True, True), run 의 main 메일 항은
(True, False). 스레드도 레지스트리도 없다.
"""

from __future__ import annotations

import pytest

from agent_cli.post import envelope as E
from agent_cli.post.office import PostOffice

A = "agent:a"

# main 앞 봉투
W = lambda i: E.words(f"u-{i}", "user:dj", "main", f"w{i}")  # 사람의 말
N = lambda i: E.notice(f"n-{i}", "job", "main", "done")  # 깨우는 메일
Q = lambda i: E.reply(f"n-{i}", "agent:b", "main", "x", re="r-0", wakes=False)
# 에이전트 앞 봉투
H = lambda i: E.human(f"u-{i}", "user:dj", A, f"h{i}")  # 사람의 말(줄 있음)
R = lambda i: E.request(f"r-{i}", "main", A, f"do {i}")  # 깨운다
S = lambda i: E.reply(f"n-{i}", "agent:b", A, "x", re="r-0", wakes=False)

#: 칸의 순서 — (waking_only, words)
CELLS = ((False, False), (False, True), (True, True), (True, False))

#: (이름, 주소, 넣는 봉투, 칸별 수)
TABLE = [
    ("main_empty", "main", [], (0, 0, 0, 0)),
    ("main_words", "main", [W(1)], (0, 1, 1, 0)),
    ("main_waking_mail", "main", [N(1)], (1, 1, 1, 1)),
    ("main_quiet_reply", "main", [Q(1)], (1, 1, 0, 0)),
    ("main_quiet_then_waking", "main", [Q(1), N(2)], (2, 2, 1, 1)),
    ("main_words_and_quiet", "main", [W(1), Q(2)], (1, 2, 1, 0)),
    ("agent_empty", A, [], (0, 0, 0, 0)),
    ("agent_human", A, [H(1)], (0, 1, 1, 0)),
    ("agent_request", A, [R(1)], (1, 1, 1, 1)),
    ("agent_quiet_reply", A, [S(1)], (1, 1, 0, 0)),
    ("agent_quiet_then_request", A, [S(1), R(2)], (2, 2, 1, 1)),
    ("agent_human_and_quiet", A, [H(1), S(2)], (1, 2, 1, 0)),
]


def arrived(po: PostOffice, addr: str, *, waking_only: bool, words: bool) -> int:
    """창구의 도착 조회 한 칸."""
    return po.arrived(addr, waking_only=waking_only, words=words)


@pytest.mark.parametrize("name,addr,envs,counts", TABLE, ids=[r[0] for r in TABLE])
def test_arrived_cells(name, addr, envs, counts):
    po = PostOffice()
    for env in envs:
        po.deliver(env)
    got = tuple(arrived(po, addr, waking_only=w, words=x) for w, x in CELLS)
    assert got == counts


def test_unknown_address_counts_nothing():
    po = PostOffice()
    assert [arrived(po, "agent:zz", waking_only=w, words=x) for w, x in CELLS] == [
        0
    ] * 4


def test_taken_envelopes_are_not_arrived():
    """런이 꺼내 든 봉투는 도착분이 아니다 — 줄 선 것만 센다."""
    po = PostOffice()
    po.deliver(R(1))
    po.absorb(A)
    assert [arrived(po, A, waking_only=w, words=x) for w, x in CELLS] == [0] * 4


def test_human_words_always_wake():
    """main 의 런 열기는 "사람의 말 또는 깨우는 메일" 이다 — 사람의 말 봉투는 언제나 깨운다(줄 없는 `words`)."""
    assert W(1).wakes is True
