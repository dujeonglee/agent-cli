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
``compaction_enabled=False`` at construction time or via the
``AGENT_CLI_COMPACTION=off`` environment variable, both of which the
``AgentLoop`` plumbs from CLI flags. With compaction off the manager
reverts to the historical plain-FIFO behaviour.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from agent_cli.context._file_extract import extract_file_paths
from agent_cli.context.token_estimator import estimate_tokens
from agent_cli.render import render_compaction_progress
from agent_cli.wire_formats import get as _get_wire_format


# ── Defaults / constants ─────────────────────────────────
DEFAULT_TOKEN_BUDGET = 100_000
_COMPACTION_THRESHOLD_RATIO = 0.9  # trigger when cache > 90% of budget
_SUMMARY_CHAR_CAP = 8000  # ≈ 2000 tokens at 4 chars/token
_COMPACTION_JSON_VERSION = 1

# NOTE: oversized single-output protection lives in the loop now, not here.
# A tool observation larger than ``context_window / 10`` is replaced with a
# narrow-it nudge at the result→observation seam (``AgentLoop._tool_observation``)
# before it ever reaches ``add`` — per-tool via ``Tool.apply_oversized_cap`` /
# ``Tool.render_observation``. ``add`` is therefore pure storage: no message it
# receives can blow past the window. (Replaced the earlier chunked-spill record.)


def compute_token_budget(context_window: int, max_output_tokens: int) -> int:
    """Compute the fallback context budget (``max_context_tokens``).

    budget = context_window - max_output_tokens - 4000 (system reserve)

    NOTE: this is no longer the live compaction threshold. flow 1
    computes the real target per call as ``(context − system(measured)
    − max_output) × 0.8`` in ``AgentLoop._call_llm``. This value remains
    the ``_evict_fifo`` default target and the budget used to restore the
    cache on resume (before the first call's ``ensure_within`` refines
    it), where a fixed 4000-token system estimate is good enough.
    """
    reserve = max_output_tokens + 4000
    budget = context_window - reserve
    return max(budget, 4000)  # floor: at least 4K tokens


