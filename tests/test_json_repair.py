"""Tests for the ReAct plugin's stage-2 JSON repair helper.

Lives at ``agent_cli/wire_formats/react.py:repair_json`` since it is
part of ReActFormat's 3-stage fallback. Other plugins may import it
explicitly, but the default expectation is that each format owns its
own recovery strategy."""

from agent_cli.wire_formats.react import repair_json


class TestRepairJsonPassthrough:
    def test_valid_json(self):
        result, truncated = repair_json('{"thought": "hello"}')
        assert result == {"thought": "hello"}
        assert truncated is False

    def test_valid_nested(self):
        result, truncated = repair_json(
            '{"action": "shell", "action_input": {"command": "ls"}}'
        )
        assert result == {"action": "shell", "action_input": {"command": "ls"}}
        assert truncated is False


class TestRepairJsonFixes:
    def test_unclosed_string(self):
        result, truncated = repair_json('{"thought": "hello')
        assert result is not None
        assert result["thought"] == "hello"
        assert truncated is True

    def test_missing_closing_brace(self):
        result, truncated = repair_json('{"thought": "hello"')
        assert result is not None
        assert result["thought"] == "hello"
        assert truncated is True

    def test_trailing_comma(self):
        result, truncated = repair_json('{"thought": "hello",}')
        assert result == {"thought": "hello"}
        assert truncated is False

    def test_single_quotes(self):
        result, truncated = repair_json("{'thought': 'hello'}")
        assert result == {"thought": "hello"}
        assert truncated is False

    def test_unquoted_keys(self):
        result, truncated = repair_json('{thought: "hello"}')
        assert result == {"thought": "hello"}
        assert truncated is False

    def test_unclosed_string_and_missing_brace(self):
        result, truncated = repair_json('{"thought": "hello", "action": "shell')
        assert result is not None
        assert result.get("thought") == "hello"
        assert truncated is True


class TestRepairJsonExtraction:
    def test_markdown_fences(self):
        result, truncated = repair_json('```json\n{"thought": "hi"}\n```')
        assert result == {"thought": "hi"}
        assert truncated is False

    def test_surrounding_text(self):
        result, truncated = repair_json(
            'Here is my response: {"thought": "hi"} hope it helps'
        )
        assert result == {"thought": "hi"}
        assert truncated is False

    def test_text_before_json(self):
        result, truncated = repair_json(
            'I will help you.\n{"thought": "reasoning", "action": "shell"}'
        )
        assert result is not None
        assert result["action"] == "shell"
        assert truncated is False


class TestRepairJsonFailure:
    def test_no_json_at_all(self):
        result, truncated = repair_json("This is just plain text")
        assert result is None

    def test_empty_string(self):
        result, truncated = repair_json("")
        assert result is None

    def test_array_not_dict(self):
        result, truncated = repair_json("[1, 2, 3]")
        assert result is None


class TestTruncationDetection:
    def test_truncated_edit_lines(self):
        """Truncated edit_file with incomplete lines array."""
        text = '{"thought": "editing", "action": "edit_file", "action_input": {"path": "f.py", "edits": [{"op": "replace", "pos": "1#HN", "lines": ["line1", "line2", "incompl'
        result, truncated = repair_json(text)
        assert result is not None
        assert truncated is True
        assert result["action"] == "edit_file"
        # The last line should be the truncated "incompl" (closed by repair)
        lines = result["action_input"]["edits"][0]["lines"]
        assert len(lines) >= 2
        assert lines[0] == "line1"

    def test_complete_json_not_truncated(self):
        """Complete JSON should not be flagged as truncated."""
        text = '{"thought": "ok", "action": "edit_file", "action_input": {"path": "f.py", "edits": [{"op": "replace", "pos": "1#HN", "lines": ["line1"]}]}}'
        result, truncated = repair_json(text)
        assert result is not None
        assert truncated is False


class TestSanitizeTruncatedEdit:
    """edit_file is flat-native (Step 3): one op = one edit, so truncation
    handling drops the last (incomplete) element of the op's ``lines``."""

    def test_drops_last_line(self):
        from agent_cli.loop import _sanitize_truncated_edit

        tool_input = {
            "path": "f.py",
            "op": "replace",
            "pos": "1#HN",
            "lines": ["good1", "good2", "trunc"],
        }
        sanitized, warning = _sanitize_truncated_edit(tool_input)
        assert sanitized["lines"] == ["good1", "good2"]
        assert "truncated" in warning.lower()

    def test_no_lines_is_noop(self):
        # delete carries no `lines` — nothing to strip, no warning.
        from agent_cli.loop import _sanitize_truncated_edit

        tool_input = {"path": "f.py", "op": "delete", "pos": "1#HN"}
        sanitized, warning = _sanitize_truncated_edit(tool_input)
        assert sanitized == tool_input
        assert warning == ""

    def test_single_line_drops_to_empty(self):
        from agent_cli.loop import _sanitize_truncated_edit

        tool_input = {
            "path": "f.py",
            "op": "replace",
            "pos": "1#HN",
            "lines": ["trunc"],
        }
        sanitized, warning = _sanitize_truncated_edit(tool_input)
        assert sanitized["lines"] == []
        assert "truncated" in warning.lower()
