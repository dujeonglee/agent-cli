"""Tests for agent_cli.parsing.json_repair."""

from agent_cli.parsing.json_repair import repair_json


class TestRepairJsonPassthrough:
    def test_valid_json(self):
        assert repair_json('{"thought": "hello"}') == {"thought": "hello"}

    def test_valid_nested(self):
        result = repair_json('{"action": "shell", "action_input": {"command": "ls"}}')
        assert result == {"action": "shell", "action_input": {"command": "ls"}}


class TestRepairJsonFixes:
    def test_unclosed_string(self):
        result = repair_json('{"thought": "hello')
        assert result is not None
        assert result["thought"] == "hello"

    def test_missing_closing_brace(self):
        result = repair_json('{"thought": "hello"')
        assert result is not None
        assert result["thought"] == "hello"

    def test_trailing_comma(self):
        result = repair_json('{"thought": "hello",}')
        assert result == {"thought": "hello"}

    def test_single_quotes(self):
        result = repair_json("{'thought': 'hello'}")
        assert result == {"thought": "hello"}

    def test_unquoted_keys(self):
        result = repair_json('{thought: "hello"}')
        assert result == {"thought": "hello"}

    def test_unclosed_string_and_missing_brace(self):
        result = repair_json('{"thought": "hello", "action": "shell')
        assert result is not None
        assert result.get("thought") == "hello"


class TestRepairJsonExtraction:
    def test_markdown_fences(self):
        text = '```json\n{"thought": "hi"}\n```'
        assert repair_json(text) == {"thought": "hi"}

    def test_surrounding_text(self):
        text = 'Here is my response: {"thought": "hi"} hope it helps'
        assert repair_json(text) == {"thought": "hi"}

    def test_text_before_json(self):
        text = 'I will help you.\n{"thought": "reasoning", "action": "shell"}'
        result = repair_json(text)
        assert result is not None
        assert result["action"] == "shell"


class TestRepairJsonFailure:
    def test_no_json_at_all(self):
        assert repair_json("This is just plain text") is None

    def test_empty_string(self):
        assert repair_json("") is None

    def test_array_not_dict(self):
        assert repair_json("[1, 2, 3]") is None
