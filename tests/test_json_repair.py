"""Tests for agent_cli.parsing.json_repair."""

from agent_cli.parsing.json_repair import repair_json


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
    def test_drops_last_line(self):
        """Last line of last edit is dropped when truncated."""
        from agent_cli.loop import _sanitize_truncated_edit

        tool_input = {
            "path": "f.py",
            "edits": [
                {"op": "replace", "pos": "1#HN", "lines": ["good1", "good2", "trunc"]},
            ],
        }
        sanitized, warning = _sanitize_truncated_edit(tool_input)
        assert sanitized["edits"][0]["lines"] == ["good1", "good2"]
        assert "truncated" in warning.lower()

    def test_drops_empty_edit(self):
        """If dropping last line leaves edit with no lines, drop the edit."""
        from agent_cli.loop import _sanitize_truncated_edit

        tool_input = {
            "path": "f.py",
            "edits": [
                {"op": "replace", "pos": "1#HN", "lines": ["good"]},
                {"op": "replace", "pos": "5#KV", "lines": ["truncated"]},
            ],
        }
        sanitized, warning = _sanitize_truncated_edit(tool_input)
        assert len(sanitized["edits"]) == 1
        assert "1 of 2" in warning

    def test_reports_applied_count(self):
        """Warning reports how many edits were applied."""
        from agent_cli.loop import _sanitize_truncated_edit

        tool_input = {
            "path": "f.py",
            "edits": [
                {"op": "replace", "pos": "1#HN", "lines": ["a", "b"]},
                {"op": "replace", "pos": "5#KV", "lines": ["c", "trunc"]},
            ],
        }
        sanitized, warning = _sanitize_truncated_edit(tool_input)
        assert len(sanitized["edits"]) == 2
        assert sanitized["edits"][1]["lines"] == ["c"]
        assert "2 of 2" in warning
