"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import json
import re

from agent_cli.loop.skill_invoke import _handle_run_skill

# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.
from agent_cli.loop.state import _CONTINUE, _NOT_HANDLED, LoopConfig, LoopState

#: message/ask 빚을 남긴 채 `complete` 하려는 주체를 독촉하는 상한 (v9.21.0;
#: v9.22.0 부터 main·상주 공통, 질문(answer)·회신(reply) 공통).
#: 1회는 "깜빡함" 을, 3회면 "이해 못 함" 까지 잡는다. 사용자 결정. 무제한이면
#: 아무것도 런을 못 멈춘다 — 개입은 max_turns 미계수, B1 은 도구 경로에만
#: 있어 반복 complete 을 안 본다. 실측(a209hq): 플레이어 셋은 1회에 응했고
#: 오케스트레이터만 1회를 넘겼다.
MAX_DEBT_NAGS = 3
from agent_cli.loop.tool_bridge import ToolBridge
from agent_cli.recovery.common_recovery import format_action_loop_intervention
from agent_cli.recovery.detectors import (
    ActionLoopDetector,
    detect_nested_envelope,
    detect_schema_mismatch,
    detect_unknown_tool,
    unwrap_nested_envelope,
)
from agent_cli.recovery.observability import (
    FAILURE_ACTION_LOOP,
    FAILURE_DEGENERATE,
    FAILURE_FOREIGN_FORMAT,
    FAILURE_NESTED_ENVELOPE,
    FAILURE_NO_ACTION,
    FAILURE_NO_JSON,
    FAILURE_NO_OUTPUT,
    FAILURE_SCHEMA_MISMATCH,
    FAILURE_UNKNOWN_TOOL,
)
from agent_cli.recovery.primitives import echo_prior_output
from agent_cli.recovery.wf_recovery import (
    format_no_action_retry,
    format_no_json_retry,
)
from agent_cli.render import (
    render_recovery,
    render_run_ended,
    render_step,
)
from agent_cli.tools import TOOLS, infer_action
from agent_cli.tools.result import ToolResult
from agent_cli.verbose import debug_log as _debug_log
from agent_cli.wire_formats import try_foreign_parse

# parallel_safe 배치 디스패치 엔진이 실제로 배선된 도구들.
# ``_dispatch_parallel_batch`` 는 도구별 병렬 엔진 호출을 알아야 하므로,
# 여기 없는 parallel_safe 도구는 배치로 묶지 않고 순차 per-op 경로를 탄다
# (종전엔 수집은 parallel_safe 플래그만 보고 묶은 뒤 디스패치에서
# NotImplementedError 로 런이 죽는 크래시 트랩 — 리뷰 §4.1). 새 도구를
# 병렬 배치에 태우려면 엔진을 배선하고 이 집합에 추가한다.
_PARALLEL_BATCH_ENGINES = frozenset({"agent"})


