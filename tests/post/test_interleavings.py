"""경합 = 메서드 호출 순서 (시안 §8 R4). 창구의 메서드는 락 안에서 원자적이므로, 두 행위자의 연산열을 가능한
모든 순서로 병합해(itertools — 결정적) 돌리고 매 걸음 불변식·참조 모델과 맞춘 뒤 경합마다의 사실을 단언한다.
진짜 스레드 판은 경합마다 하나씩 — Event 로 순서를 강제한다(시간 단언·sleep 없음).

펌프 몫(4b): resume 미배달 + 첫 질의 · 제자리 대기 중 사람 말 · 명령 뒤 회신 — 여기서 다루지 않는다.
"""

from __future__ import annotations

import itertools
import threading

import pytest

from agent_cli.post import envelope as E
from tests.post.twin import Twin

A = "agent:a"
ADDRS = ("main", A, "agent:b")


def merges(a: list, b: list):
    """두 열의 모든 병합 — 각자의 순서는 지킨다. ``(행위자, 연산)`` 목록을 낸다."""
    n = len(a) + len(b)
    for spots in itertools.combinations(range(n), len(a)):
        ia, ib, out = iter(a), iter(b), []
        for i in range(n):
            out.append(("A", next(ia)) if i in spots else ("B", next(ib)))
        yield out


def run(setup, a, b, check):
    """모든 병합 순서마다 새 쌍에서 ``setup`` → 병합열 → ``check(twin, order)``. 돈 순서 수를 돌려준다."""
    count = 0
    for order in merges(a, b):
        twin = Twin(ADDRS)
        for op, args in setup:
            assert twin.step(op, args) == "", (op, args)
        results = {}
        for who, (op, args) in order:
            why = twin.step(op, args)
            assert why == "", (order, why)
            results[(who, op)] = twin.last
        check(twin, [(w, op) for w, (op, _) in order], results)
        count += 1
    return count


REQ = E.request("r-1", "main", A, "do it")
STARTED = [("deliver", {"e": REQ}), ("start_run", {"addr": A})]


class TestOrders:
    def test_arrival_between_the_stop_check_and_entering_the_wait(self):
        """main 이 정지 판정을 하고 대기에 드는 사이에 회신이 와도 — 어느 순서든 회신은 main 앞에 깨우는 채로
        남거나 그 런이 읽었다. 펌프는 같은 조건변수 아래서 `has_waking` 을 보고 잠든다."""
        main = [
            ("absorb", {"addr": "main"}),
            (
                "end_run",
                {"addr": "main", "run_id": "u-0", "outcome": "wait", "replies": {}},
            ),
        ]
        agent = [
            (
                "pay",
                {
                    "debtor": A,
                    "rid": "r-1",
                    "reply": E.reply("n-2", A, "main", "ok", re="r-1"),
                    "run_id": "r-1",
                },
            )
        ]

        def check(twin, order, _):
            absorbed_after = order.index(("A", "absorb")) > order.index(("B", "pay"))
            assert twin.office.has_waking("main") is not absorbed_after
            assert twin.office.awaited_by("main") == ()

        assert run(STARTED, main, agent, check) == 3

    def test_arrival_after_the_last_turn_boundary(self):
        """마지막 경계(흡수) 뒤, 런 끝 전에 온 메일은 줄에 남아 다음 런을 깨운다 — 사라지지 않는다."""
        main = [
            ("absorb", {"addr": "main"}),
            ("complete", {"addr": "main", "ids": [], "run_id": "u-0"}),
            (
                "end_run",
                {"addr": "main", "run_id": "u-0", "outcome": "done", "replies": {}},
            ),
        ]
        agent = [("deliver", {"e": E.notice("n-1", A, "main", "late")})]

        def check(twin, order, _):
            late = order.index(("B", "deliver")) > order.index(("A", "absorb"))
            assert [e.id for e in twin.office.queued("main")] == (
                ["n-1"] if late else []
            )
            assert twin.office.has_waking("main") is late

        assert run([], main, agent, check) == 4

    def test_cancel_against_taking(self):
        """✕ 와 꺼내기가 같은 봉투를 두고 — 둘 중 하나만 이긴다: 집혔으면 빚, 지워졌으면 removed 회신."""
        remover = [
            (
                "remove_queued",
                {
                    "addr": A,
                    "replies": {"r-1": E.reply("n-2", A, "main", "removed", re="r-1")},
                },
            )
        ]
        worker = [("start_run", {"addr": A})]

        def check(twin, order, _):
            took = order.index(("B", "start_run")) < order.index(("A", "remove_queued"))
            rows = [(r.rid, r.state) for r in twin.office.rows()]
            assert rows == ([("r-1", "picked")] if took else [])
            assert [e.id for e in twin.office.queued("main")] == (
                [] if took else ["n-2"]
            )

        assert run([("deliver", {"e": REQ})], remover, worker, check) == 2

    def test_take_reply_while_the_requested_agent_dies(self):
        """`@agent` 명령이 회신을 꺼내려는 동안 받은 쪽이 멈춘다 — 정지 회신은 꺼내지거나 줄에 남는다, 둘 중 하나."""
        taker = [("take_reply", {"addr": "main", "rid": "r-1"})]
        dying = [
            (
                "end_run",
                {
                    "addr": A,
                    "run_id": "r-1",
                    "outcome": "stopped",
                    "replies": {
                        "r-1": E.reply(
                            "n-2",
                            A,
                            "main",
                            "stopped",
                            re="r-1",
                            mark="stopped by user",
                        )
                    },
                },
            )
        ]

        def check(twin, order, results):
            got = results[("A", "take_reply")]
            queued = [e.id for e in twin.office.queued("main")]
            assert (got is not None) != bool(queued)
            assert twin.office.rows() == ()

        assert run(STARTED, taker, dying, check) == 2

    def test_stop_everything_quiet_reply(self):
        """ "Stop everything" 의 회신은 깨우지 않는다 — 어느 순서든 main 을 깨울 것이 남지 않는다."""
        quiet = E.reply(
            "n-2", A, "main", "stopped", re="r-1", mark="stopped by user", wakes=False
        )
        agent = [
            (
                "end_run",
                {
                    "addr": A,
                    "run_id": "r-1",
                    "outcome": "stopped",
                    "replies": {"r-1": quiet},
                },
            )
        ]
        main = [("absorb", {"addr": "main"})]

        def check(twin, order, _):
            assert twin.office.has_waking("main") is False
            read = order.index(("B", "absorb")) > order.index(("A", "end_run"))
            assert [e.id for e in twin.office.taken("main")] == (
                ["n-2"] if read else []
            )

        assert run(STARTED, agent, main, check) == 2

    def test_private_take_against_arrivals(self):
        """private 루프의 꺼내기와 도착 — 어느 순서든 봉투는 꺼내졌거나 줄에 있다, 둘 중 하나만."""
        reader = [("take", {"addr": "harness:j"})]
        sender = [
            (
                "deliver",
                {"e": E.system("n-1", "job", "harness:j", "a", opens_row=False)},
            ),
            (
                "deliver",
                {"e": E.system("n-2", "job", "harness:j", "b", opens_row=False)},
            ),
        ]

        def check(twin, order, results):
            got = [e.id for e in results[("A", "take")]]
            left = [e.id for e in twin.office.queued("harness:j")]
            assert sorted(got + left) == ["n-1", "n-2"] and not set(got) & set(left)

        assert run([], reader, sender, check) == 3


