"""Multi-op execution path (DESIGN §6, step 3b).

Covers the two new pieces:
- ``Tool.wrap_single_op`` — a multi-op format's flat single-target op is
  re-wrapped into the tool's canonical prefixed (batch) input, so the
  existing validate → strip → run pipeline applies unchanged.
- the loop's N-op dispatch — ops run sequentially in array order, regular
  tool ops accumulate into ONE combined observation (per-op OK/FAIL headers,
  any-fail ⇒ turn failed), turn-ending ops flush accumulated work first,
  and a `complete` op finishes the loop (a thought-only/0-op turn is a
  NO_ACTION nudge, not a completion — DESIGN Exp 8).

Single-action formats are guarded elsewhere (full suite + prompt snapshots);
here a mock multi-op format drives the new path directly.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock


from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.registry import TOOLS
from agent_cli.wire_formats.base import Op, ParsedAction, ParsedTurn, WireFormat


# ─── Tool.wrap_single_op ────────────────────────────


class TestWrapSingleOp:
    def test_read_file_wraps_flat_path(self):
        out = TOOLS["read_file"].wrap_single_op({"path": "a.py", "stat": True})
        assert out == {"read_file_reads": [{"path": "a.py", "stat": True}]}

    def test_read_file_already_batch_passes_through(self):
        out = TOOLS["read_file"].wrap_single_op({"reads": [{"path": "a.py"}]})
        assert out == {"read_file_reads": [{"path": "a.py"}]}

    def test_edit_file_wraps_flat_edit(self):
        out = TOOLS["edit_file"].wrap_single_op(
            {"path": "a.py", "op": "replace", "pos": "2#KT", "lines": ["x"]}
        )
        assert out == {
            "edit_file_path": "a.py",
            "edit_file_edits": [{"op": "replace", "pos": "2#KT", "lines": ["x"]}],
        }

    def test_edit_file_already_batch_passes_through(self):
        flat = {"path": "a.py", "edits": [{"op": "delete", "pos": "1#AA"}]}
        out = TOOLS["edit_file"].wrap_single_op(flat)
        assert out == {
            "edit_file_path": "a.py",
            "edit_file_edits": [{"op": "delete", "pos": "1#AA"}],
        }

    def test_code_index_wraps_flat_query(self):
        out = TOOLS["code_index"].wrap_single_op({"mode": "list", "path": "a.py"})
        assert out == {"code_index_queries": [{"mode": "list", "path": "a.py"}]}

    def test_delegate_wraps_flat_task(self):
        out = TOOLS["delegate"].wrap_single_op({"task": "do x", "context": "fork"})
        assert out == {"delegate_tasks": [{"task": "do x", "context": "fork"}]}

    def test_default_is_prefix_only(self):
        # Non-batch tools: keys get the canonical prefix, no structural change.
        out = TOOLS["shell"].wrap_single_op({"command": "ls"})
        assert out == {"shell_command": "ls"}


# ─── Mock multi-op wire format ──────────────────────


class _MultiOpFormat(WireFormat):
    """Test format: the LLM 'emission' is a JSON object
    ``{"thought": ..., "ops": [{"action": ..., ...params}], "terminal": bool}``
    that parse_turn maps straight onto ParsedTurn — bypassing real wire
    syntax so the tests drive the LOOP, not a parser."""

    name = "_multi_op_test"
    thought_required = False
    action_required = False
    multi_op = True
    # Completion is an explicit `complete` op (md_array's model), not a
    # terminal flag — exposes_complete True, parse_turn never sets terminal.
    exposes_complete = True

    def parse_turn(self, llm_text: str) -> ParsedTurn:
        try:
            obj = json.loads(llm_text)
        except json.JSONDecodeError:
            return ParsedTurn(raw=llm_text, parse_stage=0)
        ops = [
            Op(
                action=o.get("action"),
                action_input={k: v for k, v in o.items() if k != "action"},
            )
            for o in obj.get("ops", [])
        ]
        return ParsedTurn(
            thought=obj.get("thought"),
            ops=ops,
            raw=llm_text,
            parse_stage=1,
        )

    def parse(self, llm_text: str) -> ParsedAction:
        # history-serialization fallback only; the loop uses parse_turn.
        return ParsedAction(raw=llm_text, parse_stage=1)

    def render_full_example(self, *, thought, action, action_input) -> str:
        return json.dumps(
            {"thought": thought or "", "ops": [{"action": action}]},
            ensure_ascii=False,
        )

    def format_rules_anchor(self) -> str:
        return "Mock multi-op anchor."

    def format_rules_field_specific(self) -> str:
        return "1. thought.\n2. ops."

    def constraint_reminder_call(self) -> str:
        return ""

    def constraint_reminder_action_required(self) -> str:
        return ""

    def failure_framing_parse_fail(self) -> str:
        return "Bad format."

    def failure_framing_no_action(self) -> str:
        return "No ops."

    def static_retry_hint_no_json(self) -> str:
        return "Emit thought+ops JSON."

    def static_retry_hint_no_action(self) -> str:
        return "Add ops."

    def system_user_prefixes(self) -> tuple[str, ...]:
        return ("Bad format.", "No ops.")


def _caps():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_structured_output=False,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


def _turn(thought="t", ops=None) -> str:
    return json.dumps({"thought": thought, "ops": ops or []})


def _finish(thought="done"):
    """Completion = a single `complete` op carrying the result (md_array's
    model since DESIGN Exp 8 — no thought-only terminal, no review gate)."""
    return [_turn(thought=thought, ops=[{"action": "complete", "result": thought}])]


def _run(responses, tmp_path, max_turns=5):
    from agent_cli.context.manager import ContextManager
    from agent_cli.loop import AgentLoop

    provider = MagicMock()
    provider.call.side_effect = [LLMResponse(content=r) for r in responses]
    ctx = ContextManager(session_dir=tmp_path)
    loop = AgentLoop(
        query="Q",
        provider=provider,
        capabilities=_caps(),
        model="m",
        ctx=ctx,
        max_turns=max_turns,
        wire_format=_MultiOpFormat(),
    )
    return loop.run(), ctx, provider


# ─── N-op dispatch ──────────────────────────────────


class TestMultiOpDispatch:
    def test_two_ops_one_combined_observation(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f1.write_text("alpha")
        f2 = tmp_path / "b.txt"
        f2.write_text("beta")
        result, ctx, _ = _run(
            [
                _turn(
                    ops=[
                        {"action": "read_file", "path": str(f1)},
                        {"action": "read_file", "path": str(f2)},
                    ]
                ),
                *_finish(),
            ],
            tmp_path,
        )
        assert result.success
        obs = [
            m
            for m in ctx.get_raw_messages()
            if m.get("role") == "user" and m.get("tool")
        ]
        # ONE combined observation for the 2-op turn (terminal adds none)
        combined = [m for m in obs if "[1/2]" in m.get("content", "")]
        assert len(combined) == 1
        content = combined[0]["content"]
        assert "[1/2] read_file — OK" in content
        assert "[2/2] read_file — OK" in content
        assert "alpha" in content and "beta" in content
        assert combined[0]["success"] is True
        assert combined[0]["tool"] == "read_file+read_file"

    def test_any_fail_marks_turn_failed(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f1.write_text("alpha")
        result, ctx, _ = _run(
            [
                _turn(
                    ops=[
                        {"action": "read_file", "path": str(f1)},
                        {"action": "read_file", "path": str(tmp_path / "missing.txt")},
                    ]
                ),
                *_finish(),
            ],
            tmp_path,
        )
        assert result.success  # the LOOP finishes; the turn obs is failed
        obs = [
            m
            for m in ctx.get_raw_messages()
            if m.get("role") == "user" and "[1/2]" in m.get("content", "")
        ]
        assert len(obs) == 1
        assert "[1/2] read_file — OK" in obs[0]["content"]
        assert "[2/2] read_file — FAILED" in obs[0]["content"]
        assert obs[0]["success"] is False  # any-fail ⇒ combined failure

    def test_flat_op_executes_via_wrap(self, tmp_path):
        # The op carries flat {"path": ...} — only wrap_single_op makes this
        # a valid read_file input, so success proves the wrap is applied.
        f1 = tmp_path / "a.txt"
        f1.write_text("alpha")
        result, ctx, _ = _run(
            [
                _turn(ops=[{"action": "read_file", "path": str(f1)}]),
                *_finish(),
            ],
            tmp_path,
        )
        assert result.success
        obs = [
            m
            for m in ctx.get_raw_messages()
            if m.get("role") == "user" and m.get("tool") == "read_file"
        ]
        assert obs and "alpha" in obs[0]["content"]

    def test_complete_op_ends_with_result(self, tmp_path):
        # Completion is an explicit `complete` op (DESIGN Exp 8): one turn,
        # result is the output, no review gate, no second turn.
        result, ctx, provider = _run(
            _finish(thought="모든 작업 완료했습니다"), tmp_path
        )
        assert result.success
        assert result.output == "모든 작업 완료했습니다"
        assert provider.call.call_count == 1  # single complete turn ends it

    def test_thought_only_turn_does_not_finish(self, tmp_path):
        # A thought-only (0-op) turn is NOT a completion — it gets a NO_ACTION
        # nudge; only a `complete` op actually ends the run.
        result, ctx, provider = _run(
            [_turn(thought="I think I'm done", ops=[]), *_finish(thought="real done")],
            tmp_path,
        )
        assert result.success
        assert result.output == "real done"
        assert provider.call.call_count == 2  # nudge turn, then complete

    def test_turn_ending_op_flushes_accumulated_first(self, tmp_path):
        # [read op, ask op]: the read executes and must be flushed as an
        # observation BEFORE the ask branch takes over the turn.
        f1 = tmp_path / "a.txt"
        f1.write_text("alpha")
        result, ctx, _ = _run(
            [
                _turn(
                    ops=[
                        {"action": "read_file", "path": str(f1)},
                        {"action": "ask", "question": "continue?"},
                    ]
                ),
            ],
            tmp_path,
            max_turns=1,
        )
        raw = ctx.get_raw_messages()
        flushed = [
            m
            for m in raw
            if m.get("role") == "user"
            and "[1/1] read_file — OK" in m.get("content", "")
        ]
        assert len(flushed) == 1
        assert "alpha" in flushed[0]["content"]

    def test_no_ops_goes_to_recovery(self, tmp_path):
        # A turn with zero usable ops (unparseable) = the model said nothing
        # usable → recovery hint, then a `complete` op finishes the run.
        result, ctx, provider = _run(
            ["{not json at all", *_finish()],
            tmp_path,
        )
        assert result.success
        # bad turn → recovery, then complete → end
        assert provider.call.call_count == 2
