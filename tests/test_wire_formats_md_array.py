"""md_array wire format — parser, lenient terminal, history round-trip.

The shapes asserted here mirror what the omlx bakeoffs measured
(docs/inputs-array-schema/DESIGN.md §3): flat multi-op arrays parse at 100%,
and every "completion reach" the models actually emitted (omitted/empty
``## Action``, a ``None`` marker, a bare result object, header-less plain
text) reads as a terminal turn.
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
    def test_multi_op_and_no_complete(self):
        assert WF.multi_op is True
        assert WF.exposes_complete is False
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


class TestParseTurnTerminal:
    def test_thought_only(self):
        t = WF.parse_turn("## Thought\nAll done — tests pass.")
        assert t.terminal and not t.ops
        assert t.thought == "All done — tests pass."

    def test_empty_action_section(self):
        t = WF.parse_turn(_wire("done", ""))
        assert t.terminal

    def test_none_marker(self):
        for marker in ("None.", "none", "N/A", "nothing"):
            assert WF.parse_turn(_wire("done", marker)).terminal

    def test_bare_result_object(self):
        t = WF.parse_turn(_wire("d", '{"result": "all finished"}'))
        assert t.terminal
        assert t.thought == "all finished"  # answer = the result

    def test_plain_text_no_headers(self):
        t = WF.parse_turn("Hello! How can I help?")
        assert t.terminal
        assert t.thought == "Hello! How can I help?"

    def test_bare_op_json_is_work_not_terminal(self):
        # Phase-2 regression: header-less op JSON (the model dropped the
        # envelope) must be read as WORK ops — swallowing it as a terminal
        # answer made completed=100% while no tool ever ran.
        t = WF.parse_turn('[{"action": "read_file", "path": "src/auth.py"}]')
        assert not t.terminal
        assert [(o.action, o.action_input) for o in t.ops] == [
            ("read_file", {"path": "src/auth.py"})
        ]
        bare_obj = WF.parse_turn('{"action": "shell", "command": "ls"}')
        assert not bare_obj.terminal
        assert bare_obj.ops[0].action == "shell"

    def test_bare_json_without_action_still_terminal(self):
        # A bare {"result": ...} (no action key) stays a completion attempt.
        t = WF.parse_turn('{"result": "all done"}')
        assert t.terminal

    def test_input_residue_tail_is_terminal(self):
        # Phase-2 dominant loop: the model FINISHING with its prefix_md prior
        # leaking through — empty ## Action + stray `## Input` + `{}`. That
        # is a completion attempt, not a missing action (it looped NO_ACTION
        # recovery 10-13x per run before this tolerance).
        t = WF.parse_turn(
            "## Thought\nAll done, reporting.\n\n## Action\n\n\n## Input\n{}"
        )
        assert t.terminal
        assert t.thought == "All done, reporting."

    def test_input_residue_without_json_is_terminal(self):
        t = WF.parse_turn("## Thought\ndone\n\n## Action\n\n## Input\n")
        assert t.terminal

    def test_empty_object_op_is_terminal(self):
        # `## Action\n{}` — nothing to run = completion attempt.
        t = WF.parse_turn(_wire("done", "{}"))
        assert t.terminal
        t2 = WF.parse_turn(_wire("done", "[{}]"))
        assert t2.terminal

    def test_actionless_op_with_real_input_stays_op(self):
        # An op that DOES carry input but dropped its action is work intent —
        # it must stay an op (NO_ACTION recovery), not become terminal.
        t = WF.parse_turn(_wire("read it", '[{"path": "a.py"}]'))
        assert not t.terminal
        assert t.ops[0].action is None
        assert t.ops[0].action_input == {"path": "a.py"}

    def test_no_action_reminder_mentions_omit_when_done(self):
        # The recovery wording must offer the DONE exit, or a finishing model
        # loops against "add an action" forever (Phase-2 pattern).
        assert "OMIT" in WF.constraint_reminder_action_required()

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

    def test_terminal_record_round_trips(self):
        rec = WF.serialize_assistant_for_history("## Thought\nfinished")
        assert rec == {"role": "assistant", "thought": "finished", "terminal": True}
        rendered = WF.render_assistant_from_history(rec)
        assert WF.parse_turn(rendered["content"]).terminal

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
            thought=None, action="ready_for_review", action_input='{"summary": "s"}'
        )
        t = WF.parse_turn(ex)
        assert t.ops[0].action == "ready_for_review"


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

    def test_two_ops_then_gated_finish(self, tmp_path):
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
            "## Thought\n다 끝났습니다",
            "## Thought\n다 끝났습니다",
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
        # gate: exactly one ready_for_review observation
        reviews = [
            m
            for m in raw
            if m.get("role") == "user" and m.get("tool") == "ready_for_review"
        ]
        assert len(reviews) == 1