def test_order_counts():
    """시나리오별 순서 수 — 보고용(3·4·2·3·2·2·3)."""
    assert [
        len(list(merges([0] * a, [0] * b)))
        for a, b in ((2, 1), (3, 1), (1, 1), (1, 2), (1, 1), (1, 1), (1, 2))
    ] == [3, 4, 2, 3, 2, 2, 3]


# ── 진짜 스레드 — 경합마다 하나, Event 로 순서를 강제 ───────────────────────


def _in_thread(fn):
    out: dict = {}

    def go():
        out["v"] = fn()

    t = threading.Thread(target=go)
    t.start()
    t.join(5)
    assert not t.is_alive()
    return out.get("v")


class TestThreads:
    def test_a_waiter_on_the_condition_sees_an_arrival_it_would_have_missed(self):
        twin = Twin(ADDRS)
        o = twin.office
        o.deliver(REQ)
        o.start_run(A)
        ready = threading.Event()
        seen = []

        def main_waits():
            with o.cv:
                ready.set()
                o.cv.wait_for(lambda: o.has_waking("main"), timeout=5)
                seen.append(o.has_waking("main"))

        t = threading.Thread(target=main_waits)
        t.start()
        assert ready.wait(5)
        with o.cv:
            pass
        o.pay(A, "r-1", E.reply("n-2", A, "main", "ok", re="r-1"), run_id="r-1")
        t.join(5)
        assert seen == [True]
        o.check_consistency()

    def test_late_mail_from_another_thread_stays_queued(self):
        twin = Twin(ADDRS)
        o = twin.office
        o.absorb("main")
        _in_thread(lambda: o.deliver(E.notice("n-1", A, "main", "late")))
        o.end_run("main", run_id="u-0", outcome="done")
        assert [e.id for e in o.queued("main")] == ["n-1"]
        o.check_consistency()

    @pytest.mark.parametrize("taker_first", [True, False])
    def test_cancel_and_take_from_two_threads(self, taker_first):
        twin = Twin(ADDRS)
        o = twin.office
        o.deliver(REQ)

        def remove():  # 지울 때 줄에 있는 요청에만 회신한다(쓰이지 않을 회신은 거절된다)
            queued = {e.id for e in o.queued(A)}
            reply = E.reply("n-2", A, "main", "rm", re="r-1")
            return o.remove_queued(A, {"r-1": reply} if "r-1" in queued else {})

        def take():
            return o.start_run(A)[0]

        first, second = (take, remove) if taker_first else (remove, take)
        _in_thread(first)
        second()
        assert len(o.rows()) == (1 if taker_first else 0)
        o.check_consistency()

    def test_take_reply_waiter_and_a_dying_agent(self):
        o = Twin(ADDRS).office
        o.deliver(REQ)
        o.start_run(A)
        ready = threading.Event()
        got = []

        def taker():
            with o.cv:
                ready.set()
                o.cv.wait_for(lambda: o.queued("main") != (), timeout=5)
                got.append(o.take_reply("main", "r-1"))

        t = threading.Thread(target=taker)
        t.start()
        assert ready.wait(5)
        with o.cv:
            pass
        o.end_run(
            A,
            run_id="r-1",
            outcome="stopped",
            replies={"r-1": E.reply("n-2", A, "main", "stopped", re="r-1")},
        )
        t.join(5)
        assert [e.id for e in got] == ["n-2"] and o.queued("main") == ()
        o.check_consistency()

    def test_quiet_reply_from_another_thread_does_not_wake(self):
        o = Twin(ADDRS).office
        o.deliver(REQ)
        o.start_run(A)
        quiet = E.reply("n-2", A, "main", "stopped", re="r-1", wakes=False)
        _in_thread(
            lambda: o.end_run(
                A, run_id="r-1", outcome="stopped", replies={"r-1": quiet}
            )
        )
        assert o.has_waking("main") is False and o.arrived("main") == 1
        o.check_consistency()
