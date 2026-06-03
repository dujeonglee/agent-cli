"""Unit tests for the PREFIX-MD wire format plugin.

PREFIX-MD uses markdown ATX H2 headings (``## Thought`` / ``## Action`` /
``## Input``) to delimit the three sections of an assistant turn.

Coverage axes:

1. **Parser** — strict sentinel matching, last-wins on Action / Input,
   action-body validation, parse_stage policy (0/1/2/3), thinking-block
   strip, surrogate sanitization.
2. **Format-rules render hooks** — anchor / field-specific / full-example
   rendering for inclusion in the shared builder.
3. **Recovery wording** — failure framings, reminders, static hints,
   NO_THOUGHT intervention.
4. **Provider override** — ``provider_call_kwargs`` returns
   ``skip_json_format`` so the provider's JSON mode doesn't force a
   JSON first token.
5. **Lifecycle defaults** — confirms PREFIX-MD's serialize / render
   round-trip via the base ABC default behaviour.
"""

from __future__ import annotations

import json

from agent_cli.recovery.intervention import Intervention
from agent_cli.wire_formats.prefix_md import PrefixMdFormat, parse_prefix_md


# ── Parser ─────────────────────────────────────────────


class TestParseHappyPath:
    """Three-section emission with valid action and JSON input."""

    def test_full_emission_parses_to_stage_1(self):
        text = (
            "## Thought\n"
            "I need to read app.py to find login.\n"
            "\n"
            "## Action\n"
            "read_file\n"
            "\n"
            "## Input\n"
            '{"path": "app.py"}'
        )
        result = parse_prefix_md(text)
        assert result.parse_stage == 1
        assert result.thought == "I need to read app.py to find login."
        assert result.action == "read_file"
        assert result.action_input == {"path": "app.py"}

    def test_multi_line_thought_preserved(self):
        text = (
            "## Thought\n"
            "First line.\n"
            "Second line.\n"
            "Third line.\n"
            "\n"
            "## Action\n"
            "shell\n"
            "\n"
            "## Input\n"
            '{"command": "ls"}'
        )
        result = parse_prefix_md(text)
        assert result.parse_stage == 1
        assert "First line." in result.thought
        assert "Second line." in result.thought
        assert "Third line." in result.thought

    def test_action_with_underscores_dots_hyphens(self):
        # Tool name regex accepts \w + . + -
        for tool in ("read_file", "my.tool", "do-it", "tool_name.v2"):
            text = f"## Thought\nt\n\n## Action\n{tool}\n\n## Input\n{{}}"
            result = parse_prefix_md(text)
            assert result.parse_stage == 1
            assert result.action == tool


class TestParseLastWins:
    """The parser's defence against ``## Action`` / ``## Input`` sub-
    headings inside reasoning text — last occurrence is canonical."""

    def test_action_sub_header_in_thought_absorbed_into_reasoning(self):
        text = (
            "## Thought\n"
            "Let me list the steps:\n"
            "## Action\n"
            "- This is a sub-header inside my reasoning, not the real action.\n"
            "\n"
            "## Action\n"
            "read_file\n"
            "\n"
            "## Input\n"
            '{"path": "x.py"}'
        )
        result = parse_prefix_md(text)
        assert result.parse_stage == 1
        assert result.action == "read_file"
        # Sub-header text is captured in thought.
        assert "sub-header inside my reasoning" in result.thought

    def test_multiple_inputs_last_one_wins(self):
        text = (
            "## Thought\nreasoning\n"
            "\n"
            "## Action\n"
            "read_file\n"
            "\n"
            "## Input\n"
            '{"path": "wrong.py"}\n'
            "\n"
            "## Input\n"
            '{"path": "right.py"}'
        )
        result = parse_prefix_md(text)
        assert result.parse_stage == 1
        assert result.action_input == {"path": "right.py"}


