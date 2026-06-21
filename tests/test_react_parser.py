"""Tests for the ReAct format's 3-stage parser.

Lives at ``agent_cli/wire_formats/react.py:parse_react`` since the
plugin owns its own parser (the previous ``agent_cli/parsing/``
package was folded in to make the plugin folder-deletable as a unit).
"""

from agent_cli.wire_formats.react import parse_react


class TestStage1DirectParse:
    def test_clean_json_action(self):
        text = '{"thought": "I need to read", "action": "read_file", "action_input": {"path": "a.py"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.thought == "I need to read"
        assert result.action == "read_file"
        assert result.action_input == {"path": "a.py"}

    def test_complete_tool(self):
        text = '{"thought": "done", "action": "complete", "action_input": {"result": "42"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.action == "complete"
        assert result.action_input == {"result": "42"}

    def test_markdown_fences(self):
        text = '```json\n{"thought": "hi", "action": "complete", "action_input": {"result": "done"}}\n```'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.thought == "hi"

    def test_json_with_surrounding_text(self):
        text = 'Sure! {"thought": "ok", "action": "shell", "action_input": {"command": "ls"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.action == "shell"


class TestStage2JsonRepair:
    def test_trailing_comma(self):
        text = '{"thought": "hi", "action": "shell",}'
        result = parse_react(text)
        assert result.parse_stage == 2
        assert result.action == "shell"

    def test_missing_brace(self):
        text = '{"thought": "hello", "action": "complete", "action_input": {"result": "done"}'
        result = parse_react(text)
        assert result.parse_stage == 2
        assert result.action == "complete"

    def test_single_quotes(self):
        text = "{'thought': 'reasoning', 'action': 'read_file'}"
        result = parse_react(text)
        assert result.parse_stage == 2
        assert result.action == "read_file"


class TestStage2LiteralControlChars:
    """The model writes a big `action_input` string (content/result blob) with
    REAL newlines/tabs instead of `\\n` escapes — invalid strict JSON. Without
    the lenient (strict=False) stage-2 retry this fell to the stage-3 regex,
    which returns action_input as an unusable raw STRING. Now it recovers as a
    proper dict at stage 2 (reproduced: same class as md_array 1781213377)."""

    def test_literal_newlines_in_action_input_recovers_as_dict(self):
        # The `\n` in this Python literal are REAL newline bytes.
        text = (
            '{"thought": "write", "action": "write_file", '
            '"action_input": {"path": "a.c", "content": "line1\nline2\nline3"}}'
        )
        result = parse_react(text)
        assert result.parse_stage == 2
        assert result.action == "write_file"
        assert result.action_input == {"path": "a.c", "content": "line1\nline2\nline3"}

    def test_literal_tab_recovers(self):
        text = (
            '{"thought": "t", "action": "shell", "action_input": {"command": "a\tb"}}'
        )
        result = parse_react(text)
        assert result.parse_stage == 2
        assert result.action_input == {"command": "a\tb"}

    def test_clean_escaped_json_stays_stage1(self):
        # Properly escaped \n must still parse clean at stage 1 (lenient path
        # is a fallback, never the primary).
        text = '{"thought": "t", "action": "complete", "action_input": {"result": "a\\nb"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.action_input == {"result": "a\nb"}


class TestStage3Regex:
    def test_extremely_broken_json(self):
        text = 'blah "thought": "I am thinking", blah "action": "shell"'
        result = parse_react(text)
        assert result.parse_stage == 3
        assert result.thought == "I am thinking"
        assert result.action == "shell"

    def test_action_input_regex(self):
        text = 'blah "thought": "t", "action": "read_file", "action_input": {"path":: broken'
        result = parse_react(text)
        assert result.parse_stage == 3
        assert result.action == "read_file"


class TestStage0Failure:
    def test_no_recognizable_content(self):
        result = parse_react("Hello, how are you?")
        assert result.parse_stage == 0
        assert result.thought is None
        assert result.action is None
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


class TestCompleteAction:
    def test_complete_with_dict(self):
        text = '{"thought": "done", "action": "complete", "action_input": {"result": "The answer is 42"}}'
        result = parse_react(text)
        assert result.action == "complete"
        assert result.action_input == {"result": "The answer is 42"}

    def test_complete_with_string_input(self):
        text = (
            '{"thought": "done", "action": "complete", "action_input": "Simple answer"}'
        )
        result = parse_react(text)
        assert result.action == "complete"
        assert result.action_input == "Simple answer"


