"""Unit tests for the ReAct wire-format plugin.

The plugin is the reference implementation that mirrors pre-plugin
behavior. Tests here verify three things:

1. **Self-containment**: the strings returned by every recovery /
   prompt method match the legacy bodies they replace. If a future
   refactor drifts the legacy strings without updating the plugin
   (or vice versa), these tests fail before the loop ever sees a
   mismatch.
2. **Boundary correctness**: ``parse()`` returns ``ParsedAction`` with
   the same fields the underlying ``parse_react`` produced — the
   adapter doesn't lose information.
3. **Lifecycle defaults**: ``prefill``, ``normalize_assistant_for_messages``,
   and ``provider_call_kwargs`` return ReAct's no-op defaults so
   provider behavior stays identical to the pre-plugin path.

Auto-registration is verified through ``agent_cli.wire_formats.get("react")``.
"""

from __future__ import annotations

import json

from agent_cli import wire_formats
from agent_cli.wire_formats.base import WireFormat as WireFormatProtocol
from agent_cli.wire_formats.react import ReActFormat


class TestRegistration:
    def test_react_is_registered_at_import_time(self):
        """``import agent_cli.wire_formats`` triggers builtin registration —
        no caller-side bootstrap needed."""
        plugin = wire_formats.get("react")
        assert isinstance(plugin, ReActFormat)

    def test_react_satisfies_protocol(self):
        assert isinstance(ReActFormat(), WireFormatProtocol)

    def test_name_and_thought_required_attributes(self):
        plugin = ReActFormat()
        assert plugin.name == "react"
        # ReAct schema has thought as a required field — the
        # NO_THOUGHT recovery path depends on this flag (Step 6 hooks
        # it up).
        assert plugin.thought_required is True


# ─── Recovery reminder content ──────────────────────


class TestRecoveryReminders:
    """Behavior tests that previously lived in
    ``test_recovery_primitives.TestConstrainFormatJson`` and
    ``TestConstrainActionRequired``. The strings now live on the
    plugin (Step 7 cleanup); the assertions follow them."""

    def test_constraint_reminder_call_mentions_required_fields(self):
        out = ReActFormat().constraint_reminder_call()
        assert "thought" in out
        assert "action" in out
        assert "action_input" in out

    def test_constraint_reminder_call_forbids_markdown_fences(self):
        out = ReActFormat().constraint_reminder_call()
        assert "markdown" in out.lower() or "fences" in out.lower()

    def test_constraint_reminder_action_required_presents_both_paths(self):
        out = ReActFormat().constraint_reminder_action_required()
        assert '"action": "tool_name"' in out
        assert '"action": "complete"' in out

    def test_failure_framing_parse_fail_is_react_legacy_wording(self):
        # The framing string was previously hardcoded inside
        # ``recovery/builders.format_no_json_retry`` — this asserts the
        # plugin's value is the same wording so behavior didn't drift.
        assert (
            ReActFormat().failure_framing_parse_fail()
            == "Your response was not valid JSON."
        )

    def test_failure_framing_no_action_is_react_legacy_wording(self):
        assert (
            ReActFormat().failure_framing_no_action()
            == "Your JSON was parsed but has no action."
        )


# ─── Parser adaptation ──────────────────────────────


class TestParseReturnsParsedAction:
    """The adapter must preserve every field ``parse_react`` populates."""

    def test_successful_parse_round_trip(self):
        plugin = ReActFormat()
        text = (
            '{"thought": "reading", "action": "read_file", '
            '"action_input": {"path": "x.py"}}'
        )
        out = plugin.parse(text)
        assert out.thought == "reading"
        assert out.action == "read_file"
        assert out.action_input == {"path": "x.py"}
        assert out.parse_stage > 0
        assert out.truncated is False

    def test_failed_parse_yields_stage_zero(self):
        out = ReActFormat().parse("this is not JSON")
        assert out.parse_stage == 0
        assert out.action is None
        # Even on failure, ``raw`` carries the (post-thinking-strip) text
        # so the recovery layer can echo it back.
        assert "this is not JSON" in out.raw

    def test_thinking_field_preserved(self):
        # ``parse_react`` strips a leading ``<think>...</think>`` block
        # and surfaces it as the ``thinking`` field — the boundary type
        # must carry that through.
        text = (
            "<think>scratch reasoning</think>"
            '{"action": "read_file", "action_input": {"path": "x"}}'
        )
        out = ReActFormat().parse(text)
        assert out.thinking is not None
        assert "scratch reasoning" in out.thinking


