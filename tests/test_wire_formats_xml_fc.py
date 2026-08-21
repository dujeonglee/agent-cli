"""xml_fc (태그-파라미터) wire format — docs/multi-wire-format/PHASE2.md.

파서 3-stage(정상/수리/보존)·스키마-주도 타입 강제·raw 값·구분자 충돌·
히스토리 round-trip·recovery 문구·degenerate·sanitize 를 고정한다.
D4: thought = 첫 <tool_call> 앞 자유 산문 (<think> 는 stage 0 격리).
"""

import pytest

from agent_cli.wire_formats import get as get_wf
from agent_cli.wire_formats.xml_fc import XmlFcFormat


@pytest.fixture
def wf():
    return XmlFcFormat()


def _call(tool: str, params: dict) -> str:
    lines = [f"<function={tool}>"]
    for k, v in params.items():
        lines.append(f"<parameter={k}>{v}</parameter>")
    lines.append("</function>")
    return "<tool_call>\n" + "\n".join(lines) + "\n</tool_call>"


# ── 등록·플래그 ──────────────────────────────────────────────


class TestRegistration:
    def test_registered_as_builtin(self):
        assert get_wf("xml_fc") is not None
        assert get_wf("xml_fc").name == "xml_fc"

    def test_flags_mirror_json_fc(self, wf):
        # multi-op flat 계열 공통 플래그 (json_fc 동형)
        assert wf.multi_op is True
        assert wf.thought_required is False
        assert wf.action_required is False
        assert wf.exposes_complete is True


# ── 정상 파싱 (stage 1) ──────────────────────────────────────


class TestParseHappyPath:
    def test_single_call_with_prose_thought(self, wf):
        turn = wf.parse_turn(
            "파일부터 읽는다.\n\n" + _call("read_file", {"path": "src/main.c"})
        )
        assert turn.parse_stage == 1
        assert turn.thought == "파일부터 읽는다."
        assert len(turn.ops) == 1
        assert turn.ops[0].action == "read_file"
        assert turn.ops[0].action_input == {"path": "src/main.c"}
        assert turn.terminal is False

    def test_multi_op_two_blocks(self, wf):
        turn = wf.parse_turn(
            _call("read_file", {"path": "a.py"})
            + "\n"
            + _call("shell", {"command": "ls -la"})
        )
        assert turn.parse_stage == 1
        assert [op.action for op in turn.ops] == ["read_file", "shell"]
        assert turn.thought is None

    def test_complete_with_raw_multiline_result(self, wf):
        # 이 포맷의 존재 이유: JSON escaping 없이 raw 본문.
        result_body = 'line one\nline "two" with quotes\n  indented \\ backslash'
        turn = wf.parse_turn(
            "done.\n\n<tool_call>\n<function=complete>\n"
            f"<parameter=result>\n{result_body}\n</parameter>\n"
            "</function>\n</tool_call>"
        )
        assert turn.parse_stage == 1
        assert turn.ops[0].action == "complete"
        assert turn.ops[0].action_input["result"] == result_body

    def test_block_style_trims_exactly_one_newline_each_side(self, wf):
        turn = wf.parse_turn(
            "<tool_call>\n<function=write_file>\n"
            "<parameter=path>a.txt</parameter>\n"
            "<parameter=content>\n\nkeep this leading blank\n\n</parameter>\n"
            "</function>\n</tool_call>"
        )
        # 블록 스타일 개행 1개씩만 트림 — 내부 공백 보존
        assert turn.ops[0].action_input["content"] == "\nkeep this leading blank\n"

    def test_empty_param_function_yields_op(self, wf):
        turn = wf.parse_turn(
            "<tool_call>\n<function=read_context>\n</function>\n</tool_call>"
        )
        assert turn.ops[0].action == "read_context"
        assert turn.ops[0].action_input == {}

    def test_no_blocks_thought_only_is_zero_op_stage1(self, wf):
        turn = wf.parse_turn("생각만 하고 아무 호출도 안 했다.")
        assert turn.parse_stage == 1
        assert turn.ops == []
        assert "생각만" in turn.thought

    def test_blank_emission_is_stage0(self, wf):
        turn = wf.parse_turn("   \n  ")
        assert turn.parse_stage == 0
        assert turn.ops == []


