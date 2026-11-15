"""run 에이전트 = 채널 — 실브라우저 (docs/agent-engine/DESIGN.md §2.5).

실제 호스트(`run_agents`)·실제 레지스트리·실제 WebRenderer 로 돌리고(엔진만 가짜) 화면을 본다:

1. 부른 자리(main)에 한 줄 카드 `→ Run … working · open channel` — 끝나면 `done`.
2. `open channel` 이 그 run 의 채널로 — 채널엔 런 블록(머리 = 과제), 그 안에 단계.
3. 끝난 run 채널은 회색 칩(`.ov-ch.run.done`, ✕/↻ 없음), 입력창은 닫혀 있다.
4. 깊이 2 — run 안의 run 도 자기 채널이고, 한 줄 카드는 부른 run 의 런 블록에 놓인다.
5. resume 재생 — 사이드카(`scopes.jsonl`)와 run 디렉터리의 history 로 같은 카드·채널이 선다.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.result import ToolResult
from tests.binding_support import make_binding
from tests.loop_kinds import make_caller

CAPS = ModelCapabilities(32768, 4096, False)


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def live(stack, monkeypatch, tmp_path):
    """세션처럼: 렌더러 = 이 스택의 WebRenderer, 슬롯 레지스트리 = 스택의 레지스트리."""
    from agent_cli import render as render_mod
    from agent_cli.context.manager import ContextManager
    from agent_cli.session_slots import get_agent_registry, set_agent_registry

    monkeypatch.setattr(render_mod, "_renderer", stack.renderer)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: stack.renderer)
    prev = get_agent_registry()
    set_agent_registry(stack.server.agent_registry)
    ctx = ContextManager(tmp_path / "sess", max_context_tokens=20_000)
    yield ctx
    set_agent_registry(prev)


def _engine_steps(kind, start, ctx, *, gate=None):
    """런 하나 — 도구 한 번(관찰까지 기록·렌더) 뒤 최종답."""
    from agent_cli.render import render_step

    ctx.add(
        {
            "role": "assistant",
            "thought": "look",
            "ops": [{"action": "read_file", "action_input": {"path": "a.py"}}],
        }
    )
    render_step(
        "action", "", 1, tool_name="read_file", tool_input=json.dumps({"path": "a.py"})
    )
    ctx.add({"role": "user", "tool": "read_file", "success": True, "content": "x"})
    render_step("observation", "x", 1, tool_name="read_file", success=True)
    if gate is not None:
        gate.wait(10)
    from agent_cli.render import get_renderer

    get_renderer().final("found 7 callers", 2)
    return ToolResult(True, output="found 7 callers")


def _call_run(ctx, args, *, depth=0):
    from agent_cli.subagent.run_agent import run_agents

    caller = make_caller(
        ctx=ctx,
        depth=depth,
        binding=make_binding(provider=object(), capabilities=CAPS),
        stop_event=threading.Event(),
    )
    return run_agents(args, caller)


def _run_key(stack):
    keys = [
        t.key for t in stack.server.agent_registry._agents.values() if t.ends_after_task
    ]
    return keys[-1] if keys else ""


class TestRunChannel:
    def test_link_card_channel_and_grey_chip(self, stack, page, live, monkeypatch):
        gate = threading.Event()
        monkeypatch.setattr(
            "agent_cli.loop.run.run_loop",
            lambda kind, start, ctx: _engine_steps(kind, start, ctx, gate=gate),
        )
        stack.emit_ready()
        page.goto(stack.url)
        page.wait_for_selector("#ov-channels", state="attached")
        out = {}
        t = threading.Thread(
            target=lambda: out.setdefault(
                "r",
                _call_run(
                    live,
                    {"tasks": [{"task": "find every caller", "agent": "code-analyst"}]},
                ),
            )
        )
        t.start()
        link = page.locator("#messages > .card-run-link")
        assert _wait(lambda: link.count() == 1)
        key = _run_key(stack)
        assert link.first.get_attribute("data-ch") == "main"
        assert "working" in link.first.inner_text()
        assert key in link.first.inner_text()
        chip = page.locator(f'.ov-ch[data-key="{key}"]')
        assert _wait(lambda: chip.count() == 1)
        assert "run" in chip.get_attribute("class")
        assert chip.locator(".ov-ch-ctl").count() == 0  # ✕/↻ 없음 (정지는 P3)

        gate.set()
        t.join(10)
        assert out["r"].success
        assert _wait(lambda: "done" in link.first.inner_text())
        assert _wait(lambda: "done" in (chip.get_attribute("class") or ""))
        assert "dead" not in chip.get_attribute("class")  # 지운 줄이 아니라 회색

        # open channel → 그 채널의 런 블록(머리 = 과제, 몸통 = 단계)
        link.first.locator(".run-link-open").click()
        assert _wait(lambda: "on" in (chip.get_attribute("class") or ""))
        block = page.locator(f'#messages > .card-run[data-task-id="{key}#run"]')
        assert block.count() == 1 and block.is_visible()
        assert "find every caller" in block.locator(".run-head").inner_text()
        assert block.locator(".run-body .card-assistant").count() >= 1
        assert not link.first.is_visible()  # main 의 카드는 main 채널에만
        assert page.locator("#input").is_disabled()
        assert "takes no messages" in page.locator("#input").get_attribute(
            "placeholder"
        )

    def test_depth_two_run_is_its_own_channel_linked_inside_the_caller(
        self, stack, page, live, monkeypatch
    ):
        def engine(kind, start, ctx):
            if start.query.startswith("outer"):
                inner = _call_run(ctx, {"tasks": [{"task": "inner job"}]}, depth=1)
                assert inner.success
                return ToolResult(True, output="outer done")
            return _engine_steps(kind, start, ctx)

        monkeypatch.setattr("agent_cli.loop.run.run_loop", engine)
        stack.emit_ready()
        page.goto(stack.url)
        page.wait_for_selector("#ov-channels", state="attached")
        assert _call_run(live, {"tasks": [{"task": "outer job"}]}).success
        reg = stack.server.agent_registry
        outer, inner = [t.key for t in reg._agents.values() if t.ends_after_task]
        assert _wait(lambda: page.locator(".card-run-link").count() == 2)
        # main 에는 바깥 run 의 카드 하나, 안쪽 run 의 카드는 바깥 run 의 런 블록 안
        top = page.locator("#messages > .card-run-link")
        assert top.count() == 1 and outer in top.first.inner_text()
        nested = page.locator(
            f'#messages > .card-run[data-task-id="{outer}#run"] .card-run-link'
        )
        assert nested.count() == 1 and inner in nested.first.inner_text()
        assert (
            page.locator(
                f'#messages > .card-run[data-task-id="{inner}#run"]'
            ).get_attribute("data-ch")
            == inner
        )


class TestRunChannelResume:
    def test_replay_draws_the_same_card_and_channel(
        self, stack, page, live, monkeypatch, tmp_path
    ):
        from agent_cli.render.web import WebRenderer

        # 1) 지난 세션: 사이드카에 적는 렌더러로 run 하나를 돌린다
        sdir = tmp_path / "sess"
        recorder = WebRenderer()
        recorder._scope_log_path = sdir / "scopes.jsonl"
        from agent_cli import render as render_mod

        monkeypatch.setattr(render_mod, "_renderer", recorder)
        monkeypatch.setattr(render_mod, "get_renderer", lambda: recorder)
        monkeypatch.setattr(
            "agent_cli.loop.run.run_loop",
            lambda kind, start, ctx: _engine_steps(kind, start, ctx),
        )
        from agent_cli.session_slots import set_agent_registry
        from agent_cli.subagent.agents_live import AgentRegistry

        before = AgentRegistry(sdir)  # 지난 세션의 레지스트리 — agents.json 을 sdir 에
        set_agent_registry(before)
        assert _call_run(live, {"tasks": [{"task": "find every caller"}]}).success
        (key,) = [t.key for t in before._agents.values() if t.ends_after_task]

        # 2) resume: 새 렌더러(스택)가 사이드카로 재생, 레지스트리는 회색 개체로 복원
        restored = AgentRegistry(sdir)
        assert restored.restore() == 0
        monkeypatch.setattr(render_mod, "_renderer", stack.renderer)
        monkeypatch.setattr(render_mod, "get_renderer", lambda: stack.renderer)
        stack.renderer._scope_log_path = sdir / "scopes.jsonl"
        stack.emit_ready()
        stack.renderer.agent_roster(restored.roster_snapshot())

        class _Ctx:
            def get_raw_messages(self):
                return [{"role": "user", "content": "go", "ts": "2020-01-01T00:00:00"}]

        stack.renderer.replay_session(_Ctx())
        page.goto(stack.url)
        link = page.locator("#messages > .card-run-link")
        assert _wait(lambda: link.count() == 1)
        assert "done" in link.first.inner_text()
        chip = page.locator(f'.ov-ch[data-key="{key}"]')
        assert _wait(lambda: chip.count() == 1)
        assert "done" in chip.get_attribute("class")
        link.first.locator(".run-link-open").click()
        block = page.locator(f'#messages > .card-run[data-task-id="{key}#run"]')
        assert _wait(lambda: block.count() == 1 and block.is_visible())
        assert "find every caller" in block.locator(".run-head").inner_text()
        # run 디렉터리의 history 가 통째로 블록 안에 재생된다
        assert block.locator(".run-body .card-assistant").count() >= 1
