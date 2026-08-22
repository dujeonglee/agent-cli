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
from typing import ClassVar
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

    def test_agent_flat_is_identity(self):
        # Flat-native (Step 3): delegate's wrap_single_op is identity — one op
        # = one task. Several delegate ops in a turn run in parallel (the loop
        # batches them), so no per-op canonical re-wrap.
        flat = {"task": "do x", "context": "fork"}
        assert TOOLS["agent"].wrap_single_op(flat) == flat

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
            parameters: ClassVar[dict] = {
                "type": "object",
                "properties": {"command": {}},
            }

            def _run(self, args, *, ctx=None):
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
    # Completion is an explicit `complete` op (json_fc's model), not a
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

    def format_rules(self) -> str:
        # v8.41.0 ABC: format_rules 가 추상 (anchor/field_specific 훅과
        # 사문 빌더는 제거됨 — 섹션은 포맷이 통째로 소유).
        return "Mock multi-op rules."

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
        supports_thinking=False,
    )


def _turn(thought="t", ops=None) -> str:
    return json.dumps({"thought": thought, "ops": ops or []})


def _finish(thought="done"):
    """Completion = a single `complete` op carrying the result (json_fc's
    model since DESIGN Exp 8 — no thought-only terminal, no review gate)."""
    return [_turn(thought=thought, ops=[{"action": "complete", "result": thought}])]