# ─── Wrap example methods (two flavors) ─────────────


class TestRenderFullExample:
    """The single rendering hook the Format Rules builder calls. ReAct
    emits the bare JSON dict — full schema fields, with ``thought``
    substituting a short placeholder when ``None`` so the reasoning
    slot stays visible in skill / agent invocation examples.
    """

    def test_full_emission_with_thought(self):
        out = ReActFormat().render_full_example(
            thought="your reasoning",
            action="tool_name",
            action_input="{...}",
        )
        assert out == (
            '{"thought": "your reasoning", '
            '"action": "tool_name", '
            '"action_input": {...}}'
        )

    def test_thought_none_substitutes_placeholder(self):
        """``thought=None`` is the skill / agent invocation example
        case. ReAct substitutes ``"reasoning here"`` so the thought
        slot stays visible — teaches the model the slot is required
        even in invocation-only examples.
        """
        out = ReActFormat().render_full_example(
            thought=None,
            action="run_skill",
            action_input='{"name": "x", "arguments": "y"}',
        )
        assert out == (
            '{"thought": "reasoning here", '
            '"action": "run_skill", '
            '"action_input": {"name": "x", "arguments": "y"}}'
        )

    def test_action_input_string_passed_verbatim(self):
        """The plugin receives a JSON string, not a dict, so the caller
        controls whitespace / key order. Plugin must splice it as-is."""
        out = ReActFormat().render_full_example(
            thought="hi",
            action="x",
            action_input='{"a": 1, "b": 2}',
        )
        assert '"action_input": {"a": 1, "b": 2}' in out


class TestRenderActionInput:
    """Inline-guide hook. The wire format owns serialization: ReAct nests
    action_input as a JSON object, so this serializes the dict via
    ``json.dumps``. The abstraction exists for future plugins whose
    action_input is not a JSON dict (e.g. XML attribute encoding)."""

    def test_serializes_dict_to_json(self):
        out = ReActFormat().render_action_input({"path": "app.py"})
        assert out == '{"path": "app.py"}'

    def test_serializes_nested_dict(self):
        out = ReActFormat().render_action_input(
            {"a": 1, "b": [2, 3], "nested": {"k": "v"}}
        )
        assert out == '{"a": 1, "b": [2, 3], "nested": {"k": "v"}}'


class TestFormatRulesAnchor:
    """The first sentence of the section after the heading."""

    def test_react_anchor_demands_single_json_object(self):
        anchor = ReActFormat().format_rules_anchor()
        assert "JSON object" in anchor
        assert "MUST" in anchor


class TestFormatRulesFieldSpecific:
    """Rules 1 and 2 — the field-name-specific obligations."""

    def test_rule_1_thought_must_state_purpose(self):
        rules = ReActFormat().format_rules_field_specific()
        assert rules.startswith("1.")
        assert "thought" in rules
        assert "MUST" in rules

    def test_rule_2_action_input_must_match_schema(self):
        rules = ReActFormat().format_rules_field_specific()
        assert "\n2." in rules
        assert "action_input" in rules


# ─── Lifecycle defaults ─────────────────────────────


class TestLifecycleDefaults:
    """ReAct's defaults are 'do nothing'; the loop's pre-plugin
    behavior is preserved bit-for-bit."""

    def test_prefill_is_empty(self):
        # Empty string → loop does NOT append a trailing assistant
        # message; provider call shape is identical to pre-plugin.
        assert ReActFormat().prefill() == ""

    def test_normalize_assistant_for_messages_is_identity(self):
        raw = '{"action": "read_file"}'
        assert ReActFormat().normalize_assistant_for_messages(raw) == raw
        # Non-JSON garbage also passes through (loop eventually echoes
        # it via the recovery path; the normalizer must not eat it).
        assert ReActFormat().normalize_assistant_for_messages("garbage") == "garbage"

    def test_provider_call_kwargs_is_empty_dict(self):
        # No JSON-mode disable, no other quirks. Capability-driven
        # format=json stays active when the model claims support.
        assert ReActFormat().provider_call_kwargs() == {}


