"""예약 배선 — 발화가 입력 큐로 들어가고, 켜진 예약이 프로세스를 살려 둔다
(docs/schedule/DESIGN.md §6.3). `run` 과 `web` 두 런타임 모두.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from agent_cli.input_queue import InputQueue
from agent_cli.schedule.registry import ScheduleRegistry


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class _Waker:
    def __init__(self):
        self.idle = threading.Event()

    def mark_idle(self):
        pass

    def handle_dequeued(self, text):
        return None

    def on_run_end(self):
        pass


class _Agents:
    def has_active_work(self):
        return False


class _Renderer:
    def has_live_connections(self):
        return False

    def worker_is_busy(self):
        return False


class _Server:
    def pending_count(self):
        return 0


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 5, 8, 59, 0))


@pytest.fixture
def reg(tmp_path, clock):
    r = ScheduleRegistry(tmp_path, clock=clock)
    yield r
    r.stop()


class TestRunPump:
    def _pump(self, q, reg, calls, waits):
        from agent_cli.main import _run_message_pump

        done = threading.Event()

        def run():
            _run_message_pump(
                q,
                _Waker(),
                _Agents(),
                lambda text, wake: calls.append(text),
                schedules=reg,
                on_schedule_wait=lambda: waits.append(1),
                poll_secs=0.02,
            )
            done.set()

        threading.Thread(target=run, daemon=True).start()
        return done

    def test_pump_ends_when_there_is_no_schedule(self, reg):
        q, calls, waits = InputQueue(), [], []
        q.enqueue(None, "task")
        assert self._pump(q, reg, calls, waits).wait(2)
        assert calls == ["task"] and waits == []

    def test_pump_waits_on_an_enabled_schedule_and_runs_its_fire(self, reg, clock):
        """일회성 실행이 예약을 걸고 끝나 버리면 "매시간 확인하겠다" 는 약속이
        조용히 사라진다 — 끝나지 않고 기다리며, 발화는 다음 요청으로 돈다."""
        q, calls, waits = InputQueue(), [], []
        reg.enqueue = lambda prompt, nickname: q.enqueue(
            None, prompt, nickname=nickname
        )
        s = reg.add("0 9 * * *", "hourly check")
        q.enqueue(None, "task")
        done = self._pump(q, reg, calls, waits)

        assert not done.wait(0.3), "켜진 예약이 있는데 펌프가 끝났다"
        assert calls == ["task"]
        assert waits == [1], "기다리기 시작할 때 한 번만 알린다"

        clock.now += timedelta(minutes=1)  # 09:00
        reg._rearm.set()
        for _ in range(100):
            if len(calls) == 2:
                break
            threading.Event().wait(0.02)
        assert calls == ["task", "hourly check"]

        reg.delete(s.id)
        assert done.wait(2), "예약이 없어졌는데 펌프가 끝나지 않는다"

    def test_disabled_schedule_does_not_hold_the_pump(self, reg):
        q, calls, waits = InputQueue(), [], []
        s = reg.add("0 9 * * *", "x")
        reg.set_enabled(s.id, False)
        q.enqueue(None, "task")
        assert self._pump(q, reg, calls, waits).wait(2)


class TestWebIdlePredicate:
    def test_enabled_schedule_keeps_the_instance_up(self, reg):
        """예약이 있으면 뷰어가 없어도 스스로 꺼지지 않는다 ("유지" 와 같은
        효과). 꺼지면 예약은 다음 기동까지 발화하지 못한다."""
        from agent_cli.main import web_instance_is_active

        args = (_Renderer(), _Server(), None, None)
        assert not web_instance_is_active(*args, reg)
        s = reg.add("0 9 * * *", "x")
        assert web_instance_is_active(*args, reg)
        reg.set_enabled(s.id, False)
        assert not web_instance_is_active(*args, reg)


class TestNotices:
    def test_wait_notice_names_count_and_next_fire(self, reg):
        from agent_cli.main import _schedule_wait_notice

        reg.add("0 9 * * *", "x")
        reg.add("30 9 * * *", "y")
        text = _schedule_wait_notice(reg)
        assert "예약 2개" in text and "10-05 09:00" in text and "Ctrl-C" in text

    def test_missed_notice_reports_without_running(self, tmp_path, clock):
        """`run` 기동 시: 꺼져 있던 동안 지난 예약은 알리기만 한다."""
        from agent_cli.main import _missed_schedules_notice

        first = ScheduleRegistry(tmp_path, clock=clock)
        first.start = lambda: None
        first.add("0 9 * * *", "x", label="아침 점검")
        clock.now += timedelta(hours=5)

        sent = []
        again = ScheduleRegistry(tmp_path, clock=clock)
        again.enqueue = lambda p, n: sent.append(p)
        text = _missed_schedules_notice(again)
        assert "놓친 예약 1건" in text and "아침 점검" in text
        assert sent == []

    def test_no_missed_notice_when_nothing_was_missed(self, reg):
        from agent_cli.main import _missed_schedules_notice

        reg.add("0 9 * * *", "x")
        assert _missed_schedules_notice(reg) == ""


class TestWebServerInjection:
    def test_scheduled_item_carries_the_schedule_nickname(self):
        """web 의 워커는 큐 아이템의 nickname 으로 화자를 표시한다 — 예약
        발화는 사용자 요청처럼 들어가되 이름은 예약의 것이다."""
        from agent_cli.web.server import WebServer

        server = WebServer.__new__(WebServer)
        server._queue = InputQueue()
        item = server.enqueue_scheduled("weekly report", "⏰ Scheduler")
        assert item["text"] == "weekly report"
        assert item["nickname"] == "⏰ Scheduler"
        assert item["system"] is False  # 화면의 대기열에도 보인다