class TestParseFailureModes:
    """parse_stage policy: 0 (no Action), 2 (Input broken), 3
    (Action body invalid)."""

    def test_no_action_header_returns_stage_0(self):
        text = "## Thought\njust reasoning, no action\n"
        result = parse_prefix_md(text)
        assert result.parse_stage == 0
        assert result.action is None

    def test_action_body_not_single_token_returns_stage_3(self):
        # Action body is natural-language prose, not a valid tool name.
        text = (
            "## Thought\nreasoning\n"
            "\n"
            "## Action\n"
            "I think I should read the file\n"
            "\n"
            "## Input\n{}"
        )
        result = parse_prefix_md(text)
        assert result.parse_stage == 3
        assert result.action is None
        # Thought preserved so recovery can echo it.
        assert result.thought == "reasoning"

    def test_action_body_empty_returns_stage_3(self):
        text = "## Thought\nreasoning\n\n## Action\n\n## Input\n{}"
        result = parse_prefix_md(text)
        assert result.parse_stage == 3
        assert result.action is None

    def test_missing_input_section_returns_stage_2(self):
        text = "## Thought\nreasoning\n\n## Action\nread_file"
        result = parse_prefix_md(text)
        assert result.parse_stage == 2
        assert result.action == "read_file"
        assert result.action_input is None

    def test_broken_input_json_returns_stage_2(self):
        text = (
            "## Thought\nreasoning\n"
            "\n"
            "## Action\n"
            "read_file\n"
            "\n"
            "## Input\n"
            '{"path": "app.py"'  # missing closing brace
        )
        result = parse_prefix_md(text)
        assert result.parse_stage == 2
        assert result.action == "read_file"
        assert result.action_input is None


class TestParseStrictSentinels:
    """The headers are matched strictly — variants don't trigger
    section breaks."""

    def test_mid_line_action_word_does_not_match(self):
        # "## Action" embedded mid-sentence is not a sentinel.
        text = (
            "## Thought\n"
            "I will call ## Action with read_file as the body.\n"
            "\n"
            "## Action\n"
            "shell\n"
            "\n"
            "## Input\n"
            '{"command": "ls"}'
        )
        result = parse_prefix_md(text)
        assert result.parse_stage == 1
        assert result.action == "shell"

    def test_different_heading_level_not_a_sentinel(self):
        # ``# Thought`` (H1) is not a ``## `` sentinel, so it isn't parsed
        # as the Thought header. With prose-as-thought, the H1 line counts
        # as free-text reasoning before ## Action instead.
        text = (
            "# Thought\nthis is an H1, ignored\n\n## Action\nread_file\n\n## Input\n{}"
        )
        result = parse_prefix_md(text)
        assert result.parse_stage == 1
        assert result.thought == "# Thought\nthis is an H1, ignored"
        assert result.action == "read_file"


class TestParseThinkingStrip:
    """Leading ``<think>...</think>`` is stripped before sentinel
    matching so reasoning-channel content doesn't pollute the
    ``## Thought`` body."""

    def test_thinking_block_stripped_and_captured(self):
        text = (
            "<think>internal reasoning</think>\n"
            "## Thought\nvisible reasoning\n"
            "\n"
            "## Action\nread_file\n"
            "\n"
            "## Input\n{}"
        )
        result = parse_prefix_md(text)
        assert result.parse_stage == 1
        assert result.thinking == "internal reasoning"
        assert result.thought == "visible reasoning"


# ── Format rules ──────────────────────────────────────


class TestFormatRulesAnchor:
    def test_anchor_describes_three_sections(self):
        anchor = PrefixMdFormat().format_rules_anchor()
        assert "## Thought" in anchor
        assert "## Action" in anchor
        assert "## Input" in anchor


class TestFormatRulesFieldSpecific:
    def test_rule_1_obligates_thought(self):
        rules = PrefixMdFormat().format_rules_field_specific()
        assert rules.startswith("1.")
        assert "## Thought" in rules
        assert "Do not leave it empty" in rules

    def test_rule_2_obligates_action_and_input(self):
        rules = PrefixMdFormat().format_rules_field_specific()
        assert "\n2." in rules
        assert "## Action" in rules
        assert "## Input" in rules


class TestRenderFullExample:
    def test_full_emission_with_thought(self):
        out = PrefixMdFormat().render_full_example(
            thought="reasoning text",
            action="read_file",
            action_input='{"path": "app.py"}',
        )
        assert out == (
            "## Thought\n"
            "reasoning text\n"
            "\n"
            "## Action\n"
            "read_file\n"
            "\n"
            "## Input\n"
            '{"path": "app.py"}'
        )

    def test_thought_none_uses_placeholder(self):
        # Skill / agent invocation example: thought=None substitutes
        # "reasoning here" so the slot stays visible.
        out = PrefixMdFormat().render_full_example(
            thought=None,
            action="run_skill",
            action_input='{"name": "summarize"}',
        )
        assert "reasoning here" in out
        assert "run_skill" in out
        assert '{"name": "summarize"}' in out


class TestFormatRulesBuilderIntegration:
    def test_format_rules_includes_anchor_and_examples(self):
        rules = PrefixMdFormat().format_rules()
        assert "## Response Format" in rules
        # Anchor + 3 examples (schema, ready_for_review, complete).
        assert rules.count("## Thought") >= 3
        assert rules.count("## Action") >= 3
        assert "ready_for_review" in rules
        assert "complete" in rules


# ── Recovery wording ──────────────────────────────────


