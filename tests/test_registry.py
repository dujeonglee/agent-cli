"""Tests for tools/registry."""

from agent_cli.tools.registry import (
    TOOL_SCHEMAS,
    TOOLS,
    validate_tool_input,
    get_tool_descriptions,
)


class TestValidateToolInput:
    def test_valid_read_file(self):
        # Flat-native (Step 3): read_file takes flat {path, ...mode}.
        ok, err, _ = validate_tool_input("read_file", {"path": "/tmp/test.py"})
        assert ok is True
        assert err is None

    def test_valid_shell(self):
        ok, err, _ = validate_tool_input("shell", {"shell_command": "ls -la"})
        assert ok is True

    def test_missing_required(self):
        ok, err, _ = validate_tool_input("read_file", {})
        assert ok is False
        assert "path" in err

    def test_missing_multiple_required(self):
        ok, err, _ = validate_tool_input("write_file", {})
        assert ok is False
        assert "path" in err

    def test_unknown_tool(self):
        ok, err, _ = validate_tool_input("nonexistent", {})
        assert ok is False
        assert "Unknown tool" in err

    def test_string_json_auto_convert(self):
        ok, err, converted = validate_tool_input(
            "read_file", '{"path": "/tmp/test.py"}'
        )
        assert ok is True
        assert converted["path"] == "/tmp/test.py"

    def test_string_auto_convert_shell(self):
        """String input for shell → {"shell_command": "..."}."""
        ok, err, converted = validate_tool_input("shell", "ls -la")
        assert ok is True
        assert converted["shell_command"] == "ls -la"

    def test_string_auto_convert_write_file(self):
        """String input for write_file → {"path": "..."}."""
        ok, err, converted = validate_tool_input("write_file", "/tmp/out.txt")
        assert ok is False  # missing required "content" field
        assert "content" in err

    def test_string_auto_convert_edit_file(self):
        """String input for edit_file → {"path": "..."}."""
        # Flat-native (Step 3): edit_file requires path/op/pos, so a bare
        # path string is missing op.
        ok, err, converted = validate_tool_input("edit_file", "src/main.py")
        assert ok is False
        assert "op" in err

    def test_none_input(self):
        ok, err, _ = validate_tool_input("read_file", None)
        assert ok is False

    def test_int_input(self):
        """Integer input should fail."""
        ok, err, _ = validate_tool_input("read_file", 42)
        assert ok is False

    def test_list_input(self):
        """List input should fail."""
        ok, err, _ = validate_tool_input("read_file", ["/tmp/test.py"])
        assert ok is False


class TestTypeValidation:
    def test_correct_types_pass(self):
        ok, err, _ = validate_tool_input(
            "shell", {"shell_command": "ls", "shell_timeout": 30}
        )
        assert ok is True

    def test_string_timeout_auto_coerced(self):
        """Small model sends "30" instead of 30 — auto-coerce."""
        inp = {"shell_command": "ls", "shell_timeout": "30"}
        ok, err, _ = validate_tool_input("shell", inp)
        assert ok is True
        assert inp["shell_timeout"] == 30  # coerced in-place

    def test_dict_array_param_auto_coerced_to_array(self):
        """Small model sends dict instead of [dict] for an array param —
        auto-coerce. edit_file went flat-native (Step 3), so this is pinned
        against a still-batch tool (delegate_tasks)."""
        inp = {"delegate_tasks": {"task": "do x"}}
        ok, err, _ = validate_tool_input("delegate", inp)
        assert ok is True
        assert isinstance(inp["delegate_tasks"], list)

    def test_wrong_type_no_coercion(self):
        """Cannot coerce list to string."""
        ok, err, _ = validate_tool_input("shell", {"shell_command": [1, 2, 3]})
        assert ok is False
        assert "expected string" in err


class TestDelegateSchema:
    def test_delegate_has_tasks_param(self):

        props = TOOL_SCHEMAS["delegate"].parameters["properties"]
        assert "delegate_tasks" in props
        assert props["delegate_tasks"]["type"] == "array"

    def test_delegate_tasks_is_array_of_objects(self):

        items = TOOL_SCHEMAS["delegate"].parameters["properties"]["delegate_tasks"][
            "items"
        ]
        assert items["type"] == "object"
        assert "task" in items["properties"]
        assert "context" in items["properties"]
        assert "tools" in items["properties"]

    def test_delegate_tasks_required(self):

        assert "delegate_tasks" in TOOL_SCHEMAS["delegate"].parameters["required"]

    def test_delegate_no_top_level_task(self):

        props = TOOL_SCHEMAS["delegate"].parameters["properties"]
        assert "task" not in props  # Only inside delegate_tasks array items

    def test_delegate_schema_has_agent_field(self):
        """AG-29: TOOL_SCHEMAS["delegate"] items have agent field."""

        items = TOOL_SCHEMAS["delegate"].parameters["properties"]["delegate_tasks"][
            "items"
        ]
        assert "agent" in items["properties"]
        assert items["properties"]["agent"]["type"] == "string"

    def test_delegate_schema_agent_not_required(self):
        """AG-30: agent field is not in required list."""

        items = TOOL_SCHEMAS["delegate"].parameters["properties"]["delegate_tasks"][
            "items"
        ]
        assert "agent" not in items["required"]


