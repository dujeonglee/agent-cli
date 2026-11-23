"""훅이 넣은 시스템 섹션 — 모델에게 간 ``system`` 에서 본다 (대본 공급자, 실제 훅 파일·실제 러너).

규칙: 훅이 넣은 섹션은 훅이 뺄 때까지, 그 작업 구간 동안 프롬프트에 있다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import render
from agent_cli.hooks.runner import HookRunner
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.render.web import WebRenderer
from tests.loop_ports import make_ports
from tests.loopkit import run_loop

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)

#: 2번째 `PreLLMCall` 에서 앞선 두 발화(`OnSessionStart`·1번째 `PreLLMCall`)가 넣은 섹션을 뺀다
REMOVING = """
EVENTS = ["OnSessionStart", "PreLLMCall"]
N = {"n": 0}
def on_session_start(ctx):
    ctx.inject_system_section("SessStart", "from-session-start")
def pre_llm_call(ctx):
    N["n"] += 1
    if N["n"] == 1:
        ctx.inject_system_section("TurnOne", "from-first-call")
        ctx.inject_system_section("SameFiring", "injected-then-removed")
        ctx.remove_system_section("SameFiring")
    if N["n"] == 2:
        ctx.remove_system_section("TurnOne")
        ctx.remove_system_section("SessStart")
"""

#: 세션 시작에 하나, 호출마다 하나
EVERY_CALL = """
EVENTS = ["OnSessionStart", "PreLLMCall"]
def on_session_start(ctx):
    ctx.inject_system_section("SessStart", "from-session-start")
def pre_llm_call(ctx):
    ctx.inject_system_section("EveryCall", "from-every-call")
"""


def _hooks(tmp_path: Path, source: str) -> HookRunner:
    d = tmp_path / "hooks"
    d.mkdir()
    (d / "90_probe.py").write_text(source, encoding="utf-8")
    return HookRunner(hook_dirs=[d])


class Provider:
    """호출 둘은 도구(`read_file`), 셋째는 `complete`. 호출마다 받은 ``system`` 을 적는다."""

    def __init__(self, on_call=None):
        self.systems: list[str] = []
        self._on_call = on_call

    def call(self, messages, system, model, capabilities, **kwargs):
        self.systems.append(system)
        if self._on_call is not None:
            self._on_call(len(self.systems))
        if len(self.systems) < 3:
            return LLMResponse(
                content=json.dumps({"action": "read_file", "path": "x.txt"})
            )
        return LLMResponse(content=json.dumps({"action": "complete", "result": "done"}))

    def has(self, title: str) -> list[bool]:
        """호출마다 그 섹션이 프롬프트에 있었는가."""
        return [f"## {title}\n" in s for s in self.systems]


def _run(tmp_path, provider, runner) -> None:
    result = run_loop(
        query="read x.txt twice",
        provider=provider,
        capabilities=CAPS,
        model="m",
        ports=make_ports(hook_runner=runner),
        max_turns=6,
    )
    assert result.success, result.error
    assert len(provider.systems) == 3


@pytest.fixture
def web_renderer():
    """DIRECTIVE 편집을 런 도중에 반영하는 표면 — 웹 렌더러(`consume_directives_dirty`)."""
    prev = render.get_renderer()
    r = WebRenderer()
    render.set_renderer(r)
    yield r
    render.set_renderer(prev)


class TestRemove:
    def test_remove_cancels_only_what_the_same_firing_injected(self, tmp_path):
        p = Provider()
        _run(tmp_path, p, _hooks(tmp_path, REMOVING))
        assert p.has("SameFiring") == [False, False, False]
        # 앞선 발화가 넣은 섹션은 `remove` 뒤에도 프롬프트에 남는다 (지금 동작)
        assert p.has("TurnOne") == [True, True, True]
        assert p.has("SessStart") == [True, True, True]


class TestRebuild:
    def test_a_directive_edit_drops_the_session_start_section(
        self, tmp_path, web_renderer
    ):
        # 첫 호출 도중 지침이 저장됐다 — `POST /api/directives` 가 세우는 바로 그 표시
        p = Provider(
            on_call=lambda n: web_renderer.mark_directives_dirty() if n == 1 else None
        )
        _run(tmp_path, p, _hooks(tmp_path, EVERY_CALL))
        assert p.has("EveryCall") == [True, True, True]
        # 다시 만든 프롬프트에서 세션 시작 섹션이 사라진다 (지금 동작)
        assert p.has("SessStart") == [True, False, False]
