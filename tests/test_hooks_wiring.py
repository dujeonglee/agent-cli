"""Python 훅 배선 (v10.35.0) — 세션당 `HookRunner` 하나를 네 루프가 같이 본다.

종전엔 `ports_for_*` 전부 `hook_runner=None`(사유 "배선만 준비 — 응용이 생기면
연결")이라 README 가 문서화한 Python 훅이 어느 루프에서도 돌지 않았다. 첫 응용이
워크스페이스 스냅샷이고, 서브에이전트의 shell 도 입력을 망치므로 네 루프 전부다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.hooks.runtime import get_hook_runner, set_hook_runner
from agent_cli.runtime import (
    build_hook_runner,
    ports_for_main,
    ports_for_oneshot,
    ports_for_resident,
    ports_for_skill,
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
    return {
        "main": ports_for_main(
            agent_registry=_Reg(),
            mcp_manager=_S,
            dequeue_user_message=_S,
            route_message=_S,
        ),
        "resident": ports_for_resident(
            key="k1", message_handler=_S, questions=_S, absorb_inbox=_S, peer_roster=_S
        ),
        "skill": ports_for_skill(agent_registry=_Reg(), owner="agent:parent"),
        "oneshot": ports_for_oneshot(owner="agent:parent"),
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
        """끝에서 끝까지: 프로젝트 훅 파일 → 세션 러너 → `ports_for_main` → ToolBridge
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

        build_hook_runner()
        ports = ports_for_main(
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
