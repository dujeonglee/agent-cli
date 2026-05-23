"""Context manager with token-budget compaction and history.jsonl persistence.

Stores full conversation in history.jsonl (JSON Lines, append-only).
Maintains an in-memory context cache that fits within a token budget.

When the cache exceeds 90% of budget the manager triggers a *compaction*
pass (RFC docs/context-compaction/):

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
``_maybe_compact`` wrapper falls through to a belt-and-braces FIFO drop
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
from agent_cli.tools.action_summary import summarize_tool_args
from agent_cli.wire_formats import get as _get_wire_format


# ── Defaults / constants ─────────────────────────────────
DEFAULT_TOKEN_BUDGET = 100_000
_COMPACTION_THRESHOLD_RATIO = 0.9  # trigger when cache > 90% of budget
_SUMMARY_CHAR_CAP = 8000  # ≈ 2000 tokens at 4 chars/token
_COMPACTION_JSON_VERSION = 1


def compute_token_budget(context_window: int, max_output_tokens: int) -> int:
    """Compute context token budget from model capabilities.

    budget = context_window - max_output_tokens - system_prompt_reserve
    System prompt reserve is estimated at 4000 tokens.
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
    empty/non-string return, etc.). ``_maybe_compact`` catches this and
    falls back to the belt-and-braces FIFO drop."""


class ContextManager:
    """Conversation history with token-budget compaction + persistence.

    - history.jsonl: append-only JSON Lines, every message ever added.
    - In-memory cache: messages within ``max_context_tokens``, refined by
      compaction at the 90% mark.
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
        # formats. Default falls back to the registered "react" plugin
        # for the headless / test paths that don't yet thread the choice
        # through; mirrors the pattern in ``AgentLoop.__init__``.
        if wire_format is None:
            wire_format = _get_wire_format("react")
        self.wire_format = wire_format

        self.session_dir = Path(session_dir)
        self.max_context_tokens = (
            max_context_tokens if max_context_tokens > 0 else DEFAULT_TOKEN_BUDGET
        )
        self._cache: list[dict] = []
        self._cache_tokens: int = 0
        self._history_path = self.session_dir / "history.jsonl"
        self._compaction_path = self.session_dir / "compaction.json"

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

    def add(self, message: dict) -> None:
        """Add a message to cache and persist to history.jsonl.

        Triggers compaction when the resulting cache exceeds 90% of the
        token budget. Falls back to plain FIFO drop when compaction is
        disabled, the summariser fails, or the rebuilt cache is still
        over budget.
        """
        msg_tokens = _estimate_message_tokens(message)
        self._cache.append(message)
        self._cache_tokens += msg_tokens
        self._maybe_compact()
        self._append_to_history(message)

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

    def _maybe_compact(self) -> None:
        """Trigger compaction when cache exceeds the 90% threshold.

        Two-layer safety:
          1. Attempt ``_compact()`` (LLM summarisation + cache rebuild).
          2. Whether (1) succeeded or raised ``CompactionError``, if the
             cache is *still* above threshold, drop oldest with plain
             FIFO until it fits. Same fallback path catches both the
             summariser-failure case and the small-budget-with-large-
             summary edge case in §2.1 of DESIGN.
        """
        threshold = int(self.max_context_tokens * _COMPACTION_THRESHOLD_RATIO)
        if self._cache_tokens <= threshold:
            return

        # Compaction disabled (CLI flag or env): skip directly to FIFO.
        if not self._compaction_enabled or self._compactor_callback is None:
            self._evict_fifo()
            return

        try:
            self._compact()
        except CompactionError as e:
            render_compaction_progress(phase="warning", reason=str(e))

        # Belt-and-braces: idempotent — no-op when ``_compact()`` already
        # brought the cache below threshold.
        if self._cache_tokens > threshold:
            self._evict_fifo()

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
            # Convert evict_set (raw history dicts with `tool`/`thought`/
            # `action` keys) to chat-ready `{role, content}` form via the
            # same path ``get_messages`` uses. The callback talks to a
            # provider that only understands role+content; without this
            # conversion the provider receives unknown keys and may either
            # error or send junk text. ``_to_natural_language`` is
            # idempotent for plain user messages, so prepending the prior
            # summary after conversion stays safe.
            chat_ready_evict = [
                _to_natural_language(m, self.wire_format) for m in evict_set
            ]
            if self._summary:
                prior_context_msg = {
                    "role": "user",
                    "content": (
                        "## Running summary of earlier conversation\n\n"
                        f"{self._summary}\n\n"
                        "Below are NEW messages to fold into this "
                        "running summary. Produce one updated summary."
                    ),
                }
                summarize_input = [prior_context_msg] + chat_ready_evict
            else:
                summarize_input = chat_ready_evict

            new_summary = self._summarize_messages(summarize_input)
            new_paths = extract_file_paths(evict_set)

            # Cap and store.
            self._summary = new_summary[:_SUMMARY_CHAR_CAP]
            self._file_list = self._merge_file_lists(self._file_list, new_paths)
            self._cache = anchor + retained
            self._cache_tokens = sum(_estimate_message_tokens(m) for m in self._cache)
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
            # raised, the caller (_maybe_compact) will run FIFO so
            # mark fallback now. If we succeeded but the cache is
            # still over threshold, _maybe_compact's belt-and-braces
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

        dynamic_tokens = sum(_estimate_message_tokens(m) for m in dynamic)
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

    def _evict_fifo(self) -> None:
        """Plain FIFO drop until cache fits within the budget. Used both
        as the ``compaction_enabled=False`` path and as the belt-and-
        braces fallback inside ``_maybe_compact``.

        Uses the full budget (100%) as the drop target — same semantic
        as the pre-compaction FIFO path. The belt-and-braces case where
        a rebuilt cache lands in the 90-100% band is acceptable: the
        next ``add`` that pushes it over 90% will simply re-enter
        compaction, and compaction reliably shrinks the cache (summary
        is capped, retained tail is half-by-tokens).
        """
        while self._cache_tokens > self.max_context_tokens and len(self._cache) > 1:
            removed = self._cache.pop(0)
            self._cache_tokens -= _estimate_message_tokens(removed)
            # The popped message came from the cache, which mirrors
            # history.jsonl — reflect the drop in the offset so
            # ``--resume`` doesn't pull the popped entry back in.
            self._dynamic_start_index += 1
        # Persist updated offset so an interrupted run survives.
        if self._summary or self._compaction_count or self._dynamic_start_index:
            self._save_compaction_json()

    # ── Persistence ──────────────────────────────────

    def _append_to_history(self, message: dict) -> None:
        """Append a single JSON line to history.jsonl."""
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

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
            self._cache_tokens = sum(_estimate_message_tokens(m) for m in forward)
            # If even the forward slice exceeds budget (budget shrank
            # since the previous run), trim oldest until it fits.
            while self._cache_tokens > self.max_context_tokens and len(self._cache) > 1:
                removed = self._cache.pop(0)
                self._cache_tokens -= _estimate_message_tokens(removed)
                self._dynamic_start_index += 1
            return

        # Legacy path: invalid or absent offset → reverse-load.
        total = sum(_estimate_message_tokens(m) for m in messages)
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
        self._cache_tokens = sum(_estimate_message_tokens(m) for m in self._cache)

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


def _estimate_message_tokens(msg: dict) -> int:
    """Estimate tokens for a single message dict."""
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
    artifact = msg.get("artifact", "")
    if artifact:
        total += estimate_tokens(artifact)
    return total


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

    return wire_format.render_assistant_from_history(msg)


def _convert_observation(msg: dict) -> dict:
    """Convert a tool result message to natural language."""
    tool = msg.get("tool", "")
    content = msg.get("content", "")
    artifact = msg.get("artifact", "")
    args = msg.get("args", {})

    if isinstance(args, dict) and args:
        arg_summary = summarize_tool_args(tool, args)
        header = f"[{tool}] {arg_summary}"
    else:
        header = f"[{tool}]"

    parts = [header]
    if content:
        parts.append(content)
    if artifact:
        parts.append(f"→ {artifact}")

    return {"role": "user", "content": "\n".join(parts)}
