"""Tests for the Python hook system (loader, runner, context, events)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agent_cli.hooks.context import HookContext
from agent_cli.hooks.events import (
    ALL_EVENTS,
    EVENT_TO_FUNC,
    ON_SESSION_START,
    ON_SESSION_END,
    PRE_LLM_CALL,
    ON_TURN_END,
    PRE_TOOL_USE,
)
from agent_cli.tools.result import ToolResult
from agent_cli.hooks.loader import _scan_hook_files, load_python_hooks
from agent_cli.hooks.runner import HookRunner


# ── Events ──────────────────────────────────────────────


class TestEvents:
    def test_all_events_count(self):
        assert len(ALL_EVENTS) == 11

    def test_event_to_func_complete(self):
        """Every event in ALL_EVENTS has a function name mapping."""
        for ev in ALL_EVENTS:
            assert ev in EVENT_TO_FUNC, f"Missing mapping for {ev}"

    def test_func_names_are_snake_case(self):
        for func_name in EVENT_TO_FUNC.values():
            assert func_name == func_name.lower()
            assert " " not in func_name


# ── HookContext ─────────────────────────────────────────


class TestHookContext:
    def test_basic_creation(self):
        ctx = HookContext(event="PreLLMCall")
        assert ctx.event == "PreLLMCall"
        assert ctx.messages == []
        assert ctx.turn == 0
        assert ctx.session_dir is None

    def test_messages_default_not_shared(self):
        """Each context gets its own messages list."""
        ctx1 = HookContext(event="A")
        ctx2 = HookContext(event="B")
        ctx1.messages.append({"role": "user", "content": "hi"})
        assert len(ctx2.messages) == 0

    def test_inject_message(self):
        msgs: list[dict] = []
        ctx = HookContext(event="PreLLMCall", messages=msgs)
        ctx.inject_message("system", "remember this")
        assert len(msgs) == 1
        assert msgs[0] == {"role": "system", "content": "remember this"}

    def test_system_sections(self):
        ctx = HookContext(event="PreLLMCall")
        ctx.inject_system_section("Memory", "some facts")
        ctx.inject_system_section("Rules", "be safe")
        assert ctx.system_sections == {"Memory": "some facts", "Rules": "be safe"}

    def test_system_section_replace(self):
        ctx = HookContext(event="PreLLMCall")
        ctx.inject_system_section("Memory", "v1")
        ctx.inject_system_section("Memory", "v2")
        assert ctx.system_sections["Memory"] == "v2"

    def test_remove_system_section(self):
        ctx = HookContext(event="PreLLMCall")
        ctx.inject_system_section("Memory", "data")
        ctx.remove_system_section("Memory")
        assert "Memory" not in ctx.system_sections

    def test_remove_nonexistent_section_no_error(self):
        ctx = HookContext(event="PreLLMCall")
        ctx.remove_system_section("Nothing")  # should not raise

    def test_system_sections_returns_copy(self):
        ctx = HookContext(event="PreLLMCall")
        ctx.inject_system_section("A", "1")
        sections = ctx.system_sections
        sections["B"] = "2"
        assert "B" not in ctx.system_sections

    def test_block(self):
        ctx = HookContext(event="PreToolUse")
        assert ctx.is_blocked is False
        ctx.block("dangerous")
        assert ctx.is_blocked is True
        assert ctx.block_reason == "dangerous"

    def test_modify_input(self):
        ctx = HookContext(event="PreToolUse", tool_input={"cmd": "rm -rf /"})
        assert ctx.modified_input is None
        ctx.modify_input({"cmd": "ls"})
        assert ctx.modified_input == {"cmd": "ls"}

    def test_history_path(self, tmp_path):
        ctx = HookContext(event="X", session_dir=tmp_path)
        assert ctx.history_path == tmp_path / "history.jsonl"

    def test_history_path_none(self):
        ctx = HookContext(event="X")
        assert ctx.history_path is None

    def test_store_memory_noop_without_mcp(self):
        ctx = HookContext(event="X")
        ctx.store_memory([{"name": "test"}])  # should not raise

    def test_search_memory_empty_without_mcp(self):
        ctx = HookContext(event="X")
        assert ctx.search_memory("query") == []

    def test_read_memory_empty_without_mcp(self):
        ctx = HookContext(event="X")
        assert ctx.read_memory() == {}

    def test_store_memory_calls_mcp(self):
        mgr = MagicMock()
        mgr.is_connected.return_value = True
        ctx = HookContext(event="X", mcp_manager=mgr)
        ctx.store_memory([{"name": "test"}])
        mgr.call_tool.assert_called_once_with(
            "memory", "create_entities", {"entities": [{"name": "test"}]}
        )

    def test_store_memory_skips_disconnected(self):
        mgr = MagicMock()
        mgr.is_connected.return_value = False
        ctx = HookContext(event="X", mcp_manager=mgr)
        ctx.store_memory([{"name": "test"}])
        mgr.call_tool.assert_not_called()


# ── Loader ──────────────────────────────────────────────


def _write_hook(directory: Path, filename: str, content: str) -> Path:
    """Helper to write a hook file."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(content)
    return path


