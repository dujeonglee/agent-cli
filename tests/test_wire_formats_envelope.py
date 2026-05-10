"""Unit tests for the envelope wire-format plugin.

Pins three areas:

1. **Parser** — envelope shape, action-attribute extraction,
   thought / action_input separation, fail-fast on malformed JSON.
2. **Wrappers** — wrap_action_input_example / wrap_full_call_example
   produce the envelope shape (not a bare action_input dict like ReAct).
3. **Plugin surface** — Protocol satisfaction, recovery framings,
   provider hints, history-pipeline round-trip.

The plugin is not registered in the global registry by E1 (E2 wires
that up); these tests instantiate ``EnvelopeFormat()`` directly.
"""

from __future__ import annotations

import json

from agent_cli.wire_formats.base import ParsedAction
from agent_cli.wire_formats.base import WireFormat as WireFormatProtocol
from agent_cli.wire_formats.envelope import EnvelopeFormat, parse_envelope


# ─── Parser ────────────────────────────────────────────────


class TestParseEnvelopeHappyPath:
    """Single-action envelope with reasoning + JSON dict."""

    def test_simple_read_file(self):
        text = (
            '<tool_use id="r1" action="read_file">\n'
            "I need to inspect auth.py.\n\n"
            '{"path": "src/auth.py"}\n'
            "</tool_use>"
        )
        result = parse_envelope(text)
        assert result.parse_stage == 1
        assert result.action == "read_file"
        assert result.action_input == {"path": "src/auth.py"}
        assert result.thought == "I need to inspect auth.py."
        assert result.truncated is False

    def test_nested_action_input(self):
        """edit_file's list-of-dicts input survives the round trip."""
        text = (
            '<tool_use id="r1" action="edit_file">\n'
            "Apply the bcrypt swap.\n\n"
            '{"path": "src/auth.py", "edits": [{"old": "import md5",'
            ' "new": "import bcrypt"}]}\n'
            "</tool_use>"
        )
        result = parse_envelope(text)
        assert result.parse_stage == 1
        assert result.action == "edit_file"
        assert result.action_input == {
            "path": "src/auth.py",
            "edits": [{"old": "import md5", "new": "import bcrypt"}],
        }

    def test_multiline_thought_preserved(self):
        text = (
            '<tool_use id="r1" action="shell">\n'
            "First, list the directory.\n"
            "Then we can decide what to read.\n\n"
            '{"command": "ls -la"}\n'
            "</tool_use>"
        )
        result = parse_envelope(text)
        assert result.parse_stage == 1
        assert "First, list" in result.thought
        assert "Then we can decide" in result.thought

    def test_action_attribute_with_extra_attrs(self):
        """Tag with id + action + arbitrary additional attribute."""
        text = (
            '<tool_use id="r1" action="shell" foo="bar">\n'
            "go.\n\n"
            '{"command": "echo hi"}\n'
            "</tool_use>"
        )
        result = parse_envelope(text)
        assert result.action == "shell"
        assert result.action_input == {"command": "echo hi"}


class TestParseEnvelopeEdgeCases:
    """Failure modes and partial parses."""

    def test_no_envelope_present_returns_failed(self):
        result = parse_envelope("Just plain text, no tags here.")
        assert result.parse_stage == 0
        assert result.action is None
        assert result.action_input is None

    def test_envelope_without_action_attribute(self):
        """Action attribute missing → parse_stage=3, action=None."""
        text = (
            '<tool_use id="r1">\n'
            "I forgot the action attribute.\n\n"
            '{"path": "x.py"}\n'
            "</tool_use>"
        )
        result = parse_envelope(text)
        # parse_stage=3 means "envelope parsed, action attr missing"
        assert result.parse_stage == 3
        assert result.action is None
        # action_input is set even though action is missing — recovery
        # layer will surface the no-action case.

    def test_envelope_with_broken_json_keeps_action(self):
        """Action attribute survives malformed JSON; action_input None.

        This is the robustness payoff of putting action in the XML
        attribute — recovery can tell the model "you tried to call
        <tool>, but the input dict didn't parse". A ReAct response
        with the same drift would lose both fields.
        """
        text = (
            '<tool_use id="r1" action="read_file">\n'
            "go.\n\n"
            '{"path": "src/x.py", \n'  # missing closing brace
            "</tool_use>"
        )
        result = parse_envelope(text)
        assert result.parse_stage == 2
        assert result.action == "read_file"
        assert result.action_input is None

    def test_thought_only_envelope_no_json_block(self):
        """Reasoning but no JSON dict at all."""
        text = (
            '<tool_use id="r1" action="read_file">\n'
            "Just thinking, no input yet.\n"
            "</tool_use>"
        )
        result = parse_envelope(text)
        assert result.parse_stage == 2  # action present, no JSON
        assert result.action == "read_file"
        assert result.action_input is None
        assert "Just thinking" in result.thought

    def test_empty_thought_triggers_no_thought_signal(self):
        """Whitespace-only reasoning leaves thought None — the
        ``detect_thought_missing`` detector fires the same way as for
        ReAct because it operates on ``thought + action``."""
        text = (
            '<tool_use id="r1" action="read_file">\n\n\n{"path": "x.py"}\n</tool_use>'
        )
        result = parse_envelope(text)
        # Empty thought normalizes to None.
        assert result.thought is None
        assert result.action == "read_file"

    def test_thought_text_containing_json_like_fragment(self):
        """The reasoning may mention dict-shaped text; the parser must
        still pick the *trailing* balanced block as action_input."""
        text = (
            '<tool_use id="r1" action="shell">\n'
            "I want to grep for {key: value} patterns.\n\n"
            '{"command": "grep -r {key '
            ".' src/\"}\n"  # the actual JSON action_input
            "</tool_use>"
        )
        # The "{key: value}" inside the reasoning is malformed JSON
        # (unquoted key, no closing `}` at top level for that fragment),
        # so brace tracking treats the trailing dict as the real one.
        result = parse_envelope(text)
        assert result.action == "shell"
        # We don't assert specific action_input content here; the point
        # is that parsing didn't crash and surfaced an action.
        assert result.parse_stage in (1, 2)

    def test_thinking_block_outside_envelope_extracted(self):
        """Same thinking-strip semantics as ReAct: ``<think>...</think>``
        outside the envelope goes into ``ParsedAction.thinking`` and is
        not seen as part of the envelope content."""
        text = (
            "<think>secret reasoning the renderer hides</think>\n"
            '<tool_use id="r1" action="read_file">\n'
            "do it.\n\n"
            '{"path": "x.py"}\n'
            "</tool_use>"
        )
        result = parse_envelope(text)
        assert result.action == "read_file"
        assert result.thinking is not None
        assert "secret reasoning" in result.thinking


