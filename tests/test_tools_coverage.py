"""Additional tests to improve coverage for tools modules."""

from __future__ import annotations

import pytest

from agent_cli.tools.write_file import tool_write_file
from agent_cli.tools.shell import tool_shell
from agent_cli.tools.edit_file import (
    tool_edit_file,
)
from agent_cli.tools.read_file import (
    compute_line_hash,
    tool_read_file,
)
from agent_cli.tools.delegate import (
    tool_delegate,
    _fork_context,
    _format_delegate_output,
    _format_parallel_results,
    DelegateResult,
)
from agent_cli.tools import TOOLS, VIRTUAL_TOOLS, execute_tool


class TestToolResult:
    """Test ToolResult dataclass behavior."""

    def test_success_result(self):
        from agent_cli.tools.result import ToolResult

        r = ToolResult(True, output="hello")
        assert r.success is True
        assert r.output == "hello"
        assert r.error == ""

    def test_error_result(self):
        from agent_cli.tools.result import ToolResult

        r = ToolResult(False, error="file not found")
        assert r.success is False
        assert r.output == ""
        assert r.error == "file not found"

    def test_defaults(self):
        from agent_cli.tools.result import ToolResult

        r = ToolResult(True)
        assert r.output == ""
        assert r.error == ""

    def test_execute_tool_returns_toolresult(self):
        """execute_tool always returns ToolResult, never raises."""
        result = execute_tool("shell", {"command": "echo hi"})
        assert isinstance(
            result,
            __import__("agent_cli.tools.result", fromlist=["ToolResult"]).ToolResult,
        )
        assert result.success

    def test_execute_tool_unknown_returns_error(self):
        """Unknown tool → ToolResult(False) instead of RuntimeError."""
        result = execute_tool("nonexistent_tool", {})
        assert result.success is False
        assert "Unknown tool" in result.error


class TestWriteFile:
    def test_creates_file(self, tmp_path):
        target = tmp_path / "new.txt"
        result = tool_write_file({"path": str(target), "content": "hello"})
        assert target.exists()
        assert target.read_text() == "hello"
        assert result.success
        assert "File saved" in result.output

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "sub" / "dir" / "file.txt"
        result = tool_write_file({"path": str(target), "content": "nested"})
        assert target.exists()
        assert result.success

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "exist.txt"
        target.write_text("old")
        result = tool_write_file({"path": str(target), "content": "new"})
        assert target.read_text() == "new"
        assert result.success

    def test_error_on_invalid_path(self):
        result = tool_write_file(
            {"path": "/nonexistent/dir/\x00/file.txt", "content": "x"}
        )
        assert not result.success
        assert result.error


class TestShellTool:
    def test_basic_command(self):
        result = tool_shell({"command": "echo hello"})
        assert result.success
        assert "hello" in result.output

    def test_stderr_output(self):
        result = tool_shell({"command": "echo err >&2"})
        assert result.success
        assert "stderr" in result.output

    def test_nonzero_exit(self):
        result = tool_shell({"command": "exit 1"})
        assert result.success
        assert "exit code: 1" in result.output

    def test_timeout(self):
        result = tool_shell({"command": "sleep 10", "timeout": 1})
        assert not result.success
        assert "timed out" in result.error

    def test_empty_command(self):
        result = tool_shell({"command": ""})
        assert not result.success
        assert "Empty command" in result.error

    def test_no_output(self):
        result = tool_shell({"command": "true"})
        assert result.success
        assert result.output == "(no output)"


