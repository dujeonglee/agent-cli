"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import json
import re

from agent_cli.recovery.common_recovery import format_action_loop_intervention
from agent_cli.recovery.wf_recovery import (
    format_no_action_retry,
    format_no_json_retry,
)
from agent_cli.tools.result import ToolResult

from agent_cli.recovery.detectors import (
    ActionLoopDetector,
    detect_nested_envelope,
    detect_schema_mismatch,
    detect_thought_missing,
    detect_unknown_tool,
    unwrap_nested_envelope,
)
from agent_cli.recovery.observability import (
    FAILURE_ACTION_LOOP,
    FAILURE_DEGENERATE,
    FAILURE_NESTED_ENVELOPE,
    FAILURE_NO_ACTION,
    FAILURE_NO_JSON,
    FAILURE_NO_OUTPUT,
    FAILURE_NO_THOUGHT,
    FAILURE_SCHEMA_MISMATCH,
    FAILURE_UNKNOWN_TOOL,
)
from agent_cli.render import (
    render_recovery,
    render_status,
    render_step,
)
from agent_cli.tools import TOOLS, infer_action

from agent_cli.verbose import debug_log as _debug_log

# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.
from agent_cli.loop.state import LoopConfig, LoopState, _CONTINUE, _NOT_HANDLED
from agent_cli.loop.tool_bridge import ToolBridge
from agent_cli.loop.skill_invoke import _handle_run_skill


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

    def _handle_text_path(self, llm_text: str):
        """Handle text parsing response (non-JSON fallback).

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
        # format — react), the action is recoverable from the
        # preserved action_input, so we infer it. Mirror of how
        # ``thought_required`` gates the NO_THOUGHT recovery.
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

        try:
            return self._dispatch_turn(llm_text, turn, outcome)
        finally:
            self.recorder.record(
                model=self.cfg.model,
                parse_stage=turn.parse_stage,
                failure_signal=outcome["failure_signal"],
                primitives_applied=outcome["primitives"],
                raw=llm_text,
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
        first_action = next((op.action for op in turn.ops if op.action), None)

        # A7 NO_THOUGHT — action present but thought missing. Retry
        # before dispatch so the omission does not enter the transcript
        # as a precedent for future turns (mimicry-strengthening loop:
        # the raw response is mirrored back on the next turn and
        # crowds out the system prompt's Format Rule 1).
        if self.cfg.wire_format.thought_required and detect_thought_missing(
            turn.thought, first_action
        ):
            # ``thought_required`` is False on plugins where the thought
            # is preceding free text rather than a schema field — for
            # those, missing thought is not a drift signal.
            _debug_log(f"NO_THOUGHT: action={first_action!r}, thought={turn.thought!r}")
            # ReAct-only: format_no_thought_retry lives on the plugin,
            # not in recovery/builders, because it has no meaning when
            # ``thought_required`` is False (envelope plugins).
            intervention = self.cfg.wire_format.format_no_thought_retry(
                prior_content=llm_text
            )
            render_recovery(
                llm_text, intervention.message, "no thought", self.state.turn
            )
            _append_observation(
                self.state.messages,
                self.ctx,
                self.cfg.wire_format,
                llm_text,
                intervention.message,
                tool_name="",
                success=False,
                turn=self.state.turn,
                render=False,  # render_recovery already surfaced it
                recovery_kind="format",
            )
            outcome["failure_signal"] = FAILURE_NO_THOUGHT
            outcome["primitives"] = list(intervention.primitives)
            self.state.turn -= 1
            return _CONTINUE

        # 6. Thought
        if turn.thought:
            render_step("thought", turn.thought, self.state.turn)

        # No usable ops at all (parse failure / no action recovered, including
        # a thought-only turn) — straight to recovery. md_array completes via
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
            # Turn-ending special actions: flush accumulated results BEFORE
            # the branch runs so its observation lands after the work done
            # so far (chronological order for the model).
            if op.action in ("complete", "run_skill"):
                self._flush_op_results(llm_text, results)
                results = []
                return self._dispatch_op(llm_text, turn, op, outcome)
            # Parallel batch: a run of ≥2 consecutive ops of the SAME
            # parallel_safe tool dispatches concurrently into one combined
            # observation (delegate: independent subagents). A lone
            # parallel_safe op falls through to the normal per-op path so it
            # keeps its B1/A4/A5 guards. Mutating tools (parallel_safe=False)
            # always take the sequential per-op path — order is their
            # correctness guarantee (write→edit same file, mkdir→touch).
            tool = TOOLS.get(op.action) if op.action else None
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
                # Guard/recovery fired inside the op (B1/A4/A5/no-action):
                # its intervention observation is already appended; flush the
                # accumulated work after it (rare mid-array edge — order is
                # intervention-first, results still preserved).
                self._flush_op_results(llm_text, results)
                return r
            i += 1
        self._flush_op_results(llm_text, results)
        return _CONTINUE

    def _flush_op_results(self, llm_text: str, results: list[dict]) -> None:
        """Append ONE combined observation for accumulated op results.

        Per-op header lines (``[i/N] tool — OK/FAILED``) frame each op's
        output; turn success = all ops succeeded (any-fail ⇒ failed so the
        model retries the failed op next turn). No-op when nothing ran.
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
            # Extension slot: a parallel_safe tool with no internal concurrent
            # engine (e.g. a future read-only read_file/code_index opt-in) would
            # fan its ops out over a thread-pool of per-op run() calls here.
            raise NotImplementedError(
                f"parallel_safe batch dispatch not wired for {tool_name!r}; only "
                "agent has an internal concurrent engine (_run_parallel)."
            )

        result = self.tools._dispatch_tool_with_hooks(tool_name, {"tasks": specs})
        observation = self.tools._tool_observation(tool_name, result, {"tasks": specs})
        # Rendered from storage by _flush_op_results' _append_observation
        # (combined card), matching ctx + resume — no separate pre-render.
        accumulate.append(
            {
                "tool_name": tool_name,
                "observation": observation,
                "success": result.success,
            }
        )

    def _dispatch_edit_batch(self, llm_text, turn, batch_ops, outcome, *, accumulate):
        """Apply a run of ≥2 consecutive same-path edit_file ops as ONE batch,
        appending ONE combined result to *accumulate*.

        Calls the pure ``apply_edits_batch`` (resolve all refs against one
        original read → reject overlaps → bottom-up apply → one write,
        all-or-nothing). Single-direction call: the loop hands it ``(path,
        edits)`` and gets a ToolResult back — edit_file knows nothing of the
        loop. Each op's flat input also gets rendered as its own action card,
        matching the single-op render.
        """
        from agent_cli.tools.edit_file import apply_edits_batch

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
        result = apply_edits_batch(path, edits)
        observation = self.tools._tool_observation(
            "edit_file", result, batch_ops[0].action_input
        )
        accumulate.append(
            {
                "tool_name": "edit_file",
                "observation": observation,
                "success": result.success,
            }
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
            return self._op_complete(turn, op, outcome)

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

        if op.action:
            return self._op_execute_tool(llm_text, turn, op, outcome, accumulate)

        return self._recover_unparsed(llm_text, turn, outcome)

    def _op_complete(self, turn, op, outcome: dict):
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

        if self.ctx:
            self.ctx.add(
                self.cfg.wire_format.serialize_terminal_for_history(
                    turn.thought or "", answer
                )
            )
        render_step("final", answer, self.state.turn)

        return ToolResult(True, output=answer)

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
            # handler 없음(main/delegate)이면 종전 단일-인자 호출 유지 —
            # 기존 테스트/외부 patcher 의 _handle_ask 교체 표면 보존.
            if self.cfg.ask_handler is not None:
                user_response = _handle_ask(questions, handler=self.cfg.ask_handler)
            else:
                user_response = _handle_ask(questions)
            # ``ask`` is a normal observation-producing op (the user's
            # reply is the observation), not a terminal. In a multi-op turn
            # it accumulates like read/shell so consecutive asks batch into
            # the one combined observation; alone it appends its own.
            if accumulate is not None:
                accumulate.append(
                    {
                        "tool_name": "ask",
                        "observation": f"User responded:\n{user_response}",
                        "success": True,
                    }
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
        except Exception as e:  # noqa: BLE001 — 핸들러 예외를 관찰로 보고
            result = f"message failed: {type(e).__name__}: {e}"
        obs = f"[message → {to or '?'}] {result}"
        if accumulate is not None:
            accumulate.append(
                {"tool_name": "message", "observation": obs, "success": True}
            )
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
        )
        obs = skill_tool_result.output or skill_tool_result.error
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
                render_status(
                    "error",
                    f"Action loop unresolved: {tool_name} repeated; "
                    "tried probe_progress and restate_task without "
                    "recovery. Stopping.",
                )
                return ToolResult(
                    False,
                    error=(
                        "Action loop unresolved: probe_progress and "
                        "restate_task did not break the repetition."
                    ),
                )
            # Level 1 or 2: inject Intervention, skip dispatch,
            # let the next turn try again with the new context.
            render_recovery(
                llm_text,
                intervention.message,
                f"action loop ({tool_name}, level {loop_level})",
                self.state.turn,
            )
            _append_observation(
                self.state.messages,
                self.ctx,
                self.cfg.wire_format,
                llm_text,
                intervention.message,
                tool_name=tool_name,
                success=False,
                turn=self.state.turn,
                render=False,  # render_recovery already surfaced it
            )
            outcome["primitives"] = list(intervention.primitives)
            self.state.turn -= 1  # Don't count loop nudges as user-facing turns
            return _CONTINUE

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
            outcome["failure_signal"] = FAILURE_UNKNOWN_TOOL
            avail = ", ".join(self.cfg.tools_list)
            err_msg = f"Unknown tool '{tool_name}'. Available: {avail}"
            render_recovery(
                llm_text, f"Observation: {err_msg}", "unknown tool", self.state.turn
            )
            _append_observation(
                self.state.messages,
                self.ctx,
                self.cfg.wire_format,
                llm_text,
                f"Observation: {err_msg}",
                tool_name=tool_name,
                success=False,
                turn=self.state.turn,
                render=False,  # render_recovery already surfaced it
                recovery_kind="format",
            )
            return _CONTINUE

        # A5 (Schema mismatch) — pre-dispatch detection. Same rationale
        # as A4: single source of truth in the recovery layer. The
        # detector also normalizes the input (string→dict promotion)
        # when valid; we use the normalized value if present.
        mismatched, schema_err, normalized = detect_schema_mismatch(
            tool_name, tool_input
        )
        if mismatched:
            outcome["failure_signal"] = FAILURE_SCHEMA_MISMATCH
            err_msg = f"{schema_err} Fix action_input and retry."
            render_recovery(
                llm_text, f"Observation: {err_msg}", "schema mismatch", self.state.turn
            )
            _append_observation(
                self.state.messages,
                self.ctx,
                self.cfg.wire_format,
                llm_text,
                f"Observation: {err_msg}",
                tool_name=tool_name,
                success=False,
                turn=self.state.turn,
                render=False,  # render_recovery already surfaced it
                recovery_kind="format",
            )
            return _CONTINUE
        tool_input = normalized  # use post-normalization input for dispatch

        # Execute tool (method tracks self.tools.recent_tool_history,
        # uses self.* for provider/ctx/hooks/etc.)
        tool_result = self.tools._dispatch_tool_with_hooks(tool_name, tool_input)

        observation = self.tools._tool_observation(tool_name, tool_result, tool_input)
        if truncation_warning:
            observation = f"{observation}\n{truncation_warning}"

        # N-op accumulate mode: record the execution for the caller's
        # combined observation instead of appending one here. The render
        # happens once, from storage, in _append_observation (single-op
        # below, or _flush_op_results for the combined) — so the live card
        # matches ctx + resume. Oversized bodies are already nudge-capped
        # by _tool_observation above.
        if accumulate is not None:
            accumulate.append(
                {
                    "tool_name": tool_name,
                    "observation": observation,
                    "success": tool_result.success,
                }
            )
            return None

        # Inject observation with structured artifact. On an
        # action-name correction, rewrite the assistant prior + history
        # to the corrected wire shape so neither the next turn nor a
        # resume re-feeds the raw drift (mimicry-strengthening).
        obs_msg = f"Observation: {observation}"
        corrected = None
        if outcome.get("action_inferred"):
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
        render_recovery(
            llm_text, intervention.message, recovery_reason, self.state.turn
        )
        _append_observation(
            self.state.messages,
            self.ctx,
            self.cfg.wire_format,
            llm_text,
            intervention.message,
            tool_name="",
            success=False,
            turn=self.state.turn,
            render=False,  # render_recovery already surfaced it
            recovery_kind="format",
        )
        # Surface composed primitive names to the enclosing _handle_text_path
        # so the trailing finally-block records them.
        outcome["primitives"] = list(intervention.primitives)
        self.state.turn -= 1  # Don't count format retries
        return _CONTINUE

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
    elif isinstance(action_input, str):
        raw_questions = action_input
    elif isinstance(action_input, list):
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


def _handle_ask(questions: list[str], handler=None) -> str:
    """Display all questions at once and collect a single response.

    ``handler`` (teammate P2): teammate 서브루프의 ask 라우팅 훅 —
    있으면 사용자 프롬프트(announce/prompt_user) 대신
    ``handler(question_text) -> answer`` 로 답을 받는다 (worker 가 질문을
    main mailbox 에 올리고 main/인간의 답변을 블록 대기). 반환 Q/A 포맷은
    사용자 경로와 동일해 teammate 모델이 같은 관찰 shape 을 본다.
    delegate 서브에이전트의 ask 는 종전대로 사용자에게 간다 (handler 없음).
    """
    import re

    from agent_cli.render import get_renderer

    # Strip existing leading "1.", "2)", "- ", etc. so our numbering isn't doubled
    def _strip_leading_marker(q: str) -> str:
        return re.sub(r"^\s*(?:\d+[.):]|[-*•])\s+", "", q)

    if handler is not None:
        cleaned = [_strip_leading_marker(q) for q in questions]
        try:
            answer = handler("\n".join(cleaned))
        except Exception:
            answer = "(no response)"
        q_part = "\n".join(f"Q: {q}" for q in cleaned)
        return f"{q_part}\nA: {answer}"

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
) -> None:
    """Text parsing: append assistant + observation + sync ctx.

    The next-turn prior (the in-memory ``messages`` assistant turn) is
    ALWAYS the rendered history record — ``render_assistant_from_history``
    of either the corrected record or the serialized one. Because the
    record is sanitized at save time (``serialize_assistant_for_history``
    cleans both the structured thought and the bare-content fallback via
    ``sanitize_thought``), the prior never carries a wire sentinel the model
    leaked mid-turn. Re-feeding such drift would strengthen mimicry (next
    turn's prior, or a resumed session's restored prior, teaches "repeating
    the shape / dropping the action is fine") — the format-runaway root
    cause, and the same class of failure the NO_THOUGHT retry avoids. This
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

    messages.append({"role": "assistant", "content": prior_content})
    messages.append({"role": "user", "content": obs_msg})
    stored_content = obs_msg
    if ctx:
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
