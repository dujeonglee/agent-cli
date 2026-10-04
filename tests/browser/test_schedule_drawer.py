"""⏰ 예약 서랍 + 놓친 예약 카드 (v10.12.0, docs/schedule/DESIGN.md §6.4).

실서버 + 헤드리스 크롬: 폼 → API → 레지스트리 → SSE → 화면의 왕복과, 서랍을
열지 않아도 놓친 예약이 대화 위에 뜨는지를 본다.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from agent_cli.schedule.registry import ScheduleRegistry
from agent_cli.schedule.runtime import set_schedule_registry


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 5, 8, 0, 0))  # 월요일


@pytest.fixture
def reg(stack, tmp_path, clock):
    r = ScheduleRegistry(tmp_path, clock=clock)
    r.start = lambda: None
    r.enqueue = stack.server.enqueue_scheduled
    r.on_change = stack.renderer.broadcast_schedules_changed
    set_schedule_registry(r)
    yield r
    set_schedule_registry(None)


def _open(stack, page):
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#schedule-btn", timeout=8000)
    page.click("#schedule-btn")
    page.wait_for_selector("#schedule-drawer.open", timeout=3000)


class TestScheduleDrawer:
    def test_add_from_the_form_then_delete(self, stack, page, reg):
        _open(stack, page)
        assert "예약이 없습니다" in page.inner_text("#sched-list")
        assert page.locator("#schedule-badge").is_hidden()

        page.click("#sched-presets button[data-cron='0 9 * * 1']")
        assert page.input_value("#sched-cron") == "0 9 * * 1"
        page.fill("#sched-label", "주간 보고")
        page.fill("#sched-prompt", "주간 보고를 작성해줘")
        page.click("#sched-add")

        page.wait_for_selector(".sched-item", timeout=3000)
        text = page.inner_text(".sched-item")
        assert "👤" in text and "주간 보고" in text and "매주 월 09:00" in text
        assert "다음 10-05 09:00" in text
        assert page.inner_text("#schedule-badge") == "1"
        assert page.input_value("#sched-prompt") == ""  # 폼이 비워진다
        (s,) = reg.list_all()
        assert s.source == "user" and s.cron == "0 9 * * 1"

        page.click(".sched-item .btn-danger")
        assert _wait(lambda: page.locator(".sched-item").count() == 0)
        assert reg.list_all() == []

    def test_bad_cron_shows_the_reason_and_adds_nothing(self, stack, page, reg):
        _open(stack, page)
        page.fill("#sched-cron", "99 9 * * *")
        page.fill("#sched-prompt", "x")
        page.click("#sched-add")
        assert _wait(lambda: "invalid cron" in page.inner_text("#sched-status"))
        assert reg.list_all() == []

    def test_agent_added_schedule_appears_without_reload(self, stack, page, reg):
        """에이전트가 도구로 등록하면 SSE 로 알려 서랍이 다시 가져온다."""
        _open(stack, page)
        reg.add("0 * * * *", "check the build", label="빌드 확인", source="agent")
        page.wait_for_selector(".sched-item", timeout=3000)
        text = page.inner_text(".sched-item")
        assert "🤖" in text and "빌드 확인" in text

    def test_toggle_off_drops_it_from_the_badge(self, stack, page, reg):
        reg.add("0 9 * * *", "x")
        _open(stack, page)
        page.wait_for_selector(".sched-item", timeout=3000)
        page.click(".sched-item-actions button:has-text('끄기')")
        assert _wait(lambda: page.locator(".sched-item.off").count() == 1)
        assert page.inner_text("#schedule-badge") == "0"
        assert reg.has_active_work() is False


class TestMissedCard:
    def test_missed_fire_is_asked_above_the_chat_without_opening_the_drawer(
        self, stack, page, reg, clock
    ):
        reg.add("0 9 * * *", "아침 점검", label="아침 점검")
        clock.now += timedelta(hours=5)
        reg.settle()

        stack.emit_ready()
        page.goto(stack.url)
        page.wait_for_selector(
            "#sched-missed:not([hidden]) .sched-missed-row", timeout=8000
        )
        assert "놓친 예약: 아침 점검 (10-05 09:00)" in page.inner_text("#sched-missed")
        assert page.inner_text("#schedule-badge") == "1!"
        assert stack.server._queue.snapshot() == []  # 자동 실행 없음

        page.click("#sched-missed button:has-text('지금 실행')")
        assert _wait(lambda: page.locator("#sched-missed").is_hidden())
        (item,) = stack.server._queue.snapshot()
        assert item["text"] == "아침 점검" and item["nickname"] == "⏰ Scheduler"

    def test_skip_clears_the_card_and_runs_nothing(self, stack, page, reg, clock):
        reg.add("0 9 * * *", "x")
        clock.now += timedelta(hours=5)
        reg.settle()

        stack.emit_ready()
        page.goto(stack.url)
        page.wait_for_selector("#sched-missed:not([hidden])", timeout=8000)
        page.click("#sched-missed button:has-text('건너뛰기')")
        assert _wait(lambda: page.locator("#sched-missed").is_hidden())
        assert stack.server._queue.snapshot() == []
        assert reg.list_all()[0].missed_at is None
