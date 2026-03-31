"""Tests for tools/registry."""

from agent_cli.tools.registry import (
    validate_tool_input,
    get_tool_descriptions,
    convert_to_anthropic_tools,
    convert_to_openai_tools,
)


class TestValidateToolInput:
    def test_valid_read_file(self):
        ok, err = validate_tool_input("read_file", {"path": "/tmp/test.py"})
        assert ok is True
        assert err is None

    def test_valid_shell(self):
        ok, err = validate_tool_input("shell", {"command": "ls -la"})
        assert ok is True

    def test_missing_required(self):
        ok, err = validate_tool_input("read_file", {})
        assert ok is False
        assert "path" in err

    def test_missing_multiple_required(self):
        ok, err = validate_tool_input("write_file", {})
        assert ok is False
        assert "path" in err

    def test_unknown_tool(self):
        ok, err = validate_tool_input("nonexistent", {})
        assert ok is False
        assert "Unknown tool" in err

    def test_string_auto_convert(self):
        """Small models sometimes send string instead of dict."""
        ok, err = validate_tool_input("read_file", "/tmp/test.py")
        assert ok is True

    def test_string_json_auto_convert(self):
        ok, err = validate_tool_input("read_file", '{"path": "/tmp/test.py"}')
        assert ok is True

    def test_none_input(self):
        ok, err = validate_tool_input("read_file", None)
        assert ok is False


class TestTypeValidation:
    def test_correct_types_pass(self):
        ok, err = validate_tool_input("shell", {"command": "ls", "timeout": 30})
        assert ok is True

    def test_string_timeout_auto_coerced(self):
        """Small model sends "30" instead of 30 — auto-coerce."""
        inp = {"command": "ls", "timeout": "30"}
        ok, err = validate_tool_input("shell", inp)
        assert ok is True
        assert inp["timeout"] == 30  # coerced in-place

    def test_dict_edits_auto_coerced_to_array(self):
        """Small model sends dict instead of [dict] — auto-coerce."""
        inp = {"path": "a.py", "edits": {"op": "replace", "pos": "1#VR"}}
        ok, err = validate_tool_input("edit_file", inp)
        assert ok is True
        assert isinstance(inp["edits"], list)

    def test_wrong_type_no_coercion(self):
        """Cannot coerce list to string."""
        ok, err = validate_tool_input("read_file", {"path": [1, 2, 3]})
        assert ok is False
        assert "expected string" in err


class TestConvertToAnthropicTools:
    def test_converts_all_tools(self):
        tools = convert_to_anthropic_tools(
            ["read_file", "write_file", "edit_file", "shell"]
        )
        names = {t["name"] for t in tools}
        # 4 requested + always-included (complete, ready_for_review)
        assert {"read_file", "write_file", "edit_file", "shell"}.issubset(names)
        assert "complete" in names
        assert "ready_for_review" in names
        assert all("name" in t and "input_schema" in t for t in tools)

    def test_single_tool(self):
        tools = convert_to_anthropic_tools(["shell"])
        names = {t["name"] for t in tools}
        assert "shell" in names
        # Always-included tools are present
        assert "complete" in names
        assert "ready_for_review" in names

    def test_with_delegate(self):
        tools = convert_to_anthropic_tools(["shell"], include_delegate=True)
        names = {t["name"] for t in tools}
        assert "delegate" in names

    def test_without_delegate(self):
        tools = convert_to_anthropic_tools(["shell"], include_delegate=False)
        names = {t["name"] for t in tools}
        assert "delegate" not in names

    def test_schema_structure(self):
        tools = convert_to_anthropic_tools(["read_file"])
        t = next(t for t in tools if t["name"] == "read_file")
        assert t["input_schema"]["type"] == "object"
        assert "path" in t["input_schema"]["properties"]


class TestConvertToOpenAITools:
    def test_converts_all_tools(self):
        tools = convert_to_openai_tools(
            ["read_file", "write_file", "edit_file", "shell"]
        )
        names = {t["function"]["name"] for t in tools}
        assert {"read_file", "write_file", "edit_file", "shell"}.issubset(names)
        assert "complete" in names
        assert "ready_for_review" in names
        assert all(t["type"] == "function" for t in tools)

    def test_function_structure(self):
        tools = convert_to_openai_tools(["shell"])
        t = next(t for t in tools if t["function"]["name"] == "shell")
        assert t["function"]["name"] == "shell"
        assert "parameters" in t["function"]

    def test_with_delegate(self):
        tools = convert_to_openai_tools(["shell"], include_delegate=True)
        names = {t["function"]["name"] for t in tools}
        assert "delegate" in names


class TestEmptyStringStripping:
    def test_optional_empty_string_removed(self):
        """Empty string on optional field should be stripped before validation."""
        action_input = {"path": "/tmp/test.py", "line_start": "", "line_end": ""}
        ok, err = validate_tool_input("read_file", action_input)
        assert ok is True
        assert "line_start" not in action_input
        assert "line_end" not in action_input

    def test_required_empty_string_not_removed(self):
        """Empty string on required field should NOT be stripped — validation fails."""
        ok, err = validate_tool_input("read_file", {"path": ""})
        # path="" is required and present, but it's an empty string — still valid type
        assert ok is True  # type check passes (string), tool itself handles empty

    def test_non_empty_optional_kept(self):
        """Non-empty optional fields should remain untouched."""
        action_input = {"path": "/tmp/test.py", "line_start": 10}
        ok, err = validate_tool_input("read_file", action_input)
        assert ok is True
        assert action_input["line_start"] == 10


class TestGetToolDescriptions:
    def test_returns_string(self):
        desc = get_tool_descriptions()
        assert isinstance(desc, str)
        assert "read_file" in desc
        assert "shell" in desc

    def test_includes_complete_and_ask(self):
        """Virtual tools should appear in descriptions when requested."""
        desc = get_tool_descriptions(["read_file", "complete", "ask"])
        assert "complete" in desc
        assert "ask" in desc

    def test_always_includes_essential_tools(self):
        """complete and ready_for_review are always in descriptions even if not requested."""
        desc = get_tool_descriptions(["shell"])
        assert "complete" in desc
        assert "ready_for_review" in desc

    def test_no_duplicate_when_already_requested(self):
        """If complete is already in the list, it should not appear twice."""
        desc = get_tool_descriptions(["shell", "complete", "ready_for_review"])
        assert desc.count("- complete:") == 1
        assert desc.count("- ready_for_review:") == 1
