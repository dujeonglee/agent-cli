"""Tests for tools/registry."""
import pytest

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
        tools = convert_to_anthropic_tools(["read_file", "write_file", "edit_file", "shell"])
        assert len(tools) == 4
        assert all("name" in t and "input_schema" in t for t in tools)

    def test_single_tool(self):
        tools = convert_to_anthropic_tools(["shell"])
        assert len(tools) == 1
        assert tools[0]["name"] == "shell"

    def test_with_delegate(self):
        tools = convert_to_anthropic_tools(["shell"], include_delegate=True)
        assert len(tools) == 2
        names = [t["name"] for t in tools]
        assert "delegate" in names

    def test_without_delegate(self):
        tools = convert_to_anthropic_tools(["shell"], include_delegate=False)
        names = [t["name"] for t in tools]
        assert "delegate" not in names

    def test_schema_structure(self):
        tools = convert_to_anthropic_tools(["read_file"])
        t = tools[0]
        assert t["input_schema"]["type"] == "object"
        assert "path" in t["input_schema"]["properties"]


class TestConvertToOpenAITools:
    def test_converts_all_tools(self):
        tools = convert_to_openai_tools(["read_file", "write_file", "edit_file", "shell"])
        assert len(tools) == 4
        assert all(t["type"] == "function" for t in tools)

    def test_function_structure(self):
        tools = convert_to_openai_tools(["shell"])
        t = tools[0]
        assert t["function"]["name"] == "shell"
        assert "parameters" in t["function"]

    def test_with_delegate(self):
        tools = convert_to_openai_tools(["shell"], include_delegate=True)
        assert len(tools) == 2


class TestGetToolDescriptions:
    def test_returns_string(self):
        desc = get_tool_descriptions()
        assert isinstance(desc, str)
        assert "read_file" in desc
        assert "shell" in desc
