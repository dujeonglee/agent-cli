"""요청 장부 — 줄 전이표의 모든 칸, 줄 종류는 봉투 머리로만, 닫힌 줄은 그 런 끝까지만, 이름 있는 질문.

기대 표(:data:`EXPECT`)는 시안 §3·§4 와 승인된 결정에서 따로 적은 것이다 — 구현의 표를 읽지 않는다.
"""

from __future__ import annotations

import itertools

import pytest

from agent_cli.post import envelope as E
from agent_cli.post.request_book import RequestBook, RowError

HOWS = (
    "reply", "fallback", "by_message", "stopped", "removed", "dropped",
    "completed", "run_ended", "user_answered", "undeliverable", "died",
)  # fmt: skip
OPS = ("pick",) + tuple(f"close:{h}" for h in HOWS)
KINDS = ("r", "u", "n", "q")
STATES = ("none", "queued", "picked", "closed")

#: 허용 칸 — (종류, 상태, 연산) → 결과("picked" | "closed" 남음 | "gone" 지워짐). 나머지 칸은 RowError.
EXPECT = {
    **{(k, "queued", "pick"): "picked" for k in ("r", "u", "n")},
    ("r", "queued", "close:removed"): "gone",
    **{(k, "queued", "close:dropped"): "gone" for k in ("r", "u", "n")},
    **{(k, "queued", "close:died"): "gone" for k in ("r", "u", "n")},
    ("r", "picked", "close:reply"): "closed",
    ("r", "picked", "close:by_message"): "closed",
    ("r", "picked", "close:fallback"): "closed",
    ("r", "picked", "close:stopped"): "closed",
    ("r", "picked", "close:dropped"): "gone",
    ("r", "picked", "close:died"): "gone",
    **{(k, "picked", "close:completed"): "closed" for k in ("u", "n")},
    **{(k, "picked", "close:run_ended"): "closed" for k in ("u", "n")},
    **{(k, "picked", "close:dropped"): "gone" for k in ("u", "n")},
    ("q", "picked", "close:user_answered"): "gone",
    ("q", "picked", "close:undeliverable"): "gone",
}

#: 세울 수 없는 칸 — q 는 줄 서지 않고(봉투 없음), 닫히면 바로 지워진다
UNREACHABLE = {("q", "queued"), ("q", "closed")}


def _book_with(kind: str, state: str) -> RequestBook:
    book = RequestBook()
    if state == "none":
        return book
    if kind == "q":
        book.open_question("x-1", "agent:a", "user:dj", "?")
        return book
    env = {
        "r": E.request("x-1", "main", "agent:a", "do"),
        "u": E.human("x-1", "user:dj", "agent:a", "hi"),
        "n": E.system("x-1", "job", "main", "done", opens_row=True),
    }[kind]
    book.open(env)
    if state in ("picked", "closed"):
        book.pick("x-1")
    if state == "closed":
        book.close("x-1", "reply" if kind == "r" else "completed", in_run="run-0")
    return book


CELLS = [
    c for c in itertools.product(KINDS, STATES, OPS) if (c[0], c[1]) not in UNREACHABLE
]


@pytest.mark.parametrize(("kind", "state", "op"), CELLS)
def test_row_transition_table(kind, state, op):
    book = _book_with(kind, state)
    want = EXPECT.get((kind, state, op))

    def act():
        if op == "pick":
            return book.pick("x-1")
        return book.close("x-1", op.split(":", 1)[1], in_run="run-1")

    if want is None:
        with pytest.raises(RowError):
            act()
        return
    act()
    row = book.get("x-1")
    if want == "gone":
        assert row is None
    else:
        assert row is not None and row.state == want
        if want == "closed":
            assert row.how == op.split(":", 1)[1] and row.closed_in_run == "run-1"


def test_table_size():
    """칸 수 — 보고용 숫자를 고정한다(허용 칸 + 금지 칸 = 전체)."""
    assert len(CELLS) == 4 * 4 * 12 - 2 * 12
    assert len([c for c in CELLS if c in EXPECT]) == len(EXPECT) == 24


@pytest.mark.parametrize(
    "how", ["reply", "by_message", "completed", "fallback", "stopped", "run_ended"]
)
def test_closing_inside_a_run_needs_the_run(how):
    kind = "r" if how in ("reply", "by_message", "fallback", "stopped") else "u"
    book = _book_with(kind, "picked")
    with pytest.raises(RowError):
        book.close("x-1", how)


@pytest.mark.parametrize(
    "env",
    [
        E.notice("x-1", "agent:a", "main", "fyi"),
        E.reply("x-1", "agent:a", "main", "ok", re="r-1"),
        E.system("x-1", "job", "agent:a", "done", opens_row=False),
        E.Envelope("x-1", "notice", "a", "b", "t", closes_by="complete"),
        E.Envelope("x-1", "system", "a", "b", "t", closes_by="reply"),
    ],
)
def test_no_row_for_a_header_outside_the_table(env):
    with pytest.raises(RowError):
        RequestBook().open(env)