# ── 타입 강제 (스키마-주도) ──────────────────────────────────


class TestTypeCoercion:
    def test_integer_param_coerced(self, wf):
        turn = wf.parse_turn(
            _call("read_file", {"path": "a.py", "line_start": "10", "line_end": "20"})
        )
        assert turn.ops[0].action_input["line_start"] == 10
        assert turn.ops[0].action_input["line_end"] == 20

    def test_string_param_with_digits_stays_string(self, wf):
        turn = wf.parse_turn(_call("read_file", {"path": "123"}))
        assert turn.ops[0].action_input["path"] == "123"

    def test_string_param_with_jsonish_text_stays_raw(self, wf):
        turn = wf.parse_turn(
            _call("write_file", {"path": "a.json", "content": '{"k": 1}'})
        )
        assert turn.ops[0].action_input["content"] == '{"k": 1}'

    def test_bad_typed_value_kept_raw_for_schema_mismatch(self, wf):
        # 강제 실패 → raw 유지, 진단은 기존 A5 경로 소관
        turn = wf.parse_turn(_call("read_file", {"path": "a.py", "line_start": "ten"}))
        assert turn.ops[0].action_input["line_start"] == "ten"

    def test_unknown_tool_params_stay_raw(self, wf):
        turn = wf.parse_turn(_call("no_such_tool", {"x": "1"}))
        assert turn.ops[0].action_input["x"] == "1"


# ── 수리 (stage 2) · 보존 (stage 3 불변식) ───────────────────