class TestSerializeAssistantForHistory:
    """The dict shape that lands in history.jsonl for a ReAct emission.

    Owned by ReActFormat since Step H2 — ``loop._append_observation``
    routes its assistant record through this method. The on-disk
    contract is verified end-to-end in ``test_loop`` integration tests;
    this class covers the structural branches (parse OK, partial dict,
    fallback) in isolation."""

    def test_parses_react_json_into_role_keyed_dict(self):
        text = (
            '{"thought": "reading", "action": "read_file", '
            '"action_input": {"path": "x.py"}}'
        )
        out = ReActFormat().serialize_assistant_for_history(text)
        assert out == {
            "role": "assistant",
            "thought": "reading",
            "action": "read_file",
            "action_input": {"path": "x.py"},
        }

    def test_partial_react_with_only_action(self):
        # Drift case: action without thought. Still parsed, role added.
        text = '{"action": "read_file", "action_input": {"path": "x"}}'
        out = ReActFormat().serialize_assistant_for_history(text)
        assert out["role"] == "assistant"
        assert out["action"] == "read_file"

    def test_partial_react_with_only_thought_falls_back_to_content(self):
        # The base default's serialize routes through ``self.parse()``
        # and uses ``parsed.action`` as the structured-vs-fallback gate.
        # Thought-only emissions (no action) are a drift case — stored
        # as bare content so the raw text survives in history.jsonl
        # for postmortem. The model on overflow-recovery will see the
        # raw text and the next-turn recovery layer can re-prompt for
        # an action.
        text = '{"thought": "no action this turn"}'
        out = ReActFormat().serialize_assistant_for_history(text)
        assert out == {
            "role": "assistant",
            "content": '{"thought": "no action this turn"}',
        }

    def test_unparseable_text_falls_back_to_content(self):
        # Garbage emission must still survive in the log for postmortem,
        # not raise. ``content`` carries the original text verbatim.
        out = ReActFormat().serialize_assistant_for_history("this is not JSON at all")
        assert out == {
            "role": "assistant",
            "content": "this is not JSON at all",
        }

    def test_json_array_falls_back_to_content(self):
        # Valid JSON but not a dict — fall back so the schema invariant
        # (role + thought/action keys) holds.
        out = ReActFormat().serialize_assistant_for_history('["a", "b"]')
        assert out["role"] == "assistant"
        assert out["content"] == '["a", "b"]'

    def test_json_dict_without_react_keys_falls_back(self):
        # Dict but neither thought nor action — not a ReAct emission.
        # Fall back instead of pretending the keys are there.
        out = ReActFormat().serialize_assistant_for_history('{"random": "data"}')
        assert out["role"] == "assistant"
        assert out["content"] == '{"random": "data"}'


