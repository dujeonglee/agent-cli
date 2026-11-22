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
from agent_cli.main import _ConsoleDispatchOutput, try_dispatch_agent_or_skill
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.session_slots import (
    get_agent_registry,
    get_hook_runner,
    set_agent_registry,
    set_hook_runner,
)
from agent_cli.subagent.agents_live import AgentRegistry
from agent_cli.tools.result import ToolResult
from tests.binding_support import make_binding

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)
#: 훅을 기다리는 상한 — 넘으면 실패다(결과를 바꾸는 값이 아니다)
_PATIENCE_S = 20.0


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
        agent_registry=registry,
    )


def _engine(monkeypatch, answer: str) -> None:
    monkeypatch.setattr(
        "agent_cli.loop.run.run_loop",
        lambda kind, start, ctx: ToolResult(True, output=answer),
    )


class TestCommandPath:
    @SURFACES
    def test_slash_skill_fires_no_start_or_end_hook(
        self, output, tmp_path, reg, hooks, monkeypatch
    ):
        _engine(monkeypatch, "skill done")
        assert _command("/plan a feature", output, tmp_path, reg) is True
        assert hooks.events() == []

    @SURFACES
    def test_at_agent_fires_no_start_or_end_hook(
        self, output, tmp_path, reg, hooks, monkeypatch
    ):
        _engine(monkeypatch, "agent done")
        assert _command("@general look around", output, tmp_path, reg) is True
        (tm,) = reg._agents.values()
        tm.worker.join(_PATIENCE_S)  # 과제가 끝나 내려왔다 — 끝 훅이 있다면 이미 불렸다
        assert not tm.worker.is_alive()
        assert hooks.events() == []
