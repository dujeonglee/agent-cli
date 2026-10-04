"""``schedule`` tool — add/delete/list against the session's in-process
registry (docs/schedule/DESIGN.md §6.2).

Until v10.12.0 the tool was a file-contract client of agent-board's scheduler
and existed only under ``AGENT_CLI_SCHEDULER=1``; a plain CLI session had no
scheduling. It is now always registered and answers immediately.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from agent_cli.schedule.registry import AGENT_CAP, ScheduleRegistry
from agent_cli.schedule.runtime import set_schedule_registry
from agent_cli.tools import TOOLS
from agent_cli.tools.schedule import ScheduleTool

HANGUL = re.compile(r"[가-힣]")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 5, 8, 0, 0))


@pytest.fixture
def reg(tmp_path, clock):
    r = ScheduleRegistry(tmp_path, clock=clock)
    r.start = lambda: None
    set_schedule_registry(r)
    yield r
    set_schedule_registry(None)


@pytest.fixture
def tool():
    return ScheduleTool()


class TestRegistration:
    def test_always_registered(self, monkeypatch):
        """No env gate: a plain ``agent-cli web``/``run`` has the tool."""
        monkeypatch.delenv("AGENT_CLI_SCHEDULER", raising=False)
        assert "schedule" in TOOLS
        assert not hasattr(ScheduleTool, "env_enabled")

    def test_description_states_the_lifetime(self, tool):
        d = tool.description
        assert "saved with the session" in d
        assert "NOT run automatically" in d
        assert "board" not in d  # 보드는 더 이상 이 기능의 일부가 아니다


class TestValidate:
    def test_mode_and_required_fields(self, tool):
        assert "invalid mode" in tool.validate({"mode": "nope"})
        assert "'prompt' is required" in tool.validate(
            {"mode": "add", "cron": "* * * * *"}
        )
        assert "'cron' is required" in tool.validate({"mode": "add", "prompt": "x"})
        assert "'id' is required" in tool.validate({"mode": "delete"})
        assert tool.validate({"mode": "list"}) is None


class TestRun:
    def test_add_registers_as_agent_and_lists(self, tool, reg):
        res = tool._run(
            {
                "mode": "add",
                "cron": "0 9 * * 1",
                "prompt": "weekly report",
                "label": "Weekly",
            }
        )
        (s,) = reg.list_all()
        assert res.success and f"Scheduled [{s.id}]" in res.output
        assert s.source == "agent"
        assert "Weekly — cron '0 9 * * 1' · next 2026-10-05 09:00" in res.output
        assert "→ weekly report" in res.output

    def test_add_passes_nickname(self, tool, reg):
        tool._run(
            {"mode": "add", "cron": "0 9 * * *", "prompt": "x", "nickname": "Morning"}
        )
        assert reg.list_all()[0].nickname == "Morning"

    def test_bad_cron_is_rejected_with_the_reason(self, tool, reg):
        res = tool._run({"mode": "add", "cron": "99 9 * * *", "prompt": "x"})
        assert not res.success and "schedule add rejected: invalid cron" in res.error
        assert reg.list_all() == []

    def test_agent_cap(self, tool, reg):
        for _ in range(AGENT_CAP):
            assert tool._run(
                {"mode": "add", "cron": "0 9 * * *", "prompt": "x"}
            ).success
        res = tool._run({"mode": "add", "cron": "0 9 * * *", "prompt": "x"})
        assert not res.success and "delete one first" in res.error

    def test_delete_by_id(self, tool, reg):
        tool._run({"mode": "add", "cron": "0 9 * * *", "prompt": "x"})
        sid = reg.list_all()[0].id
        res = tool._run({"mode": "delete", "id": sid})
        assert res.success and "No schedules in this session." in res.output

    def test_delete_unknown_id_shows_what_exists(self, tool, reg):
        tool._run({"mode": "add", "cron": "0 9 * * *", "prompt": "keep me"})
        res = tool._run({"mode": "delete", "id": "nope"})
        assert not res.success
        assert "no schedule 'nope'" in res.error and "keep me" in res.error

    def test_list_marks_disabled_and_missed(self, tool, reg, clock):
        tool._run({"mode": "add", "cron": "0 9 * * *", "prompt": "a"})
        tool._run({"mode": "add", "cron": "0 10 * * *", "prompt": "b"})
        _a, b = reg.list_all()
        reg.set_enabled(b.id, False)
        clock.now += timedelta(hours=3)
        reg.settle()
        out = tool._run({"mode": "list"}).output
        assert "MISSED 2026-10-05T09:00:00 — not run" in out
        assert "disabled" in out

    def test_listing_is_english_even_with_korean_ui_labels(self, tool, reg):
        """``cron.describe`` 는 화면용 한글 라벨("매주 월 09:00")이다 — 모델에게
        가는 목록은 cron 원문을 쓴다 (tests/test_prompt_language.py 의 원칙)."""
        tool._run({"mode": "add", "cron": "0 9 * * 1", "prompt": "x"})
        assert not HANGUL.search(tool._run({"mode": "list"}).output)

    def test_no_registry_is_an_error_not_a_crash(self, tool):
        set_schedule_registry(None)
        res = tool._run({"mode": "list"})
        assert not res.success and "no active session" in res.error


class TestMonitorPointsAtSchedule:
    def test_monitor_description_no_longer_says_board(self):
        """v10.11.2 검토 A3: monitor 가 "On a board session, … schedule" 이라며
        보드 밖에서는 없는 도구를 권했다. 이제 schedule 은 항상 있다."""
        d = TOOLS["monitor"].description
        assert "board" not in d
        assert "prefer `schedule`" in d and "survives a restart" in d
