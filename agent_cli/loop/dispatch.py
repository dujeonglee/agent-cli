"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import json
import os
import re
import time

#: message/ask 빚을 남긴 채 `complete` 하려는 주체를 독촉하는 상한 (v9.21.0;
#: v9.22.0 부터 main·상주 공통, 질문(answer)·회신(reply) 공통).
#: 1회는 "깜빡함" 을, 3회면 "이해 못 함" 까지 잡는다. 사용자 결정. 무제한이면
#: 아무것도 런을 못 멈춘다 — 개입은 max_turns 미계수, B1 은 도구 경로에만
#: 있어 반복 complete 을 안 본다. 실측(a209hq): 플레이어 셋은 1회에 응했고
#: 오케스트레이터만 1회를 넘겼다.
from agent_cli.dialects import try_foreign_parse
from agent_cli.dialects.base import ParsedTurn
from agent_cli.dialects.recovery.json import describe_json_error

# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.
from agent_cli.loop.ledger import wrong_closer_hint
from agent_cli.loop.skill_invoke import _handle_run_skill
from agent_cli.loop.state import _CONTINUE, _NOT_HANDLED, LoopConfig, LoopState
from agent_cli.loop.tool_bridge import ToolBridge, _normalize_input
from agent_cli.recovery.common_recovery import format_action_loop_intervention
from agent_cli.recovery.detectors import (
    ActionLoopDetector,
    detect_nested_envelope,
    detect_schema_mismatch,
    detect_unknown_tool,
    unwrap_nested_envelope,
)
from agent_cli.recovery.dialect_recovery import (
    build_format_nudge,
    make_format_nudge,
)
from agent_cli.recovery.observability import (
    FAILURE_ACTION_LOOP,
    FAILURE_DEGENERATE,
    FAILURE_FOREIGN_FORMAT,
    FAILURE_NESTED_ENVELOPE,
    FAILURE_NO_ACTION,
    FAILURE_NO_JSON,
    FAILURE_NO_OUTPUT,
    FAILURE_OUTPUT_SWALLOWED,
    FAILURE_SCHEMA_MISMATCH,
    FAILURE_UNKNOWN_TOOL,
)
from agent_cli.recovery.primitives import echo_prior_output, summarize_call
from agent_cli.render import (
    drop_pending_thought,
    flush_pending_thought,
    render_recovery,
    render_run_ended,
    render_step,
)
from agent_cli.tools import TOOLS, infer_action
from agent_cli.tools.registry import effective_tool_names
from agent_cli.tools.result import ToolResult
from agent_cli.verbose import debug_log as _debug_log

# parallel_safe 배치 디스패치 엔진이 실제로 배선된 도구들.
# ``_dispatch_parallel_batch`` 는 도구별 병렬 엔진 호출을 알아야 하므로,
# 여기 없는 parallel_safe 도구는 배치로 묶지 않고 순차 per-op 경로를 탄다
# (종전엔 수집은 parallel_safe 플래그만 보고 묶은 뒤 디스패치에서
# NotImplementedError 로 런이 죽는 크래시 트랩 — 리뷰 §4.1). 새 도구를
# 병렬 배치에 태우려면 엔진을 배선하고 이 집합에 추가한다.
_PARALLEL_BATCH_ENGINES = frozenset({"agent"})


#: A batch op rejected before it ran (unknown tool, schema mismatch, repeated
#: call). One wording for all three (v9.24.2): the old texts claimed "the other
#: ops in this batch already ran" even when every op had failed, and differed
#: per path. On its own line so it never glues onto the error text.
BATCH_OP_SKIPPED_NOTE = (
    "This op did NOT run. The other ops in this batch were still attempted — "
    "see their own results. Fix this op and re-send only it."
)


