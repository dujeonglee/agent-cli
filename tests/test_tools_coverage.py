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
from agent_cli.tools.delegate import _validate_subtask, _build_subprocess_cmd
from agent_cli.tools import TOOLS, VIRTUAL_TOOLS, execute_tool
from agent_cli.tools.truncation import truncate_output, TruncationConfig


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


class TestDelegate:
    def test_validate_short_task(self):
        err = _validate_subtask("Do it")
        assert err is not None
        assert "too short" in err

    def test_validate_long_task_passes(self):
        err = _validate_subtask("Analyze this file and fix the bug in it")
        assert err is None  # length >= 5 words, no vague check

    def test_validate_with_pronouns_passes(self):
        err = _validate_subtask("Read /tmp/data.csv and count the number of rows in it")
        assert err is None  # pronouns OK, subagent handles context naturally

    def test_validate_explicit_task(self):
        err = _validate_subtask(
            "Read /tmp/data.csv and count the number of rows, then save count to /tmp/result.txt"
        )
        assert err is None

    def test_build_cmd_package_mode(self, monkeypatch):
        import sys

        monkeypatch.setattr(sys, "argv", ["agent-cli"])
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        cmd = _build_subprocess_cmd(["run", "test task"])
        assert "-m" in cmd

    def test_build_cmd_wrapper_mode(self, monkeypatch):
        import sys

        monkeypatch.setattr(sys, "argv", ["agent-cli.py"])
        cmd = _build_subprocess_cmd(["run", "test task"])
        assert "agent-cli.py" in cmd

    def test_build_cmd_frozen(self, monkeypatch):
        import sys

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        cmd = _build_subprocess_cmd(["run", "test"])
        assert cmd[0] == sys.executable


class TestToolDelegate:
    def test_rejects_vague_task(self):
        from agent_cli.tools.delegate import tool_delegate

        result = tool_delegate(
            args={"task": "Fix it"},
            provider="ollama",
            model="test",
            base_url="http://localhost:11434",
            api_key="",
        )
        assert not result.success
        assert "Delegation rejected" in result.error

    @pytest.fixture()
    def mock_subprocess(self, monkeypatch):
        from unittest.mock import MagicMock

        mock_run = MagicMock()
        monkeypatch.setattr("agent_cli.tools.delegate.subprocess.run", mock_run)
        return mock_run

    def test_success(self, mock_subprocess):
        from agent_cli.tools.delegate import tool_delegate

        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = "Task completed successfully"
        mock_subprocess.return_value.stderr = ""

        result = tool_delegate(
            args={
                "task": "Read /tmp/data.csv and count the number of rows then report"
            },
            provider="ollama",
            model="test-model",
            base_url="http://localhost:11434",
            api_key="",
        )
        assert result.success
        assert "STATUS: success" in result.output
        assert "Task completed" in result.output

    def test_failure(self, mock_subprocess):
        from agent_cli.tools.delegate import tool_delegate

        mock_subprocess.return_value.returncode = 1
        mock_subprocess.return_value.stdout = ""
        mock_subprocess.return_value.stderr = "Error occurred"

        result = tool_delegate(
            args={
                "task": "Read /tmp/data.csv and count the number of rows then report"
            },
            provider="ollama",
            model="test-model",
            base_url="http://localhost:11434",
            api_key="",
        )
        assert not result.success
        assert "STATUS: error" in result.error
        assert "Error occurred" in result.error

    def test_timeout(self, mock_subprocess):
        import subprocess as sp
        from agent_cli.tools.delegate import tool_delegate

        mock_subprocess.side_effect = sp.TimeoutExpired(cmd="test", timeout=5)

        result = tool_delegate(
            args={
                "task": "Read /tmp/data.csv and count the number of rows then report"
            },
            provider="ollama",
            model="test-model",
            base_url="http://localhost:11434",
            api_key="",
            timeout=5,
        )
        assert not result.success
        assert "timed out" in result.error

    def test_api_key_appended(self, mock_subprocess):
        from agent_cli.tools.delegate import tool_delegate

        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = "done"
        mock_subprocess.return_value.stderr = ""

        tool_delegate(
            args={
                "task": "Read /tmp/data.csv and count the number of rows then report"
            },
            provider="openai",
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="sk-test-key",
        )
        cmd = mock_subprocess.call_args[0][0]
        assert "--api-key" in cmd
        assert "sk-test-key" in cmd

    def test_no_output(self, mock_subprocess):
        from agent_cli.tools.delegate import tool_delegate

        mock_subprocess.return_value.returncode = 1
        mock_subprocess.return_value.stdout = ""
        mock_subprocess.return_value.stderr = ""

        result = tool_delegate(
            args={
                "task": "Read /tmp/data.csv and count the number of rows then report"
            },
            provider="ollama",
            model="test",
            base_url="http://localhost:11434",
            api_key="",
        )
        assert not result.success
        assert "(no output)" in result.error

    def test_headless_flag_in_cmd(self, mock_subprocess):
        """Delegate passes --headless to subprocess."""
        from agent_cli.tools.delegate import tool_delegate

        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = "done"
        mock_subprocess.return_value.stderr = ""

        tool_delegate(
            args={
                "task": "Read /tmp/data.csv and count the number of rows then report"
            },
            provider="ollama",
            model="test",
            base_url="http://localhost:11434",
            api_key="",
        )
        cmd = mock_subprocess.call_args[0][0]
        assert "--headless" in cmd

    def test_no_quiet_flag_in_cmd(self, mock_subprocess):
        """Delegate no longer passes --quiet (merged into --headless)."""
        from agent_cli.tools.delegate import tool_delegate

        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = "done"
        mock_subprocess.return_value.stderr = ""

        tool_delegate(
            args={
                "task": "Read /tmp/data.csv and count the number of rows then report"
            },
            provider="ollama",
            model="test",
            base_url="http://localhost:11434",
            api_key="",
        )
        cmd = mock_subprocess.call_args[0][0]
        assert "--quiet" not in cmd


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
            {"complete", "ask", "run_skill", "read_artifact"}
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
        assert result.output == "(completed)"

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