class TestUnicodeSanitization:
    def test_surrogate_removed(self):
        text = '{"thought": "hello\ud800world", "action": "complete", "action_input": {"result": "done"}}'
        result = parse_react(text)
        assert result.action == "complete"
        assert result.parse_stage >= 1

    def test_normal_text_unchanged(self):
        text = '{"thought": "normal text", "action": "shell", "action_input": {"command": "ls"}}'
        result = parse_react(text)
        assert result.thought == "normal text"


class TestThinkingBlockStripping:
    def test_think_tags_stripped(self):
        text = '<think>\nI need to read the file.\n</think>\n{"thought": "t", "action": "read_file", "action_input": {"path": "a.py"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.thinking == "I need to read the file."
        assert result.action == "read_file"

    def test_thinking_tags_stripped(self):
        text = '<thinking>step by step reasoning</thinking>\n{"thought": "ok", "action": "complete", "action_input": {"result": "42"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.thinking == "step by step reasoning"
        assert result.action == "complete"

    def test_reasoning_tags_stripped(self):
        text = '<reasoning>analyzing the problem</reasoning>\n{"thought": "ok", "action": "complete", "action_input": {"result": "done"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.thinking == "analyzing the problem"

    def test_reflection_tags_stripped(self):
        text = '<reflection>let me reconsider</reflection>\n{"thought": "t", "action": "complete", "action_input": {"result": "ok"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.thinking == "let me reconsider"

    def test_no_thinking_tags(self):
        text = (
            '{"thought": "t", "action": "complete", "action_input": {"result": "done"}}'
        )
        result = parse_react(text)
        assert result.thinking is None
        assert result.parse_stage == 1

    def test_multiple_think_blocks(self):
        text = '<think>first thought</think>\n<think>second thought</think>\n{"thought": "t", "action": "complete", "action_input": {"result": "ok"}}'
        result = parse_react(text)
        assert result.thinking is not None
        assert "first thought" in result.thinking
        assert "second thought" in result.thinking

    def test_think_tags_case_insensitive(self):
        text = '<THINK>uppercase reasoning</THINK>\n{"thought": "t", "action": "complete", "action_input": {"result": "ok"}}'
        result = parse_react(text)
        assert result.thinking == "uppercase reasoning"

    def test_multiline_thinking(self):
        text = '<think>\nLine 1 of reasoning\nLine 2 of reasoning\nLine 3\n</think>\n{"thought": "t", "action": "complete", "action_input": {"result": "ok"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert "Line 1" in result.thinking
        assert "Line 3" in result.thinking

    def test_thinking_with_code_blocks(self):
        text = '<think>\nLet me check:\n```python\nprint("hi")\n```\n</think>\n{"thought": "t", "action": "complete", "action_input": {"result": "ok"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert "print" in result.thinking

    def test_thinking_with_json_inside(self):
        text = '<think>The format is {"key": "value"}</think>\n{"thought": "t", "action": "complete", "action_input": {"result": "ok"}}'
        result = parse_react(text)
        assert result.parse_stage == 1
        assert result.action == "complete"

    def test_empty_think_block(self):
        text = '<think></think>\n{"thought": "t", "action": "complete", "action_input": {"result": "ok"}}'
        result = parse_react(text)
        assert result.thinking is None
        assert result.parse_stage == 1

    def test_thinking_preserved_in_result(self):
        """Thinking content should be accessible but NOT interfere with parsing."""
        text = '<think>deep analysis here</think>\n{"thought": "summary", "action": "shell", "action_input": {"command": "ls"}}'
        result = parse_react(text)
        assert result.thinking == "deep analysis here"
        assert result.thought == "summary"
        assert result.action == "shell"
        assert result.parse_stage == 1