class TurnDispatcher:
    """턴/op 디스패치 소유자 (C1 PR-3 승격 클러스터).

    wire 파싱 결과(ParsedTurn)를 받아 op 순회·가드(B1/A4/A5)·배치(병렬
    delegate / 같은-파일 edit)·관찰 조립·recovery 를 담당한다. 의존은
    명시 주입 5종: cfg/state/ctx/tools(ToolBridge)/recorder. 전유 상태
    ``loop_detector`` 는 이 객체가 소유. 도구 실행·관찰 캡은 전부
    ``self.tools`` 경유 — 브리지 뒤의 것을 직접 만지지 않는다.
    """

    def __init__(
        self, config: LoopConfig, state: LoopState, ctx, tools: ToolBridge, recorder
    ) -> None:
        self.cfg = config
        self.state = state
        self.ctx = ctx
        self.tools = tools
        self.recorder = recorder
        # B1 (action loop) detector. Threshold=2 fires on the second
        # consecutive identical (action, args).
        self.loop_detector = ActionLoopDetector(threshold=2)

    def _task_text(self) -> str:
        """All user requests this run (first query + injected), for recovery /
        review anchoring. Falls back to the raw query if the log is empty."""
        return (
            "\n".join(self.state.task_log) if self.state.task_log else self.state.query
        )

    def _intervene(
        self,
        llm_text: str,
        message: str,
        reason: str,
        outcome: dict,
        *,
        failure_signal: str | None = None,
        tool_name: str = "",
        primitives=None,
        recovery_kind: str = "",
        render: bool = False,
        store_emission: bool = True,
    ):
        """개입(회복 넛지) 공통 마무리 — 종전 5곳 복제 블록의 단일화.

        render_recovery → 관찰 append(render=False — 이미 표면화됨) → outcome
        기록 → 턴 미계수 → ``_CONTINUE``. **턴 계수 통일 규칙**: 개입 턴은
        도구를 실행하지 않은 회복 넛지이므로 max_turns 예산을 소모하지 않는다
        (종전엔 A4/A5 만 계수하고 A7/B1/NO_JSON 은 미계수하던 비일관 —
        리뷰 §4.1). 반복-개입 폭주는 B1 detector(동일 액션 반복 → level≥3
        하드페일)가 상한을 잡는다.

        ``failure_signal`` None 이면 outcome 의 기존 값을 유지한다
        (``_recover_unparsed`` — 초기 분류 NO_OUTPUT/NO_JSON/NO_ACTION 승계).
        ``recovery_kind`` "format" 은 fold 대상 마킹(B1 은 빈 값 유지 —
        액션 루프 넛지는 다음 파싱 성공으로 해소된 게 아니므로 접지 않는다).
        """
        # ``render=True`` (v9.21.0): 거부를 **실패한 관찰 카드**로 남긴다.
        # 기본(False)은 형식 거부용 — 웹의 recovery() 는 휘발 retry_tick 만
        # 내고 카드를 안 그린다(거부된 원문은 재시도 기계지 모델의 작업이
        # 아니다). 그런데 회신 독촉은 모델이 **일을 끝냈다고 주장한 것**을
        # 하네스가 물리는 것이라, 아무것도 안 그리면 사용자에겐 그 complete
        # 이 통과한 것처럼 보인다(실측 a209hq — ✅ 카드로 그려져 혼동).
        if not render:
            render_recovery(llm_text, message, reason, self.state.turn)
        # ``store_emission=False`` (v9.21.1): 물린 `complete` 은 저장하지 않는다
        # — 거부 관찰이 원문을 인용해 자기완결이다(사용자 결정). 남기면 저장
        # 형태(`ops:[complete]`)가 history 에서 final 로 읽히고, 재시도는
        # 기록하지 않는다는 원칙(v9.8.0 — 형식 거부는 카드가 아니다)과도
        # 어긋난다.
        _append_observation(
            self.state.messages,
            self.ctx,
            self.cfg.wire_format,
            llm_text,
            message,
            tool_name=tool_name,
            success=False,
            turn=self.state.turn,
            render=render,  # False: render_recovery already surfaced it
            recovery_kind=recovery_kind,
            store_emission=store_emission,
        )
        if failure_signal is not None:
            outcome["failure_signal"] = failure_signal
        if primitives is not None:
            outcome["primitives"] = list(primitives)
        self.state.turn -= 1  # 개입은 턴 미계수 (통일 규칙 — docstring 참조)
        return _CONTINUE

    def _handle_text_path(self, llm_text: str, usage=None):
        """Handle text parsing response (non-JSON fallback).

        ``usage`` is the turn's provider ``TokenUsage`` (or None) — passed
        straight through to the TurnRecord so per-turn cost lives next to
        the parse outcome in ``turns.jsonl``.

        Recovery primitives consume only the emitted text (``llm_text``)
        — the thinking channel is intentionally excluded from the
        recovery path (see ``docs/robust-harness/DESIGN.md`` §2.2).

        TurnRecord is emitted exactly once per call, regardless of which
        terminal branch is taken (success/retry/exception). Branches
        that fire an Intervention mutate ``outcome`` (failure_signal +
        primitives) before returning, and the trailing finally writes
        the record.
        """
        turn = self.cfg.wire_format.parse_turn(llm_text)
        # Phase 3 — foreign-format 구제 (multi-wire-format DESIGN §9): 바인딩
        # 포맷이 0-op 로 읽은 emission 을 타 등록 포맷 파서가 action-보유
        # ops 로 읽어내면 그 turn 으로 진행한다 (실측: 35B xml_fc 스트림의
        # json_fc 회귀 — 0-op 마찰의 17%, PHASE2.md §8). 라벨은 아래
        # 분류에서 FOREIGN_FORMAT, 직렬화는 corrected_record 로 바인딩
        # 포맷의 캐노니컬 shape 재렌더 (누출 raw 재공급 없음 — 자기 교정).
        foreign_source: str | None = None
        if not turn.ops:
            rescued = try_foreign_parse(self.cfg.wire_format, llm_text)
            if rescued is not None:
                turn, foreign_source = rescued
        # fold (v4.51.0): 이 emission 이 파싱 성공(ops 보유)이면 직전의
        # 형식-복구 개입은 소비 완료 — dynamic 캐시 뷰에서 [실패 prior,
        # 개입] 쌍을 접는다(성공 궤적만 유지). messages 는 다음 _call_llm
        # 이 get_messages() 로 재파생하므로 캐시만 접으면 자동 반영.
        if turn.ops and self.ctx is not None:
            self.ctx.fold_resolved_interventions(assume_tail_resolved=True)

        # Recover dropped action names (parse_stage 3) — the dropped-action
        # recovery SEAM: an op's action slot is empty but its action_input
        # survived (parse-preservation invariant), so ``infer_action`` tries to
        # resolve the tool from the input shape. A successful inference is
        # flagged so the observation step rewrites the prior + history to the
        # corrected shape (no raw-drift mimicry) and the TurnRecorder logs it.
        #
        # As of consolidation Step 3 every builtin tool is flat-native, so the
        # current prefix-based resolver returns None for builtin payloads (this
        # hook + infer_action stay live for a FUTURE prefixed tool/format; a
        # flat action-less payload like ``{path}`` is ambiguous → NO_ACTION,
        # the documented extension point for a future schema-based resolver).
        # Ambiguous/none leaves it to the NO_ACTION recovery below.
        #
        # Gated on ``action_required``: when the wire format requires an
        # explicit action (action_required=True), a dropped action is a
        # drift to be corrected by the model, so we skip inference and fall
        # through to the NO_ACTION recovery below. When False (the namespaced
        # format), the action is recoverable from the
        # preserved action_input, so we infer it.
        action_inferred = False
        if not self.cfg.wire_format.action_required:
            for op in turn.ops:
                if not op.action and isinstance(op.action_input, dict):
                    inferred = infer_action(op.action_input)
                    if inferred:
                        op.action = inferred
                        action_inferred = True

        # Classify outcome early; the dispatch body may mutate this
        # dict to reflect a B1 (action loop) detection that is only
        # known after we see the chosen action.
        # Degeneration is a GENERATION-level pathology — the stream ran away
        # repeating the wire shape instead of terminating — so it is logically
        # PRIOR to, and a more specific cause than, the parse-level symptom (a
        # runaway naturally fails to parse and would otherwise be mislabeled
        # NO_JSON). Checked FIRST. Recovery is driven by ``turn.parse_stage``
        # (in ``_recover_unparsed``), NOT by this label, so relabeling a
        # stage-0 runaway DEGENERATE changes only the telemetry signal
        # (turns.jsonl) — the recovery path is unchanged. Empty output is not a
        # runaway (``is_degenerate("")`` is False) so it still falls through to
        # NO_OUTPUT below.
        if self.cfg.wire_format.is_degenerate(llm_text):
            initial_signal = FAILURE_DEGENERATE
        elif foreign_source is not None:
            # 구제 성공 — 실행은 진행하되 라벨로 계수 (turns.jsonl 이 모델별
            # 누출 포맷 분포의 소스: 바인딩 재조정 근거).
            initial_signal = FAILURE_FOREIGN_FORMAT
        elif turn.parse_stage == 0:
            # Split A1 into two sub-modes — empty/whitespace-only output
            # vs non-empty content that drifted from JSON. The recovery
            # path is identical (RETRY_HINT_NO_JSON fallback in both),
            # but the labels separate two operationally different
            # failure shapes for analysis (DESIGN.md §1, A1a vs A1b).
            if not (llm_text or "").strip():
                initial_signal = FAILURE_NO_OUTPUT
            else:
                initial_signal = FAILURE_NO_JSON
        elif not any(op.action for op in turn.ops):
            initial_signal = FAILURE_NO_ACTION
        else:
            initial_signal = None
        outcome: dict = {
            "failure_signal": initial_signal,
            "primitives": ["action_inferred"] if action_inferred else [],
            "action_inferred": action_inferred,
        }
        if foreign_source is not None:
            outcome["primitives"].append(f"foreign_parse:{foreign_source}")
            # 구제 turn 의 직렬화 원본 — 바인딩 포맷의 serialize 가 raw 를
            # 재파싱하면 0-op(bare content)로 돌아가므로, 관찰 append 가
            # 이 레코드를 쓰게 한다 (ops shape = cross-format 계약이라
            # 바인딩 포맷의 render 가 캐노니컬로 재방출). inference 이후에
            # 조립해 복원된 action 이름까지 반영.
            outcome["corrected_record"] = {
                "role": "assistant",
                "thought": turn.thought or "",
                "ops": [
                    {"action": op.action, "action_input": op.action_input or {}}
                    for op in turn.ops
                ],
            }

        try:
            return self._dispatch_turn(llm_text, turn, outcome)
        finally:
            self.recorder.record(
                model=self.cfg.model,
                parse_stage=turn.parse_stage,
                failure_signal=outcome["failure_signal"],
                primitives_applied=outcome["primitives"],
                usage=usage,
            )

    def _dispatch_turn(self, llm_text: str, turn, outcome: dict):
        """Turn-level dispatch: guards, then the ops in array order.

        ``turn`` is a ``ParsedTurn``. Single-action formats produce 0 or 1
        ops (the default ``parse_turn`` wrapper), so for them this reproduces
        the pre-multi-op behaviour exactly. ``outcome`` is a mutable dict
        owned by the caller (``_handle_text_path``); branches that fire an
        Intervention update it before returning so the trailing finally
        records what happened.
        """
        # NOTE (v8.4.0): prose-only completion (v7.14.0 — accept an action-less
        # prose turn as an implicit `complete`) was REMOVED. Production found
        # the counterexample the 2026-07-23 bakeoff measured as zero: a
        # transitional narration ("Now let me write the plan document:") with
        # no action residue was accepted as a skill's final result — a silent
        # wrong completion consumed downstream. Completion is tool-input
        # SEMANTICS, and semantics are strict (the v8.0.0 line: only wire
        # SYNTAX is lenient) — so an action-less prose turn now always takes
        # the NO_ACTION nudge below, whose wording tells the model to re-emit
        # a prose answer through an explicit `complete` op.

        # 6. Thought
        if turn.thought:
            render_step("thought", turn.thought, self.state.turn)

        # No usable ops at all (parse failure / no action recovered, including
        # a thought-only turn) — straight to recovery. json_fc completes via
        # an explicit `complete` op, so a thought-only emission is a NO_ACTION
        # nudge, not a silent completion.
        if not turn.ops:
            return self._recover_unparsed(llm_text, turn, outcome)

        # Dispatch ops in array order (sequential — observations append in
        # order). Single-action formats have exactly ONE op and take the
        # legacy path (accumulate=None → _dispatch_op appends its own
        # observation and returns, byte-identical to pre-multi-op).
        #
        # N ops (multi-op formats): regular tool ops execute and ACCUMULATE
        # into one combined observation (run-all; any-fail ⇒ the combined
        # observation is marked failed so the model retries the failed op).
        # A turn-ending branch (complete / run_skill / guard
        # intervention / recovery) flushes whatever already ran first so
        # executed work isn't lost, then returns. ``ask`` is NOT turn-ending —
        # it produces an observation (the user's reply) and accumulates like a
        # normal tool, so several ``ask`` ops batch (each prompts in turn) just
        # like a read/shell batch.
        if len(turn.ops) == 1:
            return self._dispatch_op(llm_text, turn, turn.ops[0], outcome)

        results: list[dict] = []
        ops = turn.ops
        i = 0
        while i < len(ops):
            op = ops[i]
            tool = TOOLS.get(op.action) if op.action else None
            # Turn-ending actions (complete/run_skill): flush accumulated
            # results BEFORE the branch runs so its observation lands after
            # the work done so far (chronological order for the model).
            # ``Tool.terminal`` 속성 파생 (T3 선언화 — 종전 도구명 튜플).
            if tool is not None and tool.terminal:
                self._flush_op_results(
                    llm_text, results, corrected_record=outcome.get("corrected_record")
                )
                results = []
                return self._dispatch_op(llm_text, turn, op, outcome)
            # Parallel batch: a run of ≥2 consecutive ops of the SAME
            # parallel_safe tool dispatches concurrently into one combined
            # observation (delegate: independent subagents). A lone
            # parallel_safe op falls through to the normal per-op path so it
            # keeps its B1/A4/A5 guards. Mutating tools (parallel_safe=False)
            # always take the sequential per-op path — order is their
            # correctness guarantee (write→edit same file, mkdir→touch).
            # Same-file edit batch: a run of ≥2 consecutive edit_file ops on the
            # SAME path is applied together against ONE original read (all refs
            # resolved before any write, bottom-up, all-or-nothing) so a later
            # op's hashline ref doesn't go stale from an earlier op's line shift.
            # A lone edit_file, or edits on different paths, take the normal
            # per-op path. Only consecutive same-path edits group — interleaving
            # another tool (e.g. write→edit) breaks the run, preserving order.
            if op.action == "edit_file" and isinstance(op.action_input, dict):
                path = op.action_input.get("path")
                j = i
                while (
                    j < len(ops)
                    and ops[j].action == "edit_file"
                    and isinstance(ops[j].action_input, dict)
                    and ops[j].action_input.get("path") == path
                ):
                    j += 1
                if j - i > 1:
                    self._dispatch_edit_batch(
                        llm_text, turn, ops[i:j], outcome, accumulate=results
                    )
                    i = j
                    continue
            if (
                tool is not None
                and tool.parallel_safe
                and op.action in _PARALLEL_BATCH_ENGINES
                and tool.parallel_batchable(op.action_input or {})
            ):
                # mode-aware 수집 (5.0.0): 같은 도구라도 배치 가능한 op
                # (agent 는 mode:"run")만 묶는다 — 상주 모드가 섞이면 거기서
                # 끊고 순차로.
                j = i
                while (
                    j < len(ops)
                    and ops[j].action == op.action
                    and tool.parallel_batchable(ops[j].action_input or {})
                ):
                    j += 1
                if j - i > 1:
                    self._dispatch_parallel_batch(
                        llm_text, turn, ops[i:j], outcome, accumulate=results
                    )
                    i = j
                    continue
            r = self._dispatch_op(llm_text, turn, op, outcome, accumulate=results)
            if r is not None:
                # Guard/recovery fired inside the op (B1/no-action …):
                # its intervention observation is already appended; flush the
                # accumulated work after it (rare mid-array edge — order is
                # intervention-first, results still preserved).
                #
                # **뒤에 남은 op 는 실행되지 않는다 — 그걸 말해 준다** (v9.21.0).
                # 침묵하면 모델은 배치 전체가 돌았다고 믿는다(실측 kdsd0j —
                # 안 나간 `message` 를 "asked player-1" 로 기억하고 complete).
                skipped = ops[i + 1 :]
                if skipped:
                    names = ", ".join(
                        f"[{i + 2 + k}/{len(ops)}] {o.action or '?'}"
                        for k, o in enumerate(skipped)
                    )
                    self.tools.accumulate_raw(
                        results,
                        "batch",
                        f"NOT executed — aborted after op {i + 1} failed: {names}. "
                        "Re-send these; do NOT re-send the ops that already ran.",
                        False,
                    )
                self._flush_op_results(
                    llm_text, results, corrected_record=outcome.get("corrected_record")
                )
                return r
            i += 1
        self._flush_op_results(
            llm_text, results, corrected_record=outcome.get("corrected_record")
        )
        return _CONTINUE

    def _flush_op_results(
        self,
        llm_text: str,
        results: list[dict],
        *,
        corrected_record: dict | None = None,
    ) -> None:
        """Append ONE combined observation for accumulated op results.

        Per-op header lines (``[i/N] tool — OK/FAILED``) frame each op's
        output; turn success = all ops succeeded (any-fail ⇒ failed so the
        model retries the failed op next turn). No-op when nothing ran.
        ``corrected_record`` (foreign-format 구제 턴) — raw 재파싱 대신 이
        레코드로 직렬화해 prior 가 바인딩 포맷 캐노니컬로 재렌더되게.
        """
        if not results:
            return
        n = len(results)
        parts = []
        for i, r in enumerate(results, start=1):
            status = "OK" if r["success"] else "FAILED"
            parts.append(f"[{i}/{n}] {r['tool_name']} — {status}\n{r['observation']}")
        combined = "\n\n".join(parts)
        all_ok = all(r["success"] for r in results)
        _append_observation(
            self.state.messages,
            self.ctx,
            self.cfg.wire_format,
            llm_text,
            f"Observation: {combined}",
            tool_name=_combined_tool_label([r["tool_name"] for r in results]),
            success=all_ok,
            turn=self.state.turn,
            corrected_record=corrected_record,
        )

    def _dispatch_parallel_batch(
        self, llm_text, turn, batch_ops, outcome, *, accumulate
    ):
        """Dispatch a run of ≥2 consecutive parallel_safe ops concurrently,
        appending ONE combined result to *accumulate*.

        Only ``delegate`` is wired today (the sole ``parallel_safe`` tool): each
        op's flat input becomes one task spec → ``tool_delegate({tasks:[...]})``
        → ``_run_parallel`` (real threading). This is what makes the prompt's
        "several delegate ops in one turn run in parallel" actually true (the
        N-op loop is otherwise sequential).

        A future read-only ``parallel_safe`` tool with no internal concurrent
        engine would fan its ops over a thread-pool of per-op ``run()`` calls
        in the extension slot below — not wired (no other tool opts in).
        """
        tool_name = batch_ops[0].action
        # Render one action card per op (the model's flat emission, pre-wrap),
        # matching the single-op render so the UI shows every delegate op.
        for op in batch_ops:
            disp = op.action_input if op.action_input is not None else {}
            render_step(
                "action",
                "",
                self.state.turn,
                tool_name=tool_name,
                tool_input=json.dumps(disp, ensure_ascii=False)
                if isinstance(disp, dict)
                else str(disp),
            )

        if tool_name == "agent":
            # 5.0.0: agent run fan-out — 각 run op 을 일회성 task 스펙으로
            # 조립해 병렬 엔진으로 (수집 단계가 mode:"run" 만 묶었음을 전제).
            from agent_cli.loop.tool_bridge import ToolBridge

            specs = [
                ToolBridge._run_spec(TOOLS["agent"].strip_prefix(op.action_input or {}))
                for op in batch_ops
            ]
        else:
            # Unreachable in practice: 수집 단계가 ``_PARALLEL_BATCH_ENGINES``
            # 로 게이트하므로 엔진 미배선 도구는 여기 오기 전에 순차 per-op
            # 경로로 빠진다. 방어적 불변식 단언으로만 유지 — a future
            # parallel_safe tool with no internal concurrent engine would fan
            # its ops out over a thread-pool of per-op run() calls here, then
            # register itself in _PARALLEL_BATCH_ENGINES.
            raise NotImplementedError(
                f"parallel_safe batch dispatch not wired for {tool_name!r}; "
                "register an engine in _PARALLEL_BATCH_ENGINES after wiring."
            )

        result = self.tools._dispatch_tool_with_hooks(tool_name, {"tasks": specs})
        # Rendered from storage by _flush_op_results' _append_observation
        # (combined card), matching ctx + resume — no separate pre-render.
        self.tools.accumulate_observation(
            accumulate, tool_name, result, {"tasks": specs}
        )

    def _dispatch_edit_batch(self, llm_text, turn, batch_ops, outcome, *, accumulate):
        """Apply a run of ≥2 consecutive same-path edit_file ops as ONE batch,
        appending ONE combined result to *accumulate*.

        Goes through ``ToolBridge.dispatch_edit_batch`` (P0-2) so the batch
        carries the SAME hook/history contract as the single-op path
        (PreToolUse per edit — any block aborts the whole batch, PostToolUse
        once, ``recent_tool_history`` per edit), while keeping the batch
        semantics (one ``apply_edits_batch`` apply → one write, all-or-nothing,
        ONE combined observation). Each op's flat input is still rendered as
        its own action card, matching the single-op render.
        """
        for op in batch_ops:
            disp = op.action_input if isinstance(op.action_input, dict) else {}
            render_step(
                "action",
                "",
                self.state.turn,
                tool_name="edit_file",
                tool_input=json.dumps(disp, ensure_ascii=False),
            )

        path = batch_ops[0].action_input.get("path")
        edits = [op.action_input for op in batch_ops]
        result = self.tools.dispatch_edit_batch(path, edits)
        self.tools.accumulate_observation(
            accumulate, "edit_file", result, batch_ops[0].action_input
        )

    def _dispatch_op(self, llm_text: str, turn, op, outcome: dict, accumulate=None):
        """Dispatch ONE op of a turn. Returns a ToolResult or a sentinel.

        Carries the pre-multi-op per-action body unchanged: special actions
        (complete / ask / run_skill), then B1/A4/A5 guards
        and tool execution, then the no-action fall-through recovery.

        ``accumulate`` (multi-op N-op path only): a list to collect this op's
        execution record into instead of appending its own observation —
        the caller combines all records into one observation. Returns
        ``None`` in that case ("executed, keep going"); every other branch
        returns a ToolResult/sentinel as before.
        """
        # 7. Complete tool (text parsing path)
        _debug_log(f"PARSED iter={self.state.turn} action={op.action}")
        if op.action == "complete":
            return self._op_complete(llm_text, turn, op, outcome)

        # 9. Detect echo-as-final-answer (common small model pattern)
        echo_answer = _try_echo_as_final(op.action, op.action_input)
        if echo_answer:
            if self.ctx:
                self.ctx.add(
                    self.cfg.wire_format.serialize_terminal_for_history(
                        turn.thought or "", echo_answer
                    )
                )
            render_step("final", echo_answer, self.state.turn)

            return ToolResult(True, output=echo_answer)

        if op.action == "ask":
            handled = self._op_ask(llm_text, turn, op, accumulate)
            if handled is not _NOT_HANDLED:
                return handled

        if op.action == "run_skill":
            return self._op_run_skill(llm_text, turn, op)

        if op.action == "message":
            handled = self._op_message(llm_text, turn, op, accumulate)
            if handled is not _NOT_HANDLED:
                return handled

        if op.action == "answer":
            handled = self._op_answer(llm_text, turn, op, accumulate)
            if handled is not _NOT_HANDLED:
                return handled

        if op.action == "reply":
            handled = self._op_reply(llm_text, turn, op, accumulate)
            if handled is not _NOT_HANDLED:
                return handled

        if op.action:
            return self._op_execute_tool(llm_text, turn, op, outcome, accumulate)

        return self._recover_unparsed(llm_text, turn, outcome)

    def _op_complete(self, llm_text: str, turn, op, outcome: dict):
        """terminal ``complete`` op — 최종 답 언랩(A6)·history 기록·final 렌더."""
        if isinstance(op.action_input, dict):
            raw = op.action_input.get("result")
            answer = (
                str(raw)
                if raw
                else "(Completed without result — model may lack capability for this task)"
            )
        elif isinstance(op.action_input, str):
            raw = op.action_input
            answer = (
                op.action_input
                or "(Completed without result — model may lack capability for this task)"
            )
        else:
            raw = None
            answer = (
                str(op.action_input)
                if op.action_input
                else "(Completed without result — model may lack capability for this task)"
            )

        # A6 (Nested envelope) — detection records the signal for
        # observability AND we unwrap one level so the user-facing
        # answer doesn't carry a literal ``{"result": "..."}`` prefix.
        # Single-level only (recursive nesting indicates a different
        # bug worth surfacing). ``raw`` may be from ``op.action_
        # input`` (dict path) or the input itself (str path); both
        # surface as the same artifact, so we re-derive ``answer``
        # from the unwrapped value.
        if detect_nested_envelope(raw):
            outcome["failure_signal"] = FAILURE_NESTED_ENVELOPE
            unwrapped = unwrap_nested_envelope(raw)
            if unwrapped != raw:
                answer = unwrapped or answer

        bounced = self._require_answers(llm_text, op, answer, outcome)
        if bounced is not None:
            return bounced

        claimed = _claimed_ids(op.action_input)
        # **지우기 전에** 뽑는다 — `_settle_requests` 가 주장분을 회계에서
        # 제거하므로, 그 뒤엔 "이 답이 무엇에 대한 답인지" 를 알 수 없다.
        answered = (
            [r for r in (self.state.run_requests or []) if str(r.get("id")) in claimed]
            if claimed
            else []
        )
        still_open = self._settle_requests(claimed)
        nagging = self._should_nag(claimed, still_open)
        if not nagging:
            answer = self._with_unanswered_notice(claimed, still_open, answer)

        # ── message/ask 빚 (v9.22.0, 유형별 통일) ──
        # 사용자 요청은 위에서 정산했다(수락·키 제거·남은 요청 독촉). 그 뒤에
        # message/ask 빚이 남아 있으면 — 갈림은 **이 런이 사용자 요청을
        # 받았는가**(`state.user_run`)이지 main/상주가 아니다(v9.22.1):
        #   사용자 요청 없는 런(에이전트 항목·🤝 웨이크): complete 을 **거부**
        #         — 산출물이 아무 데도 안 가므로. 원문을 인용한 관찰만 남고
        #         emission 은 저장하지 않는다.
        #   사용자 요청 있는 런: complete 을 **수락**(결과는 사용자에게 간다)
        #         — 배달·기록한 뒤 빚을 독촉하고 루프를 잇는다.
        # 둘 다 3회까지; 그 뒤엔 런 끝에서 폴백(요약 배달)·닫기(질문).
        debts = [] if nagging else self._debts()
        if debts and self.state.debt_nags < MAX_DEBT_NAGS:
            self.state.debt_nags += 1
            if not self.state.user_run:
                return self._refuse_for_debts(llm_text, answer, debts, outcome)
            render_step("final", answer, self.state.turn, requests=answered)
            return self._nag_debts(llm_text, debts, outcome)

        # 결과는 **먼저** 나간다 — 독촉하든 안 하든 (`_nag_open_requests` 참조).
        render_step("final", answer, self.state.turn, requests=answered)
        if nagging:
            # 기록은 여기서 하지 않는다 — `_intervene` 의 `_append_observation`
            # 이 이 턴의 assistant 레코드(원문 직렬화, `answers` 포함)를 관찰과
            # 함께 저장한다. 둘 다 넣으면 history 에 같은 final 이 두 번 들어가고
            # 웹 타임라인에도 같은 카드가 두 장 뜬다(라이브 실측).
            return self._nag_open_requests(llm_text, still_open, outcome)

        if self.ctx:
            self.ctx.add(
                self.cfg.wire_format.serialize_terminal_for_history(
                    turn.thought or "",
                    answer,
                    answers=None if claimed is None else sorted(claimed),
                )
            )
        return ToolResult(True, output=answer)

    def _require_answers(self, llm_text: str, op, answer: str, outcome: dict):
        """요청이 합쳐진 런에서 `answers` 없이 완료하려 하면 **한 번 되돌린다**.

        꼬리(per-turn tail)가 매 턴 요구하는데도 모델이 생략한다 — 라이브
        실측(xrnway)에서 모델은 꼬리를 읽고 id 까지 알면서("실제 미완 요청은
        [2],[3]뿐") 필드를 안 채웠다. 안내만으로는 안 되므로 형식 교정으로
        되돌린다(`format_no_action_retry` 와 같은 자리).

        **`complete` 을 붙잡는 것과 다르다.** 붙잡기란 "다른 요청이 풀릴
        때까지 결과를 못 내보내는 것" 이고 그건 안 한다(agent-ask 3판에서
        틀렸던 자리). 이건 같은 턴의 산출물을 **형식만 고쳐 다시 내라**는
        것이라 기다리는 상대가 없다.

        되묻기는 **런당 한 번**. 끝내 생략하면 받아주고 "미신고" 로 적는다 —
        무한 되묻기는 런을 태운다.
        """
        pending = getattr(self.state, "run_requests", None)
        if not pending:
            return None
        if getattr(self.state, "answers_prompted", False):
            return None
        if _claimed_ids(op.action_input):
            return None
        self.state.answers_prompted = True
        ids = ", ".join(f'"{r.get("id")}"' for r in pending)
        # 원문을 **인용**한다 — emission 은 저장하지 않으므로(v9.21.1) 모델이
        # 재발행할 결과가 이 관찰 안에 있어야 한다. 성공하면 형식 개입 fold 가
        # 이 관찰을 컨텍스트에서 접는다(재시도는 기록하지 않는다).
        return self._intervene(
            llm_text,
            (
                f"Observation: your `complete` was refused — {len(pending)} user "
                f"request{'s are' if len(pending) != 1 else ' is'} open in this run "
                "and `answers` is required, so the harness cannot tell which ones "
                "your result covers.\n"
                f"{self._request_lines(pending)}\n"
                f"You completed with:\n«{answer}»\n"
                f"Re-emit `complete` with that result plus `answers: [{ids}]`, "
                "dropping any id you did not actually answer."
            ),
            "answers required",
            outcome,
            recovery_kind="format",
            store_emission=False,
        )

    def _settle_requests(self, claimed):
        """주장된 요청을 회계에서 **지우고**, 아직 열린 것을 돌려준다.

        종전엔 아무것도 지우지 않았다 — 꼬리는 런 내내 같은 목록을 보였고,
        `complete` 은 무조건 종결이라 남은 요청은 각주 한 줄로 알려지고 런과
        함께 사라졌다(런 수명 `LoopState.run_requests`, 이월 없음). 이제
        주장 = 닫힘이다. 남은 것이 곧 outstanding 이고, 그게 있으면 루프는
        끝나지 않는다.

        ``claimed is None`` (필드 생략)은 **모름**이다 — 무엇이 닫혔는지 알
        수 없으니 지울 수도, 독촉할 수도 없다. 그 런의 회계는 여기서 닫고
        (되돌림이 이미 한 턴을 썼다) 합쳐진 런이면 "미신고" 로 적는다.
        """
        pending = getattr(self.state, "run_requests", None)
        if not pending:
            return []
        if claimed is None:
            # 생략은 **모름**이다 — 1건이어도 같다(v9.22.0). "결과가 곧 그 답"
            # 은 사용자 요청과 에이전트 질문이 한 런에 섞이면 틀리는 추측이다
            # (에이전트 질문에 답하려던 complete 을 사용자 답으로 배달한다).
            # 되돌림이 이미 한 번 요구했으니 여기서 회계를 닫고 미신고로 적는다.
            open_now = list(pending)
            pending.clear()
            return open_now
        remaining = [r for r in pending if str(r.get("id")) not in claimed]
        pending[:] = remaining
        return list(remaining)

    def _should_nag(self, claimed, still_open) -> bool:
        """미답이 남았는데 런을 끝내려 하는가.

        한 요청당 **한 번**만 독촉한다. 무한 독촉은 고집 센 모델과 물려 런을
        태우고, `max_turns` 가 잡기 전에 토큰을 먼저 태운다. 생략
        (``claimed is None``)에는 걸지 않는다 — 무엇이 남았는지 모르는
        상태에서 "남은 걸 해라"는 말은 근거가 없다.
        """
        if claimed is None or not still_open:
            return False
        nagged = self.state.requests_nagged
        return any(str(r.get("id")) not in nagged for r in still_open)

    def _nag_open_requests(self, llm_text: str, still_open, outcome: dict):
        """최종답은 내보내고 **루프는 계속** 돌린다.

        `complete` 을 **붙잡는 것과 다르다**. 붙잡기란 다른 요청이 풀릴 때까지
        결과를 못 내보내는 것이고, 그건 일을 시킨 쪽을 자기와 무관한 요청의
        인질로 만든다(설계 3판에서 한 번 틀렸던 자리). 여기서는 답이 **이미
        렌더되고 history 에 들어간 뒤**라 기다리는 사람이 없다 — 다만 아직
        아무도 답하지 않은 요청이 남아 있으니 런을 닫지 않을 뿐이다.

        종전엔 각주 한 줄(⏳)을 붙이고 끝냈다. 그 요청은 다음 런으로도 안
        넘어간다(`run_requests` 는 런 수명) — 다음 런이 온다는 보장도 없어,
        세션이 그대로 유휴로 들어가면 아무 데도 안 남았다.
        """
        self.state.requests_nagged.update(str(r.get("id")) for r in still_open)
        ids = ", ".join(f'"{r.get("id")}"' for r in still_open)
        return self._intervene(
            llm_text,
            (
                f"Observation: your result was delivered, but {len(still_open)} "
                "user request(s) in this run are still unanswered — you did not "
                "list them in `answers`.\n"
                f"{self._request_lines(still_open)}\n"
                "Keep working and address them now. When you are done, call "
                f"`complete` again with `answers: [{ids}]` covering what you "
                "answered this time."
            ),
            "open requests remain",
            outcome,
            tool_name="complete",
        )

    def _debts(self) -> list[dict]:
        """이 런이 아직 갚지 않은 message/ask 빚 — 포트가 없으면 빈 목록."""
        port = self.cfg.questions
        fn = getattr(port, "debts", None) if port is not None else None
        try:
            return list(fn()) if callable(fn) else []
        except Exception:
            return []

    def _debt_lines(self, debts: list[dict]) -> str:
        """빚 목록을 갚는 수단과 함께. 수단은 **이 루프에 있는 도구**로 고른다
        — `reply` 는 상주 전용(`port.nonblocking`), main 은 `agent request`.
        규칙(거부/독촉)과는 별개다: 사람 창 런의 상주는 독촉을 받되 `reply`
        를 안내받는다."""
        resident = bool(getattr(self.cfg.questions, "nonblocking", False))
        lines = []
        for d in debts:
            if d["kind"] == "answer":
                lines.append(
                    f'  - answer question {d["id"]} from {d["to"]}: "{d["text"][:120]}" '
                    f'→ answer(id="{d["id"]}", text="...")'
                )
            else:
                key = d["to"].split(":", 1)[-1]
                how = (
                    'reply(text="...")'
                    if resident
                    else f'agent(mode="request", key="{key}", task="...")'
                )
                lines.append(f'  - reply to {d["to"]}: "{d["text"][:120]}" → {how}')
        return "\n".join(lines)

    def _debt_tail(self) -> str:
        left = MAX_DEBT_NAGS - self.state.debt_nags
        if left > 0:
            return (
                f"({left} more reminder{'s' if left != 1 else ''} before the harness "
                "settles these for you: unreplied messages get your run summary, "
                "unanswered questions are closed.)"
            )
        return (
            "(Last reminder — if you complete again without settling these, the "
            "harness sends your run summary for unreplied messages and closes "
            "unanswered questions.)"
        )

    def _refuse_for_debts(
        self, llm_text: str, answer: str, debts: list[dict], outcome: dict
    ):
        """사용자 요청 없는 런: 빚을 남긴 `complete` 을 거부한다 — 원문을
        인용한 관찰 하나, emission 은 저장하지 않는다(v9.21.1 원칙)."""
        return self._intervene(
            llm_text,
            (
                "Observation: your `complete` was refused — it reports to no one, "
                "and you still owe:\n"
                f"{self._debt_lines(debts)}\n"
                f"You completed with:\n«{answer}»\n"
                "If that text was your reply, send it with the tool shown; then "
                "`complete` if nothing else remains. " + self._debt_tail()
            ),
            "debts owed",
            outcome,
            tool_name="complete",
            render=True,
            store_emission=False,
        )

    def _nag_debts(self, llm_text: str, debts: list[dict], outcome: dict):
        """사용자 요청 있는 런: `complete` 은 수락됐다(결과는 사용자에게
        갔다) — 남은 빚만 독촉하고 루프를 잇는다."""
        return self._intervene(
            llm_text,
            (
                "Observation: your result was delivered, but you still owe:\n"
                f"{self._debt_lines(debts)}\n"
                "Settle them now, then `complete` again. " + self._debt_tail()
            ),
            "debts owed",
            outcome,
            tool_name="complete",
        )

    def _with_unanswered_notice(self, claimed, still_open, answer: str) -> str:
        """아무도 답을 주장하지 않은 요청을 최종답 말미에 덧붙인다.

        독촉이 끝난 자리의 **최후 통지**다 — 요청마다 한 번씩 독촉하고도
        남았거나(고집), 생략이라 독촉할 근거가 없는 경우.

        회계일 뿐 강제가 아니다. `complete` 을 붙잡지 않는다 — 결과는 그대로
        나가고, 빠진 것이 보이게만 한다.

        **모델의 주장이 정직한지는 검증할 수 없다.** 답했다고 주장하면 믿는다
        — `ask` 도 같고, 거기서도 asker 가 읽고 판단한다. 검출되는 것은
        "아무도 주장하지 않은 요청" 뿐이고, 그것만으로 충분히 값이 있다.
        """
        if not still_open:
            return answer

        if claimed is None:
            # **생략은 "전부 답함" 도 "전부 미답" 도 아니다 — 모르는 것이다.**
            #
            # 라이브(xrnway)에서 모델은 꼬리가 매 턴 요구하는데도 `answers`
            # 를 안 실었고, 실제로는 **둘 다 답했다**. 그때 "미답" 이라고
            # 쓰면 하네스가 거짓을 단언한다 — 종전의 조용한 관용보다 나쁘다.
            # 아는 것만 적는다: "어느 것에 답했는지 밝히지 않았다".
            #
            return (
                f"{answer}\n\n📋 This run merged {len(still_open)} requests, but "
                "`complete` came without `answers` so which ones were addressed "
                f"is undeclared. Check each:\n{self._request_lines(still_open)}"
            )

        # 여기서부터는 모델이 **직접 밝힌** 것이다. 그러니 건수와 무관하게
        # 그대로 전한다 — 단건 런에서 `answers: []` 를 보냈다면 "이 요청은
        # 답하지 않았다" 는 모델 자신의 진술이고, 삼키면 안 된다.
        return (
            f"{answer}\n\n⏳ {len(still_open)} request(s) in this run were not "
            f"answered:\n{self._request_lines(still_open)}"
        )

    @staticmethod
    def _request_lines(requests) -> str:
        return "\n".join(
            f"   [{r.get('id')}]"
            + (f" ({r['author']})" if r.get("author") else "")
            + f' "{(r.get("text") or "").strip()[:120]}"'
            for r in requests
        )

    def _op_ask(self, llm_text: str, turn, op, accumulate):
        """``ask`` op — 질문 추출·사용자 응답을 관찰로. 질문이 없으면
        ``_NOT_HANDLED`` 를 반환해 일반 도구 실행 경로로 폴스루(기존 제어
        흐름 보존 — None 은 N-op 누적 의미라 센티널 분리)."""
        questions = _extract_questions(op.action_input)
        if questions:
            # Emit the action step so out-of-band renderers (web)
            # replace their streaming card with a structured
            # ``assistant_turn``. Without this, the raw-JSON
            # streaming card stays on screen and the next turn's
            # stream chunks visually append to it — the user sees
            # consecutive assistant emissions glued together.
            render_step(
                "action",
                "",
                self.state.turn,
                tool_name="ask",
                tool_input=json.dumps(op.action_input, ensure_ascii=False)
                if isinstance(op.action_input, dict)
                else str(op.action_input),
            )
            port = self.cfg.questions
            if port is not None and port.nonblocking:
                to = (
                    op.action_input.get("to")
                    if isinstance(op.action_input, dict)
                    else None
                )
                return self._op_ask_async(llm_text, questions, port, accumulate, to=to)
            # 비동기 포트가 없으면(main/delegate) 사람에게 직접 묻는다.
            # 종전엔 여기 ``cfg.ask_handler`` 분기가 하나 더 있었는데 실값
            # 생산자가 **하나도 없었다** — 상주 에이전트의 ask 는 v9.12 에서
            # 질문 포트로 옮겨갔고, 그때 남은 죽은 가지였다.
            user_response = _handle_ask(questions)
            # ``ask`` is a normal observation-producing op (the user's
            # reply is the observation), not a terminal. In a multi-op turn
            # it accumulates like read/shell so consecutive asks batch into
            # the one combined observation; alone it appends its own.
            if accumulate is not None:
                self.tools.accumulate_raw(
                    accumulate, "ask", f"User responded:\n{user_response}", True
                )
                return None
            obs_msg = f"Observation: User responded:\n{user_response}"
            _append_observation(
                self.state.messages,
                self.ctx,
                self.cfg.wire_format,
                llm_text,
                obs_msg,
                tool_name="ask",
                success=True,
                turn=self.state.turn,
                render=False,  # the answer is surfaced by the input UI
            )
            return _CONTINUE

        return _NOT_HANDLED

    def _op_message(self, llm_text: str, turn, op, accumulate):
        """``message`` op (v5.11) — 상주 에이전트끼리의 비동기 메시징.

        ``message_handler`` 로 라우팅하고 즉시 관찰(배달 확인)을 돌려준다 —
        회신은 나중에 새 메시지로 도착한다(발신자 비블록). 핸들러가 없으면
        (main/일회성 루프) ``_NOT_HANDLED`` 로 폴스루 — 일반 도구 경로가
        "message intercepted by loop" 플레이스홀더를 실행해 무해하게 끝난다.
        """
        handler = self.cfg.message_handler
        if handler is None:
            return _NOT_HANDLED
        args = op.action_input if isinstance(op.action_input, dict) else {}
        to = str(args.get("to", "")).strip()
        text = str(args.get("text", "")).strip()
        render_step(
            "action",
            "",
            self.state.turn,
            tool_name="message",
            tool_input=json.dumps(args, ensure_ascii=False),
        )
        try:
            result = handler(to, text)
        except Exception as e:
            result = f"message failed: {type(e).__name__}: {e}"
        obs = f"[message → {to or '?'}] {result}"
        if accumulate is not None:
            self.tools.accumulate_raw(accumulate, "message", obs, True)
            return None
        _append_observation(
            self.state.messages,
            self.ctx,
            self.cfg.wire_format,
            llm_text,
            f"Observation: {obs}",
            tool_name="message",
            success=True,
            turn=self.state.turn,
        )
        return _CONTINUE

    def _op_ask_async(self, llm_text: str, questions, port, accumulate, *, to=None):
        """상주 에이전트의 ``ask`` — **막지 않는다** (DESIGN.md §3.1).

        질문을 등록하고 즉시 관찰을 돌려준다. 답은 나중에 새 메시지(= 새
        런)로 오고, 그때 이 에이전트의 ctx 는 그대로라 하던 일을 잇는다.

        ``to`` (v9.20.0): ``"user"`` 면 원 요청자 대신 **사람**에게 — ❓
        트레이에 뜬다. 종전엔 주소가 원 요청자로 고정이라 main 이 시킨
        일에서 사람에게 물을 방법이 없었고, 그 질문을 받은 main 이 사람
        대신 답을 지어냈다(실측). 값 검증은 포트가 한다 — 모르는 값은
        보내지 않고 관찰로 되돌린다(조용한 폴백 금지).
        """
        lines = []
        for q in questions:
            qid, err = port.ask(q, to=to)
            if err:
                lines.append(f"could not ask: {err}")
            else:
                lines.append(f'question {qid} sent — "{q}"')
        obs = (
            "\n".join(lines)
            + "\nYou are NOT blocked. Continue with whatever does not depend "
            "on the answer; it will arrive as a new message. If nothing else "
            "can proceed, `complete` — you will be resumed when it arrives."
        )
        if accumulate is not None:
            self.tools.accumulate_raw(accumulate, "ask", obs, True)
            return None
        _append_observation(
            self.state.messages,
            self.ctx,
            self.cfg.wire_format,
            llm_text,
            f"Observation: {obs}",
            tool_name="ask",
            success=True,
            turn=self.state.turn,
        )
        return _CONTINUE

    def _op_reply(self, llm_text: str, turn, op, accumulate):
        """``reply`` op (v9.21.0) — 이 런을 시킨 쪽에게 빚진 회신을 갚는다.

        ``_op_answer`` 와 동형. 포트가 없거나 상주가 아니면 ``_NOT_HANDLED``.
        """
        port = self.cfg.questions
        if port is None or not getattr(port, "nonblocking", False):
            return _NOT_HANDLED
        args = op.action_input if isinstance(op.action_input, dict) else {}
        text = str(args.get("text", "")).strip()
        render_step(
            "action",
            "",
            self.state.turn,
            tool_name="reply",
            tool_input=json.dumps(args, ensure_ascii=False),
        )
        try:
            err = port.reply(text)
        except Exception as e:
            err = f"reply failed: {type(e).__name__}: {e}"
        ok = not err
        obs = "[reply] " + (err or "delivered to whoever requested this work")
        if accumulate is not None:
            self.tools.accumulate_raw(accumulate, "reply", obs, ok)
            return None
        _append_observation(
            self.state.messages,
            self.ctx,
            self.cfg.wire_format,
            llm_text,
            f"Observation: {obs}",
            tool_name="reply",
            success=ok,
            turn=self.state.turn,
        )
        return _CONTINUE

    def _op_answer(self, llm_text: str, turn, op, accumulate):
        """``answer`` op — 열린 질문에 id 로 짝지어 답한다 (§3.1).

        ``_op_message`` 와 동형: 포트로 라우팅하고 즉시 관찰을 돌려준다.
        포트가 없으면(일회성 루프) ``_NOT_HANDLED`` 로 폴스루 — 일반 도구
        경로의 플레이스홀더가 무해하게 끝낸다.
        """
        port = self.cfg.questions
        if port is None:
            return _NOT_HANDLED
        args = op.action_input if isinstance(op.action_input, dict) else {}
        qid = str(args.get("id", "")).strip()
        text = str(args.get("text", "")).strip()
        render_step(
            "action",
            "",
            self.state.turn,
            tool_name="answer",
            tool_input=json.dumps(args, ensure_ascii=False),
        )
        try:
            err = port.answer(qid, text)
        except Exception as e:
            err = f"answer failed: {type(e).__name__}: {e}"
        ok = not err
        obs = f"[answer → {qid or '?'}] " + (err or "delivered to the asker")
        if accumulate is not None:
            self.tools.accumulate_raw(accumulate, "answer", obs, ok)
            return None
        _append_observation(
            self.state.messages,
            self.ctx,
            self.cfg.wire_format,
            llm_text,
            f"Observation: {obs}",
            tool_name="answer",
            success=ok,
            turn=self.state.turn,
        )
        return _CONTINUE

    def _op_run_skill(self, llm_text: str, turn, op):
        """``run_skill`` op — 루프 레벨 인터셉트(깊이/사이클 가드는
        _handle_run_skill 내부)."""
        skill_input = op.action_input if isinstance(op.action_input, dict) else {}
        # Same reason as ``ask`` above — close out the streaming
        # card before the (often long-running) skill subprocess
        # starts emitting its own events.
        render_step(
            "action",
            "",
            self.state.turn,
            tool_name="run_skill",
            tool_input=json.dumps(skill_input, ensure_ascii=False),
        )
        skill_tool_result = _handle_run_skill(
            skill_input,
            self.cfg.provider_name,
            self.cfg.base_url,
            self.cfg.api_key,
            self.cfg.capabilities,
            self.cfg.model,
            self.ctx,
            self.cfg.session,
            self.cfg.skill_name,
            skill_stack=self.cfg.skill_stack,
            graceful_interrupt=self.cfg.graceful_interrupt,
            stop_event=self.state.stop_event,
            hook_runner=self.cfg.hook_runner,
            mcp_manager=self.cfg.mcp_manager,
            parent_hooks_config=self.cfg.hooks_config,
            parent_depth=self.cfg.depth,
            max_depth=self.cfg.max_depth,
            compaction_enabled=self.cfg.compaction_enabled,
            # A skill inherits the running loop's registry so it can spawn/manage
            # workers (an orchestrate skill's whole point); a sub-agent loop has
            # no registry, so its skills stay run-only. See executor.execute_skill.
            agent_registry=self.cfg.agent_registry,
            owner=self.cfg.owner,
        )
        # Through the same result→observation seam as every other tool: a skill
        # returns its sub-loop's whole output, so it is the single largest
        # observation the loop can produce — it used to be the ONE path that
        # skipped the cap entirely and went into context unbounded.
        obs = self.tools._tool_observation("run_skill", skill_tool_result, skill_input)
        obs_msg = f"Observation: {obs}"
        _append_observation(
            self.state.messages,
            self.ctx,
            self.cfg.wire_format,
            llm_text,
            obs_msg,
            tool_name="run_skill",
            success=skill_tool_result.success,
            artifact=skill_tool_result.artifact,
            turn=self.state.turn,
        )
        return _CONTINUE

    def _op_execute_tool(self, llm_text: str, turn, op, outcome: dict, accumulate):
        """일반 도구 op — wrap→truncation 가드→B1/A4/A5→실행→관찰 조립
        (단독이면 자체 append, N-op 이면 accumulate)."""
        tool_name = op.action
        tool_input = op.action_input or {}

        # Multi-op formats emit flat single-target ops (one file / edit /
        # query / task per op); the tool re-wraps that into its canonical
        # prefixed input so the validate → strip → run pipeline below is
        # unchanged. Single-action formats bypass this (their input is
        # already canonical).
        if (
            getattr(self.cfg.wire_format, "multi_op", False)
            and tool_name in TOOLS
            and isinstance(tool_input, dict)
        ):
            tool_input = TOOLS[tool_name].wrap_single_op(tool_input)

        # Truncation guard: if JSON was repaired (truncated response),
        # strip the last element from edit_file's lines arrays
        truncation_warning = ""
        if op.truncated and tool_name == "edit_file":
            tool_input, truncation_warning = _sanitize_truncated_edit(tool_input)

        # B1 (action loop) detection — observe BEFORE dispatch so a
        # repeated call doesn't pay the cost of the redundant tool
        # run. Counter resets after a tool error so legitimate
        # retries don't false-positive.
        prev_was_error = bool(
            self.tools.recent_tool_history
            and self.tools.recent_tool_history[-1].get("tool") == tool_name
            and self.tools.recent_tool_history[-1].get("success") is False
        )
        loop_level = self.loop_detector.observe(
            tool_name, tool_input, prev_was_error=prev_was_error
        )
        if loop_level >= 1:
            outcome["failure_signal"] = FAILURE_ACTION_LOOP
            args_repr = (
                json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
                if isinstance(tool_input, dict)
                else str(tool_input)
            )
            intervention = format_action_loop_intervention(
                level=loop_level,
                action=tool_name,
                args_repr=args_repr,
                repeat_count=self.loop_detector.consecutive_count,
                task=self._task_text(),
            )
            if intervention is None:
                # Level ≥3: recovery exhausted — hard fail with a
                # message that cites which primitives were already
                # tried so the user knows we did not give up early.
                _debug_log(
                    f"Loop hard-fail: {tool_name} input={args_repr[:100]} "
                    f"level={loop_level} skill_name={self.cfg.skill_name}"
                )
                render_run_ended(
                    f"Action loop unresolved: {tool_name} repeated; "
                    "tried probe_progress and restate_task without "
                    "recovery. Stopping."
                )
                return ToolResult(
                    False,
                    error=(
                        "Action loop unresolved: probe_progress and "
                        "restate_task did not break the repetition."
                    ),
                )
            if accumulate is not None:
                # N-op 배치 (v9.23.4): A4/A5 와 같은 모양 — 이 op 만 실행하지
                # 않았다고 적고 **다음 op 로 간다**. 종전엔 배치 한가운데서
                # `_intervene` 을 불러(단일 op 전용 — 턴 되감기 + 자기 관찰)
                # 배치의 flush 와 겹쳤고, 런이 첫 op 의 shell 출력을 최종답으로
                # **성공 종료**했다(재현: [새 op, 직전과 같은 op, 새 op]).
                self.tools.accumulate_raw(
                    accumulate,
                    tool_name,
                    f"{intervention.message} This op did NOT run — the other ops "
                    "in this batch did.",
                    False,
                )
                return None
            # Level 1 or 2: inject Intervention, skip dispatch,
            # let the next turn try again with the new context.
            #
            # 막힌 호출은 저장하지 않는다 (v9.23.4 — "재시도는 기록하지 않는다"
            # 를 B1 로 확장). 실행되지 않은 반복 호출이 assistant 레코드로 남으면
            # 실행된 것처럼 읽히고 반복 패턴을 모델에게 보여 준다 — B1 넛지는
            # fold 대상이 아니라 영구히 쌓였다. 넛지 문구가 이미
            # `shell({...}) N times` 로 무엇을 반복했는지 말한다.
            return self._intervene(
                llm_text,
                intervention.message,
                f"action loop ({tool_name}, level {loop_level})",
                outcome,
                failure_signal=FAILURE_ACTION_LOOP,
                tool_name=tool_name,
                primitives=intervention.primitives,
                store_emission=False,
            )

        # Render the model's ACTUAL emission, not the dispatch-canonical
        # form. `wrap_single_op` above re-wrapped the flat op into the
        # tool's prefixed/batch shape (e.g. read_file `{path}` →
        # `{read_file_reads:[...]}`) so the validate→strip→run pipeline is
        # unchanged — but showing THAT misrepresents what the model wrote
        # and diverges from history.jsonl / resume-replay (which store the
        # raw op). The dispatch keeps `tool_input` (wrapped); only the card
        # shows `op.action_input` (pre-wrap).
        display_input = op.action_input if op.action_input is not None else {}
        render_step(
            "action",
            "",
            self.state.turn,
            tool_name=tool_name,
            tool_input=json.dumps(display_input, ensure_ascii=False)
            if isinstance(display_input, dict)
            else str(display_input),
        )

        # A4 (Unknown tool) — pre-dispatch detection. Skips _dispatch_tool_with_hooks
        # entirely so the recovery layer is the single source of truth for
        # this failure mode (DESIGN.md §4 invariant: same primitive shape
        # across reused failures). The error message is the same one the
        # leaf-level dispatch would have produced — primitive extraction
        # for "did you mean" suggestions is deferred to Step 4b once
        # observability data shows whether it improves recovery.
        if detect_unknown_tool(tool_name, self.cfg.tools_list):
            avail = ", ".join(self.cfg.tools_list)
            err_msg = f"Unknown tool '{tool_name}'. Available: {avail}"
            if accumulate is not None:
                # N-op 배치 (v9.21.0): 이 op 만 실패로 적고 **다음 op 로 간다**.
                # 종전엔 턴 수준 형식 개입으로 배치를 중단했고, 뒤의 op 들은
                # 아무 말 없이 버려졌다 — 실측(kdsd0j): [shell, memory, memory✗,
                # message] 에서 message 가 안 나갔는데 관찰은 "[1/2] OK [2/2]
                # OK" 라 모델이 보냈다고 믿었다. 런타임 실패는 이미 FAILED 로
                # 적고 계속 가므로, 입력 검증 실패도 같은 모양이어야 한다.
                outcome["failure_signal"] = FAILURE_UNKNOWN_TOOL
                self.tools.accumulate_raw(
                    accumulate,
                    tool_name,
                    f"{err_msg} Fix this op and re-send it alone.",
                    False,
                )
                return None
            return self._intervene(
                llm_text,
                f"Observation: {err_msg}\n{echo_prior_output(llm_text)}",
                "unknown tool",
                outcome,
                failure_signal=FAILURE_UNKNOWN_TOOL,
                tool_name=tool_name,
                recovery_kind="format",
                store_emission=False,  # 인용이 유일한 사본 (v9.23.2)
            )

        # A5 (Schema mismatch) — pre-dispatch detection. Same rationale
        # as A4: single source of truth in the recovery layer. The
        # detector also normalizes the input (string→dict promotion)
        # when valid; we use the normalized value if present.
        mismatched, schema_err, normalized = detect_schema_mismatch(
            tool_name, tool_input
        )
        if mismatched:
            err_msg = f"{schema_err} Fix action_input and retry."
            if accumulate is not None:
                # N-op 배치: 위 A4 와 같은 이유 — 실패 op 로 적고 계속.
                outcome["failure_signal"] = FAILURE_SCHEMA_MISMATCH
                self.tools.accumulate_raw(
                    accumulate,
                    tool_name,
                    f"{schema_err} This op did NOT run. Fix action_input and re-send "
                    "it alone — the other ops in this batch already ran.",
                    False,
                )
                return None
            return self._intervene(
                llm_text,
                f"Observation: {err_msg}\n{echo_prior_output(llm_text)}",
                "schema mismatch",
                outcome,
                failure_signal=FAILURE_SCHEMA_MISMATCH,
                tool_name=tool_name,
                recovery_kind="format",
                store_emission=False,  # 인용이 유일한 사본 (v9.23.2)
            )
        tool_input = normalized  # use post-normalization input for dispatch

        # Execute tool (method tracks self.tools.recent_tool_history,
        # uses self.* for provider/ctx/hooks/etc.)
        tool_result = self.tools._dispatch_tool_with_hooks(tool_name, tool_input)

        # N-op accumulate mode: record the execution for the caller's
        # combined observation instead of appending one here. The render
        # happens once, from storage, in _append_observation (single-op
        # below, or _flush_op_results for the combined) — so the live card
        # matches ctx + resume. ``accumulate_observation`` applies BOTH the
        # per-result cap and the per-turn budget; the single-op path below
        # needs only the former (one op cannot blow a turn budget by itself).
        if accumulate is not None:
            self.tools.accumulate_observation(
                accumulate,
                tool_name,
                tool_result,
                tool_input,
                suffix=truncation_warning,
            )
            return None

        observation = self.tools._tool_observation(tool_name, tool_result, tool_input)
        if truncation_warning:
            observation = f"{observation}\n{truncation_warning}"

        # Inject observation with structured artifact. On an
        # action-name correction, rewrite the assistant prior + history
        # to the corrected wire shape so neither the next turn nor a
        # resume re-feeds the raw drift (mimicry-strengthening).
        obs_msg = f"Observation: {observation}"
        # foreign-format 구제(ops 레코드)가 우선 — 단수 inference 보정은
        # 구제가 없을 때만 (구제 레코드는 inference 결과까지 이미 반영).
        corrected = outcome.get("corrected_record")
        if corrected is None and outcome.get("action_inferred"):
            corrected = {
                "role": "assistant",
                "thought": turn.thought or "",
                "action": op.action,
                "action_input": op.action_input,
            }
        _append_observation(
            self.state.messages,
            self.ctx,
            self.cfg.wire_format,
            llm_text,
            obs_msg,
            tool_name=tool_name,
            success=tool_result.success,
            artifact=tool_result.artifact,
            corrected_record=corrected,
            turn=self.state.turn,
        )
        return _CONTINUE

    # No usable action on this op — fall through to recovery.
    def _recover_unparsed(self, llm_text: str, turn, outcome: dict):
        """Missing action or parse failure — retry with the appropriate hint.

        Echoes the model's failed output back as failure grounding (content
        shows structural drift: YAML-style keys, function-call syntax,
        bare prose). Thinking-channel echo is excluded from v1 — see
        docs/robust-harness/DESIGN.md §2.2.
        """
        if turn.parse_stage > 0:
            # Parsed OK but no action -- LLM forgot to include the action
            _debug_log(
                f"No action in parsed JSON (stage={turn.parse_stage}):\n{llm_text}"
            )
            intervention = format_no_action_retry(
                prior_content=llm_text, wire_format=self.cfg.wire_format
            )
            recovery_reason = "no action"
        else:
            # JSON parse failed entirely
            _debug_log(f"JSON parse failed (stage={turn.parse_stage}):\n{llm_text}")
            syntax_error = self.cfg.wire_format.diagnose_syntax_error(llm_text)
            intervention = format_no_json_retry(
                prior_content=llm_text,
                wire_format=self.cfg.wire_format,
                syntax_error=syntax_error,
            )
            recovery_reason = "invalid JSON"
        # failure_signal 은 넘기지 않는다 — _handle_text_path 의 초기 분류
        # (NO_OUTPUT/NO_JSON/NO_ACTION)를 그대로 승계.
        # 재시도는 기록하지 않는다 (v9.21 원칙, v9.23.2 에서 이 경로로 확장):
        # 실패한 원문은 저장하지 않고 넛지가 앞뒤를 인용한다. 종전엔 원문을
        # 저장하고 **전문을** 인용해 같은 텍스트가 두 번 들어갔다 — 32K자 폭주
        # 한 번이 컨텍스트를 2만 토큰 늘렸고, 재시도가 계속 실패해 fold 도 안
        # 됐다(Harbor extract-elf: 모델이 그 조각을 22턴 흉내 냈다).
        return self._intervene(
            llm_text,
            intervention.message,
            recovery_reason,
            outcome,
            primitives=intervention.primitives,
            recovery_kind="format",
            store_emission=False,
        )

    # ── C1 PR-2: 도구 호출은 ToolBridge 소유 — 아래는 기존 호출면 유지용
    #    위임 (dispatch 클러스터가 PR-3 에서 승격되면 bridge 를 직접 주입받아
    #    이 위임들도 소멸).


