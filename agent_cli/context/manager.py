"""Context manager with token-budget compaction and history.jsonl persistence.

Stores full conversation in history.jsonl (JSON Lines, append-only).
Maintains an in-memory context cache that fits within a token budget.

Before each LLM call the loop calls ``ensure_within(target)`` (flow 1),
which triggers a *compaction* pass when the cache exceeds the target
(``(context − system − output) × 0.8``, system measured live). The pass
(RFC docs/context-compaction/):

  1. Split cache into system anchor + dynamic.
  2. Evict roughly half of dynamic (oldest, token-based).
  3. LLM summarises the evicted half (via injected compactor callback).
  4. Script extracts touched file paths from the evicted half.
  5. Cache rebuilt as ``[system, summary, file_list, retained dynamic]``.
  6. ``compaction.json`` persisted next to history.jsonl so a future
     ``--resume`` restores the same compacted state without re-reading
     the already-summarised tail of history.

When the LLM summariser fails OR the rebuilt cache is still over budget
(small ``max_context_tokens`` + dominant summary cap edge case), the
``ensure_within`` wrapper falls through to a belt-and-braces FIFO drop
so the cache always returns under budget — no infinite-trigger loop.

Compaction can be disabled entirely (NFR-CC-5) by passing
``compaction_enabled=False`` at construction time, which the
``AgentLoop`` plumbs from its own constructor argument. With compaction
off the manager reverts to the historical plain-FIFO behaviour.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

# C5: 관심사 분리 — record 계약은 records, LLM 표현은 render, 디스크
# I/O primitive 는 store 소유. manager 는 캐시 + 압축 정책만.
from agent_cli import images as _images
from agent_cli.context import store
from agent_cli.context._file_extract import extract_file_paths
from agent_cli.context.records import _classify_record
from agent_cli.context.render import (
    _estimate_message_tokens,
    _message_bytes,
    _to_summary_text,
    render_history_message,
)
from agent_cli.context.token_estimator import estimate_tokens, utf8_len
from agent_cli.dialects import get as _get_dialect
from agent_cli.render import (
    get_renderer,
    render_compaction_progress,
    render_context_view,
)


def _current_scope_id() -> str:
    """The calling thread's innermost renderer scope ("" = main timeline).

    Best effort by design: a history write must never fail because of a
    presentation surface. CLI renderers return "" (they encode nesting as depth
    indentation and need no association), so this is a no-op there."""
    try:
        return get_renderer().current_scope() or ""
    except Exception:
        return ""


# ── Defaults / constants ─────────────────────────────────
DEFAULT_TOKEN_BUDGET = 100_000
# 압축은 캐시가 호출별 목표치(``ensure_within(target)``, 루프가 ``context ×
# compaction_ratio − system − 세션상태`` 로 계산)를 **넘는 순간** 돈다 — 별도의
# 90% 임계는 없다(v9.25.1 에 잔재 상수 제거; 꼬리의 퍼센트가 곧 그 목표치 대비).
_SUMMARY_CHAR_CAP = 8000  # ≈ 2000 tokens at 4 chars/token

# Compaction TARGET ratio: the loop squeezes the cache down to this fraction of
# available headroom before each LLM call (``(context − system − output) ×
# ratio``). Lower = compact earlier/more aggressively (more headroom, less
# context kept); higher = compact later (more context, higher overflow risk).
# Lives on the ContextManager (not a loop constant) so the web UI can adjust it
# live — the loop and the web server share one ctx instance, so a slider write
# is read by the next LLM call with no dirty-flag rebuild. Clamped to
# [_MIN, _MAX] so a stray value can't over-squeeze.
#
# v10.11.0: default and max are 1.0 — "compact when the window is actually
# needed". The loop's request clamp (``max_tokens = window − prompt − margin``)
# shrinks the output budget as the window fills; when a generation hits that
# clamp and ends with ``length``, the loop compacts right there
# (``core._on_output_truncated``, detail ``context_clamp``) and the model
# re-emits with the room back. The preventive target still reserves the
# request's minimum output (``LLMCaller._MIN_REQUEST_OUTPUT_TOKENS`` +
# margin) so a full cache never produces an over-window request. Lower
# ratios keep the old behaviour: compact ahead of need, at a fixed fraction.
DEFAULT_COMPACTION_RATIO = 1.0
COMPACTION_RATIO_MIN = 0.5
COMPACTION_RATIO_MAX = 1.0
COMPACTION_RATIO_STEP = 0.05
STREAM_IDLE_TIMEOUT_MIN_S = 60


def clamp_stream_idle_timeout(seconds: int) -> int:
    """스트림 무진전 한도 clamp (P3, v8.55.0): 0 → 감지 끔, 그 외
    [60, 3600s]."""
    from agent_cli.constants import STREAM_IDLE_TIMEOUT_MAX_S

    s = int(seconds)
    if s <= 0:
        return 0
    return max(STREAM_IDLE_TIMEOUT_MIN_S, min(s, STREAM_IDLE_TIMEOUT_MAX_S))


def clamp_stream_max_attempts(attempts: int) -> int:
    """무진전 재전송 **총** 시도 횟수 clamp (v8.60.0): [1, 10].
    1 = 재전송 없음(첫 전송만). 한도(``clamp_stream_idle_timeout``)와 달리
    0 은 "끔"이 아니다 — 시도 0회는 콜 자체를 안 한다는 뜻이 되므로 1 로
    올린다. 감지를 끄는 축은 한도(0=끔) 쪽 하나뿐이다."""
    from agent_cli.constants import STREAM_MAX_ATTEMPTS_MAX

    return max(1, min(int(attempts), STREAM_MAX_ATTEMPTS_MAX))


def grammar_active(override: bool | None, supports: bool | None) -> bool:
    """문법 제약이 이 콜에 실리는가 — 루프(llm.py)와 웹(칩·status.json·health)이
    **같은 규칙**을 읽는다. 지원으로 **기록된**(models.json `supports_grammar:
    true`) 모델에서만 켜지고, 거기서 세션/env 가 off 로 끌 수 있다. 미지원(False)
    은 물론 미확인(None)에도 어떤 값으로도 켜지지 않는다 — 지원 여부는 모델
    감지 때 판정하는 것이지 켜기 요청이 덮어쓰는 것이 아니다."""
    return supports is True and override is not False


# ── Observation complete-nudge ───────────────────────────────
# get_messages() appends this reminder to the CURRENT observation only (the
# last cache record, if it is a tool result). Applied at feed time and NEVER
# stored — _cache and history.jsonl stay clean, and each get_messages call
# re-derives it, so it does not accumulate across turns or in context.
# Purpose: some models sometimes fail to terminate — they finish the work but
# never emit `complete`, or spin re-checking a sub-agent instead of
# completing-and-waiting. Reminding the model, right after each tool result
# (the point where it decides whether it is done), that `complete` is the way
# to finish OR to park on a pending async reply, steers it to terminate cleanly.
# Names both legitimate reasons to complete: nothing left to do, or blocked
# until a sub-agent replies (a pending reply is delivered + wakes it).
# Unconditional (no flag): measured harmless on Qwen3.6-27B (no premature
# completion — that model completes cleanly regardless, so the benefit shows
# only on models that actually mis-terminate) and never persisted, so there is
# nothing to gate. Observation-side text only → mimicry-safe.
_OBS_COMPLETE_NUDGE = (
    "\n\n(If nothing remains to do, call `complete`. If you can only continue "
    "after something you started reports back — a sub-agent's reply, a job, a "
    "monitor — call `wait`: the report wakes you.)"
)


#: 실측 토큰/바이트 비율의 허용 범위. 토큰 하나는 대개 1바이트 이상이라 1 을
#: 넘기 어렵고(작은 요청은 채팅 템플릿 몫으로 조금 넘는다), 영문은 약 0.25,
#: 한글은 약 0.4~0.6 이다. 범위 밖 실측이 산술을 뒤집지 않게 자른다.
TOKEN_RATIO_MIN = 0.05
TOKEN_RATIO_MAX = 2.0


def _clamp_token_ratio(ratio: float | None) -> float | None:
    if ratio is None or ratio <= 0:
        return None
    return min(max(float(ratio), TOKEN_RATIO_MIN), TOKEN_RATIO_MAX)


def clamp_compaction_ratio(ratio: float) -> float:
    return max(COMPACTION_RATIO_MIN, min(COMPACTION_RATIO_MAX, float(ratio)))


# NOTE: oversized single-output protection lives in the loop now, not here.
# A tool observation larger than ``context_window / 10`` is replaced with a
# narrow-it nudge at the result→observation seam (``AgentLoop._tool_observation``)
# before it ever reaches ``add`` — per-tool via ``Tool.apply_oversized_cap`` /
# ``Tool.render_observation``. ``add`` is therefore pure storage: no message it
# receives can blow past the window. (Replaced the earlier chunked-spill record.)


def compute_token_budget(context_window: int) -> int:
    """Compute the fallback context budget (``max_context_tokens``).

    budget = (context_window * 7) // 10 — 70% of the window.

    v4.46.0 통일: 이전엔 run(``context − max_output − 4000``)과 web
    (``×0.7`` 인라인)이 서로 다른 폴백을 써서 같은 세션을 어느 엔트리로
    여느냐에 따라 resume 복원 창이 달랐다(C4 감사 발견). web 의 보수적
    70% 로 단일화 — chars/4 추정의 CJK 과소평가에 대한 여유이기도 하다.

    NOTE: this is no longer the live compaction threshold. flow 1
    computes the real target per call as ``context × compaction_ratio −
    system(measured) − session_state`` in ``AgentLoop._call_llm`` (v8.53.0
    — 총입력 기준; max_output 예약은 요청-시 클램프로 대체). This value remains
    the ``_evict_fifo`` default target and the budget used to restore the
    cache on resume (before the first call's ``ensure_within`` refines it).
    """
    return max((context_window * 7) // 10, 4000)  # floor: at least 4K tokens


class CompactionError(RuntimeError):
    """Raised when the summariser callback fails (provider error,
    empty/non-string return, etc.). ``ensure_within`` catches this and
    falls back to the belt-and-braces FIFO drop."""


class ContextManager:
    """Conversation history with token-budget compaction + persistence.

    - history.jsonl: append-only JSON Lines, every message ever added.
    - In-memory cache: messages within ``max_context_tokens``, refined by
      preventive compaction before each call (flow 1, ``ensure_within``)
      and reactive ``force_fit`` after a server overflow (flow 2).
    - ``compaction.json``: tracks the recursive summary, accumulated file
      list, and the ``dynamic_start_index`` offset into history so that
      ``--resume`` restores cache without overlapping with already-
      summarised tail.
    """

    def __init__(
        self,
        session_dir: Path,
        max_context_tokens: int = 0,
        *,
        resume: bool = False,
        dialect=None,
        binding=None,
        compaction_enabled: bool = True,
        compaction_ratio: float | None = None,
        token_ratio: float | None = None,
    ):
        # 세션의 모델 바인딩 (v10.43.0, providers/binding): 방언은 여기서 한 번
        # 받고(세션 수명 — 한 ctx 는 한 포맷), 토큰 예산은 `max_context_tokens`
        # 속성이 **매번** 바인딩의 창에서 읽는다 — 모델이 바뀌면 다음 압축 판정부터
        # 새 예산. 명시 ``max_context_tokens`` 는 바인딩 없이 쓰는 쪽(테스트·fork
        # 상속)의 고정값. ``dialect`` 없고 바인딩도 없으면 기본 방언(headless·테스트).
        self.binding = binding
        if dialect is None:
            dialect = binding.dialect if binding is not None else _get_dialect()
        self.dialect = dialect

        self.session_dir = Path(session_dir)
        self._budget_override = max(0, int(max_context_tokens or 0))
        # Live-tunable compaction target (web slider). Clamped on set so the
        # loop's per-call ``× compaction_ratio`` can never disable compaction.
        # None = 미지정 → env(AGENT_CLI_COMPACTION_RATIO) 또는 기본값.
        # 명시값(서브에이전트 상속·테스트)은 그대로 clamp (v8.61.0).
        from agent_cli.constants import (
            DEFAULT_STREAM_IDLE_TIMEOUT_S,
            STREAM_MAX_ATTEMPTS,
        )

        # 노브 기본값 (v10.43.0): env·CLI 는 여기서 읽지 않는다 — `knobs.KNOBS` 가
        # 선언하고 부트가 `apply_all` 로 main ctx 에 적용한다. 서브에이전트 ctx 는
        # 부모의 현재 값을 물려받는다(`subagent.runner.create_subagent_ctx`).
        self.compaction_ratio = (
            DEFAULT_COMPACTION_RATIO
            if compaction_ratio is None
            else clamp_compaction_ratio(compaction_ratio)
        )
        # Per-session thinking/reasoning override. 공유 ctx 라 다음 LLM 콜이 즉시
        # 읽는다. enable_thinking: None|bool · reasoning_effort: None|"low"|…|"off".
        # 기본은 **끔을 명시** (v9.24.8, 사용자 결정): Qwen 계열 템플릿은 스위치가
        # 없으면 사고를 켜고, 라이브 보드(2026-09-27)에서 상주 에이전트가 `</think>`
        # 없이 사고 구간 안에서 같은 도구 호출을 22분 반복했다.
        self.thinking_override: dict = {"enable_thinking": False}
        #: 디코딩 문법 제약 (v9.24.0): None=자동(서버 지원 시 켬) / True / False.
        self.grammar_override: bool | None = None
        #: 세션 샘플링 노브 (v10.69.0, 🎲): ``{"temperature": float, "top_p": float}``
        #: 중 세션이 정한 키만. 비어 있으면(기본) 모델 엔트리 → 서버 기본
        #: (`providers.base.resolve_sampling`). 공유 ctx 라 다음 LLM 콜이 즉시 읽는다.
        self.sampling: dict = {}
        # P3 (v8.55.0): 스트림 무진전(no-token) 한도(초) — 0=끔. 세션 한정.
        self.stream_idle_timeout_s: int = DEFAULT_STREAM_IDLE_TIMEOUT_S
        # v8.60.0: 한도와 한 쌍인 **총** 시도 횟수(첫 전송 포함).
        self.stream_max_attempts: int = STREAM_MAX_ATTEMPTS
        self._cache: list[dict] = []
        # P0-8a: 캐시 레코드별 history.jsonl 서수(index) 병행 리스트 — 캐시와
        # 항상 같은 길이/순서. fold 가 캐시 **중간**을 제거해도(오프셋은 prefix-
        # skip 모델이라 중간 제거를 표현 못 함) front-pop/compact 가 "다음
        # 레코드의 실제 history 위치"로 오프셋을 정확히 전진시키기 위한 정렬
        # 북키핑. 종전 ``+= 1``/``+= len(evict)`` 은 접힌 레코드 수만큼 과소
        # 전진해, fold·evict 교차 후 resume 시 잉여 레코드가 재유입됐다(수렴
        # 테스트로 실증). in-memory 전용(레코드 dict 오염 없음).
        self._cache_hidx: list[int] = []
        self._history_ordinal: int = 0  # 지금까지 add 된(=history 의) 레코드 수
        # 모델 시점 뷰(v10.6.0): 마지막 압축의 (전, 후) 토큰 — resume 땐 None.
        self._last_compaction_tokens: tuple[int, int] | None = None
        self._folded_count: int = 0  # fold 로 캐시에서 뺀 형식 넛지(+실패 원문) 수
        self._cache_tokens: int = 0
        # 서버 실측으로 잰 **토큰/바이트 비율** (v10.18.0). None = 아직 실측이
        # 없다 → chars/4 추정. 호출마다 ``reconcile_actual_tokens`` 가 갱신하고
        # ``token_ratio.json`` 에 남긴다; resume 은 그 값을 그대로 읽어 캐시
        # 크기를 재개 전과 같은 척도로 센다. 새로 만든 서브 컨텍스트는 부모의
        # 비율을 물려받는다(같은 모델·같은 토크나이저).
        #
        # 종전 계수는 ``실측 ÷ 추정`` 이었다. 실측에는 매턴 꼬리 같은 고정
        # 덩어리가 들어 있어, 대화가 짧으면 계수가 상한(8배)까지 부풀었다 —
        # 다음 메시지 하나가 8배로 세어져 압축이 일찍 걸린다. 바이트당 비율은
        # 분모가 보낸 요청 전체라 대화 길이에 흔들리지 않는다.
        self._token_ratio: float | None = _clamp_token_ratio(token_ratio)
        self._token_ratio_samples: int = 0
        # Rendered (natural-language) mirror of the cache's dynamic slice —
        # everything after the optional leading system message, in order.
        # ``get_messages`` used to re-run ``_to_natural_language`` over the
        # WHOLE cache on every call (3-4×/turn from the loop) — O(n) per turn,
        # O(n²) per session. Now each record is rendered ONCE: ``add`` appends
        # its rendered form here, and every bulk cache mutation (compaction
        # rebuild, FIFO pops, resume restore) sets this to ``None`` (dirty) so
        # the next ``get_messages`` re-renders from scratch. A length check in
        # ``get_messages`` is the belt-and-braces backstop for any missed
        # mutation site. VALIDITY ASSUMPTION: the render is a pure function of
        # the record (true today — ``_context_view`` is per-record identity);
        # a future TURN-DEPENDENT context view must invalidate this cache.
        self._nl_cache: list[dict] | None = []
        self._history_path = self.session_dir / "history.jsonl"
        self._compaction_path = self.session_dir / "compaction.json"
        # Current LLM turn — stamped onto each history.jsonl record's retrieval
        # `turn` field so history can range/group by turn. The loop sets
        # it at each turn boundary; 0 covers the run-starting query.
        self._current_turn: int = 0

        # Volatile session-state block, re-set by the loop before each call and
        # appended to the last message at feed time. Storage only — this object
        # never builds it (it has no turn counter or agent registry) and never
        # persists it.
        self._session_state: str = ""

        # Compaction state. ``_summary`` empty until first compaction
        # completes. ``_dynamic_start_index`` tracks how many history
        # entries have been compacted away — resume reads ``history
        # [index:]`` to avoid replaying the summarised prefix.
        self._summary: str = ""
        self._file_list: list[str] = []
        self._compaction_count: int = 0
        self._last_compacted_at: str = ""
        self._dynamic_start_index: int = 0
        # 미답 사용자 요청의 id 를 돌려주는 콜백 (v10.41.0) — 그 요청의 user
        # 레코드는 압축·FIFO 에서 **원문 그대로** 남긴다. 루프가 등록한다
        # (`LoopState.ledger.request_ids`); 없으면 핀도 없다.
        self._open_requests: Callable[[], Iterable[str]] | None = None

        # Compaction can be disabled via the constructor flag, which the
        # ``AgentLoop`` plumbs from its own constructor argument.
        self._compaction_enabled = compaction_enabled

        # Injected by AgentLoop after construction. ``_compactor_callback``
        # = function ``(messages) -> summary text``; ``_recorder`` =
        # TurnRecorder (or None for headless paths). Both stay optional
        # so unit tests can drive ContextManager without a full loop.
        self._compactor_callback: Callable[[list[dict]], str] | None = None
        self._recorder = None

        self.session_dir.mkdir(parents=True, exist_ok=True)

        self._token_ratio_path = self.session_dir / "token_ratio.json"
        if resume:
            saved = store.load_token_ratio(self._token_ratio_path)
            if saved is not None:  # 자기 실측이 물려받은 값보다 우선
                self._token_ratio = _clamp_token_ratio(saved[0])
                self._token_ratio_samples = saved[1]
            self._load_compaction_json()
            if self._history_path.is_file():
                self._restore_cache()

    # ── Public API ────────────────────────────────────

    def set_compactor(self, callback: Callable[[list[dict]], str]) -> None:
        """Register the LLM summariser callback. AgentLoop wires its
        ``_llm_compact_summarize`` here at setup."""
        self._compactor_callback = callback

    def set_recorder(self, recorder) -> None:
        """Register a TurnRecorder instance for compaction event logging
        (NFR-CC-6). May be ``None`` (headless / no session)."""
        self._recorder = recorder

    def park_requests(self, requests: list[dict]) -> None:
        """``wait`` 가 열린 요청을 다음 런으로 넘긴다 (v10.65.0, docs/wait/DESIGN.md).

        장부는 런 수명이라 이월이 없다 — 그래서 깨우기 런이 같은 요청을 들고
        시작하려면 세션 수명인 여기에 둔다. 메모리뿐이다: 프로세스가 죽으면
        resume 런은 빈 장부로 시작한다(그 요청의 원문은 history 에 있다)."""
        self._parked_requests = [dict(r) for r in requests]

    def take_parked_requests(self) -> list[dict]:
        """다음 런이 시작하며 가져간다 — 한 번만."""
        parked = getattr(self, "_parked_requests", [])
        self._parked_requests = []
        return parked

    def set_open_requests(self, callback: Callable[[], Iterable[str]]) -> None:
        """미답 사용자 요청 id 를 돌려주는 콜백 등록 (v10.41.0). 압축·FIFO 는
        호출 시점에 평가해, 그 id 의 ``request_id`` 를 단 user 레코드를 자르지
        않는다 — 요청 본문은 history 에만 있고 꼬리 ``## Open Requests`` 는 앞
        120자뿐이라, 요약기에 맡기면 오래 미답인 요청이 흐려졌다. 답하고 나면
        목록에서 빠지니 다음 압축 때 자연히 잘린다."""
        self._open_requests = callback

    def _pinned_indices(self) -> set[int]:
        """지금 자르면 안 되는 캐시 인덱스 — 미답 요청의 user 레코드.

        요약 상한(``_SUMMARY_CHAR_CAP``)보다 큰 요청은 핀하지 않는다: 원문
        보존은 요약보다 쌀 때만 뜻이 있고, 붙여넣은 로그 같은 거대 요청을
        영구히 들고 있으면 나머지 작업 맥락이 그만큼 밀려난다."""
        if self._open_requests is None:
            return set()
        ids = {str(i) for i in self._open_requests()}
        if not ids:
            return set()
        cap = _SUMMARY_CHAR_CAP // 4
        return {
            i
            for i, m in enumerate(self._cache)
            if m.get("role") == "user"
            and str(m.get("request_id", "")) in ids
            and _estimate_message_tokens(m) <= cap
        }

    def add(self, message: dict) -> dict:
        """Add a message to cache and persist to history.jsonl.

        Compaction is NOT triggered here. Preventive compaction (flow 1)
        runs once per turn — right before the LLM call — via
        ``ensure_within``. The loop knows the live system-prompt size and
        the model's context window at that point, so the threshold
        reflects actual headroom ``(context − system − output) × 0.8``
        instead of a fixed 90%-of-budget estimate against a 4000-token
        system reserve. See docs/ARCHITECTURE.md §compaction.

        Pure storage: oversized tool outputs are already nudge-capped at the
        loop's result→observation seam, so no message reaching here can blow
        past the window and break compaction.
        """
        msg_tokens = self._scaled_tokens(message)  # 실측 앵커와 같은 척도
        self._cache.append(message)
        self._cache_hidx.append(self._history_ordinal)  # P0-8a: history 정렬
        self._history_ordinal += 1
        # Incremental render: keep the natural-language mirror in step so
        # ``get_messages`` never re-renders old records. A leading system
        # message is excluded (``get_messages`` passes it through verbatim).
        if self._nl_cache is not None and not (
            len(self._cache) == 1 and message.get("role") == "system"
        ):
            self._nl_cache.append(self._render_record(len(self._cache) - 1))
        self._cache_tokens += msg_tokens
        self._append_to_history(message)
        # Return the stored message so callers can render exactly what was
        # stored (live card == ctx == resume).
        return message

    def cache_ordinals(self) -> list[int]:
        """캐시 레코드들의 history 서수 (``get_raw_messages()`` 와 같은 순서) —
        resume 재생이 카드에 ``hidx`` 를 싣는 데 쓴다 (v10.6.0)."""
        return list(self._cache_hidx)

    @property
    def next_ordinal(self) -> int:
        """다음 ``add`` 가 받을 history 서수 — 레코드보다 먼저 그려지는 카드
        (assistant·final·사용자 에코)가 자기 레코드를 가리키는 데 쓴다 (v10.6.0)."""
        return self._history_ordinal

    @property
    def last_ordinal(self) -> int | None:
        """마지막으로 ``add`` 된 레코드의 history 서수 (없으면 None) — 레코드
        뒤에 그려지는 카드(관찰)용."""
        return self._history_ordinal - 1 if self._history_ordinal else None

    def set_turn(self, turn: int) -> None:
        """Set the current LLM turn index. The loop calls this at each turn
        boundary so subsequent history records carry the right ``turn``."""
        self._current_turn = turn

    @property
    def current_turn(self) -> int:
        """The last turn index the loop stamped (0 before the first turn).
        The authoritative "how many turns did this loop run" — unlike
        counting history records, it survives compaction (old records
        dropped) and is not skewed by format retries (never recorded)."""
        return self._current_turn

    @property
    def token_ratio(self) -> float | None:
        """실측 토큰/바이트 비율, 없으면 None (서브 컨텍스트가 물려받는다)."""
        return self._token_ratio

    def estimate_text_tokens(self, text: str | None) -> int:
        """캐시 밖 텍스트(시스템 프롬프트·매턴 꼬리)의 토큰 — 캐시와 같은 척도.
        실측 비율이 있으면 바이트 × 비율, 없으면 chars/4."""
        if self._token_ratio is None:
            return estimate_tokens(text)
        return round(utf8_len(text) * self._token_ratio)

    def _scaled_tokens(self, message: dict) -> int:
        """카운터 증감용 레코드 토큰 — 실측 비율이 있으면 바이트 × 비율,
        없으면 chars/4 추정 그대로. 이미지(v10.59.0)는 글이 아니라 바이트
        비율을 타지 않는다 — 픽셀 기준 추정을 따로 더한다."""
        if self._token_ratio is None:
            return _estimate_message_tokens(message)
        return max(
            1, round(_message_bytes(message) * self._token_ratio)
        ) + _images.message_image_tokens(message)

    def _sum_tokens(self, messages) -> int:
        return sum(self._scaled_tokens(m) for m in messages)

    def reconcile_actual_tokens(
        self,
        actual_total_tokens: int,
        *,
        request_bytes: int = 0,
        outside_bytes: int = 0,
        image_tokens: int = 0,
    ) -> None:
        """Re-anchor the cache token count to the server's actual input
        count, and refresh the tokens-per-byte ratio from it.

        ``actual_total_tokens`` is what the provider reported for the last
        call's prompt (``usage.input_tokens`` + cache fields). It covers the
        whole request; ``request_bytes`` is that request's UTF-8 size and
        ``outside_bytes`` the part of it that is not the cache (system prompt,
        function schemas, per-turn tail). ``image_tokens`` (v10.59.0) is the
        pixel-based estimate for the images the request carried — subtracted
        before the ratio so base64 pixels never price the text.

        Ratio: the first measurement is taken as is; after that
        ``new = (previous + actual / request_bytes) / 2``. Consecutive requests
        overlap almost entirely, so the average mostly matters right after a
        compaction or when a server reports an odd count once.

        Counter: the total stays the server's number — the cache gets
        ``actual − outside`` (outside at the refreshed ratio). The ratio only
        prices what is added before the next call, and the whole cache after a
        resume. No-op when the provider reported no usage (cold start /
        provider without usage), leaving the running estimate in place.
        """
        if actual_total_tokens <= 0:
            return
        if request_bytes > 0:
            text_tokens = max(actual_total_tokens - image_tokens, 1)
            measured = _clamp_token_ratio(text_tokens / request_bytes)
            self._token_ratio = (
                measured
                if self._token_ratio is None
                else (self._token_ratio + measured) / 2
            )
            self._token_ratio_samples += 1
            try:
                store.save_token_ratio(
                    self._token_ratio_path, self._token_ratio, self._token_ratio_samples
                )
            except OSError:
                pass  # best-effort: 기록 실패가 턴을 막지 않는다
        outside = (
            round(outside_bytes * self._token_ratio)
            if self._token_ratio is not None
            else outside_bytes // 4
        )
        self._cache_tokens = max(actual_total_tokens - outside, 0)

    def get_messages(self) -> list[dict]:
        """Return cached messages converted to natural language for LLM.

        When a compaction summary exists, two synthesised ``role=user``
        messages get prepended right after the system prompt: the
        recursive summary and the accumulated file list. Wire-format
        plugins handle them as plain text (no format-specific shape).
        """
        result: list[dict] = []
        cache = self._cache

        # Pass through the system prompt first if it's at the head —
        # the synthesised summary / file-list messages slot in
        # immediately after it so the LLM sees ``system → summary →
        # files → dynamic`` in that order. System messages stay
        # verbatim (no dialect conversion); ``_to_natural_language``
        # only handles user / assistant records and would misroute a
        # system entry through the assistant rendering path.
        if cache and cache[0].get("role") == "system":
            result.append(dict(cache[0]))
            rest_start = 1
        else:
            rest_start = 0

        if self._summary:
            result.append(
                {
                    "role": "user",
                    "content": (
                        f"## Summary of earlier conversation\n\n{self._summary}"
                    ),
                }
            )
        if self._file_list:
            listing = "\n".join(f"- {p}" for p in self._file_list)
            result.append(
                {
                    "role": "user",
                    "content": (f"## Files touched in earlier turns\n\n{listing}"),
                }
            )

        # Dynamic slice: served from the incremental render mirror. ``None``
        # (bulk mutation) or a length mismatch (backstop for any missed
        # invalidation) → full re-render once; steady state is a pointer copy.
        n_rest = len(cache) - rest_start
        if self._nl_cache is None or len(self._nl_cache) != n_rest:
            self._nl_cache = [
                self._render_record(i) for i in range(rest_start, len(cache))
            ]
        for group in self._nl_cache:
            result.extend(group)
        # 서버 파싱 방언: 호출마다 결과가 있게 맞춘다 (v10.20.0).
        result = self.dialect.pair_call_results(result)
        # 살아 있는 이미지 수 상한 (v10.59.0): 최근 N 장만 참조를 남긴다 —
        # 기록은 그대로, 자리표시 글은 content 에 남아 있다.
        result = _images.keep_recent(result, _images.image_keep())

        # ── tail annotations (feed time only, never persisted) ──────────
        # Both are appended to the LAST message rather than added as new ones:
        # providers forward ``messages`` verbatim, so an extra trailing user
        # message would create consecutive same-role turns whose handling
        # differs per provider. Copy before mutating so _nl_cache and the
        # stored records stay clean.
        tail = ""
        # (a) complete-nudge — CURRENT observation only (it is about how to end
        # a turn, which only makes sense right after a tool result).
        if cache and cache[-1].get("role") == "user" and cache[-1].get("tool"):
            tail += _OBS_COMPLETE_NUDGE
        # (b) session state — every turn, whatever the last message is (a fresh
        # user request is exactly when the memory index and the agent roster
        # matter most). Last, so the volatile state is the true tail: strongest
        # recency, and zero KV-prefix cost (see prompts/session_state.py).
        if self._session_state:
            tail += "\n\n" + self._session_state
        if tail and result and result[-1].get("role") == "tool":
            # 서버 파싱 방언: 꼬리는 도구 결과가 아니라 하니스의 말 — `tool` 뒤에
            # user 메시지로 따로 둔다 (OpenAI 규격상 허용; 종전엔 본문 끝에 붙여
            # 도구가 한 말처럼 보였다).
            result.append({"role": "user", "content": tail.strip()})
        elif tail and result and result[-1].get("role") == "user":
            last = dict(result[-1])
            last["content"] = last.get("content", "") + tail
            result[-1] = last
        return result

    def _render_record(self, i: int) -> list[dict]:
        """캐시 레코드 ``i`` → 요청 메시지 목록 (v10.2.0). 관찰은 바로 앞 assistant
        레코드의 index 로 호출 id 를 짝 맞춘다(서버 파싱 방언).

        짝은 **바로 앞 레코드**가 호출을 가진 assistant 일 때만 성립한다
        (v10.14.0). 형식 거절(모르는 도구·인자 오류)은 거절된 호출을 저장하지
        않으므로 그 관찰 앞에는 짝이 없다 — 종전엔 더 앞의 assistant 를 찾아
        붙여 같은 호출 id 의 `tool` 메시지가 둘이 되거나, 아예 없으면 호출 없는
        `tool` 메시지가 나갔다(OpenAI 규격 위반 — 엄격한 서버는 400). 짝이
        없으면 ``assistant_index=None`` 이고 방언이 user 메시지로 낸다."""
        msg = self._cache[i]
        assistant_index = None
        if i > 0 and self._is_call_result(self._cache[i - 1], msg):
            assistant_index = i - 1
        # 종결 레코드 뒤에 짝 관찰(독촉)이 있으면 그것이 답 — 합성하지 않는다.
        answered = i + 1 < len(self._cache) and self._is_call_result(
            msg, self._cache[i + 1]
        )
        return render_history_message(
            msg,
            self.dialect,
            index=i,
            assistant_index=assistant_index,
            answered=answered,
        )

    def _is_call_result(self, prev: dict, obs: dict) -> bool:
        """``obs`` 가 바로 앞 assistant 레코드 ``prev`` 의 호출 결과인가.

        관찰은 바로 앞 레코드가 호출을 가진 assistant 일 때만 짝이 된다(v10.14.0).
        **종결 호출 뒤에는 그 종결에 대한 관찰만** 짝이다(v10.55.0) — `complete`
        에 붙는 독촉·되묻기(``tool`` = 종결 op 이름). 다음 런의 첫 턴 경계가 배달한
        메일은 결과가 아니다: 깨우기 런이 시작 메시지 없이 열리면서(장부 P2) 메일
        관찰이 직전 런의 `complete` 바로 뒤에 오게 됐고, 서버 파싱 방언은 그것을
        `complete` 의 `tool` 결과로 짝지어 하니스의 종결 결과("Request [1]
        removed…")를 지우고 메일을 `complete` 가 돌려준 것처럼 보였다."""
        if not (obs.get("role") == "user" and obs.get("tool")):
            return False
        ops = prev.get("ops") if prev.get("role") == "assistant" else None
        if not ops:
            return False
        terminal = getattr(
            getattr(self.dialect, "spec", None), "terminal_op", "complete"
        )
        last = ops[-1] if isinstance(ops, list) else None
        if isinstance(last, dict) and last.get("action") == terminal:
            return obs.get("tool") == terminal
        return True

    def set_session_state(self, text: str) -> None:
        """Set the session-state block appended to the last message by
        :meth:`get_messages`. Called by the loop each turn (it owns the turn
        counter / agent registry); ``""`` disables it. Never persisted."""
        self._session_state = text or ""

    def get_raw_messages(self) -> list[dict]:
        """Return cached messages as raw JSON dicts (no conversion)."""
        return list(self._cache)

    def get_estimated_tokens(self) -> int:
        """Current estimated token count of the cache."""
        return self._cache_tokens

    @property
    def history_path(self) -> Path:
        return self._history_path

    @property
    def compaction_count(self) -> int:
        return self._compaction_count

    @property
    def max_context_tokens(self) -> int:
        """컨텍스트 예산 — 명시값 > 바인딩(창의 70%, 모델이 바뀌면 따라감) > 기본."""
        if self._budget_override > 0:
            return self._budget_override
        if self.binding is not None:
            return self.binding.max_context_tokens
        return DEFAULT_TOKEN_BUDGET

    def set_compaction_ratio(self, ratio: float) -> float:
        """Set the live compaction target ratio (web slider). Clamped to
        [_MIN, _MAX]; returns the clamped value actually stored. Read by the
        loop's next LLM call (shared ctx) — no rebuild needed."""
        self.compaction_ratio = clamp_compaction_ratio(ratio)
        return self.compaction_ratio

    def set_stream_idle_timeout(self, seconds: int) -> int:
        """스트림 무진전 한도 설정 (web "Stall"). clamp 결과 반환 — 다음
        LLM 콜부터 즉시 반영(공유 ctx). 0=감지 끔."""
        self.stream_idle_timeout_s = clamp_stream_idle_timeout(seconds)
        return self.stream_idle_timeout_s

    def set_stream_max_attempts(self, attempts: int) -> int:
        """무진전 재전송 총 시도 횟수 설정 (web ⏳ 노브 2번째 입력, v8.60.0).
        clamp 결과 반환 — 다음 LLM 콜부터 즉시 반영(공유 ctx)."""
        self.stream_max_attempts = clamp_stream_max_attempts(attempts)
        return self.stream_max_attempts

    def set_grammar_override(self, enabled: bool | None) -> bool | None:
        """세션 문법-제약 오버라이드(web UI). None=자동, True/False=강제. 다음
        LLM 콜부터 반영(공유 ctx)."""
        self.grammar_override = None if enabled is None else bool(enabled)
        return self.grammar_override

    def set_thinking_override(
        self, enable_thinking=None, reasoning_effort=None
    ) -> dict:
        """세션 thinking 오버라이드 설정(web UI). None=미설정(모델 기본), 값이 있으면
        provider 요청 body 에 반영(다음 LLM 콜 즉시). enable_thinking: bool|None,
        reasoning_effort: "auto"/None|"low"|"medium"|"high"|"off". 저장된 dict 반환."""
        ov: dict = {}
        if enable_thinking is not None:
            ov["enable_thinking"] = bool(enable_thinking)
        if reasoning_effort in ("low", "medium", "high", "off"):
            ov["reasoning_effort"] = reasoning_effort
        self.thinking_override = ov
        return ov

    def set_sampling(self, name: str, value) -> float | None:
        """세션 샘플링 노브 하나(🎲 temperature/top_p)를 정하거나 지운다 (v10.69.0).
        ``None`` = auto(키 삭제 → 모델 엔트리·서버 기본으로). 값은
        `constants.SAMPLING_RANGES` 로 clamp; 저장된 값을 돌려준다. 다음 LLM
        콜부터 반영(공유 ctx)."""
        from agent_cli.constants import SAMPLING_RANGES

        lo, hi = SAMPLING_RANGES[name]
        if value is None:
            self.sampling.pop(name, None)
            return None
        v = max(lo, min(hi, float(value)))
        self.sampling[name] = v
        return v

    @property
    def summary(self) -> str:
        return self._summary

    @property
    def file_list(self) -> list[str]:
        return list(self._file_list)

    # ── Compaction / eviction ────────────────────────

    def ensure_within(self, target_tokens: int) -> None:
        """Preventive compaction (flow 1): bring the cache to/under
        ``target_tokens`` before the next LLM call.

        Two-layer safety:
          1. Attempt ``_compact()`` (LLM summarisation + cache rebuild).
          2. Whether (1) succeeded or raised ``CompactionError``, if the
             cache is *still* above ``target_tokens``, drop oldest with
             plain FIFO until it fits. Same fallback path catches both
             the summariser-failure case and the small-budget-with-large-
             summary edge case in §2.1 of DESIGN.

        The loop computes ``target_tokens`` as ``context_window ×
        compaction_ratio − system − session_state`` (v8.53.0) each call — system measured
        live, so a large tool-laden system prompt correctly reduces the
        room left for messages. Paired with ``reconcile_actual_tokens``
        keeping ``_cache_tokens`` anchored to the server's real count,
        this fires on time even for CJK-heavy content the chars/4
        estimate would under-count.
        """
        if self._cache_tokens <= target_tokens:
            return

        # Compaction disabled (CLI flag or env): skip directly to FIFO.
        if not self._compaction_enabled or self._compactor_callback is None:
            self._evict_fifo(target_tokens)
            return

        try:
            self._compact(target_tokens)
        except CompactionError as e:
            render_compaction_progress(phase="warning", reason=str(e))

        # Belt-and-braces: idempotent — no-op when ``_compact()`` already
        # brought the cache below the target.
        if self._cache_tokens > target_tokens:
            self._evict_fifo(target_tokens)

    def compact_now(self) -> tuple[int, int]:
        """Manual compaction — the ``/compact`` command. Run one
        ``_compact`` pass (summarise the oldest ~half of the dynamic
        cache) and return ``(tokens_before, tokens_after)``.

        No-op (equal values) when compaction is disabled, no compactor
        callback is wired, or there's nothing old enough to evict
        (``_split_for_compaction`` yields an empty set — e.g. only the
        system anchor + one turn). A summariser failure surfaces a
        warning and leaves the cache as-is; unlike ``ensure_within`` we
        don't force a FIFO drop here, because a manual ``/compact`` asks
        to *summarise*, not to silently discard turns.
        """
        before = self._cache_tokens
        if not self._compaction_enabled or self._compactor_callback is None:
            return before, before
        try:
            self._compact()
        except CompactionError as e:
            render_compaction_progress(phase="warning", reason=str(e))
        return before, self._cache_tokens

    def _compact(self, target_tokens: int | None = None) -> None:
        """Execute one compaction pass — split, summarise, extract paths,
        rebuild cache, persist. Raises ``CompactionError`` on summariser
        failure (callback exception or empty/non-string return).

        ``target_tokens`` is the caller's goal — ``ensure_within`` passes
        the live per-call target so the recorded ``fallback_used`` says
        whether its FIFO belt-and-braces will run; ``compact_now`` passes
        nothing (no FIFO follows a manual pass)."""
        anchor, evict_set, retained = self._split_for_compaction()
        if not evict_set:
            return

        old_tokens = self._cache_tokens
        render_compaction_progress(
            phase="start",
            old_tokens=old_tokens,
            evicted_count=len(evict_set),
        )

        t0 = time.monotonic()
        failure_signal: str | None = None
        fallback_used = False
        try:
            # Recursive single-call summarisation: when a prior summary
            # exists, fold it into the input so the LLM produces ONE
            # consolidated summary in a single round-trip rather than
            # two (evict → new summary → merge(prev, new)). Two calls
            # cost twice as much and the intermediate ``new summary``
            # loses prior context (e.g. "user originally asked for X")
            # while it's generated, only to be glued back on merge.
            # Single call with prior context wins on both axes.
            # Render the evicted slice as a single natural-language
            # *transcript* inside one user message — NOT a role-structured
            # replay. assistant turns become prose (via ``_to_summary_text``)
            # with tool args summarised (no file bodies), so the model sees
            # "here is a transcript, summarise it" instead of a ReAct
            # conversation whose next turn is its own — which made a small
            # model continue the task (emit another ``write_file``) instead
            # of summarising. Single user message = no dangling assistant
            # turn to mimic. The callback still receives chat-ready
            # ``{role, content}`` only.
            transcript = "\n".join(_to_summary_text(m) for m in evict_set)
            if self._summary:
                content = (
                    "## Running summary of earlier conversation\n\n"
                    f"{self._summary}\n\n"
                    "Below is a transcript of NEW messages to fold into the "
                    "running summary. Produce one updated summary under the "
                    "same section headings.\n\n"
                    f"{transcript}"
                )
            else:
                content = "Transcript to summarise:\n\n" + transcript
            summarize_input = [{"role": "user", "content": content}]

            new_summary = self._summarize_messages(summarize_input)
            new_paths = extract_file_paths(evict_set)

            # Cap and store.
            self._summary = new_summary[:_SUMMARY_CHAR_CAP]
            self._file_list = self._merge_file_lists(self._file_list, new_paths)
            # P0-8a: hidx 재조립 — 레코드 동일성으로 따라간다. 핀된 요청은
            # evict 구간 **안**에서 retained 로 옮겨지므로 슬라이스로는 안 된다.
            hidx_of = {id(m): h for m, h in zip(self._cache, self._cache_hidx)}
            retained_hidx = [hidx_of[id(m)] for m in retained]
            self._cache_hidx = self._cache_hidx[: len(anchor)] + retained_hidx
            self._cache = anchor + retained
            self._nl_cache = None  # bulk rebuild → re-render on next get
            self._cache_tokens = self._sum_tokens(self._cache)
            self._compaction_count += 1
            self._last_compacted_at = _now_iso()
            # P0-8a: resume 오프셋 = 마지막으로 빠진 레코드 **다음** 캐시 레코드의
            # 실제 history 위치(없으면 지금까지의 전체 서수) — ``+= len(evict_set)``
            # 은 fold 가 만든 공백을 몰라 과소 전진했다. 핀된 요청의 서수는 그
            # 앞이라 경계가 아니다(resume 땐 런이 끝나 핀도 없다).
            last_evicted = max(hidx_of[id(m)] for m in evict_set)
            self._dynamic_start_index = next(
                (h for h in retained_hidx if h > last_evicted), self._history_ordinal
            )
            self._save_compaction_json()
        except CompactionError:
            failure_signal = "summary_failed"
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1000.0
            # Determine ``fallback_used`` AFTER the try-block. If we
            # raised, the caller (ensure_within) will run FIFO so
            # mark fallback now. If we succeeded but the cache is
            # still over the caller's target, ensure_within's
            # belt-and-braces will also run FIFO — same flag. (Until
            # v9.25.1 this compared against 90% of the stale
            # ``max_context_tokens`` budget, not the live target.)
            if failure_signal is not None or (
                target_tokens is not None and self._cache_tokens > target_tokens
            ):
                fallback_used = True

            if self._recorder is not None:
                self._recorder.record_compaction(
                    tokens_before=old_tokens,
                    tokens_after=self._cache_tokens,
                    evicted_count=len(evict_set),
                    fallback_used=fallback_used,
                    failure_signal=failure_signal,
                    duration_ms=duration_ms,
                )

        self._last_compaction_tokens = (old_tokens, self._cache_tokens)
        render_compaction_progress(
            phase="done",
            old_tokens=old_tokens,
            new_tokens=self._cache_tokens,
        )
        self._notify_context_view()

    def _split_for_compaction(
        self,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Partition cache into ``(anchor, evict, retained)``.

        ``anchor`` = system prompt if present (just one message),
        evict-protected. ``evict`` = oldest dynamic, approximately
        half of dynamic by tokens. ``retained`` = the newer remainder
        — the most recent user query lives at its tail.

        First user query is intentionally NOT an anchor (RFC FR-CC-4):
        sessions evolve and the first query loses anchoring value as
        work progresses. Its content is captured in the LLM summary
        anyway — unless the run still owes it an answer (v10.41.0):
        a user record whose ``request_id`` is in the loop's open
        requests (``_pinned_indices``) is skipped by the evict walk and
        placed at the head of ``retained``, verbatim, in its original
        order. It counts toward neither the evict target nor its total.
        """
        anchor: list[dict] = []
        dynamic_start = 0
        if self._cache and self._cache[0].get("role") == "system":
            anchor = [self._cache[0]]
            dynamic_start = 1
        dynamic = self._cache[dynamic_start:]

        if not dynamic:
            return anchor, [], []

        pinned = {
            i - dynamic_start for i in self._pinned_indices() if i >= dynamic_start
        }
        dynamic_tokens = sum(
            _estimate_message_tokens(m)
            for i, m in enumerate(dynamic)
            if i not in pinned
        )
        target_evict = dynamic_tokens // 2

        # Edge case: a single huge dynamic message would otherwise be
        # untouchable (loop never reaches target). Force evict the
        # first message in that case — better to lose context detail
        # than to spin in an infinite compaction loop.
        evict: list[dict] = []
        kept: list[dict] = []
        evicted_tokens = 0
        end = len(dynamic)
        for i, msg in enumerate(dynamic):
            if i in pinned:
                kept.append(msg)
                continue
            evict.append(msg)
            evicted_tokens += _estimate_message_tokens(msg)
            if evicted_tokens >= target_evict:
                end = i + 1
                break

        if not evict:
            return anchor, [], dynamic
        return anchor, evict, kept + dynamic[end:]

    def _summarize_messages(self, messages: list[dict]) -> str:
        """Invoke the registered compactor callback. Raises
        ``CompactionError`` on missing callback, callback exception, or
        empty/non-string return."""
        if not messages:
            return ""
        if self._compactor_callback is None:
            raise CompactionError("no compactor callback registered")
        try:
            summary = self._compactor_callback(messages)
        except Exception as e:
            raise CompactionError(f"summariser raised: {e}") from e
        if not isinstance(summary, str) or not summary.strip():
            raise CompactionError("summariser returned empty/non-string")
        return summary

    @staticmethod
    def _merge_file_lists(prev: list[str], new: list[str]) -> list[str]:
        """Union prev + new preserving insertion order, dedup."""
        seen: set[str] = set(prev)
        result = list(prev)
        for p in new:
            if p not in seen:
                seen.add(p)
                result.append(p)
        return result

    def _evict_fifo(self, target_tokens: int | None = None) -> None:
        """Plain FIFO drop until cache fits within ``target_tokens``. Used
        as the ``compaction_enabled=False`` path, the belt-and-braces
        fallback inside ``ensure_within``, and the FIFO stage of
        ``force_fit``.

        ``target_tokens`` defaults to the full budget
        (``max_context_tokens``) — same semantic as the pre-compaction
        FIFO path. ``force_fit`` passes a smaller target to shed more
        aggressively after a server-side overflow rejection. The
        belt-and-braces case where a rebuilt cache lands in the 90-100%
        band is acceptable: the next ``add`` that pushes it over 90%
        will simply re-enter compaction, and compaction reliably shrinks
        the cache (summary is capped, retained tail is half-by-tokens).
        """
        target = target_tokens if target_tokens is not None else self.max_context_tokens
        dropped = 0
        while self._cache_tokens > target and len(self._cache) > 1:
            self._pop_oldest_evictable()
            dropped += 1
        # Persist updated offset so an interrupted run survives.
        if self._summary or self._compaction_count or self._dynamic_start_index:
            self._save_compaction_json()
        if dropped:
            self._notify_context_view()

    def force_fit(self, target_tokens: int, actual_tokens: int | None = None) -> bool:
        """Aggressively shrink the cache after a context-overflow rejection.

        This is the reactive safety net (flow 2): the server returned a
        400 because the prompt exceeded its context window, so we must
        shed history before retrying. It runs ``compact`` first (least
        information loss), then FIFO-evicts the oldest messages.

        Why a dedicated method instead of trusting ``ensure_within``:
        the local token estimate (``chars/4``) under-counts CJK text
        badly, so ``_cache_tokens`` can sit well below the threshold
        while the *real* prompt is over the limit — compaction never
        fires. Here the server has spoken (the 400 is ground truth), so
        we shrink regardless of what the local estimate claims.

        Sizing is **ratio-based** to neutralise the estimate's
        inaccuracy: if ``actual_tokens`` (server count) and
        ``target_tokens`` are known, we drop the cache to
        ``target/actual`` of its current estimated size. A consistent
        under-count factor cancels out of that ratio, so the real prompt
        lands near the target even though every absolute number is
        wrong. Any residual error is corrected by the caller's bounded
        retry loop, which re-submits and reacts to the next 400.

        Forward progress is guaranteed: when ratio eviction removes
        nothing (estimate too small to clear the target), one oldest
        message is popped outright. The anchor (the single most recent
        message) is always preserved.

        Args:
            target_tokens: desired post-shrink prompt size, typically
                ``limit * 0.8`` (server limit with output headroom).
            actual_tokens: server-reported actual prompt size, when the
                400 message carried it. ``None`` falls back to a fixed
                25% trim per call.

        Returns:
            True if the cache shrank (a retry is worthwhile), False if
            only the anchor remains (caller should give up).
        """
        if len(self._cache) <= 1:
            return False
        before_len = len(self._cache)

        # 1) Compact first — summarise the oldest slice if possible.
        if self._compaction_enabled and self._compactor_callback is not None:
            try:
                self._compact()
            except CompactionError as e:
                render_compaction_progress(phase="warning", reason=str(e))

        # 2) FIFO-evict the remainder if compaction didn't shrink enough.
        if len(self._cache) > 1:
            if actual_tokens and target_tokens and actual_tokens > target_tokens:
                # Ratio-based: shed so the (estimate-invariant) fraction
                # target/actual of the prompt remains.
                keep_ratio = target_tokens / actual_tokens
                floor = max(int(self._cache_tokens * keep_ratio), 1)
            else:
                # No server count: trim ~25% and let the retry converge.
                floor = max(int(self._cache_tokens * 0.75), 1)
            self._evict_fifo(floor)

            # Guarantee forward progress: ratio eviction can be a no-op
            # when the local estimate already sits below ``floor`` (the
            # estimate under-counted), yet the server rejected the
            # prompt. Pop one oldest message so the retry makes headway.
            if len(self._cache) >= before_len and len(self._cache) > 1:
                self._pop_oldest_evictable()
                self._save_compaction_json()
                self._notify_context_view()

        return len(self._cache) < before_len

    def _pop_oldest_evictable(self) -> None:
        """FIFO 한 칸 — 가장 오래된 레코드를 버리되 핀된(미답 요청) 레코드는
        건너뛴다. 핀된 것과 마지막 레코드만 남았으면 핀된 것을 버린다(전진
        보장이 핀보다 우선 — 창에 안 맞는 프롬프트는 구제 불가).
        ``_cache_tokens`` 는 ``_scaled_tokens`` 로 깎고(P0-8b) 오프셋은 버린
        레코드의 실제 history 위치 + 1 로 전진한다(P0-8a — ``+= 1`` 은 fold 가
        만든 중간 공백을 몰라 과소 전진했다)."""
        pinned = self._pinned_indices()
        i = next((k for k in range(len(self._cache) - 1) if k not in pinned), 0)
        removed = self._cache.pop(i)
        self._nl_cache = None  # 캐시 변형 → 렌더 미러 재빌드
        self._cache_tokens = max(self._cache_tokens - self._scaled_tokens(removed), 0)
        self._dynamic_start_index = max(
            self._dynamic_start_index, self._cache_hidx.pop(i) + 1
        )

    # ── Persistence ──────────────────────────────────

    def fold_resolved_interventions(self, *, assume_tail_resolved: bool = False) -> int:
        """해소된 형식-복구 개입 쌍을 dynamic 캐시 **뷰**에서 접는다 (v4.51.0).

        설계(사용자 결정): "dynamic context 에는 성공 궤적만" — 형식 개입은
        교정(다음 파싱 성공)의 일회성 재료라, 소비된 뒤에도 남으면 실패
        shape 의 self-reinforcement 재료가 된다(v3.16.1 mimicry 계열 교훈의
        관찰-측 확장 — 단 이것은 **변형이 아니라 제거**라 모델이 가짜 마커를
        볼 일이 없음). history.jsonl 은 불변(관측·디버그 보존) — 캐시 뷰만.

        - ``assume_tail_resolved=True``: live 경로(파싱 성공 직후 — 성공
          레코드가 아직 캐시에 없으므로 꼬리의 개입도 해소로 간주).
        - False: resume 경로 — 레코드만으로 재판정(개입 뒤 ops-보유
          assistant 존재). live 가 접은 뒤 저장된 순서와 동일 결과 →
          live↔resume 동일 뷰(사이드카 상태 불필요).
        - 연속 실패: 새 형식 개입을 붙이기 직전에도 dispatcher 가 같은 live
          호출을 한다(v10.2.3) — 컨텍스트에는 항상 최신 개입 1개만. resume 은
          "뒤에 더 새로운 개입이 있음" 을 해소로 보아 같은 뷰로 수렴한다.
        반환: 접은 레코드 수.
        """
        from agent_cli.context.records import (
            fold_resolved_intervention_indices,
            is_folding_intervention,
            is_format_intervention,
        )

        if assume_tail_resolved:
            # live 경로(파싱 성공 직후): 성공이 확인된 순간, 캐시의 **모든**
            # 형식 개입이 소비 완료다 — 연속 실패로 쌓인 [실패,개입]×k 전체를
            # 접는다("성공 궤적만" 의미론; 개별 해소-증거 판정 불필요 —
            # 호출 시점 자체가 해소 신호). B1 루프 넛지(v10.32.0)도 같이
            # 접되 그 앞 레코드는 실행된 호출의 관찰이라 넛지만 뺀다.
            idxs = []
            for i, rec in enumerate(self._cache):
                if is_folding_intervention(rec):
                    idxs.append(i)
                    if (
                        is_format_intervention(rec)
                        and i > 0
                        and self._cache[i - 1].get("role") == "assistant"
                    ):
                        idxs.append(i - 1)
            idxs = sorted(set(idxs), reverse=True)
        else:
            # resume 경로: 레코드-기반 재판정(개입 뒤 ops-보유 assistant 존재)
            # — live 가 접은 순서(성공 레코드가 뒤에 남음)와 동일 결과.
            idxs = fold_resolved_intervention_indices(self._cache)
        if not idxs:
            return 0
        for i in idxs:
            removed = self._cache.pop(i)
            self._cache_hidx.pop(i)  # P0-8a: 정렬 유지 (중간 제거 동기화)
            self._cache_tokens = max(
                self._cache_tokens - self._scaled_tokens(removed),
                0,  # P0-8b
            )
        self._nl_cache = None  # 벌크 변형 → 렌더 미러 재빌드
        self._folded_count += len(idxs)
        self._notify_context_view()
        return len(idxs)

    # ── 모델 시점 뷰 (v10.6.0, docs/inspector-model-view §3.1) ─────────

    def context_view(self) -> dict:
        """챗이 "모델 시점" 을 그리는 데 필요한 사실.

        ``gone``: 캐시에 남은 첫 동적 레코드의 history 서수 ``{"hidx": n}`` — 그
        앞(서수 < n)은 전부 빠졌다(압축·FIFO·복원 트림은 모두 앞에서부터 비우므로
        경계 하나면 된다; fold 는 중간의 넛지를 빼지만 넛지는 카드가 없다). 빠진 게
        없으면 None. 카드는 자기 ``hidx``(이벤트에 실림)로 판정한다 — 턴 번호는
        런마다 1 부터 다시 시작해 경계가 될 수 없다(실측).
        ``summary``: 압축 요약 본문·파일 목록·대체한 턴 범위(history 의 turn)·전후
        토큰(resume 땐 None). 압축은 드문 사건이라 파일 읽기는 싸다.
        """
        view: dict = {
            "gone": None,
            "summary": None,
            "compactions": self._compaction_count,
            "folded_nudges": self._folded_count,
        }
        first_cached, kept, evicted = self._evicted_prefix()
        turns = None
        if evicted:
            view["gone"] = {"hidx": first_cached}
            if kept:
                # 경계 앞인데 남은 핀 요청(v10.41.0) — 프런트가 이 카드는 접지
                # 않는다.
                view["gone"]["kept"] = kept
            turns = [
                int(evicted[0][1].get("turn", 0) or 0),
                int(evicted[-1][1].get("turn", 0) or 0),
            ]
        if self._summary:
            before, after = self._last_compaction_tokens or (None, None)
            view["summary"] = {
                "text": self._summary,
                "files": list(self._file_list),
                "turns": turns,
                "before_tokens": before,
                "after_tokens": after,
            }
        return view

    def _kept_before_boundary(self) -> list[int]:
        """경계(``_dynamic_start_index``) 앞인데 캐시에 남은 동적 레코드의 서수 —
        압축·FIFO 가 건너뛴 핀된 요청(v10.41.0). 핀이 없으면 비어 있다: 경계는
        늘 캐시의 첫 동적 레코드 이하로만 움직인다."""
        return sorted(
            h
            for h, m in zip(self._cache_hidx, self._cache)
            if m.get("role") != "system" and h < self._dynamic_start_index
        )

    def _evicted_prefix(self) -> tuple[int | None, list[int], list[tuple[int, dict]]]:
        """(경계 = 캐시에 남은 첫 **안 핀된** 동적 레코드의 history 서수, 경계 앞에
        남은 핀 서수들, 그 앞에서 빠진 ``(서수, 레코드)``).

        압축·FIFO·복원 트림은 모두 앞에서부터 비우므로 빠진 것은 접두사 하나 —
        핀된 요청만 그 안에서 남는다. history 의 선두 system 레코드는 대화가
        아니라 세지 않는다."""
        kept = self._kept_before_boundary()
        first_cached = next(
            (
                h
                for h, m in zip(self._cache_hidx, self._cache)
                if m.get("role") != "system" and h not in kept
            ),
            None,
        )
        if first_cached is None or not self._history_path.is_file():
            return first_cached, kept, []
        records = store.load_records(self._history_path)
        first_dyn = 1 if records and records[0].get("role") == "system" else 0
        evicted = [
            (h, records[h])
            for h in range(first_dyn, min(first_cached, len(records)))
            if h not in kept
        ]
        return first_cached, kept, evicted

    def evicted_records(self) -> list[tuple[int, dict]]:
        """캐시에서 빠진 접두사의 ``(history 서수, 레코드)`` — resume 재생이
        "컨텍스트 밖 대화" 의 카드를 다시 그리는 데 쓴다 (v10.24.1). 라이브에서는
        빠진 카드가 화면에 남아 접히는데, 재생이 캐시만 돌면 그 카드가 아예
        없어 접을 것이 없었다."""
        return self._evicted_prefix()[2]

    def _notify_context_view(self) -> None:
        """캐시가 바뀐 지점(압축·FIFO·fold)마다 — 실패가 루프를 막지 않는다."""
        try:
            render_context_view(self.context_view())
        except Exception:  # 표시면이 저장·루프를 깨면 안 된다
            pass

    def _append_to_history(self, message: dict) -> None:
        """enrich(turn 은 manager 소유) 후 history.jsonl 에 append.
        가드/패턴 근거는 :mod:`agent_cli.fsio` 모듈 docstring 참조."""
        # C5: 디스크 접촉은 store primitive 로 — 외부-정리 복구 가드
        # (mkdir-on-failure) 는 fsio.append_line 에 일반화돼 있다.
        store.append_record(self._history_path, self._enrich_record(message))

    def _enrich_record(self, message: dict) -> dict:
        """Build the history.jsonl record: the round-trip message + retrieval
        keys for history's structured JSON queries.

        Additive — round-trip fields (role/thought/ops/content/tool/success)
        are preserved verbatim (resume/compaction read them unchanged); we ADD
        ``kind``/``turn``/``ts``/``tools``/``text`` (and pass ``author``
        through) so queries no longer guess via the "Observation:" / "[nick]:"
        prefix conventions. The cache / LLM path is untouched (this is the file
        write only)."""
        kind, tools, text = _classify_record(message)
        record = dict(message)
        record["kind"] = kind
        record["turn"] = self._current_turn
        record["ts"] = _now_iso()
        # Which scope (skill subloop / worker) produced this record, "" = main.
        # Resume needs it: without the association, replayed turns all land on
        # the main timeline and a resumed session shows NO skill/agent cards at
        # all — the swimlane then advertises click-navigation to cards that do
        # not exist. Read from the renderer because the record is written ON the
        # scope's own thread (a parallel worker included), so the thread's
        # innermost scope IS this record's owner. Omitted when empty to keep
        # main-timeline records unchanged.
        scope = _current_scope_id()
        if scope:
            record["task_id"] = scope
        record["tools"] = tools
        # Files this record's tool(s) operate on — reuses the tool-aware
        # ``extract_file_paths`` (handles flat/ops shapes) so queries can find
        # "everything that touched auth.py", not just by tool name.
        record["files"] = extract_file_paths([message])
        record["text"] = text
        return record

    def _save_compaction_json(self) -> None:
        """Serialise compaction state next to history.jsonl. Idempotent
        — written atomically (temp + rename) so a crash mid-write
        leaves the previous version intact."""
        store.save_compaction(
            self._compaction_path,
            {
                "summary": self._summary,
                "file_list": list(self._file_list),
                "compaction_count": self._compaction_count,
                "last_compacted_at": self._last_compacted_at,
                "dynamic_start_index": self._dynamic_start_index,
            },
        )

    def _load_compaction_json(self) -> None:
        """Restore compaction state from disk on resume. Forward-compat:
        unknown ``version`` is silently ignored (cleared state)."""
        data = store.load_compaction(self._compaction_path)
        if data is None:
            return
        self._summary = data.get("summary", "") or ""
        self._file_list = list(data.get("file_list", []) or [])
        self._compaction_count = int(data.get("compaction_count", 0) or 0)
        self._last_compacted_at = data.get("last_compacted_at", "") or ""
        self._dynamic_start_index = int(data.get("dynamic_start_index", 0) or 0)

    def _restore_cache(self) -> None:
        """Read history.jsonl and load messages within token budget.

        When ``compaction.json`` declared a ``dynamic_start_index`` we
        load ``history[index:]`` forward in time — that's exactly the
        slice the running session would have had in cache when it last
        compacted, so the summary + dynamic stay in sync.

        When there is no compaction state (or the saved offset is
        invalid for the current history), fall back to the legacy
        reverse-load-until-budget strategy.
        """
        self._nl_cache = None  # cache rebuilt from disk → re-render on next get
        messages = store.load_records(self._history_path)

        use_offset = self._dynamic_start_index > 0 and self._dynamic_start_index <= len(
            messages
        )
        if use_offset:
            forward = messages[self._dynamic_start_index :]
            self._cache = forward
            # P0-8a: forward slice 는 history 와 연속이므로 서수 = 구간 range.
            self._cache_hidx = list(range(self._dynamic_start_index, len(messages)))
            self._history_ordinal = len(messages)
            self._cache_tokens = self._sum_tokens(forward)
            # If even the forward slice exceeds budget (budget shrank
            # since the previous run), trim oldest until it fits.
            while self._cache_tokens > self.max_context_tokens and len(self._cache) > 1:
                removed = self._cache.pop(0)
                self._cache_tokens -= self._scaled_tokens(removed)
                self._dynamic_start_index = self._cache_hidx.pop(0) + 1  # P0-8a
            self.fold_resolved_interventions()  # v4.51.0 — 위와 동일 재적용
            return

        # Legacy path: invalid or absent offset → reverse-load.
        total = self._sum_tokens(messages)
        start_idx = 0
        if total > self.max_context_tokens:
            running = total
            for i, msg in enumerate(messages):
                if running <= self.max_context_tokens:
                    start_idx = i
                    break
                running -= self._scaled_tokens(msg)
            else:
                start_idx = len(messages) - 1

        self._cache = messages[start_idx:]
        # P0-8a: legacy 경로도 정렬 북키핑 초기화 — 이후 evict/fold 가 hidx 를
        # 소비하므로 캐시와 길이/순서가 일치해야 한다.
        self._cache_hidx = list(range(start_idx, len(messages)))
        self._history_ordinal = len(messages)
        self._cache_tokens = self._sum_tokens(self._cache)
        # fold 재적용 (v4.51.0): live 가 접은 뷰를 레코드-기반 재판정으로
        # 동일 재현 — history 는 전부 남아 있으므로 여기서 다시 접는다.
        self.fold_resolved_interventions()

    # ── Fork support ─────────────────────────────────

    def fork_history_to(self, target_dir: Path) -> Path:
        """Copy this context's history.jsonl to target_dir for fork mode."""
        return store.fork_history(self._history_path, target_dir)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
