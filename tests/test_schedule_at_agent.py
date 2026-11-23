"""예약이 낸 ``@agt-<key> 글`` 의 작성자 (v10.149.4 의 규칙 — 사용자 결정 2026-11-19).

예약 발화는 사람의 말처럼 main 상자에 들어오고(항목 별명 = 예약의 `effective_nickname`, 기본 ``Scheduler-<이름>``),
그 말이 ``@agt-…`` 명령이면 에이전트에게 ``[user:<그 별명>]: 글`` 로 간다 — 별명이 있는 사람의 말과 같은 규칙
(`main_input.human_addr`). 회신은 사람 주소라 그 에이전트의 채널 창으로 가고 main 메일함에는 서지 않는다.
"""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_cli import runtime
from agent_cli.command_table import web_input_is_command
from agent_cli.constants import schedule_addr
from agent_cli.main import _command_dispatch
from agent_cli.main_input import MainInput
from agent_cli.pump import ForeverPolicy
from agent_cli.schedule.registry import ScheduleRegistry
from agent_cli.subagent.agent_mail import is_human_addr
from agent_cli.web.surface import WebSurface
from tests.pump_support import make_pump
from tests.test_agents_live import (  # noqa: F401
    _FakeLoopResult,
    make_registry,
    renderer,
)
from tests.test_engine_p2 import wait_until


def _wire_like_the_session(schedules, main_input):
    """예약의 발화 자리를 세션 조립(`runtime.build_session_stores`)과 같게 — 아래 테스트가 그 줄이 그대로인지 본다."""
    schedules.enqueue = lambda prompt, nickname, schedule_id: main_input.enqueue(
        None, prompt, nickname=nickname, src=schedule_addr(schedule_id)
    )


def test_the_session_wires_schedule_fires_into_main_words_this_way():
    """위 도우미가 세션 조립의 그 줄과 같다 — 조립이 바뀌면 이 테스트가 먼저 안다."""
    src = inspect.getsource(runtime.build_session_stores)
    tree = ast.parse(src)
    wired = [
        ast.unparse(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and ast.unparse(n.targets[0]) == "schedules.enqueue"
    ]
    assert wired == [
        (
            "lambda prompt, nickname, schedule_id: main_input.enqueue(None, prompt, "
            "nickname=nickname, src=schedule_addr(schedule_id))"
        )
    ]


def test_scheduled_at_agent_goes_as_user_scheduler_name(tmp_path, renderer):  # noqa: F811
    debts: list = []

    def runner(kind, start, ctx):
        debts.append(reg.reply_debt(f"agent:{key}", "user:Scheduler-nightly"))
        seen.append(start.query)
        return _FakeLoopResult(output="checked"), 0.01

    seen: list = []
    reg = make_registry(tmp_path / "agents", runner=runner)
    key, _ = reg.spawn(name="a")
    assert wait_until(lambda: reg.get(key).state == "idle")

    main_input = MainInput(is_command=web_input_is_command)  # 웹 방의 문 판정(5b-3)
    schedules = ScheduleRegistry(tmp_path / "sched")
    _wire_like_the_session(schedules, main_input)
    s = schedules.add(
        "0 0 1 1 *", f"@{key} look at the logs", label="nightly", source="user"
    )
    spec = SimpleNamespace(
        binding=None,
        max_turns=0,
        verbose=False,
        max_depth=2,
        ctx=None,
        session=None,
        mcp_manager=None,
    )
    surface = WebSurface(
        renderer=MagicMock(),
        server=MagicMock(),
        ctx=None,
        binding=None,
        dispatch=_command_dispatch(
            spec,
            SimpleNamespace(agent_registry=reg),
            MagicMock,
            graceful_interrupt=True,
        ),
    )
    main_runs: list = []
    pump = make_pump(
        main_input=main_input,
        run_main=lambda start: main_runs.append(start.query),
        registry=reg,
        surface=surface,
        policy=ForeverPolicy(),
    )
    try:
        with renderer.lock:
            renderer.calls.clear()
        assert (
            schedules.run_now(s.id) is True
        )  # 스케줄러의 발화 길 — main 상자에 사람의 말로
        real_route = surface.route

        def route_then_close(text, start=None, *, item=None):
            try:
                return real_route(text, start, item=item)
            finally:
                main_input.close()  # 그 한 차례 뒤에 끝낸다(닫힘은 줄 선 말을 이긴다)

        surface.route = route_then_close
        pump.run()  # 그 말을 꺼내 라우팅(명령) — main 런은 열리지 않는다
        assert main_runs == []
        assert wait_until(lambda: seen)
        assert seen[-1] == "[user:Scheduler-nightly]: look at the logs"
        # 사람 주소 — 빚 장부도 사람 빚으로 적는다
        assert is_human_addr("user:Scheduler-nightly")
        assert debts[-1] is not None and debts[-1].human
        # 회신은 그 채널 창으로(사람 주소), main 메일함에는 서지 않는다
        assert wait_until(
            lambda: any(
                c[1].get("direction") == "out" for c in renderer.named("agent_message")
            )
        )
        outs = [
            c[1]
            for c in renderer.named("agent_message")
            if c[1].get("direction") == "out"
        ]
        assert outs[-1]["to"] == "user:Scheduler-nightly"
        assert [e.text for e in reg._post.queued("main")] == []
    finally:
        schedules.close()
        reg.shutdown_all()