class TestRepairAndPreservation:
    def test_bare_function_without_wrapper_is_drift(self, wf):
        turn = wf.parse_turn(
            "<function=read_file>\n<parameter=path>a.py</parameter>\n</function>"
        )
        assert turn.parse_stage == 2
        assert turn.ops[0].action == "read_file"
        assert turn.ops[0].action_input == {"path": "a.py"}

    def test_eof_truncation_missing_closers_recovers(self, wf):
        # </tool_call>·</function> 미닫힘 — op 은 살린다
        turn = wf.parse_turn(
            "<tool_call>\n<function=shell>\n<parameter=command>make</parameter>"
        )
        assert turn.parse_stage == 2
        assert turn.ops[0].action == "shell"
        assert turn.ops[0].action_input == {"command": "make"}

    def test_unclosed_parameter_value_runs_to_eof(self, wf):
        turn = wf.parse_turn(
            "<tool_call>\n<function=complete>\n<parameter=result>\ncut off mid-answ"
        )
        assert turn.parse_stage == 2
        assert turn.ops[0].truncated is True
        assert turn.ops[0].action_input["result"] == "cut off mid-answ"

    def test_closerless_style_all_params_recovered(self, wf):
        # 모델이 </parameter> 를 전부 생략하는 변종
        turn = wf.parse_turn(
            "<tool_call>\n<function=read_file>\n"
            "<parameter=path>a.py\n<parameter=line_start>3\n"
            "</function>\n</tool_call>"
        )
        assert turn.ops[0].action_input["path"] == "a.py"
        assert turn.ops[0].action_input["line_start"] == 3
        assert turn.parse_stage == 2

    def test_orphan_closer_inside_value_preserved(self, wf):
        # 구분자 충돌 (§5.3): 값 속 고아 </parameter> 는 구조 토큰이 안 따라오면 값
        body = "text mentioning </parameter> mid-value continues"
        turn = wf.parse_turn(
            "<tool_call>\n<function=write_file>\n"
            "<parameter=path>doc.md</parameter>\n"
            f"<parameter=content>{body}</parameter>\n"
            "</function>\n</tool_call>"
        )
        assert turn.ops[0].action_input["content"] == body

    def test_keyname_closer_in_canonical_block(self, wf):
        # 실전 (board 세션 2026-07-17): 캐노니컬 블록 안에서
        # <parameter=path>…</path> (키-이름 closer) — strict 미닫힘-복구가
        # </path> 를 값에 포함시켜 ENOENT ×3 발생했던 오염 케이스.
        turn = wf.parse_turn(
            "<tool_call>\n<function=read_file>\n"
            "<parameter=path>/ws/linked_list.h</path>\n"
            "</function>\n</tool_call>"
        )
        assert turn.ops[0].action_input == {"path": "/ws/linked_list.h"}

    def test_keyname_closer_multi_param_mixed(self, wf):
        turn = wf.parse_turn(
            "<tool_call>\n<function=read_file>\n"
            "<parameter=path>a.c</path>\n"
            "<parameter=line_start>3</parameter>\n"
            "</function>\n</tool_call>"
        )
        assert turn.ops[0].action_input == {"path": "a.c", "line_start": 3}

    def test_keyname_closer_multi_op_batch(self, wf):
        # 실전 shape: 한 턴 3개 read_file 배치 전부 키-이름 closer
        turn = wf.parse_turn(
            "\n".join(
                "<tool_call>\n<function=read_file>\n"
                f"<parameter=path>/ws/{f}</path>\n"
                "</function>\n</tool_call>"
                for f in ("linked_list.h", "linked_list.c", "main.c")
            )
        )
        assert [op.action_input["path"] for op in turn.ops] == [
            "/ws/linked_list.h",
            "/ws/linked_list.c",
            "/ws/main.c",
        ]

    def test_orphan_keyname_tag_mid_value_still_preserved(self, wf):
        # 충돌 규칙 유지: 값 속 </path> 유사 텍스트는 구조 토큰이 안
        # 따라오면 값 (lookahead 앵커가 계속 보호)
        body = "mentions </path> mid-value and continues"
        turn = wf.parse_turn(
            "<tool_call>\n<function=write_file>\n"
            "<parameter=path>doc.md</parameter>\n"
            f"<parameter=content>{body}</parameter>\n"
            "</function>\n</tool_call>"
        )
        assert turn.ops[0].action_input["content"] == body

    def test_empty_function_name_preserves_params(self, wf):
        # parse 불변식: action 무효여도 action_input 보존 (infer/NO_ACTION echo)
        turn = wf.parse_turn(
            "<tool_call>\n<function=>\n<parameter=path>a.py</parameter>\n</function>\n</tool_call>"
        )
        assert turn.ops, "params-carrying op must be preserved"
        assert turn.ops[0].action is None
        assert turn.ops[0].action_input == {"path": "a.py"}
        assert turn.parse_stage >= 1


# ── stage 0 — thinking 격리 (D4) ─────────────────────────────


class TestStage0Thinking:
    def test_leading_think_block_isolated(self, wf):
        turn = wf.parse_turn(
            "<think>internal scratch</think>\nreal thought\n\n"
            + _call("read_file", {"path": "a.py"})
        )
        assert turn.thinking == "internal scratch"
        assert turn.thought == "real thought"
        assert turn.ops[0].action == "read_file"

    def test_think_block_containing_tag_like_text_does_not_confuse(self, wf):
        # think 산문이 <tool_call> 유사 텍스트를 담아도 stage 0 이 먼저 격리
        turn = wf.parse_turn(
            "<think>maybe emit <tool_call> for shell?</think>\n"
            + _call("read_file", {"path": "b.py"})
        )
        assert len(turn.ops) == 1
        assert turn.ops[0].action == "read_file"
        assert "<tool_call>" in turn.thinking


# ── 렌더링·round-trip·히스토리 ───────────────────────────────