class TestVirtualToolPayloadHoist:
    """Models (qwen3 notably) sometimes put the final-answer payload at
    the top level instead of nesting inside action_input. These tests
    pin down the normalization that hoists it back."""

    def test_complete_hoists_top_level_result(self):
        """The exact drift observed with qwen3.6:35b-a3b-bf16."""
        text = '{"thought": "done", "action": "complete", "result": "42"}'
        result = parse_react(text)
        assert result.action == "complete"
        assert result.action_input == {"result": "42"}

    def test_complete_hoists_answer_as_result(self):
        text = '{"thought": "done", "action": "complete", "answer": "the final answer"}'
        result = parse_react(text)
        assert result.action_input == {"result": "the final answer"}

    def test_complete_hoists_response_as_result(self):
        text = '{"thought": "done", "action": "complete", "response": "hello"}'
        result = parse_react(text)
        assert result.action_input == {"result": "hello"}

    def test_complete_prefers_result_over_other_fallbacks(self):
        """When multiple candidate keys are present, `result` wins."""
        text = '{"thought": "done", "action": "complete", "result": "primary", "answer": "secondary"}'
        result = parse_react(text)
        assert result.action_input == {"result": "primary"}

    def test_complete_with_existing_action_input_unchanged(self):
        """Hoist must NOT overwrite a real action_input."""
        text = '{"thought": "done", "action": "complete", "action_input": {"result": "kept"}, "result": "ignored"}'
        result = parse_react(text)
        assert result.action_input == {"result": "kept"}

    def test_ask_hoists_top_level_question_as_questions(self):
        text = '{"thought": "need input", "action": "ask", "question": "What color?"}'
        result = parse_react(text)
        assert result.action == "ask"
        # Normalized under "questions" — _extract_questions handles str→list
        assert result.action_input == {"questions": "What color?"}

    def test_ask_hoists_top_level_questions_list(self):
        text = '{"thought": "need input", "action": "ask", "questions": ["A", "B"]}'
        result = parse_react(text)
        assert result.action_input == {"questions": ["A", "B"]}

    def test_virtual_tool_with_unknown_sibling_only_stays_none(self):
        """When a virtual tool has no known-alias sibling, action_input
        stays None — we do NOT fall through to the real-tool hoist for
        virtual tools. Rationale: complete/ask have
        well-defined payload shapes; arbitrary sibling keys shouldn't
        be blindly stuffed into action_input and pretend to be the
        payload."""
        text = '{"thought": "blank", "action": "complete", "unknown_field": "x"}'
        result = parse_react(text)
        assert result.action == "complete"
        assert result.action_input is None

    def test_no_fallback_keys_leaves_action_input_none(self):
        """complete without action_input AND without any known fallback
        key → action_input stays None, loop will render the 'no result'
        message as before."""
        text = '{"thought": "blank", "action": "complete"}'
        result = parse_react(text)
        assert result.action == "complete"
        assert result.action_input is None

    def test_hoist_works_after_json_repair(self):
        """Drift + truncation both — stage 2 repair yields the dict,
        and the hoist still fires."""
        text = '{"thought": "ok", "action": "complete", "result": "done"'
        result = parse_react(text)
        assert result.parse_stage == 2
        assert result.action_input == {"result": "done"}


