"""Python 훅 배선 (v10.35.0) — 세션당 `HookRunner` 하나를 네 루프가 같이 본다.

종전엔 포트 빌더 전부 `hook_runner=None`(사유 "배선만 준비 — 응용이 생기면
연결")이라 README 가 문서화한 Python 훅이 어느 루프에서도 돌지 않았다. 첫 응용이
워크스페이스 스냅샷이고, 서브에이전트의 shell 도 입력을 망치므로 네 루프 전부다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.hooks.runtime import get_hook_runner, set_hook_runner
from agent_cli.runtime import (
    build_hook_runner,
)


@pytest.fixture(autouse=True)
def _clean_runner():
    set_hook_runner(None)
    yield
    set_hook_runner(None)


class _Reg:
    def question_port(self, key):
        return object()


_S = object()


def _all_ports():
    from agent_cli.loop.kind import PrivateLoop
    from agent_cli.runtime import MainLoop
    from agent_cli.skills.executor import SkillLoop
    from agent_cli.subagent.resident_loop import ResidentLoop

    return {
        "main": MainLoop.make_ports(
            agent_registry=_Reg(),
            mcp_manager=_S,
            dequeue_user_message=_S,
            route_message=_S,
        ),
        "resident": ResidentLoop.make_ports(
            key="k1", message_handler=_S, questions=_S, absorb_inbox=_S, peer_roster=_S
        ),
        "skill": SkillLoop.make_ports(agent_registry=_Reg(), owner="agent:parent"),
        "oneshot": PrivateLoop.make_ports("run:parent"),
    }


class TestSessionRunner:
    def test_build_sets_the_session_singleton(self):
        assert get_hook_runner() is None
        runner = build_hook_runner()
        assert get_hook_runner() is runner

    def test_every_loop_gets_the_same_runner(self):
        """main 만이 아니다 — 상주·스킬·일회성 루프의 shell 도 파괴적이다."""
        runner = build_hook_runner()
        for name, ports in _all_ports().items():
            assert ports.hook_runner is runner, name
            assert "hook_runner" not in ports.unwired, name

    def test_without_bootstrap_the_port_is_unwired_with_a_reason(self):
        for name, ports in _all_ports().items():
            assert ports.hook_runner is None, name
            assert "build_hook_runner" in ports.unwired["hook_runner"], name

    def test_runner_carries_no_shell_hooks(self):
        """셸 훅(`hooks.json`)은 `LoopConfig.hooks_config` 경로가 따로 돈다 —
        러너에도 주면 PreToolUse 마다 두 번 실행된다."""
        runner = build_hook_runner()
        assert runner._shell_hooks_config is None

    def test_teardown_clears_it(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from agent_cli.runtime import teardown_session

        build_hook_runner()
        with patch("agent_cli.context.session.finalize_session"):
            teardown_session(MagicMock(), None)
        assert get_hook_runner() is None


class TestBothBootstrapsBuildIt:
    def test_run_and_web_call_build_hook_runner(self):
        """`build_monitor_registry` 와 같은 자리 — 한쪽만 고치면 run 과 web 이 갈린다."""
        import ast

        src = Path(__import__("agent_cli.main", fromlist=["x"]).__file__).read_text()
        calls = [
            n
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "build_hook_runner"
        ]
        assert len(calls) == 2, f"호출부가 {len(calls)}개 — run/web 둘 다여야 한다"


class TestBuiltinHooksDir:
    def test_builtin_dir_precedes_project_hooks(self, tmp_path, monkeypatch):
        from agent_cli.hooks import loader

        monkeypatch.setattr(
            "agent_cli.paths.project_dir", lambda: tmp_path / ".agent-cli"
        )
        dirs = loader._hook_dirs()
        assert dirs[0] == loader.BUILTIN_HOOKS_DIR
        assert dirs[1] == tmp_path / ".agent-cli" / "hooks"
        assert loader.BUILTIN_HOOKS_DIR.is_dir()

    def test_underscore_files_are_not_hooks(self, tmp_path):
        from agent_cli.hooks.loader import _scan_hook_files

        (tmp_path / "__init__.py").write_text("")
        (tmp_path / "_helper.py").write_text("")
        (tmp_path / "10_real.py").write_text("EVENTS = []\n")
        assert [p.name for p in _scan_hook_files([tmp_path])] == ["10_real.py"]

    def test_a_project_hook_fires_through_the_wired_bridge(self, tmp_path, monkeypatch):
        """끝에서 끝까지: 프로젝트 훅 파일 → 세션 러너 → `MainLoop.make_ports` → ToolBridge
        의 PreToolUse. 종전엔 이 사슬의 가운데가 끊겨 있었다."""
        hooks = tmp_path / ".agent-cli" / "hooks"
        hooks.mkdir(parents=True)
        seen = tmp_path / "seen.txt"
        (hooks / "10_probe.py").write_text(
            "EVENTS = ['PreToolUse']\n"
            "def pre_tool_use(ctx):\n"  # EVENT_TO_FUNC: PreToolUse → pre_tool_use (on_ 없음)
            f"    open({str(seen)!r}, 'a').write(ctx.tool_name + '\\n')\n"
        )
        monkeypatch.setattr(
            "agent_cli.paths.project_dir", lambda: tmp_path / ".agent-cli"
        )
        from agent_cli.loop import LoopConfig, LoopState, ToolBridge
        from agent_cli.runtime import MainLoop

        build_hook_runner()
        ports = MainLoop.make_ports(
            agent_registry=None,
            mcp_manager=None,
            dequeue_user_message=None,
            route_message=None,
        )
        cfg = LoopConfig(tools_list=["shell"], hook_runner=ports.hook_runner)
        bridge = ToolBridge(cfg, LoopState(), ctx=None, provider=None)
        r = bridge._dispatch_tool_with_hooks("shell", {"command": "echo hi"})
        assert r.success
        assert seen.read_text().splitlines() == ["shell"]


class TestSessionStartSectionsReachThePrompt:
    """`OnSessionStart` 가 주입한 시스템 섹션은 프롬프트에 남는다 (v10.36.0).

    종전엔 루프가 `PreLLMCall` 컨텍스트의 섹션만 적용하고 `OnSessionStart` 반환값을
    버렸다 — README 의 `on_session_start → inject_system_section` 예시가 동작하지
    않았고, 내장 스냅샷 훅의 안내가 모델에 닿지 않았다(Harbor 실측: 모델이
    `agent-cli snapshot restore` 를 한 번도 안 쓰고 `.agent-cli` 를 스스로 뒤졌다).
    그리고 `apply_hook_sections` 는 매번 앞의 훅 섹션을 **전부** 버려 첫 LLM 호출에서
    사라졌을 것이다 — 이제 제목별 병합이다.
    """

    def _loop(self):
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop
        from agent_cli.providers.capabilities import ModelCapabilities
        from tests.loop_ports import split_ports

        caps = ModelCapabilities(
            context_window=4096, max_output_tokens=2048, supports_thinking=False
        )
        return AgentLoop(
            query="q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            **split_ports({}),
        )

    def test_session_start_section_survives_a_pre_llm_call_section(self):
        from agent_cli.hooks.context import HookContext

        loop = self._loop()
        start = HookContext(event="OnSessionStart")
        start.inject_system_section("Workspace snapshot", "restore with …")
        loop._apply_system_sections(start)
        assert "## Workspace snapshot" in loop.system

        per_call = HookContext(event="PreLLMCall")
        per_call.inject_system_section("Memory", "v1")
        loop._apply_system_sections(per_call)
        assert "## Workspace snapshot" in loop.system and "## Memory\nv1" in loop.system

        again = HookContext(event="PreLLMCall")
        again.inject_system_section("Memory", "v2")
        loop._apply_system_sections(again)
        assert "## Memory\nv2" in loop.system and "v1" not in loop.system
        assert loop.system.count("## Workspace snapshot") == 1
        assert (
            "<!--" not in loop.system
        )  # 조립 표식은 모델에게 나가지 않는다 (v10.75.1)

    def test_run_applies_the_session_start_context(self):
        """구조 핀: `run()` 이 OnSessionStart 의 반환값을 적용한다."""
        import inspect

        from agent_cli.loop.core import AgentLoop

        src = inspect.getsource(AgentLoop.run)
        assert '_apply_system_sections(self._fire_hook("OnSessionStart"))' in src