_QUESTION_TEXT_KEYS = ("question", "text", "content", "q")


def _extract_question_text(item) -> str | None:
    """Pull the text out of a single question item, or return None if
    the item can't be interpreted as a question.

    Strings are returned as-is (when non-empty). Dicts are probed for
    one of the known text-bearing field names. Anything else — nested
    lists, numbers, dicts without a recognizable text field, dicts
    whose value is itself a non-string — returns None so the caller
    can drop the item rather than rendering a raw repr.
    """
    if isinstance(item, str):
        return item if item else None
    if isinstance(item, dict):
        for key in _QUESTION_TEXT_KEYS:
            v = item.get(key)
            if isinstance(v, str) and v:
                return v
    return None


def _extract_questions(action_input) -> list[str]:
    """Extract a list of question strings from an `ask` tool input,
    tolerating the various shapes models emit:

    - {"questions": ["a", "b"]}                 — the canonical form
    - {"questions": "single"}                   — single-string variant
    - {"question": "legacy"}                    — older singular field
    - "direct question"                         — bare string
    - ["q1", "q2"]                              — bare list
    - {"questions": [{"question": "..."}, ...]} — list of dict items
      with a nested text field (qwen3.6 drift)
    - {"questions": {"question": "..."}}        — single dict wrapper

    Dict items without a recognizable text field drop silently instead
    of rendering as `str(dict)` repr noise.
    """
    if isinstance(action_input, dict):
        raw_questions = action_input.get("questions") or action_input.get("question")
    elif isinstance(action_input, (str, list)):
        raw_questions = action_input
    else:
        return []

    # Normalize to list
    if isinstance(raw_questions, str):
        return [raw_questions] if raw_questions else []
    if isinstance(raw_questions, list):
        return [t for q in raw_questions if (t := _extract_question_text(q))]
    if isinstance(raw_questions, dict):
        text = _extract_question_text(raw_questions)
        return [text] if text else []
    return []