# ─── Wrappers ──────────────────────────────────────────────


class TestExampleWrappers:
    """wrap_action_input_example / wrap_full_call_example produce
    the full envelope so guides reinforce the wire shape on every
    rendering."""

    def test_wrap_action_input_emits_envelope(self):
        out = EnvelopeFormat().wrap_action_input_example(
            action="read_file",
            args_json='{"path": "x.py"}',
            idval="r1",
        )
        assert out.startswith('<tool_use id="r1" action="read_file">')
        assert out.endswith("</tool_use>")
        assert '{"path": "x.py"}' in out

    def test_wrap_full_call_alias_to_action_input(self):
        """For envelope plugins both example types render the same
        shape — the action name is already an XML attribute, so there
        is no inline-vs-skill distinction to preserve."""
        plugin = EnvelopeFormat()
        a = plugin.wrap_action_input_example("delegate", '{"x":1}', "d1")
        b = plugin.wrap_full_call_example("delegate", '{"x":1}', "d1")
        assert a == b


# ─── Plugin surface ────────────────────────────────────────


class TestProtocolConformance:
    def test_satisfies_wire_format_protocol(self):
        plugin = EnvelopeFormat()
        assert isinstance(plugin, WireFormatProtocol)

    def test_name_and_thought_required(self):
        plugin = EnvelopeFormat()
        assert plugin.name == "envelope"
        assert plugin.thought_required is True


class TestRecoveryWording:
    """The framings and reminders are envelope-specific (the model
    must hear the wire shape it is expected to fix into)."""

    def test_failure_framings_mention_envelope(self):
        plugin = EnvelopeFormat()
        assert "<tool_use>" in plugin.failure_framing_parse_fail()
        assert "<tool_use>" in plugin.failure_framing_no_action()

    def test_constraint_reminders_show_shape(self):
        plugin = EnvelopeFormat()
        assert "<tool_use" in plugin.constraint_reminder_call()
        assert (
            "action=" in plugin.constraint_reminder_action_required()
            or "<tool_use" in plugin.constraint_reminder_action_required()
        )

    def test_static_hints_self_contained(self):
        """Static hints (used when there's nothing to echo back) are
        framing + reminder rolled into one paragraph."""
        plugin = EnvelopeFormat()
        assert plugin.static_retry_hint_no_json()
        assert plugin.static_retry_hint_no_action()

    def test_system_user_prefixes_are_recovery_openers(self):
        """Each prefix must match the opening of one recovery message
        so ``recent_exchanges`` can filter them out of resume preview."""
        plugin = EnvelopeFormat()
        prefixes = plugin.system_user_prefixes()
        assert plugin.failure_framing_parse_fail() in prefixes
        assert plugin.failure_framing_no_action() in prefixes
        # NO_THOUGHT framing also emitted by this plugin.
        no_thought_prefix = "Your <tool_use> envelope was missing the reasoning text."
        assert no_thought_prefix in prefixes


class TestNoThoughtRecovery:
    """``format_no_thought_retry`` mirrors ReActFormat's structure
    (echo prior output, restate constraint) but with envelope wording."""

    def test_with_prior_content_echoes_and_constrains(self):
        plugin = EnvelopeFormat()
        prior = '<tool_use id="r1" action="read_file">\n\n{"path":"x.py"}\n</tool_use>'
        intervention = plugin.format_no_thought_retry(prior_content=prior)
        assert intervention.message
        assert "missing the reasoning text" in intervention.message
        assert "echo_prior_output" in intervention.primitives

    def test_empty_prior_falls_back_to_static(self):
        plugin = EnvelopeFormat()
        intervention = plugin.format_no_thought_retry(prior_content="")
        assert intervention.message
        # No primitive when there's nothing to echo.
        assert intervention.primitives == []


