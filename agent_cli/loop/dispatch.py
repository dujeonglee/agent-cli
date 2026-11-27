"""`AgentLoop` 의 협력자 `TurnDispatcher` — 턴 처리 (docs/ARCHITECTURE.md §5.1)."""

from __future__ import annotations

import json
import os
import re
import time

from agent_cli import confirm
from agent_cli import render as render_surface
from agent_cli.constants import format_duration
from agent_cli.dialects import try_foreign_parse
from agent_cli.dialects.base import ParsedTurn
from agent_cli.dialects.recovery.json import describe_json_error
from agent_cli.hooks.events import PRE_WAIT
from agent_cli.loop.kind import Caller
from agent_cli.loop.ledger import wrong_closer_hint
from agent_cli.loop.reroute import reroute
from agent_cli.loop.skill_invoke import _handle_run_skill
from agent_cli.loop.state import _CONTINUE, _NOT_HANDLED, LoopConfig, LoopState
from agent_cli.loop.tool_bridge import ToolBridge, _normalize_input
from agent_cli.loop.wait_sentences import (
    NOTHING_TO_WAIT_FOR,
    RUN_ENDED_NOTHING_TO_WAIT_FOR,
    USER,
    arrived_refusal,
    mutual_wait_refusal,
    open_items_refusal,
    wait_interrupted,
)
from agent_cli.recovery.common_recovery import format_action_loop_intervention
from agent_cli.recovery.detectors import (
    ActionLoopDetector,
    StaleGuard,
    detect_nested_envelope,
    detect_schema_mismatch,
    detect_unknown_tool,
    stale_turn_pairs,
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
    FAILURE_STALE_LOOP,
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
from agent_cli.wake import harness_work_running

#: 제자리 고리 확인에서 Continue 의 코멘트를 보낸 사람을 모를 때의 작성자 (v10.120.0)
STALE_HINT_AUTHOR = "Hint"
#: 확인 프롬프트에 싣는 반복 호출 줄 수 — 대표 두세 개
_STALE_CALLS_SHOWN = 3


#: A batch op rejected before it ran (unknown tool, schema mismatch, repeated
#: call). One wording for all three (v9.24.2): the old texts claimed "the other
#: ops in this batch already ran" even when every op had failed, and differed
#: per path. On its own line so it never glues onto the error text.
BATCH_OP_SKIPPED_NOTE = (
    "This op did not run. The other ops in this batch were still attempted — "
    "see their own results. Fix this op and re-send only it."
)
#: 묶음 안의 B1(같은 호출 반복) 자리 (감사 3-a) — 넛지가 "다른 행동을" 이라고 하므로 다시
#: 보내라는 말은 붙이지 않는다(다시 보내면 곧 반복이고 다음 단계, 끝내 런 종료다).
BATCH_LOOP_SKIPPED_NOTE = (
    "This op did not run. The other ops in this batch were still attempted — "
    "see their own results."
)

#: 답할 길이 없는 곳의 `ask` (6c) — 표면이 "답이 올 수 있다" 고 말하지 않으면(`Renderer.can_prompt`) 질문을 올리지도
#: 묻지도 않는다. 누가 묻든(main·스킬·에이전트) 같은 글이다
ASK_REFUSED_NOBODY_CAN_ANSWER = (
    "ask refused — nobody can answer here: this session has no terminal and no "
    "viewer. Go on with what you have and say what you assumed in your result."
)


def _ops_shape(ops, *, broken_as_empty: bool = False) -> list[dict]:
    """op 들의 ops shape 레코드 — 층이 바꾼 op 는 ``rewritten_from`` 을 함께
    (모델에게는 그리지 않는다, `loop/reroute.py`). ``broken_as_empty`` 는 서버
    파싱 방언의 규칙: 깨진 인자(str)는 실행되지 않으므로 빈 인자로 적는다."""
    out = []
    for op in ops:
        ai = op.action_input
        if broken_as_empty:
            ai = ai if isinstance(ai, dict) else {}
        entry = {"action": op.action, "action_input": ai or {}}
        if op.rewritten_from is not None:
            entry["rewritten_from"] = op.rewritten_from
        out.append(entry)
    return out


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


#: A5 자동 전문의 머리 (v10.109.0) — 뒤에 `tool_help` 가 줄 전문이 온다.
FULL_GUIDE_HEAD = "Full guide:"


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
        # 제자리 고리 (v10.120.0, docs/stale-guard): B1 과 별개로 비연속 고리를 본다 —
        # 글자가 달라도 결과가 같은 턴이 이어진다. 표시(+웹 main 은 두 번째에 묻기)만 한다.
        self.stale_guard = StaleGuard()
        #: 🔁 표시가 떠 있나 — 발화 뒤 제자리 턴마다 N 을 갱신하고 새 결과면 지운다
        self._stale_shown = False
        #: 이 런에서 A5 자동 전문을 이미 받은 지연 도구 (v10.109.0) — 한 도구에 한 번.
        #: 디스패치만 쓰는 상태라 ``LoopState`` 가 아니라 여기(소유자)에 둔다.
        self._guided_tools: set[str] = set()

    def _first_rejection_guide(self, tool_name: str) -> str:
        """지연 도구가 이 런에서 **처음** 스키마 불일치(A5)로 거절될 때 거절 관찰 끝에
        붙일 전문 (v10.109.0, docs/tool-help/DESIGN.md §4.2) — 뼈대만 보고 틀리게 부른
        순간이 전문이 필요한 순간이고, `tool_help` 왕복 하나를 아낀다. 두 번째부터는
        ``""``. 지연 도구가 없으면(지금) 언제나 ``""``."""
        tool = TOOLS.get(tool_name)
        if not getattr(tool, "deferred", False) or tool_name in self._guided_tools:
            return ""
        self._guided_tools.add(tool_name)
        text = self.tools._run_ctx().tool_guide(tool_name)
        return f"\n{FULL_GUIDE_HEAD}\n{text}" if text else ""

    def _progressed(self, tool_name: str, tool_input) -> bool:
        """같은 호출(도구 + 정규화 인자)이 아직 진행 중인가 — B1 의 일반 규칙
        (v10.107.0, 사용자 결정 2026-11-05): 이력에서 그 호출의 **직전 두 결과**가
        다르면 진행 중이다. 결과가 하나뿐이면 비교할 것이 없어 진행으로 본다 —
        그래서 진짜 루프(같은 호출, 같은 결과)는 세 번째 호출에서 1단계가 된다.
        결과가 하나도 없으면(실행 전에 거부만 된 호출) 진행이 아니다 — 거부 반복의
        상한(짝 없음, 4번째 하드 실패)은 그대로 센다.

        비교는 관찰 본문 전체의 지문(``digest``) 그대로, 정규화 없음. 그래서 결과에
        시각·난수가 섞여 매번 다른 호출(`date`)은 감지기를 피한다 — 감수한 대가."""
        sig = _normalize_input(tool_input)
        digests: list[str] = []
        for row in reversed(self.tools.recent_tool_history):
            if row.get("tool") == tool_name and row.get("input") == sig:
                digests.append(row.get("digest", ""))
                if len(digests) == 2:
                    break
        if len(digests) == 1:
            return True
        return len(digests) == 2 and digests[0] != digests[1]

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
        # 기록하지 않는 시도의 생각은 그리지도 않는다 (v9.24.11). 생각은
        # 턴 처리 전 보류돼 있다(render.drop_pending_thought) — 맨 먼저
        # 버려야 한다: 아래 기록 단계(ctx.add → get_renderer)가 보류분을
        # 내보내기 때문이다. 기록되는 개입(B1 등)은 그대로 보인다.
        if not store_emission:
            drop_pending_thought()
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
            render=False,  # render_recovery already surfaced it
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
        call=None,
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

        ``call`` (v10.129.0): 이 턴 공급자 호출의 ``CallMetrics`` — 기록에 그대로.
        디스패치 벽시계(``tool_ms``)와 실행 목록(``tools``)은 여기서 잰다.
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
        action_inferred = False
        for op in turn.ops:
            if not op.action and isinstance(op.action_input, dict):
                inferred = infer_action(op.action_input)
                if inferred:
                    op.action = inferred
                    action_inferred = True

        # 도구를 바꾸는 층 (v10.77.0, loop/reroute.py): 결과가 똑같은 다른 도구로
        # 바꾼다 — 기록·컨텍스트에는 바뀐 호출이 남는다(아래 ops shape 레코드).
        rerouted = reroute(turn.ops, self.cfg.tools_list)

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
        outcome["primitives"].extend(rerouted)
        # 이 턴의 history 레코드 — **여기서 한 번** 만든다(v10.42.0). 종전엔
        # 관찰을 기록하는 쪽이 원문을 다시 파싱했고, 서버 파싱·구제·이름 복원은
        # 선택 인자 ``corrected_record`` 로 덮어쓰는 구조라 그 인자를 안 넘긴
        # 가상 op 경로(reply·message·answer·ask)가 `{"content": ""}` 를 남겼다.
        # 서버 호출·구제·복원은 ops shape(방언 중립, 바인딩 포맷의 render 가
        # 캐노니컬로 재방출 — 누출 raw 재공급 없음); 텍스트 파싱은 방언의 직렬화.
        if server_record is not None and rerouted:
            server_record["ops"] = _ops_shape(turn.ops, broken_as_empty=True)
        if server_record is not None:
            turn.record = server_record
        elif foreign_source is not None or action_inferred or rerouted:
            turn.record = {
                "role": "assistant",
                "thought": turn.thought or "",
                "ops": _ops_shape(turn.ops),
            }
        else:
            turn.record = self.cfg.dialect.serialize_assistant_for_history(llm_text)

        # 이 턴이 기록할 첫 레코드의 서수 — 턴 끝에 그 구간을 StaleGuard 에 먹인다
        turn_start = self._next_hidx()
        self.tools.take_turn_calls()  # 앞 턴 밖에서 남은 것이 섞이지 않게
        dispatch_started = time.monotonic()
        try:
            result = self._dispatch_turn(llm_text, turn, outcome)
        finally:
            tool_ms = round((time.monotonic() - dispatch_started) * 1000)
            # 아무 출력 없이 끝난 턴의 생각도 사라지지 않게 (v9.24.11 안전망)
            flush_pending_thought()
            self.recorder.record(
                model=self.cfg.model,
                parse_stage=turn.parse_stage,
                failure_signal=outcome["failure_signal"],
                primitives_applied=outcome["primitives"],
                usage=usage,
                call=call,
                tool_ms=tool_ms,
                tools=self.tools.take_turn_calls(),
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
        if result is _CONTINUE:
            self._watch_stale(turn_start)
        return result

    # ── 제자리 고리 (v10.120.0, docs/stale-guard/DESIGN.md) ──────────

    def _watch_stale(self, turn_start: int | None) -> None:
        """턴의 관찰이 다 기록된 뒤 한 번 — 그 턴이 ``ctx`` 에 남긴 레코드를 StaleGuard 에.

        쌍은 history 의 그 구간에서 뽑는다(`stale_turn_pairs`) — trajectory 의 (호출 이름,
        관찰 본문) 과 같은 출처다. 저장되지 않은 시도(형식 재시도·B1 이 막은 반복)는 턴이
        아니라 부르지 않는다. 모델 입력은 바꾸지 않는다: 발화는 failures.jsonl·렌더러
        표시, 웹 main 의 두 번째 발화부터는 사람에게 묻는다(Hint 만 사용자 메시지로 간다)."""
        records_since = getattr(self.ctx, "records_since", None)
        if turn_start is None or records_since is None:
            return
        pairs = stale_turn_pairs(records_since(turn_start))
        if pairs is None:
            return
        guard = self.stale_guard
        fired = guard.observe(
            pairs,
            turn=self.state.turn,
            work_running=harness_work_running(self.cfg.kind.ports.owner),
        )
        if not fired:
            if self._stale_shown:
                # 떠 있는 표시의 N 갱신 — 새 결과(0)면 지운다
                self._show_stale(asking=False)
                self._stale_shown = guard.turns_without_new > 0
            return
        calls = self._stale_calls(guard.since_turn)
        self._record_stale(calls)
        from agent_cli.render import get_renderer

        asking = (
            guard.fires >= 2
            and self.cfg.kind.owns_main_view
            and get_renderer().can_pause_for_human()
            and confirm.answerable()
        )
        self._stale_shown = True
        self._show_stale(asking=asking)
        if asking:
            self._ask_stale(calls)

    def _begin_segment(self) -> None:
        """구간(깨어 있는 토막)을 새로 시작한다 — 제자리 `wait` 에서 깰 때(5c-1). 구간마다 새로 세는 것이 여기 다 있다:
        턴(상한·꼬리의 턴 줄이 세는 기준), 되돌림 예산, 같은 호출 반복(`ActionLoopDetector`), 제자리 고리(`StaleGuard`)와
        그 🔁 표시. 런 시작은 이 객체가 새로 서므로 부르지 않는다. 열린 항목·기록의 턴 번호는 그대로다."""
        self.state.segment_turn0 = self.state.turn
        self.state.ledger.new_segment()
        self.loop_detector = ActionLoopDetector(threshold=2)
        self.stale_guard = StaleGuard()
        self.clear_stale()

    def clear_stale(self) -> None:
        """떠 있는 🔁 표시를 지운다 — 런 끝(엔진의 ``run`` finally)과 구간의 시작(`_begin_segment`)."""
        if not self._stale_shown:
            return
        self._stale_shown = False
        from agent_cli.render import get_renderer

        get_renderer().stale_loop(
            {"turns_without_new": 0, "since_turn": 0, "fires": 0, "asking": False}
        )

    def _stale_info(self, *, asking: bool) -> dict:
        g = self.stale_guard
        return {
            "turns_without_new": g.turns_without_new,
            "since_turn": g.since_turn,
            "fires": g.fires,
            "asking": asking,
        }

    def _show_stale(self, *, asking: bool) -> None:
        from agent_cli.render import get_renderer

        get_renderer().stale_loop(self._stale_info(asking=asking))

    def _stale_calls(self, since_turn: int) -> list[str]:
        """제자리 구간의 대표 호출 — 결과가 같은 것끼리 하나, 마지막 몇 개, 각 한 줄
        ``도구 인자 → 결과 첫 줄``. 확인 프롬프트와 failures.jsonl 이 같이 쓴다."""
        uniq: dict[tuple[str, str], dict] = {}
        for row in self.tools.recent_tool_history:
            if int(row.get("turn") or 0) < since_turn:
                continue
            key = (str(row.get("tool") or ""), str(row.get("digest") or ""))
            uniq.pop(key, None)
            uniq[key] = row  # 마지막에 본 자리로
        lines = []
        for row in list(uniq.values())[-_STALE_CALLS_SHOWN:]:
            call = " ".join(f"{row.get('tool')} {row.get('input') or ''}".split())
            result = (str(row.get("result") or "").strip().splitlines() or [""])[0]
            lines.append(f"{call[:100]} → {' '.join(result.split())[:60]}")
        return lines

    def _record_stale(self, calls: list[str]) -> None:
        """발화 하나 = failures.jsonl 한 줄 (`record_failure`, failure_signal ``stale_loop``)."""
        sdir = getattr(self.ctx, "session_dir", None) if self.ctx is not None else None
        if not sdir:
            return
        from agent_cli.recovery.failures import record_failure

        g = self.stale_guard
        record_failure(
            sdir,
            turn=self.state.turn,
            model=self.cfg.model,
            dialect=getattr(self.cfg.dialect, "name", "?"),
            failure_signal=FAILURE_STALE_LOOP,
            stop_reason=None,
            stop_detail=None,
            parse_stage=None,
            primitives=[],
            text="\n".join(calls),
            thinking=None,
            usage=None,
            detail={
                "turns_without_new": g.turns_without_new,
                "since_turn": g.since_turn,
                "fires": g.fires,
                "calls": calls,
            },
        )

    def _ask_stale(self, calls: list[str]) -> None:
        """웹 main 의 두 번째 발화부터 — 런을 세우고 사람에게 Continue/Stop 을 묻는다.

        안전 확인과 같은 확인(`c-N`, 6d): 트레이의 확인 항목, 말 칸이 Hint. Continue(approve) 는 런을 잇고 남긴 말을
        사람의 말로 넣는다. Stop(deny) 과 기다리는 동안의 정지(■·띠의 Stop — `/api/stop`)는 같다: 런 끝 줄(``stop_note``)
        을 남기고 멈춘다. 세션 종료로 멈추면 그 줄은 쓰이지 않는다(`_on_interrupt` 가 먼저 끝난다)."""
        g = self.stale_guard
        n = g.turns_without_new
        title = (
            f"🔁 No new results for {n} turns (since turn {g.since_turn}) "
            "— the model may be going in circles."
        )
        lines = []
        if calls:
            lines.append("Repeated calls:")
            lines.extend(f"  {c}" for c in calls)
        lines.append(
            "Continue lets the run go on — a comment is sent to the model as a hint. "
            "Stop ends the run."
        )
        _cid, decision = confirm.ask_person(
            title,
            "\n".join(lines),
            (
                confirm.Choice(confirm.APPROVE, "Continue"),
                confirm.Choice(confirm.DENY, "Stop"),
            ),
            owner=self.cfg.kind.ports.owner,
            stopped=self.state.stop_event.is_set,
        )
        if decision.action != confirm.APPROVE:
            self.state.stop_note = (
                f"Stopped by the user after {n} turns without new results."
            )
            self.state.stop_event.set()
            self._show_stale(asking=False)
            return
        g.acknowledge()
        self._show_stale(asking=False)
        hint = decision.note
        enqueue = getattr(self.cfg.kind.ports, "enqueue_user_message", None)
        if hint and enqueue is not None:
            # 사람이 입력창에 친 메시지와 같은 길 — 다음 턴 경계가 흡수해 `u-N` 이 된다. 답한 사람의 별명으로
            # (``user:<별명>`` → 별명, 터미널의 ``user`` → 별명 없음)
            author = decision.by.partition(":")[2]
            enqueue(hint, author or STALE_HINT_AUTHOR)

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
            if self._stopped_before(turn.ops[0]):
                return _CONTINUE  # 런 루프가 다음 경계에서 interrupt 로 끝낸다
            return self._dispatch_op(llm_text, turn, turn.ops[0], outcome)

        results: list[dict] = []
        ops = turn.ops
        i = 0
        while i < len(ops):
            op = ops[i]
            if self._stopped_before(op):
                self._note_not_run_after_stop(ops[i:], len(ops), i, results)
                break
            tool = TOOLS.get(op.action) if op.action else None
            # Turn-ending actions (complete/run_skill): flush accumulated
            # results BEFORE the branch runs so its observation lands after
            # the work done so far (chronological order for the model).
            # ``Tool.terminal`` 속성 파생 (T3 선언화 — 종전 도구명 튜플).
            if tool is not None and tool.terminal:
                self._note_not_run_after_terminal(
                    op.action, ops[i + 1 :], len(ops), i, results
                )
                self._flush_op_results(turn.record, results)
                return self._dispatch_op(llm_text, turn, op, outcome)
            # Ops run sequentially, in order — the correctness guarantee for
            # side-effecting tools (write→edit same file, mkdir→touch). (The
            # concurrent `agent` run batch went with v10.141.0: a run returns at
            # once and its agent works on its own thread.)
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
                        f"Not executed — aborted after op {i + 1} failed: {names}. "
                        "Re-send these; do not re-send the ops that already ran.",
                        False,
                    )
                self._flush_op_results(turn.record, results)
                return r
            i += 1
        self._flush_op_results(turn.record, results)
        return _CONTINUE

    def _stopped_before(self, op) -> bool:
        """이 op 를 실행하기 직전에 런이 멈췄는가 — main·상주·run 공통 규칙: **멈춘 뒤엔 도구를
        실행하지 않는다**. 응답을 받는 사이에 선 정지(웹 ■·Ctrl+C·호출자의 정지)는 스트리밍
        도중이 아니면 응답이 끝난 뒤에야 보이므로 여기서 본다. ``complete`` 는 아무것도
        실행하지 않으므로 막지 않는다(이미 쓴 답을 버리지 않는다). 읽기만 한다."""
        if op.action == "complete":
            return False
        stop = self.state.stop_event
        return bool(stop is not None and stop.is_set())

    def _note_not_run_after_terminal(
        self, name: str, skipped: list, total: int, at: int, results: list[dict]
    ) -> None:
        """턴을 끝내는 호출 뒤의 op 은 실행하지 않았다고 결과에 한 줄 남긴다 (감사 2-4) —
        문법은 종결 호출을 끝에만 허용하지만 문법 없는 서버·native_fc 에는 그 막음이 없다.
        종전엔 말없이 버려져 모델은 그 결과를 기다렸다."""
        if not skipped:
            return
        names = ", ".join(
            f"[{at + 2 + k}/{total}] {o.action or '?'}" for k, o in enumerate(skipped)
        )
        self.tools.accumulate_raw(
            results,
            "batch",
            f"Not executed — `{name}` ends the turn, so the calls after it did not "
            f"run: {names}. Send them in a later turn if you still need them.",
            False,
        )

    def _note_not_run_after_stop(
        self, skipped: list, total: int, first: int, results: list[dict]
    ) -> None:
        """여러 op 중 멈춘 뒤의 것들은 실행하지 않았다고 결과에 한 줄 남긴다 — 앞서 실행한
        것이 있을 때만(없으면 관찰 자체가 없다). 다음은 interrupt 기록이다."""
        if not results:
            return
        names = ", ".join(
            f"[{first + 1 + k}/{total}] {o.action or '?'}"
            for k, o in enumerate(skipped)
        )
        self.tools.accumulate_raw(
            results,
            "batch",
            f"Not executed — the user stopped the run: {names}.",
            False,
        )

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
        tail: str = "",
    ):
        """이 op 을 실행하지 않는다 — 입력 검증 실패의 한 길 (v10.49.0 C3).

        **묶음 안이면** 실패 op 로 적고 다음 op 로 간다(v9.21.0 — 뒤의 op 이
        조용히 버려지던 kdsd0j 의 수리). **홀로면** 형식 개입: 원문을 인용하고
        재시도는 저장하지 않으며 턴을 세지 않는다(인용이 유일한 사본, v9.23.2).
        이 차이는 의도다 — 홀로 망가진 호출은 되묻는 게 싸고, 묶음의 나머지는
        이미 실행됐을 수 있다. A4(모르는 도구)·A5(스키마)·인자 JSON 오류가 쓴다."""
        outcome["failure_signal"] = signal
        if accumulate is not None:
            # ``tail``(A5 자동 전문, v10.109.0)은 "이 op 는 안 돌았다" 뒤에 — 거절 사실이
            # 한 덩어리로 먼저 오고 참고 글이 뒤따른다.
            self.tools.accumulate_raw(
                accumulate,
                tool_name,
                f"{batch_note}\n{BATCH_OP_SKIPPED_NOTE}{tail}",
                False,
            )
            return None
        return self._intervene(
            llm_text,
            alone + tail,
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

    def _dispatch_edit_batch(self, llm_text, turn, batch_ops, outcome, *, accumulate):
        """Apply a run of ≥2 consecutive same-path edit_file ops as ONE batch,
        appending ONE combined result to *accumulate*.

        Goes through ``ToolBridge.dispatch_edit_batch`` (P0-2) so the batch
        carries the SAME hook/history contract as the single-op path
        (PreToolUse per edit — any block aborts the whole batch, PostToolUse
        once, ``recent_tool_history`` per edit), while keeping the batch
        semantics (one ``apply_edits_batch_parts`` apply → one write, all-or-nothing,
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

        # 7b. wait (v10.65.0) — 깨울 것이 있으면 그 자리에서 기다린다(런을 끝내지 않는다).
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
        # bug worth surfacing). ``raw`` is the ``result`` of ``op.action_
        # input``; we re-derive ``answer`` from the unwrapped value.
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
        # 기다리는 회신 (v10.141.0) — 보낸 요청의 회신이 아직 안 왔다: 같은 예산으로 되돌린다
        awaited = ledger.awaited()
        render_step(
            "final", answer, self.state.turn, requests=answered, hidx=self._next_hidx()
        )
        nag = bool(still_open or debts or awaited) and ledger.spend()
        show_open_items(self.cfg, self.state)  # 정산·예산 소진이 띠에 바로 선다
        if nag:
            # 기록은 `_intervene` 이 한다 — 이 턴의 assistant 레코드(종결 모양)를
            # 관찰과 함께 저장한다. 따로 넣으면 같은 final 이 두 번 들어간다.
            turn.record = self._terminal_record(turn, answer, claimed, still_open)
            return self._nag_open_items(
                llm_text, turn, claimed, still_open, debts, outcome, awaited
            )
        answer = self._with_unanswered_notice(claimed, still_open, answer)
        answer = self._with_awaited_notice(awaited, answer)
        if self.ctx:
            self.ctx.add(self._terminal_record(turn, answer, claimed, still_open))
        return ToolResult(True, output=answer)

    def _op_wait(self, llm_text: str, turn, op, outcome: dict):
        """``wait`` op (v10.65.0, docs/wait/DESIGN.md) — 런을 끝내지 않는다(5c-1).

        깨울 것(`ports.mailbox.wake_sources`)이 있으면: 호출과 관찰을 기록하고(히스토리는 보통의 op + 관찰
        모양 — 새 카드 종류 없이 재생·resume 이 그대로 그린다) **그 자리에서** 깨울 것이 올 때까지 막는다
        (`_wait_in_place`). 깨면 같은 런이 잇는다 — 다음 턴 경계의 흡수(`kind.intake`)가 깨운 것을 넣고, 장부는
        그대로다(같은 런). main·상주·run 에이전트·하니스 작업, 기다리는 것이 회신·작업·모니터·내가 건 질문 무엇이든 같다.
        없으면 물음은 하나다(5d) — 닫지 않은 것이 있나: 갚지 않은 회신(`ledger.debts` — `r-`)과 답하지 않은 사람의
        요청(`ledger.unanswered_human_request_ids` — `u-`, `complete` 의 `answers` 로 닫는 것). 있으면 그 번호를 말하는
        거절 관찰을 남기고 런을 잇는다. 없으면 받아들인다 — 관찰 한 줄을 기록하고 빈 결과의 성공으로 작업 구간을 끝낸다
        (`complete` 로 끝난 것과 같은 끝 — 새 끝 종류가 없다). 통지 항목(`n-`)은 세지 않는다(작업 구간 끝으로 닫힌다).
        이미 도착한 깨우는 봉투가 있으면 먼저 거부한다(v10.66.2): 다음 관찰에 그것이 온다고 말해 준다.

        v10.84.0 (docs/wait/DESIGN.md §7): 깨울 길이 없는 루프(스킬 — 호출자의 턴 안 인라인, `ports.wakeable`
        이 거짓)는 첫 판정에서 거부한다."""

        if not self.cfg.kind.ports.wakeable():
            _append_observation(
                self.state.messages,
                self.ctx,
                self.cfg.dialect,
                turn.record,
                "Observation: wait refused — this loop runs inline inside its caller "
                "and nothing wakes it. What you started (a job or a monitor) belongs "
                "to the caller and reports to it; finish with `complete` and the "
                "caller waits.",
                tool_name="wait",
                success=False,
                turn=self.state.turn,
            )
            return _CONTINUE
        mailbox = self.cfg.kind.ports.mailbox
        arrivals = mailbox.arrivals()
        if arrivals:
            _append_observation(
                self.state.messages,
                self.ctx,
                self.cfg.dialect,
                turn.record,
                f"Observation: wait refused — {arrived_refusal(arrivals)}",
                tool_name="wait",
                success=False,
                turn=self.state.turn,
            )
            return _CONTINUE
        # 무엇을 기다리기로 했나 — 받아들일 때 한 번 읽는다(5c-1 4b): 수락 문장·대기 줄·체크인 대상이 이 값 하나에서.
        # 기다림 표시를 먼저 세운다 — 거절이면 내린다
        on = mailbox.accept_wait()
        if on is None:
            parked = mailbox.parked_debtors()
            ledger = self.state.ledger
            owed = [str(d.get("id")) for d in ledger.debts()]
            unanswered = ledger.unanswered_human_request_ids()
            if not parked and not owed and not unanswered:
                # 깨울 것도 닫지 않은 것도 없다 — 받는다. 이 작업 구간은 여기서 끝난다(결과 글도 되묻기도 없이)
                _append_observation(
                    self.state.messages,
                    self.ctx,
                    self.cfg.dialect,
                    turn.record,
                    f"Observation: {NOTHING_TO_WAIT_FOR}",
                    tool_name="wait",
                    success=True,
                    turn=self.state.turn,
                )
                if self.cfg.kind.renders():
                    render_run_ended(
                        RUN_ENDED_NOTHING_TO_WAIT_FOR, self.state.turn, failed=False
                    )
                return ToolResult(True, output="")
            why = (
                mutual_wait_refusal(parked, mailbox.debts_to(parked))
                if parked
                else open_items_refusal(owed, unanswered)
            )
            _append_observation(
                self.state.messages,
                self.ctx,
                self.cfg.dialect,
                turn.record,
                f"Observation: wait refused — {why}",
                tool_name="wait",
                success=False,
                turn=self.state.turn,
            )
            return _CONTINUE
        # 받아들인 뒤 — 기다림 표시(`accept_wait` 가 세움)·체크인·대기 줄은 이 ``finally`` 하나가 내린다(5e1): 수락 문장
        # 기록·`PreWait`·대기 줄 그리기·`arm_checkin`·기다림 어디서 예외가 나도 남지 않는다(종전엔 기다림만 감싸, 그 앞의
        # 예외가 표시를 남겨 그를 기다리는 쪽의 `wait` 가 거짓으로 거절됐다 — 오라클 s42)
        # 모듈로 부른다 — 테스트가 `render.get_renderer` 를 갈아 끼운다
        renderer = render_surface.get_renderer()
        try:
            # 중간 깨우기 (v10.79.0, docs/wait/DESIGN.md §6): 타이머는 제자리 대기가 걸지만(`_wait_in_place` →
            # `mailbox.arm_checkin`), 이번 간격은 여기서 같은 계산(`next_checkin_s`)으로 미리 적는다 — 설명엔 숫자가
            # 없다(간격이 바뀐다). 예약만 기다려 타이머가 없으면 종전 문장 그대로.
            checkin_s = mailbox.next_checkin_s(on.checkin_ids)
            timer = (
                f", or in {format_duration(checkin_s)} if nothing has reported"
                if checkin_s
                else ""
            )
            obs = (
                f"Observation: waiting — will wake on: {'; '.join(on.sources)}{timer}."
            )
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
            # 받아들여진 `wait` 에서만 — 기다리기 직전(내장 스냅샷 훅이 "before wait (turn N)" 을 찍는다, 5c-1 결정 K).
            # 결과를 읽지 않는다: 훅이 `wait` 를 막거나 바꿀 수 없다
            self.tools.fire_hook(PRE_WAIT)
            why = self._wait_in_place(renderer, mailbox, on)
        finally:
            mailbox.cancel_checkin()  # 체크인 해제 — 다음 wait 가 다시 건다(걸리지 않았으면 할 일 없음)
            mailbox.clear_waiting()
            renderer.wait_in_place(False, [])
        return self._woke(turn, mailbox, why)

    def _wait_in_place(self, renderer, mailbox, on) -> str:
        """제자리 `wait` 의 기다림 — 받아들여진 `wait` 는 모두 여기다(5c-1; 종전 v10.84.0 private, v10.141.0 회신을
        기다리는 main): 대기 줄을 그리고 체크인 타이머를 건 뒤 메일(회신·작업 통지·모니터·체크인)이 오거나, main 이면
        사람 입력이 도착할 때까지 막는다 — 같은 런이라 감사 범위도 그 런 하나다. 깬 이유를 돌려준다. 내리는 것(표시·
        체크인·대기 줄)은 부르는 쪽(`_op_wait`)의 ``finally`` 하나가 한다. 레지스트리가 없으면 기다릴 메일함이 없다 —
        깨울 것이 사라진 것과 같이 다룬다.

        기다리는 동안 이 스레드가 끊기면(CLI Ctrl+C) 런의 정지를 세우고 그대로 던진다 — 이
        런에 묶인 run 에이전트(`LinkedStop`)가 같이 멈춘다(v10.140.0 의 막힌 기다림과 같다)."""
        # 대기 줄(결정 C)과 체크인 대상은 받아들일 때 읽은 ``on``(`WaitingOn`)에서 — 다시 읽지 않는다(4b). 회신만이면
        # 종전 그대로, 작업·모니터·예약은 짧은 이름으로 덧붙인다(비면 넘기지 않는다)
        awaited = list(on.awaited)
        if on.others:
            renderer.wait_in_place(True, awaited, others=on.others)
        else:
            renderer.wait_in_place(True, awaited)
        mailbox.arm_checkin(on.checkin_ids)
        try:
            return mailbox.wait(
                stop_event=self.state.stop_event,
                still_waiting=lambda: bool(mailbox.wake_sources()),
            )
        except BaseException:
            if self.state.stop_event is not None:
                self.state.stop_event.set()
            raise

    def _woke(self, turn, mailbox, why: str):
        """제자리 대기에서 깼다 — 깬 이유가 사람의 말(``input``)이나 깨울 것의 사라짐(``nothing``)이면 그 관찰을 남기고, 새
        구간을 연다(`_begin_segment`). 같은 런을 잇는다."""
        if why == "input":
            # `wait_input` 이 사람의 말을 보았다 — 그 사이 취소돼 상자에 없어도 사람이 쓴 것은 사실이다
            kinds = mailbox.arrivals() or (USER,)
            left = mailbox.awaited()
            ids = ", ".join(str(r.get("id")) for r in left)
            still = (
                f" — {ids} still awaited, so `wait` again when you are done"
                if left
                else ""
            )
            _append_observation(
                self.state.messages,
                self.ctx,
                self.cfg.dialect,
                turn.record,
                f"Observation: {wait_interrupted(kinds, still)}",
                tool_name="wait",
                success=True,
                turn=self.state.turn,
            )
        if why == "nothing":
            _append_observation(
                self.state.messages,
                self.ctx,
                self.cfg.dialect,
                turn.record,
                "Observation: nothing you were waiting on is running any more.",
                tool_name="wait",
                success=True,
                turn=self.state.turn,
            )
        self._begin_segment()  # 깼다 — 새 구간(정지면 곧 루프 맨 위의 정지 판정이 끝낸다 — 세션 종료도, 5e1)
        return _CONTINUE

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

    def _nag_open_items(
        self, llm_text: str, turn, claimed, requests, debts, outcome, awaited=()
    ):
        """결과는 이미 나갔다 — 남은 항목을 닫는 순서대로 보여 주고 런을 잇는다.

        **붙잡기가 아니다.** 결과를 인질로 잡지 않는다(agent-ask 3판에서 틀린 자리)
        — 답은 이미 렌더되고 기록에 들어갔다. 아직 닫히지 않은 항목이 있어 런을
        닫지 않을 뿐이다. 남은 예산을 적는다: 다 쓰면 하니스가 닫는다."""
        ledger = self.state.ledger
        n = len(requests) + len(debts) + len(awaited)
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
            "replies get your run summary.)"
            if left > 0
            else "(Last reminder — after this the harness closes the rest.)"
        )
        # 기다리려고 complete 를 썼다면 (v10.65.0) — 옛 습관을 한 번에 교정한다.
        waiting_hint = (
            " If you are only waiting for something you started to report back, "
            "call `wait` instead — it keeps these items open and the report wakes you."
            if self.cfg.kind.ports.mailbox.wake_sources()
            else ""
        )
        return self._intervene(
            llm_text,
            (
                f"Observation: your result was delivered, but {why}:\n"
                f"{ledger.items_lines(requests, debts, awaited)}\n"
                "Close them in that order — by action first, then `complete` again "
                "with `answers` for the items it covers." + waiting_hint + " " + budget
            ),
            "open items remain",
            outcome,
            record=turn.record,
            tool_name="complete",
        )

    def _with_awaited_notice(self, awaited, answer: str) -> str:
        """예산이 끝난 자리 — 보낸 요청 중 회신이 아직 안 온 것을 최종 답 말미에 (v10.141.0).
        하니스가 닫는다: 회신은 그래도 오고(받은 쪽은 계속 일한다), 오면 새 메일로 깨운다."""
        if not awaited:
            return answer
        n = len(awaited)
        return (
            f"{answer}\n\n⏳ {n} repl{'ies' if n != 1 else 'y'} you asked for had not "
            f"arrived when this run closed (they still come as mail):\n"
            f"{self.state.ledger.awaited_for_notice(awaited)}"
        )

    def _with_unanswered_notice(self, claimed, requests, answer: str) -> str:
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
            # 답이 올 수 없으면 묻지 않는다 — 갈래(막음/올림) 앞 한 곳(6c)
            if not render_surface.get_renderer().can_prompt():
                return self._emit_obs(
                    turn.record, accumulate, "ask", ASK_REFUSED_NOBODY_CAN_ANSWER, False
                )
            # 누가 묻든 한 길(6b) — 질문 포트가 질문을 올리고 바로 돌아온다. `ask` 는 포트가 있는 루프에만 붙는다
            # (`AskTool.requires_handler`)
            return self._op_ask_async(
                turn.record, questions, self.cfg.kind.ports.questions, accumulate
            )

        return _NOT_HANDLED

    def _op_message(self, llm_text: str, turn, op, accumulate):
        """``message`` op (v5.11) — 상주 에이전트끼리의 비동기 메시징.

        ``message_handler`` 로 라우팅하고 즉시 관찰(배달 확인)을 돌려준다 —
        회신은 나중에 새 메시지로 도착한다(발신자 비블록). 핸들러가 없으면
        (main/일회성 루프) ``_NOT_HANDLED`` 로 폴스루 — 일반 도구 경로가
        "message intercepted by loop" 플레이스홀더를 실행해 무해하게 끝난다.
        """
        handler = self.cfg.kind.ports.message_handler
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
        # 핸들러는 ``(receipt, err)`` 를 돌려준다(v10.145.2) — 보냈을 때만 성공으로 적는다. 관찰 글은 영수증 또는
        # 사유 그대로다.
        try:
            receipt, err = handler(to, text, expects_reply=expects_reply)
        except Exception as e:
            receipt, err = "", f"message failed: {type(e).__name__}: {e}"
        obs = f"[message → {to or '?'}] {err or receipt}"
        return self._emit_obs(turn.record, accumulate, "message", obs, not err)

    def _op_ask_async(self, record: dict, questions, port, accumulate):
        """상주 에이전트의 ``ask`` — **막지 않는다** (DESIGN.md §3.1).

        질문을 사람의 트레이에 올리고 즉시 관찰을 돌려준다. 답은 나중에 새
        메시지로 온다 — `wait` 로 기다리면 그 답이 같은 런을 깨우고(6a), 런이 끝났으면 새 런을 연다. 어느 쪽이든
        이 에이전트의 ctx 는 그대로라 하던 일을 잇는다. 주소는 언제나 사람이다(v10.77.0, B) — 종전의 ``to`` 는 지웠다.
        """
        lines = []
        refused = 0
        for q in questions:
            qid, err = port.ask(q)
            if err:
                refused += 1
                lines.append(f"could not ask: {err}")
            else:
                lines.append(f'question "{qid}" sent — "{q}"')
        # 관찰은 일어난 일만 — 묻고 나서 어디로 가나는 질문이 열린 동안 꼬리의 ``## Your open questions`` 가 말한다(6a)
        obs = "\n".join(lines)
        # 질문이 전부 올라갔을 때만 성공으로 적는다 — ``_op_message`` 와 같다. 관찰 글은 그대로다.
        return self._emit_obs(record, accumulate, "ask", obs, not refused)

    def _op_reply(self, llm_text: str, turn, op, accumulate):
        """``reply`` op (v9.21.0) — 이 런을 시킨 쪽에게 빚진 회신을 갚는다.

        ``_op_message`` 와 동형. 포트가 없으면 ``_NOT_HANDLED``.
        """
        port = self.cfg.kind.ports.questions
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
            err = port.reply(text, id=str(args.get("id", "") or ""))
        except Exception as e:
            err = f"reply failed: {type(e).__name__}: {e}"
        if err:
            err = wrong_closer_hint(str(args.get("id", "") or ""), "reply") or err
        ok = not err
        obs = "[reply] " + (err or "delivered to whoever was waiting on it")
        return self._emit_obs(turn.record, accumulate, "reply", obs, ok)

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
        # 돌고 있는 이 루프가 호출자다 (v10.87.0 `Caller`): 스킬은 이 루프의 레지스트리를
        # 상속해 spawn 할 수 있고(orchestrate 류의 존재 이유), 서브에이전트 루프엔
        # 레지스트리가 없어 run-only 로 남는다 — `SkillLoop` 참조.
        skill_tool_result = _handle_run_skill(
            skill_input, Caller.from_loop(self.cfg, self.state, self.ctx)
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
        """일반 도구 op — truncation 가드→B1/A4/A5→실행→관찰 조립
        (단독이면 자체 append, N-op 이면 accumulate)."""
        tool_name = op.action
        tool_input = op.action_input or {}

        # Truncation guard: if JSON was repaired (truncated response),
        # strip the last element from edit_file's lines arrays
        truncation_warning = ""
        if op.truncated and tool_name == "edit_file":
            tool_input, truncation_warning = _sanitize_truncated_edit(tool_input)

        # B1 (action loop) detection — observe BEFORE dispatch so a
        # repeated call doesn't pay the cost of the redundant tool
        # run. Counter resets after a tool error so legitimate
        # retries don't false-positive, and (v10.107.0) while the same
        # call's results are still changing (``_progressed``).
        prev_was_error = bool(
            self.tools.recent_tool_history
            and self.tools.recent_tool_history[-1].get("tool") == tool_name
            and self.tools.recent_tool_history[-1].get("success") is False
        )
        loop_level = self.loop_detector.observe(
            tool_name,
            tool_input,
            prev_was_error=prev_was_error,
            progressed=self._progressed(tool_name, tool_input),
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
                    repeat_count=self.loop_detector.run_length,
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
                    f"skill_name={self.cfg.kind.skill_name}"
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
                    f"{intervention.message}\n{BATCH_LOOP_SKIPPED_NOTE}",
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

        # Render the model's ACTUAL emission (`op.action_input`) — the same op
        # history.jsonl / resume-replay store. The dispatch may have changed
        # `tool_input` (the truncation guard above); the card does not show that.
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
            err_msg = f"Unknown tool `{tool_name}`. Available: {avail}"
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
            guide = self._first_rejection_guide(tool_name)
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
                tail=guide,
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

    def _op_bad_arguments(self, llm_text: str, turn, op, outcome: dict, accumulate):
        """호출 인자가 JSON 이 아닌 op (v10.20.0) — 실행 없이 어디서 깨졌는지
        알린다. 배치 안이면 그 op 의 자리에 실패 메모로, 홀로면 형식 개입으로."""
        tool_name = op.action or "?"
        raw = str(op.action_input)
        where = describe_json_error(raw) or "not a JSON object"
        err_msg = (
            f"The arguments of your `{tool_name}` call were not valid JSON "
            f"({where}), so the call was not executed. Re-send it with the "
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


# Fields a model might use to wrap a question's text inside a dict —
# e.g. `questions=[{"question": "..."}]` drift observed with qwen3.6
# in S25FE-kernel session 1776954600. Checked in priority order, so a
# `question` key wins over `text` when both are present.
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


def show_open_items(cfg, state) -> None:
    """장부를 사람 화면에 (v10.119.0, docs/ledger/DESIGN.md §15) — 런 시작·턴 경계·
    `complete` 정산·런 끝에서 부른다. **main 런만**: 사람 요청을 받는 런(입력 큐가
    있는 종류 — `owns_main_view`)의 장부가 사람이 보는 장부다. 상주·
    스킬 루프도 같은 렌더러를 쓰므로 거르지 않으면 main 의 띠를 덮어쓴다. 같은
    스냅샷이 연달아 와도 렌더러가 거른다. 모델 입력과 무관하다."""
    if not cfg.kind.owns_main_view:
        return
    from agent_cli.render import get_renderer

    get_renderer().open_items(state.ledger.snapshot(state.turn))


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