class TestRenderAndHistory:
    def test_render_full_example_shape(self, wf):
        out = wf.render_full_example(
            thought="reason",
            action="read_file",
            action_input="<parameter=path>a.py</parameter>",
        )
        assert out.startswith("reason")
        assert "<tool_call>" in out and "</tool_call>" in out
        assert "<function=read_file>" in out

    def test_render_action_input_unprefixes_to_tag_call(self, wf):
        # 가이드는 wire-key prefixed dict 를 넘긴다 (json_fc 동형 계약)
        out = wf.render_action_input({"read_file_path": "src/a.py"})
        assert "<function=read_file>" in out
        assert "<parameter=path>src/a.py</parameter>" in out
        assert "read_file_path" not in out

    def test_serialize_ops_record_shape_matches_json_fc(self, wf):
        rec = wf.serialize_assistant_for_history(
            "t.\n\n" + _call("read_file", {"path": "a.py"})
        )
        assert rec["role"] == "assistant"
        assert rec["thought"] == "t."
        assert rec["ops"] == [{"action": "read_file", "action_input": {"path": "a.py"}}]

    def test_terminal_record_ops_shape(self, wf):
        rec = wf.serialize_terminal_for_history("done", "final answer")
        assert rec["ops"] == [
            {"action": "complete", "action_input": {"result": "final answer"}}
        ]

    def test_history_round_trip(self, wf):
        raw = "생각.\n\n" + _call("write_file", {"path": "x.txt", "content": "hello"})
        rec = wf.serialize_assistant_for_history(raw)
        msg = wf.render_assistant_from_history(rec)
        reparsed = wf.parse_turn(msg["content"])
        assert reparsed.thought == "생각."
        assert reparsed.ops[0].action == "write_file"
        assert reparsed.ops[0].action_input == {"path": "x.txt", "content": "hello"}

    def test_round_trip_multiline_content_block_style(self, wf):
        content = "line1\n\nline3 with </parameter+almost\nend"
        raw = _call("write_file", {"path": "x", "content": "\n" + content + "\n"})
        rec = wf.serialize_assistant_for_history(raw)
        msg = wf.render_assistant_from_history(rec)
        reparsed = wf.parse_turn(msg["content"])
        assert reparsed.ops[0].action_input["content"] == content

    def test_round_trip_typed_params(self, wf):
        raw = _call("read_file", {"path": "a.py", "line_start": "5"})
        rec = wf.serialize_assistant_for_history(raw)
        assert rec["ops"][0]["action_input"]["line_start"] == 5
        msg = wf.render_assistant_from_history(rec)
        reparsed = wf.parse_turn(msg["content"])
        assert reparsed.ops[0].action_input["line_start"] == 5

    def test_legacy_singular_record_falls_back_to_base(self, wf):
        msg = wf.render_assistant_from_history(
            {"thought": "t", "action": "complete", "action_input": {"result": "r"}}
        )
        assert "<function=complete>" in msg["content"]

    def test_degenerate_fallback_serializes_sanitized_content(self, wf):
        rec = wf.serialize_assistant_for_history("prose only, no call")
        assert rec == {"role": "assistant", "content": "prose only, no call"}


# ── sanitize · degenerate · recovery 문구 ────────────────────


class TestGuards:
    def test_sanitize_strips_structural_sentinels_and_orphan_think(self, wf):
        out = wf.sanitize_thought(
            "real reasoning\n<tool_call>\n</function>\n</think>more"
        )
        assert "real reasoning" in out
        assert "<tool_call>" not in out
        assert "</function>" not in out
        assert "</think>" not in out

    def test_sanitize_none_passthrough(self, wf):
        assert wf.sanitize_thought(None) is None

    def test_degenerate_repeated_empty_skeletons(self, wf):
        assert wf.is_degenerate("<tool_call>\n</tool_call>\n<tool_call>\n</tool_call>")
        assert wf.is_degenerate("<tool_call>\n<tool_call>\n<tool_call>")

    def test_not_degenerate_normal_emission(self, wf):
        assert not wf.is_degenerate(_call("read_file", {"path": "a"}))
        assert not wf.is_degenerate("plain prose")

    def test_recovery_wordings_present_and_tag_flavored(self, wf):
        assert "<tool_call>" in wf.constraint_reminder_call()
        assert "complete" in wf.constraint_reminder_action_required()
        assert wf.failure_framing_parse_fail()
        assert wf.failure_framing_no_action()
        assert wf.static_retry_hint_no_json()
        assert wf.static_retry_hint_no_action()

    def test_system_user_prefixes_cover_framings(self, wf):
        prefixes = wf.system_user_prefixes()
        assert any(wf.failure_framing_parse_fail().startswith(p) for p in prefixes)
        assert any(wf.failure_framing_no_action().startswith(p) for p in prefixes)

    def test_format_rules_positive_no_html_ban(self, wf):
        rules = wf.format_rules()
        assert "<tool_call>" in rules
        assert "<parameter=" in rules
        # 이 포맷의 본질이 태그 — HTML 금지 조항이 있으면 자기모순
        assert "NEVER use HTML" not in rules

    def test_format_rules_no_bracketed_prose_placeholders(self, wf):
        # 35B 실측(bakeoff 예4): `<your reasoning>` placeholder 를 모델이
        # 태그로 복창 — 산문 placeholder 는 꺾쇠 없이 (구조 태그만 꺾쇠).
        rules = wf.format_rules()
        assert "<your" not in rules
        assert "<tool name>" not in rules
        assert "<name>" not in rules


