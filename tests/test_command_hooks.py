"""시작·끝 훅은 일이 돌면 불린다 — 누가 띄웠든(모델의 도구든 사람의 명령이든), run·web 어디서든.

사람의 명령(`@<profile> <task>`·`/<skill>`)은 run·web 모두 `try_dispatch_agent_or_skill` 하나로 들어온다 —
표면의 차이는 결과를 받는 출력 어댑터뿐이라 그것을 매개변수로 둔다. 훅은 세션의 러너
(`session_slots.get_hook_runner`)가 받는다: 여기서는 그 슬롯에 기록만 하는 러너를 꽂아 본다.
모델이 도구로 부르는 길은 표면과 무관하다(루프 안) — `tests/test_run_agent.py::TestHooks` 와 아래 스킬 한 건.
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.loop.kind import Caller
from agent_cli.main import _ConsoleDispatchOutput, try_dispatch_agent_or_skill
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.session_slots import (
    get_agent_registry,
    get_hook_runner,
    set_agent_registry,
    set_hook_runner,
)
from agent_cli.skills import load_skills
from agent_cli.skills.executor import SkillLoop
from agent_cli.subagent.agents_live import AgentRegistry
from agent_cli.tools.result import ToolResult
from tests.binding_support import make_binding
from tests.loop_kinds import make_caller

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)
#: 훅을 기다리는 상한 — 넘으면 실패다(결과를 바꾸는 값이 아니다)
_PATIENCE_S = 20.0
#: main 세션의 MCP 매니저 자리에 서는 표지 — 훅 ctx 까지 같은 객체가 가는지 본다
MCP = object()


class RecordingHooks:
    """세션 러너 자리에 서서 불린 사건을 적는다. 끝 사건이 오면 ``ended`` 를 세운다."""

    def __init__(self) -> None:
        self.fired: list[tuple[str, dict]] = []
        self.ended = threading.Event()

    def fire(self, event: str, **kw) -> None:
        self.fired.append((event, kw))
        if event.endswith("End"):
            self.ended.set()

    def events(self) -> list[str]:
        return [e for e, _ in self.fired]


@pytest.fixture
def hooks():
    prev = get_hook_runner()
    runner = RecordingHooks()
    set_hook_runner(runner)
    yield runner
    set_hook_runner(prev)


@pytest.fixture
def reg(tmp_path):
    prev = get_agent_registry()
    registry = AgentRegistry(tmp_path / "sess")
    set_agent_registry(registry)
    yield registry
    registry.shutdown_all()
    set_agent_registry(prev)


def _console_output():
    return _ConsoleDispatchOutput()


def _web_output():
    from agent_cli.render.web import WebRenderer
    from agent_cli.web.slash import WebDispatchOutput

    return WebDispatchOutput(WebRenderer())


SURFACES = pytest.mark.parametrize(
    "output", [_console_output, _web_output], ids=["run", "web"]
)


def _command(text: str, output, tmp_path, registry):
    """사람의 명령 한 줄 — 표면이 부르는 그대로."""
    return try_dispatch_agent_or_skill(
        text,
        output(),
        binding=make_binding(provider=object(), capabilities=CAPS),
        max_turns=0,
        verbose=False,
        max_depth=2,
        ctx=ContextManager(tmp_path / "sess", max_context_tokens=10_000),
        session=None,
        mcp_manager=MCP,
        agent_registry=registry,
    )


def _engine(monkeypatch, answer: str) -> None:
    monkeypatch.setattr(
        "agent_cli.loop.run.run_loop",
        lambda kind, start, ctx: ToolResult(True, output=answer),
    )


class TestCommandPath:
    @SURFACES
    def test_slash_skill_fires_start_then_end(
        self, output, tmp_path, reg, hooks, monkeypatch
    ):
        _engine(monkeypatch, "skill done")
        assert _command("/plan a feature", output, tmp_path, reg) is True
        # 스킬은 명령을 부른 스레드에서 돈다 — 돌아왔으면 둘 다 불렸다
        assert hooks.events() == ["OnSkillStart", "OnSkillEnd"]
        start, end = (kw for _, kw in hooks.fired)
        assert start["tool_name"] == end["tool_name"] == "run_skill"
        assert start["mcp_manager"] is MCP and end["mcp_manager"] is MCP
        assert end["skill_result"].output == "skill done"

    @SURFACES
    def test_at_agent_fires_start_at_launch_and_end_when_the_task_ends(
        self, output, tmp_path, reg, hooks, monkeypatch
    ):
        _engine(monkeypatch, "agent done")
        assert _command("@general look around", output, tmp_path, reg) is True
        # 끝 훅은 에이전트 스레드가 과제를 마치고 내려오며 부른다(회신 배달 뒤) — 그 사건을 기다린다
        assert hooks.ended.wait(_PATIENCE_S)
        assert hooks.events() == ["OnAgentStart", "OnAgentEnd"]
        start, end = (kw for _, kw in hooks.fired)
        assert start["tool_name"] == end["tool_name"] == "agent"
        assert start["tool_input"]["task"] == "look around"
        assert start["mcp_manager"] is MCP and end["mcp_manager"] is MCP
        assert end["delegate_result"].output == "agent done"


class TestOneSource:
    def test_the_command_caller_carries_the_session_runner(self, hooks):
        """루프 밖 호출자의 러너는 세션의 러너다 — 종류들의 포트가 읽는 것과 같은 슬롯."""
        c = Caller.main_session(binding=None, ctx=None, session=None, mcp_manager=MCP)
        assert c.hook_runner is hooks and c.hook_runner is get_hook_runner()
        assert c.mcp_manager is MCP

    def test_no_session_runner_means_no_hooks(self, hooks):
        set_hook_runner(None)  # 부트스트랩을 거치지 않은 조립 — 픽스처가 끝에 되돌린다
        c = Caller.main_session(binding=None, ctx=None, session=None, mcp_manager=None)
        assert c.hook_runner is None


class TestToolPath:
    """모델이 도구로 부르는 길 — 호출자는 돌고 있는 루프(`Caller.from_loop`)이고 표면과 무관하다.
    `agent` 의 ``mode`` "run" 은 `tests/test_run_agent.py::TestHooks` 가 본다."""

    def test_run_skill_fires_start_then_end(self, tmp_path, monkeypatch):
        _engine(monkeypatch, "skill done")
        runner = RecordingHooks()
        caller = make_caller(
            hook_runner=runner,
            mcp_manager=MCP,
            binding=make_binding(provider=object(), capabilities=CAPS),
            ctx=ContextManager(tmp_path / "sess", max_context_tokens=10_000),
        )
        result = SkillLoop(load_skills()["plan"], "a feature", caller).run()
        assert result.success and result.output == "skill done"
        assert runner.events() == ["OnSkillStart", "OnSkillEnd"]
        assert all(kw["mcp_manager"] is MCP for _, kw in runner.fired)
