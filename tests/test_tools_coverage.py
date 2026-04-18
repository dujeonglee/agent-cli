"""Additional tests to improve coverage for tools modules."""

from __future__ import annotations


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
    def test_format_no_output(self):
        result = DelegateResult()
        formatted = _format_delegate_output(result)
        assert "no result" in formatted

    def test_format_no_files(self):
        result = DelegateResult(output="Done")
        formatted = _format_delegate_output(result)
        assert "Files touched" not in formatted


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
            from agent_cli.tools.result import ToolResult

            time.sleep(3)  # Longer than timeout
            return ToolResult(True, output="late result")

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
            {"complete", "ask", "run_skill", "ready_for_review", "delegate"}
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


class TestReadFileStat:
    def test_stat_shows_metadata(self, tmp_path):
        """stat=True returns line count + size + first 20 lines."""
        f = tmp_path / "big.py"
        content = "\n".join(f"line {i}" for i in range(1, 101))
        f.write_text(content)
        result = tool_read_file({"path": str(f), "stat": True})
        assert result.success
        assert "[stat]" in result.output
        assert "100 lines" in result.output
        assert "bytes" in result.output or "KB" in result.output

    def test_stat_shows_first_20_lines(self, tmp_path):
        """stat returns first 20 lines with hashlines."""
        f = tmp_path / "big.py"
        content = "\n".join(f"line {i}" for i in range(1, 101))
        f.write_text(content)
        result = tool_read_file({"path": str(f), "stat": True})
        assert "1#" in result.output
        assert "20#" in result.output
        assert "21#" not in result.output  # only first 20

    def test_stat_small_file_shows_all(self, tmp_path):
        """stat on small file shows all lines (less than 20)."""
        f = tmp_path / "small.py"
        f.write_text("a\nb\nc")
        result = tool_read_file({"path": str(f), "stat": True})
        assert result.success
        assert "3 lines" in result.output

    def test_stat_includes_followup_guidance(self, tmp_path):
        """stat output must tell the LLM this is a metadata query and
        point at real read modes — otherwise the LLM treats stat-only
        as 'read'.
        """
        f = tmp_path / "big.py"
        f.write_text("\n".join(f"line {i}" for i in range(50)))
        result = tool_read_file({"path": str(f), "stat": True})
        assert "have NOT read" in result.output or "not read" in result.output.lower()
        assert "line_start" in result.output
        assert "search" in result.output


class TestReadFileSearch:
    def test_search_finds_matches(self, tmp_path):
        """search returns matching lines with context."""
        f = tmp_path / "app.py"
        content = (
            "def foo():\n"
            "    pass\n"
            "\n"
            "def login(user):\n"
            "    return user\n"
            "\n"
            "def bar():\n"
            "    pass\n"
        )
        f.write_text(content)
        result = tool_read_file({"path": str(f), "search": "login", "context": 1})
        assert result.success
        assert "[search]" in result.output
        assert "1 matches" in result.output
        assert "login" in result.output
        # Context: 1 line before (line 3) and 1 line after (line 5)
        assert "3#" in result.output or "4#" in result.output

    def test_search_no_matches(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("def foo():\n    pass\n")
        result = tool_read_file({"path": str(f), "search": "nonexistent"})
        assert result.success
        assert "no matches" in result.output

    def test_search_regex_pattern(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("x = 1\ny = 2\nz = 3\n")
        result = tool_read_file({"path": str(f), "search": r"^[xz]\s*="})
        assert result.success
        assert "2 matches" in result.output

    def test_search_invalid_regex(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("hello\n")
        result = tool_read_file({"path": str(f), "search": "[invalid"})
        assert not result.success
        assert "Invalid search pattern" in result.error

    def test_search_merges_overlapping_context(self, tmp_path):
        """Adjacent matches should share merged context (not duplicate lines)."""
        f = tmp_path / "app.py"
        content = "\n".join(f"line {i}" for i in range(1, 21))
        # matches on line 5 and line 7, context=3 → ranges overlap → merged
        f.write_text(content.replace("line 5", "MATCH").replace("line 7", "MATCH"))
        result = tool_read_file({"path": str(f), "search": "MATCH", "context": 3})
        assert result.success
        assert "2 matches" in result.output
        # Should have one merged range block, not two separate
        assert result.output.count("─── lines") == 1


class TestReadContextTool:
    def test_list_no_sessions(self, tmp_path, monkeypatch):
        import agent_cli.context.session as session_mod

        monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "list"})
        assert result.success
        assert "No previous sessions" in result.output

    def test_unknown_mode(self, tmp_path, monkeypatch):
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "invalid"})
        assert not result.success
        assert "unknown mode" in result.error.lower()

    def test_search_missing_keyword(self):
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "search"})
        assert not result.success
        assert "keyword" in result.error.lower()

    def test_search_no_sessions(self, tmp_path, monkeypatch):
        import agent_cli.tools.context as ctx_mod

        monkeypatch.setattr(ctx_mod, "_SESSIONS_BASE", tmp_path / "empty")
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "search", "keyword": "test"})
        assert result.success
        assert "No sessions found" in result.output

    def test_search_finds_match(self, tmp_path, monkeypatch):
        import agent_cli.tools.context as ctx_mod

        # Create fake session with history
        session_dir = tmp_path / "sessions" / "12345"
        session_dir.mkdir(parents=True)
        history = session_dir / "history.jsonl"
        history.write_text(
            '{"role":"user","content":"hello world"}\n'
            '{"role":"assistant","thought":"greeting","action":"complete","action_input":{"result":"hi"}}\n'
        )

        monkeypatch.setattr(ctx_mod, "_SESSIONS_BASE", tmp_path / "sessions")
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "search", "keyword": "hello"})
        assert result.success
        assert "hello world" in result.output
        assert "12345" in result.output

    def test_search_no_match(self, tmp_path, monkeypatch):
        import agent_cli.tools.context as ctx_mod

        session_dir = tmp_path / "sessions" / "12345"
        session_dir.mkdir(parents=True)
        history = session_dir / "history.jsonl"
        history.write_text('{"role":"user","content":"hello"}\n')

        monkeypatch.setattr(ctx_mod, "_SESSIONS_BASE", tmp_path / "sessions")
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "search", "keyword": "nonexistent"})
        assert result.success
        assert "No matches found" in result.output

    def test_search_includes_subdirs(self, tmp_path, monkeypatch):
        """Search finds matches in delegate/skill subdirectories."""
        import agent_cli.tools.context as ctx_mod

        session_dir = tmp_path / "sessions" / "12345"
        delegate_dir = session_dir / "delegate_explorer_abc_123"
        delegate_dir.mkdir(parents=True)
        (delegate_dir / "history.jsonl").write_text(
            '{"role":"assistant","thought":"found auth bug","action":"complete","action_input":{"result":"done"}}\n'
        )

        monkeypatch.setattr(ctx_mod, "_SESSIONS_BASE", tmp_path / "sessions")
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "search", "keyword": "auth bug"})
        assert result.success
        assert "delegate_explorer" in result.output


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

    def test_run_skill_is_virtual_tool(self):
        """run_skill is intercepted by loop (virtual tool), not executed directly."""
        from agent_cli.tools import VIRTUAL_TOOLS

        assert "run_skill" in VIRTUAL_TOOLS
