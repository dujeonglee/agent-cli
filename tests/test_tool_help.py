"""도구 설명 지연 로딩의 기반 (v10.109.0, docs/tool-help/DESIGN.md).

이 PR 에서는 어떤 도구도 지연하지 않는다 — 모델이 보는 글은 종전과 같다(종전 코드와의
바이트 비교는 inventory 를 두 커밋에서 떠서 대조했다; 아래는 그 불변을 지키는 자기
비교). 지연 경로는 가짜로 ``deferred=True`` 를 켠 도구로 검증한다.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli import dialects as D
from agent_cli.loop.dispatch import FULL_GUIDE_HEAD
from agent_cli.prompts.system_prompt import (
    _build_tools_section,
    function_schemas_for,
    tool_guide_text,
)
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools import registry as R
from agent_cli.tools.base import RunContext
from agent_cli.tools.registry import _BUILTIN_ORDER, TOOLS
from agent_cli.tools.tool_help import HEADER, NO_GUIDE
from tests.loop_ports import TEST_PORTS

ALL = list(_BUILTIN_ORDER)
SUMMARY = "Use it to watch a file or a command and be told when a condition fires."


def _defer(monkeypatch, name, summary=SUMMARY):
    monkeypatch.setattr(TOOLS[name], "deferred", True, raising=False)
    monkeypatch.setattr(TOOLS[name], "summary", summary, raising=False)


#: 결정 D1 (사용자 2026-11-05) — 세션의 5% 이하에서만 불린 도구 11개.
D1 = {
    "agent", "code_index", "monitor", "browser", "schedule", "history",
    "fetch", "snapshot", "memory", "message", "reply",
}  # fmt: skip


class TestDeferredSet:
    """v10.110.0 (PR 2): D1 열한 개가 지연, 나머지는 전문."""

    def test_exactly_d1_is_deferred(self):
        assert {n for n, t in TOOLS.items() if t.deferred} == D1

    @pytest.mark.parametrize("name", sorted(D1))
    def test_summary_is_one_short_sentence_in_the_notation(self, name):
        """요약은 "언제 쓰는가" 한 문장(10~40 낱말), 표기 규칙 — 대문자 강조 없음.
        상한 40: 처음엔 25 였는데 v10.110.0 측정에서 monitor 가 요약만 보고 조건 짝을
        섞어 첫 호출이 거절됐다 — 세 짝(`file` + `pattern`·`file` + `idle`·`command` +
        `every`)을 이름으로 넣은 요약이 36 낱말(`+`·`—` 포함)이다.
        도구 이름·호출 모양은 inventory 의 지연 뷰가 프롬프트 절에 실리므로 표기
        가드(tests/test_prompt_notation.py)가 본다."""
        from tests.test_prompt_notation import caps_emphasis

        s = TOOLS[name].summary
        assert s.endswith(".") and "\n" not in s
        assert s[:-1].count(". ") == 0, s  # 한 문장
        assert 10 <= len(s.split()) <= 40, (name, len(s.split()))
        assert not caps_emphasis(s), s

    def test_tool_help_sits_right_before_complete(self):
        assert ALL[-2:] == ["tool_help", "complete"]

    def test_tool_view_is_the_raw_tool_for_the_rest(self):
        for name in ALL:
            if name in D1:
                continue
            desc, params, guide = R.tool_view(name)
            assert desc == TOOLS[name].description
            assert params is TOOLS[name].parameters and guide == ""


class TestMainPromptIsDeferred:
    """main 시나리오 프롬프트에 `tool_help` 가 광고되고 지연 도구 줄은 요약 + 뼈대 —
    native `tools[]` 도 같다."""

    @pytest.mark.parametrize("dn", ["xml_fc", "json_fc"])
    def test_tools_section(self, dn):
        from agent_cli.prompts import inventory as inv

        sc = inv._scenario(D.get(dn), inv.SCENARIOS[0])
        tools = next(
            s["text"] for s in sc["sections"] if s["name"] == "Available Tools"
        )
        assert "\n- tool_help: " in tools
        for name in D1:
            line = next(ln for ln in tools.split("\n") if ln.startswith(f"- {name}: "))
            assert line == f"- {name}: {TOOLS[name].summary}", name
        # agent 의 안내 블록(`run`·`spawn` 예시)은 실리지 않는다
        assert "Persistent:" not in tools
        assert TOOLS["code_index"].description not in tools

    def test_native_tools(self):
        fns = {
            f["function"]["name"]: f["function"]
            for f in function_schemas_for(ALL, D.get("native_fc"))
        }
        assert "tool_help" in fns
        for name in D1:
            assert fns[name]["description"] == TOOLS[name].summary
            assert "description" not in json.dumps(fns[name]["parameters"]).replace(
                '"description": {', ""
            )
        assert fns["shell"]["description"] == TOOLS["shell"].description


class TestDeferredSchema:
    def test_drops_only_descriptions_and_keeps_the_shape(self):
        schema = {
            "type": "object",
            "description": "top",
            "properties": {
                "mode": {"type": "string", "enum": ["a", "b"], "description": "m"},
                # 인자 이름이 "description" 이어도 인자는 남는다 — 값만 내려간다
                "description": {"type": "string", "description": "a field"},
                "ops": {
                    "type": "array",
                    "description": "list",
                    "items": {
                        "type": "object",
                        "description": "one op",
                        "properties": {
                            "path": {
                                "type": "string",
                                "minLength": 1,
                                "description": "p",
                            }
                        },
                        "required": ["path"],
                    },
                },
                "any": {"anyOf": [{"type": "string", "description": "s"}]},
            },
            "required": ["mode"],
        }
        out = R.deferred_schema(schema)
        assert "description" not in json.dumps(out).replace('"description": {', "")
        assert out["required"] == ["mode"]
        assert out["properties"]["mode"] == {"type": "string", "enum": ["a", "b"]}
        assert out["properties"]["description"] == {"type": "string"}
        item = out["properties"]["ops"]["items"]
        assert item["required"] == ["path"]
        assert item["properties"]["path"] == {"type": "string", "minLength": 1}
        assert out["properties"]["any"] == {"anyOf": [{"type": "string"}]}
        assert schema["properties"]["mode"]["description"] == "m"  # 원본은 그대로

    def test_enum_values_render_from_the_schema_when_there_is_no_description(self):
        prop = {"type": "string", "enum": ["add", "list", "delete"]}
        assert (
            R.render_param_value(prop, True)
            == 'string, required — "add" | "list" | "delete"'
        )
        # 설명이 있으면 종전 그대로 — 값 목록은 설명이 이미 말한다
        prop["description"] = "what to do"
        assert R.render_param_value(prop, True) == "string, required — what to do"


class TestDeferredView:
    @staticmethod
    def _per_tool(dialect):
        """도구마다 ``(목록 줄, 안내)`` — 절은 목록 줄들 + 안내들 그대로여야 한다."""
        from agent_cli.prompts.system_prompt import _build_tool_inline_guides

        guides = _build_tool_inline_guides(ALL, dialect)
        names = R.effective_tool_names(ALL, dialect)
        blocks = {
            n: R.render_tool_block(n, dialect=dialect, inline_guides=guides)
            for n in names
        }
        section = _build_tools_section(ALL, dialect)
        assert section == "## Available Tools\n" + "\n".join(
            line for line, _ in blocks.values()
        ) + "".join(g for _, g in blocks.values())
        return blocks

    @pytest.mark.parametrize("dn", ["xml_fc", "json_fc"])
    def test_only_the_deferred_tool_changes(self, monkeypatch, full_tool_text, dn):
        """전문 판에서 지연을 켜면 그 도구의 목록 줄(요약·뼈대)과 안내만 바뀌고 다른
        도구의 글은 바이트 그대로 — `agent` 의 안내 블록은 빠진다."""
        d = D.get(dn)
        before = self._per_tool(d)
        for name in ("monitor", "agent"):
            _defer(monkeypatch, name)
        after = self._per_tool(d)
        assert [n for n in before if before[n] != after[n]] == ["monitor", "agent"]
        assert before["agent"][1] and after["agent"][1] == ""  # 안내가 빠진다
        line = after["monitor"][0]
        assert line.startswith(f"- monitor: {SUMMARY}\n  Input JSON: ")
        params = json.loads(line.split("Input JSON: ", 1)[1])
        full = json.loads(before["monitor"][0].split("Input JSON: ", 1)[1])
        # 뼈대 (V2 — v10.110.0 측정으로 확정): 이름·타입·필수·enum 값만
        assert params["mode"] == 'string, required — "add" | "list" | "delete"'
        assert params["file"] == "string"
        assert list(params) == list(full)  # 인자 이름·순서는 그대로

    def test_native_tools_get_the_same_summary_and_skeleton(self, monkeypatch):
        _defer(monkeypatch, "agent")
        fns = {
            f["function"]["name"]: f["function"]
            for f in function_schemas_for(ALL, D.get("native_fc"))
        }
        assert fns["agent"]["description"] == SUMMARY  # 안내 없음
        assert fns["agent"]["parameters"] == R.deferred_schema(
            TOOLS["agent"].parameters
        )
        assert fns["shell"]["description"] == TOOLS["shell"].description

    def test_a_deferred_tool_without_summary_fails_at_registration(self, monkeypatch):
        _defer(monkeypatch, "fetch", summary="  ")
        with pytest.raises(ValueError, match="deferred but has no summary"):
            R.check_deferred(TOOLS["fetch"])


class TestToolHelp:
    def _ctx(self, dn="xml_fc", names=ALL):
        return RunContext(
            tools_available=frozenset(names),
            tool_guide=lambda n: tool_guide_text(n, list(names), D.get(dn)),
        )

    def test_returns_the_prompt_block_of_a_deferred_tool(self, monkeypatch):
        """지연 도구도 전문 — 지연을 끈 프롬프트에 실렸을 바로 그 블록."""
        full = tool_guide_text("agent", ALL, D.get("xml_fc"))
        _defer(monkeypatch, "agent")
        out = TOOLS["tool_help"]._run({"name": "agent"}, ctx=self._ctx())
        assert out.success
        assert out.output == f"{HEADER.format(name='agent')}\n{full}"
        assert full.startswith(f"- agent: {TOOLS['agent'].description}")
        assert SUMMARY not in out.output

    def test_a_tool_that_is_not_deferred_gets_the_same_block(self):
        out = TOOLS["tool_help"]._run({"name": "shell"}, ctx=self._ctx())
        line, guide = R.render_tool_block("shell", dialect=D.get("xml_fc"))
        assert out.output == f"{HEADER.format(name='shell')}\n{line}{guide}"
        assert line in _build_tools_section(ALL, D.get("xml_fc"))

    def test_unknown_name_lists_this_loops_tools(self):
        out = TOOLS["tool_help"]._run(
            {"name": "grep"}, ctx=self._ctx(names=["read_file", "shell"])
        )
        assert not out.success
        assert out.error == (
            'No tool named "grep" in this loop. Tools: `read_file`, `shell`, `complete`'
        )

    def test_without_a_loop_binding(self):
        out = TOOLS["tool_help"]._run({"name": "shell"}, ctx=RunContext())
        assert not out.success and out.error == NO_GUIDE


def _loop(active=None, **kw):
    from agent_cli.loop import AgentLoop

    return AgentLoop(
        query="Q",
        provider=MagicMock(),
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        active_tools=active,
        ports=TEST_PORTS,
        **kw,
    )


class TestAdvertised:
    def test_advertised_in_a_main_loop(self):
        """v10.110.0: 전체 도구의 루프엔 지연 도구가 있으니 `tool_help` 가 붙는다."""
        assert "tool_help" in _loop().tools_list

    def test_not_advertised_without_deferred_tools(self):
        assert "tool_help" not in _loop(active=["read_file", "shell"]).tools_list

    def test_mounted_when_a_deferred_tool_remains(self, monkeypatch):
        """allowed-tools 에 없어도 붙는다(force_mount) — 지연 도구가 남은 루프면."""
        _defer(monkeypatch, "fetch")
        assert "tool_help" in _loop(active=["read_file", "fetch"]).tools_list
        # 지연 도구가 목록에 없으면 붙지 않는다
        assert "tool_help" not in _loop(active=["read_file", "shell"]).tools_list

    def test_the_loop_binds_its_own_guide(self):
        loop = _loop()
        guide = loop._tools._run_ctx().tool_guide("shell")
        assert guide == tool_guide_text("shell", loop.tools_list, loop.dialect)


class TestA5FullGuideOnce:
    def test_first_schema_rejection_of_a_deferred_tool_carries_the_guide(
        self, monkeypatch, tmp_path
    ):
        """뼈대만 보고 틀리게 부른 첫 거절에 전문이 붙고, 두 번째에는 붙지 않는다."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop

        _defer(monkeypatch, "monitor")
        bad = json.dumps({"action": "monitor", "every": "5s"})  # `mode` 누락
        done = json.dumps({"action": "complete", "result": "ok"})
        seen: list[str] = []  # 호출 때의 마지막 메시지 — 목록은 뒤에 제자리 접힌다
        replies = iter([bad, bad, done])

        def call(*a, **kw):
            msgs = kw.get("messages") or a[0]
            seen.append(str(msgs[-1]["content"]))
            return LLMResponse(content=next(replies))

        provider = MagicMock()
        provider.call.side_effect = call
        result = run_loop(
            ports=TEST_PORTS,
            query="watch it",
            provider=provider,
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
            max_turns=6,
        )
        assert result.success

        first, second = seen[1], seen[2]
        assert "Missing required field" in first
        assert FULL_GUIDE_HEAD in first and TOOLS["monitor"].description in first
        assert "Missing required field" in second
        assert FULL_GUIDE_HEAD not in second

    def test_a_tool_that_is_not_deferred_never_gets_it(self, monkeypatch, tmp_path):
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop

        bad = json.dumps({"action": "write_file", "path": "x.txt"})  # `content` 누락
        done = json.dumps({"action": "complete", "result": "ok"})
        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=c) for c in (bad, done)]
        run_loop(
            ports=TEST_PORTS,
            query="watch it",
            provider=provider,
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
            max_turns=6,
        )
        msgs = provider.call.call_args_list[1].kwargs["messages"]
        assert FULL_GUIDE_HEAD not in str(msgs[-1]["content"])
