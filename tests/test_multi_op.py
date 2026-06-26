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
    def test_read_file_flat_is_identity(self):
        # Flat-native (Step 3): read_file's wrap_single_op is identity — the
        # model's flat single-file op dispatches with no canonical re-wrap.
        flat = {"path": "a.py", "stat": True}
        assert TOOLS["read_file"].wrap_single_op(flat) == flat

    def test_edit_file_flat_is_identity(self):
        # Flat-native (Step 3): edit_file's wrap_single_op is identity — one
        # op carries one edit, dispatched with no canonical re-wrap.
        flat = {"path": "a.py", "op": "replace", "pos": "2#KT", "lines": ["x"]}
        assert TOOLS["edit_file"].wrap_single_op(flat) == flat

    def test_code_index_flat_is_identity(self):
        # Flat-native (Step 3): code_index's wrap_single_op is identity — one
        # op runs one query, dispatched with no canonical re-wrap.
        flat = {"mode": "list", "path": "a.py"}
        assert TOOLS["code_index"].wrap_single_op(flat) == flat

    def test_delegate_flat_is_identity(self):
        # Flat-native (Step 3): delegate's wrap_single_op is identity — one op
        # = one task. Several delegate ops in a turn run in parallel (the loop
        # batches them), so no per-op canonical re-wrap.
        flat = {"task": "do x", "context": "fork"}
        assert TOOLS["delegate"].wrap_single_op(flat) == flat

    def test_shell_flat_is_identity(self):
        # Flat-native (Step 3): shell is the last builtin to flatten — its
        # wrap_single_op is now identity too.
        assert TOOLS["shell"].wrap_single_op({"command": "ls"}) == {"command": "ls"}

    def test_base_default_wrap_is_add_prefix(self):
        # No builtin tool uses the base default wrap anymore (all flat-native →
        # identity, Step 3). A synthetic prefixed tool pins the base behavior,
        # kept for MCP / future prefixed tools.
        from agent_cli.tools.base import Tool
        from agent_cli.tools.result import ToolResult

        class _Prefixed(Tool):
            name = "synthtool"
            description = "x"
            parameters = {"type": "object", "properties": {"command": {}}}

            def _run(self, args, *, session_dir=None):
                return ToolResult(True, output="")

        assert _Prefixed().wrap_single_op({"command": "ls"}) == {
            "synthtool_command": "ls"
        }


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
        # run-length compressed (was "read_file+read_file")
        assert combined[0]["tool"] == "read_file×2"

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
        # [read op, complete op]: the read executes and must be flushed as an
        # observation BEFORE the terminal `complete` ends the turn.
        f1 = tmp_path / "a.txt"
        f1.write_text("alpha")
        result, ctx, _ = _run(
            [
                _turn(
                    ops=[
                        {"action": "read_file", "path": str(f1)},
                        {"action": "complete", "result": "done"},
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

    def test_ask_is_not_turn_ending_accumulates(self, tmp_path, monkeypatch):
        # ask is NOT terminal — [read, ask] both accumulate into ONE combined
        # observation (read=[1/2], ask=[2/2]), like a normal tool batch.
        import agent_cli.loop as loop_mod

        monkeypatch.setattr(loop_mod, "_handle_ask", lambda qs: "yes")
        f1 = tmp_path / "a.txt"
        f1.write_text("alpha")
        _result, ctx, _ = _run(
            [
                _turn(
                    ops=[
                        {"action": "read_file", "path": str(f1)},
                        {"action": "ask", "question": "continue?"},
                    ]
                ),
                *_finish(),
            ],
            tmp_path,
        )
        combined = [
            m["content"]
            for m in ctx.get_raw_messages()
            if m.get("role") == "user" and "[2/2] ask — OK" in m.get("content", "")
        ]
        assert len(combined) == 1
        assert "[1/2] read_file — OK" in combined[0] and "alpha" in combined[0]
        assert "User responded:\nyes" in combined[0]

    def test_multiple_ask_ops_batch_sequentially(self, tmp_path, monkeypatch):
        # several ask ops in one turn each prompt in sequence → ONE combined obs
        # (the read_file-style batch, applied to ask).
        import agent_cli.loop as loop_mod

        asked: list[list[str]] = []
        answers = iter(["A1", "A2"])

        def fake_ask(qs):
            asked.append(list(qs))
            return next(answers)

        monkeypatch.setattr(loop_mod, "_handle_ask", fake_ask)
        _result, ctx, _ = _run(
            [
                _turn(
                    ops=[
                        {"action": "ask", "question": "q1?"},
                        {"action": "ask", "question": "q2?"},
                    ]
                ),
                *_finish(),
            ],
            tmp_path,
        )
        assert asked == [["q1?"], ["q2?"]]  # one question per op, in order
        combined = [
            m["content"]
            for m in ctx.get_raw_messages()
            if m.get("role") == "user" and "[1/2] ask — OK" in m.get("content", "")
        ]
        assert len(combined) == 1
        assert "A1" in combined[0] and "A2" in combined[0]
        assert "[2/2] ask — OK" in combined[0]


class TestMultiOpDelegateParallel:
    """delegate is flat-native + ``parallel_safe`` (Step 3): a run of ≥2
    consecutive delegate ops in one turn is batched by the loop into ONE
    ``tool_delegate({tasks:[...]})`` call → ``_run_parallel`` (real
    concurrency). This is what makes the prompt's "several delegate ops run in
    parallel" actually true — the N-op loop is otherwise sequential."""

    def _patch(self, monkeypatch):
        import agent_cli.loop as loop_mod
        from agent_cli.tools.result import ToolResult

        calls = []

        def fake_tool_delegate(args, **kw):
            calls.append(args.get("tasks"))
            return ToolResult(True, output="STATUS: success\nRESULT:\nok")

        monkeypatch.setattr(loop_mod, "tool_delegate", fake_tool_delegate)
        return calls

    def test_two_delegate_ops_batched_into_one_parallel_call(
        self, tmp_path, monkeypatch
    ):
        calls = self._patch(monkeypatch)
        result, _, _ = _run(
            [
                _turn(
                    ops=[
                        {"action": "delegate", "task": "Analyze A", "context": "fork"},
                        {"action": "delegate", "task": "Analyze B", "context": "fork"},
                    ]
                ),
                *_finish(),
            ],
            tmp_path,
        )
        assert result.success
        # ONE tool_delegate call carrying BOTH tasks → _run_parallel path.
        assert len(calls) == 1
        assert len(calls[0]) == 2
        assert {t["task"] for t in calls[0]} == {"Analyze A", "Analyze B"}

    def test_single_delegate_op_runs_one_task_and_keeps_agent(
        self, tmp_path, monkeypatch
    ):
        # A lone delegate op takes the normal per-op path; _invoke_delegate
        # wraps the flat spec as {tasks:[it]} (sync) — preserving every field
        # incl. agent (the flat-native normalization fix).
        calls = self._patch(monkeypatch)
        result, _, _ = _run(
            [
                _turn(
                    ops=[{"action": "delegate", "task": "solo", "agent": "explorer"}]
                ),
                *_finish(),
            ],
            tmp_path,
        )
        assert result.success
        assert len(calls) == 1 and len(calls[0]) == 1
        assert calls[0][0]["task"] == "solo"
        assert calls[0][0]["agent"] == "explorer"

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


# ─── Same-file edit batching (consecutive edit_file ops → one apply) ──


class TestEditBatchGrouping:
    """A run of >=2 consecutive edit_file ops on the SAME path is grouped and
    applied via apply_edits_batch (one read, all refs against original,
    bottom-up, all-or-nothing). Non-consecutive / different-path edits and lone
    edits keep the normal per-op path."""

    @staticmethod
    def _ref(n, line):
        from agent_cli.tools.read_file import compute_line_hash

        return f"{n}#{compute_line_hash(n, line)}"

    def test_line_shifting_edits_grouped(self, tmp_path):
        # The case grouping is FOR: edit #1 inserts a line (shifting everything
        # down), so edit #2's ref (line 5) would go stale under sequential apply
        # — but grouped, both resolve against the ORIGINAL and land correctly.
        f = tmp_path / "f.txt"
        f.write_text("a\nb\nc\nd\ne\n")
        result, ctx, _ = _run(
            [
                _turn(
                    ops=[
                        {
                            "action": "edit_file",
                            "path": str(f),
                            "op": "append",
                            "pos": self._ref(1, "a"),
                            "lines": ["a2"],
                        },  # shifts down
                        {
                            "action": "edit_file",
                            "path": str(f),
                            "op": "replace",
                            "pos": self._ref(5, "e"),
                            "lines": ["E"],
                        },  # would go stale
                    ]
                ),
                *_finish(),
            ],
            tmp_path,
        )
        assert result.success
        assert f.read_text().splitlines() == ["a", "a2", "b", "c", "d", "E"]
        # one combined observation, the batch unit succeeded
        obs = [
            m
            for m in ctx.get_raw_messages()
            if m.get("role") == "user" and m.get("tool")
        ]
        combined = [m for m in obs if "edit" in (m.get("tool") or "")]
        assert combined and combined[0]["success"] is True

    def test_overlap_batch_fails_file_untouched(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\nb\nc\nd\ne\n")
        before = f.read_text()
        result, ctx, _ = _run(
            [
                _turn(
                    ops=[
                        {
                            "action": "edit_file",
                            "path": str(f),
                            "op": "replace",
                            "pos": self._ref(2, "b"),
                            "end": self._ref(4, "d"),
                            "lines": ["X"],
                        },
                        {
                            "action": "edit_file",
                            "path": str(f),
                            "op": "replace",
                            "pos": self._ref(3, "c"),
                            "lines": ["Y"],
                        },
                    ]
                ),
                *_finish(),
            ],
            tmp_path,
        )
        # batch rejected → file untouched (all-or-nothing)
        assert f.read_text() == before
        obs = [
            m
            for m in ctx.get_raw_messages()
            if m.get("role") == "user" and "overlap" in (m.get("content") or "")
        ]
        assert obs

    def test_different_files_not_grouped(self, tmp_path):
        f1 = tmp_path / "f1.txt"
        f1.write_text("a\nb\n")
        f2 = tmp_path / "f2.txt"
        f2.write_text("x\ny\n")
        result, ctx, _ = _run(
            [
                _turn(
                    ops=[
                        {
                            "action": "edit_file",
                            "path": str(f1),
                            "op": "replace",
                            "pos": self._ref(1, "a"),
                            "lines": ["A"],
                        },
                        {
                            "action": "edit_file",
                            "path": str(f2),
                            "op": "replace",
                            "pos": self._ref(1, "x"),
                            "lines": ["X"],
                        },
                    ]
                ),
                *_finish(),
            ],
            tmp_path,
        )
        assert result.success
        # both applied (separate files, separate per-op edits)
        assert f1.read_text().splitlines() == ["A", "b"]
        assert f2.read_text().splitlines() == ["X", "y"]
