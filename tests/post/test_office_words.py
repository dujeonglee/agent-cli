"""창구의 사람의 말 — 줄 없이 서는 봉투(`envelope.words`)를 넣고 빼고, 런을 열 것을 기다린다."""

from __future__ import annotations

import threading

import pytest

from agent_cli.post import envelope as E
from agent_cli.post.office import CLOSED, PostOffice, Take
from agent_cli.post.request_book import RowError


def _words(i: int, text: str = "hi") -> E.Envelope:
    return E.words(f"u-{i}", "user:dj", "main", text)


def _mail(i: int, *, wakes: bool = True) -> E.Envelope:
    env = E.notice(f"n-{i}", "job", "main", "done")
    return (
        env
        if wakes
        else E.reply(f"n-{i}", "agent:a", "main", "x", re="r-1", wakes=False)
    )


class TestShape:
    def test_words_have_no_row(self):
        env = _words(1)
        assert env.closes_by is None and env.wakes is True
        assert E.is_human_words(env)

    def test_delivering_words_opens_no_row(self):
        po = PostOffice()
        po.deliver(_words(1))
        assert po.rows() == () and [e.id for e in po.words("main")] == ["u-1"]
        po.check_consistency()


class TestTakeAndSplit:
    def test_words_and_mail_are_read_apart_in_arrival_order(self):
        po = PostOffice()
        for env in (_mail(1), _words(2), _mail(3), _words(4)):
            po.deliver(env)
        assert [e.id for e in po.words("main")] == ["u-2", "u-4"]
        assert [e.id for e in po.mail("main")] == ["n-1", "n-3"]

    def test_absorb_takes_words_and_mail_in_arrival_order(self):
        po = PostOffice()
        for env in (_words(1), _mail(2), _words(3)):
            po.deliver(env)
        assert [e.id for e in po.absorb("main")] == ["u-1", "n-2", "u-3"]
        assert po.queued("main") == ()
        # 런이 든다(런이 끝나면 비운다) — 줄은 메일의 것만, 사람의 말은 줄이 없다
        assert [e.id for e in po.taken("main")] == ["u-1", "n-2", "u-3"]
        po.check_consistency()

    def test_withdraw(self):
        po = PostOffice()
        po.deliver(_words(1))
        po.deliver(_words(2))
        env = po.withdraw("main", "u-1")
        assert env.id == "u-1" and po.withdraw("main", "u-1") is None
        assert [e.id for e in po.words("main")] == ["u-2"]
        po.check_consistency()

    def test_an_envelope_with_a_row_is_not_withdrawn(self):
        po = PostOffice()
        po.deliver(E.human("u-1", "user:dj", "agent:a", "hi"))
        with pytest.raises(RowError):
            po.withdraw("agent:a", "u-1")
        assert [e.id for e in po.queued("agent:a")] == ["u-1"]


class TestWaitToOpen:
    def test_words_first_then_waking_mail_then_nothing(self):
        po = PostOffice()
        po.deliver(_mail(1))
        po.deliver(_words(2))
        assert po.next_run("main", Take.WORDS_FIRST, timeout=0)[0].id == "u-2"
        assert po.next_run("main", Take.WORDS_FIRST, timeout=0) == ()
        assert [e.id for e in po.queued("main")] == ["n-1"]  # 메일은 꺼내지 않는다

    def test_mail_that_does_not_wake_opens_nothing(self):
        po = PostOffice()
        po.deliver(_mail(1, wakes=False))
        assert po.next_run("main", Take.WORDS_FIRST, timeout=0) is None

    def test_closed_wins_over_queued_words(self):
        """닫히면 줄 선 사람의 말이 있어도 끝 — 그 말로 런을 열지 않는다(끄는 중이다). 줄은 그대로다."""
        po = PostOffice()
        po.deliver(_words(1))
        po.deliver(_mail(2))
        po.close("main")
        assert po.next_run("main", Take.WORDS_FIRST, timeout=0) is CLOSED
        assert [e.id for e in po.queued("main")] == ["u-1", "n-2"]
        assert not po.wait_words("main", 0)

    def test_idle_stays_through_a_timeout_and_goes_off_when_something_opens(self):
        po = PostOffice()
        assert not po.idle("main")
        assert po.next_run("main", Take.WORDS_FIRST, timeout=0) is None
        assert po.idle("main")
        po.deliver(_words(1))
        po.next_run("main", Take.WORDS_FIRST, timeout=0)
        assert not po.idle("main")
        po.next_run("main", Take.WORDS_FIRST, timeout=0)
        po.deliver(_mail(2))
        assert po.next_run("main", Take.WORDS_FIRST, timeout=0) == ()
        assert not po.idle("main")

    def test_a_delivery_wakes_the_waiter(self):
        po = PostOffice()
        got: list = []
        entered = threading.Event()

        def waiter():
            entered.set()
            got.append(po.next_run("main", Take.WORDS_FIRST, timeout=None))

        t = threading.Thread(target=waiter)
        t.start()
        assert entered.wait(5)
        # 기다림에 든 뒤 배달한다 — 판정과 대기가 한 락이라 먼저 와도 놓치지 않는다
        po.deliver(_words(1))
        t.join(5)
        assert not t.is_alive() and got[0][0].id == "u-1"