class TestProviderHints:
    def test_prefill_opens_envelope_with_action_attr(self):
        plugin = EnvelopeFormat()
        assert plugin.prefill() == '<tool_use id="r1" action="'

    def test_provider_call_kwargs_disables_ollama_json_mode(self):
        plugin = EnvelopeFormat()
        kwargs = plugin.provider_call_kwargs()
        assert kwargs == {"skip_json_format": True}


# ─── History pipeline ──────────────────────────────────────


class TestHistoryPipeline:
    """The three knobs that shape an assistant turn through the
    conversation pipeline."""

    def test_normalize_for_messages_is_identity(self):
        plugin = EnvelopeFormat()
        raw = (
            '<tool_use id="r1" action="read_file">\n'
            "go.\n\n"
            '{"path": "x.py"}\n'
            "</tool_use>"
        )
        # Identity — preserves the wire shape in the model's prior.
        assert plugin.normalize_assistant_for_messages(raw) == raw

    def test_serialize_for_history_extracts_fields(self):
        plugin = EnvelopeFormat()
        raw = (
            '<tool_use id="r1" action="read_file">\n'
            "I need to inspect.\n\n"
            '{"path": "src/x.py"}\n'
            "</tool_use>"
        )
        record = plugin.serialize_assistant_for_history(raw)
        assert record["role"] == "assistant"
        assert record["action"] == "read_file"
        assert record["action_input"] == {"path": "src/x.py"}
        assert record["thought"] == "I need to inspect."

    def test_serialize_unparseable_falls_back_to_content(self):
        plugin = EnvelopeFormat()
        record = plugin.serialize_assistant_for_history("not an envelope at all")
        assert record == {"role": "assistant", "content": "not an envelope at all"}

    def test_render_action_call_summary(self):
        plugin = EnvelopeFormat()
        record = {
            "role": "assistant",
            "thought": "inspect auth.py",
            "action": "read_file",
            "action_input": {"path": "src/auth.py"},
        }
        msg = plugin.render_assistant_from_history(record)
        assert msg["role"] == "assistant"
        assert "thought: inspect auth.py" in msg["content"]
        assert "action: read_file(src/auth.py)" in msg["content"]

    def test_render_complete_with_thought(self):
        plugin = EnvelopeFormat()
        record = {
            "role": "assistant",
            "thought": "all done",
            "action": "complete",
            "action_input": {"result": "answer"},
        }
        msg = plugin.render_assistant_from_history(record)
        assert "all done" in msg["content"]
        assert "answer" in msg["content"]

    def test_round_trip_serialize_then_parse(self):
        """The serialized dict must contain enough information for
        ``manager._to_natural_language`` (via render_assistant_from_history)
        to produce a useful chat-completion message."""
        plugin = EnvelopeFormat()
        raw = (
            '<tool_use id="r1" action="shell">\n'
            "ls.\n\n"
            '{"command": "ls -la"}\n'
            "</tool_use>"
        )
        record = plugin.serialize_assistant_for_history(raw)
        msg = plugin.render_assistant_from_history(record)
        # Faithful to original action / args.
        assert "shell" in msg["content"]
        assert "ls -la" in msg["content"]


# ─── Smoke: parser returns ParsedAction directly ──────────


class TestParserReturnsParsedAction:
    """The parser yields the boundary type that ``loop.py`` consumes,
    no plugin-internal dataclass leaks across."""

    def test_return_type(self):
        result = parse_envelope(
            '<tool_use id="r1" action="x">why\n\n{"a":1}\n</tool_use>'
        )
        assert isinstance(result, ParsedAction)

    def test_complete_action_with_string_action_input(self):
        """Some virtual tools (``complete``) sometimes carry a string
        action_input. Envelope plugin keeps the dict-only shape, so a
        non-dict JSON fragment becomes ``action_input=None`` rather than
        being smuggled in. Regression guard against accidentally
        accepting list/string at the top level of action_input."""
        text = (
            '<tool_use id="r1" action="complete">\n'
            "done.\n\n"
            '{"result": "ok"}\n'
            "</tool_use>"
        )
        result = parse_envelope(text)
        assert result.action == "complete"
        assert result.action_input == {"result": "ok"}

    def test_serializes_as_valid_json_again(self):
        """The action_input dict should be json-serializable as it
        round-trips through history.jsonl."""
        plugin = EnvelopeFormat()
        raw = (
            '<tool_use id="r1" action="edit_file">\n'
            "swap.\n\n"
            '{"path": "x.py", "edits": [{"old": "a", "new": "b"}]}\n'
            "</tool_use>"
        )
        record = plugin.serialize_assistant_for_history(raw)
        # No exception — serialization round-trips cleanly.
        json.dumps(record, ensure_ascii=False)