def test_row_kind_comes_from_the_header_not_the_address():
    """주소가 무엇처럼 보이든 머리가 정한다 — 사람처럼 보이는 src 의 요청은 r, 에이전트 src 의 사람 말 머리는 u."""
    book = RequestBook()
    assert book.open(E.request("r-1", "user:dj", "agent:a", "x")).kind == "r"
    assert book.open(E.human("u-2", "agent:z", "agent:a", "x")).kind == "u"
    assert (
        book.open(E.system("n-3", "user:dj", "main", "x", opens_row=True)).kind == "n"
    )


def test_duplicate_ids_are_refused():
    book = RequestBook()
    book.open(E.request("r-1", "main", "agent:a", "x"))
    with pytest.raises(RowError):
        book.open(E.request("r-1", "main", "agent:b", "y"))
    with pytest.raises(RowError):
        book.open_question("r-1", "agent:a", "user:dj", "?")


def test_closed_rows_last_until_the_debtors_run_ends():
    """런 안에서 닫힌 줄만 남는다 — 그 빚진 쪽의 런이 끝나면 버린다. 런 밖에서 닫힌 줄은 바로 지운다."""
    book = RequestBook()
    for rid, dst in (("r-1", "agent:a"), ("r-2", "agent:a"), ("r-3", "agent:b")):
        book.open(E.request(rid, "main", dst, "x"))
        book.pick(rid)
    book.close("r-1", "reply", in_run="run-a")
    book.close("r-2", "dropped")
    book.close("r-3", "reply", in_run="run-b")
    assert [(r.rid, r.state) for r in book.rows()] == [
        ("r-1", "closed"),
        ("r-3", "closed"),
    ]
    assert book.purge_closed("agent:a") == ("r-1",)
    assert [r.rid for r in book.rows()] == ["r-3"]


class TestQuestions:
    def _book(self):
        book = RequestBook()
        book.open(E.request("r-1", "main", "agent:a", "first"))
        book.open(E.request("r-2", "agent:b", "agent:a", "second"))
        book.open(E.request("r-3", "main", "agent:a", "third"))
        book.open(E.human("u-4", "user:dj", "agent:a", "hi"))
        book.open(E.system("n-5", "job", "main", "done", opens_row=True))
        book.open_question("q-6", "agent:a", "user:dj", "v1 or v2?")
        for rid in ("r-1", "r-2", "u-4", "n-5"):
            book.pick(rid)
        return book

    def test_owed_by_is_the_picked_reply_debts(self):
        assert [r.rid for r in self._book().owed_by("agent:a")] == ["r-1", "r-2"]

    def test_awaited_by_includes_the_not_yet_picked(self):
        """빚으로 적히기 전(queued)에도 기다림은 있다 — `wait` 의 근거."""
        assert [r.rid for r in self._book().awaited_by("main")] == ["r-1", "r-3"]

    def test_owes_oldest_picks_the_oldest_to_that_requester(self):
        book = self._book()
        assert book.owes_oldest("agent:a", "main").rid == "r-1"
        assert book.owes_oldest("agent:a", "agent:b").rid == "r-2"
        assert book.owes_oldest("agent:a", "agent:c") is None

    def test_last_to_still_sees_a_paid_debt(self):
        book = self._book()
        book.close("r-1", "reply", in_run="run-1")
        last = book.last_to("agent:a", "main")
        assert last.rid == "r-1" and last.how == "reply"

    def test_open_requests_are_the_picked_u_and_n(self):
        book = self._book()
        assert [r.rid for r in book.open_requests("agent:a")] == ["u-4"]
        assert [r.rid for r in book.open_requests("main")] == ["n-5"]

    def test_open_questions(self):
        assert [r.rid for r in self._book().open_questions()] == ["q-6"]

    def test_open_human_is_picked_at_once_without_an_envelope(self):
        book = RequestBook()
        row = book.open_human("u-1", "user:dj", "main", "hello", answers=("dj",))
        assert (row.kind, row.state, row.answers) == ("u", "picked", ("dj",))
        assert [r.rid for r in book.open_requests("main")] == ["u-1"]


def test_owe_opens_a_picked_reply_debt_without_an_envelope():
    book = RequestBook()
    row = book.owe("r-1", "main", "agent:a", "decide", hop=2, answers=("dj",))
    assert (row.kind, row.state, row.src, row.dst, row.hop) == (
        "r",
        "picked",
        "agent:a",
        "main",
        2,
    )
    assert [r.rid for r in book.owed_by("main")] == ["r-1"]
    with pytest.raises(RowError):
        book.owe(
            "r-1", "main", "agent:a", "again"
        )  # 같은 id — 두 뜻을 한 이름에 담지 않는다
    book.open(E.request("r-2", "main", "agent:b", "x"))
    with pytest.raises(RowError):
        book.owe("r-2", "agent:b", "main", "x")  # 봉투로 열린 줄도 같다