# ── cross-format parity (논리 동형) ──────────────────────────


class TestCrossFormatParity:
    def test_same_logical_turn_same_ops_as_json_fc(self):
        xml = get_wf("xml_fc")
        md = get_wf("json_fc")
        x = xml.parse_turn(
            "t\n\n"
            + _call("read_file", {"path": "a.py"})
            + "\n"
            + _call("shell", {"command": "ls"})
        )
        m = md.parse_turn(
            "## Thought\nt\n\n## Action\n"
            '[{"action": "read_file", "path": "a.py"},'
            ' {"action": "shell", "command": "ls"}]'
        )
        assert [(o.action, o.action_input) for o in x.ops] == [
            (o.action, o.action_input) for o in m.ops
        ]

    def test_history_record_shape_parity(self):
        xml = get_wf("xml_fc")
        md = get_wf("json_fc")
        xr = xml.serialize_terminal_for_history("t", "r")
        mr = md.serialize_terminal_for_history("t", "r")
        assert xr == mr  # ops 레코드 shape 는 cross-format 계약


# ── lenient 구제 — tool-name 태그 변종 (35B bakeoff 실측 지배 shape) ──


class TestLenientToolNameTag:
    """2026-07-17 bakeoff: Qwen3.6-35B-A3B 가 `<function=X>` 를 `<X>` 로,
    `<parameter=k>` 를 `<k>` 로 붕괴시키는 변종이 0-op(NO_ACTION) 마찰의
    83%. strict 경로가 0-op 일 때만 lenient 스캔 → stage 2 (drift 신호).
    구제된 턴의 prior 는 캐노니컬 shape 로 재렌더(B→C) — 자기 교정."""

    def test_plain_tag_params_rescued(self, wf):
        # 실측 캡처 그대로 (shell plain-tag, closer 일부 생략)
        turn = wf.parse_turn("<shell>\n<command>head -50 /Users/x/app.py</command>")
        assert turn.parse_stage == 2
        assert turn.ops[0].action == "shell"
        assert turn.ops[0].action_input["command"] == "head -50 /Users/x/app.py"

    def test_mixed_three_styles_rescued(self, wf):
        # 실측 캡처: plain-tag + parameter= 열고 키-이름으로 닫기 + canonical
        turn = wf.parse_turn(
            "<read_file>\n"
            "<path>/Users/x/app.py</path>\n"
            "<parameter=line_start>1</line_start>\n"
            "<parameter=line_end>50</parameter>"
        )
        assert turn.parse_stage == 2
        assert turn.ops[0].action == "read_file"
        assert turn.ops[0].action_input == {
            "path": "/Users/x/app.py",
            "line_start": 1,  # 스키마-주도 coercion 은 lenient 에도 적용
            "line_end": 50,
        }

    def test_canonical_params_wrong_function_tag_rescued(self, wf):
        turn = wf.parse_turn(
            "<read_file>\n<parameter=path>a.py</parameter>\n</read_file>"
        )
        assert turn.parse_stage == 2
        assert turn.ops[0].action_input == {"path": "a.py"}

    def test_bare_tool_tag_yields_empty_op(self, wf):
        # 파라미터 없음 (2/30) — {} op 로 구제 → A5 가 required 누락을 진단
        turn = wf.parse_turn("checking the file.\n<shell>")
        assert turn.parse_stage == 2
        assert turn.ops[0].action == "shell"
        assert turn.ops[0].action_input == {}

    def test_multiple_tool_tags_multi_op(self, wf):
        turn = wf.parse_turn(
            "<read_file>\n<path>a.py</path>\n</read_file>\n"
            "<shell>\n<command>ls</command>\n</shell>"
        )
        assert [op.action for op in turn.ops] == ["read_file", "shell"]
        assert turn.parse_stage == 2

    def test_prose_thought_captured_before_lenient_block(self, wf):
        turn = wf.parse_turn("파일을 확인한다.\n<shell>\n<command>ls</command>")
        assert turn.thought == "파일을 확인한다."
        assert turn.ops[0].action == "shell"

    def test_inline_prose_mention_not_rescued(self, wf):
        # 산문 속 인라인 언급은 구제 대상 아님 (line-anchor 가드) — 0-op 유지
        turn = wf.parse_turn(
            "I could use the <shell> tool here, or maybe read the file first."
        )
        assert turn.ops == []
        assert turn.parse_stage == 1  # thought-only

    def test_unknown_tag_not_rescued(self, wf):
        turn = wf.parse_turn("<div>\n<span>hello</span>\n</div>")
        assert turn.ops == []

    def test_canonical_emission_never_enters_lenient(self, wf):
        # strict 성공 경로 무영향 — stage 1 유지
        turn = wf.parse_turn(_call("read_file", {"path": "a.py"}))
        assert turn.parse_stage == 1

    def test_json_fc_regression_not_rescued_here(self, wf):
        # 타 포맷 누출(17%)은 이 구제 범위 밖 (Phase 3 소관) — thought-only
        turn = wf.parse_turn(
            '## Thought\nt\n\n## Action\n[{"action": "shell", "command": "ls"}]'
        )
        assert turn.ops == []

    def test_hybrid_canonical_function_plain_params_recovered(self, wf):
        # 하이브리드: 캐노니컬 <function=X> + plain-tag 파라미터 —
        # strict 가 0개일 때 lenient 파라미터 폴백 (종전엔 조용히 유실)
        turn = wf.parse_turn(
            "<tool_call>\n<function=read_file>\n<path>aaa</path>\n"
            "</function>\n</tool_call>"
        )
        assert turn.parse_stage == 2  # 회수 = drift 신호
        assert turn.ops[0].action == "read_file"
        assert turn.ops[0].action_input == {"path": "aaa"}

    def test_hybrid_fallback_not_triggered_when_strict_params_exist(self, wf):
        # strict 파라미터가 있으면 폴백 미발화 — 값 안의 태그-유사 raw
        # content 를 파라미터로 오인하지 않는다
        turn = wf.parse_turn(
            "<tool_call>\n<function=write_file>\n"
            "<parameter=path>a.html</parameter>\n"
            "<parameter=content>\n<div>hello</div>\n</parameter>\n"
            "</function>\n</tool_call>"
        )
        assert turn.parse_stage == 1
        assert turn.ops[0].action_input == {
            "path": "a.html",
            "content": "<div>hello</div>",
        }

    def test_paramless_canonical_function_unaffected_by_hybrid_fallback(self, wf):
        turn = wf.parse_turn(
            "<tool_call>\n<function=read_context>\n</function>\n</tool_call>"
        )
        assert turn.parse_stage == 1
        assert turn.ops[0].action_input == {}

    def test_rescued_turn_rerenders_canonical(self, wf):
        # 자기 교정: 구제 턴의 history 재렌더 = 캐노니컬 <function=> shape
        rec = wf.serialize_assistant_for_history("<shell>\n<command>ls -la</command>")
        assert rec["ops"][0]["action"] == "shell"
        msg = wf.render_assistant_from_history(rec)
        assert "<function=shell>" in msg["content"]
        assert "<parameter=command>ls -la</parameter>" in msg["content"]


