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
3. **Lifecycle defaults**: ``prefill``, ``normalize_assistant_text``,
   and ``provider_call_kwargs`` return ReAct's no-op defaults so
   provider behavior stays identical to the pre-plugin path.

Auto-registration is verified through ``agent_cli.wire_formats.get("react")``.
"""

from __future__ import annotations

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


class TestWrapActionInputExample:
    """Inline tool guide flavor — show ONLY the action_input dict."""

    def test_identity_for_react(self):
        # ReAct doesn't envelope-wrap; the surrounding shape lives in
        # FORMAT_RULES and the inline guides show only the action_input
        # dict. Plugin returns the dict unchanged.
        out = ReActFormat().wrap_action_input_example(
            action="read_file",
            args_json='{"path": "x.py"}',
            idval="r1",
        )
        assert out == '{"path": "x.py"}'

    def test_idval_unused_for_react(self):
        # The id is meaningful only for envelope formats; ReAct must
        # not embed it anywhere — the underlying parser would be
        # confused by an extraneous field.
        out = ReActFormat().wrap_action_input_example("foo", "{}", "anything")
        assert "anything" not in out

    def test_action_name_not_embedded_for_react(self):
        # Inline guide flavor must NOT add the action name — it's
        # already obvious from the guide's header. Adding it would
        # double-state and could confuse the model about whether the
        # guide is showing the full call or the inner shape.
        out = ReActFormat().wrap_action_input_example(
            "read_file", '{"path": "x"}', "r1"
        )
        assert '"action"' not in out


class TestWrapFullCallExample:
    """Skill/agent invocation flavor — must show action + action_input."""

    def test_react_returns_bare_react_invocation(self):
        # Matches the legacy literal in ``build_skill_descriptions`` /
        # ``build_agent_descriptions``: a single JSON object with
        # ``action`` and ``action_input`` keys. No ``thought`` key —
        # those doc examples have historically omitted it.
        out = ReActFormat().wrap_full_call_example(
            action="run_skill",
            args_json='{"name": "x", "arguments": "y"}',
            idval="sk1",
        )
        assert out == (
            '{"action": "run_skill", "action_input": {"name": "x", "arguments": "y"}}'
        )

    def test_react_full_call_no_thought_key(self):
        # ``thought`` is the user's reasoning, not part of the
        # invocation template. Docs show the ``{action, action_input}``
        # pair only.
        out = ReActFormat().wrap_full_call_example("delegate", '{"tasks": []}', "ag1")
        assert '"thought"' not in out


# ─── Lifecycle defaults ─────────────────────────────


class TestLifecycleDefaults:
    """ReAct's defaults are 'do nothing'; the loop's pre-plugin
    behavior is preserved bit-for-bit."""

    def test_prefill_is_empty(self):
        # Empty string → loop does NOT append a trailing assistant
        # message; provider call shape is identical to pre-plugin.
        assert ReActFormat().prefill() == ""

    def test_normalize_assistant_text_is_identity(self):
        raw = '{"action": "read_file"}'
        assert ReActFormat().normalize_assistant_text(raw) == raw
        # Non-JSON garbage also passes through (loop eventually echoes
        # it via the recovery path; the normalizer must not eat it).
        assert ReActFormat().normalize_assistant_text("garbage") == "garbage"

    def test_provider_call_kwargs_is_empty_dict(self):
        # No JSON-mode disable, no other quirks. Capability-driven
        # format=json stays active when the model claims support.
        assert ReActFormat().provider_call_kwargs() == {}


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
