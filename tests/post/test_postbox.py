"""상자 — 봉투 위치 전이표의 모든 칸, 꺼내는 순서, 이름 있는 수, close 는 버리지 않는다."""

from __future__ import annotations

import pytest

from agent_cli.post import envelope as E
from agent_cli.post.postbox import BoxError, Postbox

X = E.reply("x-1", "agent:a", "main", "ans", re="r-9")
OPS = (
    "put", "take_next", "take_all", "take_all_human_first", "take_reply",
    "drop_queued", "finish_run", "discard", "take_away",
)  # fmt: skip

#: (위치, 연산) → 새 위치. "error" 는 BoxError. 시안 §3 에서 따로 적은 표.
EXPECT = {
    ("none", "put"): "queued",
    **{("none", op): "none" for op in OPS if op != "put"},
    ("queued", "put"): "error",
    ("queued", "take_next"): "taken",
    ("queued", "take_all"): "taken",
    ("queued", "take_all_human_first"): "taken",
    ("queued", "take_reply"): "none",
    ("queued", "drop_queued"): "none",
    ("queued", "finish_run"): "queued",
    ("queued", "discard"): "none",
    ("queued", "take_away"): "none",
    ("taken", "put"): "error",
    **{
        ("taken", op): "taken"
        for op in (
            "take_next",
            "take_all",
            "take_all_human_first",
            "take_reply",
            "drop_queued",
            "take_away",
        )
    },
    ("taken", "finish_run"): "none",
    ("taken", "discard"): "none",
}


def _where(box) -> str:
    if any(e.id == X.id for e in box.queued()):
        return "queued"
    if any(e.id == X.id for e in box.taken()):
        return "taken"
    return "none"


def _box_at(place) -> Postbox:
    box = Postbox()
    if place != "none":
        box.put(X)
    if place == "taken":
        box.take_all()
    return box


@pytest.mark.parametrize(("place", "op"), sorted(EXPECT))
def test_envelope_location_table(place, op):
    box = _box_at(place)
    want = EXPECT[(place, op)]
    call = {
        "put": lambda: box.put(X),
        "take_reply": lambda: box.take_reply("r-9"),
        "discard": lambda: box.discard(X.id),
    }.get(op, getattr(box, op))
    if want == "error":
        with pytest.raises(BoxError):
            call()
        assert _where(box) == place
        return
    call()
    assert _where(box) == want


def test_table_size():
    assert len(EXPECT) == 3 * len(OPS) == 27


def _h(i):
    return E.human(f"u-{i}", "user:dj", "agent:a", f"h{i}")


def _r(i, wakes=True):
    return E.reply(f"n-{i}", "agent:b", "agent:a", f"r{i}", re="r-0", wakes=wakes)


class TestTaking:
    def test_take_next_takes_the_first_waking_one(self):
        box = Postbox()
        box.put(_r(1, wakes=False))
        box.put(_r(2))
        box.put(_r(3))
        assert [e.id for e in box.take_next()] == ["n-2"]
        assert [e.id for e in box.queued()] == [
            "n-1",
            "n-3",
        ]  # 깨우지 않는 것은 줄에 남는다

    def test_take_next_batches_the_humans_that_follow(self):
        box = Postbox()
        for e in (_h(1), _h(2), _r(3), _h(4)):
            box.put(e)
        assert [e.id for e in box.take_next()] == ["u-1", "u-2"]
        assert [e.id for e in box.take_next()] == ["n-3"]
        assert [e.id for e in box.take_next()] == ["u-4"]
        assert box.take_next() == ()

    def test_only_quiet_envelopes_open_nothing(self):
        box = Postbox()
        box.put(_r(1, wakes=False))
        assert box.take_next() == () and box.has_waking() is False

    def test_take_all_keeps_arrival_order_and_human_first_moves_people_ahead(self):
        a, b = Postbox(), Postbox()
        for e in (_r(1), _h(2), _r(3), _h(4)):
            a.put(e)
            b.put(e)
        assert [e.id for e in a.take_all()] == ["n-1", "u-2", "n-3", "u-4"]
        assert [e.id for e in b.take_all_human_first()] == ["u-2", "u-4", "n-1", "n-3"]
        assert [e.id for e in b.taken()] == ["u-2", "u-4", "n-1", "n-3"]


class TestCounts:
    def test_arrived_counts_quiet_mail_too(self):
        box = Postbox()
        box.put(_r(1, wakes=False))
        box.put(_r(2))
        assert box.arrived() == 2

    def test_arrived_counts_a_queued_human_envelope_like_now(self):
        """에이전트 앞에 줄 선 사람의 말도 도착분이다 — 지금 `arrived_for` 가 받은편지함 미러 전부를 세는 것과
        같다(main 의 사람 입력은 상자에 들지 않으니 세지 않는다)."""
        box = Postbox()
        box.put(_h(1))
        assert box.arrived() == 1

    def test_backlog_skips_a_quiet_reply_queued_before_a_busy_resident(self):
        """의도된 차이 후보 (D2): 바쁜 상주 앞에 깨우지 않는 회신이 줄 서 있으면 지금(`inbox.qsize()`)은 세고
        새 구조는 세지 않는다. 종료 표식을 세던 것은 사라진다."""
        box = Postbox()
        box.put(_r(1, wakes=False))
        box.put(_r(2))
        box.put(_r(3))
        assert box.backlog() == 2 and box.arrived() == 3

    def test_has_waking(self):
        box = Postbox()
        assert not box.has_waking()
        box.put(_r(1, wakes=False))
        assert not box.has_waking()
        box.put(_r(2))
        assert box.has_waking()


def test_close_only_marks_and_keeps_the_envelopes():
    box = Postbox()
    box.put(_r(1))
    box.close()
    assert box.closed and [e.id for e in box.queued()] == ["n-1"]
    box.put(_r(2))  # 닫힌 상자도 받는다 — 남은 메일은 저장돼 다음에 읽힌다
    assert box.arrived() == 2


def test_views_are_copies():
    box = Postbox()
    box.put(_r(1))
    q = box.queued()
    box.take_all()
    assert [e.id for e in q] == ["n-1"] and box.queued() == ()