# ── loop e2e (mocked provider) ───────────────────────────────


class TestLoopE2E:
    """파서 유닛 밖의 실경로 — 프롬프트 조립(multi_op 가이드)·dispatch
    parse_turn·관찰 주입·히스토리 round-trip 이 xml_fc 로 한 바퀴 돈다."""

    def _provider(self, *responses):
        from unittest.mock import MagicMock

        from agent_cli.providers.base import LLMResponse

        p = MagicMock()
        p.call.side_effect = [LLMResponse(content=r) for r in responses]
        return p

    def _caps(self):
        from agent_cli.providers.capabilities import ModelCapabilities

        return ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
        )

    def test_read_then_complete(self, tmp_path):
        from agent_cli.loop import run_loop

        f = tmp_path / "note.txt"
        f.write_text("hello xml world")
        provider = self._provider(
            "read the file first.\n\n" + _call("read_file", {"path": str(f)}),
            "found it.\n\n<tool_call>\n<function=complete>\n"
            "<parameter=result>\nFile says: hello xml world\n</parameter>\n"
            "</function>\n</tool_call>",
        )
        result = run_loop(
            query="What does note.txt say?",
            provider=provider,
            capabilities=self._caps(),
            model="test-model",
            wire_format="xml_fc",
        )
        assert result.success
        assert "hello xml world" in result.output
        # 시스템 프롬프트가 xml_fc 규칙으로 조립됐는지 (json 배열 아님)
        sys_prompt = provider.call.call_args_list[0].kwargs.get("system") or (
            provider.call.call_args_list[0].args[1]
            if len(provider.call.call_args_list[0].args) > 1
            else ""
        )
        assert "<tool_call>" in sys_prompt

    def test_multi_op_batch_dispatches_both(self, tmp_path):
        from agent_cli.loop import run_loop

        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("alpha")
        b.write_text("bravo")
        provider = self._provider(
            _call("read_file", {"path": str(a)})
            + "\n"
            + _call("read_file", {"path": str(b)}),
            "<tool_call>\n<function=complete>\n"
            "<parameter=result>alpha and bravo</parameter>\n"
            "</function>\n</tool_call>",
        )
        result = run_loop(
            query="Read both files",
            provider=provider,
            capabilities=self._caps(),
            model="test-model",
            wire_format="xml_fc",
        )
        assert result.success
        # 두 op 의 관찰이 둘째 콜의 메시지에 들어갔는지 (positional/kwarg 양쪽)
        args, kwargs = provider.call.call_args_list[1]
        msgs = args[0] if args else kwargs.get("messages")
        joined = str(msgs)
        assert "alpha" in joined and "bravo" in joined


