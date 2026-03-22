"""Tests for agent_cli.parsing.react_parser."""

from agent_cli.parsing.react_parser import parse_react


class TestStage1DirectParse:
    def test_clean_json_action(self):
        text = '{"thought": "I need to read", "action": "read_file", "action_input": {"path": "a.py"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.thought == "I need to read"
        assert result.action == "read_file"
        assert result.action_input == {"path": "a.py"}
        assert result.final_answer is None

    def test_clean_json_final(self):
        text = '{"thought": "done", "final_answer": "The result is 42"}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.final_answer == "The result is 42"
        assert result.action is None

    def test_markdown_fences(self):
        text = '```json\n{"thought": "hi", "final_answer": "done"}\n```'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.thought == "hi"

    def test_json_with_surrounding_text(self):
        text = 'Sure! {"thought": "ok", "action": "shell", "action_input": {"command": "ls"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.action == "shell"

    def test_final_key_compat(self):
        """Accept 'final' as alias for 'final_answer'."""
        text = '{"thought": "done", "final": "answer here"}'
        result = parse_react(text)
        assert result.final_answer == "answer here"


class TestStage2JsonRepair:
    def test_trailing_comma(self):
        text = '{"thought": "hi", "action": "shell",}'
        result = parse_react(text)
        assert result.parse_stage == 2
        assert result.action == "shell"

    def test_missing_brace(self):
        text = '{"thought": "hello", "final_answer": "done"'
        result = parse_react(text)
        assert result.parse_stage == 2
        assert result.final_answer == "done"

    def test_single_quotes(self):
        text = "{'thought': 'reasoning', 'action': 'read_file'}"
        result = parse_react(text)
        assert result.parse_stage == 2
        assert result.action == "read_file"


class TestStage3Regex:
    def test_extremely_broken_json(self):
        text = 'blah "thought": "I am thinking", blah "action": "shell"'
        result = parse_react(text)
        assert result.parse_stage == 3
        assert result.thought == "I am thinking"
        assert result.action == "shell"

    def test_partial_fields(self):
        text = 'some garbage "final_answer": "the answer is 42" more garbage'
        result = parse_react(text)
        assert result.parse_stage == 3
        assert result.final_answer == "the answer is 42"


class TestStage0Failure:
    def test_no_recognizable_content(self):
        result = parse_react("Hello, how are you?")
        assert result.parse_stage == 0
        assert result.thought is None
        assert result.action is None
        assert result.final_answer is None
        assert result.raw == "Hello, how are you?"

    def test_empty_string(self):
        result = parse_react("")
        assert result.parse_stage == 0


class TestActionInputTypes:
    def test_dict_action_input(self):
        text = (
            '{"thought": "t", "action": "read_file", "action_input": {"path": "f.py"}}'
        )
        result = parse_react(text)
        assert isinstance(result.action_input, dict)

    def test_string_action_input(self):
        text = '{"thought": "t", "action": "shell", "action_input": "ls -la"}'
        result = parse_react(text)
        assert result.action_input == "ls -la"


class TestUnicodeSanitization:
    def test_surrogate_removed(self):
        # Surrogate \ud800 embedded in JSON — should be stripped and parsed
        text = '{"thought": "hello\ud800world", "final_answer": "done"}'
        result = parse_react(text)
        assert result.final_answer == "done"
        assert result.parse_stage >= 1

    def test_normal_text_unchanged(self):
        text = '{"thought": "normal text", "final_answer": "ok"}'
        result = parse_react(text)
        assert result.thought == "normal text"
        assert result.final_answer == "ok"


class TestThinkingBlockStripping:
    def test_think_tags_stripped(self):
        text = '<think>\nI need to read the file.\n</think>\n{"thought": "t", "action": "read_file", "action_input": {"path": "a.py"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.thinking == "I need to read the file."
        assert result.action == "read_file"

    def test_thinking_tags_stripped(self):
        text = '<thinking>step by step reasoning</thinking>\n{"thought": "ok", "final_answer": "42"}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.thinking == "step by step reasoning"
        assert result.final_answer == "42"

    def test_reasoning_tags_stripped(self):
        text = '<reasoning>analyzing the problem</reasoning>\n{"thought": "ok", "final_answer": "done"}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.thinking == "analyzing the problem"

    def test_reflection_tags_stripped(self):
        text = '<reflection>let me reconsider</reflection>\n{"thought": "t", "final_answer": "ok"}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.thinking == "let me reconsider"

    def test_no_thinking_tags(self):
        text = '{"thought": "t", "final_answer": "done"}'
        result = parse_react(text)
        assert result.thinking is None
        assert result.parse_stage == 1

    def test_multiple_think_blocks(self):
        text = '<think>first thought</think>\n<think>second thought</think>\n{"thought": "t", "final_answer": "ok"}'
        result = parse_react(text)
        assert result.thinking is not None
        assert "first thought" in result.thinking
        assert "second thought" in result.thinking
        assert result.parse_stage == 1

    def test_think_tags_case_insensitive(self):
        text = (
            '<THINK>uppercase reasoning</THINK>\n{"thought": "t", "final_answer": "ok"}'
        )
        result = parse_react(text)
        assert result.thinking == "uppercase reasoning"
        assert result.parse_stage == 1

    def test_multiline_thinking(self):
        text = '<think>\nLine 1 of reasoning\nLine 2 of reasoning\nLine 3\n</think>\n{"thought": "t", "final_answer": "ok"}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert "Line 1" in result.thinking
        assert "Line 3" in result.thinking

    def test_thinking_with_code_blocks(self):
        text = '<think>\nLet me check:\n```python\nprint("hi")\n```\n</think>\n{"thought": "t", "final_answer": "ok"}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert "print" in result.thinking

    def test_thinking_with_json_inside(self):
        text = '<think>The format is {"key": "value"}</think>\n{"thought": "t", "final_answer": "ok"}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.final_answer == "ok"

    def test_empty_think_block(self):
        text = '<think></think>\n{"thought": "t", "final_answer": "ok"}'
        result = parse_react(text)
        assert result.thinking is None  # empty block → None
        assert result.parse_stage == 1

    def test_thinking_preserved_in_result(self):
        """Thinking content should be accessible but NOT interfere with parsing."""
        text = '<think>deep analysis here</think>\n{"thought": "summary", "action": "shell", "action_input": {"command": "ls"}}'
        result = parse_react(text)
        assert result.thinking == "deep analysis here"
        assert result.thought == "summary"
        assert result.action == "shell"
        assert result.parse_stage == 1