def _compaction_disabled_via_env() -> bool:
    """``AGENT_CLI_COMPACTION=off`` (or false/0/disabled) wins over the
    constructor flag — operator-level kill switch for deployments where
    the LLM summarisation cost is undesirable."""
    val = os.environ.get("AGENT_CLI_COMPACTION", "").strip().lower()
    return val in ("off", "false", "0", "disabled", "no")


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
        self._cache: list[dict] = []
        self._cache_tokens: int = 0
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

        # Compaction can be disabled either by constructor flag
        # (CLI ``--no-compaction``) or by environment variable
        # (``AGENT_CLI_COMPACTION=off``). Env wins over flag so a deploy
        # can shut the feature off without re-deploying the agent loop.
        self._compaction_enabled = compaction_enabled and not (
            _compaction_disabled_via_env()
        )

        # Injected by AgentLoop after construction. ``_compactor_callback``
        # = function ``(messages) -> summary text``; ``_recorder`` =
        # TurnRecorder (or None for headless paths). Both stay optional
        # so unit tests can drive ContextManager without a full loop.
        self._compactor_callback: Optional[Callable[[list[dict]], str]] = None
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
            cache_rest = cache[1:]
        else:
            cache_rest = cache

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

        result.extend(_to_natural_language(msg, self.wire_format) for msg in cache_rest)
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
        failure_signal: Optional[str] = None
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
        except Exception as e:  # noqa: BLE001 — provider boundary
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
                self._cache_tokens = max(
                    self._cache_tokens - _estimate_message_tokens(removed), 0
                )
                self._dynamic_start_index += 1
                self._save_compaction_json()

        return len(self._cache) < before_len

    # ── Persistence ──────────────────────────────────

    def _append_to_history(self, message: dict) -> None:
        """Append a single JSON line to history.jsonl.

        Defensively recreates the parent directory before opening — the
        session dir is created in ``__init__`` but can disappear between
        construction and the first write if an external process (user
        cleanup, parallel `rm -rf .agent-cli/sessions/`, etc.) wipes the
        tree mid-run. Without this guard, parallel delegate workers
        would crash on the first turn's history flush.
        """
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        record = self._enrich_record(message)
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

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
        data = {
            "version": _COMPACTION_JSON_VERSION,
            "summary": self._summary,
            "file_list": list(self._file_list),
            "compaction_count": self._compaction_count,
            "last_compacted_at": self._last_compacted_at,
            "dynamic_start_index": self._dynamic_start_index,
        }
        tmp = self._compaction_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self._compaction_path)

    def _load_compaction_json(self) -> None:
        """Restore compaction state from disk on resume. Forward-compat:
        unknown ``version`` is silently ignored (cleared state)."""
        if not self._compaction_path.is_file():
            return
        try:
            with open(self._compaction_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        if data.get("version") != _COMPACTION_JSON_VERSION:
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
        with open(self._history_path, "r", encoding="utf-8") as f:
            raw_lines = [line.strip() for line in f.readlines() if line.strip()]

        messages: list[dict] = []
        for line in raw_lines:
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue

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

    # ── Fork support ─────────────────────────────────

    def fork_history_to(self, target_dir: Path) -> Path:
        """Copy this context's history.jsonl to target_dir for fork mode."""
        import shutil

        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "history.jsonl"
        if self._history_path.is_file():
            shutil.copy2(self._history_path, target_path)
        return target_path


# ── Helpers ─────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_OBSERVATION_PREFIX = "Observation: "
_OP_SUMMARY_CAP = 200


def _op_summary(action: str, action_input) -> str:
    """Flatten one op to a compact ``action {args}`` search string (capped)."""
    try:
        args = json.dumps(action_input, ensure_ascii=False)
    except (TypeError, ValueError):
        args = str(action_input)
    s = f"{action} {args}".strip()
    return s[:_OP_SUMMARY_CAP]


def _classify_record(message: dict) -> tuple[str, list[str], str]:
    """Derive ``(kind, tools, text)`` retrieval fields from a record's shape.

    ``kind``  — query | observation | action | final | raw | system | <role>
    ``tools`` — tool names involved (list; empty for query/final/raw)
    ``text``  — flat searchable surface (prefix-stripped query/observation,
                thought+op summaries for actions, the result for a final)

    Pure function of the record shape — no prefix-convention guessing leaks
    into read_context; this is the single place that encodes it.
    """
    role = message.get("role")
    content = str(message.get("content") or "")

    if role == "system":
        return "system", [], content

    if role == "user":
        if "tool" in message:  # tool observation
            text = (
                content[len(_OBSERVATION_PREFIX) :]
                if content.startswith(_OBSERVATION_PREFIX)
                else content
            )
            return "observation", [str(message.get("tool") or "")], text
        # human query — strip the "[author]: " label for the search surface
        author = message.get("author")
        text = content
        if author and content.startswith(f"[{author}]: "):
            text = content[len(f"[{author}]: ") :]
        return "query", [], text

    if role == "assistant":
        ops = message.get("ops")
        if isinstance(ops, list) and ops:
            actions = [
                o.get("action") for o in ops if isinstance(o, dict) and o.get("action")
            ]
            is_final = "complete" in actions
            thought = str(message.get("thought") or "")
            parts: list[str] = [thought] if thought else []
            for o in ops:
                if not isinstance(o, dict):
                    continue
                action = o.get("action") or ""
                ai = o.get("action_input")
                if action == "complete":
                    result = ai.get("result") if isinstance(ai, dict) else ai
                    parts.append(str(result or ""))
                else:
                    parts.append(_op_summary(action, ai))
            text = " | ".join(p for p in parts if p)
            return ("final" if is_final else "action"), actions, text
        # raw assistant content (e.g. NO_JSON fallback stored verbatim)
        return "raw", [], content

    return str(role or "?"), [], content


def _context_view(message: dict) -> dict:
    """An assistant turn as it should appear in RE-FED context: each op's
    ``action_input`` passed through its tool's
    ``render_action_input_for_context`` (default identity — so this is a no-op
    for every tool today; the seam is consulted by both the render path
    (:func:`_to_natural_language`) and the budget path
    (:func:`_estimate_message_tokens`) so the two always agree).

    Returns ``message`` unchanged (same object) when nothing is elided, else a
    shallow copy with rewritten ops — the source record (history.jsonl + cache)
    is never mutated.
    """
    if message.get("role") != "assistant":
        return message
    from agent_cli.tools import TOOLS  # lazy: registry → context.manager cycle

    def _view(action: str, ai):
        if not action or not isinstance(ai, dict):
            return ai
        tool = TOOLS.get(action)
        return tool.render_action_input_for_context(ai) if tool else ai

    ops = message.get("ops")
    if isinstance(ops, list):
        new_ops = list(ops)
        changed = False
        for i, op in enumerate(ops):
            if not isinstance(op, dict):
                continue
            ai = op.get("action_input")
            view = _view(op.get("action"), ai)
            if view is not ai:
                new_ops[i] = {**op, "action_input": view}
                changed = True
        return {**message, "ops": new_ops} if changed else message

    ai = message.get("action_input")
    view = _view(message.get("action"), ai)
    return {**message, "action_input": view} if view is not ai else message


def _estimate_message_tokens(msg: dict) -> int:
    """Estimate tokens for a single message dict."""
    msg = _context_view(msg)  # count what is actually re-fed (elided body)
    total = 4  # role + formatting overhead
    for key in ("content", "thought", "action_input"):
        val = msg.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            total += estimate_tokens(val)
        elif isinstance(val, dict):
            total += estimate_tokens(json.dumps(val, ensure_ascii=False))
    action = msg.get("action", "")
    if action:
        total += estimate_tokens(action)
    # Multi-op (md_array / react default) assistant records carry their
    # action(s) + action_input + complete result inside ``ops`` — count them,
    # else every assistant turn is undercounted to just its ``thought`` (a
    # large write_file content arg / complete result would be invisible to the
    # budget estimator).
    ops = msg.get("ops")
    if isinstance(ops, list):
        for op in ops:
            if not isinstance(op, dict):
                continue
            op_action = op.get("action")
            if op_action:
                total += estimate_tokens(op_action)
            op_input = op.get("action_input")
            if isinstance(op_input, str):
                total += estimate_tokens(op_input)
            elif isinstance(op_input, dict):
                total += estimate_tokens(json.dumps(op_input, ensure_ascii=False))
    artifact = msg.get("artifact", "")
    if artifact:
        total += estimate_tokens(artifact)
    return total


def _sum_message_tokens(messages) -> int:
    """Estimated token total for a message list — the single expression for
    ``sum(_estimate_message_tokens(...))`` used across the cache (re)builds
    (resume restore, compaction evict, force_fit)."""
    return sum(_estimate_message_tokens(m) for m in messages)


def _to_natural_language(msg: dict, wire_format) -> dict:
    """Convert a JSON history record to a natural-language message for the LLM.

    Input formats (from history.jsonl):
        User input:     {"role":"user", "content":"..."}
        Tool result:    {"role":"user", "tool":"...", "args":{...}, "content":"...", "artifact":"..."}
        Assistant act:  {"role":"assistant", "thought":"...", "action":"...", "action_input":{...}}
        Complete:       {"role":"assistant", "thought":"...", "action":"complete", "action_input":{"result":"..."}}

    Output format (for chat completion):
        {"role": "user"|"assistant", "content": "...natural language..."}

    Assistant records are handed off to ``wire_format.render_assistant_
    from_history`` so each plugin owns the on-disk → message conversion
    for its own format. The user / tool branches live here because they
    are format-agnostic.
    """
    role = msg.get("role", "user")

    if role == "user":
        tool = msg.get("tool")
        if tool:
            return _convert_observation(msg)
        return {"role": "user", "content": msg.get("content", "")}

    # Re-feed the context view (bulky action_input bodies elided per-tool;
    # default identity → unchanged today). History/cache stay faithful.
    return wire_format.render_assistant_from_history(_context_view(msg))


_SUMMARY_CONTENT_EXCERPT = 200  # chars of tool-result content kept per line


def _to_summary_text(msg: dict) -> str:
    """Render one history record as a single natural-language line for the
    summarisation transcript.

    Unlike ``_to_natural_language`` (which round-trips assistant turns back
    to the wire shape — ReAct JSON — for resume/recovery self-reinforcement),
    this is for the *summariser's* input: assistant turns become prose and
    tool args are summarised (``write_file`` → path only, no file body), so
    the model sees a transcript to summarise rather than a ReAct conversation
    to continue. Keeping the wire shape here made a small model emit another
    ``write_file`` action instead of a summary.
    """
    role = msg.get("role", "user")

    if role == "user":
        tool = msg.get("tool")
        if not tool:
            return f"User: {msg.get('content', '')}"
        header = f"[{tool}]"
        content = str(msg.get("content", "") or "").strip()
        if content:
            excerpt = content[:_SUMMARY_CONTENT_EXCERPT]
            if len(content) > _SUMMARY_CONTENT_EXCERPT:
                excerpt += "…"
            header += f" → {excerpt}"
        artifact = msg.get("artifact", "")
        if artifact:
            header += f" → {artifact}"
        return header

    # assistant
    ops = msg.get("ops")
    if not ops and "action" not in msg and "thought" not in msg:
        return f"Assistant: {msg.get('content', '')}"
    thought = (msg.get("thought") or "").strip()
    # Normalize single-op ({action, action_input}) and multi-op ({ops:[...]})
    # records to one op list. Multi-op formats (md_array, react) store `ops`,
    # so reading only the top-level `action` here lost EVERY tool label for
    # them — the summariser saw thought-only prose with no record of which
    # tools ran.
    op_list = ops if isinstance(ops, list) else ([msg] if msg.get("action") else [])
    # Delegate the per-action label to the tool itself (sibling of
    # touched_paths): each tool reads its OWN prefixed/array action_input
    # shape. Lazy import avoids the module-load cycle (registry →
    # context-tool → context.manager).
    from agent_cli.tools.registry import TOOLS

    action_lines: list[str] = []
    for op in op_list:
        if not isinstance(op, dict):
            continue
        action = op.get("action") or ""
        if not action:
            continue
        tool = TOOLS.get(action)
        # Stored ops are FLAT (the model's emission, e.g. read_file `{path}`);
        # summary_arg reads the tool's CANONICAL shape (`read_file_reads[]`).
        # Normalize flat → canonical (idempotent on already-canonical input).
        ai = op.get("action_input") or {}
        arg_summary = tool.summary_arg(tool.wrap_single_op(ai)) if tool else ""
        action_lines.append(f"  → action: {action}({arg_summary})")
    head = f"Assistant: {thought}" if thought else "Assistant:"
    return head + "\n" + "\n".join(action_lines) if action_lines else head


def _convert_observation(msg: dict) -> dict:
    """Convert a tool result message to natural language."""
    tool = msg.get("tool", "")
    content = msg.get("content", "")
    artifact = msg.get("artifact", "")

    # Tool-result records carry no args (history.jsonl stores only
    # {role, tool, success, content}), so there is nothing to label here.
    parts = [f"[{tool}]"]
    if content:
        parts.append(content)
    if artifact:
        parts.append(f"→ {artifact}")

    return {"role": "user", "content": "\n".join(parts)}