class TestRecoveryWording:
    def test_constraint_reminders_reference_section_headers(self):
        plugin = PrefixMdFormat()
        assert "## Thought" in plugin.constraint_reminder_call()
        assert "## Action" in plugin.constraint_reminder_action_required()

    def test_failure_framings_describe_the_format(self):
        plugin = PrefixMdFormat()
        assert "## Thought" in plugin.failure_framing_parse_fail()
        assert "## Action" in plugin.failure_framing_no_action()

    def test_static_hints_combine_framing_and_reminder(self):
        plugin = PrefixMdFormat()
        assert plugin.failure_framing_parse_fail() in plugin.static_retry_hint_no_json()
        assert plugin.constraint_reminder_call() in plugin.static_retry_hint_no_json()

    def test_system_user_prefixes_includes_all_framings(self):
        prefixes = PrefixMdFormat().system_user_prefixes()
        plugin = PrefixMdFormat()
        assert plugin.failure_framing_parse_fail() in prefixes
        assert plugin.failure_framing_no_action() in prefixes


class TestFormatNoThoughtRetry:
    """``format_no_thought_retry`` is duck-typed (not in the ABC) since
    only ``thought_required=True`` plugins emit it."""

    def test_no_prior_content_uses_static_message(self):
        result = PrefixMdFormat().format_no_thought_retry(prior_content="")
        assert isinstance(result, Intervention)
        assert "## Thought" in result.message
        assert result.primitives == []

    def test_with_prior_content_echoes_back(self):
        prior = "## Action\nshell\n## Input\n{}"
        result = PrefixMdFormat().format_no_thought_retry(prior_content=prior)
        assert isinstance(result, Intervention)
        assert prior in result.message
        assert "echo_prior_output" in result.primitives


# ── Provider override ─────────────────────────────────


class TestProviderCallKwargs:
    def test_json_mode_always_disabled(self):
        # Markdown opening ``## `` conflicts with an OpenAI-compatible JSON
        # mode (forces ``{`` first token), so json_mode is False regardless
        # of the model's structured-output capability.
        class _CapsYes:
            supports_structured_output = True

        kwargs = PrefixMdFormat().provider_call_kwargs(_CapsYes())
        assert kwargs == {"json_mode": False}


# ── Lifecycle defaults (inherited from WireFormat ABC) ────


class TestLifecycleDefaults:
    """The serialize/render round-trip is provided by the base ABC; this
    confirms PREFIX-MD's parse + render_full_example compose into the
    expected behaviour through the defaults."""

    def test_serialize_extracts_structured_fields(self):
        raw = (
            "## Thought\nreasoning\n"
            "\n"
            "## Action\nread_file\n"
            "\n"
            "## Input\n"
            '{"path": "app.py"}'
        )
        out = PrefixMdFormat().serialize_assistant_for_history(raw)
        assert out == {
            "role": "assistant",
            "thought": "reasoning",
            "action": "read_file",
            "action_input": {"path": "app.py"},
        }

    def test_serialize_garbage_falls_back_to_content(self):
        out = PrefixMdFormat().serialize_assistant_for_history("not markdown")
        assert out == {"role": "assistant", "content": "not markdown"}

    def test_render_round_trips_back_to_markdown(self):
        record = {
            "role": "assistant",
            "thought": "reasoning",
            "action": "read_file",
            "action_input": {"path": "app.py"},
        }
        msg = PrefixMdFormat().render_assistant_from_history(record)
        # The default in the ABC calls render_full_example which produces
        # the markdown shape.
        assert msg["role"] == "assistant"
        assert "## Thought" in msg["content"]
        assert "## Action" in msg["content"]
        assert "## Input" in msg["content"]
        assert "read_file" in msg["content"]
        # JSON inside Input section parses back to the original dict.
        # We don't byte-compare the whole thing because key ordering of
        # the action_input dict is the default ``json.dumps`` order.
        re_parsed = PrefixMdFormat().parse(msg["content"])
        assert re_parsed.parse_stage == 1
        assert re_parsed.thought == "reasoning"
        assert re_parsed.action == "read_file"
        assert re_parsed.action_input == {"path": "app.py"}

    def test_render_fallback_for_unstructured_record(self):
        record = {"role": "assistant", "content": "bare text"}
        msg = PrefixMdFormat().render_assistant_from_history(record)
        assert msg == {"role": "assistant", "content": "bare text"}


# ── Registry integration ──────────────────────────────


class TestRegistryIntegration:
    def test_registered_as_prefix_md(self):
        from agent_cli.wire_formats import get

        plugin = get("prefix_md")
        assert isinstance(plugin, PrefixMdFormat)

    def test_listed_in_registered_names(self):
        from agent_cli.wire_formats import list_names

        assert "prefix_md" in list_names()