def _flag(value, *, default: bool) -> bool:
    """도구 인자의 불리언 — bool 그대로, 문자열은 false/no/0/off 만 거짓."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("false", "no", "0", "off"):
            return False
        if v in ("true", "yes", "1", "on"):
            return True
        return default
    return bool(value)


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
        #: 마지막 턴의 판정 (--verbose 기록용, v9.24.3) — `_handle_text_path` 가 채운다.
        self.last_outcome: dict | None = None
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
        record: dict | None,
        failure_signal: str | None = None,
        tool_name: str = "",
        primitives=None,
        recovery_kind: str = "",
        render: bool = False,
        store_emission: bool = True,
        nudge: dict | None = None,
    ):
        """개입(회복 넛지) 공통 마무리 — 종전 5곳 복제 블록의 단일화.

        ``nudge`` (v10.5.0): 파싱 실패의 구조화 기록 — 있으면 관찰 레코드는
        ``message`` 대신 이것만 저장한다(문장은 읽을 때 방언이 조립).

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
        # 기록하지 않는 시도의 생각은 그리지도 않는다 (v9.24.11). 생각은
        # 턴 처리 전 보류돼 있다(render.drop_pending_thought) — 맨 먼저
        # 버려야 한다: 아래 기록 단계(ctx.add → get_renderer)가 보류분을
        # 내보내기 때문이다. 기록되는 개입(B1 등)은 그대로 보인다.
        if not store_emission:
            drop_pending_thought()
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
            self.cfg.dialect,
            record,
            message,
            tool_name=tool_name,
            success=False,
            turn=self.state.turn,
            render=render,  # False: render_recovery already surfaced it
            recovery_kind=recovery_kind,
            store_emission=store_emission,
            nudge=nudge,
        )
        if failure_signal is not None:
            outcome["failure_signal"] = failure_signal
        if primitives is not None:
            outcome["primitives"] = list(primitives)
        self.state.turn -= 1  # 개입은 턴 미계수 (통일 규칙 — docstring 참조)
        return _CONTINUE

    def _next_hidx(self) -> int | None:
        """레코드보다 **먼저** 그리는 카드(행동·최종답)의 history 서수 (v10.6.0)."""
        return getattr(self.ctx, "next_ordinal", None)

    def _last_hidx(self) -> int | None:
        """레코드 **뒤에** 그리는 카드(관찰·에코 최종답)의 history 서수."""
        return getattr(self.ctx, "last_ordinal", None)

    def _record_emission(self, llm_text: str) -> None:
        """옵트인 원문 기록 (Phase 5 — docs/dialects/PHASE5.md §7).

        ``AGENT_CLI_RECORD_EMISSIONS=1`` 이면 모델이 낸 텍스트를 파싱 **전에**
        ``<session>/emissions.jsonl`` 에 한 줄씩 남긴다. history 는 파싱 결과만,
        turns 는 지표만 담아 원문이 어디에도 없었다 — 파서 등가성 코퍼스와
        라이브 bakeoff 의 재료다. 기본은 꺼짐(디스크·용량 영향 0)."""
        if os.environ.get("AGENT_CLI_RECORD_EMISSIONS") != "1":
            return
        sdir = getattr(self.ctx, "session_dir", None) if self.ctx is not None else None
        if not sdir:
            return
        try:
            with open(
                os.path.join(str(sdir), "emissions.jsonl"), "a", encoding="utf-8"
            ) as f:
                f.write(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "format": self.cfg.dialect.name,
                            "text": llm_text,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass  # 기록은 best-effort — 런을 막지 않는다

    def _handle_text_path(
        self,
        llm_text: str,
        usage=None,
        *,
        thinking=None,
        swallowed: bool = False,
        server_ops: list | None = None,
    ):
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

        ``server_ops`` (v10.20.0): 서버 파싱 방언이 호출 목록에서 바로 만든
        op 들 — ``llm_text`` 는 산문(생각)뿐이라 파싱하지 않는다. 기록은
        ``turn.record`` 에 ops shape 로 한 번 만든다(산문만 직렬화하면 호출이
        사라진다).
        """
        self._record_emission(llm_text)
        # v10.1.4: 빈 content 의 재시도 문구가 사고 채널 유무로 갈린다
        self._turn_thinking = (thinking or "").strip()
        self._turn_swallowed = bool(swallowed)
        server_record: dict | None = None
        if server_ops:
            thought = self.cfg.dialect.sanitize_thought(llm_text) or None
            turn = ParsedTurn(
                thought=thought, ops=list(server_ops), raw=llm_text, parse_stage=1
            )
            server_record = {
                "role": "assistant",
                "thought": thought or "",
                "ops": [
                    {
                        "action": op.action,
                        # 깨진 인자(str)는 실행되지 않는다 — 기록에는 빈 인자로.
                        "action_input": op.action_input
                        if isinstance(op.action_input, dict)
                        else {},
                    }
                    for op in turn.ops
                ],
            }
        else:
            turn = self.cfg.dialect.parse_turn(llm_text)
        # Phase 3 — foreign-format 구제 (dialects DESIGN §9): 바인딩
        # 포맷이 0-op 로 읽은 emission 을 타 등록 포맷 파서가 action-보유
        # ops 로 읽어내면 그 turn 으로 진행한다 (실측: 35B xml_fc 스트림의
        # json_fc 회귀 — 0-op 마찰의 17%, PHASE2.md §8). 라벨은 아래
        # 분류에서 FOREIGN_FORMAT, 직렬화는 corrected_record 로 바인딩
        # 포맷의 캐노니컬 shape 재렌더 (누출 raw 재공급 없음 — 자기 교정).
        foreign_source: str | None = None
        if not turn.ops and server_ops is None:
            rescued = try_foreign_parse(self.cfg.dialect, llm_text)
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
        # Gated on ``action_required``: when the dialect requires an
        # explicit action (action_required=True), a dropped action is a
        # drift to be corrected by the model, so we skip inference and fall
        # through to the NO_ACTION recovery below. When False (the namespaced
        # format), the action is recoverable from the
        # preserved action_input, so we infer it.
        action_inferred = False
        if not self.cfg.dialect.action_required:
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
        if self.cfg.dialect.is_degenerate(llm_text):
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
                initial_signal = (
                    FAILURE_OUTPUT_SWALLOWED
                    if getattr(self, "_turn_swallowed", False)
                    else FAILURE_NO_OUTPUT
                )
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
        # 이 턴의 history 레코드 — **여기서 한 번** 만든다(v10.42.0). 종전엔
        # 관찰을 기록하는 쪽이 원문을 다시 파싱했고, 서버 파싱·구제·이름 복원은
        # 선택 인자 ``corrected_record`` 로 덮어쓰는 구조라 그 인자를 안 넘긴
        # 가상 op 경로(reply·message·answer·ask)가 `{"content": ""}` 를 남겼다.
        # 서버 호출·구제·복원은 ops shape(방언 중립, 바인딩 포맷의 render 가
        # 캐노니컬로 재방출 — 누출 raw 재공급 없음); 텍스트 파싱은 방언의 직렬화.
        if server_record is not None:
            turn.record = server_record
        elif foreign_source is not None or action_inferred:
            turn.record = {
                "role": "assistant",
                "thought": turn.thought or "",
                "ops": [
                    {"action": op.action, "action_input": op.action_input or {}}
                    for op in turn.ops
                ],
            }
        else:
            turn.record = self.cfg.dialect.serialize_assistant_for_history(llm_text)

        try:
            return self._dispatch_turn(llm_text, turn, outcome)
        finally:
            # 아무 출력 없이 끝난 턴의 생각도 사라지지 않게 (v9.24.11 안전망)
            flush_pending_thought()
            self.recorder.record(
                model=self.cfg.model,
                parse_stage=turn.parse_stage,
                failure_signal=outcome["failure_signal"],
                primitives_applied=outcome["primitives"],
                usage=usage,
            )
            # --verbose 기록(v9.24.3)이 원문과 함께 싣는 이 턴의 판정 — 코어가
            # 응답(사고·종료 사유)과 합쳐 한 줄로 쓴다.
            self.last_outcome = {
                "parse_stage": turn.parse_stage,
                "failure_signal": outcome["failure_signal"],
                "primitives": list(outcome["primitives"]),
                "ops": [
                    {"action": op.action, "input": op.action_input} for op in turn.ops
                ],
            }

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
                self._flush_op_results(turn.record, results)
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
            path = (
                op.action_input.get("path")
                if isinstance(op.action_input, dict)
                else None
            )
            if op.action == "edit_file" and isinstance(path, str) and path:
                # 경로가 진짜 문자열일 때만 묶는다 (v10.49.0) — 경로 없는 op 끼리
                # (`None == None`) 묶여 스키마 검사를 건너뛰고 "cannot read 'None'"
                # 이 op 수만큼 나왔다(편집 A/B 실측 base-t4-3, 16건). 홀로 가면
                # A5 가 "Missing required field(s) … path" 로 답한다.
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
                self._flush_op_results(turn.record, results)
                return r
            i += 1
        self._flush_op_results(turn.record, results)
        return _CONTINUE

    def _emit_obs(
        self, record, accumulate, tool_name: str, obs: str, ok: bool, *, render=True
    ):
        """한 op 이 관찰을 냈다 — 묶음이면 모으고, 홀로면 바로 붙인다 (v10.49.0 C3).

        종전엔 ask·message·reply·answer 가 이 if/else 를 각자 다섯 벌 들고
        있었다. 모양은 그대로다: 묶음 ``accumulate_raw`` → ``None``(다음 op),
        홀로 ``Observation: …`` 관찰 → ``_CONTINUE``."""
        if accumulate is not None:
            self.tools.accumulate_raw(accumulate, tool_name, obs, ok)
            return None
        _append_observation(
            self.state.messages,
            self.ctx,
            self.cfg.dialect,
            record,
            f"Observation: {obs}",
            tool_name=tool_name,
            success=ok,
            turn=self.state.turn,
            render=render,
        )
        return _CONTINUE

    def _reject_op(
        self,
        llm_text: str,
        turn,
        outcome: dict,
        accumulate,
        *,
        tool_name: str,
        batch_note: str,
        alone: str,
        reason: str,
        signal: str,
    ):
        """이 op 을 실행하지 않는다 — 입력 검증 실패의 한 길 (v10.49.0 C3).

        **묶음 안이면** 실패 op 로 적고 다음 op 로 간다(v9.21.0 — 뒤의 op 이
        조용히 버려지던 kdsd0j 의 수리). **홀로면** 형식 개입: 원문을 인용하고
        재시도는 저장하지 않으며 턴을 세지 않는다(인용이 유일한 사본, v9.23.2).
        이 차이는 의도다 — 홀로 망가진 호출은 되묻는 게 싸고, 묶음의 나머지는
        이미 실행됐을 수 있다. A4(모르는 도구)·A5(스키마)·인자 JSON 오류가 쓴다."""
        outcome["failure_signal"] = signal
        if accumulate is not None:
            self.tools.accumulate_raw(
                accumulate, tool_name, f"{batch_note}\n{BATCH_OP_SKIPPED_NOTE}", False
            )
            return None
        return self._intervene(
            llm_text,
            alone,
            reason,
            outcome,
            record=turn.record,
            failure_signal=signal,
            tool_name=tool_name,
            recovery_kind="format",
            store_emission=False,
        )

    def _flush_op_results(self, record: dict, results: list[dict]) -> None:
        """Append ONE combined observation for accumulated op results.

        Per-op header lines (``[i/N] tool — OK/FAILED``) frame each op's
        output; turn success = all ops succeeded (any-fail ⇒ failed so the
        model retries the failed op next turn). No-op when nothing ran.
        ``record`` 는 이 턴의 history 레코드(``turn.record``).
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
            self.cfg.dialect,
            record,
            f"Observation: {combined}",
            tool_name=_combined_tool_label([r["tool_name"] for r in results]),
            success=all_ok,
            turn=self.state.turn,
            # 이미지 (v10.59.0): op 순서대로 이어 붙인다 — 글의 자리표시 줄이
            # 어느 op 의 것인지 말한다.
            images=[img for r in results for img in (r.get("images") or [])],
            # 기록은 방언 중립(v10.2.2): op 별 조각을 항상 남긴다 — 서버 파싱
            # 방언으로 다시 읽을 때 op 마다 `tool` 메시지가 필요하고, 합친
            # 본문에서는 경계를 되살릴 수 없다(v10.2.0 은 native 에서만 저장해
            # json_fc 세션을 native 로 이으면 호출·결과 짝이 안 맞았다).
            parts=[
                {
                    "tool": r["tool_name"],
                    "success": bool(r["success"]),
                    "content": r["observation"],
                }
                for r in results
            ],
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
                hidx=self._next_hidx(),
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
                hidx=self._next_hidx(),
                tool_name="edit_file",
                tool_input=json.dumps(disp, ensure_ascii=False),
            )

        path = batch_ops[0].action_input.get("path")
        edits = [op.action_input for op in batch_ops]
        result, parts = self.tools.dispatch_edit_batch(path, edits)
        # 호출마다 자기 결과 (v10.23.0): 앞의 조각들은 작아서 그대로, 마지막
        # 조각은 파일 전체 에코를 업고 있어 예산·스필을 타는 일반 관찰 경로로.
        for part in parts[:-1]:
            self.tools.accumulate_raw(
                accumulate, "edit_file", part["content"], bool(part["success"])
            )
        last = parts[-1]
        last_result = (
            ToolResult(True, output=last["content"], artifact=result.artifact)
            if last["success"]
            else ToolResult(False, error=last["content"])
        )
        self.tools.accumulate_observation(
            accumulate, "edit_file", last_result, batch_ops[-1].action_input
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
        _debug_log(f"PARSED iter={self.state.turn} action={op.action}")
        # v10.20.0: 서버가 준 호출인데 인자가 JSON 이 아니었다(provider 가 원문을
        # 문자열로 넘김). 어느 도구든 실행하지 않는다 — `complete` 도: 종전엔
        # 빈 dict 로 바뀌어 "(Completed without result)" 로 런이 끝났다.
        if isinstance(op.action_input, str):
            return self._op_bad_arguments(llm_text, turn, op, outcome, accumulate)

        # 7. Complete tool (text parsing path)
        if op.action == "complete":
            return self._op_complete(llm_text, turn, op, outcome)

        # 7b. wait (v10.65.0) — 닫지 않고 끝낸다, 깨울 것이 있을 때만.
        if op.action == "wait":
            return self._op_wait(llm_text, turn, op, outcome)

        # 9. Detect echo-as-final-answer (common small model pattern)
        echo_answer = _try_echo_as_final(op.action, op.action_input)
        if echo_answer:
            if self.ctx:
                self.ctx.add(
                    self.cfg.dialect.serialize_terminal_for_history(
                        turn.thought or "", echo_answer
                    )
                )
            render_step("final", echo_answer, self.state.turn, hidx=self._last_hidx())

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

        # ── 장부 정산 (v10.56.0, docs/ledger/DESIGN.md P3) ──
        # 정책 하나: 결과는 **언제나** 먼저 배달하고, 열린 항목이 남으면 런 공유
        # 예산 안에서 되돌린다(계속형 붙잡기). 종전엔 되묻기(결과 미배달, 런당 1)·
        # 요청 독촉(요청당 1)·빚 독촉(런 3, 비사용자 런은 결과 미배달 거부)이 셋으로
        # 갈렸다.
        ledger = self.state.ledger
        claimed = _claimed_ids(op.action_input)
        # **지우기 전에** 뽑는다 — 정산이 주장분을 지우므로 그 뒤엔 이 답이 무엇에
        # 대한 답인지 알 수 없다.
        answered = ledger.claimed_requests(claimed)
        still_open = ledger.settle(claimed)
        debts = ledger.debts()
        render_step(
            "final", answer, self.state.turn, requests=answered, hidx=self._next_hidx()
        )
        if (still_open or debts) and ledger.spend():
            # 기록은 `_intervene` 이 한다 — 이 턴의 assistant 레코드(종결 모양)를
            # 관찰과 함께 저장한다. 따로 넣으면 같은 final 이 두 번 들어간다.
            turn.record = self._terminal_record(turn, answer, claimed, still_open)
            return self._nag_open_items(
                llm_text, turn, claimed, still_open, debts, outcome
            )
        answer = self._with_unanswered_notice(claimed, still_open, debts, answer)
        if self.ctx:
            self.ctx.add(self._terminal_record(turn, answer, claimed, still_open))
        return ToolResult(True, output=answer)

    def _op_wait(self, llm_text: str, turn, op, outcome: dict):
        """terminal ``wait`` op (v10.65.0, docs/wait/DESIGN.md).

        깨울 것(`runtime.wake_sources`)이 있으면: 열린 요청을 ctx 에 넘기고
        (`park_requests` — 다음 런의 장부로), 호출과 관찰을 기록하고(히스토리는
        보통의 op + 관찰 모양 — 새 카드 종류 없이 재생·resume 이 그대로 그린다),
        런을 ``waiting`` 결과로 끝낸다. 없으면: 거부 관찰을 남기고 런을 잇는다 —
        막혔을 때 wait 로 도피하는 길은 없다."""
        from agent_cli.runtime import wake_sources

        reason = ""
        if isinstance(op.action_input, dict):
            reason = str(op.action_input.get("reason") or "").strip()
        sources = wake_sources(self.cfg.owner)
        if not sources:
            _append_observation(
                self.state.messages,
                self.ctx,
                self.cfg.dialect,
                turn.record,
                "Observation: wait refused — nothing you started is pending (no "
                "background job, monitor or agent of yours), so nothing would wake "
                "you. Keep working, or call `complete` if the task is done.",
                tool_name="wait",
                success=False,
                turn=self.state.turn,
            )
            return _CONTINUE
        ledger = self.state.ledger
        open_ids = ledger.request_ids()
        if self.ctx is not None:
            self.ctx.park_requests(list(ledger.requests))
        kept = (
            f" Open items stay open for the next run: {', '.join(open_ids)}."
            if open_ids
            else ""
        )
        obs = f"Observation: waiting — will wake on: {'; '.join(sources)}.{kept}"
        _append_observation(
            self.state.messages,
            self.ctx,
            self.cfg.dialect,
            turn.record,
            obs,
            tool_name="wait",
            success=True,
            turn=self.state.turn,
        )
        summary = f"⏸ waiting on {'; '.join(sources)}" + (
            f" — {reason}" if reason else ""
        )
        return ToolResult(True, output=summary, waiting=True)

    def _terminal_record(self, turn, answer: str, claimed, still_open) -> dict:
        """종결 레코드 — `answers` 가 있으면 정산 뒤 남은 요청 id 도 함께
        (v10.23.0; 서버 파싱 렌더가 결과 문구에 쓴다)."""
        return self.cfg.dialect.serialize_terminal_for_history(
            turn.thought or "",
            answer,
            answers=None if claimed is None else sorted(claimed),
            open_requests=None
            if claimed is None
            else [str(r.get("id")) for r in still_open],
        )

    def _nag_open_items(self, llm_text: str, turn, claimed, requests, debts, outcome):
        """결과는 이미 나갔다 — 남은 항목을 닫는 순서대로 보여 주고 런을 잇는다.

        **붙잡기가 아니다.** 결과를 인질로 잡지 않는다(agent-ask 3판에서 틀린 자리)
        — 답은 이미 렌더되고 기록에 들어갔다. 아직 닫히지 않은 항목이 있어 런을
        닫지 않을 뿐이다. 남은 예산을 적는다: 다 쓰면 하니스가 닫는다."""
        ledger = self.state.ledger
        n = len(requests) + len(debts)
        if claimed is None and requests:
            why = (
                "your `complete` did not say which items it closes (`answers` is "
                "missing), so nothing was closed"
            )
        else:
            why = f"{n} item{'s are' if n != 1 else ' is'} still open"
        left = ledger.budget
        budget = (
            f"({left} more reminder{'s' if left != 1 else ''} before the harness "
            "closes the rest: unanswered requests are reported to the user, unpaid "
            "replies get your run summary, unanswered questions are closed.)"
            if left > 0
            else "(Last reminder — after this the harness closes the rest.)"
        )
        # 기다리려고 complete 를 썼다면 (v10.65.0) — 옛 습관을 한 번에 교정한다.
        from agent_cli.runtime import wake_sources

        waiting_hint = (
            " If you are only waiting for something you started to report back, "
            "call `wait` instead — it keeps these items open and the report wakes you."
            if wake_sources(self.cfg.owner)
            else ""
        )
        return self._intervene(
            llm_text,
            (
                f"Observation: your result was delivered, but {why}:\n"
                f"{ledger.items_lines(requests, debts)}\n"
                "Close them in that order — by action first, then `complete` again "
                "with `answers` for the items it covers." + waiting_hint + " " + budget
            ),
            "open items remain",
            outcome,
            record=turn.record,
            tool_name="complete",
        )

    def _with_unanswered_notice(self, claimed, requests, debts, answer: str) -> str:
        """예산이 끝난 자리의 **최후 통지** — 닫히지 않은 요청을 최종 답 말미에.

        회계일 뿐 강제가 아니다. 모델의 주장이 정직한지는 검증할 수 없다 — 검출되는
        것은 "아무도 닫지 않은 항목" 뿐이고 그것만으로 값이 있다. 빚은 런 끝의
        폴백(회신 빚은 런 요약 배달, 질문은 닫힘)이 처리한다."""
        if not requests:
            return answer
        ledger = self.state.ledger
        if claimed is None:
            # 생략은 "전부 답함" 도 "전부 미답" 도 아니다 — 모르는 것이다(xrnway:
            # 실제로는 둘 다 답했는데 `answers` 를 안 실었다). 아는 것만 적는다.
            return (
                f"{answer}\n\n📋 `complete` came without `answers`, so which of "
                f"these {len(requests)} open item(s) it covers is undeclared. Check "
                f"each:\n{ledger.lines_for_notice(requests)}"
            )
        return (
            f"{answer}\n\n⏳ {len(requests)} open item(s) in this run were not "
            f"closed:\n{ledger.lines_for_notice(requests)}"
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
                hidx=self._next_hidx(),
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
                return self._op_ask_async(
                    turn.record, questions, port, accumulate, to=to
                )
            # 비동기 포트가 없으면(main/delegate) 사람에게 직접 묻는다.
            # 종전엔 여기 ``cfg.ask_handler`` 분기가 하나 더 있었는데 실값
            # 생산자가 **하나도 없었다** — 상주 에이전트의 ask 는 v9.12 에서
            # 질문 포트로 옮겨갔고, 그때 남은 죽은 가지였다.
            user_response = _handle_ask(questions)
            # ``ask`` is a normal observation-producing op (the user's
            # reply is the observation), not a terminal. In a multi-op turn
            # it accumulates like read/shell so consecutive asks batch into
            # the one combined observation; alone it appends its own.
            return self._emit_obs(
                turn.record,
                accumulate,
                "ask",
                f"User responded:\n{user_response}",
                True,
                render=False,  # the answer is surfaced by the input UI
            )

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
        # v10.1.0: `expects_reply:false` = 빚 없는 통지. 태그 방언은 값을 문자열로
        # 주므로 "false"/"no"/"0" 도 거짓으로 읽는다.
        expects_reply = _flag(args.get("expects_reply", True), default=True)
        render_step(
            "action",
            "",
            self.state.turn,
            hidx=self._next_hidx(),
            tool_name="message",
            tool_input=json.dumps(args, ensure_ascii=False),
        )
        try:
            result = handler(to, text, expects_reply=expects_reply)
        except Exception as e:
            result = f"message failed: {type(e).__name__}: {e}"
        obs = f"[message → {to or '?'}] {result}"
        return self._emit_obs(turn.record, accumulate, "message", obs, True)

    def _op_ask_async(self, record: dict, questions, port, accumulate, *, to=None):
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
        return self._emit_obs(record, accumulate, "ask", obs, True)

    def _op_reply(self, llm_text: str, turn, op, accumulate):
        """``reply`` op (v9.21.0) — 이 런을 시킨 쪽에게 빚진 회신을 갚는다.

        ``_op_answer`` 와 동형. 포트가 없거나 상주가 아니면 ``_NOT_HANDLED``.
        """
        port = self.cfg.questions
        if port is None:
            return _NOT_HANDLED
        args = op.action_input if isinstance(op.action_input, dict) else {}
        text = str(args.get("text", "")).strip()
        render_step(
            "action",
            "",
            self.state.turn,
            hidx=self._next_hidx(),
            tool_name="reply",
            tool_input=json.dumps(args, ensure_ascii=False),
        )
        try:
            err = port.reply(
                text, id=str(args.get("id", "") or ""), to=str(args.get("to", "") or "")
            )
        except Exception as e:
            err = f"reply failed: {type(e).__name__}: {e}"
        if err:
            err = wrong_closer_hint(str(args.get("id", "") or ""), "reply") or err
        ok = not err
        obs = "[reply] " + (err or "delivered to whoever was waiting on it")
        return self._emit_obs(turn.record, accumulate, "reply", obs, ok)

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
            hidx=self._next_hidx(),
            tool_name="answer",
            tool_input=json.dumps(args, ensure_ascii=False),
        )
        try:
            err = port.answer(qid, text)
        except Exception as e:
            err = f"answer failed: {type(e).__name__}: {e}"
        if err:
            # v10.30.0 → v10.56.0: 다른 종류의 항목 id 를 질문 id 로 썼다(실측 dj72nx
            # — 사용자 요청 번호). 접두가 닫는 법을 말하므로 그 길을 돌려준다.
            err = wrong_closer_hint(qid, "answer") or err
        ok = not err
        obs = f"[answer → {qid or '?'}] " + (err or "delivered to the asker")
        return self._emit_obs(turn.record, accumulate, "answer", obs, ok)

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
            hidx=self._next_hidx(),
            tool_name="run_skill",
            tool_input=json.dumps(skill_input, ensure_ascii=False),
        )
        skill_tool_result = _handle_run_skill(
            skill_input,
            self.cfg.binding,
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
            self.cfg.dialect,
            turn.record,
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
            getattr(self.cfg.dialect, "multi_op", False)
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
        # 짝 (v10.32.0): 이력의 마지막 행이 정확히 이 호출(도구+정규화 인자)
        # 이면 그 결과가 "직전 결과". 감지기는 dispatch 전에 세므로 A4/A5 로
        # **실행 전에 거부된** 호출도 센다 — 그 반복은 결과를 본 적이 없는
        # 재시도라 루프 넛지가 아니라 거부 안내를 다시 받아야 한다(사용자
        # 지적). 그래서 1·2단계는 짝이 있을 때만 끼어들고, 짝이 없으면 아래
        # A4/A5 로 흘려 보낸다. 상한(4번째 동일 반복 = 하드 실패)은 짝과
        # 무관하게 유지 — 같은 거부를 세 번 받고도 같은 인자를 보내는 건
        # 개입 턴이 max_turns 를 안 세는 이상 다른 상한이 없다.
        last = (
            self.tools.recent_tool_history[-1]
            if self.tools.recent_tool_history
            else None
        )
        paired = bool(
            last
            and last.get("tool") == tool_name
            and last.get("input") == _normalize_input(tool_input)
        )
        if loop_level >= 1 and (paired or loop_level >= 3):
            outcome["failure_signal"] = FAILURE_ACTION_LOOP
            args_repr = (
                json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
                if isinstance(tool_input, dict)
                else str(tool_input)
            )
            intervention = (
                format_action_loop_intervention(
                    level=loop_level,
                    action=tool_name,
                    args_repr=args_repr,
                    repeat_count=self.loop_detector.consecutive_count,
                    task=self._task_text(),
                    prior_result=last.get("result") or "",
                )
                if paired
                else None
            )
            if intervention is None:
                # Level ≥3: recovery exhausted — hard fail. The message
                # says what was tried: the two nudges (paired), or the same
                # rejection three times (unpaired — no result ever came).
                _debug_log(
                    f"Loop hard-fail: {tool_name} input={args_repr[:100]} "
                    f"level={loop_level} paired={paired} "
                    f"skill_name={self.cfg.skill_name}"
                )
                tried = (
                    "tried probe_progress and restate_task without recovery"
                    if paired
                    else "the same arguments were rejected before every run"
                )
                render_run_ended(
                    f"Action loop unresolved: {tool_name} repeated; {tried}. Stopping."
                )
                return ToolResult(
                    False,
                    error=f"Action loop unresolved: {tried}.",
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
                    f"{intervention.message}\n{BATCH_OP_SKIPPED_NOTE}",
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
            # ``recovery_kind="loop"`` (v10.32.0): 넛지도 일회성 교정 재료라
            # 다음 파싱 성공(또는 더 새로운 개입)에 캐시 뷰에서 접는다 —
            # 형식 개입과 같은 fold. 종전엔 빈 값이라 런 끝까지 남았다.
            return self._intervene(
                llm_text,
                intervention.message,
                f"action loop ({tool_name}, level {loop_level})",
                outcome,
                record=turn.record,
                failure_signal=FAILURE_ACTION_LOOP,
                tool_name=tool_name,
                primitives=intervention.primitives,
                recovery_kind="loop",
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

        def show_action() -> None:
            render_step(
                "action",
                "",
                self.state.turn,
                hidx=self._next_hidx(),
                tool_name=tool_name,
                tool_input=json.dumps(display_input, ensure_ascii=False)
                if isinstance(display_input, dict)
                else str(display_input),
            )

        # 단일 호출은 형식 검사(A4/A5)를 **통과한 뒤에** 그린다 (v9.24.11) —
        # 형식 거절된 시도는 저장되지 않고 재시도가 대신하므로 그 행동 카드도
        # 생각도 그리지 않는다(v9.8.0 원칙). 먼저 그리면 보류한 생각까지
        # 내보냈다. 배치(accumulate)는 형식이 틀린 op 도 실패 op 로 **기록**되고
        # 나머지가 계속되므로 종전대로 먼저 그린다.
        if accumulate is not None:
            show_action()

        # A4 (Unknown tool) — pre-dispatch detection. Skips _dispatch_tool_with_hooks
        # entirely so the recovery layer is the single source of truth for
        # this failure mode (DESIGN.md §4 invariant: same primitive shape
        # across reused failures). The error message is the same one the
        # leaf-level dispatch would have produced — primitive extraction
        # for "did you mean" suggestions is deferred to Step 4b once
        # observability data shows whether it improves recovery.
        if detect_unknown_tool(tool_name, self.cfg.tools_list):
            # 프롬프트·함수 스키마와 같은 함수에서 (v10.14.0) — `tools_list` 만
            # 쓰면 항상 실리는 `complete` 이 빠지고 순서도 프롬프트와 달랐다.
            avail = ", ".join(
                effective_tool_names(self.cfg.tools_list, self.cfg.dialect)
            )
            err_msg = f"Unknown tool '{tool_name}'. Available: {avail}"
            # 거부된 호출의 한 줄 요약 (v10.37.0): 저장되지 않는 호출을 모델이
            # 볼 수 있는 유일한 자리. native_fc 는 산문 인용이 비어 이것만 남는다.
            sent = summarize_call(
                tool_name,
                display_input,
                hint="Re-emit it under one of the available names.",
            )
            return self._reject_op(
                llm_text,
                turn,
                outcome,
                accumulate,
                tool_name=tool_name,
                batch_note=f"{err_msg}\n{sent}",
                alone=f"Observation: {err_msg}\n{sent}\n{echo_prior_output(llm_text)}",
                reason="unknown tool",
                signal=FAILURE_UNKNOWN_TOOL,
            )

        # A5 (Schema mismatch) — pre-dispatch detection. Same rationale
        # as A4: single source of truth in the recovery layer. The
        # detector also normalizes the input (string→dict promotion)
        # when valid; we use the normalized value if present.
        mismatched, schema_err, normalized = detect_schema_mismatch(
            tool_name, tool_input
        )
        if mismatched:
            err_msg = f"{schema_err} Fix the arguments and retry."
            # 틈(빠진 필수·모르는 키)은 도구가 답한다 — 검증이 본 모양
            # (`tool_input`)으로 묻고, 인자는 모델이 쓴 모양(`display_input`)
            # 으로 보여 준다(카드와 같은 이유).
            missing, unknown = TOOLS[tool_name].schema_gaps(tool_input)
            sent = summarize_call(
                tool_name, display_input, missing=missing, unknown=unknown
            )
            return self._reject_op(
                llm_text,
                turn,
                outcome,
                accumulate,
                tool_name=tool_name,
                batch_note=f"{schema_err}\n{sent}",
                alone=f"Observation: {err_msg}\n{sent}\n{echo_prior_output(llm_text)}",
                reason="schema mismatch",
                signal=FAILURE_SCHEMA_MISMATCH,
            )
        tool_input = normalized  # use post-normalization input for dispatch
        if accumulate is None:
            show_action()

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

        observation, images = self.tools.observe(tool_name, tool_result, tool_input)
        if truncation_warning:
            observation = f"{observation}\n{truncation_warning}"

        # Inject observation with structured artifact. On an
        # action-name correction, rewrite the assistant prior + history
        # to the corrected wire shape so neither the next turn nor a
        # resume re-feeds the raw drift (mimicry-strengthening).
        obs_msg = f"Observation: {observation}"
        _append_observation(
            self.state.messages,
            self.ctx,
            self.cfg.dialect,
            turn.record,
            obs_msg,
            tool_name=tool_name,
            success=tool_result.success,
            artifact=tool_result.artifact,
            turn=self.state.turn,
            images=images,
        )
        return _CONTINUE

    # No usable action on this op — fall through to recovery.
    def _op_bad_arguments(self, llm_text: str, turn, op, outcome: dict, accumulate):
        """호출 인자가 JSON 이 아닌 op (v10.20.0) — 실행 없이 어디서 깨졌는지
        알린다. 배치 안이면 그 op 의 자리에 실패 메모로, 홀로면 형식 개입으로."""
        tool_name = op.action or "?"
        raw = str(op.action_input)
        where = describe_json_error(raw) or "not a JSON object"
        err_msg = (
            f"The arguments of your `{tool_name}` call were not valid JSON "
            f"({where}), so the call was NOT executed. Re-send it with the "
            "arguments as one JSON object."
        )
        return self._reject_op(
            llm_text,
            turn,
            outcome,
            accumulate,
            tool_name=tool_name,
            batch_note=err_msg,
            alone=f"Observation: {err_msg}\n{echo_prior_output(raw)}",
            reason="bad arguments",
            signal=FAILURE_SCHEMA_MISMATCH,
        )

    def _recover_unparsed(self, llm_text: str, turn, outcome: dict):
        """Missing action or parse failure — retry with the appropriate hint.

        Echoes the model's failed output back as failure grounding (content
        shows structural drift: YAML-style keys, function-call syntax,
        bare prose). Thinking-channel echo is excluded from v1 — see
        docs/robust-harness/DESIGN.md §2.2.
        """
        if getattr(turn, "split_calls", False):
            # 호출이 산문으로 나뉜 두 군데 이상 — 고르지 않고 되묻는다 (v10.24.0).
            _debug_log(f"Split tool calls (not dispatched):\n{llm_text}")
            nudge = make_format_nudge("split_calls", llm_text)
            recovery_reason = "split calls"
        elif turn.parse_stage > 0:
            # Parsed OK but no action -- LLM forgot to include the action
            _debug_log(
                f"No action in parsed JSON (stage={turn.parse_stage}):\n{llm_text}"
            )
            nudge = make_format_nudge("no_action", llm_text)
            recovery_reason = "no action"
        else:
            # JSON parse failed entirely
            _debug_log(f"JSON parse failed (stage={turn.parse_stage}):\n{llm_text}")
            nudge = make_format_nudge(
                "no_json",
                llm_text,
                syntax_error=self.cfg.dialect.diagnose_syntax_error(llm_text),
                thinking_only=bool(
                    not (llm_text or "").strip() and getattr(self, "_turn_thinking", "")
                ),
                swallowed=bool(
                    not (llm_text or "").strip()
                    and getattr(self, "_turn_swallowed", False)
                ),
            )
            recovery_reason = "invalid JSON"
        # 저장은 구조(nudge)만, 문장은 방언이 조립한다 (v10.5.0) — 라이브
        # 메시지와 캐시 렌더·resume 이 같은 build_format_nudge 를 쓴다.
        intervention = build_format_nudge(nudge, self.cfg.dialect)
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
            record=turn.record,
            primitives=intervention.primitives,
            recovery_kind="format",
            store_emission=False,
            nudge=nudge,
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
    (모델 자신의 진술). 둘 다 아무것도 닫지 않는다(`Ledger.settle`) — 되돌림
    문구와 최종 통지가 가른다. 같게 다루면 하네스가 거짓을 단언하거나(실측
    xrnway) 진술을 삼킨다.
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
    dialect,
    record: dict | None,
    obs_msg: str,
    *,
    tool_name: str,
    success: bool,
    turn: int = 0,
    artifact: str = "",
    render: bool = True,
    recovery_kind: str = "",
    store_emission: bool = True,
    parts: list[dict] | None = None,
    nudge: dict | None = None,
    images: list[dict] | None = None,
) -> None:
    """Append assistant (``record``) + observation + sync ctx.

    ``images`` (v10.59.0): 관찰에 딸린 이미지 참조(``ImageRef.to_dict()``) —
    레코드의 ``images`` 필드로 저장되고 요청 조립 때 공급자 경계가 조각으로
    펼친다. 본문(``obs_msg``)에는 자리표시 줄이 이미 들어 있다.

    ``record`` (v10.42.0) 는 이 턴의 history 레코드 — ``ParsedTurn.record``,
    ``_handle_text_path`` 가 한 번 만든다. 여기서는 원문을 파싱하지 않는다:
    저장하는 모든 경로가 같은 레코드를 쓰므로 "어느 경로가 구조화 기록을
    빠뜨렸나" 가 생길 수 없다. ``store_emission=False`` 면 None 이어도 된다
    (저장할 emission 이 없다).

    ``nudge`` (v10.5.0): 구조화 형식 넛지 — 레코드에 ``content`` 대신
    ``nudge`` 를 저장한다. 요청 메시지(``obs_msg``)는 호출자가 같은 구조에서
    현재 방언으로 조립한 것이라 라이브 == 렌더.

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
    if store_emission:
        if record is None:
            raise ValueError("_append_observation: a stored emission needs its record")
        history_record = record
        prior_content = dialect.render_assistant_from_history(history_record)["content"]
        messages.append({"role": "assistant", "content": prior_content})
    live_obs = {"role": "user", "content": obs_msg}
    if images:
        live_obs["images"] = list(images)
    messages.append(live_obs)
    stored_content = obs_msg
    if ctx:
        # 넛지는 마지막 실패 것만 남긴다(v10.2.3, 사용자 결정): 앞선 미해소
        # 형식 개입은 이 새 개입이 대체한다 — 쌓이면 실패 원문 인용이 겹쳐
        # 모방 재료가 된다(v4.51.0 이 피하려던 것). 성공 시 전체 접기는 그대로.
        # 여기(모든 관찰의 단일 기록점)에 둔다: 종전엔 ``_intervene`` 에만 있어
        # 이 함수를 직접 부르는 출력 잘림·폭주 중단 안내가 연속 실패 동안
        # 쌓였다(v10.19.1 — 세 번 잘리면 인용 셋이 다음 호출에 실렸다).
        # "loop"(B1, v10.32.0)도 같은 자리: 2단계 넛지가 붙기 직전 1단계
        # 넛지를 접어 컨텍스트엔 항상 최신 개입 하나만 남는다.
        if recovery_kind:
            ctx.fold_resolved_interventions(assume_tail_resolved=True)
        if store_emission:
            ctx.add(history_record)
        obs_entry = {"role": "user", "tool": tool_name, "success": success}
        if nudge is not None:
            obs_entry["nudge"] = nudge
        else:
            obs_entry["content"] = obs_msg
        if artifact:
            obs_entry["artifact"] = artifact
        # 개입 마킹 (fold, v4.51.0): "format"=파싱/스키마 개입 — 해소 시
        # 캐시 뷰에서 접힌다(records.is_format_intervention 계약). additive
        # 라 구 세션 레코드(필드 없음)는 fold 대상 아님 = 안전 기본.
        if recovery_kind:
            obs_entry["recovery"] = recovery_kind
        if parts:
            obs_entry["parts"] = parts
        if images:
            obs_entry["images"] = list(images)
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
        render_step(
            "observation",
            display,
            turn,
            tool_name=tool_name,
            success=success,
            hidx=getattr(ctx, "last_ordinal", None),  # 더미 ctx(테스트)는 None
            images=list(images) if images else None,
        )
