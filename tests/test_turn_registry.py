"""TurnRegistry — 동시 병렬 턴 디스패치 (A1 코어, v7.29.0).

계약 근거는 포크(Coagora) ``backend/src/agent/pi.ts``:
  - turnId 디스패치 시점 발급 (240-244)
  - cap + FIFO 펌프, 완료/실패 양쪽에서 슬롯 회수 (226-237, 251-252)
  - ``interrupt(turnId)`` 는 지정 턴만 (624-639)
"""

import threading
import time

import pytest

from agent_cli.loop.turns import DEFAULT_MAX_CONCURRENT_TURNS, Turn, TurnRegistry


class _Recorder:
    """runner 대역 — 관측 + 원하는 지점에서 블록."""

    def __init__(self, block: threading.Event | None = None):
        self.block = block
        self.seen: list[Turn] = []
        self.entered = threading.Event()
        self.concurrent_peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def __call__(self, turn: Turn) -> None:
        with self._lock:
            self.seen.append(turn)
            self._live += 1
            self.concurrent_peak = max(self.concurrent_peak, self._live)
        self.entered.set()
        try:
            if self.block is not None:
                self.block.wait(timeout=5)
        finally:
            with self._lock:
                self._live -= 1


class TestDispatch:
    def test_runs_submitted_turn(self):
        rec = _Recorder()
        reg = TurnRegistry(rec)
        reg.submit("hello", author="a", conn_id="c1")
        assert reg.wait_idle(timeout=5)
        assert [t.text for t in rec.seen] == ["hello"]
        assert rec.seen[0].author == "a"
        assert rec.seen[0].conn_id == "c1"

    def test_turn_ids_are_monotonic_and_issued_at_dispatch(self):
        """대기 중 항목은 id 가 없다 — 발급은 디스패치 시점 (pi.ts:240-244)."""
        block = threading.Event()
        rec = _Recorder(block)
        reg = TurnRegistry(rec, max_concurrent=1)
        reg.submit("first")
        assert rec.entered.wait(timeout=5)
        reg.submit("second")

        # 두 번째는 cap 에 막혀 대기 — 아직 id 없음.
        assert reg.pending_count() == 1
        assert reg.active_ids() == ["t1"]

        block.set()
        assert reg.wait_idle(timeout=5)
        assert [t.id for t in rec.seen] == ["t1", "t2"]

    def test_turns_run_concurrently_up_to_cap(self):
        block = threading.Event()
        rec = _Recorder(block)
        reg = TurnRegistry(rec, max_concurrent=3)
        for i in range(3):
            reg.submit(f"m{i}")
        # 셋 다 동시에 inflight 여야 한다.
        deadline = time.monotonic() + 5
        while reg.active_count() < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert reg.active_count() == 3
        block.set()
        assert reg.wait_idle(timeout=5)
        assert rec.concurrent_peak == 3

    def test_cap_is_enforced(self):
        block = threading.Event()
        rec = _Recorder(block)
        reg = TurnRegistry(rec, max_concurrent=2)
        for i in range(6):
            reg.submit(f"m{i}")
        time.sleep(0.2)
        assert reg.active_count() == 2
        assert reg.pending_count() == 4
        block.set()
        assert reg.wait_idle(timeout=10)
        assert rec.concurrent_peak <= 2
        assert len(rec.seen) == 6

    def test_fifo_order_of_dispatch(self):
        rec = _Recorder()
        reg = TurnRegistry(rec, max_concurrent=1)
        for i in range(5):
            reg.submit(f"m{i}")
        assert reg.wait_idle(timeout=10)
        assert [t.text for t in rec.seen] == [f"m{i}" for i in range(5)]

    def test_default_cap(self):
        assert DEFAULT_MAX_CONCURRENT_TURNS == 4
        assert TurnRegistry(lambda t: None)._max_concurrent == 4

    def test_rejects_bad_cap(self):
        with pytest.raises(ValueError):
            TurnRegistry(lambda t: None, max_concurrent=0)


