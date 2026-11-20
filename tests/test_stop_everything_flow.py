""" "Stop everything" 과 "Stop all agents" 의 회신 — 실제 조립(`wire_session`)과 펌프로 (시안 v4.1 §5 종료 술어).

깨우기 장치에 기대지 않고 펌프 바깥에서 보이는 것만 단언한다: 어떤 런이 어떤 순서로 열렸는가(펌프 표면의
기록), main 메일함에 무엇이 남았는가, `run` 이 끝나는가, web 리퍼가 방을 걷는가. `test_stop_surfaces` 의
`TestStopEverythingDoesNotWakeMain`(깨우미 결합)과 같은 사실이다. 세션·대본은 오라클 드라이버의 것을 쓴다.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent_cli.main import web_instance_is_active
from agent_cli.render import get_renderer, set_renderer
from tests.oracle.driver import (
    _PATIENCE_S,
    Provider,
    Session,
    call,
    complete_all,
    runs_ended,
    until,
)


@pytest.fixture
def room(tmp_path, monkeypatch):
    """오라클 드라이버와 같은 작업 자리 — cwd·HOME 을 임시 폴더로, 스냅샷 끔, 렌더러는 끝나면 되돌린다."""
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
    prev = get_renderer()
    yield tmp_path
    set_renderer(prev)


def _pump_rows(s, name):
    return [r for r in s.recorder.lanes.get("pump", []) if r[0] == name]


def _stop_everything_script(box):
    def stop_everything(msgs):
        s = box["s"]
        assert s.recorder.main_waiting.wait(_PATIENCE_S)
        s.surface.stop.set()  # main 먼저 (서버·Ctrl+C 와 같은 순서)
        assert s.reg.stop_all_runs(wakes_requesters=False)
        return call("shell", command="true")

    return Provider(
        {
            "MAIN": [
                call("agent", mode="run", task="TASK-L slow work"),
                call("wait"),
                complete_all("read the stopped reply"),
            ],
            "TASK-L": [stop_everything],
        }
    )


def _quiet_renderer():
    return SimpleNamespace(
        hub=SimpleNamespace(has_live_connections=lambda: False),
        worker_is_busy=lambda: False,
    )


class TestStopEverything:
    def test_run_ends_with_the_quiet_reply_left_undelivered(self, room):
        """run 경로 — 멈춘 회신만 남으면 메일 런 없이 `run` 이 끝난다(회신은 남아 다음 세션에)."""
        box: dict = {}
        s = box["s"] = Session(room, _stop_everything_script(box))
        try:
            s.say("MAIN go")
            s.pump()  # 끝난다 — 조용해졌다
            assert [r[1] for r in _pump_rows(s, "run_starting")] == ["MAIN go"]
            (line,) = s.reg.pending_mail()
            assert line["text"].endswith("· stopped by user")
            assert not s.reg.arrived_for("main", waking_only=True, words=False)
        finally:
            s.close()

    def test_web_quiet_reply_waits_for_the_next_human_run_and_the_room_can_be_reaped(
        self, room
    ):
        box: dict = {}
        s = box["s"] = Session(room, _stop_everything_script(box), path="web")
        t = s.pump_in_thread()
        try:
            s.say("MAIN go", nickname="dj")
            until(lambda: runs_ended(s) >= 1)
            key = s.agent_keys()[0]
            until(lambda: s.agent_state(key) == "dead")
            until(lambda: len(s.reg.pending_mail()) == 1, "quiet reply")
            until(lambda: not s.reg.has_active_work(), "quiet")
            # 방이 걷힌다 — 뷰어 없음·main 한가·깨우는 메일 없음
            assert not web_instance_is_active(
                _quiet_renderer(),
                SimpleNamespace(pending_count=s.main_input.pending_count),
                s.reg,
                monitors=s.stores.monitors,
                schedules=s.stores.schedules,
                harness=s.owners.auditor,
                jobs=s.stores.jobs,
            )
            # 다음 사람 입력 — 그 사이 메일 런은 없었고, 이 런이 멈춘 회신을 함께 읽는다
            s.say("MAIN what happened?", nickname="dj")
            until(lambda: runs_ended(s) >= 2)
            assert [(r[1], r[2]) for r in _pump_rows(s, "run_starting")] == [
                ("MAIN go", False),
                ("MAIN what happened?", False),
            ]
            assert s.reg.pending_mail() == []
            records = [r for r in s.ctx.get_raw_messages() if r.get("role") == "user"]
            assert any("· stopped by user ──" in str(r.get("content")) for r in records)
        finally:
            s.close()
            t.join(10)


class TestStopAllAgents:
    def test_the_stopped_reply_wakes_an_idle_main(self, room):
        """ "Stop all agents" 의 회신은 깨운다 — 한가한 main 에게 메일 런이 열린다."""
        box: dict = {}
        go = threading.Event()

        def held(msgs):
            assert go.wait(_PATIENCE_S)
            return call("shell", command="true")

        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="spawn", name="helper"),
                    complete_all("spawned"),
                    complete_all("read the stopped reply"),
                ],
                "TASK-W": [held],
            }
        )
        s = box["s"] = Session(room, p, path="web")
        t = s.pump_in_thread()
        try:
            s.say("MAIN go", nickname="dj")
            until(lambda: runs_ended(s) >= 1)
            key = s.agent_keys()[0]
            assert s.reg.request(key, "TASK-W slow job") == ""
            until(lambda: s.agent_state(key) == "busy")
            assert s.reg.stop_all_runs() == [key]
            go.set()
            until(lambda: runs_ended(s) >= 2, "mail run")
            (first, second) = _pump_rows(s, "run_starting")
            assert first[1] == "MAIN go" and second[2] is True  # 둘째는 메일 런
            records = [r for r in s.ctx.get_raw_messages() if r.get("role") == "user"]
            assert any("· stopped by user ──" in str(r.get("content")) for r in records)
        finally:
            s.close()
            t.join(10)
