"""Terminal (`complete`) history serialization.

The loop's complete handler holds the unwrapped result, not raw text, so it
records the terminal turn via ``DialectBase.serialize_terminal_for_history``
rather than ``serialize_assistant_for_history``. This must produce the SAME
shape the format uses for every other op (homogeneous history) — a regression
once stored `complete` differently from the 73 other op turns in a real json_fc
session. v10.115.1: the base has no default (abstract); ``Dialect`` is the only
implementation.
"""

import pytest

from agent_cli.dialects.base import DialectBase
from agent_cli.dialects.engine import Dialect
from agent_cli.dialects.specs.json_fc import JSON_FC


class TestTerminalSerialize:
    def test_json_fc_uses_ops_shape(self):
        rec = Dialect(JSON_FC).serialize_terminal_for_history("done", "the answer")
        assert rec["role"] == "assistant"
        assert rec["thought"] == "done"
        assert "action" not in rec  # NOT the singular shape
        assert rec["ops"] == [
            {"action": "complete", "action_input": {"result": "the answer"}}
        ]

    def test_xml_fc_uses_ops_shape(self):
        from agent_cli.dialects.engine import Dialect
        from agent_cli.dialects.specs.xml_fc import XML_FC

        rec = Dialect(XML_FC).serialize_terminal_for_history("done", "the answer")
        assert "action" not in rec
        assert rec["ops"] == [
            {"action": "complete", "action_input": {"result": "the answer"}}
        ]

    def test_json_fc_and_xml_fc_parity(self):
        # both shipped formats store a terminal turn identically
        from agent_cli.dialects.engine import Dialect
        from agent_cli.dialects.specs.xml_fc import XML_FC

        a = Dialect(JSON_FC).serialize_terminal_for_history("t", "r")
        b = Dialect(XML_FC).serialize_terminal_for_history("t", "r")
        assert a == b

    def test_base_declares_history_methods_abstract(self):
        # v10.115.1: 기본 구현(단수 shape)이 삭제됐다 — history 직렬화 셋을
        # 빠뜨린 서브클래스는 인스턴스화부터 실패하고 메시지에 셋의 이름이 있다.
        class _WithoutHistory(DialectBase):
            # 나머지 추상은 전부 구현, history 직렬화 셋만 빠뜨린다.
            # v8.41.0 ABC: parse_turn 이 1차 추상 (parse 는 첫-op 투영 기본).
            def render_full_example(self, **k):
                return ""

            def format_rules(self):
                return ""

            def parse_turn(self, t):
                from agent_cli.dialects.base import ParsedTurn

                return ParsedTurn()

            def constraint_reminder_call(self):
                return ""

            def constraint_reminder_action_required(self):
                return ""

            def failure_framing_parse_fail(self):
                return ""

            def no_action_detail(self):
                return ""

            def static_retry_hint_no_json(self):
                return ""

            def static_retry_hint_no_action(self):
                return ""

            def system_user_prefixes(self):
                return ()

        with pytest.raises(TypeError) as exc_info:
            _WithoutHistory()
        msg = str(exc_info.value)
        for name in (
            "serialize_assistant_for_history",
            "serialize_terminal_for_history",
            "render_observation_from_history",
        ):
            assert name in msg

    def test_round_trips_through_render(self):
        # the stored terminal record renders to the format's wire prior, same
        # as any other op turn (resume consistency)
        fmt = Dialect(JSON_FC)
        rec = fmt.serialize_terminal_for_history("all done", "final result text")
        content = fmt.render_assistant_from_history(rec)["content"]
        assert content.startswith("all done\n\n[")  # PHASE4: 헤더 없는 캐노니컬
        assert '"action": "complete"' in content
        assert '"result": "final result text"' in content