def _handle_ask(questions: list[str]) -> str:
    """Display all questions at once and collect a single response.

    종전엔 ``handler`` 인자로 **블로킹 ask 라우팅**(teammate P2)을 받았다 —
    worker 가 질문을 main mailbox 에 올리고 답을 블록 대기하는 경로. 상주
    에이전트의 ask 가 v9.12 에서 비동기 질문 포트로 옮겨가면서 실값을 주는
    생산자가 사라졌고, 그 뒤로 분기는 **테스트만 붙들고 있었다**(협력자를
    직접 만들어 검사하니 도달 불가를 아무도 못 봤다 — 이 저장소의 배선
    누락과 정확히 같은 모양). C3 에서 인자째 제거한다.
    """
    import re

    from agent_cli.render import get_renderer

    # Strip existing leading "1.", "2)", "- ", etc. so our numbering isn't doubled
    def _strip_leading_marker(q: str) -> str:
        return re.sub(r"^\s*(?:\d+[.):]|[-*•])\s+", "", q)

    # Respect nested depth prefix (so ask inside skill/delegate aligns with │)
    renderer = get_renderer()
    prefix = getattr(renderer, "_prefix", "")

    # Announce the questions through the renderer instead of writing
    # to ``console`` directly. CLI renderers print the colored block;
    # WebRenderer no-ops because the same text reaches the UI via
    # ``prompt_user(context=...)`` below, and a duplicate emission
    # would just bleed terminal noise into the web-launch terminal.
    cleaned_questions = [_strip_leading_marker(q) for q in questions]
    renderer.announce_ask(cleaned_questions, prefix=prefix)
    # Plain-text mirror of the announcement above — passed to
    # ``prompt_user`` as ``context`` so out-of-band renderers (web)
    # can surface the question alongside the input affordance. CLI
    # renderers ignore it; ``announce_ask`` is what the terminal
    # user sees with colour.
    if len(questions) > 1:
        context_lines = [
            f"{i}. {_strip_leading_marker(q)}" for i, q in enumerate(questions, 1)
        ]
    else:
        context_lines = [_strip_leading_marker(questions[0])]
    context_text = "Agent asks:\n" + "\n".join(f"  {line}" for line in context_lines)
    # Route through the renderer so paste and """ ... """ multiline work
    # at the CLI and a web renderer can serve the same prompt as a form
    # without the loop knowing the difference. ``prompt_user`` propagates
    # EOF / Ctrl+C — caller policy is "(no response)" so the assistant
    # gets a stable answer slot even when the user bails.
    if not renderer.can_prompt():
        # No interactive channel right now (non-TTY CLI / no connected web
        # client) → don't block forever on an answer that can't arrive;
        # give the model a stable answer slot. ``prompt_user`` itself
        # serializes + pauses any Live panel, so a delegate worker thread
        # is fine when a channel IS available.
        answer = "(no response)"
    else:
        try:
            answer = renderer.prompt_user(
                f"{prefix}\n{prefix}Your answer: ",
                multiline=True,
                continuation=f"{prefix}... ",
                context=context_text,
            )
        except (EOFError, KeyboardInterrupt):
            answer = "(no response)"

    q_part = "\n".join(f"Q: {_strip_leading_marker(q)}" for q in questions)
    return f"{q_part}\nA: {answer}"