class TestRealToolArgHoist:
    """Layer 2 normalization: when the action is a real tool (shell,
    read_file, etc. — anything NOT in the virtual tool payload map) and
    action_input is missing/empty, bundle non-reserved top-level keys
    into action_input. Pins the pcie_scsc-session drift where qwen3.6
    kept emitting {"action":"shell","command":"..."} with command as a
    sibling of action rather than nested inside action_input."""

    def test_shell_hoists_top_level_command(self):
        """The exact drift observed in session 1776942600."""
        text = '{"thought": "find files", "action": "shell", "command": "ls"}'
        result = parse_react(text)
        assert result.action == "shell"
        assert result.action_input == {"command": "ls"}

    def test_shell_hoists_command_and_timeout_together(self):
        """All non-reserved siblings bundle as a whole — tool-specific
        multi-arg shapes work without the parser knowing the schema."""
        text = (
            '{"thought": "sleep then exit", "action": "shell", '
            '"command": "sleep 5", "timeout": 10}'
        )
        result = parse_react(text)
        assert result.action_input == {"command": "sleep 5", "timeout": 10}

    def test_read_file_hoists_multiple_args(self):
        text = (
            '{"thought": "read", "action": "read_file", '
            '"path": "a.py", "line_start": 1, "line_end": 50}'
        )
        result = parse_react(text)
        assert result.action_input == {"path": "a.py", "line_start": 1, "line_end": 50}

    def test_edit_file_hoists_edits_array(self):
        """Nested array payloads (like edit_file's edits list) hoist
        correctly — parser just treats the value opaquely."""
        text = (
            '{"thought": "edit", "action": "edit_file", '
            '"path": "loop.py", '
            '"edits": [{"op": "replace", "pos": "1#AA", "lines": ["x"]}]}'
        )
        result = parse_react(text)
        assert result.action_input == {
            "path": "loop.py",
            "edits": [{"op": "replace", "pos": "1#AA", "lines": ["x"]}],
        }

    def test_nested_action_input_takes_priority_over_siblings(self):
        """If action_input is present and truthy, siblings are ignored.
        Defensive choice: assume model explicitly picked the nested form
        and any sibling was unrelated metadata."""
        text = (
            '{"thought": "...", "action": "shell", '
            '"action_input": {"command": "nested"}, '
            '"command": "sibling-ignored"}'
        )
        result = parse_react(text)
        assert result.action_input == {"command": "nested"}

    def test_empty_action_input_triggers_hoist(self):
        """action_input={} is falsy — siblings ARE hoisted. This covers
        models that emit an empty placeholder plus siblings."""
        text = (
            '{"thought": "...", "action": "shell", "action_input": {}, "command": "ls"}'
        )
        result = parse_react(text)
        assert result.action_input == {"command": "ls"}

    def test_shell_no_siblings_leaves_action_input_none(self):
        """Nothing to hoist — action_input stays None and the loop's
        validator will catch the missing required field (unchanged
        behavior)."""
        text = '{"thought": "...", "action": "shell"}'
        result = parse_react(text)
        assert result.action == "shell"
        assert result.action_input is None

    def test_real_tool_hoist_survives_json_repair(self):
        """Truncated sibling-form JSON still hoists after stage 2 repair."""
        text = '{"thought": "...", "action": "shell", "command": "ls"'
        result = parse_react(text)
        assert result.parse_stage == 2
        assert result.action_input == {"command": "ls"}

    def test_unknown_action_hoists_like_real_tool(self):
        """Unknown actions (e.g. MCP-provided tools not in our virtual
        map) follow the real-tool rule — bundle siblings."""
        text = (
            '{"thought": "call MCP", "action": "myserver.search", '
            '"query": "python", "limit": 10}'
        )
        result = parse_react(text)
        assert result.action_input == {"query": "python", "limit": 10}


class TestReservedKeyBlacklist:
    """Keys that might appear in model output but must NEVER be bundled
    into action_input — they are ReAct protocol fields or meta keys.
    These tests pin the blacklist so an accidental drift (e.g. a model
    emitting `role:"assistant"` alongside action) does not poison tool
    input."""

    def test_role_not_hoisted(self):
        """`role` is added at history storage time, but a confused model
        might emit it. Blacklisted."""
        text = (
            '{"thought": "...", "action": "shell", '
            '"command": "ls", "role": "assistant"}'
        )
        result = parse_react(text)
        assert result.action_input == {"command": "ls"}

    def test_observation_not_hoisted(self):
        """System prompt forbids `observation` in model output. If a
        model emits it anyway, it isn't a tool arg."""
        text = (
            '{"thought": "...", "action": "shell", '
            '"command": "ls", "observation": "fake"}'
        )
        result = parse_react(text)
        assert result.action_input == {"command": "ls"}

    def test_reasoning_and_reflection_not_hoisted(self):
        """`reasoning` and `reflection` are thinking-tag variants. A
        model that emits them at top level has drifted in a different
        way (wrong field name for `thought`); the right fix is prompt
        correction, not stuffing them into a tool's argument dict."""
        text = (
            '{"thought": "...", "action": "shell", '
            '"command": "ls", "reasoning": "x", "reflection": "y"}'
        )
        result = parse_react(text)
        assert result.action_input == {"command": "ls"}

    def test_meta_key_not_hoisted(self):
        """`_meta` is an internal marker for session/history records,
        never a tool arg."""
        text = (
            '{"thought": "...", "action": "shell", '
            '"command": "ls", "_meta": {"session": "abc"}}'
        )
        result = parse_react(text)
        assert result.action_input == {"command": "ls"}

    def test_only_blacklisted_siblings_leaves_action_input_none(self):
        """Every sibling is blacklisted → nothing real to hoist,
        action_input stays None."""
        text = (
            '{"thought": "...", "action": "shell", '
            '"role": "assistant", "observation": "x"}'
        )
        result = parse_react(text)
        assert result.action == "shell"
        assert result.action_input is None