# ── End-to-end round-trip ─────────────────────────────


class TestRoundTrip:
    """Emit → parse → serialize → render → parse should preserve
    structured content (key/values) across the full pipeline."""

    def test_emit_through_full_pipeline_preserves_fields(self):
        plugin = PrefixMdFormat()
        emit = plugin.render_full_example(
            thought="my reasoning here",
            action="shell",
            action_input='{"command": "ls -la"}',
        )
        record = plugin.serialize_assistant_for_history(emit)
        assert record["thought"] == "my reasoning here"
        assert record["action"] == "shell"
        assert record["action_input"] == {"command": "ls -la"}

        msg = plugin.render_assistant_from_history(record)
        re_parsed = plugin.parse(msg["content"])
        assert re_parsed.parse_stage == 1
        assert re_parsed.thought == "my reasoning here"
        assert re_parsed.action == "shell"
        assert re_parsed.action_input == {"command": "ls -la"}

    def test_string_action_input_renders_as_quoted_in_input_section(self):
        # Edge case: ``action_input`` is a raw string (legacy / drift
        # emission, typically a ``complete`` action carrying the final
        # answer as a string rather than a dict). The ABC default must
        # ``json.dumps`` it so the ## Input section contains a valid
        # JSON literal — earlier behaviour str()'d the value and emitted
        # an unquoted body that the parser couldn't recover as a dict.
        plugin = PrefixMdFormat()
        record = {
            "role": "assistant",
            "thought": "done",
            "action": "complete",
            "action_input": "the answer",
        }
        msg = plugin.render_assistant_from_history(record)
        # ## Input section body is a quoted JSON string literal.
        assert '## Input\n"the answer"' in msg["content"]

    def test_non_ascii_thought_preserved(self):
        plugin = PrefixMdFormat()
        emit = (
            "## Thought\n한글 reasoning 입니다\n\n"
            "## Action\nread_file\n\n"
            '## Input\n{"path": "x.py"}'
        )
        record = plugin.serialize_assistant_for_history(emit)
        assert record["thought"] == "한글 reasoning 입니다"
        msg = plugin.render_assistant_from_history(record)
        assert "한글 reasoning 입니다" in msg["content"]
        # render_full_example uses ensure_ascii=False in the base default
        # so non-ASCII stays literal rather than \uXXXX escaped.
        assert "\\u" not in json.dumps(record["action_input"], ensure_ascii=False)


class TestDeleteOpPreserved:
    """edit_file ``op=delete`` survives the prefix_md Input JSON round-trip
    verbatim — op semantics belong to the tool, and delete carries no lines."""

    def test_parse_preserves_delete_op(self):
        text = (
            "## Thought\ndel line 2\n\n## Action\nedit_file\n\n## Input\n"
            '{"path": "x.c", "edits": [{"op": "delete", "pos": "2#ab"}]}'
        )
        result = parse_prefix_md(text)
        assert result.action == "edit_file"
        assert result.action_input["edits"][0] == {"op": "delete", "pos": "2#ab"}
        assert "lines" not in result.action_input["edits"][0]


class TestThoughtOptional:
    """thought is optional: a missing ## Thought is allowed (no NO_THOUGHT
    retry), and free-text reasoning before ## Action is treated as the
    thought rather than dropped."""

    def test_thought_not_required(self):
        from agent_cli.wire_formats.prefix_md import PrefixMdFormat

        assert PrefixMdFormat().thought_required is False

    def test_prose_before_action_is_thought(self):
        # The DOOM pattern: model reasons in prose without ## Thought.
        text = (
            "I see the problem - an extra brace on line 131. Let me fix it.\n\n"
            "## Action\nedit_file\n\n## Input\n"
            '{"path": "x.c", "edits": [{"op": "delete", "pos": "131#ab"}]}'
        )
        out = parse_prefix_md(text)
        assert out.parse_stage == 1
        assert out.action == "edit_file"
        assert out.thought is not None and "I see the problem" in out.thought

    def test_explicit_thought_header_still_wins(self):
        text = "## Thought\nreal thought\n\n## Action\nread_file\n\n## Input\n{}"
        out = parse_prefix_md(text)
        assert out.thought == "real thought"
        assert out.action == "read_file"

    def test_action_only_empty_thought_ok(self):
        # No prose, no ## Thought — empty thought is allowed, not a failure.
        out = parse_prefix_md("## Action\nread_file\n\n## Input\n{}")
        assert out.action == "read_file"
        assert out.parse_stage == 1
        assert out.thought is None
