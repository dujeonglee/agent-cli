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
        ok, err, _ = validate_tool_input("shell", {"command": "ls -la"})
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
        """String input for shell → {"command": "..."}."""
        ok, err, converted = validate_tool_input("shell", "ls -la")
        assert ok is True
        assert converted["command"] == "ls -la"

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
        ok, err, _ = validate_tool_input("shell", {"command": "ls", "timeout": 30})
        assert ok is True

    def test_string_timeout_auto_coerced(self):
        """Small model sends "30" instead of 30 — auto-coerce."""
        inp = {"command": "ls", "timeout": "30"}
        ok, err, _ = validate_tool_input("shell", inp)
        assert ok is True
        assert inp["timeout"] == 30  # coerced in-place

    def test_dict_array_param_auto_coerced_to_array(self):
        """Small model sends a dict instead of [dict] for an array param —
        auto-coerce. All builtin tools are flat-native now (Step 3), so this
        pins the ``_try_coerce`` helper directly; the coercion still serves
        MCP / external tools whose schemas declare array params."""
        from agent_cli.tools.registry import _try_coerce

        assert _try_coerce({"task": "do x"}, "array") == [{"task": "do x"}]

    def test_wrong_type_no_coercion(self):
        """Cannot coerce list to string."""
        ok, err, _ = validate_tool_input("shell", {"command": [1, 2, 3]})
        assert ok is False
        assert "expected string" in err


class TestDelegateSchema:
    """Flat-native (consolidation Step 3): delegate takes one flat task per op
    — `{task, context?, tools?, agent?}`, no `delegate_tasks` array. Several
    delegate ops in a turn run in parallel (parallel_safe=True, loop-batched)."""

    def test_delegate_flat_task_param(self):
        props = TOOL_SCHEMAS["delegate"].parameters["properties"]
        assert "task" in props
        assert props["task"]["type"] == "string"
        # the old batch array wrapper is gone
        assert "delegate_tasks" not in props

    def test_delegate_flat_fields_present(self):
        props = TOOL_SCHEMAS["delegate"].parameters["properties"]
        for k in ("task", "context", "tools", "agent"):
            assert k in props

    def test_delegate_task_required(self):
        assert TOOL_SCHEMAS["delegate"].parameters["required"] == ["task"]

    def test_delegate_agent_is_string(self):
        props = TOOL_SCHEMAS["delegate"].parameters["properties"]
        assert props["agent"]["type"] == "string"

    def test_delegate_agent_not_required(self):
        assert "agent" not in TOOL_SCHEMAS["delegate"].parameters["required"]

    def test_delegate_is_parallel_safe(self):
        # The marker the loop reads to batch consecutive delegate ops into one
        # concurrent dispatch (the only tool that opts in).
        assert TOOLS["delegate"].parallel_safe is True


