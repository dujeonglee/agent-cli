"""`PostOffice.next_run` 이 지금의 두 함수(main 의 `wait_to_open`, 에이전트의 `start_run`)와 같은 것을 꺼내는가 —
넣기·닫기·꺼내기의 연산 열을 길이 5까지 전부 돌려, 매 꺼내기의 결과·상자(줄·든 것)·한가함 표시를 맞댄다."""

from __future__ import annotations

import itertools

import pytest

from agent_cli.post import envelope as E
from agent_cli.post.office import CLOSED, OPEN_MAIL, PostOffice, Take

A = "agent:a"


def _main_env(op: str, i: int):
    return {
        "W": lambda: E.words(f"u-{i}", "user:dj", "main", "w"),
        "N": lambda: E.notice(f"n-{i}", "job", "main", "done"),
        "Q": lambda: E.reply(f"n-{i}", "agent:a", "main", "x", re="r-0", wakes=False),
    }[op]()


def _agent_env(op: str, i: int):
    return {
        "H": lambda: E.human(f"u-{i}", "user:dj", A, "h"),
        "R": lambda: E.request(f"r-{i}", "main", A, "do"),
        "S": lambda: E.reply(f"n-{i}", "agent:b", A, "x", re="r-0", wakes=False),
    }[op]()


def _old_main(po):
    got = po.wait_to_open("main", timeout=0)
    if got is None or got is CLOSED:
        return got
    if got is OPEN_MAIL:
        return ()
    return (got.id,)


def _new_main(po):
    got = po.next_run("main", Take.WORDS_FIRST, timeout=0)
    return got if got is None or got is CLOSED else tuple(e.id for e in got)


def _old_agent(po):
    envs = po.start_run(A)
    return tuple(e.id for e in envs) if envs else None


def _new_agent(po):
    got = po.next_run(A, Take.ARRIVAL, timeout=0)
    return got if got is None or got is CLOSED else tuple(e.id for e in got)


def _view(po, addr):
    return (
        [e.id for e in po.queued(addr)],
        [e.id for e in po.taken(addr)],
        [(r.rid, r.state) for r in po.rows()],
    )


def _replay(seq, addr, make, open_):
    po = PostOffice()
    trace = []
    for i, op in enumerate(seq):
        if op == "O":
            trace.append((open_(po), _view(po, addr)))
        elif op == "C":
            po.close(addr)
        else:
            po.deliver(make(op, i))
    po.check_consistency()
    return trace


def _seqs(alphabet, n):
    for k in range(1, n + 1):
        yield from itertools.product(alphabet, repeat=k)


MAIN_SEQS = [s for s in _seqs("WNQCO", 5) if "O" in s]
AGENT_SEQS = [s for s in _seqs("HRSO", 5) if "O" in s]


@pytest.mark.parametrize("chunk", range(8))
def test_main_words_first_takes_what_wait_to_open_takes(chunk):
    for seq in MAIN_SEQS[chunk::8]:
        old = _replay(seq, "main", _main_env, _old_main)
        new = _replay(seq, "main", _main_env, _new_main)
        assert old == new, "".join(seq)


def test_main_idle_mark_is_the_same():
    for seq in MAIN_SEQS:
        marks = []
        for open_ in (_old_main, _new_main):
            po = PostOffice()
            got = []
            for i, op in enumerate(seq):
                if op == "O":
                    open_(po)
                    got.append(po.idle("main"))
                elif op == "C":
                    po.close("main")
                else:
                    po.deliver(_main_env(op, i))
            marks.append(got)
        assert marks[0] == marks[1], "".join(seq)


@pytest.mark.parametrize("chunk", range(4))
def test_agent_arrival_takes_what_start_run_takes(chunk):
    """닫기는 넣지 않는다 — 지금의 `start_run` 은 닫힘을 보지 않는다(워커의 정지 판정이 본다)."""
    for seq in AGENT_SEQS[chunk::4]:
        old = _replay(seq, A, _agent_env, _old_agent)
        new = _replay(seq, A, _agent_env, _new_agent)
        assert old == new, "".join(seq)


def test_arrival_on_a_closed_box_takes_nothing():
    po = PostOffice()
    po.deliver(E.request("r-1", "main", A, "do"))
    po.close(A)
    assert po.next_run(A, Take.ARRIVAL, timeout=0) is CLOSED
    assert [e.id for e in po.queued(A)] == ["r-1"] and po.taken(A) == ()
