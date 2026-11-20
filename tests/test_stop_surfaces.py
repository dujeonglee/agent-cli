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

    def stop_all_runs(self, by="user", wakes_requesters=True):
        self.calls.append(("all", by, wakes_requesters))
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
        # "전부" — 그 정지 회신은 main 을 깨우지 않는다 (사용자 결정)
        assert stop.is_set() and reg.calls == [("all", "user", False)]
        assert said == ["■ stopping everything — main and 2 working agents"]

    def test_main_idle_agents_working(self):
        said, reg = [], FakeRegistry(["agt-3"])
        stop_for_ctrl_c(MAIN, main_stop=None, registry=reg, say=said.append)
        assert said == [MAIN_IDLE] and reg.calls == []
        stop_for_ctrl_c(ALL, main_stop=None, registry=reg, say=said.append)
        assert reg.calls == [("all", "user", False)]

    def test_nothing_to_stop_exits_as_before(self):
        with pytest.raises(KeyboardInterrupt):
            stop_for_ctrl_c(MAIN, main_stop=None, registry=FakeRegistry(), say=print)
        with pytest.raises(KeyboardInterrupt):
            stop_for_ctrl_c(MAIN, main_stop=None, registry=None, say=print)

    def test_a_waiting_prompt_is_interrupted_as_before(self):
        """`ask`·확인 프롬프트가 입력을 기다리는 중이면 종전대로 KeyboardInterrupt — 아무것도 멈추지 않는다."""
        reg, stop = FakeRegistry(["agt-3"]), threading.Event()
        for scope in (MAIN, ALL):
            with pytest.raises(KeyboardInterrupt):
                stop_for_ctrl_c(
                    scope, main_stop=stop, registry=reg, say=print, awaiting_input=True
                )
        assert not stop.is_set() and reg.calls == []

    def test_renderer_reports_a_waiting_prompt_only_during_the_read(self):
        import io

        from rich.console import Console

        from agent_cli.render.minimal import MinimalRenderer

        r = MinimalRenderer(Console(file=io.StringIO()))
        seen = []
        assert not r.is_awaiting_input()
        r._guarded_read(lambda: seen.append(r.is_awaiting_input()))
        assert seen == [True] and not r.is_awaiting_input()
        with pytest.raises(EOFError):
            r._guarded_read(lambda: (_ for _ in ()).throw(EOFError()))
        assert not r.is_awaiting_input()

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
        # Stop all agents 의 회신은 main 을 깨우고, Stop everything 의 것은 깨우지 않는다
        assert reg.calls == [("all", "user", True), ("all", "user", False)]

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
        assert (
            "The user stopped agt-" in text
            and "Decide whether to continue another way" in text
        )
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


class TestStopEverythingDoesNotWakeMain:
    """사용자 결정(2026-11-15): "Stop everything" 의 정지 회신은 main 을 깨우지 않고 쌓였다가 사람의 다음 입력
    런에 함께 배달된다. "Stop all agents"·채널 ■/✕ 의 회신은 지금처럼 깨운다."""

    def _main_input(self, s):
        """세션의 main 상자 — 펌프가 런을 열 것을 묻는 자리(`MainInput.wait_to_open`)."""
        from agent_cli.main_input import MainInput

        return MainInput(post=s["reg"]._post)

    def test_everything_leaves_main_asleep_and_the_next_input_run_reads_the_replies(
        self,
        session,  # noqa: F811
    ):
        stop = threading.Event()
        box: dict = {}

        def stop_everything():
            reg = box["s"]["reg"]
            stop.set()  # main 먼저 (서버·Ctrl+C 와 같은 순서)
            assert reg.stop_all_runs(wakes_requesters=False)

        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-E find callers"),
                    call("wait"),
                    complete_all("read the stopped reply"),
                ],
                "TASK-E": [slow_step(i) for i in range(400)],
            }
        )
        s = session(p)
        box["s"] = s
        main_input = self._main_input(s)
        threading.Thread(
            target=lambda: (
                wait_until(lambda: main_waits_in_place(s["reg"])),
                stop_everything(),
            ),
            daemon=True,
        ).start()
        result = s["run"](stop_event=stop)
        assert not result.success  # main 의 런은 interrupt
        (tm,) = s["reg"]._agents.values()
        assert wait_until(lambda: tm.state == "dead")
        assert wait_until(lambda: len(s["reg"].queued_for("main")) == 1)
        (view,) = s["reg"].queued_for("main")
        mail = {"wakes_main": view.wakes, "mark": view.mark}  # 메일의 표지 키로
        assert mail["wakes_main"] is False and mail["mark"] == "stopped by user"
        # main 런 0회 — 한가한 펌프가 열 것이 없다
        assert main_input.wait_to_open(0) is None
        assert not s["reg"].arrived_for("main", waking_only=True, words=False)
        # `run` 은 조용해지면 끝난다 — 세션 판정(main 상자·에이전트 몫)
        from agent_cli.runtime import session_has_live_work

        assert not session_has_live_work(post=main_input.post, agent_registry=s["reg"])
        # 끝나도 회신은 agents.json 미러에 남는다
        import json as _json

        saved = _json.loads((s["reg"].session_dir / "agents.json").read_text())
        assert [m.get("wakes_main") for m in saved["pending"]] == [False]
        assert s["reg"].awaited_replies("main") == []  # 기다리던 r- 는 닫혔다
        # 사람의 다음 입력 런이 함께 읽는다
        calls_before = len(p.calls["MAIN"])
        assert s["run"](text="MAIN what happened?").success
        first = p.calls["MAIN"][calls_before]
        text = "\n".join(str(m.get("content") or "") for m in first)
        assert (
            "· stopped by user ──" in text
            and "Decide whether to continue another way" in text
        )

    def test_stop_all_agents_wakes_main(self, session):  # noqa: F811
        p = Provider(
            {
                "MAIN": [call("agent", mode="run", task="TASK-W find callers")],
                "TASK-W": [slow_step(i) for i in range(400)],
            }
        )
        s = session(p)
        main_input = self._main_input(s)
        assert s["run"]().success is not None
        (tm,) = s["reg"]._agents.values()
        assert wait_until(lambda: tm.state == "busy")
        assert s["reg"].stop_all_runs() == [tm.key]
        assert wait_until(lambda: main_input.wait_to_open(0) == ())
        assert s["reg"].arrived_for("main", waking_only=True, words=False)