class TestRenderAssistantFromHistory:
    """history.jsonl record → message dict for chat completion.

    Round-trip back to the ReAct wire shape (a JSON object) so the
    model sees the same shape it originally emitted regardless of
    whether the turn came from the live buffer or from history.
    Self-reinforcement of the wire format survives overflow recovery."""

    def test_action_yields_json_wire_shape(self):
        record = {
            "role": "assistant",
            "thought": "reading first",
            "action": "read_file",
            "action_input": {"path": "src/foo.py"},
        }
        msg = ReActFormat().render_assistant_from_history(record)
        assert msg["role"] == "assistant"
        # Content is a JSON object — re-emit of the original wire shape.
        parsed = json.loads(msg["content"])
        assert parsed == {
            "thought": "reading first",
            "action": "read_file",
            "action_input": {"path": "src/foo.py"},
        }

    def test_key_order_thought_action_input(self):
        # Canonical key order in the re-emit so the wire shape model
        # sees is stable across recoveries.
        record = {
            "role": "assistant",
            "action": "read_file",
            "thought": "reading",
            "action_input": {"path": "x.py"},
        }
        msg = ReActFormat().render_assistant_from_history(record)
        # Order in the serialized string: thought, action, action_input.
        content = msg["content"]
        assert content.index('"thought"') < content.index('"action"')
        assert content.index('"action"') < content.index('"action_input"')

    def test_complete_emits_same_json_shape(self):
        # No special-case for complete — same JSON wire shape.
        record = {
            "role": "assistant",
            "thought": "task done",
            "action": "complete",
            "action_input": {"result": "Found 3 files."},
        }
        msg = ReActFormat().render_assistant_from_history(record)
        parsed = json.loads(msg["content"])
        assert parsed["action"] == "complete"
        assert parsed["action_input"] == {"result": "Found 3 files."}
        assert parsed["thought"] == "task done"

    def test_missing_thought_renders_as_empty_string(self):
        # Defensive shape: action without thought field. The re-emit
        # uses an empty string rather than omitting the key so the
        # wire shape stays uniform across recoveries. The 3-field JSON
        # object shape is constant; only the values vary.
        record = {
            "role": "assistant",
            "action": "shell",
            "action_input": {"command": "ls"},
        }
        msg = ReActFormat().render_assistant_from_history(record)
        parsed = json.loads(msg["content"])
        assert parsed == {
            "thought": "",
            "action": "shell",
            "action_input": {"command": "ls"},
        }

    def test_non_ascii_preserved_verbatim(self):
        # ``ensure_ascii=False`` keeps Unicode literal — avoids the
        # \uXXXX escape that would inflate the model's input tokens
        # for any non-Latin reasoning text.
        record = {
            "role": "assistant",
            "thought": "한글 reasoning",
            "action": "read_file",
            "action_input": {"path": "x.py"},
        }
        msg = ReActFormat().render_assistant_from_history(record)
        assert "한글 reasoning" in msg["content"]
        assert "\\u" not in msg["content"]

    def test_string_action_input_round_trips_as_quoted_json_literal(self):
        # Edge case: legacy / drift emission where ``action_input`` is a
        # raw string (e.g. ``complete`` action carrying the answer as a
        # bare string). The re-render must produce VALID JSON — earlier
        # behaviour str()'d the string and spliced it raw, producing
        # ``"action_input": the answer`` (no quotes, invalid JSON).
        record = {
            "role": "assistant",
            "thought": "done",
            "action": "complete",
            "action_input": "the answer",
        }
        msg = ReActFormat().render_assistant_from_history(record)
        # The whole content must parse back as valid JSON.
        parsed = json.loads(msg["content"])
        assert parsed == {
            "thought": "done",
            "action": "complete",
            "action_input": "the answer",
        }

    def test_no_structured_fields_falls_back_to_content(self):
        # Defensive: a record that ``serialize_assistant_for_history``
        # could not parse and stored as bare ``content``. The fallback
        # echoes that content rather than producing an empty message.
        record = {"role": "assistant", "content": "free-form note"}
        msg = ReActFormat().render_assistant_from_history(record)
        assert msg["content"] == "free-form note"


class TestSystemUserPrefixes:
    def test_includes_react_framings_and_no_thought(self):
        prefixes = ReActFormat().system_user_prefixes()
        # Both recovery framings emitted by this plugin must be listed
        # so ``recent_exchanges`` skips them in the resume preview.
        assert "Your response was not valid JSON." in prefixes
        assert "Your JSON was parsed but has no action." in prefixes
        # NO_THOUGHT framing (only relevant when thought_required=True)
        assert "Your JSON was missing the 'thought' field." in prefixes

    def test_format_agnostic_prefixes_not_duplicated(self):
        # B1 action-loop prefixes ("You have called", "You were asked
        # to:") and the interrupt notice are format-agnostic; they live
        # in constants.SYSTEM_USER_PREFIXES and must not be duplicated
        # in the plugin's list.
        prefixes = ReActFormat().system_user_prefixes()
        assert "You have called" not in prefixes
        assert "You were asked to:" not in prefixes
        assert "⚡ User interrupted." not in prefixes


class TestDeleteOpPreserved:
    """edit_file ``op=delete`` is a plain JSON value: the wire format must
    preserve it verbatim (op semantics belong to the tool, not the parser).
    No ``lines`` key, since delete's schema has none."""

    def test_parse_preserves_delete_op(self):
        plugin = ReActFormat()
        text = (
            '{"thought": "del line 2", "action": "edit_file", "action_input": '
            '{"path": "x.c", "edits": [{"op": "delete", "pos": "2#ab"}]}}'
        )
        out = plugin.parse(text)
        assert out.action == "edit_file"
        assert out.action_input["edits"][0] == {"op": "delete", "pos": "2#ab"}
        assert "lines" not in out.action_input["edits"][0]
