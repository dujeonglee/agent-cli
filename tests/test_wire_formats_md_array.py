"""md_array wire format — parser, completion (`complete` op), history round-trip.

Flat multi-op arrays parse at 100% (the shape the omlx bakeoffs measured,
DESIGN §3). Completion is an explicit `complete` op — the proven
prefix_md/react model, revived after thought-only termination produced a
recurring class of finish bugs (DESIGN Exp 8). A thought-only / empty / no-op
emission is therefore NOT a completion: it parses to 0 ops so the loop nudges
the model (NO_ACTION) to call `complete` or emit work.
"""

from __future__ import annotations

import json

from unittest.mock import MagicMock

from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.wire_formats import get as get_wire_format

WF = get_wire_format("md_array")


def _wire(thought: str, action_body: str | None) -> str:
    out = f"## Thought\n{thought}"
    if action_body is not None:
        out += f"\n\n## Action\n{action_body}"
    return out


class TestFlags:
    def test_multi_op_and_complete_exposed(self):
        # Completion is an explicit `complete` op (proven prefix_md/react
        # model), NOT thought-only — exposes_complete reverted to True
        # (DESIGN Exp 8: thought-only termination caused a recurring class of
        # finish bugs, fixed at the origin by reviving complete).
        assert WF.multi_op is True
        assert WF.exposes_complete is True
        assert WF.thought_required is False
        assert WF.action_required is False

    def test_json_mode_always_off(self):
        # Phase-2 regression: the base default leaked json_mode=True
        # (supports_structured_output), which forces a leading `{` and makes
        # the markdown envelope impossible — every turn degraded to bare JSON.
        caps = ModelCapabilities(
            context_window=1,
            max_output_tokens=1,
            supports_structured_output=True,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        assert WF.provider_call_kwargs(caps) == {"json_mode": False}

    def test_registered(self):
        from agent_cli.wire_formats import list_names

        assert "md_array" in list_names()


class TestParseTurnWork:
    def test_multi_op_array(self):
        t = WF.parse_turn(
            _wire(
                "read two things",
                '[{"action": "read_file", "path": "a.py"},'
                ' {"action": "shell", "command": "ls"}]',
            )
        )
        assert not t.terminal
        assert [(o.action, o.action_input) for o in t.ops] == [
            ("read_file", {"path": "a.py"}),
            ("shell", {"command": "ls"}),
        ]
        assert t.thought == "read two things"
        assert t.parse_stage == 1

    def test_bare_object_is_one_op(self):
        t = WF.parse_turn(_wire("x", '{"action": "shell", "command": "ls"}'))
        assert len(t.ops) == 1
        assert t.ops[0].action == "shell"

    def test_fenced_array_accepted(self):
        t = WF.parse_turn(
            _wire("x", '```json\n[{"action": "shell", "command": "ls"}]\n```')
        )
        assert len(t.ops) == 1

    def test_op_without_action_preserved_for_recovery(self):
        # parse invariant: the input survives so NO_ACTION recovery can echo.
        t = WF.parse_turn(_wire("x", '[{"path": "a.py", "extra": 1}]'))
        assert not t.terminal
        assert len(t.ops) == 1
        assert t.ops[0].action is None
        assert t.ops[0].action_input == {"path": "a.py", "extra": 1}

    def test_malformed_action_body_is_parse_failure(self):
        t = WF.parse_turn(_wire("x", '[{"action": "shell", "command": }]'))
        assert t.parse_stage == 0
        assert t.ops == []
        assert not t.terminal

    def test_missing_thought_header_recovers_leading_prose(self):
        # Model emitted reasoning WITHOUT the `## Thought` header, then
        # `## Action`. The leading prose is recovered as the thought (was
        # dropped → thought=None before).
        t = WF.parse_turn(
            "Need to inspect mgt.c first.\n\n"
            '## Action\n[{"action": "read_file", "path": "mgt.c"}]'
        )
        assert t.thought == "Need to inspect mgt.c first."
        assert [(o.action, o.action_input) for o in t.ops] == [
            ("read_file", {"path": "mgt.c"})
        ]
        assert t.parse_stage == 1

    def test_action_first_no_thought(self):
        # `## Action` is the very first thing → no leading prose → thought None.
        t = WF.parse_turn('## Action\n[{"action": "complete", "result": "done"}]')
        assert t.thought is None
        assert t.ops[0].action == "complete"


class TestAnonymousObjectRepair:
    """Batching ops with large params, the model wraps each op's params in an
    ANONYMOUS nested object — invalid JSON → the whole turn was NO_JSON and
    nothing ran. Two shapes seen (DESIGN Exp 8), consistent within an emission:
      A. `{"action": X, {params}}`  (anon AND op close — session 1781208482, 27B)
      B. `{"action": X, {params}`   (one `}` reused — session 1781210802, 35B)
    `_extract_op_json` tries both and keeps whichever parses; a recovered turn
    is parse_stage 2 (drift)."""

    def test_pattern_A_balanced_braces_recovers(self):
        # {"action": X, {params}}  → both anon and op close.
        t = WF.parse_turn(
            _wire("x", '[{"action": "write_file", {"path": "a.c", "content": "x"}}]')
        )
        assert t.parse_stage == 2
        assert len(t.ops) == 1 and t.ops[0].action == "write_file"
        assert t.ops[0].action_input == {"path": "a.c", "content": "x"}

    def test_pattern_B_single_brace_recovers(self):
        # {"action": X, {params}  → the model reuses the anon `}` as the op `}`,
        # so the array has N unbalanced `{`. This is the 35B live failure.
        t = WF.parse_turn(
            _wire(
                "two files",
                '[{"action": "write_file", {"path": "a"}, '
                '{"action": "write_file", {"path": "b"}]',
            )
        )
        assert t.parse_stage == 2
        assert [o.action for o in t.ops] == ["write_file", "write_file"]
        assert [o.action_input["path"] for o in t.ops] == ["a", "b"]

    def test_pattern_B_mixed_with_valid_op(self):
        # Live shape: a valid op (shell) followed by malformed write_file ops.
        t = WF.parse_turn(
            _wire(
                "setup",
                '[{"action": "shell", "command": "mkdir src"}, '
                '{"action": "write_file", {"path": "a.c", "content": "x"}]',
            )
        )
        assert [o.action for o in t.ops] == ["shell", "write_file"]
        assert t.ops[1].action_input == {"path": "a.c", "content": "x"}

    def test_string_safe_with_braces_in_content(self):
        # C code in `content` has its own {}/"/escapes — must not break matching
        # (both A and B). Use pattern B (the worse, real one).
        raw = _wire(
            "c file",
            '[{"action": "write_file", '
            '{"path": "x.c", "content": "int main(){ return \\"}\\"; }"}]',
        )
        t = WF.parse_turn(raw)
        assert len(t.ops) == 1
        assert t.ops[0].action_input["content"] == 'int main(){ return "}"; }'

    def test_headerless_prose_then_malformed_recovers(self):
        # reasoning prose, then a header-less malformed op array (pattern B).
        raw = (
            "Good, creating the headers in a batch.\n\n"
            '[{"action": "write_file", {"path": "i_video.h", "content": "#ifndef X"}]'
        )
        t = WF.parse_turn(raw)
        assert len(t.ops) == 1 and t.ops[0].action == "write_file"
        assert t.parse_stage == 2

    def test_valid_json_unchanged_and_stage1(self):
        from agent_cli.wire_formats.md_array import _repair_anonymous_op_objects

        valid = '[{"action": "read_file", "path": "a"}, {"action": "shell", "command": "ls"}]'
        # both variants leave valid JSON untouched
        assert _repair_anonymous_op_objects(valid, drop_close=True) == valid
        assert _repair_anonymous_op_objects(valid, drop_close=False) == valid
        # and a valid turn is not flagged as a drift recovery
        assert WF.parse_turn(_wire("x", valid)).parse_stage == 1

    def test_unrecoverable_garbage_stays_parse_failure(self):
        # Genuinely broken JSON (not the anon-object shape) must not be
        # force-"recovered" into bogus ops.
        t = WF.parse_turn(_wire("x", '[{"action": "shell", "command": }]'))
        assert t.parse_stage == 0 and t.ops == []


class TestLiteralControlCharRepair:
    """The model writes a big multi-line `result`/`content` blob with REAL
    newlines (and tabs) instead of `\\n` escapes — invalid strict JSON
    ("Invalid control character"), so the whole turn was flagged invalid JSON
    and nothing ran. The shape is impossible to see in a terminal/echo (a real
    newline and `\\n` render identically). `_extract_op_json` re-parses
    leniently (``json.loads(strict=False)``) as a final fallback, recovering
    the turn at parse_stage 2 (a drift recovery — the signal that the JSON was
    non-strict is kept). Reproduced live: session 1781213377 `complete` op."""

    def test_complete_with_literal_newlines_recovers(self):
        # The live case: a `complete` whose result is a markdown blob with real
        # line breaks. (The `\n` in this Python literal ARE real newlines.)
        result = "## Done\n\n1. build ok\n2. tests pass\n8. ESC exit"
        t = WF.parse_turn(
            _wire("done", '[{"action": "complete", "result": "' + result + '"}]')
        )
        assert t.parse_stage == 2
        assert len(t.ops) == 1 and t.ops[0].action == "complete"
        # The newlines are preserved verbatim in the recovered value.
        assert t.ops[0].action_input["result"] == result

    def test_write_file_content_with_literal_newlines_recovers(self):
        content = "#include <stdio.h>\n\nint main(){\n    return 0;\n}"
        t = WF.parse_turn(
            _wire(
                "write",
                '[{"action": "write_file", "path": "a.c", "content": "'
                + content
                + '"}]',
            )
        )
        assert t.parse_stage == 2
        assert t.ops[0].action_input["content"] == content

    def test_headerless_complete_with_literal_newlines_recovers(self):
        # FINISHING model: prose then a header-less complete array with real
        # newlines in the result.
        raw = (
            "All criteria met, finishing.\n\n"
            '[{"action": "complete", "result": "line A\nline B"}]'
        )
        t = WF.parse_turn(raw)
        assert t.parse_stage == 2
        assert t.ops[0].action == "complete"
        assert t.ops[0].action_input["result"] == "line A\nline B"

    def test_literal_tab_in_string_recovers(self):
        # Tabs are control chars too (0x09) — same strict rejection.
        t = WF.parse_turn(
            _wire("x", '[{"action": "write_file", "path": "m", "content": "a\tb"}]')
        )
        assert t.parse_stage == 2
        assert t.ops[0].action_input["content"] == "a\tb"

    def test_multi_op_with_one_literal_newline_op_recovers(self):
        # A clean op + an op carrying literal newlines: the whole array is
        # invalid strict JSON, lenient reparse recovers BOTH ops.
        t = WF.parse_turn(
            _wire(
                "two",
                '[{"action": "shell", "command": "ls"}, '
                '{"action": "write_file", "path": "a", "content": "x\ny"}]',
            )
        )
        assert t.parse_stage == 2
        assert [o.action for o in t.ops] == ["shell", "write_file"]
        assert t.ops[1].action_input["content"] == "x\ny"

    def test_escaped_newlines_stay_stage1_no_regression(self):
        # Properly ESCAPED \n (the correct form) must still parse clean at
        # stage 1 — the lenient path is a fallback, never the primary.
        body = json.dumps([{"action": "complete", "result": "a\nb\nc"}])
        t = WF.parse_turn(_wire("ok", body))
        assert t.parse_stage == 1
        assert t.ops[0].action_input["result"] == "a\nb\nc"

    def test_lenient_does_not_rescue_truly_broken_json(self):
        # strict=False only relaxes control chars — a missing value is still
        # broken and must stay a parse failure (no bogus op).
        t = WF.parse_turn(_wire("x", '[{"action": "shell", "command": }]'))
        assert t.parse_stage == 0 and t.ops == []


class TestCompletionAndNoAction:
    """md_array completes via an explicit `complete` op (DESIGN Exp 8) — NOT
    thought-only. A thought-only / empty / no-op emission is therefore NOT a
    completion: it parses to 0 ops so the loop's NO_ACTION recovery nudges the
    model to call `complete` or emit work. ``terminal`` is never set."""

    def test_complete_is_an_op_not_a_terminal_flag(self):
        t = WF.parse_turn(_wire("done", '[{"action": "complete", "result": "ok"}]'))
        assert not t.terminal
        assert len(t.ops) == 1
        assert t.ops[0].action == "complete"
        assert t.ops[0].action_input == {"result": "ok"}

    def test_thought_only_is_no_action_not_terminal(self):
        t = WF.parse_turn("## Thought\nAll done — tests pass.")
        assert not t.terminal
        assert t.ops == []
        assert t.thought == "All done — tests pass."
        assert t.parse_stage == 1  # valid parse, just no op → NO_ACTION nudge

    def test_empty_action_section_is_no_action(self):
        t = WF.parse_turn(_wire("done", ""))
        assert not t.terminal and t.ops == []

    def test_plain_text_no_headers_is_no_action(self):
        t = WF.parse_turn("Hello! How can I help?")
        assert not t.terminal and t.ops == []
        assert t.thought == "Hello! How can I help?"

    def test_bare_op_json_is_work(self):
        # Header-less op JSON (envelope dropped) is read as WORK ops.
        t = WF.parse_turn('[{"action": "read_file", "path": "src/auth.py"}]')
        assert not t.terminal
        assert [(o.action, o.action_input) for o in t.ops] == [
            ("read_file", {"path": "src/auth.py"})
        ]
        bare_obj = WF.parse_turn('{"action": "shell", "command": "ls"}')
        assert bare_obj.ops[0].action == "shell"

    def test_headerless_complete_after_prose_is_not_lost(self):
        # Live (delegate explorer, DESIGN Exp 8): a FINISHING model wrote its
        # reasoning then appended `[{"action":"complete","result":<full
        # analysis>}]` with NO `## Action` header. The header-less recovery
        # only fired when the text STARTED with a bracket, so the complete op —
        # carrying the entire deliverable in `result` — was discarded (→
        # NO_ACTION → empty result). Now the op array is extracted even after
        # prose.
        raw = (
            "모든 파일을 읽었으므로 분석을 완료하겠습니다.\n\n"
            '[{"action": "complete", "result": "# Full analysis report ..."}]'
        )
        t = WF.parse_turn(raw)
        assert len(t.ops) == 1
        assert t.ops[0].action == "complete"
        assert t.ops[0].action_input["result"].startswith("# Full analysis")

    def test_prose_with_stray_bracket_no_action_falls_through(self):
        # A stray non-op bracket in prose must NOT be mistaken for ops — it
        # falls through to the NO_ACTION nudge (no spurious op).
        t = WF.parse_turn("the array [1, 2, 3] is sorted; done.")
        assert t.ops == []

    def test_input_residue_stripped_to_no_action(self):
        # prefix_md prior leak (`## Action\n\n## Input\n{}`): stripped to 0 ops
        # → NO_ACTION nudge (no longer mistaken for completion).
        t = WF.parse_turn(
            "## Thought\nAll done, reporting.\n\n## Action\n\n\n## Input\n{}"
        )
        assert not t.terminal and t.ops == []
        assert t.thought == "All done, reporting."

    def test_empty_containers_are_no_action(self):
        for body in ("{}", "[{}]", "[]"):
            t = WF.parse_turn(_wire("done", body))
            assert not t.terminal and t.ops == [], body
            assert t.parse_stage == 1, body  # thought present → NO_ACTION nudge

    def test_no_op_without_thought_is_parse_failure(self):
        # No thought + empty container → nothing usable at all → stage 0.
        assert WF.parse_turn("## Action\n[]").parse_stage == 0

    def test_nonempty_nondict_array_yields_no_ops(self):
        # `[1,2,3]` has no dict ops — same "no usable op" family as []/{}: with
        # a thought it's a NO_ACTION nudge (stage 1), never runnable ops.
        t = WF.parse_turn(_wire("x", "[1, 2, 3]"))
        assert not t.terminal and t.ops == []
        # without a thought there is nothing usable at all → parse failure
        assert WF.parse_turn("## Action\n[1, 2, 3]").parse_stage == 0

    def test_actionless_op_with_real_input_stays_op(self):
        # An op that DOES carry input but dropped its action is work intent —
        # it stays an op (NO_ACTION recovery / infer), not dropped.
        t = WF.parse_turn(_wire("read it", '[{"path": "a.py"}]'))
        assert not t.terminal
        assert t.ops[0].action is None
        assert t.ops[0].action_input == {"path": "a.py"}

    def test_action_required_reminder_points_to_complete(self):
        # The recovery wording must point a finishing model at `complete`,
        # not the old "OMIT ## Action" thought-only exit.
        r = WF.constraint_reminder_action_required()
        assert "complete" in r and "OMIT" not in r

    def test_blank_emission_is_no_output(self):
        t = WF.parse_turn("   \n  ")
        assert t.parse_stage == 0
        assert not t.terminal


class TestHistoryRoundTrip:
    def test_ops_record_round_trips(self):
        raw = _wire(
            "do two",
            '[{"action": "read_file", "path": "a.py"},'
            ' {"action": "shell", "command": "ls"}]',
        )
        rec = WF.serialize_assistant_for_history(raw)
        assert rec["thought"] == "do two"
        assert rec["ops"] == [
            {"action": "read_file", "action_input": {"path": "a.py"}},
            {"action": "shell", "action_input": {"command": "ls"}},
        ]
        rendered = WF.render_assistant_from_history(rec)
        t = WF.parse_turn(rendered["content"])
        assert [(o.action, o.action_input) for o in t.ops] == [
            ("read_file", {"path": "a.py"}),
            ("shell", {"command": "ls"}),
        ]

    def test_complete_op_record_round_trips(self):
        # Completion is a normal `complete` op now (no terminal record).
        raw = _wire("finished", '[{"action": "complete", "result": "all green"}]')
        rec = WF.serialize_assistant_for_history(raw)
        assert rec["ops"] == [
            {"action": "complete", "action_input": {"result": "all green"}}
        ]
        rendered = WF.render_assistant_from_history(rec)
        t = WF.parse_turn(rendered["content"])
        assert not t.terminal
        assert t.ops[0].action == "complete"
        assert t.ops[0].action_input == {"result": "all green"}

    def test_thought_only_record_is_content_fallback(self):
        # A 0-op (thought-only) emission has no ops → stored as sanitized
        # content (not a terminal record, which no longer exists).
        rec = WF.serialize_assistant_for_history("## Thought\nfinished")
        assert "terminal" not in rec
        assert rec.get("content") is not None or rec.get("ops") == []

    def test_garbage_falls_back_to_sanitized_content(self):
        rec = WF.serialize_assistant_for_history("## Thought\n\n## Action\n[{broken")
        assert rec.get("content") is not None
        assert "## Thought" not in rec["content"]  # sentinels stripped


class TestSanitizeAndDegenerate:
    def test_sanitize_strips_lone_headers(self):
        assert WF.sanitize_thought("ok\n## Action\nrest") == "ok\n\nrest".replace(
            "\n\n", "\n\n"
        ) or "## Action" not in WF.sanitize_thought("ok\n## Action\nrest")

    def test_inline_mention_preserved(self):
        s = WF.sanitize_thought("the ## Action section is next")
        assert "## Action" in s

    def test_degenerate_repeated_empty_headers(self):
        assert WF.is_degenerate("## Thought\n## Action\n## Thought\n## Action") is True

    def test_normal_turn_not_degenerate(self):
        assert WF.is_degenerate(_wire("x", '[{"action": "shell"}]')) is False


class TestFormatRulesBatchSteering:
    """Multi-op uptake nudge (DESIGN §6, B): the format rules must actively
    steer the model to batch INDEPENDENT ops into one turn — and must keep
    the two guardrails that stop this from causing regressions:
      (1) dependent ops split across turns (a later op needs an earlier
          result; all ops in a turn run before any observation), and
      (2) no nested arrays inside one op (nesting broke 27B, DESIGN §3).
    Losing either guardrail is the failure mode this nudge risks, so both
    are asserted alongside the steering."""

    # Whitespace-normalized so assertions survive the prose line-wrapping.
    rules = " ".join(WF.format_rules().split())
    low = rules.lower()

    def test_steers_batching_independent_ops(self):
        assert "batch independent work into one turn" in self.low
        # the active decision cue, not just a passive "you may add elements"
        assert "goes in this turn as a separate array element" in self.low

    def test_keeps_dependent_split_guardrail(self):
        assert "only when a later step needs an earlier step's result" in self.low

    def test_keeps_no_nesting_guardrail(self):
        assert "no nested arrays" in self.low
        assert "never put a list of items inside a single op" in self.low

    def test_example_models_multifile_read_batch(self):
        # The dominant missed-batch pattern was consecutive single read_file
        # turns, so the worked example shows several read_file ops at once.
        # (format_rules now has two `## Action` examples — the batch one and a
        # `complete` finish one; assert the batch op count over the whole text.)
        assert WF.format_rules().count('"action": "read_file"') >= 3
        # and it parses as a real multi-op turn
        from agent_cli.wire_formats import get as _get

        ex = (
            "## Thought\nx\n\n## Action\n"
            '[{"action": "read_file", "path": "a.py"}, '
            '{"action": "read_file", "path": "b.py"}, '
            '{"action": "read_file", "path": "c.py"}]'
        )
        t = _get("md_array").parse_turn(ex)
        assert len(t.ops) == 3 and all(o.action == "read_file" for o in t.ops)


class TestRenderHelpers:
    def test_render_action_input_flattens_prefixed(self):
        out = WF.render_action_input({"read_file_path": "a.py", "read_file_stat": True})
        assert json.loads(out) == {"action": "read_file", "path": "a.py", "stat": True}

    def test_render_full_example_wraps_array(self):
        ex = WF.render_full_example(
            thought="t",
            action="shell",
            action_input='{"action": "shell", "command": "ls"}',
        )
        t = WF.parse_turn(ex)
        assert t.ops[0].action == "shell"

    def test_render_full_example_splices_missing_action(self):
        # Virtual tools authored with standard keys (no "action" inside).
        ex = WF.render_full_example(
            thought=None, action="complete", action_input='{"result": "s"}'
        )
        t = WF.parse_turn(ex)
        assert t.ops[0].action == "complete"


class TestEndToEnd:
    """Real md_array plugin driving the real loop (no mock format)."""

    def _caps(self):
        return ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

    def test_two_ops_then_complete_finish(self, tmp_path):
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import AgentLoop

        f1 = tmp_path / "a.txt"
        f1.write_text("alpha")
        responses = [
            _wire(
                "read it and list dir",
                json.dumps(
                    [
                        {"action": "read_file", "path": str(f1)},
                        {"action": "shell", "command": f"ls {tmp_path}"},
                    ]
                ),
            ),
            # Completion is now an explicit `complete` op — the result field
            # carries the deliverable (no thought-only terminal, no gate).
            _wire(
                "다 끝났습니다", '[{"action": "complete", "result": "다 끝났습니다"}]'
            ),
        ]
        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=r) for r in responses]
        ctx = ContextManager(session_dir=tmp_path)
        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=self._caps(),
            model="m",
            ctx=ctx,
            max_turns=6,
            wire_format=WF,
        )
        result = loop.run()
        assert result.success
        assert result.output == "다 끝났습니다"
        raw = ctx.get_raw_messages()
        combined = [
            m
            for m in raw
            if m.get("role") == "user"
            and "[1/2] read_file — OK" in m.get("content", "")
        ]
        assert len(combined) == 1
        assert "alpha" in combined[0]["content"]
        # multi-op assistant record persisted with the op list
        op_records = [m for m in raw if m.get("role") == "assistant" and m.get("ops")]
        assert op_records and op_records[0]["ops"][0]["action"] == "read_file"

    def test_thought_only_nudges_then_completes(self, tmp_path):
        # A thought-only emission is NOT a completion — it gets a NO_ACTION
        # nudge; the model then calls `complete` to actually finish.
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import AgentLoop

        responses = [
            "## Thought\nI think I'm done.",  # thought-only → NO_ACTION nudge
            _wire("done", '[{"action": "complete", "result": "finished"}]'),
        ]
        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=r) for r in responses]
        ctx = ContextManager(session_dir=tmp_path)
        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=self._caps(),
            model="m",
            ctx=ctx,
            max_turns=6,
            wire_format=WF,
        )
        result = loop.run()
        assert result.success
        assert result.output == "finished"