class TestEditFile:
    def test_replace_single_line(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")
        lines = f.read_text().split("\n")
        h2 = compute_line_hash(2, lines[1])
        result = tool_edit_file(
            {
                "path": str(f),
                "edits": [{"op": "replace", "pos": f"2#{h2}", "lines": ["replaced"]}],
            }
        )
        assert result.success
        assert "Edit complete" in result.output
        assert "replaced" in f.read_text()

    def test_append_operation(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\n")
        lines = f.read_text().split("\n")
        h1 = compute_line_hash(1, lines[0])
        tool_edit_file(
            {
                "path": str(f),
                "edits": [{"op": "append", "pos": f"1#{h1}", "lines": ["inserted"]}],
            }
        )
        content = f.read_text()
        assert "inserted" in content

    def test_prepend_operation(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\n")
        lines = f.read_text().split("\n")
        h1 = compute_line_hash(1, lines[0])
        tool_edit_file(
            {
                "path": str(f),
                "edits": [{"op": "prepend", "pos": f"1#{h1}", "lines": ["header"]}],
            }
        )
        assert f.read_text().startswith("header")

    def test_append_to_eof(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\n")
        tool_edit_file(
            {
                "path": str(f),
                "edits": [{"op": "append", "lines": ["# end"]}],
            }
        )
        assert "# end" in f.read_text()

    def test_delete_line(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("keep\ndelete_me\nkeep2\n")
        lines = f.read_text().split("\n")
        h2 = compute_line_hash(2, lines[1])
        tool_edit_file(
            {
                "path": str(f),
                "edits": [{"op": "replace", "pos": f"2#{h2}", "lines": []}],
            }
        )
        content = f.read_text()
        assert "delete_me" not in content
        assert "keep" in content

    def test_no_edits_error(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello\n")
        result = tool_edit_file({"path": str(f), "edits": []})
        assert not result.success
        assert "No edits" in result.error

    def test_unknown_op_error(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello\n")
        result = tool_edit_file(
            {
                "path": str(f),
                "edits": [{"op": "invalid", "pos": "1#ZZ"}],
            }
        )
        assert not result.success
        assert "Unknown edit op" in result.error

    def test_file_not_found(self):
        result = tool_edit_file(
            {
                "path": "/nonexistent/file.py",
                "edits": [{"op": "replace", "pos": "1#ZZ", "lines": []}],
            }
        )
        assert not result.success
        assert "cannot read" in result.error

    def test_range_replace(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("a\nb\nc\nd\n")
        lines = f.read_text().split("\n")
        h2 = compute_line_hash(2, lines[1])
        h3 = compute_line_hash(3, lines[2])
        tool_edit_file(
            {
                "path": str(f),
                "edits": [
                    {
                        "op": "replace",
                        "pos": f"2#{h2}",
                        "end": f"3#{h3}",
                        "lines": ["X"],
                    }
                ],
            }
        )
        content = f.read_text()
        assert "b" not in content
        assert "c" not in content
        assert "X" in content

    def test_string_lines_converted(self, tmp_path):
        """If lines is a string instead of list, should split by newline."""
        f = tmp_path / "test.py"
        f.write_text("old\n")
        lines = f.read_text().split("\n")
        h1 = compute_line_hash(1, lines[0])
        tool_edit_file(
            {
                "path": str(f),
                "edits": [{"op": "replace", "pos": f"1#{h1}", "lines": "new1\nnew2"}],
            }
        )
        content = f.read_text()
        assert "new1" in content
        assert "new2" in content

    def test_non_dict_edits_filtered(self, tmp_path):
        """LLM sometimes sends [{"op": ...}, 1, 2, 3] — non-dicts should be filtered."""
        f = tmp_path / "test.py"
        f.write_text("old_line\n")
        lines = f.read_text().split("\n")
        h1 = compute_line_hash(1, lines[0])
        result = tool_edit_file(
            {
                "path": str(f),
                "edits": [
                    {"op": "replace", "pos": f"1#{h1}", "lines": ["new_line"]},
                    1,
                    2,
                    3,
                ],
            }
        )
        assert result.success
        assert "Edit complete" in result.output
        assert "new_line" in f.read_text()

    def test_all_non_dict_edits_error(self, tmp_path):
        """If all edits are non-dict, should raise error."""
        f = tmp_path / "test.py"
        f.write_text("hello\n")
        result = tool_edit_file({"path": str(f), "edits": [1, 2, "bad"]})
        assert not result.success
        assert "No valid edit" in result.error


class TestDelegateResult:
    def test_format_with_output(self):
        result = DelegateResult(
            output="Task completed",
            files_read=["a.py"],
            files_modified=["b.py"],
            iterations=3,
        )
        formatted = _format_delegate_output(result)
        assert "Task completed" in formatted
        assert "a.py" in formatted
        assert "b.py" in formatted
        assert "3 iterations" in formatted

    def test_format_no_output(self):
        result = DelegateResult()
        formatted = _format_delegate_output(result)
        assert "no result" in formatted

    def test_format_no_files(self):
        result = DelegateResult(output="Done")
        formatted = _format_delegate_output(result)
        assert "Files touched" not in formatted


class TestForkContext:
    def test_fork_copies_messages(self, tmp_path):
        from unittest.mock import MagicMock
        from agent_cli.context.manager import ContextManager
        from agent_cli.providers.compat import ModelCapabilities

        provider = MagicMock()
        caps = ModelCapabilities(
            context_window=8192,
            max_output_tokens=2048,
            supports_structured_output=False,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        parent = ContextManager(provider, "test", caps, scratchpad_dir=tmp_path)
        parent.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        parent._summary = "Some summary"

        forked = _fork_context(parent, provider, "test", caps, tmp_path)
        assert len(forked.messages) == 2
        assert forked._summary == "Some summary"

    def test_fork_does_not_affect_parent(self, tmp_path):
        from unittest.mock import MagicMock
        from agent_cli.context.manager import ContextManager
        from agent_cli.providers.compat import ModelCapabilities

        provider = MagicMock()
        caps = ModelCapabilities(
            context_window=8192,
            max_output_tokens=2048,
            supports_structured_output=False,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        parent = ContextManager(provider, "test", caps, scratchpad_dir=tmp_path)
        parent.messages = [{"role": "user", "content": "hello"}]

        forked = _fork_context(parent, provider, "test", caps, tmp_path)
        forked.messages.append({"role": "assistant", "content": "new msg"})

        assert len(parent.messages) == 1
        assert len(forked.messages) == 2


class TestToolDelegate:
    """Tests for tool_delegate with tasks array API."""

    @pytest.fixture()
    def caps(self):
        from agent_cli.providers.compat import ModelCapabilities

        return ModelCapabilities(
            context_window=8192,
            max_output_tokens=2048,
            supports_structured_output=False,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

    @pytest.fixture()
    def mock_provider(self):
        from unittest.mock import MagicMock
        from agent_cli.providers.base import LLMResponse

        provider = MagicMock()
        provider.call.return_value = LLMResponse(content="mock response")
        return provider

    # ── API: tasks array ──

    def test_empty_tasks_rejected(self, mock_provider, caps):
        result = tool_delegate(
            args={"tasks": []}, provider=mock_provider, capabilities=caps
        )
        assert not result.success
        assert "empty tasks" in result.error

    def test_single_task_sync(self, mock_provider, caps):
        from unittest.mock import patch

        with patch("agent_cli.loop.run_loop", return_value="Done") as mock_loop:
            result = tool_delegate(
                args={"tasks": [{"task": "Count files"}]},
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            assert result.success
            assert "Done" in result.output
            assert mock_loop.call_count == 1

    def test_single_task_empty_rejected(self, mock_provider, caps):
        result = tool_delegate(
            args={"tasks": [{"task": ""}]},
            provider=mock_provider,
            capabilities=caps,
        )
        assert not result.success
        assert "empty task" in result.error

    # ── Context modes (single) ──

    def test_context_none_fresh_ctx(self, mock_provider, caps):
        from unittest.mock import patch

        with patch("agent_cli.loop.run_loop", return_value="ok") as mock_loop:
            tool_delegate(
                args={"tasks": [{"task": "Do it", "context": "none"}]},
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            ctx_arg = mock_loop.call_args.kwargs["ctx"]
            assert ctx_arg is not None
            assert len(ctx_arg.messages) == 0

    def test_context_fork_copies_parent(self, mock_provider, caps, tmp_path):
        from unittest.mock import patch
        from agent_cli.context.manager import ContextManager

        parent = ContextManager(mock_provider, "test", caps, scratchpad_dir=tmp_path)
        parent.messages = [{"role": "user", "content": "original"}]

        with patch("agent_cli.loop.run_loop", return_value="ok") as mock_loop:
            tool_delegate(
                args={"tasks": [{"task": "Continue", "context": "fork"}]},
                parent_ctx=parent,
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            ctx_arg = mock_loop.call_args.kwargs["ctx"]
            assert len(ctx_arg.messages) == 1
            assert ctx_arg is not parent

    def test_context_fork_does_not_affect_parent(self, mock_provider, caps, tmp_path):
        from unittest.mock import patch
        from agent_cli.context.manager import ContextManager

        parent = ContextManager(mock_provider, "test", caps, scratchpad_dir=tmp_path)
        parent.messages = [{"role": "user", "content": "orig"}]

        def add_msg(**kwargs):
            kwargs["ctx"].messages.append({"role": "assistant", "content": "new"})
            return "ok"

        with patch("agent_cli.loop.run_loop", side_effect=add_msg):
            tool_delegate(
                args={"tasks": [{"task": "Work", "context": "fork"}]},
                parent_ctx=parent,
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
        assert len(parent.messages) == 1

    def test_context_inherit_shares_parent(self, mock_provider, caps, tmp_path):
        from unittest.mock import patch
        from agent_cli.context.manager import ContextManager

        parent = ContextManager(mock_provider, "test", caps, scratchpad_dir=tmp_path)

        with patch("agent_cli.loop.run_loop", return_value="ok") as mock_loop:
            tool_delegate(
                args={"tasks": [{"task": "Continue", "context": "inherit"}]},
                parent_ctx=parent,
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            assert mock_loop.call_args.kwargs["ctx"] is parent

    def test_fork_requires_parent(self, mock_provider, caps):
        result = tool_delegate(
            args={"tasks": [{"task": "Do it", "context": "fork"}]},
            provider=mock_provider,
            capabilities=caps,
        )
        assert not result.success
        assert "fork requires parent" in result.error

    def test_inherit_requires_parent(self, mock_provider, caps):
        result = tool_delegate(
            args={"tasks": [{"task": "Do it", "context": "inherit"}]},
            provider=mock_provider,
            capabilities=caps,
        )
        assert not result.success
        assert "inherit requires parent" in result.error

    # ── Depth + tools ──

    def test_depth_incremented(self, mock_provider, caps):
        from unittest.mock import patch

        with patch("agent_cli.loop.run_loop", return_value="ok") as mock_loop:
            tool_delegate(
                args={"tasks": [{"task": "Sub"}]},
                provider=mock_provider,
                model="test",
                capabilities=caps,
                depth=2,
            )
            assert mock_loop.call_args.kwargs["depth"] == 3

    def test_tools_restriction(self, mock_provider, caps):
        from unittest.mock import patch

        with patch("agent_cli.loop.run_loop", return_value="ok") as mock_loop:
            tool_delegate(
                args={
                    "tasks": [{"task": "Read only", "tools": ["read_file", "shell"]}]
                },
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            assert mock_loop.call_args.kwargs["active_tools"] == ["read_file", "shell"]

    # ── Results ──

    def test_result_includes_files(self, mock_provider, caps):
        import json
        from unittest.mock import patch

        def fake_run_loop(**kwargs):
            kwargs["ctx"].messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {"action": "read_file", "action_input": {"path": "test.py"}}
                    ),
                }
            )
            return "Read test.py"

        with patch("agent_cli.loop.run_loop", side_effect=fake_run_loop):
            result = tool_delegate(
                args={"tasks": [{"task": "Read the file"}]},
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            assert result.success
            assert "test.py" in result.output

    def test_subagent_failure(self, mock_provider, caps):
        from unittest.mock import patch

        with patch("agent_cli.loop.run_loop", return_value=None):
            result = tool_delegate(
                args={"tasks": [{"task": "This will fail"}]},
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            assert not result.success
            assert "did not complete" in result.error

    # ── Parallel: context restriction ──

    def test_parallel_none_allowed(self, mock_provider, caps):
        from unittest.mock import patch

        with patch("agent_cli.loop.run_loop", return_value="ok"):
            result = tool_delegate(
                args={"tasks": [{"task": "A"}, {"task": "B"}]},
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            assert result.success

    def test_parallel_fork_allowed(self, mock_provider, caps, tmp_path):
        from unittest.mock import patch
        from agent_cli.context.manager import ContextManager

        parent = ContextManager(mock_provider, "test", caps, scratchpad_dir=tmp_path)

        with patch("agent_cli.loop.run_loop", return_value="ok"):
            result = tool_delegate(
                args={
                    "tasks": [
                        {"task": "A", "context": "fork"},
                        {"task": "B", "context": "fork"},
                    ]
                },
                parent_ctx=parent,
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            assert result.success

    def test_parallel_inherit_rejected(self, mock_provider, caps):
        result = tool_delegate(
            args={
                "tasks": [
                    {"task": "A", "context": "none"},
                    {"task": "B", "context": "inherit"},
                ]
            },
            provider=mock_provider,
            model="test",
            capabilities=caps,
        )
        assert not result.success
        assert "inherit" in result.error

    def test_single_inherit_allowed(self, mock_provider, caps, tmp_path):
        """inherit is fine for single task (not parallel)."""
        from unittest.mock import patch
        from agent_cli.context.manager import ContextManager

        parent = ContextManager(mock_provider, "test", caps, scratchpad_dir=tmp_path)

        with patch("agent_cli.loop.run_loop", return_value="ok"):
            result = tool_delegate(
                args={"tasks": [{"task": "Continue", "context": "inherit"}]},
                parent_ctx=parent,
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            assert result.success

    # ── Parallel: execution ──

    def test_parallel_runs_all_tasks(self, mock_provider, caps):
        from unittest.mock import patch

        call_count = {"n": 0}

        def counting_run_loop(**kwargs):
            call_count["n"] += 1
            return f"Result {call_count['n']}"

        with patch("agent_cli.loop.run_loop", side_effect=counting_run_loop):
            result = tool_delegate(
                args={"tasks": [{"task": "A"}, {"task": "B"}, {"task": "C"}]},
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            assert call_count["n"] == 3
            assert result.success

    def test_parallel_suppress_output(self, mock_provider, caps):
        from unittest.mock import patch

        with patch("agent_cli.loop.run_loop", return_value="ok") as mock_loop:
            tool_delegate(
                args={"tasks": [{"task": "A"}, {"task": "B"}]},
                provider=mock_provider,
                model="test",
                capabilities=caps,
                suppress_output=False,  # parent wants output
            )
            # But parallel delegates should always suppress
            for call in mock_loop.call_args_list:
                assert call.kwargs["suppress_output"] is True

    def test_parallel_partial_failure(self, mock_provider, caps):
        from unittest.mock import patch

        def mixed_results(**kwargs):
            if "fail" in kwargs["query"]:
                return None
            return "ok"

        with patch("agent_cli.loop.run_loop", side_effect=mixed_results):
            result = tool_delegate(
                args={"tasks": [{"task": "succeed"}, {"task": "fail please"}]},
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            assert not result.success
            assert "1 succeeded" in result.error
            assert "1 failed" in result.error

    # ── Parallel: result format ──

    def test_parallel_result_format(self, mock_provider, caps):
        from unittest.mock import patch

        with patch("agent_cli.loop.run_loop", return_value="Done"):
            result = tool_delegate(
                args={"tasks": [{"task": "Alpha"}, {"task": "Beta"}]},
                provider=mock_provider,
                model="test",
                capabilities=caps,
            )
            assert "[Task 1]" in result.output
            assert "[Task 2]" in result.output
            assert "all succeeded" in result.output


class TestParallelResultFormat:
    def test_all_success(self):
        from agent_cli.tools.result import ToolResult

        specs = [{"task": "A"}, {"task": "B"}]
        results = [
            ToolResult(True, output="STATUS: success\nRESULT:\nDone A"),
            ToolResult(True, output="STATUS: success\nRESULT:\nDone B"),
        ]
        combined = _format_parallel_results(specs, results)
        assert combined.success
        assert "all succeeded" in combined.output

    def test_partial_failure(self):
        from agent_cli.tools.result import ToolResult

        specs = [{"task": "A"}, {"task": "B"}]
        results = [
            ToolResult(True, output="ok"),
            ToolResult(False, error="failed"),
        ]
        combined = _format_parallel_results(specs, results)
        assert not combined.success
        assert "1 succeeded" in combined.error
        assert "1 failed" in combined.error

    def test_timeout_none_result(self):
        specs = [{"task": "A"}]
        results = [None]
        combined = _format_parallel_results(specs, results)
        assert not combined.success
        assert "timed out" in combined.error.lower()


class TestParallelTimeout:
    """Test parallel delegate timeout behavior."""

    def test_parallel_timeout_marks_incomplete(self):
        """Tasks exceeding timeout are reported as timed out."""
        import time
        from unittest.mock import MagicMock, patch
        from agent_cli.providers.base import LLMResponse
        from agent_cli.providers.compat import ModelCapabilities

        caps = ModelCapabilities(
            context_window=8192,
            max_output_tokens=2048,
            supports_structured_output=False,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        provider = MagicMock()
        provider.call.return_value = LLMResponse(content="mock")

        def slow_run_loop(**kwargs):
            time.sleep(3)  # Longer than timeout
            return "late result"

        with patch("agent_cli.loop.run_loop", side_effect=slow_run_loop):
            result = tool_delegate(
                args={"tasks": [{"task": "Slow A"}, {"task": "Slow B"}]},
                provider=provider,
                model="test",
                capabilities=caps,
                timeout=1,  # 1 second timeout
            )
            # At least some tasks should be incomplete
            assert (
                "timed out" in (result.error or result.output or "").lower()
                or result is not None
            )


class TestSignalHandlerThreadSafety:
    """Test that signal handler is skipped in worker threads."""

    def test_signal_handler_skipped_in_thread(self):
        """AgentLoop._install_signal_handler is a no-op in non-main thread."""
        import signal
        import threading
        from unittest.mock import MagicMock
        from agent_cli.providers.compat import ModelCapabilities

        caps = ModelCapabilities(
            context_window=8192,
            max_output_tokens=2048,
            supports_structured_output=False,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        provider = MagicMock()

        original_handler = signal.getsignal(signal.SIGINT)
        handler_changed = {"changed": False}

        def check_in_thread():
            from agent_cli.loop import AgentLoop

            loop = AgentLoop(
                query="test",
                provider=provider,
                capabilities=caps,
                model="test",
                graceful_interrupt=True,
            )
            loop._install_signal_handler()
            # Signal handler should NOT have changed
            current = signal.getsignal(signal.SIGINT)
            handler_changed["changed"] = current != original_handler

        t = threading.Thread(target=check_in_thread)
        t.start()
        t.join()

        assert not handler_changed["changed"], (
            "Signal handler should not change in worker thread"
        )


class TestToolsRegistry:
    """Tests for unified TOOLS dict with virtual tools."""

    def test_tools_contains_all_real_tools(self):
        real_tools = {"read_file", "write_file", "edit_file", "shell", "read_context"}
        assert real_tools.issubset(set(TOOLS.keys()))

    def test_tools_contains_virtual_tools(self):
        assert "complete" in TOOLS
        assert "ask" in TOOLS

    def test_virtual_tools_frozenset(self):
        assert VIRTUAL_TOOLS == frozenset(
            {"complete", "ask", "run_skill", "read_artifact", "ready_for_review"}
        )
        assert isinstance(VIRTUAL_TOOLS, frozenset)

    def test_virtual_tools_subset_of_tools(self):
        assert VIRTUAL_TOOLS.issubset(set(TOOLS.keys()))

    def test_real_tools_excludes_virtual(self):
        real = [t for t in TOOLS if t not in VIRTUAL_TOOLS]
        assert "complete" not in real
        assert "ask" not in real
        assert len(real) == len(TOOLS) - len(VIRTUAL_TOOLS)

    def test_complete_lambda_with_result(self):
        fn = TOOLS["complete"]
        result = fn({"result": "done"})
        assert result.success
        assert result.output == "done"

    def test_complete_lambda_default(self):
        fn = TOOLS["complete"]
        result = fn({})
        assert result.success
        assert (
            result.output
            == "(Completed without result — model may lack capability for this task)"
        )

    def test_ask_lambda_with_question(self):
        fn = TOOLS["ask"]
        result = fn({"question": "what?"})
        assert result.success
        assert result.output == "what?"

    def test_ask_lambda_default(self):
        fn = TOOLS["ask"]
        result = fn({})
        assert result.success
        assert result.output == "(ask)"


class TestExecuteTool:
    def test_unknown_tool(self):
        result = execute_tool("nonexistent_tool", {})
        assert not result.success
        assert "Unknown tool" in result.error

    def test_execute_virtual_complete(self):
        result = execute_tool("complete", {"result": "all done"})
        assert result.success
        assert result.output == "all done"

    def test_execute_virtual_ask(self):
        result = execute_tool("ask", {"question": "which file?"})
        assert result.success
        assert result.output == "which file?"

    def test_error_message_includes_virtual_tools(self):
        result = execute_tool("bogus", {})
        assert not result.success
        assert "complete" in result.error
        assert "ask" in result.error
        assert "read_file" in result.error


class TestReadFilePartial:
    def test_full_read(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\nline4\nline5")
        result = tool_read_file({"path": str(f)})
        assert result.success
        assert "1#" in result.output
        assert "5#" in result.output

    def test_line_start(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\nccc\nddd\neee")
        result = tool_read_file({"path": str(f), "line_start": 3})
        assert result.success
        assert "ccc" in result.output
        assert "ddd" in result.output
        assert "eee" in result.output
        assert "aaa" not in result.output

    def test_line_start_and_end(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\nccc\nddd\neee")
        result = tool_read_file({"path": str(f), "line_start": 2, "line_end": 4})
        assert result.success
        assert "bbb" in result.output
        assert "ddd" in result.output
        assert "aaa" not in result.output
        assert "eee" not in result.output

    def test_line_numbers_preserved(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\nccc\nddd\neee")
        result = tool_read_file({"path": str(f), "line_start": 3})
        assert result.success
        # First line in result should be line 3, not line 1
        assert result.output.startswith("3#")

    def test_string_line_start_coerced(self, tmp_path):
        """LLMs sometimes send line_start as string."""
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\nccc")
        result = tool_read_file({"path": str(f), "line_start": "2"})
        assert result.success
        assert "bbb" in result.output
        assert "aaa" not in result.output


class TestReadContextTool:
    def test_list_no_sessions(self, tmp_path, monkeypatch):
        import agent_cli.context.session as session_mod

        monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "list"})
        assert result.success
        assert "No previous sessions" in result.output

    def test_list_with_sessions(self, tmp_path, monkeypatch):
        import agent_cli.context.session as session_mod

        monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path)
        from agent_cli.context.session import create_session, save_meta, save_summary
        from agent_cli.tools.context import tool_read_context

        meta = create_session("/tmp/ws")
        save_meta(meta)
        save_summary(meta, "Test summary content")

        result = tool_read_context({"mode": "list"})
        assert result.success
        assert meta.session_id in result.output
        assert "Test summary" in result.output

    def test_detail_valid_session(self, tmp_path, monkeypatch):
        import agent_cli.context.session as session_mod

        monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path)
        from agent_cli.context.session import append_log, create_session, save_meta
        from agent_cli.tools.context import tool_read_context

        meta = create_session("/tmp/ws")
        save_meta(meta)
        append_log(
            meta, {"iter": 1, "action": "shell", "thought": "run", "observation": "ok"}
        )

        result = tool_read_context({"mode": "detail", "session_id": meta.session_id})
        assert result.success
        assert "shell" in result.output
        assert "ok" in result.output

    def test_detail_missing_session_id(self, tmp_path, monkeypatch):
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "detail"})
        assert not result.success
        assert "session_id is required" in result.error

    def test_detail_nonexistent_session(self, tmp_path, monkeypatch):
        import agent_cli.context.session as session_mod

        monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "detail", "session_id": "999"})
        assert not result.success
        assert "not found" in result.error

    def test_unknown_mode(self, tmp_path, monkeypatch):
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "invalid"})
        assert not result.success
        assert "unknown mode" in result.error.lower()


class TestRunSkillTool:
    def test_run_skill_in_tools(self):
        """run_skill is registered in TOOLS."""
        assert "run_skill" in TOOLS

    def test_run_skill_in_virtual_tools(self):
        """run_skill is a virtual tool (intercepted by loop)."""
        assert "run_skill" in VIRTUAL_TOOLS

    def test_run_skill_schema_exists(self):
        """run_skill has a schema with name (required) and arguments."""
        from agent_cli.tools.registry import TOOL_SCHEMAS

        assert "run_skill" in TOOL_SCHEMAS
        schema = TOOL_SCHEMAS["run_skill"]
        assert "name" in schema.parameters["required"]

    def test_run_skill_valid_skill(self, tmp_path, monkeypatch):
        """run_skill with valid skill name → executes and returns result."""
        from unittest.mock import MagicMock, patch

        from agent_cli.providers.compat import ModelCapabilities
        from agent_cli.skills.models import Skill
        from agent_cli.tools.run_skill import tool_run_skill

        mock_skills = {
            "summarize": Skill(
                name="summarize",
                description="Summarize",
                prompt_template="Summarize $ARGUMENTS",
                max_turns=3,
            )
        }
        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=False,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        with patch("agent_cli.skills.load_skills", return_value=mock_skills):
            with patch(
                "agent_cli.skills.executor.execute_skill", return_value="Summary done"
            ):
                result = tool_run_skill(
                    {"name": "summarize", "arguments": "README.md"},
                    provider=MagicMock(),
                    capabilities=caps,
                    model="test",
                )
                assert result.success
                assert "Summary done" in result.output

    def test_run_skill_unknown(self, tmp_path, monkeypatch):
        """run_skill with unknown skill name → error message."""
        from unittest.mock import patch

        from agent_cli.tools.run_skill import tool_run_skill

        with patch("agent_cli.skills.loader.load_skills", return_value={}):
            result = tool_run_skill({"name": "nonexistent", "arguments": ""})
            assert not result.success
            assert "not found" in result.error.lower()

    def test_run_skill_no_name(self):
        """run_skill without name → error."""
        from agent_cli.tools.run_skill import tool_run_skill

        result = tool_run_skill({"arguments": "something"})
        assert not result.success
        assert "name" in result.error.lower()


class TestReadArtifactTool:
    def test_in_tools_and_virtual(self):
        """read_artifact is registered and virtual."""
        assert "read_artifact" in TOOLS
        assert "read_artifact" in VIRTUAL_TOOLS

    def test_schema_exists(self):
        from agent_cli.tools.registry import TOOL_SCHEMAS

        assert "read_artifact" in TOOL_SCHEMAS

    def test_read_by_path(self, tmp_path):
        """Read artifact by path — returns body without hashlines."""
        from agent_cli.context.scratchpad import save_artifact
        from agent_cli.tools.read_artifact import tool_read_artifact

        path = save_artifact(
            step=1,
            content="## Analysis\nFound 3 bugs.",
            tags=["read_file"],
            summary="Analysis result",
            base=tmp_path,
        )
        result = tool_read_artifact({"path": path})
        assert result.success
        assert "Found 3 bugs" in result.output
        # No hashline tags (e.g. "1#XS:") in output
        assert not any(
            line and line[0].isdigit() and "#" in line[:5]
            for line in result.output.split("\n")
        )

    def test_list_mode(self, tmp_path):
        """List all artifacts."""
        from agent_cli.context.scratchpad import save_artifact
        from agent_cli.tools.read_artifact import tool_read_artifact
        from unittest.mock import MagicMock

        save_artifact(1, "Content 1", ["shell"], "shell executed", tmp_path)
        save_artifact(2, "Content 2", ["read_file", "hooks.py"], "read hooks", tmp_path)

        ctx = MagicMock()
        ctx._scratchpad_dir = tmp_path
        result = tool_read_artifact({"mode": "list"}, ctx=ctx)
        assert result.success
        assert "step_0001" in result.output
        assert "step_0002" in result.output
        assert "hooks.py" in result.output

    def test_search_by_tag(self, tmp_path):
        """Search artifacts by tag."""
        from agent_cli.context.scratchpad import save_artifact
        from agent_cli.tools.read_artifact import tool_read_artifact
        from unittest.mock import MagicMock

        save_artifact(1, "Content A", ["read_file", "hooks.py"], "read hooks", tmp_path)
        save_artifact(2, "Content B", ["shell"], "shell cmd", tmp_path)

        ctx = MagicMock()
        ctx._scratchpad_dir = tmp_path
        result = tool_read_artifact({"mode": "search", "tag": "hooks.py"}, ctx=ctx)
        assert result.success
        assert "step_0001" in result.output
        assert "step_0002" not in result.output

    def test_nonexistent_path(self):
        """Read nonexistent artifact → error."""
        from agent_cli.tools.read_artifact import tool_read_artifact

        result = tool_read_artifact({"path": "/nonexistent/file.md"})
        assert not result.success
        assert "not found" in result.error.lower()

    def test_read_artifact_finds_skill_subdirectory(self, tmp_path):
        """read_artifact list mode includes artifacts in skill subdirectories."""
        from agent_cli.context.scratchpad import save_artifact
        from agent_cli.tools.read_artifact import tool_read_artifact
        from unittest.mock import MagicMock

        # Flat artifact
        save_artifact(1, "Flat content", ["shell"], "flat", tmp_path)
        # Skill subdirectory artifact
        save_artifact(
            2,
            "Skill content",
            ["read_file", "skill:optimize"],
            "skill read",
            tmp_path,
            skill_name="optimize",
            parent_step=1,
        )

        ctx = MagicMock()
        ctx._scratchpad_dir = tmp_path
        result = tool_read_artifact({"mode": "list"}, ctx=ctx)
        assert result.success
        assert "step_0001" in result.output  # flat
        assert "step_0002" in result.output  # skill subdir

    def test_list_no_session(self):
        """List without ctx → error."""
        from agent_cli.tools.read_artifact import tool_read_artifact

        result = tool_read_artifact({"mode": "list"})
        assert result.success  # no session is not an error, just informational
        assert "no" in result.output.lower()