_ECHO_FINAL_RE = re.compile(
    r'^echo\s+["\']?(.+?)["\']?\s*$',
    re.DOTALL,
)


def _claimed_ids(action_input) -> set[str] | None:
    """`complete` 이 주장한 요청 id 집합. **부재는 `None`** (빈 집합과 다르다).

    부재 = "어느 것에 답했는지 안 밝힘"(모름), 빈 집합 = "아무것도 안 답함"
    (모델 자신의 진술). 이 둘을 같게 다루면 하네스가 거짓을 단언하거나
    (실측 xrnway) 진술을 삼킨다.
    """
    if not isinstance(action_input, dict):
        return None
    raw = action_input.get("answers")
    if isinstance(raw, list):
        return {str(x) for x in raw if x}
    if isinstance(raw, str) and raw.strip():
        # 단일 id 를 문자열로 보내는 모델 습관 — 관용한다.
        return {raw.strip()}
    return None


def _try_echo_as_final(tool_name: str, tool_input) -> str | None:
    """Detect 'echo ...' shell calls that are actually final answers.

    Small models often use shell echo instead of the complete tool.
    Only matches simple echo commands with no pipes, redirects, or chaining.
    """
    if tool_name != "shell" or not isinstance(tool_input, dict):
        return None
    cmd = tool_input.get("command", "").strip()
    # Reject if command has pipes, redirects, semicolons, &&, || etc.
    if any(c in cmd for c in ["|", ">", "<", ";", "&&", "||", "`", "$("]):
        return None
    m = _ECHO_FINAL_RE.match(cmd)
    if m:
        return m.group(1).strip().strip("'\"")
    return None