class TestTruncationByteLimit:
    def test_byte_limit_head(self):
        text = "a" * 100 + "\n" + "b" * 100
        config = TruncationConfig(max_lines=1000, max_bytes=50, direction="head")
        result = truncate_output(text, config)
        assert len(result.encode("utf-8")) < 200
        assert "truncated" in result

    def test_byte_limit_tail(self):
        text = "a" * 100 + "\n" + "b" * 100
        config = TruncationConfig(max_lines=1000, max_bytes=50, direction="tail")
        result = truncate_output(text, config)
        assert "truncated" in result

    def test_utf8_safe(self):
        text = "한글테스트\n" * 100
        config = TruncationConfig(max_lines=5, max_bytes=500)
        result = truncate_output(text, config)
        # Should not crash on UTF-8 boundaries
        assert isinstance(result, str)


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


class TestTruncationReadFileGuide:
    def test_head_truncation_includes_read_guide(self):
        text = "\n".join(f"line{i}" for i in range(100))
        config = TruncationConfig(
            max_lines=10, max_bytes=100_000, direction="head", tool_name="read_file"
        )
        result = truncate_output(text, config)
        assert "line_start=" in result

    def test_non_read_file_no_guide(self):
        text = "\n".join(f"line{i}" for i in range(100))
        config = TruncationConfig(
            max_lines=10, max_bytes=100_000, direction="head", tool_name="shell"
        )
        result = truncate_output(text, config)
        assert "line_start=" not in result


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
                max_iter=3,
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
        with patch("agent_cli.skills.loader.load_skills", return_value=mock_skills):
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
            turn=1,
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
        assert "turn_0001" in result.output
        assert "turn_0002" in result.output
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
        assert "turn_0001" in result.output
        assert "turn_0002" not in result.output

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
            parent_turn=1,
        )

        ctx = MagicMock()
        ctx._scratchpad_dir = tmp_path
        result = tool_read_artifact({"mode": "list"}, ctx=ctx)
        assert result.success
        assert "turn_0001" in result.output  # flat
        assert "turn_0002" in result.output  # skill subdir

    def test_list_no_session(self):
        """List without ctx → error."""
        from agent_cli.tools.read_artifact import tool_read_artifact

        result = tool_read_artifact({"mode": "list"})
        assert result.success  # no session is not an error, just informational
        assert "no" in result.output.lower()
