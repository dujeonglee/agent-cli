"""정지의 표면 — CLI Ctrl+C(한 번 main · 2초 안 두 번 전부)·``/stop``·웹 API·main 의 제자리 대기 (v10.142.0).

규칙(누가 무엇을 받나)은 tests/test_agent_stop.py. 여기는 사람이 누르는 자리가 그 규칙에 닿는지다. 두 번
누름의 창은 주입한 시계로 정한다(시각에 기대지 않는다).
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from agent_cli.ctrl_c import (
    ALL,
    MAIN,
    MAIN_IDLE,
    STOPPED_MAIN,
    CtrlCPresses,
    stop_for_ctrl_c,
)
from agent_cli.main import STOP_NONE_WORKING, STOP_USAGE, try_dispatch_agent_or_skill
from agent_cli.render.web import WebRenderer
from agent_cli.web.server import WebServer, create_app
from tests.test_engine_p2 import (
    Provider,
    call,
    complete_all,
    contents,
    main_waits_in_place,
    session,  # noqa: F401 — 픽스처
    slow_step,
    wait_until,
)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeRegistry:
    """정지 표면이 부르는 것만 — 무엇을 불렀는지 적는다."""

    def __init__(self, working=()):
        self.working = list(working)
        self.calls: list = []

    def working_agents(self):
        return list(self.working)

    def stop_all_runs(self, by="user"):
        self.calls.append(("all", by))
        out, self.working = self.working, []
        return out

    def resolve_key(self, addr):
        return {"reviewer": "agt-5"}.get(
            addr, addr if addr.startswith("agt-") else None
        )

    def stop_run(self, key, by="user"):
        self.calls.append(("one", key))
        if key == "agt-5":
            return ""
        return f'unknown agent "{key}"'


class TestCtrlCPresses:
    def test_one_press_is_main_two_within_the_window_is_all(self):
        clock = Clock()
        p = CtrlCPresses(clock=clock)
        assert p.press() == MAIN
        clock.now += 2.0  # 창의 끝 — 안이다
        assert p.press() == ALL
        clock.now += 0.1
        assert p.press() == MAIN  # 셋째는 새로 시작
        clock.now += 2.01
        assert p.press() == MAIN  # 창 밖


class TestStopForCtrlC:
    def test_main_running_one_press_stops_main_only(self):
        said, reg, stop = [], FakeRegistry(["agt-3"]), threading.Event()
        stop_for_ctrl_c(MAIN, main_stop=stop, registry=reg, say=said.append)
        assert stop.is_set() and reg.calls == [] and said == [STOPPED_MAIN]

    def test_double_press_stops_main_and_every_agent(self):
        said, reg, stop = [], FakeRegistry(["agt-3", "agt-5"]), threading.Event()
        stop_for_ctrl_c(ALL, main_stop=stop, registry=reg, say=said.append)
        assert stop.is_set() and reg.calls == [("all", "user")]
        assert said == ["■ stopping everything — main and 2 working agents"]

    def test_main_idle_agents_working(self):
        said, reg = [], FakeRegistry(["agt-3"])
        stop_for_ctrl_c(MAIN, main_stop=None, registry=reg, say=said.append)
        assert said == [MAIN_IDLE] and reg.calls == []
        stop_for_ctrl_c(ALL, main_stop=None, registry=reg, say=said.append)
        assert reg.calls == [("all", "user")]

    def test_nothing_to_stop_exits_as_before(self):
        with pytest.raises(KeyboardInterrupt):
            stop_for_ctrl_c(MAIN, main_stop=None, registry=FakeRegistry(), say=print)
        with pytest.raises(KeyboardInterrupt):
            stop_for_ctrl_c(MAIN, main_stop=None, registry=None, say=print)

    def test_press_while_main_is_already_stopping_is_the_escape(self):
        stop = threading.Event()
        stop.set()
        with pytest.raises(KeyboardInterrupt):
            stop_for_ctrl_c(
                MAIN, main_stop=stop, registry=FakeRegistry(["agt-3"]), say=print
            )


class _Out:
    def __init__(self):
        self.said: list = []

    def agent_dispatch_result(self, text, success):
        self.said.append((text, success))


def _dispatch(text, reg):
    out = _Out()
    handled = try_dispatch_agent_or_skill(
        text,
        out,
        binding=None,
        max_turns=0,
        verbose=False,
        max_depth=2,
        ctx=None,
        session=None,
        agent_registry=reg,
    )
    return handled, out.said


class TestSlashStop:
    def test_stop_by_name_or_key(self):
        reg = FakeRegistry()
        assert _dispatch("/stop reviewer", reg) == (True, [])
        assert reg.calls == [("one", "agt-5")]

    def test_stop_all_and_nothing_working(self):
        reg = FakeRegistry(["agt-3"])
        assert _dispatch("/stop all", reg) == (True, [])
        assert _dispatch("/stop all", reg) == (True, [(STOP_NONE_WORKING, False)])

    def test_errors_and_usage(self):
        reg = FakeRegistry()
        assert _dispatch("/stop agt-9", reg) == (
            True,
            [('/stop: unknown agent "agt-9"', False)],
        )
        assert _dispatch("/stop", reg) == (True, [(STOP_USAGE, False)])
        assert _dispatch("/stop a b", reg) == (True, [(STOP_USAGE, False)])
        assert _dispatch("/stop all", None)[1] == [
            ("(no agent registry in this mode)", False)
        ]


@pytest.fixture
def web():
    server = WebServer(WebRenderer(), token="t")
    return server, TestClient(create_app(server))


class TestWebStop:
    def test_scopes(self, web):
        server, client = web
        reg = FakeRegistry(["agt-3"])
        server.agent_registry = reg
        ev = threading.Event()
        server.set_stop_handle(ev)
        r = client.post("/api/stop?token=t", json={"scope": "agents"})
        assert r.json() == {"stopped": False, "agents": ["agt-3"]} and not ev.is_set()
        reg.working = ["agt-5"]
        r = client.post("/api/stop?token=t", json={"scope": "all"})
        assert r.json() == {"stopped": True, "agents": ["agt-5"]} and ev.is_set()

    def test_default_scope_is_main_and_bad_scope_is_400(self, web):
        server, client = web
        reg = FakeRegistry(["agt-3"])
        server.agent_registry = reg
        assert client.post("/api/stop?token=t").json() == {
            "stopped": False,
            "agents": [],
        }
        assert reg.calls == []
        assert client.post("/api/stop?token=t", json={"scope": "x"}).status_code == 400

    def test_agent_stop_endpoint(self, web):
        server, client = web
        server.agent_registry = FakeRegistry()
        assert client.post("/api/agent/agt-5/stop?token=t").json() == {"ok": True}
        assert client.post("/api/agent/agt-9/stop?token=t").status_code == 404
        fresh = TestClient(create_app(server))  # 쿠키 없는 손님
        assert fresh.post("/api/agent/agt-5/stop?token=x").status_code == 401

    def test_agent_stop_conflict(self, web):
        server, client = web

        class Reg(FakeRegistry):
            def stop_run(self, key, by="user"):
                return f'agent "{key}" is not working on a request'

        server.agent_registry = Reg()
        assert client.post("/api/agent/agt-5/stop?token=t").status_code == 409


class TestMainWaitWakesOnAStoppedReply:
    def test_run_stopped_by_the_user_wakes_main_and_closes_the_wait(self, session):  # noqa: F811
        """main 이 run 의 회신을 제자리에서 기다리는 중 사람이 그 run 을 ■ — main 은 같은 런에서 stopped 회신을
        읽고 r- 가 닫힌다. 멈춘 run 은 도구를 더 돌리지 않는다(멈춘 뒤의 응답은 실행되지 않는다)."""
        holder: dict = {}
        steps_seen = threading.Event()

        def step_then_stop(i):
            def step(messages):
                if i == 1:
                    reg = holder["reg"]
                    assert wait_until(lambda: main_waits_in_place(reg))
                    (key,) = list(reg._agents)
                    assert reg.stop_run(key) == ""  # 응답 도중 ■
                    steps_seen.set()
                    return call("write_file", path=str(holder["target"]), content="x\n")
                return slow_step(i)(messages)

            return step

        p = Provider(
            {
                "MAIN": [
                    call(
                        "agent", mode="run", task="TASK-S find callers of parse_tokens"
                    ),
                    call("wait"),
                    complete_all("main saw the stop"),
                ],
                "TASK-S": [step_then_stop(0), step_then_stop(1)],
            }
        )
        s = session(p)
        holder["reg"] = s["reg"]
        holder["target"] = s["ctx"].history_path.parent / "after-stop.txt"
        result = s["run"]()
        assert steps_seen.is_set()
        assert result.success and result.output.startswith("main saw the stop")
        assert not holder["target"].exists()  # 멈춘 뒤 도구 0회
        text = contents(s["ctx"])
        assert "re [r-" in text and "· stopped by user ──" in text
        assert "The user stopped agt-" in text and "Do not redo this work" in text
        assert 'Last activity: `shell` "ls -d . >/dev/null # step 0" ✓' in text
        assert s["reg"].awaited_replies("main") == []
        (tm,) = s["reg"]._agents.values()
        assert wait_until(lambda: tm.state == "dead")


class TestCliStopLine:
    def test_stop_summary_groups_by_recipient_and_counts_kept(self):
        from agent_cli.render.minimal import stop_summary

        assert (
            stop_summary("stopped by user", [("main", "r-21")], 0)
            == "stopped by user — reply sent to main (r-21)"
        )
        assert stop_summary(
            "stopped by user",
            [("main", "r-30"), ("agt-6", "r-31"), ("main", "r-33")],
            1,
        ) == (
            "stopped by user — replies sent to main (r-30, r-33), agt-6 (r-31); "
            "1 queued request kept"
        )
        assert stop_summary("removed by main", [], 2) == (
            "removed by main — nobody was waiting on it; 2 queued requests kept"
        )
