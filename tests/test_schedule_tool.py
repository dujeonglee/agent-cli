"""``schedule`` tool — env-gated file contract to an external scheduler (board).

agent-cli writes a request line + reads the board's ack back; env off ⇒ the
tool is not even registered (plain CLI surface unchanged).
"""

from __future__ import annotations

import importlib
import json
import threading

import pytest

from agent_cli.tools.base import RunContext
from agent_cli.tools.schedule import ScheduleTool


@pytest.fixture
def ws(tmp_path):
    """A session dir <ws>/.agent-cli/sessions/<sid> + the .agent-cli dir."""
    sdir = tmp_path / ".agent-cli" / "sessions" / "S1"
    sdir.mkdir(parents=True)
    return tmp_path, sdir


def _acdir(tmp_path):
    return tmp_path / ".agent-cli"


def _run(tool, args, sdir):
    return tool.run(args, ctx=RunContext(session_dir=sdir))


def _board_ack(acdir, results, schedules=None):
    """Simulate the board applying requests → writing schedule-state.json."""
    acdir.mkdir(parents=True, exist_ok=True)
    (acdir / "schedule-state.json").write_text(
        json.dumps({"results": results, "schedules": schedules or []}),
        encoding="utf-8",
    )


class TestValidate:
    def test_bad_mode(self):
        assert "invalid mode" in ScheduleTool().validate({"mode": "nope"})

    def test_add_requires_cron_and_prompt(self):
        t = ScheduleTool()
        assert "prompt" in t.validate({"mode": "add", "cron": "0 9 * * 1"})
        assert "cron" in t.validate({"mode": "add", "prompt": "x"})
        assert t.validate({"mode": "add", "cron": "0 9 * * 1", "prompt": "x"}) is None

    def test_delete_requires_id(self):
        assert "schedule_id" in ScheduleTool().validate({"mode": "delete"})


class TestRun:
    def test_no_session_dir(self):
        r = ScheduleTool().run({"mode": "list"}, ctx=RunContext(session_dir=None))
        assert not r.success and "no active session" in r.error

    def test_add_writes_request_and_returns_ack(self, ws, monkeypatch):
        tmp_path, sdir = ws
        tool = ScheduleTool()
        # 보드 역할: 요청 파일에 라인이 생기면 그 req_id 로 state 를 써 준다
        monkeypatch.setattr("agent_cli.tools.schedule._ACK_TIMEOUT_S", 2.0)

        def fake_board():
            import time as _t

            for _ in range(40):
                try:
                    lines = (
                        (_acdir(tmp_path) / "schedule-requests.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()
                    )
                except OSError:
                    lines = []
                if lines:
                    req = json.loads(lines[-1])
                    _board_ack(
                        _acdir(tmp_path),
                        {req["req_id"]: {"ok": True, "schedule_id": "sid1"}},
                        [
                            {
                                "schedule_id": "sid1",
                                "cron": req["cron"],
                                "human": "매주 월 09:00",
                                "label": req.get("label", ""),
                                "prompt": req["prompt"],
                                "enabled": True,
                            }
                        ],
                    )
                    return
                _t.sleep(0.02)

        th = threading.Thread(target=fake_board)
        th.start()
        r = _run(
            tool,
            {
                "mode": "add",
                "cron": "0 9 * * 1",
                "prompt": "주간 보고",
                "label": "주간",
            },
            sdir,
        )
        th.join()
        assert r.success
        assert "sid1" in r.output
        # 요청 파일에 실제 라인이 append 됐는지
        reqs = (_acdir(tmp_path) / "schedule-requests.jsonl").read_text().splitlines()
        assert json.loads(reqs[-1])["op"] == "add"

    def test_add_includes_nickname_in_request(self, ws, monkeypatch):
        tmp_path, sdir = ws
        monkeypatch.setattr("agent_cli.tools.schedule._ACK_TIMEOUT_S", 0.3)
        _run(
            ScheduleTool(),
            {
                "mode": "add",
                "cron": "0 9 * * 1",
                "prompt": "주간 보고",
                "nickname": "주간봇",
            },
            sdir,
        )
        reqs = (_acdir(tmp_path) / "schedule-requests.jsonl").read_text().splitlines()
        req = json.loads(reqs[-1])
        assert req["nickname"] == "주간봇"

    def test_add_defaults_nickname_to_empty(self, ws, monkeypatch):
        tmp_path, sdir = ws
        monkeypatch.setattr("agent_cli.tools.schedule._ACK_TIMEOUT_S", 0.3)
        _run(ScheduleTool(), {"mode": "add", "cron": "0 9 * * 1", "prompt": "x"}, sdir)
        reqs = (_acdir(tmp_path) / "schedule-requests.jsonl").read_text().splitlines()
        assert json.loads(reqs[-1])["nickname"] == ""

    def test_rejected_ack_is_error(self, ws, monkeypatch):
        tmp_path, sdir = ws
        monkeypatch.setattr("agent_cli.tools.schedule._ACK_TIMEOUT_S", 2.0)

        def fake_board():
            import time as _t

            for _ in range(40):
                p = _acdir(tmp_path) / "schedule-requests.jsonl"
                if p.exists() and p.read_text().strip():
                    req = json.loads(p.read_text().splitlines()[-1])
                    _board_ack(
                        _acdir(tmp_path), {req["req_id"]: {"error": "cap reached"}}
                    )
                    return
                _t.sleep(0.02)

        th = threading.Thread(target=fake_board)
        th.start()
        r = _run(
            tool_run_add(), {"mode": "add", "cron": "* * * * *", "prompt": "x"}, sdir
        )
        th.join()
        assert not r.success and "cap reached" in r.error

    def test_no_ack_is_soft_success(self, ws, monkeypatch):
        # 보드가 응답 안 함(스캐너 지연/미기동) → 부드러운 성공 안내
        _tmp, sdir = ws
        monkeypatch.setattr("agent_cli.tools.schedule._ACK_TIMEOUT_S", 0.3)
        r = _run(ScheduleTool(), {"mode": "list"}, sdir)
        assert r.success and "hasn't acknowledged" in r.output


def tool_run_add():
    return ScheduleTool()


class TestRegistryGate:
    def test_absent_without_env(self, monkeypatch):
        monkeypatch.delenv("AGENT_CLI_SCHEDULER", raising=False)
        import agent_cli.tools.registry as reg

        reg = importlib.reload(reg)
        assert "schedule" not in reg.TOOLS

    def test_present_with_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_SCHEDULER", "1")
        import agent_cli.tools.registry as reg

        reg = importlib.reload(reg)
        try:
            assert "schedule" in reg.TOOLS
        finally:
            monkeypatch.delenv("AGENT_CLI_SCHEDULER", raising=False)
            importlib.reload(reg)  # 다른 테스트 오염 방지 — 기본(미등록)으로 복구