def _sanitize_truncated_edit(tool_input: dict) -> tuple[dict, str]:
    """Strip the last (likely incomplete) line from a truncated edit_file op.

    edit_file is flat-native (consolidation Step 3) — one op = one edit. When
    the response was cut off mid-emission the final ``lines`` element is
    probably a partial line, so drop it and warn the model to re-read and
    finish the edit. Returns (sanitized_input, warning_message).
    """
    lines = tool_input.get("lines")
    if not lines:
        return tool_input, ""

    sanitized = {**tool_input, "lines": lines[:-1]}
    warning = (
        "[warn] Response was truncated — the last (incomplete) line of this "
        "edit was dropped. Re-read the file to verify and complete the edit."
    )
    return sanitized, warning


def _combined_tool_label(names: list[str]) -> str:
    """Run-length-compress a multi-op turn's tool names for the combined
    observation's label: ``["shell"] + ["write_file"]*12`` → ``shell+write_file×12``
    instead of a 137-char ``shell+write_file+write_file+...`` that overflows the
    line. Consecutive same-tool ops collapse to ``tool×N``; order is preserved
    (non-adjacent repeats stay separate runs)."""
    out: list[str] = []
    i = 0
    while i < len(names):
        j = i
        while j < len(names) and names[j] == names[i]:
            j += 1
        out.append(names[i] if j - i == 1 else f"{names[i]}×{j - i}")
        i = j
    return "+".join(out)


