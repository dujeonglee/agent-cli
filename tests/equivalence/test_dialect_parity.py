"""스펙 구동 방언 vs 옛 손 코딩 모듈 — 등가성 합격선 (PHASE5.md §7).

옛 모듈은 ``agent_cli/wire_formats/_legacy/`` 에 등록 없이 남아 있고(S5 에서 삭제),
이 테스트가 코퍼스(``tests/equivalence/corpus/<name>.jsonl``) 전건과 표면 전체를
바이트 단위로 비교한다. 차이가 하나라도 나면 어느 쪽이 맞는지 §10 에 올려
결정한 뒤 테스트로 고정한다 — 허용 차이 목록은 비어 있어야 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.tools.registry import (
    TOOL_SCHEMAS,
    allows_extra_keys,
    effective_tool_names,
    flat_param_schemas,
)
from agent_cli.tools.virtual import AskTool
from agent_cli.wire_formats import get

CORPUS = Path(__file__).parent / "corpus"


def _legacy(name: str):
    if name == "xml_fc":
        from agent_cli.wire_formats._legacy.xml_fc import XmlFcFormat

        return XmlFcFormat()
    if name == "json_fc":
        from agent_cli.wire_formats._legacy.json_fc import JsonFcFormat

        return JsonFcFormat()
    pytest.skip(f"no legacy module for {name}")


def _corpus(name: str) -> list[str]:
    p = CORPUS / f"{name}.jsonl"
    if not p.is_file():
        return []
    return [json.loads(l)["text"] for l in p.open(encoding="utf-8") if l.strip()]


def _tools(wf):
    out = []
    for n in effective_tool_names(None, wf):
        p = AskTool.RESIDENT_PARAMETERS if n == "ask" else TOOL_SCHEMAS[n].parameters
        out.append((n, flat_param_schemas(n, p), allows_extra_keys(p)))
    return out


def _turn_key(t):
    return (
        t.thought,
        [(o.action, o.action_input, o.truncated) for o in t.ops],
        t.terminal,
        t.raw,
        t.parse_stage,
        t.thinking,
    )


def _action_key(a):
    return (
        a.thought,
        a.action,
        a.action_input,
        a.raw,
        a.parse_stage,
        a.thinking,
        a.truncated,
    )


PARITY_FORMATS = ["xml_fc", "json_fc"]


@pytest.mark.parametrize("name", PARITY_FORMATS)
class TestParity:
    def test_corpus_has_rows(self, name):
        assert len(_corpus(name)) > 100

    def test_parse_turn_byte_identical(self, name):
        new, old = get(name), _legacy(name)
        diffs = []
        for text in _corpus(name):
            a, b = _turn_key(old.parse_turn(text)), _turn_key(new.parse_turn(text))
            if a != b:
                diffs.append((text[:120], a[:2], b[:2]))
        assert not diffs, f"{len(diffs)} differing rows; first: {diffs[0]}"

    def test_parse_projection_identical(self, name):
        new, old = get(name), _legacy(name)
        for text in _corpus(name):
            assert _action_key(old.parse(text)) == _action_key(new.parse(text)), text[
                :120
            ]

    def test_history_serialize_identical(self, name):
        new, old = get(name), _legacy(name)
        for text in _corpus(name):
            assert old.serialize_assistant_for_history(
                text
            ) == new.serialize_assistant_for_history(text), text[:120]

    def test_history_render_roundtrip_identical(self, name):
        new, old = get(name), _legacy(name)
        for text in _corpus(name):
            rec = old.serialize_assistant_for_history(text)
            assert old.render_assistant_from_history(
                rec
            ) == new.render_assistant_from_history(rec), text[:120]
        term = old.serialize_terminal_for_history("done", "result", ["r1"])
        assert term == new.serialize_terminal_for_history("done", "result", ["r1"])
        assert term == new.serialize_terminal_for_history("done", "result", ["r1"])

    def test_degenerate_and_sanitize_identical(self, name):
        new, old = get(name), _legacy(name)
        for text in _corpus(name):
            assert old.is_degenerate(text) == new.is_degenerate(text), text[:120]
            assert old.sanitize_thought(text) == new.sanitize_thought(text), text[:120]
            assert old.diagnose_syntax_error(text) == new.diagnose_syntax_error(text), (
                text[:120]
            )
        assert old.degeneration_trigger == new.degeneration_trigger
        assert old.thinking_stop.pattern == new.thinking_stop.pattern

    def test_prose_byte_identical(self, name):
        new, old = get(name), _legacy(name)
        assert old.format_rules() == new.format_rules()
        for m in (
            "constraint_reminder_call",
            "constraint_reminder_action_required",
            "failure_framing_parse_fail",
            "failure_framing_no_action",
            "no_action_detail",
            "static_retry_hint_no_json",
            "static_retry_hint_no_action",
            "system_user_prefixes",
        ):
            assert getattr(old, m)() == getattr(new, m)(), m
        assert (old.name, old.multi_op, old.action_required, old.exposes_complete) == (
            new.name,
            new.multi_op,
            new.action_required,
            new.exposes_complete,
        )

    def test_render_identical(self, name):
        new, old = get(name), _legacy(name)
        samples = [
            {"read_file_path": "src/a.c"},
            {"write_file_path": "a", "write_file_content": "l1\nl2"},
            {"shell_command": "ls -la", "shell_timeout": 30},
            {"result": "done"},
            "bare",
            {},
        ]
        for s in samples:
            assert old.render_action_input(s) == new.render_action_input(s), s
        for th, act, inp in [
            ("t", "read_file", "<parameter=path>x</parameter>"),
            (None, "complete", ""),
            (
                "t",
                "shell",
                "<function=shell>\n<parameter=command>ls</parameter>\n</function>",
            ),
        ]:
            assert old.render_full_example(
                thought=th, action=act, action_input=inp
            ) == new.render_full_example(thought=th, action=act, action_input=inp)

    def test_grammar_byte_identical(self, name):
        new, old = get(name), _legacy(name)
        tools = _tools(old)
        for open_ in (False, True):
            assert old.grammar(tools, thinking_open=open_) == new.grammar(
                tools, thinking_open=open_
            ), f"thinking_open={open_}"
        # 자유 스키마 도구(extra keys)와 enum/required 조합도
        extra = [("mcp_x", {"q": ({"type": "string", "minLength": 1}, True)}, True)]
        enum = [
            (
                "pick",
                {
                    "mode": ({"type": "string", "enum": ["a", "b"]}, True),
                    "n": ({"type": "integer"}, False),
                },
                False,
            )
        ]
        assert old.grammar(tools + extra + enum) == new.grammar(tools + extra + enum)