class TestSlotReclamation:
    def test_failing_turn_releases_its_slot(self):
        """실패한 턴이 슬롯을 영구 점유하면 세션이 굳는다 (pi.ts:251-252)."""
        seen: list[str] = []

        def runner(turn: Turn) -> None:
            seen.append(turn.text)
            raise RuntimeError("boom")

        reg = TurnRegistry(runner, max_concurrent=1)
        for i in range(3):
            reg.submit(f"m{i}")
        assert reg.wait_idle(timeout=5)
        assert seen == ["m0", "m1", "m2"]
        assert reg.active_count() == 0

    def test_failure_does_not_kill_sibling_turns(self):
        done: list[str] = []

        def runner(turn: Turn) -> None:
            if turn.text == "bad":
                raise RuntimeError("boom")
            done.append(turn.text)

        reg = TurnRegistry(runner, max_concurrent=2)
        reg.submit("bad")
        reg.submit("good")
        assert reg.wait_idle(timeout=5)
        assert done == ["good"]


class TestInterrupt:
    def test_interrupts_only_the_named_turn(self):
        """다른 동시 턴은 불간섭 (pi.ts:624-639)."""
        started = threading.Barrier(3)
        observed: dict[str, bool] = {}

        def runner(turn: Turn) -> None:
            started.wait(timeout=5)
            turn.stop_event.wait(timeout=2)
            observed[turn.text] = turn.stop_event.is_set()

        reg = TurnRegistry(runner, max_concurrent=2)
        reg.submit("a")
        reg.submit("b")
        started.wait(timeout=5)

        ids = sorted(reg.active_ids())
        assert reg.interrupt(ids[0]) is True

        assert reg.wait_idle(timeout=10)
        # 하나만 중단 신호를 받았다.
        assert sorted(observed.values()) == [False, True]

    def test_unknown_id_is_idempotent(self):
        reg = TurnRegistry(lambda t: None)
        assert reg.interrupt("t999") is False

    def test_owner_only_cancel(self):
        block = threading.Event()
        rec = _Recorder(block)
        reg = TurnRegistry(rec, max_concurrent=1)
        reg.submit("m", conn_id="owner")
        assert rec.entered.wait(timeout=5)
        tid = reg.active_ids()[0]

        assert reg.interrupt(tid, conn_id="stranger") is False
        assert reg.interrupt(tid, conn_id="owner") is True
        block.set()
        assert reg.wait_idle(timeout=5)

    def test_interrupt_all(self):
        block = threading.Event()
        rec = _Recorder(block)
        reg = TurnRegistry(rec, max_concurrent=3)
        for i in range(3):
            reg.submit(f"m{i}")
        deadline = time.monotonic() + 5
        while reg.active_count() < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert reg.interrupt_all() == 3
        block.set()
        assert reg.wait_idle(timeout=5)


class TestBusyAndIdle:
    def test_is_busy_counts_pending_too(self):
        """대기분을 빼먹으면 마지막 턴이 시작되기 전에 세션이 reap 된다."""
        block = threading.Event()
        rec = _Recorder(block)
        reg = TurnRegistry(rec, max_concurrent=1)
        reg.submit("a")
        reg.submit("b")
        assert rec.entered.wait(timeout=5)
        assert reg.active_count() == 1
        assert reg.pending_count() == 1
        assert reg.is_busy() is True
        block.set()
        assert reg.wait_idle(timeout=5)
        assert reg.is_busy() is False

    def test_idle_before_any_submit(self):
        reg = TurnRegistry(lambda t: None)
        assert reg.wait_idle(timeout=0.1)
        assert reg.is_busy() is False

    def test_on_change_fires(self):
        calls = []
        reg = TurnRegistry(lambda t: None, on_change=lambda: calls.append(1))
        reg.submit("m")
        assert reg.wait_idle(timeout=5)
        assert calls, "on_change 가 한 번도 안 불렸다"

    def test_on_change_failure_does_not_break_dispatch(self):
        def bad():
            raise RuntimeError("nope")

        seen = []
        reg = TurnRegistry(lambda t: seen.append(t.text), on_change=bad)
        reg.submit("m")
        assert reg.wait_idle(timeout=5)
        assert seen == ["m"]


class TestShutdown:
    def test_shutdown_stops_and_drains(self):
        block = threading.Event()

        def runner(turn: Turn) -> None:
            turn.stop_event.wait(timeout=5)

        reg = TurnRegistry(runner, max_concurrent=2)
        reg.submit("a")
        reg.submit("b")
        reg.submit("c")
        time.sleep(0.2)
        reg.shutdown(timeout=5)
        assert reg.active_count() == 0
        assert reg.pending_count() == 0
        block.set()

    def test_submit_after_shutdown_is_noop(self):
        seen = []
        reg = TurnRegistry(lambda t: seen.append(t.text))
        reg.shutdown(timeout=5)
        reg.submit("late")
        assert reg.wait_idle(timeout=1)
        assert seen == []


