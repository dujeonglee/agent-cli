"""ScheduleRegistry — 세션 소유 예약의 정산 (docs/schedule/DESIGN.md §4–§5).

가짜 시계로 돈다: 스레드는 띄우지 않고 ``settle()`` 을 직접 부른다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from agent_cli.schedule.registry import (
    AGENT_CAP,
    DEFAULT_NICKNAME,
    ScheduleError,
    ScheduleRegistry,
)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    # 2026-10-05 는 월요일.
    return Clock(datetime(2026, 10, 5, 8, 0, 0))


@pytest.fixture
def sent():
    return []


@pytest.fixture
def reg(tmp_path, clock, sent):
    r = ScheduleRegistry(tmp_path, clock=clock)
    r.start = lambda: None  # 스레드 없이 — settle() 을 직접 부른다
    r.enqueue = lambda prompt, nickname: sent.append((prompt, nickname))
    return r


def _log(tmp_path) -> list[dict]:
    p = tmp_path / "schedule-log.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()]


class TestAdd:
    def test_rejects_bad_cron_and_empty_prompt(self, reg):
        with pytest.raises(ScheduleError, match="invalid cron"):
            reg.add("not a cron", "do it")
        with pytest.raises(ScheduleError, match="prompt is empty"):
            reg.add("0 9 * * *", "   ")

    def test_agent_cap_does_not_count_user_schedules(self, reg):
        for _ in range(AGENT_CAP):
            reg.add("0 9 * * *", "x")
        with pytest.raises(ScheduleError, match="delete one first"):
            reg.add("0 9 * * *", "x")
        reg.add("0 9 * * *", "x", source="user")  # 사용자는 캡 밖

    def test_state_survives_a_restart(self, reg, tmp_path, clock):
        s = reg.add("0 9 * * 1", "weekly report", label="주간 보고")
        again = ScheduleRegistry(tmp_path, clock=clock)
        (loaded,) = again.list_all()
        assert loaded == s

    def test_corrupt_row_does_not_block_the_rest(self, reg, tmp_path, clock):
        reg.add("0 9 * * *", "x")
        state = json.loads((tmp_path / "schedules.json").read_text())
        state["schedules"].append({"id": "broken"})
        (tmp_path / "schedules.json").write_text(json.dumps(state))
        assert len(ScheduleRegistry(tmp_path, clock=clock).list_all()) == 1


class TestSettle:
    def test_fires_on_time_exactly_once(self, reg, clock, sent, tmp_path):
        reg.add("0 9 * * *", "morning check", nickname="아침봇")
        reg.settle()
        assert sent == []  # 08:00 — 아직

        clock.advance(hours=1, seconds=2)  # 09:00:02
        reg.settle()
        reg.settle()  # 같은 발화를 다시 봐도 한 번만
        assert sent == [("morning check", "아침봇")]
        (s,) = reg.list_all()
        assert s.last_fired_at == "2026-10-05T09:00:02"
        assert [r["event"] for r in _log(tmp_path)] == ["fired"]

    def test_default_nickname(self, reg, clock, sent):
        reg.add("0 9 * * *", "x")
        clock.advance(hours=1)
        reg.settle()
        assert sent == [("x", DEFAULT_NICKNAME)]

    def test_nothing_owed_from_before_creation(self, reg, clock, sent):
        """09:00 발화 예약을 09:30 에 만들면 오늘 09:00 은 놓친 게 아니다."""
        clock.advance(hours=1, minutes=30)
        reg.add("0 9 * * *", "x")
        reg.settle()
        (s,) = reg.list_all()
        assert sent == [] and s.missed_at is None

    def test_late_wake_becomes_a_question_not_a_run(self, reg, clock, sent, tmp_path):
        """120초를 넘겨 깬 발화는 자동 실행하지 않는다."""
        reg.add("0 9 * * *", "x")
        clock.advance(hours=1, seconds=121)
        reg.settle()
        (s,) = reg.list_all()
        assert sent == []
        assert s.missed_at == "2026-10-05T09:00:00"
        assert [r["event"] for r in _log(tmp_path)] == ["missed"]

    def test_boundary_within_threshold_still_fires(self, reg, clock, sent):
        reg.add("0 9 * * *", "x")
        clock.advance(hours=1, seconds=120)
        reg.settle()
        assert len(sent) == 1

    def test_several_missed_periods_fold_into_one_question(
        self, reg, clock, sent, tmp_path
    ):
        """일주일 꺼져 있어 매일 예약을 여러 번 놓쳐도 질문은 가장 최근 것 하나."""
        reg.add("0 9 * * *", "x")
        clock.advance(days=3, hours=5)  # 10-08 13:00
        reg.settle()
        reg.settle()
        (s,) = reg.list_all()
        assert s.missed_at == "2026-10-08T09:00:00"
        assert len(_log(tmp_path)) == 1  # 같은 놓침을 다시 봐도 이력 한 줄

        clock.advance(days=1)  # 또 하루 놓침 — 질문은 덮어써 여전히 1건
        reg.settle()
        (s,) = reg.list_all()
        assert s.missed_at == "2026-10-09T09:00:00"
        assert sent == []

    def test_restart_after_downtime_asks(self, reg, tmp_path, clock, sent):
        """프로세스가 꺼져 있던 동안 지난 발화 — 새 레지스트리의 첫 정산이
        질문으로 남긴다."""
        reg.add("0 9 * * *", "x")
        clock.advance(hours=6)
        fresh = ScheduleRegistry(tmp_path, clock=clock)
        fresh.enqueue = lambda p, n: sent.append((p, n))
        fresh.settle()
        assert sent == []
        assert fresh.list_all()[0].missed_at == "2026-10-05T09:00:00"

    def test_disabled_time_is_not_owed(self, reg, clock, sent):
        s = reg.add("0 9 * * *", "x")
        reg.set_enabled(s.id, False)
        clock.advance(hours=6)
        reg.settle()
        reg.set_enabled(s.id, True)
        reg.settle()
        assert sent == [] and reg.get(s.id).missed_at is None

    def test_failed_fire_becomes_a_question_and_is_not_retried(
        self, reg, clock, tmp_path
    ):
        calls = []

        def boom(prompt, nickname):
            calls.append(prompt)
            raise RuntimeError("queue closed")

        reg.enqueue = boom
        reg.add("0 9 * * *", "x")
        clock.advance(hours=1)
        reg.settle()
        reg.settle()
        (s,) = reg.list_all()
        assert calls == ["x"]
        assert s.missed_at == "2026-10-05T09:00:00" and s.last_fired_at is None
        (row,) = _log(tmp_path)
        assert row["event"] == "failed" and "queue closed" in row["error"]

    def test_no_queue_attached_is_a_failed_fire(self, reg, clock):
        reg.enqueue = None
        reg.add("0 9 * * *", "x")
        clock.advance(hours=1)
        reg.settle()
        assert reg.list_all()[0].missed_at is not None


class TestMissedAnswer:
    @pytest.fixture
    def missed(self, reg, clock):
        s = reg.add("0 9 * * *", "x", label="아침")
        clock.advance(hours=3)
        reg.settle()
        assert reg.get(s.id).missed_at
        return s

    def test_run_now_fires_and_clears(self, reg, missed, sent, tmp_path):
        assert reg.run_now(missed.id) is True
        s = reg.get(missed.id)
        assert sent == [("x", DEFAULT_NICKNAME)] and s.missed_at is None
        assert [r["event"] for r in _log(tmp_path)] == ["missed", "fired"]
        reg.settle()
        assert len(sent) == 1  # 답한 뒤 다시 묻지도 다시 돌지도 않는다

    def test_dismiss_clears_without_running(self, reg, missed, sent, tmp_path):
        assert reg.dismiss(missed.id) is True
        reg.settle()
        assert sent == [] and reg.get(missed.id).missed_at is None
        assert [r["event"] for r in _log(tmp_path)] == ["missed", "skipped"]

    def test_dismiss_without_a_question_is_a_noop(self, reg):
        s = reg.add("0 9 * * *", "x")
        assert reg.dismiss(s.id) is False
        assert reg.dismiss("nope") is False


class TestLifetime:
    def test_enabled_schedule_keeps_the_process_alive(self, reg):
        assert reg.has_active_work() is False
        s = reg.add("0 9 * * *", "x")
        assert reg.has_active_work() is True
        reg.set_enabled(s.id, False)
        assert reg.has_active_work() is False
        reg.set_enabled(s.id, True)
        reg.delete(s.id)
        assert reg.has_active_work() is False

    def test_next_wake_is_the_earliest_enabled_fire(self, reg):
        reg.add("0 12 * * *", "noon")
        early = reg.add("30 8 * * *", "early")
        assert reg.next_wake() == datetime(2026, 10, 5, 8, 30)
        reg.set_enabled(early.id, False)
        assert reg.next_wake() == datetime(2026, 10, 5, 12, 0)

    def test_sleep_is_capped(self, reg):
        reg.add("0 9 * * 1", "weekly")  # 한 시간 뒤
        assert reg._sleep_for() == 300.0

    def test_on_change_fires_on_every_mutation(self, reg):
        seen = []
        reg.on_change = lambda: seen.append(1)
        s = reg.add("0 9 * * *", "x")
        reg.set_enabled(s.id, False)
        reg.delete(s.id)
        assert len(seen) == 3


class TestThread:
    def test_rearm_wakes_the_loop_to_fire_a_new_schedule(self, tmp_path, sent):
        """실제 스레드: 자고 있는 루프가 add 의 rearm 으로 깨어 정시 발화를
        잡는다 (가짜 시계가 발화 시각을 가리키게 한 뒤 깨운다)."""
        import threading

        clock = Clock(datetime(2026, 10, 5, 8, 59, 0))
        fired = threading.Event()
        r = ScheduleRegistry(tmp_path, clock=clock)

        def enq(prompt, nickname):
            sent.append(prompt)
            fired.set()

        r.enqueue = enq
        try:
            r.add("0 9 * * *", "x")  # 스레드 시작, 08:59 라 발화 없음
            clock.advance(minutes=1)
            r._rearm.set()
            assert fired.wait(5), "loop did not wake on rearm"
            assert sent == ["x"]
        finally:
            r.stop()