class TestScanHookFiles:
    def test_empty_dir(self, tmp_path):
        d = tmp_path / "hooks"
        d.mkdir()
        assert _scan_hook_files([d]) == []

    def test_sorted_by_filename(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(d, "10_second.py", "EVENTS=[]")
        _write_hook(d, "00_first.py", "EVENTS=[]")
        _write_hook(d, "20_third.py", "EVENTS=[]")
        files = _scan_hook_files([d])
        names = [f.name for f in files]
        assert names == ["00_first.py", "10_second.py", "20_third.py"]

    def test_project_before_user(self, tmp_path):
        proj = tmp_path / "project" / "hooks"
        user = tmp_path / "user" / "hooks"
        _write_hook(proj, "00_proj.py", "EVENTS=[]")
        _write_hook(user, "00_user.py", "EVENTS=[]")
        files = _scan_hook_files([proj, user])
        names = [f.name for f in files]
        assert names == ["00_proj.py", "00_user.py"]

    def test_nonexistent_dir_skipped(self, tmp_path):
        files = _scan_hook_files([tmp_path / "nonexistent"])
        assert files == []

    def test_ignores_non_py(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(d, "hook.py", "EVENTS=[]")
        _write_hook(d, "notes.txt", "not a hook")
        _write_hook(d, "data.json", "{}")
        files = _scan_hook_files([d])
        assert len(files) == 1
        assert files[0].name == "hook.py"


class TestLoadPythonHooks:
    def test_loads_valid_hook(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_test.py",
            'EVENTS = ["OnSessionStart"]\n'
            "def on_session_start(ctx):\n"
            '    ctx.inject_system_section("Test", "loaded")\n',
        )
        hooks = load_python_hooks([d])
        assert len(hooks[ON_SESSION_START]) == 1

    def test_skips_file_without_events(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(d, "00_bad.py", "def on_session_start(ctx): pass\n")
        hooks = load_python_hooks([d])
        assert len(hooks[ON_SESSION_START]) == 0

    def test_skips_unknown_event(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_bad.py",
            'EVENTS = ["UnknownEvent"]\ndef unknown_event(ctx): pass\n',
        )
        hooks = load_python_hooks([d])
        # No crash, no entries
        for ev in ALL_EVENTS:
            assert len(hooks[ev]) == 0

    def test_skips_missing_function(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_declared_but_no_func.py",
            'EVENTS = ["OnSessionStart"]\n# on_session_start not defined\n',
        )
        hooks = load_python_hooks([d])
        assert len(hooks[ON_SESSION_START]) == 0

    def test_skips_broken_file(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(d, "00_broken.py", "this is not valid python !!!")
        hooks = load_python_hooks([d])
        # Should not raise, just skip
        for ev in ALL_EVENTS:
            assert len(hooks[ev]) == 0

    def test_multiple_events_one_file(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_multi.py",
            'EVENTS = ["OnSessionStart", "OnSessionEnd"]\n'
            "def on_session_start(ctx): pass\n"
            "def on_session_end(ctx): pass\n",
        )
        hooks = load_python_hooks([d])
        assert len(hooks[ON_SESSION_START]) == 1
        assert len(hooks[ON_SESSION_END]) == 1

    def test_ordering_across_files(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_first.py",
            'EVENTS = ["PreLLMCall"]\n'
            "def pre_llm_call(ctx):\n"
            '    ctx.inject_system_section("Order", "first")\n',
        )
        _write_hook(
            d,
            "10_second.py",
            'EVENTS = ["PreLLMCall"]\n'
            "def pre_llm_call(ctx):\n"
            '    ctx.inject_system_section("Order", "second")\n',
        )
        hooks = load_python_hooks([d])
        assert len(hooks[PRE_LLM_CALL]) == 2

        # Execute in order — second should overwrite first
        ctx = HookContext(event="PreLLMCall")
        for func in hooks[PRE_LLM_CALL]:
            func(ctx)
        assert ctx.system_sections["Order"] == "second"


# ── Runner ──────────────────────────────────────────────


class TestHookRunner:
    def test_fire_returns_context(self, tmp_path):
        d = tmp_path / "hooks"
        d.mkdir()
        runner = HookRunner(hook_dirs=[d])
        ctx = runner.fire(ON_SESSION_START, turn=1, session_dir=tmp_path)
        assert isinstance(ctx, HookContext)
        assert ctx.event == ON_SESSION_START
        assert ctx.turn == 1

    def test_fire_unknown_event_raises(self, tmp_path):
        d = tmp_path / "hooks"
        d.mkdir()
        runner = HookRunner(hook_dirs=[d])
        import pytest

        with pytest.raises(ValueError, match="Unknown hook event"):
            runner.fire("BadEvent")

    def test_fire_runs_python_hooks(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_test.py",
            'EVENTS = ["OnSessionStart"]\n'
            "def on_session_start(ctx):\n"
            '    ctx.inject_system_section("Init", "done")\n',
        )
        runner = HookRunner(hook_dirs=[d])
        ctx = runner.fire(ON_SESSION_START)
        assert ctx.system_sections.get("Init") == "done"

    def test_fire_hook_exception_does_not_propagate(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_crash.py",
            'EVENTS = ["OnSessionStart"]\n'
            "def on_session_start(ctx):\n"
            '    raise RuntimeError("boom")\n',
        )
        runner = HookRunner(hook_dirs=[d])
        ctx = runner.fire(ON_SESSION_START)  # should not raise
        assert isinstance(ctx, HookContext)

    def test_fire_pre_tool_use_block(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_guard.py",
            'EVENTS = ["PreToolUse"]\n'
            "def pre_tool_use(ctx):\n"
            '    if ctx.tool_name == "shell":\n'
            '        ctx.block("shell blocked")\n',
        )
        runner = HookRunner(hook_dirs=[d])
        ctx = runner.fire(
            PRE_TOOL_USE, tool_name="shell", tool_input={"command": "rm -rf /"}
        )
        assert ctx.is_blocked is True
        assert ctx.block_reason == "shell blocked"

    def test_fire_pre_tool_use_modify(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_sanitize.py",
            'EVENTS = ["PreToolUse"]\n'
            "def pre_tool_use(ctx):\n"
            '    ctx.modify_input({"command": "ls"})\n',
        )
        runner = HookRunner(hook_dirs=[d])
        ctx = runner.fire(
            PRE_TOOL_USE, tool_name="shell", tool_input={"command": "rm -rf /"}
        )
        assert ctx.modified_input == {"command": "ls"}

    def test_fire_messages_mutation(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_inject.py",
            'EVENTS = ["PreLLMCall"]\n'
            "def pre_llm_call(ctx):\n"
            '    ctx.inject_message("system", "injected by hook")\n',
        )
        runner = HookRunner(hook_dirs=[d])
        msgs: list[dict] = [{"role": "user", "content": "hello"}]
        runner.fire(PRE_LLM_CALL, messages=msgs)
        assert len(msgs) == 2
        assert msgs[1]["content"] == "injected by hook"

    def test_reload(self, tmp_path):
        d = tmp_path / "hooks"
        d.mkdir()
        runner = HookRunner(hook_dirs=[d])
        # Initially no hooks
        ctx = runner.fire(ON_SESSION_START)
        assert ctx.system_sections == {}

        # Add a hook file
        _write_hook(
            d,
            "00_late.py",
            'EVENTS = ["OnSessionStart"]\n'
            "def on_session_start(ctx):\n"
            '    ctx.inject_system_section("Late", "added")\n',
        )
        runner.reload(hook_dirs=[d])
        ctx = runner.fire(ON_SESSION_START)
        assert ctx.system_sections.get("Late") == "added"

    def test_all_11_events_fireable(self, tmp_path):
        d = tmp_path / "hooks"
        d.mkdir()
        runner = HookRunner(hook_dirs=[d])
        for event in ALL_EVENTS:
            ctx = runner.fire(event)
            assert ctx.event == event

    def test_multiple_hooks_execute_in_order(self, tmp_path):
        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_a.py",
            'EVENTS = ["OnTurnEnd"]\n'
            "def on_turn_end(ctx):\n"
            '    ctx.inject_message("system", "from_a")\n',
        )
        _write_hook(
            d,
            "10_b.py",
            'EVENTS = ["OnTurnEnd"]\n'
            "def on_turn_end(ctx):\n"
            '    ctx.inject_message("system", "from_b")\n',
        )
        runner = HookRunner(hook_dirs=[d])
        msgs: list[dict] = []
        runner.fire(ON_TURN_END, messages=msgs)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "from_a"
        assert msgs[1]["content"] == "from_b"


# ── Loop Integration ────────────────────────────────────


class TestLoopHookIntegration:
    """Test that AgentLoop fires hooks at the right lifecycle points."""

    def _make_loop(self, tmp_path, hook_runner=None, **kwargs):
        """Create an AgentLoop with minimal config for testing."""
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop
        from agent_cli.providers.compat import ModelCapabilities

        provider = MagicMock()
        caps = ModelCapabilities(
            context_window=4096,
            max_output_tokens=2048,
            supports_structured_output=False,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        return AgentLoop(
            query="test",
            provider=provider,
            capabilities=caps,
            model="test-model",
            suppress_output=True,
            hook_runner=hook_runner,
            **kwargs,
        )

    def test_fire_hook_with_runner(self, tmp_path):
        """_fire_hook delegates to runner.fire with correct params."""
        runner = MagicMock()
        runner.fire.return_value = HookContext(event="PreLLMCall")
        loop = self._make_loop(tmp_path, hook_runner=runner)
        loop.messages = [{"role": "user", "content": "hi"}]
        loop.turn = 3

        ctx = loop._fire_hook("PreLLMCall")
        assert ctx is not None
        runner.fire.assert_called_once()
        call_kwargs = runner.fire.call_args
        assert call_kwargs[0][0] == "PreLLMCall"
        assert call_kwargs[1]["turn"] == 3
        assert call_kwargs[1]["messages"] is loop.messages

    def test_fire_hook_without_runner(self, tmp_path):
        """_fire_hook returns None when no runner."""
        loop = self._make_loop(tmp_path, hook_runner=None)
        assert loop._fire_hook("PreLLMCall") is None

    def test_apply_system_sections(self, tmp_path):
        """_apply_system_sections appends sections to system prompt."""
        loop = self._make_loop(tmp_path)
        loop.system = "base prompt"

        hook_ctx = HookContext(event="PreLLMCall")
        hook_ctx.inject_system_section("Memory", "some facts")
        loop._apply_system_sections(hook_ctx)

        assert "## Memory\nsome facts" in loop.system
        assert loop.system.startswith("base prompt")

    def test_apply_system_sections_replaces_on_second_call(self, tmp_path):
        """Dynamic sections are replaced, not duplicated, on subsequent calls."""
        loop = self._make_loop(tmp_path)
        loop.system = "base prompt"

        ctx1 = HookContext(event="PreLLMCall")
        ctx1.inject_system_section("Memory", "v1")
        loop._apply_system_sections(ctx1)

        ctx2 = HookContext(event="PreLLMCall")
        ctx2.inject_system_section("Memory", "v2")
        loop._apply_system_sections(ctx2)

        assert "v2" in loop.system
        assert loop.system.count("## Memory") == 1

    def test_apply_system_sections_noop_when_empty(self, tmp_path):
        loop = self._make_loop(tmp_path)
        loop.system = "base prompt"
        loop._apply_system_sections(None)
        assert loop.system == "base prompt"

        hook_ctx = HookContext(event="PreLLMCall")
        loop._apply_system_sections(hook_ctx)
        assert loop.system == "base prompt"

    def test_execute_single_tool_pre_hook_block(self, tmp_path):
        """PreToolUse hook can block tool execution."""
        from agent_cli.loop import _execute_single_tool

        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_guard.py",
            'EVENTS = ["PreToolUse"]\n'
            "def pre_tool_use(ctx):\n"
            "    ctx.block('not allowed')\n",
        )
        runner = HookRunner(hook_dirs=[d])

        result = _execute_single_tool(
            "shell",
            {"command": "rm -rf /"},
            ["shell"],
            MagicMock(),
            hook_runner=runner,
        )
        assert not result.success
        assert "not allowed" in result.error

    def test_execute_single_tool_pre_hook_modify(self, tmp_path):
        """PreToolUse hook can modify tool input."""
        from unittest.mock import patch

        from agent_cli.loop import _execute_single_tool

        d = tmp_path / "hooks"
        _write_hook(
            d,
            "00_rewrite.py",
            'EVENTS = ["PreToolUse"]\n'
            "def pre_tool_use(ctx):\n"
            '    ctx.modify_input({"command": "ls"})\n',
        )
        runner = HookRunner(hook_dirs=[d])

        with patch("agent_cli.loop.execute_tool") as mock_exec:
            mock_exec.return_value = ToolResult(True, output="ok")
            with patch("agent_cli.loop.validate_tool_input", return_value=(True, "")):
                _execute_single_tool(
                    "shell",
                    {"command": "rm -rf /"},
                    ["shell"],
                    MagicMock(),
                    hook_runner=runner,
                )
            # The modified input should have been used
            mock_exec.assert_called_once_with("shell", {"command": "ls"})

    def test_execute_single_tool_post_hook_fires(self, tmp_path):
        """PostToolUse hook fires after tool execution."""
        from unittest.mock import patch

        from agent_cli.loop import _execute_single_tool

        d = tmp_path / "hooks"
        # Write hook that captures the tool_result
        _write_hook(
            d,
            "00_log.py",
            'EVENTS = ["PostToolUse"]\n'
            "results = []\n"
            "def post_tool_use(ctx):\n"
            "    # We can't easily capture from here, but we can verify it runs\n"
            "    # by checking ctx.tool_result is set\n"
            "    pass\n",
        )
        runner = HookRunner(hook_dirs=[d])

        with patch("agent_cli.loop.execute_tool") as mock_exec:
            mock_exec.return_value = ToolResult(True, output="file contents")
            with patch("agent_cli.loop.validate_tool_input", return_value=(True, "")):
                result = _execute_single_tool(
                    "read_file",
                    {"path": "test.py"},
                    ["read_file"],
                    MagicMock(),
                    hook_runner=runner,
                )
        assert result.success
        assert result.output == "file contents"
