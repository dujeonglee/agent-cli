"""`Tool.schema_gaps` (v10.37.0) — 거부된 호출에서 도구가 답하는 사실.

고정하는 계약: ① 기본 구현(빠진 필수는 스키마 순서, 모르는 키는 보낸 순서, 객체가
아니면 빈 답) ② **등록된 모든 도구**에서 중앙 검증과 같은 답(검증이 이 메서드를 읽는다
— 두 판정이 갈라지지 않는다) ③ 도구가 재정의할 수 있다(AskTool 의 상주 키, 가상의
배치 도구) ④ 뒤늦게 등록되는 도구(MCP)도 같은 기본 구현을 받는다 ⑤ 레지스트리의
옛 자유 함수는 없다(중복 금지).
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from agent_cli.tools import registry
from agent_cli.tools.base import Tool
from agent_cli.tools.registry import TOOLS, validate_tool_input
from agent_cli.tools.result import ToolResult


class _Probe(Tool):
    name = "probe"
    description = "test tool"
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "b": {"type": "string"},
            "a": {"type": "string"},
            "opt": {"type": "integer"},
        },
        "required": ["b", "a"],
    }

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        return ToolResult(True, output="ok")


class TestDefault:
    def test_missing_follows_the_schema_required_order(self):
        assert _Probe().schema_gaps({}) == (["b", "a"], [])
        assert _Probe().schema_gaps({"a": "x"}) == (["b"], [])

    def test_unknown_follows_the_order_sent(self):
        got = _Probe().schema_gaps({"z": 1, "b": "x", "a": "y", "c": 2})
        assert got == ([], ["z", "c"])

    def test_missing_and_unknown_together(self):
        assert _Probe().schema_gaps({"bb": "x", "a": "y"}) == (["b"], ["bb"])

    def test_complete_call_has_no_gaps(self):
        assert _Probe().schema_gaps({"b": "x", "a": "y", "opt": 3}) == ([], [])

    def test_optional_field_is_known_not_missing(self):
        assert _Probe().schema_gaps({"b": "x", "a": "y"}) == ([], [])

    def test_present_but_empty_is_not_missing(self):
        """존재 여부만 본다 — 빈 값의 판정은 검증의 뒤 단계(minLength·의미) 소관."""
        assert _Probe().schema_gaps({"b": "", "a": None}) == ([], [])

    @pytest.mark.parametrize("bad", ["a string", None, 3, 1.5, True, ["x"], ("b",)])
    def test_non_object_input_has_no_gaps(self, bad):
        assert _Probe().schema_gaps(bad) == ([], [])

    def test_input_is_not_mutated(self):
        sent = {"zz": 1}
        _Probe().schema_gaps(sent)
        assert sent == {"zz": 1}

    def test_schema_without_required_or_properties(self):
        class Bare(_Probe):
            parameters: ClassVar[dict] = {"type": "object"}

        assert Bare().schema_gaps({"x": 1}) == ([], ["x"])
        assert Bare().schema_gaps({}) == ([], [])


class TestEveryRegisteredTool:
    """등록된 도구 전부: 메서드의 답과 중앙 검증의 판정이 같다."""

    @pytest.mark.parametrize("name", sorted(TOOLS))
    def test_empty_call_reports_exactly_the_required_fields(self, name):
        tool = TOOLS[name]
        required = list(tool.parameters.get("required", []))
        assert tool.schema_gaps({}) == (required, [])

    @pytest.mark.parametrize("name", sorted(TOOLS))
    def test_validation_rejects_iff_the_tool_reports_missing(self, name):
        tool = TOOLS[name]
        missing, _ = tool.schema_gaps({})
        ok, err, _ = validate_tool_input(name, {})
        if missing:
            assert not ok
            assert err.startswith(f"Missing required field(s) for `{name}`: ")
            for field in missing:
                assert f"`{field}` (" in err
        else:
            assert err is None or "Missing required field" not in err

    @pytest.mark.parametrize("name", sorted(TOOLS))
    def test_every_schema_property_is_known(self, name):
        tool = TOOLS[name]
        props = dict.fromkeys(tool.parameters.get("properties", {}), "x")
        assert tool.schema_gaps(props) == ([], [])

    @pytest.mark.parametrize("name", sorted(TOOLS))
    def test_a_made_up_key_is_unknown(self, name):
        _, unknown = TOOLS[name].schema_gaps({"__nope__": 1})
        assert unknown == ["__nope__"]

    @pytest.mark.parametrize("name", sorted(TOOLS))
    def test_flat_op_and_wrapped_op_give_the_same_answer(self, name):
        """dispatch 는 감싼 뒤의 인자로 묻고 모델이 쓴 인자를 보여 준다 — 내장 도구는
        flat-native 라 두 모양의 답이 같아야 요약의 이름과 값이 어긋나지 않는다."""
        tool = TOOLS[name]
        for flat in ({}, {"__nope__": 1}):
            assert tool.schema_gaps(tool.wrap_single_op(dict(flat))) == (
                tool.schema_gaps(flat)
            )


class TestValidationReadsTheMethod:
    def test_missing_message_names_the_fields_the_method_returned(self, monkeypatch):
        """검증의 '빠진 필수' 판정은 메서드 한 곳 — 재정의가 검증에도 닿는다."""
        monkeypatch.setattr(
            type(TOOLS["shell"]),
            "schema_gaps",
            lambda self, action_input: (["command"], []),
        )
        ok, err, _ = validate_tool_input("shell", {"command": "ls"})
        assert not ok and "Missing required field(s) for `shell`: `command`" in err

    def test_unknown_keys_alone_do_not_fail_validation(self):
        ok, err, _ = validate_tool_input("shell", {"command": "ls", "zzz": 1})
        assert ok and err is None

    def test_known_real_cases(self):
        assert TOOLS["shell"].schema_gaps({"cmd": "ls"}) == (["command"], ["cmd"])
        missing, unknown = TOOLS["edit_file"].schema_gaps(
            {"op": "replace", "pos": "1#TV", "end": "1#TV", "lines": ["x"]}
        )
        assert missing == ["path"] and unknown == []
        assert TOOLS["memory"].schema_gaps({"summary": "s", "tag": "t"}) == (
            ["mode"],
            [],
        )


class TestOverrides:
    def test_ask_knows_the_resident_only_key(self):
        """상주 에이전트는 `to` 를 정당하게 보낸다(프롬프트의 스키마 교체) — 공용
        스키마엔 없지만 '모르는 키' 가 아니다."""
        ask = TOOLS["ask"]
        assert "to" not in ask.parameters["properties"]
        assert ask.schema_gaps({"question": "q", "to": "user"}) == ([], [])
        assert ask.schema_gaps({"to": "user"}) == (["question"], [])
        assert ask.schema_gaps({"question": "q", "whom": "user"}) == ([], ["whom"])

    def test_a_batch_shaped_tool_can_answer_in_flat_terms(self):
        class Batch(_Probe):
            name = "batch"
            parameters: ClassVar[dict] = {
                "type": "object",
                "properties": {"items": {"type": "array"}},
                "required": ["items"],
            }

            def wrap_single_op(self, flat: dict) -> dict:
                return flat if "items" in flat else {"items": [flat]}

            def schema_gaps(self, action_input):
                items = (
                    action_input.get("items")
                    if isinstance(action_input, dict)
                    else None
                )
                first = items[0] if items else {}
                return ([] if "path" in first else ["path"]), []

        t = Batch()
        assert t.schema_gaps(t.wrap_single_op({"mode": "x"})) == (["path"], [])
        assert t.schema_gaps(t.wrap_single_op({"path": "a"})) == ([], [])


class TestLateRegisteredTools:
    def test_mcp_tool_gets_the_default(self):
        from agent_cli.mcp.adapter import McpTool

        tool = McpTool(
            manager=None,
            server="srv",
            tool_name="search",
            description="d",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
                "required": ["query"],
            },
        )
        assert tool.schema_gaps({"q": "x"}) == (["query"], ["q"])
        assert tool.schema_gaps({"query": "x", "k": 3}) == ([], [])


class TestNoDuplicate:
    def test_registry_has_no_free_function(self):
        assert not hasattr(registry, "schema_gaps")