# ── Phase 1 바인딩 연동 ──────────────────────────────────────


class TestBindingIntegration:
    def test_models_json_binding_resolves_xml_fc(self, tmp_path, monkeypatch):
        import json as _json

        import agent_cli.config as _config
        from agent_cli.wire_formats import resolve_wire_format

        target = tmp_path / "models.json"
        target.write_text(
            _json.dumps({"models": {"qwen-x": {"wire_format": "xml_fc"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
        monkeypatch.setattr(_config, "_cached_registry", None)
        wf = resolve_wire_format(explicit=None, session_format=None, model="qwen-x")
        assert wf.name == "xml_fc"


class TestLenientValueIntegrity:
    """★감사 발견 data-loss (v7.11.4): lenient 값 추출이 값 속의 임의
    `<tag>` 토큰에서 끊겨 — content 빈 파일 기록·shell 명령 파손·phantom
    param 생성. 값 종료는 키-이름/parameter closer 를 우선해야 한다."""

    def _wf(self):
        from agent_cli.wire_formats import get as get_wf

        return get_wf("xml_fc")

    def test_hybrid_content_with_html_not_shattered(self):
        turn = self._wf().parse_turn(
            "<tool_call>\n<function=write_file>\n<path>x.html</path>\n"
            "<content><div>hi</div></content>\n</function>\n</tool_call>"
        )
        ai = turn.ops[0].action_input
        assert ai["content"] == "<div>hi</div>"
        assert ai["path"] == "x.html"
        assert "div" not in ai  # phantom param 금지

    def test_shell_command_with_angle_tokens_intact(self):
        turn = self._wf().parse_turn(
            "<tool_call>\n<function=shell>\n"
            '<command>grep "<pattern>" file.txt</command>\n'
            "</function>\n</tool_call>"
        )
        ai = turn.ops[0].action_input
        assert ai["command"] == 'grep "<pattern>" file.txt'
        assert "pattern" not in ai

    def test_sed_style_multiple_angle_tokens(self):
        turn = self._wf().parse_turn(
            "<tool_call>\n<function=shell>\n"
            '<command>sed "s/<old>/<new>/" f.txt</command>\n'
            "</function>\n</tool_call>"
        )
        assert turn.ops[0].action_input["command"] == 'sed "s/<old>/<new>/" f.txt'

    def test_keyname_closer_still_accepted(self):
        # 기존 실측 변종 보존: <parameter=k>v</k>
        turn = self._wf().parse_turn(
            "<tool_call>\n<function=read_file>\n"
            "<parameter=line_start>1</line_start>\n"
            "<parameter=path>a.py</parameter>\n"
            "</function>\n</tool_call>"
        )
        ai = turn.ops[0].action_input
        assert ai["line_start"] == 1 and ai["path"] == "a.py"  # int 코어션은 기존 동작

    def test_same_line_params_do_not_swallow_each_other(self):
        """자기-closer 우선 경로가 구별되는 케이스: 같은 줄의 두 param —
        폴백(라인-끝 closer)만으로는 앞 param 이 뒤 param 을 삼킨다."""
        turn = self._wf().parse_turn(
            "<tool_call>\n<function=write_file>\n"
            "<path>x.txt</path> <content>hello</content>\n"
            "</function>\n</tool_call>"
        )
        ai = turn.ops[0].action_input
        assert ai["path"] == "x.txt"
        assert ai["content"] == "hello"

    def test_unclosed_param_falls_back_to_line_anchored_next(self):
        # closer 생략 드리프트 — 다음 라인-선두 오픈 직전까지가 값
        turn = self._wf().parse_turn(
            "<tool_call>\n<function=write_file>\n"
            "<path>a.py\n<content>hello</content>\n</function>\n</tool_call>"
        )
        ai = turn.ops[0].action_input
        assert ai["path"] == "a.py" and ai["content"] == "hello"


class TestThinkingOpenerDoesNotEatOps:
    """★감사 발견 op-삼킴 (v7.11.4): 미닫힘 <thinking> 이 action 앞에
    있으면 stage 0 가 EOF 까지 잘라 턴의 ops 전체가 무실패 소실."""

    def _wf(self):
        from agent_cli.wire_formats import get as get_wf

        return get_wf("xml_fc")

    def test_midline_mention_not_treated_as_opener(self):
        turn = self._wf().parse_turn(
            "Discussing the <thinking> channel design.\n"
            "<tool_call>\n<function=read_file>\n"
            "<parameter=path>a.py</parameter>\n</function>\n</tool_call>"
        )
        assert turn.ops and turn.ops[0].action == "read_file"

    def test_genuine_unclosed_opener_stops_at_structure(self):
        # 라인-선두 진짜 opener 라도 구조 마커부터는 살아야 한다
        turn = self._wf().parse_turn(
            "<think>\nweighing options without a closer\n"
            "<tool_call>\n<function=read_file>\n"
            "<parameter=path>a.py</parameter>\n</function>\n</tool_call>"
        )
        assert turn.ops and turn.ops[0].action == "read_file"
        assert "weighing options" in (turn.thinking or "")

    def test_trailing_unclosed_opener_still_stripped(self):
        # 기존 동작 보존: 끝자락 미닫힘 opener 는 EOF 까지 thinking
        turn = self._wf().parse_turn(
            "<tool_call>\n<function=complete>\n"
            "<parameter=result>done</parameter>\n</function>\n</tool_call>\n"
            "<think>\nleftover musing"
        )
        assert turn.ops and turn.ops[0].action == "complete"
        assert "leftover musing" in (turn.thinking or "")
