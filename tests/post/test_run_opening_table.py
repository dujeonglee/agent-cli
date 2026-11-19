"""런을 열 봉투를 꺼내는 규칙 — main(사람의 말 먼저, 메일 런은 꺼내지 않음)과 에이전트(첫 깨우는 봉투, 사람의 말은
묶음)를 같은 표로 고정한다. 표의 값이 정본이다 — 꺼내는 함수가 바뀌어도 같은 표를 지나야 한다.

결과는 한 모양으로 적는다: ``("took", ids)`` 꺼낸 봉투 · ``("mail_run",)`` 깨우는 메일이 있어 메일이 연 런(꺼내지
않음) · ``("nothing",)`` 열 것이 없다 · ``("closed",)`` 닫혔다. 그리고 꺼낸 뒤의 상자(줄·든 것)와 한가함 표시."""

from __future__ import annotations

import pytest

from agent_cli.post import envelope as E
from agent_cli.post.office import CLOSED, PostOffice, Take

A = "agent:a"


def _open(po: PostOffice, addr: str) -> tuple:
    """그 주소의 다음 런을 열 것 — 기다리지 않는다."""
    take = Take.WORDS_FIRST if addr == "main" else Take.ARRIVAL
    got = po.next_run(addr, take, timeout=0)
    if got is None:
        return ("nothing",)
    if got is CLOSED:
        return ("closed",)
    if got == ():
        return ("mail_run",)
    return ("took", [e.id for e in got])


def _state(po: PostOffice, addr: str) -> tuple:
    return (
        [e.id for e in po.queued(addr)],
        [e.id for e in po.taken(addr)],
    )


# main 의 봉투
W = lambda i: E.words(f"u-{i}", "user:dj", "main", f"w{i}")
N = lambda i: E.notice(f"n-{i}", "job", "main", "done")  # 깨운다
Q = lambda i: E.reply(f"n-{i}", "agent:a", "main", "x", re="r-0", wakes=False)
# 에이전트 앞 봉투
H = lambda i: E.human(f"u-{i}", "user:dj", A, f"h{i}")  # 사람의 말(줄 있음)
R = lambda i: E.request(f"r-{i}", "main", A, f"do {i}")
S = lambda i: E.reply(
    f"n-{i}", "agent:b", A, "x", re="r-0", wakes=False
)  # 깨우지 않는다

#: (이름, 넣는 봉투, 닫기, 기대 결과, 기대 (줄, 든 것), 기대 한가함)
MAIN_TABLE = [
    ("empty", [], False, ("nothing",), ([], []), True),
    ("words_first_one", [W(1), W(2)], False, ("took", ["u-1"]), (["u-2"], []), False),
    ("words_before_mail", [N(1), W(2)], False, ("took", ["u-2"]), (["n-1"], []), False),
    (
        "waking_mail_opens_without_taking",
        [N(1)],
        False,
        ("mail_run",),
        (["n-1"], []),
        False,
    ),
    ("quiet_mail_opens_nothing", [Q(1)], False, ("nothing",), (["n-1"], []), True),
    (
        "quiet_then_waking",
        [Q(1), N(2)],
        False,
        ("mail_run",),
        (["n-1", "n-2"], []),
        False,
    ),
    (
        "closed_words_still_first",
        [W(1), N(2)],
        True,
        ("took", ["u-1"]),
        (["n-2"], []),
        False,
    ),
    ("closed_empty", [], True, ("closed",), ([], []), True),
    ("closed_with_mail", [N(1)], True, ("closed",), (["n-1"], []), True),
]

AGENT_TABLE = [
    ("empty", [], ("nothing",), ([], [])),
    ("one_request", [R(1)], ("took", ["r-1"]), ([], ["r-1"])),
    (
        "human_words_batch",
        [H(1), H(2), R(3)],
        ("took", ["u-1", "u-2"]),
        (["r-3"], ["u-1", "u-2"]),
    ),
    ("first_waking_after_quiet", [S(1), R(2)], ("took", ["r-2"]), (["n-1"], ["r-2"])),
    ("quiet_only", [S(1)], ("nothing",), (["n-1"], [])),
    ("request_then_words", [R(1), H(2)], ("took", ["r-1"]), (["u-2"], ["r-1"])),
]


@pytest.mark.parametrize(
    ("name", "envs", "closed", "want", "after", "idle"),
    MAIN_TABLE,
    ids=[t[0] for t in MAIN_TABLE],
)
def test_main_opens_as_the_table(name, envs, closed, want, after, idle):
    po = PostOffice()
    for env in envs:
        po.deliver(env)
    if closed:
        po.close("main")
    assert _open(po, "main") == want
    assert _state(po, "main") == after
    assert po.idle("main") is idle
    po.check_consistency()


@pytest.mark.parametrize(
    ("name", "envs", "want", "after"), AGENT_TABLE, ids=[t[0] for t in AGENT_TABLE]
)
def test_agent_opens_as_the_table(name, envs, want, after):
    po = PostOffice()
    for env in envs:
        po.deliver(env)
    assert _open(po, A) == want
    assert _state(po, A) == after
    po.check_consistency()
