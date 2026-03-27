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


class TestWriteFile:
    def test_creates_file(self, tmp_path):
        target = tmp_path / "new.txt"
        result = tool_write_file({"path": str(target), "content": "hello"})
        assert target.exists()
        assert target.read_text() == "hello"
        assert "File saved" in result

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "sub" / "dir" / "file.txt"
        tool_write_file({"path": str(target), "content": "nested"})
        assert target.exists()

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "exist.txt"
        target.write_text("old")
        tool_write_file({"path": str(target), "content": "new"})
        assert target.read_text() == "new"

    def test_error_on_invalid_path(self):
        with pytest.raises(RuntimeError):
            tool_write_file({"path": "/nonexistent/dir/\x00/file.txt", "content": "x"})


class TestShellTool:
    def test_basic_command(self):
        result = tool_shell({"command": "echo hello"})
        assert "hello" in result

    def test_stderr_output(self):
        result = tool_shell({"command": "echo err >&2"})
        assert "stderr" in result

    def test_nonzero_exit(self):
        result = tool_shell({"command": "exit 1"})
        assert "exit code: 1" in result

    def test_timeout(self):
        with pytest.raises(RuntimeError, match="timed out"):
            tool_shell({"command": "sleep 10", "timeout": 1})

    def test_empty_command(self):
        with pytest.raises(RuntimeError, match="Empty command"):
            tool_shell({"command": ""})

    def test_no_output(self):
        result = tool_shell({"command": "true"})
        assert result == "(no output)"


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
        assert "Edit complete" in result
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
        with pytest.raises(RuntimeError, match="No edits"):
            tool_edit_file({"path": str(f), "edits": []})

    def test_unknown_op_error(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello\n")
        with pytest.raises(RuntimeError, match="Unknown edit op"):
            tool_edit_file(
                {
                    "path": str(f),
                    "edits": [{"op": "invalid", "pos": "1#ZZ"}],
                }
            )

    def test_file_not_found(self):
        with pytest.raises(RuntimeError, match="cannot read"):
            tool_edit_file(
                {
                    "path": "/nonexistent/file.py",
                    "edits": [{"op": "replace", "pos": "1#ZZ", "lines": []}],
                }
            )

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
        assert "Edit complete" in result
        assert "new_line" in f.read_text()

    def test_all_non_dict_edits_error(self, tmp_path):
        """If all edits are non-dict, should raise error."""
        f = tmp_path / "test.py"
        f.write_text("hello\n")
        with pytest.raises(RuntimeError, match="No valid edit"):
            tool_edit_file({"path": str(f), "edits": [1, 2, "bad"]})


class TestDelegate:
    def test_validate_short_task(self):
        err = _validate_subtask("Do it")
        assert err is not None
        assert "too short" in err

    def test_validate_vague_refs(self):
        err = _validate_subtask("Analyze this file and fix the bug in it")
        assert err is not None
        assert "vague" in err

    def test_validate_good_task(self):
        err = _validate_subtask("Read /tmp/data.csv and count the number of rows in it")
        # "it" is vague but the task is long enough
        assert err is not None  # still has "it"

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

        with pytest.raises(RuntimeError, match="Delegation rejected"):
            tool_delegate(
                args={"task": "Fix it"},
                provider="ollama",
                model="test",
                base_url="http://localhost:11434",
                api_key="",
            )

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
        assert "STATUS: success" in result
        assert "Task completed" in result

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
        assert "STATUS: error" in result
        assert "Error occurred" in result

    def test_timeout(self, mock_subprocess):
        import subprocess as sp
        from agent_cli.tools.delegate import tool_delegate

        mock_subprocess.side_effect = sp.TimeoutExpired(cmd="test", timeout=5)

        with pytest.raises(RuntimeError, match="timed out"):
            tool_delegate(
                args={
                    "task": "Read /tmp/data.csv and count the number of rows then report"
                },
                provider="ollama",
                model="test-model",
                base_url="http://localhost:11434",
                api_key="",
                timeout=5,
            )

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
        assert "(no output)" in result


class TestToolsRegistry:
    """Tests for unified TOOLS dict with virtual tools."""

    def test_tools_contains_all_real_tools(self):
        real_tools = {"read_file", "write_file", "edit_file", "shell", "read_context"}
        assert real_tools.issubset(set(TOOLS.keys()))

    def test_tools_contains_virtual_tools(self):
        assert "complete" in TOOLS
        assert "ask" in TOOLS

    def test_virtual_tools_frozenset(self):
        assert VIRTUAL_TOOLS == frozenset({"complete", "ask"})
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
        assert fn({"result": "done"}) == "done"

    def test_complete_lambda_default(self):
        fn = TOOLS["complete"]
        assert fn({}) == "(completed)"

    def test_ask_lambda_with_question(self):
        fn = TOOLS["ask"]
        assert fn({"question": "what?"}) == "what?"

    def test_ask_lambda_default(self):
        fn = TOOLS["ask"]
        assert fn({}) == "(ask)"


class TestExecuteTool:
    def test_unknown_tool(self):
        with pytest.raises(RuntimeError, match="Unknown tool"):
            execute_tool("nonexistent_tool", {})

    def test_execute_virtual_complete(self):
        result = execute_tool("complete", {"result": "all done"})
        assert result == "all done"

    def test_execute_virtual_ask(self):
        result = execute_tool("ask", {"question": "which file?"})
        assert result == "which file?"

    def test_error_message_includes_virtual_tools(self):
        try:
            execute_tool("bogus", {})
        except RuntimeError as e:
            msg = str(e)
            assert "complete" in msg
            assert "ask" in msg
            assert "read_file" in msg


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


class TestPlanParserJsonExtraction:
    def test_json_steps_array(self):
        from agent_cli.parsing.plan_parser import parse_plan_steps

        text = '{"steps": ["Read file", "Analyze", "Write summary"]}'
        steps = parse_plan_steps(text)
        assert len(steps) == 3
        assert steps[0].description == "Read file"

    def test_json_plan_key(self):
        from agent_cli.parsing.plan_parser import parse_plan_steps

        text = '{"plan": ["Step 1", "Step 2"]}'
        steps = parse_plan_steps(text)
        assert len(steps) == 2

    def test_json_dict_items(self):
        from agent_cli.parsing.plan_parser import parse_plan_steps

        text = '{"steps": [{"description": "Read file"}, {"task": "Analyze"}]}'
        steps = parse_plan_steps(text)
        assert len(steps) == 2
        assert steps[0].description == "Read file"
        assert steps[1].description == "Analyze"

    def test_json_plan_string(self):
        from agent_cli.parsing.plan_parser import parse_plan_steps

        text = '{"plan": "1. Read file\\n2. Analyze it"}'
        steps = parse_plan_steps(text)
        assert len(steps) == 2


class TestReadFilePartial:
    def test_full_read(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\nline4\nline5")
        result = tool_read_file({"path": str(f)})
        assert "1#" in result
        assert "5#" in result

    def test_line_start(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\nccc\nddd\neee")
        result = tool_read_file({"path": str(f), "line_start": 3})
        assert "ccc" in result
        assert "ddd" in result
        assert "eee" in result
        assert "aaa" not in result

    def test_line_start_and_end(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\nccc\nddd\neee")
        result = tool_read_file({"path": str(f), "line_start": 2, "line_end": 4})
        assert "bbb" in result
        assert "ddd" in result
        assert "aaa" not in result
        assert "eee" not in result

    def test_line_numbers_preserved(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\nccc\nddd\neee")
        result = tool_read_file({"path": str(f), "line_start": 3})
        # First line in result should be line 3, not line 1
        assert result.startswith("3#")

    def test_string_line_start_coerced(self, tmp_path):
        """LLMs sometimes send line_start as string."""
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\nccc")
        result = tool_read_file({"path": str(f), "line_start": "2"})
        assert "bbb" in result
        assert "aaa" not in result


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
        assert "No previous sessions" in result

    def test_list_with_sessions(self, tmp_path, monkeypatch):
        import agent_cli.context.session as session_mod

        monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path)
        from agent_cli.context.session import create_session, save_meta, save_summary
        from agent_cli.tools.context import tool_read_context

        meta = create_session("/tmp/ws")
        save_meta(meta)
        save_summary(meta, "Test summary content")

        result = tool_read_context({"mode": "list"})
        assert meta.session_id in result
        assert "Test summary" in result

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
        assert "shell" in result
        assert "ok" in result

    def test_detail_missing_session_id(self, tmp_path, monkeypatch):
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "detail"})
        assert "session_id is required" in result

    def test_detail_nonexistent_session(self, tmp_path, monkeypatch):
        import agent_cli.context.session as session_mod

        monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path)
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "detail", "session_id": "999"})
        assert "not found" in result

    def test_unknown_mode(self, tmp_path, monkeypatch):
        from agent_cli.tools.context import tool_read_context

        result = tool_read_context({"mode": "invalid"})
        assert "unknown mode" in result.lower()
