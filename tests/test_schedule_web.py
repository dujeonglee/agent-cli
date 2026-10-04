"""⏰ 예약 web API (docs/schedule/DESIGN.md §6.4) — 방 화면의 서랍이 쓰는 여섯
엔드포인트. 예약은 세션의 것이라 화면도 agent-cli web 하나다."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agent_cli.render.web import WebRenderer
from agent_cli.schedule.registry import ScheduleRegistry
from agent_cli.schedule.runtime import set_schedule_registry
from agent_cli.web.server import WebServer, create_app

T = "?token=t"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 5, 8, 0, 0))  # 월요일


@pytest.fixture
def stack(tmp_path, clock):
    renderer = WebRenderer()
    server = WebServer(renderer, token="t")
    reg = ScheduleRegistry(tmp_path, clock=clock)
    reg.start = lambda: None
    reg.enqueue = server.enqueue_scheduled
    set_schedule_registry(reg)
    yield server, reg, TestClient(create_app(server))
    set_schedule_registry(None)


def _add(client, **kw):
    body = {"cron": "0 9 * * 1", "prompt": "주간 보고를 작성해줘", **kw}
    return client.post(f"/api/schedules{T}", json=body)


class TestScheduleApi:
    def test_empty_list(self, stack):
        _, _, client = stack
        assert client.get(f"/api/schedules{T}").json() == {"schedules": [], "log": []}

    def test_add_is_a_user_schedule_with_ui_fields(self, stack):
        _, reg, client = stack
        (row,) = _add(client, label="주간 보고").json()["schedules"]
        assert row["source"] == "user"
        assert row["human"] == "매주 월 09:00"
        assert row["next_fire"] == "2026-10-05T09:00"
        assert row["effective_nickname"] == "⏰ Scheduler"
        assert reg.list_all()[0].label == "주간 보고"

    def test_bad_cron_is_400_with_the_reason(self, stack):
        _, _, client = stack
        r = _add(client, cron="99 9 * * *")
        assert r.status_code == 400 and "invalid cron" in r.json()["detail"]

    def test_user_schedules_are_not_capped(self, stack):
        """상한 5개는 에이전트 등록분에만 건다."""
        _, _, client = stack
        for _ in range(7):
            assert _add(client).status_code == 200

    def test_toggle_and_delete(self, stack):
        _, reg, client = stack
        sid = _add(client).json()["schedules"][0]["id"]
        (row,) = client.post(
            f"/api/schedules/{sid}/toggle{T}", json={"enabled": False}
        ).json()["schedules"]
        assert row["enabled"] is False and row["next_fire"] is None
        assert client.delete(f"/api/schedules/{sid}{T}").json()["schedules"] == []
        assert reg.list_all() == []

    def test_unknown_id_is_404(self, stack):
        _, _, client = stack
        assert client.delete(f"/api/schedules/nope{T}").status_code == 404
        assert client.post(f"/api/schedules/nope/run-now{T}").status_code == 404
        assert client.post(f"/api/schedules/nope/dismiss{T}").status_code == 404

    def test_run_now_queues_the_prompt_under_the_nickname(self, stack):
        server, _, client = stack
        sid = _add(client, nickname="주간봇").json()["schedules"][0]["id"]
        view = client.post(f"/api/schedules/{sid}/run-now{T}").json()
        (item,) = server._queue.snapshot()
        assert item["text"] == "주간 보고를 작성해줘" and item["nickname"] == "주간봇"
        assert view["log"][-1]["event"] == "fired"

    def test_missed_fire_is_answered_by_dismiss(self, stack, clock):
        server, reg, client = stack
        sid = _add(client).json()["schedules"][0]["id"]
        clock.now += timedelta(hours=5)
        reg.settle()
        (row,) = client.get(f"/api/schedules{T}").json()["schedules"]
        assert row["missed_at"] == "2026-10-05T09:00:00"

        view = client.post(f"/api/schedules/{sid}/dismiss{T}").json()
        assert view["schedules"][0]["missed_at"] is None
        assert [r["event"] for r in view["log"]] == ["missed", "skipped"]
        assert server._queue.snapshot() == []  # 건너뛰기는 실행하지 않는다

    def test_no_registry_is_503(self):
        set_schedule_registry(None)
        client = TestClient(create_app(WebServer(WebRenderer(), token="t")))
        assert client.get(f"/api/schedules{T}").status_code == 503

    def test_requires_the_token(self, stack):
        _, _, client = stack
        assert client.get("/api/schedules").status_code in (401, 403)


class TestChangeBroadcast:
    def test_mutation_tells_open_pages_to_refetch(self, stack):
        server, reg, _ = stack
        seen = []
        server.renderer._emit = lambda ev, data, **kw: seen.append(ev)
        reg.on_change = server.renderer.broadcast_schedules_changed
        reg.add("0 9 * * *", "x")
        assert seen == ["schedules_changed"]
