"""json_fc (산문 + bare op 배열) — multi-wire-format PHASE4.

md_array 의 후계: 헤더 envelope 제거 (D7=bare 배열, 래퍼 기각). 캐노니컬
파싱·legacy 헤더 관용(stage 2)·round-trip·recovery 문구를 고정한다.
상세 JSON-수리 케이스는 test_wire_formats_json_fc_repair.py (md_array
포팅분)가 커버한다.
"""

import pytest

from agent_cli.wire_formats import get as get_wf
from agent_cli.wire_formats.json_fc import JsonFcFormat


@pytest.fixture
def wf():
    return JsonFcFormat()


class TestRegistrationAndFlags:
    def test_registered(self):
        assert get_wf("json_fc").name == "json_fc"

    def test_flags(self, wf):
        assert wf.multi_op is True
        assert wf.thought_required is False
        assert wf.action_required is False
        assert wf.exposes_complete is True


class TestCanonicalParse:
    def test_prose_then_array_multi_op(self, wf):
        turn = wf.parse_turn(
            "auth 와 session 을 같이 본다.\n\n"
            '[{"action": "read_file", "path": "a.py"},'
            ' {"action": "read_file", "path": "b.py"}]'
        )
        assert turn.parse_stage == 1
        assert turn.thought == "auth 와 session 을 같이 본다."
        assert [op.action_input["path"] for op in turn.ops] == ["a.py", "b.py"]

    def test_array_only_no_prose(self, wf):
        turn = wf.parse_turn('[{"action": "shell", "command": "ls"}]')
        assert turn.parse_stage == 1
        assert turn.thought is None
        assert turn.ops[0].action == "shell"

    def test_bare_object_is_one_op(self, wf):
        turn = wf.parse_turn('{"action": "read_file", "path": "a.py"}')
        assert turn.parse_stage == 1
        assert turn.ops[0].action == "read_file"

    def test_complete_terminates(self, wf):
        turn = wf.parse_turn(
            'done.\n\n[{"action": "complete", "result": "final answer"}]'
        )
        assert turn.ops[0].action == "complete"
        assert turn.ops[0].action_input == {"result": "final answer"}

    def test_non_op_array_in_prose_is_thought_only(self, wf):
        # "action" 가드: 산문 속 [1,2,3] 은 op 로 오인하지 않는다
        turn = wf.parse_turn("배열 [1, 2, 3] 을 생각해 보자.")
        assert turn.ops == []
        assert turn.parse_stage == 1

    def test_thought_only_is_zero_op_nudge_target(self, wf):
        # 산문-only = 완료 아님 (false-terminate 교훈) — NO_ACTION 넛지 대상
        turn = wf.parse_turn("이제 파일을 읽겠다.")
        assert turn.parse_stage == 1
        assert turn.ops == []

    def test_blank_is_stage0(self, wf):
        assert wf.parse_turn("  \n ").parse_stage == 0

    def test_repaired_array_is_stage2(self, wf):
        # 미닫힘 배열 — json_fc 승계 수리 기계 (close_unbalanced)
        turn = wf.parse_turn('고친다.\n\n[{"action": "shell", "command": "make"}')
        assert turn.parse_stage == 2
        assert turn.ops[0].action == "shell"

    def test_stage0_thinking_isolated(self, wf):
        turn = wf.parse_turn(
            '<think>scratch</think>\nplan.\n\n[{"action": "read_file", "path": "a.py"}]'
        )
        assert turn.thinking == "scratch"
        assert turn.thought == "plan."
        assert turn.ops[0].action == "read_file"


class TestLegacyHeaderAcceptance:
    def test_json_fc_shape_accepted_as_drift(self, wf):
        turn = wf.parse_turn(
            '## Thought\nt\n\n## Action\n[{"action": "shell", "command": "ls"}]'
        )
        assert turn.parse_stage == 2  # legacy = drift 신호
        assert turn.thought == "t"
        assert turn.ops[0].action == "shell"

    def test_legacy_headerless_thought_recovered(self, wf):
        turn = wf.parse_turn(
            'prose reasoning\n\n## Action\n[{"action": "shell", "command": "ls"}]'
        )
        assert turn.parse_stage == 2
        assert turn.thought == "prose reasoning"

    def test_legacy_prior_rerenders_canonical(self, wf):
        # 자기 교정: legacy emission 의 레코드가 캐노니컬(헤더 없는)로 재렌더
        rec = wf.serialize_assistant_for_history(
            '## Thought\nt\n\n## Action\n[{"action": "shell", "command": "ls"}]'
        )
        msg = wf.render_assistant_from_history(rec)
        assert "## Action" not in msg["content"]
        assert msg["content"].startswith("t\n\n[")