class TestPerUserFairness:
    """A5: 한 사용자가 cap 을 독점하지 못한다 (포크 pumpConcurrent 규칙).

    ``conn_id`` 가 공정성 단위다 — 닉네임은 중복·사칭이 자유롭지만 연결
    식별자는 서버가 발급하며, 대기 메시지 취소 소유권(``InputQueue.cancel``)이
    이미 같은 단위를 쓴다.
    """

    def test_one_user_gets_only_one_active_turn(self):
        block = threading.Event()
        rec = _Recorder(block)
        reg = TurnRegistry(rec, max_concurrent=4)
        for i in range(4):
            reg.submit(f"m{i}", conn_id="alice")
        time.sleep(0.2)
        # cap 은 4 지만 alice 는 1개만 — 나머지 3개는 대기.
        assert reg.active_count() == 1
        assert reg.pending_count() == 3
        block.set()
        assert reg.wait_idle(timeout=10)
        assert len(rec.seen) == 4  # 결국 전부 처리된다

    def test_other_users_get_slots_while_one_user_waits(self):
        """기아 방지의 본체 — alice 가 큐를 채워도 bob 이 즉시 슬롯을 얻는다."""
        block = threading.Event()
        rec = _Recorder(block)
        reg = TurnRegistry(rec, max_concurrent=3)
        for i in range(5):
            reg.submit(f"alice{i}", conn_id="alice")
        reg.submit("bob0", conn_id="bob")
        reg.submit("carol0", conn_id="carol")

        deadline = time.monotonic() + 5
        while reg.active_count() < 3 and time.monotonic() < deadline:
            time.sleep(0.01)

        running = {t.text for t in rec.seen}
        assert "bob0" in running, "뒤에 온 bob 이 alice 의 큐에 막혔다"
        assert "carol0" in running, "뒤에 온 carol 이 막혔다"
        # alice 는 정확히 1건만 활성.
        assert sum(1 for t in rec.seen if t.text.startswith("alice")) == 1
        block.set()
        assert reg.wait_idle(timeout=15)

    def test_next_turn_of_same_user_starts_after_the_first_finishes(self):
        rec = _Recorder()
        reg = TurnRegistry(rec, max_concurrent=4)
        for i in range(3):
            reg.submit(f"m{i}", conn_id="alice")
        assert reg.wait_idle(timeout=10)
        # 순서는 보존된다 — 스킵은 "건너뛰기"지 "재정렬"이 아니다.
        assert [t.text for t in rec.seen] == ["m0", "m1", "m2"]

    def test_blank_conn_id_is_exempt(self):
        """CLI·MailWaker wake 처럼 사람에게 귀속되지 않는 턴은 서로 안 막는다.

        빈 값끼리 매칭시키면 그것들이 서로를 차단해 병렬이 통째로 무너진다.
        """
        block = threading.Event()
        rec = _Recorder(block)
        reg = TurnRegistry(rec, max_concurrent=3)
        for i in range(3):
            reg.submit(f"m{i}")  # conn_id 없음
        deadline = time.monotonic() + 5
        while reg.active_count() < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert reg.active_count() == 3
        block.set()
        assert reg.wait_idle(timeout=10)

    def test_gate_releases_on_failure_too(self):
        """실패한 턴이 사용자를 영구 차단하면 그 사람은 다시 말을 못 한다."""
        seen = []

        def runner(turn):
            seen.append(turn.text)
            raise RuntimeError("boom")

        reg = TurnRegistry(runner, max_concurrent=4)
        for i in range(3):
            reg.submit(f"m{i}", conn_id="alice")
        assert reg.wait_idle(timeout=10)
        assert seen == ["m0", "m1", "m2"]

    def test_interrupted_turn_releases_the_gate(self):
        """취소된 턴이 사용자를 막아두면 그 사람은 다시 말을 못 한다."""

        def runner(turn):
            turn.stop_event.wait(timeout=3)

        reg = TurnRegistry(runner, max_concurrent=2)
        reg.submit("first", conn_id="alice")
        deadline = time.monotonic() + 5
        while reg.active_count() < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        reg.submit("second", conn_id="alice")
        assert reg.pending_count() == 1  # 게이트에 막힘

        assert reg.interrupt(reg.active_ids()[0]) is True
        assert reg.wait_idle(timeout=10)
        assert reg.pending_count() == 0  # 취소 후 다음 턴이 진행됐다