class TestEmptyStringStripping:
    def test_optional_empty_string_removed(self):
        """Empty string on optional field should be stripped before validation."""
        action_input = {
            "shell_command": "ls",
            "shell_timeout": "",
        }
        ok, err, _ = validate_tool_input("shell", action_input)
        assert ok is True
        assert "shell_timeout" not in action_input

    def test_required_empty_string_not_removed(self):
        """Empty string on required field should NOT be stripped — validation fails."""
        ok, err, _ = validate_tool_input("shell", {"shell_command": ""})
        # shell_command="" is required and present, but it's an empty string
        assert ok is True  # type check passes (string), tool itself handles empty

    def test_non_empty_optional_kept(self):
        """Non-empty optional fields should remain untouched."""
        action_input = {"shell_command": "ls", "shell_timeout": 30}
        ok, err, _ = validate_tool_input("shell", action_input)
        assert ok is True
        assert action_input["shell_timeout"] == 30


class TestStringInputAutoConversion:
    """String→dict auto-conversion now lives in the recovery layer's
    schema detector (which wraps ``validate_tool_input``). The downstream
    dispatch path (``_dispatch_tool_with_hooks``) assumes already-validated
    dict input, so the conversion contract is exercised here at the
    detector boundary, then executed through the internal ``_execute_tool``
    primitive to confirm end-to-end behaviour is preserved.
    """

    def test_shell_string_input(self):
        """shell with string input is normalized to {'command': '...'}."""
        from agent_cli.recovery.detectors import detect_schema_mismatch
        from agent_cli.tools import _execute_tool as execute_tool

        mismatched, err, normalized = detect_schema_mismatch("shell", "echo hello")
        assert not mismatched, err
        assert normalized == {"shell_command": "echo hello"}

        result = execute_tool("shell", normalized)
        assert result.success
        assert "hello" in result.output


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

    def test_required_fields_appear_in_descriptions(self):
        """Every required field of every native tool must surface in the
        rendered descriptions — the structural guard that a batch tool
        added without an inline guide can never expose an empty schema."""
        desc = get_tool_descriptions(list(TOOLS.keys()))
        for name, tool in TOOLS.items():
            for field in tool.parameters.get("required", []):
                assert field in desc, f"required field {field!r} of {name} missing"
                # required marker present for that field's value
                assert "required" in desc

    def test_array_of_object_item_keys_surfaced(self):
        """Array-of-object params surface their item keys as
        ``array<object{...}>`` so the item shape is visible without an
        inline guide. code_index/delegate are the canonical batch tools
        (read_file went flat-native in Step 3, so it is no longer an array)."""
        desc = get_tool_descriptions(["code_index", "delegate"])
        assert "array<object{" in desc
        # code_index_queries items: mode (required) + path?/name?/...
        assert "mode" in desc and "path" in desc
        # delegate_tasks items: task (required) + context?/tools?/agent?
        assert "context?" in desc and "agent?" in desc

    def test_scalar_type_preserved(self):
        """Scalar params keep their type even when they carry a
        description (the old flattening dropped type when description
        existed)."""
        desc = get_tool_descriptions(["shell"])
        # shell_timeout is an integer with a description
        assert "integer" in desc


class TestRenderParamValue:
    def test_scalar_required(self):
        from agent_cli.tools.registry import render_param_value

        out = render_param_value({"type": "string", "description": "a path"}, True)
        assert out == "string, required — a path"

    def test_scalar_optional(self):
        from agent_cli.tools.registry import render_param_value

        out = render_param_value({"type": "integer", "description": "secs"}, False)
        assert out == "integer — secs"

    def test_array_of_objects(self):
        from agent_cli.tools.registry import render_param_value

        schema = {
            "type": "array",
            "description": "list",
            "items": {
                "type": "object",
                "properties": {"path": {}, "stat": {}},
                "required": ["path"],
            },
        }
        out = render_param_value(schema, True)
        assert out == "array<object{path, stat?}>, required — list"

    def test_array_of_scalars(self):
        from agent_cli.tools.registry import render_param_value

        schema = {"type": "array", "items": {"type": "string"}, "description": "qs"}
        assert render_param_value(schema, True) == "array<string>, required — qs"

    def test_no_description(self):
        from agent_cli.tools.registry import render_param_value

        assert render_param_value({"type": "string"}, True) == "string, required"