class TestEmptyStringStripping:
    def test_optional_empty_string_removed(self):
        """Empty string on optional field should be stripped before validation."""
        action_input = {
            "command": "ls",
            "timeout": "",
        }
        ok, err, _ = validate_tool_input("shell", action_input)
        assert ok is True
        assert "timeout" not in action_input

    def test_required_empty_string_not_removed(self):
        """Empty string on required field should NOT be stripped — validation fails."""
        ok, err, _ = validate_tool_input("shell", {"command": ""})
        # command="" is required and present, but it's an empty string
        assert ok is True  # type check passes (string), tool itself handles empty

    def test_non_empty_optional_kept(self):
        """Non-empty optional fields should remain untouched."""
        action_input = {"command": "ls", "timeout": 30}
        ok, err, _ = validate_tool_input("shell", action_input)
        assert ok is True
        assert action_input["timeout"] == 30


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
        assert normalized == {"command": "echo hello"}

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
        """complete is always in descriptions even if not requested."""
        desc = get_tool_descriptions(["shell"])
        assert "complete" in desc

    def test_no_duplicate_when_already_requested(self):
        """If complete is already in the list, it should not appear twice."""
        desc = get_tool_descriptions(["shell", "complete"])
        assert desc.count("- complete:") == 1

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
        ``array<object{...}>`` so the item shape is visible without an inline
        guide. All builtin tools are flat-native now (Step 3), so this pins the
        ``render_param_value`` renderer directly — the mechanism still serves
        MCP / external tools whose schemas declare array-of-object params."""
        from agent_cli.tools.registry import render_param_value

        out = render_param_value(
            {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"task": {"type": "string"}, "agent": {}},
                    "required": ["task"],
                },
            },
            required=True,
        )
        assert "array<object{" in out
        assert "task" in out and "agent?" in out

    def test_scalar_type_preserved(self):
        """Scalar params keep their type even when they carry a
        description (the old flattening dropped type when description
        existed)."""
        desc = get_tool_descriptions(["shell"])
        # timeout is an integer with a description
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


# ── C7: Tool.validate 훅 — shape(중앙 1~5단계) + 의미론(6단계) 통합 ────


class TestSemanticValidationHook:
    def test_default_validate_is_none(self):
        from agent_cli.tools import TOOLS
        from agent_cli.tools.base import Tool

        assert Tool.validate(TOOLS["shell"], {"command": "ls"}) is None

    def test_central_stage6_catches_mode_semantics(self):
        # 정밀화 계약 ①: 의미론 실패도 중앙(A5 경로)에서 잡힘
        from agent_cli.tools.registry import validate_tool_input

        ok, err, _ = validate_tool_input("code_index", {"mode": "fetch"})
        assert not ok
        # 정밀화 계약 ②: 관찰 문구는 도구의 짧은 오류 그대로 —
        # 전체 스키마 전문("Expected: {")은 shape 실패에만 동봉
        assert err == "'path' is required for mode='fetch'"
        assert "Expected: {" not in err

    def test_shape_failure_still_carries_schema(self):
        from agent_cli.tools.registry import validate_tool_input

        ok, err, _ = validate_tool_input("read_file", {})
        assert not ok and "Expected: {" in err  # 기존 A5 동작 보존

    def test_run_defends_direct_callers(self):
        # 로직 1곳(validate)·실행 2곳 — 직접 호출자도 같은 문구로 거절
        from agent_cli.tools import TOOLS

        r = TOOLS["code_index"].run({"mode": "fetch"})
        assert not r.success and r.error == "'path' is required for mode='fetch'"

    def test_per_tool_validate_semantics(self):
        from agent_cli.tools import TOOLS

        assert TOOLS["edit_file"].validate({"path": "a", "op": "bogus"}) is not None
        assert "hashline string" in TOOLS["edit_file"].validate(
            {"path": "a", "op": "replace", "pos": 5}
        )
        assert (
            TOOLS["memory"].validate({"mode": "get"})
            == "'id' is required for mode='get'"
        )
        assert TOOLS["memory"].validate({"mode": "list"}) is None
        assert (
            TOOLS["code_index"].validate(
                {"mode": "lookup", "name": "f", "symbol_kind": "nope"}
            )
            is not None
        )

    def test_semantic_failure_records_schema_mismatch_via_a5(self):
        # 정밀화 계약 ③: 의미론 실패가 이제 관측 기록(SCHEMA_MISMATCH)에 잡힘
        from agent_cli.loop import LoopConfig, LoopState, ToolBridge, TurnDispatcher
        from agent_cli.wire_formats import get as get_wf
        from agent_cli.wire_formats.base import Op, ParsedTurn

        cfg = LoopConfig(
            tools_list=["code_index", "complete"], wire_format=get_wf("md_array")
        )
        st = LoopState(query="q")
        d = TurnDispatcher(
            cfg, st, ctx=None, tools=ToolBridge(cfg, st, None, None), recorder=None
        )
        turn = ParsedTurn(
            thought="t",
            ops=[Op("code_index", {"mode": "fetch"})],
            raw="r",
            parse_stage=1,
        )
        outcome = {}
        d._dispatch_op("r", turn, turn.ops[0], outcome)
        assert outcome.get("failure_signal") == "SCHEMA_MISMATCH"  # 이전엔 {}
        # 모델이 받는 관찰은 여전히 짧은 도구 문구
        obs = [m for m in st.messages if m["role"] == "user"][-1]
        assert "'path' is required for mode='fetch'" in obs["content"]
        assert "Expected: {" not in obs["content"]