def _append_observation(
    messages: list[dict],
    ctx,
    wire_format,
    llm_text: str,
    obs_msg: str,
    *,
    tool_name: str,
    success: bool,
    turn: int = 0,
    artifact: str = "",
    corrected_record: dict | None = None,
    render: bool = True,
    recovery_kind: str = "",
    store_emission: bool = True,
) -> None:
    """Text parsing: append assistant + observation + sync ctx.

    ``store_emission=False`` (v9.21.0): 모델의 원문을 **저장하지 않는다** —
    관찰만 남긴다. 물린 ``complete`` 에 쓴다: 거부 관찰이 원문을 인용해
    자기완결로 안내하므로(사용자 결정) 그 emission 을 컨텍스트에 둘 이유가
    없고, 두면 저장 형태(`ops:[complete]`)가 history 에서 final 로 읽힌다.
    user 턴이 연속되지만 드레인 주입이 이미 그렇고 프로바이더는 그대로
    전달한다.

    The next-turn prior (the in-memory ``messages`` assistant turn) is
    ALWAYS the rendered history record — ``render_assistant_from_history``
    of either the corrected record or the serialized one. Because the
    record is sanitized at save time (``serialize_assistant_for_history``
    cleans both the structured thought and the bare-content fallback via
    ``sanitize_thought``), the prior never carries a wire sentinel the model
    leaked mid-turn. Re-feeding such drift would strengthen mimicry (next
    turn's prior, or a resumed session's restored prior, teaches "repeating
    the shape / dropping the action is fine") — the format-runaway root
    cause. This
    unifies the live prior with the resume prior (both go through render) and
    with the action-inferred correction, which already rendered its record.

    ``corrected_record`` (set after an action-name correction, where
    ``infer_action`` recovered a dropped action from the action_input key
    prefixes) supplies the structured record directly instead of re-parsing
    the raw. The correction stays traceable via the TurnRecorder
    (``parse_stage=3`` + ``action_inferred``).

    For history.jsonl (via ctx.add), the same record is stored, so the
    on-disk history retains structured form.

    The observation entry stores ``tool`` (the tool that ran, or an
    empty string for format-retry interventions) and ``success`` (so
    the web renderer's ``replay_from_history`` can re-emit the same
    ✓/✗ shape a live observation event has). The presence of the
    ``tool`` key — not its truthiness — distinguishes a tool result
    from a plain user chat turn, so empty-string ``tool_name`` (used
    by format-retry paths) still routes through ``observation()``.
    """
    if corrected_record is not None:
        history_record = corrected_record
    else:
        history_record = wire_format.serialize_assistant_for_history(llm_text)
    prior_content = wire_format.render_assistant_from_history(history_record)["content"]

    if store_emission:
        messages.append({"role": "assistant", "content": prior_content})
    messages.append({"role": "user", "content": obs_msg})
    stored_content = obs_msg
    if ctx:
        if store_emission:
            ctx.add(history_record)
        obs_entry = {
            "role": "user",
            "tool": tool_name,
            "success": success,
            "content": obs_msg,
        }
        if artifact:
            obs_entry["artifact"] = artifact
        # 개입 마킹 (fold, v4.51.0): "format"=파싱/스키마 개입 — 해소 시
        # 캐시 뷰에서 접힌다(records.is_format_intervention 계약). additive
        # 라 구 세션 레코드(필드 없음)는 fold 대상 아님 = 안전 기본.
        if recovery_kind:
            obs_entry["recovery"] = recovery_kind
        stored = ctx.add(obs_entry)
        # ctx.add returns the stored (possibly spilled) message; tolerate a
        # ctx stub that returns None (some tests) by keeping obs_msg.
        if isinstance(stored, dict):
            stored_content = stored.get("content", obs_msg)

    # Single render point for observations: render what was STORED so the live
    # web/CLI card matches ctx and resume. ``render=False`` for recovery paths,
    # which already surface the intervention via ``render_recovery`` (no
    # double-render).
    if render:
        display = stored_content
        if isinstance(display, str) and display.startswith("Observation: "):
            display = display[len("Observation: ") :]
        render_step("observation", display, turn, tool_name=tool_name, success=success)
