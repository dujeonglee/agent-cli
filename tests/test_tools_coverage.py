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
)
from agent_cli.tools.delegate import _validate_subtask, _build_subprocess_cmd
from agent_cli.tools import execute_tool
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
        assert "-m" in cmd or "agent-cli" in str(cmd)


class TestExecuteTool:
    def test_unknown_tool(self):
        with pytest.raises(RuntimeError, match="Unknown tool"):
            execute_tool("nonexistent_tool", {})


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