class TestCtrlCDuringAPrompt:
    def test_both_presses_interrupt_the_prompt_and_stop_nothing(self):
        """주입 시계로 두 번 누름(창 안) — 프롬프트 입력 대기 중이면 둘 다 종전대로 KeyboardInterrupt."""
        clock = Clock()
        presses = CtrlCPresses(clock=clock)
        reg, stop = FakeRegistry(["agt-3"]), threading.Event()
        for _ in range(2):
            scope = presses.press()
            clock.now += 0.5
            with pytest.raises(KeyboardInterrupt):
                stop_for_ctrl_c(
                    scope, main_stop=stop, registry=reg, say=print, awaiting_input=True
                )
        assert not stop.is_set() and reg.calls == []
        # 프롬프트가 끝나면 같은 누름이 다시 main 을 멈춘다
        clock.now += 5
        stop_for_ctrl_c(
            presses.press(), main_stop=stop, registry=reg, say=lambda _t: None
        )
        assert stop.is_set() and reg.calls == []


class TestCommandsTypedDuringTheWait:
    """제자리 대기 중 친 명령은 그 자리에서 라우팅되고 main 을 깨우지 않는다(web 기본 — `wait_for_human_words`)."""

    def _setup(self, handled=("/stop agt-5",)):
        from agent_cli.main_input import MainInput
        from agent_cli.runtime import wait_for_human_words

        q = MainInput()
        routed: list = []

        class _Surface:  # 판정은 표면의 것 — 여기서는 "라우팅이 받는 글" 그대로
            def is_command(self, text):
                return text in handled

            def echo(self, item):
                routed.append(("echo", item["text"]))

            def route(self, text, start=None, *, item=None):
                routed.append(text)
                return text in handled

        return q, routed, wait_for_human_words(q, _Surface())

    def test_a_command_is_routed_in_place_and_does_not_wake(self):
        q, routed, wait = self._setup()
        q.enqueue("c", "/stop agt-5")
        assert wait(0) is False
        # 말풍선이 실행 앞에 — 펌프가 연 차례·턴 경계와 같다
        assert routed == [("echo", "/stop agt-5"), "/stop agt-5"]
        assert q.pending_count() == 0

    def test_plain_words_wake_and_stay_queued(self):
        q, routed, wait = self._setup()
        q.enqueue("c", "Skip tests/")
        assert wait(0) is True and routed == [] and q.pending_count() == 1

    def test_words_that_are_not_a_command_stay_first_and_wake(self):
        q, routed, wait = self._setup(handled=())
        q.enqueue("c", "/Users/me/x — look at this")
        q.enqueue("c", "second")
        assert wait(0) is True and routed == []  # 명령이 아니면 꺼내지 않는다
        assert [i["text"] for i in q.snapshot()] == [
            "/Users/me/x — look at this",
            "second",
        ]

    def test_mail_is_not_human_words(self):
        from agent_cli.post import envelope as post_envelope

        q, routed, wait = self._setup()
        q.post.deliver(post_envelope.notice("n-1", "job", "main", "done"))
        assert wait(0) is False and routed == [] and q.pending_count() == 0
