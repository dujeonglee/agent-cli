"""Terminal (`complete`) history serialization.

The loop's complete handler holds the unwrapped result, not raw text, so it
records the terminal turn via ``WireFormat.serialize_terminal_for_history``
rather than ``serialize_assistant_for_history``. This must produce the SAME
shape the format uses for every other op (homogeneous history), not the base
singular shape — a regression that once stored `complete` differently from
the 73 other op turns in a real json_fc session.
"""

from agent_cli.wire_formats.base import WireFormat
from agent_cli.wire_formats.json_fc import JsonFcFormat


class TestTerminalSerialize:
    def test_json_fc_uses_ops_shape(self):
        rec = JsonFcFormat().serialize_terminal_for_history("done", "the answer")
        assert rec["role"] == "assistant"
        assert rec["thought"] == "done"
        assert "action" not in rec  # NOT the singular shape
        assert rec["ops"] == [
            {"action": "complete", "action_input": {"result": "the answer"}}
        ]

    def test_xml_fc_uses_ops_shape(self):
        from agent_cli.wire_formats.xml_fc import XmlFcFormat

        rec = XmlFcFormat().serialize_terminal_for_history("done", "the answer")
        assert "action" not in rec
        assert rec["ops"] == [
            {"action": "complete", "action_input": {"result": "the answer"}}
        ]

    def test_json_fc_and_xml_fc_parity(self):
        # both shipped formats store a terminal turn identically
        from agent_cli.wire_formats.xml_fc import XmlFcFormat

        a = JsonFcFormat().serialize_terminal_for_history("t", "r")
        b = XmlFcFormat().serialize_terminal_for_history("t", "r")
        assert a == b

    def test_base_default_is_singular(self):
        # a hypothetical singular format keeps the {action, action_input} shape
        class _Singular(WireFormat):
            # minimal concrete: inherit everything, only need the default.
            # v8.41.0 ABC: parse_turn 이 1차 추상 (parse 는 첫-op 투영 기본).
            def render_full_example(self, **k):
                return ""

            def format_rules(self):
                return ""

            def parse_turn(self, t):
                from agent_cli.wire_formats.base import ParsedTurn

                return ParsedTurn()

            def constraint_reminder_call(self):
                return ""

            def constraint_reminder_action_required(self):
                return ""

            def failure_framing_parse_fail(self):
                return ""

            def failure_framing_no_action(self):
                return ""

            def static_retry_hint_no_json(self):
                return ""

            def static_retry_hint_no_action(self):
                return ""

            def system_user_prefixes(self):
                return ()

        rec = _Singular().serialize_terminal_for_history("done", "answer")
        assert rec["action"] == "complete"
        assert rec["action_input"] == {"result": "answer"}
        assert "ops" not in rec

    def test_round_trips_through_render(self):
        # the stored terminal record renders to the format's wire prior, same
        # as any other op turn (resume consistency)
        fmt = JsonFcFormat()
        rec = fmt.serialize_terminal_for_history("all done", "final result text")
        content = fmt.render_assistant_from_history(rec)["content"]
        assert content.startswith("all done\n\n[")  # PHASE4: 헤더 없는 캐노니컬
        assert '"action": "complete"' in content
        assert '"result": "final result text"' in content
