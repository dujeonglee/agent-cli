"""창구 — 메서드마다 무엇을 바꾸고 무엇을 돌려주는가, 런 끝의 규칙 하나, 한 락 구간, 조건변수 공유.

매 테스트 끝에 불변식을 본다(`office` 픽스처의 정리 단계 — 시안 §8 R10).
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.post import envelope as E
from agent_cli.post.office import ConsistencyError, PostOffice, Take
from agent_cli.post.postbox import BoxError
from agent_cli.post.request_book import RowError

A = "agent:a"


@pytest.fixture
def office():
    o = PostOffice()
    yield o
    o.check_consistency()


def _rows(o):
    return [(r.rid, r.state, r.how) for r in o.rows()]


class TestDeliver:
    def test_a_request_opens_a_queued_row(self, office):
        office.deliver(E.request("r-1", "main", A, "do"))
        assert _rows(office) == [("r-1", "queued", None)]

    def test_main_n_items_open_their_row_at_put_too(self, office):
        """수정 2: 닫는 법이 있는 봉투는 모두 put 때 줄이 선다 — main 앞 n- 도. 열린 요청(`open_requests`)은
        picked 만 보이므로 밖에서는 "주입 때 생김" 과 같다."""
        office.deliver(E.system("n-1", "job", "main", "done", opens_row=True))
        assert _rows(office) == [("n-1", "queued", None)]
        assert office.open_requests("main") == ()
        office.absorb("main")
        assert [r.rid for r in office.open_requests("main")] == ["n-1"]

    def test_notices_replies_and_checkins_open_no_row(self, office):
        office.deliver(E.notice("n-1", A, "main", "fyi"))
        office.deliver(E.reply("n-2", A, "main", "ok", re="r-0"))
        office.deliver(E.system("n-3", "harness", A, "⏰", opens_row=False))
        assert office.rows() == ()

    def test_a_duplicate_id_changes_nothing(self, office):
        office.deliver(E.request("r-1", "main", A, "do"))
        with pytest.raises(BoxError):
            office.deliver(E.notice("r-1", "main", "agent:b", "same id"))
        assert office.queued("agent:b") == () and len(office.rows()) == 1


class TestRuns:
    def test_next_run_picks_and_absorb_takes_the_rest(self, office):
        office.deliver(E.request("r-1", "main", A, "one"))
        office.deliver(E.request("r-2", "main", A, "two"))
        envs = office.next_run(A, Take.ARRIVAL, timeout=0)
        assert [e.id for e in envs] == ["r-1"]
        assert _rows(office) == [("r-1", "picked", None), ("r-2", "queued", None)]
        envs = office.absorb(A)
        assert [e.id for e in envs] == ["r-2"]
        assert [e.id for e in office.taken(A)] == ["r-1", "r-2"]

    def test_next_run_returns_a_tuple_of_people(self, office):
        office.deliver(E.human("u-1", "user:dj", A, "a"))
        office.deliver(E.human("u-2", "user:dj", A, "b"))
        envs = office.next_run(A, Take.ARRIVAL, timeout=0)
        assert [e.id for e in envs] == ["u-1", "u-2"]
        assert office.next_run(A, Take.ARRIVAL, timeout=0) is None  # 열 것이 없다

    def test_pay_closes_and_delivers(self, office):
        office.deliver(E.request("r-1", "main", A, "do"))
        office.next_run(A, Take.ARRIVAL, timeout=0)
        office.pay(A, "r-1", E.reply("n-2", A, "main", "done", re="r-1"), run_id="r-1")
        assert [e.id for e in office.queued("main")] == ["n-2"]
        assert _rows(office) == [("r-1", "closed", "reply")]  # 그 런 끝까지 남는다
        assert office.awaited_by("main") == ()

    def test_pay_unsent_closes_and_keeps_the_row_without_delivering(self, office):
        """갚았는데 회신이 가지 못했다(요청자가 사라짐) — 닫힌 채 그 런 끝까지 남는다("이미 회신했다"), 배달 없음."""
        office.deliver(E.request("r-1", "agent:b", A, "do"))
        office.next_run(A, Take.ARRIVAL, timeout=0)
        office.pay_unsent(A, "r-1", run_id="r-1")
        assert _rows(office) == [("r-1", "closed", "unsent")]
        assert office.queued("agent:b") == () and office.owed_by(A) == ()
        office.end_run(A, run_id="r-1", outcome="done")
        assert _rows(office) == []

    def test_pay_refuses_a_reply_that_answers_something_else(self, office):
        office.deliver(E.request("r-1", "main", A, "do"))
        office.next_run(A, Take.ARRIVAL, timeout=0)
        with pytest.raises(RowError):
            office.pay(A, "r-1", E.reply("n-2", A, "main", "x", re="r-9"), run_id="r-1")
        with pytest.raises(RowError):
            office.pay(
                A, "r-1", E.reply("n-2", A, "agent:z", "x", re="r-1"), run_id="r-1"
            )
        assert (
            _rows(office) == [("r-1", "picked", None)] and office.queued("main") == ()
        )

    def test_complete_closes_open_u_and_n(self, office):
        office.deliver(E.human("u-1", "user:dj", A, "hi"))
        office.next_run(A, Take.ARRIVAL, timeout=0)
        assert office.complete(A, ["u-1", "u-9"], run_id="u-1") == ("u-1",)
        assert office.open_requests(A) == ()

    def test_open_human_has_no_envelope(self, office):
        office.open_human("u-1", "user:dj", "main", "hello", answers=["dj"])
        assert [r.rid for r in office.open_requests("main")] == ["u-1"]
        assert office.queued("main") == () == office.taken("main")


class TestEndRun:
    def _two_debts(self, office):
        office.deliver(E.request("r-1", "main", A, "one"))
        office.deliver(E.request("r-2", "agent:b", A, "two"))
        office.deliver(E.human("u-3", "user:dj", A, "hi"))
        office.absorb(A)

    @pytest.mark.parametrize(
        ("outcome", "how"), [("done", "fallback"), ("stopped", "stopped")]
    )
    def test_one_rule_replies_where_given_and_drops_the_rest(
        self, office, outcome, how
    ):
        self._two_debts(office)
        office.end_run(
            A,
            run_id="r-1",
            outcome=outcome,
            replies={"r-1": E.reply("n-4", A, "main", "summary", re="r-1")},
        )
        assert (
            office.rows() == ()
        )  # fallback/stopped·run_ended 는 그 런 끝에 버려지고, r-2 는 dropped
        assert [e.re for e in office.queued("main")] == ["r-1"]
        assert office.queued("agent:b") == () and office.taken(A) == ()

    def test_main_stopping_is_just_empty_replies(self, office):
        """main/에이전트 분기가 없다 — main 정지(회신 없이 버림)는 빈 replies 다."""
        office.deliver(E.request("r-1", A, "main", "q"))
        office.absorb("main")
        office.end_run("main", run_id="u-0", outcome="stopped", replies={})
        assert office.rows() == () and office.queued(A) == ()

    def test_wait_keeps_the_rows_and_empties_the_hands(self, office):
        self._two_debts(office)
        office.end_run(A, run_id="r-1", outcome="wait")
        assert [r.state for r in office.rows()] == ["picked"] * 3
        assert office.taken(A) == ()  # 스스로 잠든 빚 — 들고 있는 봉투는 없다

    def test_wait_drops_a_paid_debt_like_any_run_end(self, office):
        """v10.145.2: 런이 어떻게 끝나든 그 런에서 닫힌 줄은 버린다 — `wait` 로 끝난 뒤 다음 런이 같은 id 에
        "이 런에서 이미 회신했다" 는 거짓 사유를 받지 않게. 열린 빚(잠든 빚)은 남는다."""
        office.deliver(E.request("r-1", "main", A, "do"))
        office.deliver(E.request("r-2", "main", A, "and this"))
        office.next_run(A, Take.ARRIVAL, timeout=0)
        office.absorb(A)
        office.pay(A, "r-1", E.reply("n-3", A, "main", "ok", re="r-1"), run_id="r-1")
        office.end_run(A, run_id="r-1", outcome="wait")
        assert office.row("r-1") is None
        assert office.row("r-2").state == "picked"  # 잠든 빚은 다음 런으로

    def test_session_end_touches_nothing(self, office):
        """버그 3 의 구조 수준 짝 — 세션 끝은 들고 있던 봉투도 줄도 남긴다(재시작이 되살린다)."""
        self._two_debts(office)
        before = (_rows(office), office.taken(A))
        office.end_run(A, run_id="r-1", outcome="session_end", replies={})
        assert (_rows(office), office.taken(A)) == before

    def test_hops_are_carried_by_the_caller(self, office):
        """D3: office 는 hop 을 잇기만 한다 — 줄은 요청의 hop 을, 회신은 만든 쪽이 준 hop 을 든다."""
        office.deliver(E.request("r-1", "agent:b", A, "do", hop=4))
        office.absorb(A)
        (row,) = office.owed_by(A)
        assert row.hop == 4
        fb = E.reply("n-2", A, "agent:b", "fb", re="r-1", hop=row.hop + 1)
        office.end_run(A, run_id="r-1", outcome="done", replies={"r-1": fb})
        assert [e.hop for e in office.queued("agent:b")] == [5]

    def test_a_duplicate_reply_id_changes_nothing(self, office):
        self._two_debts(office)
        office.deliver(E.notice("n-4", A, "main", "taken id"))
        with pytest.raises(BoxError):
            office.end_run(
                A, run_id="r-1", outcome="done",
                replies={"r-1": E.reply("n-4", A, "main", "x", re="r-1")},
            )  # fmt: skip
        assert [r.state for r in office.rows()] == ["picked"] * 3


class TestOweAndTake:
    def test_owe_is_a_picked_debt_the_run_end_falls_back_on(self, office):
        office.owe("r-1", "main", A, "decide")
        office.end_run(
            "main", run_id="u-0", outcome="done",
            replies={"r-1": E.reply("n-2", "main", A, "summary", re="r-1")},
        )  # fmt: skip
        assert office.rows() == () and [e.re for e in office.queued(A)] == ["r-1"]

    def test_take_empties_a_private_box_without_holding(self, office):
        office.deliver(E.system("n-1", "job", "harness:j", "done", opens_row=False))
        envs = office.take("harness:j")
        assert [e.id for e in envs] == ["n-1"]
        assert office.queued("harness:j") == () == office.taken("harness:j")
        assert office.take("harness:j") == ()

    def test_take_refuses_to_orphan_a_row(self, office):
        office.deliver(E.request("r-1", "main", "harness:j", "x"))
        office.deliver(E.notice("n-2", "main", "harness:j", "y"))
        with pytest.raises(RowError):
            office.take("harness:j")
        assert [e.id for e in office.queued("harness:j")] == ["r-1", "n-2"]


class TestUnusedReplies:
    """쓰이지 않을 회신은 말없이 버리지 않는다 — 아무것도 바꾸지 않고 거절한다."""

    def test_end_run_refuses_a_reply_for_a_row_it_does_not_close(self, office):
        office.deliver(E.request("r-1", "main", A, "do"))
        office.absorb(A)
        before = (_rows(office), office.taken(A))
        with pytest.raises(RowError):
            office.end_run(
                A, run_id="r-1", outcome="done",
                replies={
                    "r-1": E.reply("n-2", A, "main", "fb", re="r-1"),
                    "r-9": E.reply("n-3", A, "main", "stray", re="r-9"),
                },
            )  # fmt: skip
        assert (_rows(office), office.taken(A)) == before and office.queued(
            "main"
        ) == ()

    @pytest.mark.parametrize("outcome", ["wait", "session_end"])
    def test_end_run_without_closing_takes_no_replies(self, office, outcome):
        office.deliver(E.request("r-1", "main", A, "do"))
        office.absorb(A)
        with pytest.raises(RowError):
            office.end_run(
                A, run_id="r-1", outcome=outcome,
                replies={"r-1": E.reply("n-2", A, "main", "fb", re="r-1")},
            )  # fmt: skip
        assert [e.id for e in office.taken(A)] == ["r-1"]

    def test_remove_queued_refuses_a_reply_for_a_row_not_queued(self, office):
        office.deliver(E.request("r-1", "main", A, "one"))
        office.deliver(E.request("r-2", "main", A, "two"))
        office.next_run(A, Take.ARRIVAL, timeout=0)  # r-1 은 집혔다 — 줄에 없다
        with pytest.raises(RowError):
            office.remove_queued(A, {"r-1": E.reply("n-3", A, "main", "rm", re="r-1")})
        assert [e.id for e in office.queued(A)] == ["r-2"]


class TestStops:
    def test_remove_queued_answers_r_drops_people_and_forgets_the_rest(self, office):
        office.deliver(E.request("r-1", "main", A, "one"))
        office.deliver(E.human("u-2", "user:dj", A, "hi"))
        office.deliver(E.notice("n-3", "agent:b", A, "fyi"))
        office.remove_queued(
            A,
            {
                "r-1": E.reply(
                    "n-4", A, "main", "removed", re="r-1", mark="removed by user"
                )
            },
        )
        assert office.rows() == () and office.queued(A) == ()
        assert [e.mark for e in office.queued("main")] == ["removed by user"]

    def test_a_gone_requester_s_queued_request_leaves_the_box(self, office):
        """요청한 쪽이 사라졌다 — 아직 아무 런도 집지 않은 요청은 상자에서 빠지고 줄은 dropped(남지 않는다). 사람의
        말·통지·다른 요청자의 요청은 그대로 — 다음 런은 사람의 말이 연다."""
        office.deliver(E.request("r-1", "agent:b", A, "for b"))
        office.deliver(E.human("u-2", "user:dj", A, "hi"))
        office.deliver(E.notice("n-3", "agent:b", A, "fyi"))
        office.deliver(E.request("r-4", "main", "agent:c", "for main"))
        gone = office.forget_requests_of("agent:b", lambda addr: "")
        assert [(r.rid, r.state) for r in gone] == [("r-1", "queued")]
        assert [e.id for e in office.queued(A)] == ["u-2", "n-3"]
        assert _rows(office) == [("u-2", "queued", None), ("r-4", "queued", None)]
        assert office.arrived(A, waking_only=True, words=True) == 2  # 사람의 말·통지
        office.forget_requests_of("agent:b", lambda addr: "")  # 멱등
        assert [e.id for e in office.next_run(A, Take.ARRIVAL, timeout=0)] == ["u-2"]

    def test_picked_or_queued_never_both(self, office):
        """집기와 같은 락 구간 — 집힌 뒤면 빚(unsent, 그 런 끝까지 남는다), 집히기 전이면 상자에서 빠짐(dropped).
        두 순서 모두 한 요청은 한 갈래로만 닫힌다."""
        office.deliver(E.request("r-1", "agent:b", A, "first"))
        office.deliver(E.request("r-2", "agent:b", A, "second"))
        assert [e.id for e in office.next_run(A, Take.ARRIVAL, timeout=0)] == ["r-1"]
        gone = office.forget_requests_of("agent:b", lambda addr: "run-1")
        assert [(r.rid, r.state) for r in gone] == [
            ("r-1", "picked"),
            ("r-2", "queued"),
        ]
        assert _rows(office) == [("r-1", "closed", "unsent")]
        assert office.queued(A) == () and [e.id for e in office.taken(A)] == ["r-1"]
        assert office.next_run(A, Take.ARRIVAL, timeout=0) is None

    def test_main_s_mail_run_can_lose_its_mail(self, office):
        """main 의 메일 런은 메일을 꺼내지 않고 열린다 — 그 사이 요청자가 사라지면 첫 경계에 읽을 것이 없다."""
        office.deliver(E.request("r-1", "agent:b", "main", "for b"))
        assert office.next_run("main", Take.WORDS_FIRST, timeout=0) == ()
        office.forget_requests_of("agent:b", lambda addr: "")
        assert office.absorb("main") == () and office.rows() == ()

    def test_take_reply_leaves_the_box(self, office):
        office.deliver(E.reply("n-1", A, "main", "ans", re="r-7"))
        assert office.take_reply("main", "r-7").id == "n-1"
        assert office.queued("main") == () and office.taken("main") == ()


class TestQuestions:
    def test_ask_answer_forget(self, office):
        row = office.ask("q-1", A, "user:dj", "v1 or v2?")
        assert row.kind == "q"
        office.ask("q-2", A, "user:dj", "another?")
        office.answer("q-1", E.human("u-3", "user:dj", A, "v2"))
        assert [r.rid for r in office.open_questions()] == ["q-2"]
        assert [e.id for e in office.queued(A)] == ["u-3"]
        assert office.forget_questions_of(A) == ("q-2",)
        assert office.open_questions() == ()
        assert office.forget_questions_of(A) == ()

    def test_meta_is_handed_back_untouched(self, office):
        """질문의 meta(물은 때·런 — D3)는 해석하지 않는다: 아무 객체나 그대로 돌아오고 비교에도 들지 않는다."""
        token = object()
        office.ask("q-1", A, "user:dj", "v1 or v2?", meta=token)
        (row,) = office.open_questions()
        assert row.meta is token

    def test_an_answer_must_go_to_the_asker(self, office):
        office.ask("q-1", A, "user:dj", "?")
        with pytest.raises(RowError):
            office.answer("q-1", E.human("u-3", "user:dj", "agent:b", "v2"))


class TestConsistency:
    def test_the_checker_names_what_is_broken(self):
        o = PostOffice()
        o.deliver(E.request("r-1", "main", A, "do"))
        o._box(A).take_away()  # 줄은 queued 인데 봉투가 없다 — 테스트만 일부러 깨뜨린다
        with pytest.raises(ConsistencyError) as err:
            o.check_consistency()
        assert any(p.startswith("I2 r-1") for p in err.value.problems)

    def test_a_row_closed_in_the_run_that_still_holds_its_envelope_is_fine(
        self, office
    ):
        office.deliver(E.human("u-1", "user:dj", A, "hi"))
        office.next_run(A, Take.ARRIVAL, timeout=0)
        office.complete(A, ["u-1"], run_id="u-1")
        office.check_consistency()  # 봉투는 런이 아직 들고 있고, 줄은 그 런 안에서 닫혔다


class TestOneLockSection:
    def test_pay_order_is_not_visible_from_outside(self):
        """버그 5 의 구조 수준 짝 — 닫기와 배달이 한 락 구간이라, 회신이 보이는 첫 순간 빚은 이미 갚혔다."""
        o = PostOffice()
        o.deliver(E.request("r-1", "main", A, "do"))
        o.next_run(A, Take.ARRIVAL, timeout=0)
        seen = []
        ready = threading.Event()

        def watcher():
            with o.cv:
                ready.set()
                o.cv.wait_for(lambda: o.queued("main") != ())
                seen.append([r.rid for r in o.owed_by(A)])

        t = threading.Thread(target=watcher)
        t.start()
        assert ready.wait(5)
        with o.cv:  # 감시자가 wait 에 들어가 락을 놓은 뒤에야 잡힌다
            pass
        o.pay(A, "r-1", E.reply("n-2", A, "main", "ok", re="r-1"), run_id="r-1")
        t.join(5)
        assert seen == [[]]

    def test_reentrant_calls_while_holding_the_condition(self, office):
        """펌프는 cv 를 쥔 채 큐·우편을 부른다 — 기본 조건변수는 재진입 락이다."""
        with office.cv:
            office.deliver(E.notice("n-1", A, "main", "fyi"))
            assert office.arrived("main", waking_only=True, words=True)

    def test_a_shared_condition_wakes_the_other_owner(self):
        """입력 큐와 같은 조건변수를 쓰면, 우편의 배달이 큐 쪽에서 기다리는 이를 깨운다."""
        cv = threading.Condition()
        o = PostOffice(cv)
        woke = []
        ready = threading.Event()

        def queue_owner():
            with cv:
                ready.set()
                cv.wait_for(lambda: o.arrived("main", waking_only=True, words=True))
                woke.append(True)

        t = threading.Thread(target=queue_owner)
        t.start()
        assert ready.wait(5)
        with cv:
            pass
        o.deliver(E.notice("n-1", A, "main", "fyi"))
        t.join(5)
        assert woke == [True]