class TestRenderAndHistory:
    def test_render_full_example_no_headers(self, wf):
        out = wf.render_full_example(
            thought="reason", action="read_file", action_input='{"path": "a.py"}'
        )
        assert out == 'reason\n\n[{"action": "read_file", "path": "a.py"}]'
        assert "## " not in out

    def test_round_trip(self, wf):
        raw = '생각.\n\n[{"action": "write_file", "path": "x", "content": "hi"}]'
        rec = wf.serialize_assistant_for_history(raw)
        assert rec["ops"][0]["action"] == "write_file"
        msg = wf.render_assistant_from_history(rec)
        re2 = wf.parse_turn(msg["content"])
        assert re2.thought == "생각."
        assert re2.ops[0].action_input == {"path": "x", "content": "hi"}

    def test_terminal_record_ops_shape(self, wf):
        rec = wf.serialize_terminal_for_history("t", "r")
        assert rec["ops"] == [{"action": "complete", "action_input": {"result": "r"}}]

    def test_record_shape_parity_with_xml_fc(self):
        assert get_wf("json_fc").serialize_terminal_for_history("t", "r") == get_wf(
            "xml_fc"
        ).serialize_terminal_for_history("t", "r")


class TestGuards:
    def test_format_rules_no_headers_in_canonical_shape(self, wf):
        rules = wf.format_rules()
        assert '{"action"' in rules
        assert "## Thought" not in rules  # 캐노니컬 예시에 헤더 없음
        assert "plain prose" in rules

    def test_degenerate_legacy_header_runaway_still_detected(self, wf):
        assert wf.is_degenerate("## Thought\n## Action\n## Thought\n## Action")

    def test_sanitize_strips_legacy_sentinels(self, wf):
        out = wf.sanitize_thought("real\n## Thought\n</think>x")
        assert "## Thought" not in out and "</think>" not in out

    def test_recovery_wordings(self, wf):
        assert "JSON array" in wf.constraint_reminder_call()
        assert "complete" in wf.constraint_reminder_action_required()
        prefixes = wf.system_user_prefixes()
        assert any(wf.failure_framing_parse_fail().startswith(p) for p in prefixes)
        assert any(wf.failure_framing_no_action().startswith(p) for p in prefixes)

    def test_diagnose_syntax_error_headerless(self, wf):
        out = wf.diagnose_syntax_error('prose\n\n[{"action": "shell", "command": ')
        assert out  # 위치 진단이 나옴 (describe_json_error 위임)


class TestLoopE2E:
    """실경로 — 프롬프트 조립(신 format rules)·dispatch·관찰·히스토리가
    json_fc 캐노니컬 셰이프로 한 바퀴 돈다."""

    def _caps(self):
        from agent_cli.providers.capabilities import ModelCapabilities

        return ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
            thinking_budget=0,
        )

    def test_read_then_complete(self, tmp_path):
        from unittest.mock import MagicMock

        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse

        f = tmp_path / "note.txt"
        f.write_text("hello json world")
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=f'read it first.\n\n[{{"action": "read_file", "path": "{f}"}}]'
            ),
            LLMResponse(
                content='done.\n\n[{"action": "complete", "result": "File says: hello json world"}]'
            ),
        ]
        result = run_loop(
            query="What does note.txt say?",
            provider=provider,
            capabilities=self._caps(),
            model="m",
            wire_format="json_fc",
        )
        assert result.success
        assert "hello json world" in result.output
        # 시스템 프롬프트가 신 규칙으로 조립됐는지 (헤더 없는 캐노니컬)
        args, kwargs = provider.call.call_args_list[0]
        sys_prompt = kwargs.get("system") or (args[1] if len(args) > 1 else "")
        assert "ONE JSON" in sys_prompt