def _run(responses, tmp_path, max_turns=5, wire_format=None):
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
        wire_format=wire_format or _MultiOpFormat(),
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

    def test_unwired_parallel_safe_tool_runs_sequentially(self, tmp_path):
        """parallel_safe=True 지만 배치 엔진 미배선(_PARALLEL_BATCH_ENGINES
        밖) 도구의 연속 op 는 배치로 묶이지 않고 **순차 per-op** 로 실행된다 —
        종전엔 수집이 플래그만 보고 묶은 뒤 디스패치의 NotImplementedError 로
        런이 죽는 크래시 트랩(리뷰 §4.1 수리)."""
        from agent_cli.tools.base import Tool
        from agent_cli.tools.result import ToolResult

        calls: list[dict] = []

        class _ParTool(Tool):
            name = "partool"
            description = "synthetic parallel_safe tool without a batch engine"
            parallel_safe = True
            parameters: ClassVar[dict] = {
                "type": "object",
                "properties": {"item": {"type": "string"}},
                "required": [],
            }

            def wrap_single_op(self, flat):
                return flat

            def parallel_batchable(self, args):
                return True

            def _run(self, args, *, ctx=None):
                calls.append(dict(args))
                return ToolResult(True, output=f"ran {args.get('item')}")

        TOOLS["partool"] = _ParTool()
        try:
            result, ctx, _ = _run(
                [
                    _turn(
                        ops=[
                            {"action": "partool", "item": "a"},
                            {"action": "partool", "item": "b"},
                        ]
                    ),
                    *_finish(),
                ],
                tmp_path,
            )
        finally:
            del TOOLS["partool"]
        assert result.success  # NotImplementedError 로 죽지 않는다
        assert calls == [{"item": "a"}, {"item": "b"}]  # 순차 실행, 순서 보존
        obs = [
            m
            for m in ctx.get_raw_messages()
            if m.get("role") == "user" and "[1/2]" in m.get("content", "")
        ]
        assert len(obs) == 1  # 순차 per-op 도 하나의 combined 관찰로 합류
        assert "[1/2] partool — OK" in obs[0]["content"]
        assert "[2/2] partool — OK" in obs[0]["content"]

    def test_terminal_attribute_flushes_and_ends_turn(self, tmp_path):
        """턴-종결 flush 분기가 Tool.terminal **속성 파생**임을 실루프로 고정
        (T3 선언화 — 종전 ("complete","run_skill") 튜플 하드코딩): terminal=
        True 합성 도구의 op 을 만나면 ① 그때까지의 누적 결과를 먼저 flush
        하고 ② 그 op 을 디스패치한 뒤 ③ 턴을 끝낸다 — 배열 뒤의 op 은
        실행되지 않는다."""
        from agent_cli.tools.base import Tool
        from agent_cli.tools.result import ToolResult

        ran: list[str] = []

        class _TermTool(Tool):
            name = "termtool"
            description = "synthetic terminal tool"
            terminal = True
            parameters: ClassVar[dict] = {
                "type": "object",
                "properties": {"tag": {"type": "string"}},
                "required": [],
            }

            def wrap_single_op(self, flat):
                return flat

            def _run(self, args, *, ctx=None):
                ran.append(args.get("tag", ""))
                return ToolResult(True, output="term ran")

        f1 = tmp_path / "a.txt"
        f1.write_text("alpha")
        never = tmp_path / "never-read.txt"
        never.write_text("should not appear")

        TOOLS["termtool"] = _TermTool()
        try:
            result, ctx, _ = _run(
                [
                    _turn(
                        ops=[
                            {"action": "read_file", "path": str(f1)},
                            {"action": "termtool", "tag": "t1"},
                            {"action": "read_file", "path": str(never)},
                        ]
                    ),
                    *_finish(),
                ],
                tmp_path,
            )
        finally:
            del TOOLS["termtool"]
        assert result.success
        assert ran == ["t1"]  # terminal op 은 디스패치됨
        msgs = [m.get("content", "") for m in ctx.get_raw_messages()]
        # ① 누적 read 결과가 flush 됨 (단독 op 라 [1/1] combined)
        assert any("[1/1] read_file — OK" in c and "alpha" in c for c in msgs)
        # ③ terminal 뒤의 op 은 실행되지 않음
        assert not any("should not appear" in c for c in msgs)

    def test_complete_op_ends_with_result(self, tmp_path):
        # Completion is an explicit `complete` op (DESIGN Exp 8): one turn,
        # result is the output, no review gate, no second turn.
        result, _ctx, provider = _run(
            _finish(thought="모든 작업 완료했습니다"), tmp_path
        )
        assert result.success
        assert result.output == "모든 작업 완료했습니다"
        assert provider.call.call_count == 1  # single complete turn ends it

    def test_thought_only_turn_does_not_finish(self, tmp_path):
        # A thought-only (0-op) turn is NOT a completion — it gets a NO_ACTION
        # nudge; only a `complete` op actually ends the run.
        result, _ctx, provider = _run(
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
        _result, ctx, _ = _run(
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
        import agent_cli.loop.dispatch as loop_mod

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
        import agent_cli.loop.dispatch as loop_mod

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
        # 5.0.0: 브리지가 oneshot.tool_delegate 를 함수-내부 import 하므로
        # 소유 모듈을 patch 한다.
        import agent_cli.subagent.oneshot as oneshot_mod
        from agent_cli.tools.result import ToolResult

        calls = []

        def fake_tool_delegate(args, **kw):
            calls.append(args.get("tasks"))
            return ToolResult(True, output="STATUS: success\nRESULT:\nok")

        monkeypatch.setattr(oneshot_mod, "tool_delegate", fake_tool_delegate)
        return calls

    def test_two_delegate_ops_batched_into_one_parallel_call(
        self, tmp_path, monkeypatch
    ):
        calls = self._patch(monkeypatch)
        result, _, _ = _run(
            [
                _turn(
                    ops=[
                        {
                            "action": "agent",
                            "mode": "run",
                            "task": "Analyze A",
                            "context": "fork",
                        },
                        {
                            "action": "agent",
                            "mode": "run",
                            "task": "Analyze B",
                            "context": "fork",
                        },
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
                    ops=[
                        {
                            "action": "agent",
                            "mode": "run",
                            "task": "solo",
                            "profile": "explorer",
                        }
                    ]
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
        result, _ctx, provider = _run(
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
        _result, ctx, _ = _run(
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
        result, _ctx, _ = _run(
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


class TestProseRequiresExplicitComplete:
    """v8.4.0 — prose_completion 제거: action-less 산문 턴은 절대 암묵 완료되지
    않고 NO_ACTION 넛지를 받는다. 근거=프로덕션 반례(plan 스킬이 "Now let me
    write the plan document:" 35자를 결과로 조기 종료 — 2026-07-23 bakeoff 가
    0건으로 측정한 '중간 서술' 부류의 실사례). 완료는 도구-입력 의미론이고
    의미론은 엄격(v8.0.0 선) — 넛지 문구가 산문 답변의 재방출 경로를 직접
    가리켜 한 턴 안에 수렴시킨다."""

    def _get(self, name):
        from agent_cli.wire_formats import get

        return get(name)

    def test_json_fc_prose_final_answer_nudges_then_completes(self, tmp_path):
        # 순수 산문 최종답변 → 넛지 1회 → 명시적 complete 로 종결 (2 콜).
        result, _ctx, provider = _run(
            [
                "The src/ directory contains 2 files: auth.py and app.py.",
                (
                    '[{"action": "complete", "result": '
                    '"The src/ directory contains 2 files: auth.py and app.py."}]'
                ),
            ],
            tmp_path,
            wire_format=self._get("json_fc"),
        )
        assert result.success
        assert "auth.py" in result.output
        assert provider.call.call_count == 2  # 암묵 완료 없음 — 넛지 왕복

    def test_xml_fc_parity_prose_nudges_then_completes(self, tmp_path):
        result, _ctx, provider = _run(
            [
                "Here is the listing: auth.py and app.py.",
                (
                    "<tool_call><function=complete><parameter=result>"
                    "auth.py and app.py</parameter></function></tool_call>"
                ),
            ],
            tmp_path,
            wire_format=self._get("xml_fc"),
        )
        assert result.success
        assert "auth.py" in result.output
        assert provider.call.call_count == 2

    def test_transitional_prose_is_never_a_result(self, tmp_path):
        # 회귀 사례 원문: 전환 서술만 방출하고 생성이 끊긴 턴. 이 문장이
        # complete result 로 삼켜지면 안 된다 — 넛지 후 모델이 마저 작업.
        result, _ctx, provider = _run(
            [
                "Now let me write the plan document:",
                '[{"action": "complete", "result": "plan written"}]',
            ],
            tmp_path,
            wire_format=self._get("json_fc"),
        )
        assert result.success
        assert result.output == "plan written"
        assert "Now let me" not in result.output
        assert provider.call.call_count == 2

    def test_nudge_points_at_prose_reemission(self):
        # 넛지 문구가 '산문이 답이었으면 result 로 재방출'을 직접 안내해야
        # 수렴이 1턴에 끝난다 (양 포맷 의미 parity).
        for fmt in ("json_fc", "xml_fc"):
            reminder = self._get(fmt).constraint_reminder_action_required()
            assert "re-emit that answer" in reminder, fmt
            assert "complete" in reminder, fmt

    def test_broken_action_residue_also_nudges(self, tmp_path):
        # 깨진 액션 잔해도 종전대로 넛지 (제거 전에는 산문/잔해 분기가
        # 있었지만 이제 action-less 는 전부 같은 경로).
        result, _ctx, provider = _run(
            [
                '[{"action": "read_file", "path>/foo</parameter>',
                (
                    "<tool_call><function=complete><parameter=result>"
                    "done</parameter></function></tool_call>"
                ),
            ],
            tmp_path,
            wire_format=self._get("xml_fc"),
        )
        assert result.success
        assert provider.call.call_count == 2

    def test_no_synthetic_complete_in_history(self, tmp_path):
        # history 에 합성 complete 이 없어야 한다: 산문 턴은 산문 레코드로,
        # 종결은 모델의 명시적 complete 레코드로만.
        import json as _json

        result, _ctx, _provider = _run(
            [
                "The task is complete: both files were scanned.",
                '[{"action": "complete", "result": "both files were scanned"}]',
            ],
            tmp_path,
            wire_format=self._get("json_fc"),
        )
        assert result.success
        recs = [
            _json.loads(ln)
            for ln in (tmp_path / "history.jsonl").read_text().splitlines()
            if ln.strip()
        ]
        finals = [
            r
            for r in recs
            if r.get("role") == "assistant"
            and any(o.get("action") == "complete" for o in r.get("ops", []))
        ]
        assert len(finals) == 1  # 명시적 complete 하나뿐 (산문 턴의 합성분 없음)
        assert finals[0]["ops"][0]["action_input"]["result"] == (
            "both files were scanned"
        )
