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
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

# C5: 관심사 분리 — record 계약은 records, LLM 표현은 render, 디스크
# I/O primitive 는 store 소유. manager 는 캐시 + 압축 정책만.
from agent_cli.context import store
from agent_cli.context._file_extract import extract_file_paths
from agent_cli.context.records import _classify_record
from agent_cli.context.render import (
    _estimate_message_tokens,
    _sum_message_tokens,
    _to_natural_language,
    _to_summary_text,
)
from agent_cli.render import render_compaction_progress
from agent_cli.wire_formats import get as _get_wire_format

# ── Defaults / constants ─────────────────────────────────
DEFAULT_TOKEN_BUDGET = 100_000
_COMPACTION_THRESHOLD_RATIO = 0.9  # trigger when cache > 90% of budget
_SUMMARY_CHAR_CAP = 8000  # ≈ 2000 tokens at 4 chars/token

# Compaction TARGET ratio: the loop squeezes the cache down to this fraction of
# available headroom before each LLM call (``(context − system − output) ×
# ratio``). Lower = compact earlier/more aggressively (more headroom, less
# context kept); higher = compact later (more context, higher overflow risk).
# Lives on the ContextManager (not a loop constant) so the web UI can adjust it
# live — the loop and the web server share one ctx instance, so a slider write
# is read by the next LLM call with no dirty-flag rebuild. Clamped to
# [_MIN, _MAX] so a stray value can't disable compaction or over-squeeze.
DEFAULT_COMPACTION_RATIO = 0.8
COMPACTION_RATIO_MIN = 0.5
COMPACTION_RATIO_MAX = 0.95
COMPACTION_RATIO_STEP = 0.05

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
    "\n\n(If nothing remains to do — or you can only continue after a "
    "sub-agent's reply arrives — call `complete`; a pending reply will wake "
    "you when it comes.)"
)


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
    computes the real target per call as ``(context − system(measured)
    − max_output) × 0.8`` in ``AgentLoop._call_llm``. This value remains
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
        wire_format=None,
        compaction_enabled: bool = True,
        compaction_ratio: float = DEFAULT_COMPACTION_RATIO,
    ):
        # Wire-format plugin attached to this ctx — drives the on-disk
        # → in-memory rendering of assistant turns when ``get_messages``
        # is called (overflow recovery, session resume). One ctx instance
        # owns one plugin instance so a single session can never mix
        # formats. Default falls back to the default wire format
        # for the headless / test paths that don't yet thread the choice
        # through; mirrors the pattern in ``AgentLoop.__init__``.
        if wire_format is None:
            wire_format = _get_wire_format()
        self.wire_format = wire_format

        self.session_dir = Path(session_dir)
        self.max_context_tokens = (
            max_context_tokens if max_context_tokens > 0 else DEFAULT_TOKEN_BUDGET
        )
        # Live-tunable compaction target (web slider). Clamped on set so the
        # loop's per-call ``× compaction_ratio`` can never disable compaction.
        self.compaction_ratio = clamp_compaction_ratio(compaction_ratio)
        self._cache: list[dict] = []
        self._cache_tokens: int = 0
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
        # `turn` field so read_context can range/group by turn. The loop sets
        # it at each turn boundary; 0 covers the run-starting query.
        self._current_turn: int = 0

        # Compaction state. ``_summary`` empty until first compaction
        # completes. ``_dynamic_start_index`` tracks how many history
        # entries have been compacted away — resume reads ``history
        # [index:]`` to avoid replaying the summarised prefix.
        self._summary: str = ""
        self._file_list: list[str] = []
        self._compaction_count: int = 0
        self._last_compacted_at: str = ""
        self._dynamic_start_index: int = 0

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

        if resume:
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
        msg_tokens = _estimate_message_tokens(message)
        self._cache.append(message)
        # Incremental render: keep the natural-language mirror in step so
        # ``get_messages`` never re-renders old records. A leading system
        # message is excluded (``get_messages`` passes it through verbatim).
        if self._nl_cache is not None and not (
            len(self._cache) == 1 and message.get("role") == "system"
        ):
            self._nl_cache.append(_to_natural_language(message, self.wire_format))
        self._cache_tokens += msg_tokens
        self._append_to_history(message)
        # Return the stored message so callers can render exactly what was
        # stored (live card == ctx == resume).
        return message

    def set_turn(self, turn: int) -> None:
        """Set the current LLM turn index. The loop calls this at each turn
        boundary so subsequent history records carry the right ``turn``."""
        self._current_turn = turn

    def reconcile_actual_tokens(
        self, actual_total_tokens: int, system_tokens: int = 0
    ) -> None:
        """Re-anchor the cache token count to the server's actual input
        count (flow 1, part B).

        ``actual_total_tokens`` is what the provider reported for the
        last call's prompt (``usage.input_tokens`` + cache fields) — it
        covers system + messages. The cache holds only messages, so we
        subtract ``system_tokens`` (measured by the loop for that same
        call) and store the remainder.

        The local ``chars/4`` estimate under-counts CJK badly; replacing
        the accumulated estimate with ground truth each call means error
        never compounds across turns — at most one turn's worth of
        newly-added (still-estimated) messages drifts before the next
        reconcile. No-op when the provider reported no usage (cold start
        / provider without usage), leaving the running estimate in place.
        """
        if actual_total_tokens <= 0:
            return
        self._cache_tokens = max(actual_total_tokens - system_tokens, 0)

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
        # verbatim (no wire-format conversion); ``_to_natural_language``
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
                _to_natural_language(msg, self.wire_format)
                for msg in cache[rest_start:]
            ]
        result.extend(self._nl_cache)

        # Append the complete-nudge to the CURRENT observation only, at feed
        # time. A tool result is always in the dynamic slice, so when cache[-1]
        # is one, result[-1] is its rendered form (nl_cache is non-empty). Copy
        # before mutating so _nl_cache and the stored records stay clean (this
        # never persists — see constant).
        if cache and cache[-1].get("role") == "user" and cache[-1].get("tool"):
            last = dict(result[-1])
            last["content"] = last.get("content", "") + _OBS_COMPLETE_NUDGE
            result[-1] = last
        return result

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

    def set_compaction_ratio(self, ratio: float) -> float:
        """Set the live compaction target ratio (web slider). Clamped to
        [_MIN, _MAX]; returns the clamped value actually stored. Read by the
        loop's next LLM call (shared ctx) — no rebuild needed."""
        self.compaction_ratio = clamp_compaction_ratio(ratio)
        return self.compaction_ratio

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

        The loop computes ``target_tokens`` as ``(context_window −
        system_tokens − max_output) × 0.8`` each call — system measured
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
            self._compact()
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

    def _compact(self) -> None:
        """Execute one compaction pass — split, summarise, extract paths,
        rebuild cache, persist. Raises ``CompactionError`` on summariser
        failure (callback exception or empty/non-string return)."""
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
            self._cache = anchor + retained
            self._nl_cache = None  # bulk rebuild → re-render on next get
            self._cache_tokens = _sum_message_tokens(self._cache)
            self._compaction_count += 1
            self._last_compacted_at = _now_iso()
            # The evicted slice came from the cache; reflect it in the
            # history offset so resume skips the summarised tail.
            self._dynamic_start_index += len(evict_set)
            self._save_compaction_json()
        except CompactionError:
            failure_signal = "summary_failed"
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1000.0
            # Determine ``fallback_used`` AFTER the try-block. If we
            # raised, the caller (ensure_within) will run FIFO so
            # mark fallback now. If we succeeded but the cache is
            # still over threshold, ensure_within's belt-and-braces
            # will also run FIFO — same flag.
            threshold = int(self.max_context_tokens * _COMPACTION_THRESHOLD_RATIO)
            if failure_signal is not None or self._cache_tokens > threshold:
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

        render_compaction_progress(
            phase="done",
            old_tokens=old_tokens,
            new_tokens=self._cache_tokens,
        )

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
        anyway.
        """
        anchor: list[dict] = []
        dynamic_start = 0
        if self._cache and self._cache[0].get("role") == "system":
            anchor = [self._cache[0]]
            dynamic_start = 1
        dynamic = self._cache[dynamic_start:]

        if not dynamic:
            return anchor, [], []

        dynamic_tokens = _sum_message_tokens(dynamic)
        target_evict = dynamic_tokens // 2

        # Edge case: a single huge dynamic message would otherwise be
        # untouchable (loop never reaches target). Force evict the
        # first message in that case — better to lose context detail
        # than to spin in an infinite compaction loop.
        evict: list[dict] = []
        evicted_tokens = 0
        for msg in dynamic:
            evict.append(msg)
            evicted_tokens += _estimate_message_tokens(msg)
            if evicted_tokens >= target_evict:
                break

        retained = dynamic[len(evict) :]
        return anchor, evict, retained

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
        while self._cache_tokens > target and len(self._cache) > 1:
            removed = self._cache.pop(0)
            self._nl_cache = None  # front pop → rendered mirror stale
            self._cache_tokens -= _estimate_message_tokens(removed)
            # The popped message came from the cache, which mirrors
            # history.jsonl — reflect the drop in the offset so
            # ``--resume`` doesn't pull the popped entry back in.
            self._dynamic_start_index += 1
        # Persist updated offset so an interrupted run survives.
        if self._summary or self._compaction_count or self._dynamic_start_index:
            self._save_compaction_json()

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
                removed = self._cache.pop(0)
                self._nl_cache = None  # front pop → rendered mirror stale
                self._cache_tokens = max(
                    self._cache_tokens - _estimate_message_tokens(removed), 0
                )
                self._dynamic_start_index += 1
                self._save_compaction_json()

        return len(self._cache) < before_len

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
        - 연속 실패는 자연 처리: 미해소(꼬리) 개입은 남고, 새 개입이
          그 앞의 것을 해소 조건 충족 전이라도 live 호출로 접는다 —
          컨텍스트에는 항상 최신 개입 1개만.
        반환: 접은 레코드 수.
        """
        from agent_cli.context.records import (
            fold_resolved_intervention_indices,
            is_format_intervention,
        )

        if assume_tail_resolved:
            # live 경로(파싱 성공 직후): 성공이 확인된 순간, 캐시의 **모든**
            # 형식 개입이 소비 완료다 — 연속 실패로 쌓인 [실패,개입]×k 전체를
            # 접는다("성공 궤적만" 의미론; 개별 해소-증거 판정 불필요 —
            # 호출 시점 자체가 해소 신호).
            idxs = []
            for i, rec in enumerate(self._cache):
                if is_format_intervention(rec):
                    idxs.append(i)
                    if i > 0 and self._cache[i - 1].get("role") == "assistant":
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
            self._cache_tokens = max(
                self._cache_tokens - _estimate_message_tokens(removed), 0
            )
        self._nl_cache = None  # 벌크 변형 → 렌더 미러 재빌드
        return len(idxs)

    def _append_to_history(self, message: dict) -> None:
        """enrich(turn 은 manager 소유) 후 history.jsonl 에 append.
        가드/패턴 근거는 :mod:`agent_cli.fsio` 모듈 docstring 참조."""
        # C5: 디스크 접촉은 store primitive 로 — 외부-정리 복구 가드
        # (mkdir-on-failure) 는 fsio.append_line 에 일반화돼 있다.
        store.append_record(self._history_path, self._enrich_record(message))

    def _enrich_record(self, message: dict) -> dict:
        """Build the history.jsonl record: the round-trip message + retrieval
        keys for read_context's structured JSON queries.

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
            self._cache_tokens = _sum_message_tokens(forward)
            # If even the forward slice exceeds budget (budget shrank
            # since the previous run), trim oldest until it fits.
            while self._cache_tokens > self.max_context_tokens and len(self._cache) > 1:
                removed = self._cache.pop(0)
                self._cache_tokens -= _estimate_message_tokens(removed)
                self._dynamic_start_index += 1
            self.fold_resolved_interventions()  # v4.51.0 — 위와 동일 재적용
            return

        # Legacy path: invalid or absent offset → reverse-load.
        total = _sum_message_tokens(messages)
        start_idx = 0
        if total > self.max_context_tokens:
            running = total
            for i, msg in enumerate(messages):
                if running <= self.max_context_tokens:
                    start_idx = i
                    break
                running -= _estimate_message_tokens(msg)
            else:
                start_idx = len(messages) - 1

        self._cache = messages[start_idx:]
        self._cache_tokens = _sum_message_tokens(self._cache)
        # fold 재적용 (v4.51.0): live 가 접은 뷰를 레코드-기반 재판정으로
        # 동일 재현 — history 는 전부 남아 있으므로 여기서 다시 접는다.
        self.fold_resolved_interventions()

    # ── Fork support ─────────────────────────────────

    def fork_history_to(self, target_dir: Path) -> Path:
        """Copy this context's history.jsonl to target_dir for fork mode."""
        return store.fork_history(self._history_path, target_dir)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
