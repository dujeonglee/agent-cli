"""Tests for context compaction (RFC docs/context-compaction/).

Coverage axes (TEST_PLAN.md):
  - Trigger / split / summary / file_list / get_messages
  - Fallback: LLM failure + cache-still-too-big belt-and-braces
  - Persistence: compaction.json schema + resume invariant
  - CLI flag + env var (NFR-CC-5)
  - TurnRecorder integration (NFR-CC-6)
  - render_compaction_progress single entry point (UI invariant)
  - File path extraction (_file_extract)

All tests use mock compactor callbacks — no real LLM calls in unit tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli.context._file_extract import extract_file_paths
from agent_cli.context.manager import (
    CompactionError,
    ContextManager,
)


# ── Fixtures ─────────────────────────────────────────


def _make_ctx(
    tmp_path: Path,
    *,
    max_context_tokens: int = 100,
    compaction_enabled: bool = True,
    summary: str = "answer",
):
    """Build a ContextManager with a small budget so tests can trigger
    compaction by adding only a handful of messages."""
    ctx = ContextManager(
        tmp_path / "session",
        max_context_tokens=max_context_tokens,
        compaction_enabled=compaction_enabled,
    )
    calls: list[list[dict]] = []

    def fake_compactor(messages: list[dict]) -> str:
        calls.append(list(messages))
        return summary

    ctx.set_compactor(fake_compactor)
    return ctx, calls


def _add(ctx, msg):
    """Add a message, then run flow-1's preventive pass at the budget
    threshold. Mirrors the pre-flow-1 inline trigger: compaction now
    fires from the loop's per-call ``ensure_within``, not from ``add``."""
    ctx.add(msg)
    ctx.ensure_within(ctx.max_context_tokens)


# ── 1. Trigger ───────────────────────────────────────


class TestCompactionTrigger:
    def test_triggers_above_90_percent(self, tmp_path):
        """``add`` fires ``_compact`` when cache > 0.9 * budget."""
        ctx, calls = _make_ctx(tmp_path, max_context_tokens=100)
        _add(ctx, {"role": "system", "content": "sys"})
        # ~10 tokens per message → 11 messages ~ 110 tokens > 90.
        for i in range(20):
            _add(ctx, {"role": "user", "content": f"x{i}" * 8})
        assert len(calls) >= 1, "summariser must have been invoked"

    def test_skipped_below_threshold(self, tmp_path):
        """Small cache (well under threshold) doesn't trigger."""
        ctx, calls = _make_ctx(tmp_path, max_context_tokens=10_000)
        _add(ctx, {"role": "system", "content": "sys"})
        _add(ctx, {"role": "user", "content": "small"})
        assert calls == []
        assert ctx.summary == ""


# ── 2. Split ─────────────────────────────────────────


class TestSplitForCompaction:
    def test_anchor_is_system_only(self, tmp_path):
        """First user query is NOT an anchor (RFC FR-CC-4) — only the
        system prompt survives compaction unconditionally."""
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=10_000)
        ctx._cache = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first query"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "b"},
        ]
        anchor, evict, retained = ctx._split_for_compaction()
        assert [m["role"] for m in anchor] == ["system"]
        # First user query is in evict, last message stays in retained.
        assert evict[0]["content"] == "first query"
        assert retained[-1]["content"] == "b"

    def test_no_system_prompt_means_no_anchor(self, tmp_path):
        """When there's no system message, the anchor is empty and
        everything is dynamic."""
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=10_000)
        ctx._cache = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        anchor, evict, retained = ctx._split_for_compaction()
        assert anchor == []
        assert len(evict) >= 1


# ── 3. Summary ───────────────────────────────────────


class TestSummary:
    def test_first_compaction_stores_summary(self, tmp_path):
        ctx, calls = _make_ctx(tmp_path, max_context_tokens=80, summary="first-summary")
        _add(ctx, {"role": "system", "content": "sys"})
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})
        assert "first-summary" in ctx.summary

    def test_recursive_summarisation_passes_prior_context(self, tmp_path):
        """Second compaction: the input messages to the callback MUST
        include the prior summary as a context message (single-call
        recursive design — no separate merge step)."""
        ctx, calls = _make_ctx(tmp_path, max_context_tokens=80)
        _add(ctx, {"role": "system", "content": "sys"})
        # Force first compaction.
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})
        assert len(calls) >= 1
        first_call = calls[0]
        # First call has no "Running summary" header.
        assert not any(
            "Running summary of earlier conversation" in m.get("content", "")
            for m in first_call
        )

        # Force second compaction.
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "y" * 30})
        assert len(calls) >= 2
        second_call = calls[1]
        assert any(
            "Running summary of earlier conversation" in m.get("content", "")
            for m in second_call
        )

    def test_summary_truncated_at_cap(self, tmp_path):
        ctx, _ = _make_ctx(
            tmp_path,
            max_context_tokens=80,
            summary="z" * 100_000,
        )
        _add(ctx, {"role": "system", "content": "sys"})
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})
        from agent_cli.context.manager import _SUMMARY_CHAR_CAP

        assert len(ctx.summary) == _SUMMARY_CHAR_CAP

    def test_callback_receives_chat_ready_messages(self, tmp_path):
        """ContextManager → compactor callback contract: every message
        the callback receives MUST be in chat-ready ``{role, content}``
        form. Raw history-record keys (``tool``, ``args``, ``thought``,
        ``action``, ``action_input``) MUST NOT leak through, because the
        downstream provider only understands ``role + content``.

        Regression guard for the omlx live-test bug: raw evict_set was
        being passed straight to the provider, which silently dropped
        the unknown keys and produced corrupt summaries."""
        ctx, calls = _make_ctx(tmp_path, max_context_tokens=120)
        _add(ctx, {"role": "system", "content": "sys"})
        # Mix of user + tool result + assistant action — exercises every
        # branch of _to_natural_language.
        _add(ctx, {"role": "user", "content": "first question"})
        _add(
            ctx,
            {
                "role": "user",
                "tool": "write_file",
                "args": {"path": "a.py"},
                "content": "ok",
            },
        )
        _add(
            ctx,
            {
                "role": "assistant",
                "thought": "I will read it",
                "action": "read_file",
                "action_input": {"path": "a.py"},
            },
        )
        # Pad to force compaction.
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})

        assert len(calls) >= 1, "compactor must have been invoked"
        forbidden_keys = {"tool", "args", "thought", "action", "action_input"}
        for callback_invocation in calls:
            for m in callback_invocation:
                assert set(m.keys()) <= {"role", "content"}, (
                    f"callback received message with unexpected keys: "
                    f"{set(m.keys()) - {'role', 'content'}} in {m!r}"
                )
                assert not (forbidden_keys & set(m.keys())), (
                    f"raw history keys leaked into callback: "
                    f"{forbidden_keys & set(m.keys())} in {m!r}"
                )
                assert isinstance(m.get("content"), str), (
                    f"callback message content must be a string: {m!r}"
                )


# ── 4. File list ─────────────────────────────────────


class TestFileList:
    def test_paths_accumulate_across_compactions(self, tmp_path):
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=80)
        _add(ctx, {"role": "system", "content": "sys"})
        _add(
            ctx,
            {
                "role": "user",
                "tool": "write_file",
                "args": {"path": "a.py"},
                "content": "written",
            },
        )
        # Add padding to force compaction.
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})
        assert "a.py" in ctx.file_list

    def test_dedup_across_compactions(self, tmp_path):
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=80)
        ctx._file_list = ["a.py"]
        # Trigger compaction with the same path again.
        ctx._cache = [
            {"role": "system", "content": "sys"},
            {
                "role": "user",
                "tool": "write_file",
                "args": {"path": "a.py"},
                "content": "...",
            },
        ]
        ctx._cache_tokens = 1000
        ctx._compact()
        assert ctx.file_list.count("a.py") == 1


class TestFileExtractHelper:
    """Direct tests for ``_file_extract.extract_file_paths``."""

    def test_extracts_from_tool_result(self):
        msgs = [
            {
                "role": "user",
                "tool": "write_file",
                "args": {"path": "foo.py"},
                "content": "ok",
            },
            {
                "role": "user",
                "tool": "read_file",
                "args": {"path": "bar.py"},
                "content": "...",
            },
        ]
        assert extract_file_paths(msgs) == ["foo.py", "bar.py"]

    def test_extracts_from_assistant_action(self):
        msgs = [
            {
                "role": "assistant",
                "action": "code_index",
                "action_input": {"mode": "list", "path": "x.py"},
            },
        ]
        assert extract_file_paths(msgs) == ["x.py"]

    def test_skips_shell_commands(self):
        msgs = [
            {
                "role": "user",
                "tool": "shell",
                "args": {"command": "rm foo.py"},
                "content": "ok",
            },
        ]
        assert extract_file_paths(msgs) == []

    def test_delegate_records_agent_placeholder(self):
        msgs = [
            {
                "role": "assistant",
                "action": "delegate",
                "action_input": {"tasks": [{"agent": "explorer", "task": "find X"}]},
            },
        ]
        assert extract_file_paths(msgs) == ["<delegate:explorer>"]

    def test_dedup(self):
        msgs = [
            {
                "role": "user",
                "tool": "read_file",
                "args": {"path": "a.py"},
                "content": "",
            },
            {
                "role": "user",
                "tool": "read_file",
                "args": {"path": "a.py"},
                "content": "",
            },
        ]
        assert extract_file_paths(msgs) == ["a.py"]


# ── 5. get_messages prepend ──────────────────────────


class TestGetMessagesPrepend:
    def test_summary_prepended_after_system(self, tmp_path):
        ctx = ContextManager(tmp_path / "s", max_context_tokens=1000)
        ctx._summary = "user did X then Y"
        ctx._cache = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "now"},
        ]
        msgs = ctx.get_messages()
        assert msgs[0]["role"] == "system"
        assert "## Summary of earlier conversation" in msgs[1]["content"]
        assert "user did X then Y" in msgs[1]["content"]
        assert msgs[-1]["content"] == "now"

    def test_file_list_prepended_after_summary(self, tmp_path):
        ctx = ContextManager(tmp_path / "s", max_context_tokens=1000)
        ctx._summary = "..."
        ctx._file_list = ["a.py", "b.py"]
        ctx._cache = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "now"},
        ]
        msgs = ctx.get_messages()
        files_msg = next(m for m in msgs if "Files touched" in m["content"])
        assert "- a.py" in files_msg["content"]
        assert "- b.py" in files_msg["content"]

    def test_no_summary_means_passthrough(self, tmp_path):
        """Without a summary the output matches the legacy contract."""
        ctx = ContextManager(tmp_path / "s", max_context_tokens=1000)
        ctx._cache = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        msgs = ctx.get_messages()
        assert len(msgs) == 2
        assert all("Summary" not in m["content"] for m in msgs)


# ── force_fit (reactive overflow recovery, flow 2) ───


def _fill(ctx, n: int, *, system: bool = True):
    """Add ``n`` user messages (plus an optional leading system msg)."""
    if system:
        _add(ctx, {"role": "system", "content": "sys"})
    for i in range(n):
        _add(ctx, {"role": "user", "content": f"m{i} " * 8})


class TestEvictFifoTarget:
    def test_target_param_sheds_more_than_default(self, tmp_path):
        """A smaller target_tokens evicts more aggressively than the
        default (max_context_tokens)."""
        ctx, _ = _make_ctx(
            tmp_path, max_context_tokens=1_000_000, compaction_enabled=False
        )
        _fill(ctx, 20)
        ctx._evict_fifo(target_tokens=30)
        assert ctx._cache_tokens <= 30 or len(ctx._cache) == 1

    def test_default_target_is_budget(self, tmp_path):
        """No arg → drops to max_context_tokens (legacy behaviour)."""
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=40, compaction_enabled=False)
        _fill(ctx, 20)
        ctx._evict_fifo()
        assert ctx._cache_tokens <= 40 or len(ctx._cache) == 1


class TestForceFit:
    def test_ratio_shrink_with_actual_and_target(self, tmp_path):
        """keep_ratio = target/actual; cache estimate drops to that
        fraction (FIFO path, compaction disabled)."""
        ctx, _ = _make_ctx(
            tmp_path, max_context_tokens=1_000_000, compaction_enabled=False
        )
        _fill(ctx, 20)
        before_tokens = ctx._cache_tokens
        before_len = len(ctx._cache)
        # Server: actual=1000, target=500 → keep half.
        shrunk = ctx.force_fit(target_tokens=500, actual_tokens=1000)
        assert shrunk is True
        assert len(ctx._cache) < before_len
        assert ctx._cache_tokens <= before_tokens * 0.5

    def test_returns_false_when_only_anchor(self, tmp_path):
        """A single-message cache can't shrink → False, message kept."""
        ctx, _ = _make_ctx(
            tmp_path, max_context_tokens=1_000_000, compaction_enabled=False
        )
        _add(ctx, {"role": "user", "content": "only"})
        assert ctx.force_fit(target_tokens=1, actual_tokens=100) is False
        assert len(ctx._cache) == 1

    def test_forward_progress_when_estimate_underflows(self, tmp_path):
        """Even when the local estimate is absurdly low (CJK under-count),
        force_fit removes at least one oldest message so the retry can
        make headway."""
        ctx, _ = _make_ctx(
            tmp_path, max_context_tokens=1_000_000, compaction_enabled=False
        )
        _fill(ctx, 10)
        before_len = len(ctx._cache)
        # Simulate a severe under-estimate: estimate says 0 tokens, but the
        # server rejected a 500-token prompt.
        ctx._cache_tokens = 0
        shrunk = ctx.force_fit(target_tokens=100, actual_tokens=500)
        assert shrunk is True
        assert len(ctx._cache) < before_len

    def test_no_actual_trims_fraction(self, tmp_path):
        """Without a server count, force_fit trims ~25% and lets the
        retry loop converge."""
        ctx, _ = _make_ctx(
            tmp_path, max_context_tokens=1_000_000, compaction_enabled=False
        )
        _fill(ctx, 20)
        before_tokens = ctx._cache_tokens
        shrunk = ctx.force_fit(target_tokens=999_999, actual_tokens=None)
        assert shrunk is True
        assert ctx._cache_tokens <= before_tokens * 0.75

    def test_compaction_attempted_before_fifo(self, tmp_path):
        """When compaction is enabled, force_fit summarises first."""
        ctx, calls = _make_ctx(
            tmp_path, max_context_tokens=1_000_000, compaction_enabled=True
        )
        _fill(ctx, 20)
        ctx.force_fit(target_tokens=10, actual_tokens=1_000_000)
        assert len(calls) >= 1, "summariser should run before FIFO"

    def test_compaction_disabled_uses_fifo_only(self, tmp_path):
        """compaction_enabled=False → no summariser, pure FIFO."""
        ctx, calls = _make_ctx(
            tmp_path, max_context_tokens=1_000_000, compaction_enabled=False
        )
        _fill(ctx, 20)
        before_len = len(ctx._cache)
        ctx.force_fit(target_tokens=10, actual_tokens=1_000)
        assert calls == []
        assert len(ctx._cache) < before_len

    def test_never_empties_cache_preserves_most_recent(self, tmp_path):
        """Shedding everything possible still leaves the most recent
        message as the anchor."""
        ctx, _ = _make_ctx(
            tmp_path, max_context_tokens=1_000_000, compaction_enabled=False
        )
        _add(ctx, {"role": "system", "content": "sys"})
        for i in range(10):
            _add(ctx, {"role": "user", "content": f"m{i} " * 8})
        ctx.force_fit(target_tokens=0, actual_tokens=1_000_000)
        assert len(ctx._cache) >= 1
        assert ctx._cache[-1]["content"].startswith("m9")


# ── 6. Fallback ──────────────────────────────────────


class TestFallback:
    def test_llm_failure_falls_back_to_fifo(self, tmp_path):
        """A summariser exception triggers ``CompactionError`` and the
        belt-and-braces FIFO drop. Cache ends up within the budget."""
        ctx = ContextManager(tmp_path / "s", max_context_tokens=80)
        ctx.set_compactor(lambda msgs: (_ for _ in ()).throw(RuntimeError("boom")))
        _add(ctx, {"role": "system", "content": "sys"})
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})
        assert ctx._cache_tokens <= ctx.max_context_tokens
        # Summary stays empty because LLM never produced one.
        assert ctx.summary == ""

    def test_failed_compaction_retries_on_next_add(self, tmp_path):
        """First trigger: callback raises → FIFO. Subsequent add can
        still exceed threshold (depending on message sizes) and call
        the compactor again — no internal retry counter blocks it."""
        ctx = ContextManager(tmp_path / "s", max_context_tokens=80)
        call_count = [0]

        def sometimes_failing(msgs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("transient")
            return "later success"

        ctx.set_compactor(sometimes_failing)
        _add(ctx, {"role": "system", "content": "sys"})
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})
        # First call raised.
        assert call_count[0] >= 1
        # Add more to exceed threshold again.
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "y" * 30})
        assert call_count[0] >= 2
        assert "later success" in ctx.summary

    def test_belt_and_braces_when_summary_does_not_shrink_enough(self, tmp_path):
        """If the rebuilt cache (anchor + summary + retained) is itself
        over threshold (small budget + large summary), the FIFO
        fallback still brings it down. No infinite trigger."""
        # Budget tiny so even the summary dominates.
        ctx = ContextManager(tmp_path / "s", max_context_tokens=40)
        ctx.set_compactor(lambda msgs: "S" * 500)  # forces big summary
        _add(ctx, {"role": "system", "content": "sys"})
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})
        # Final cache fits.
        assert ctx._cache_tokens <= ctx.max_context_tokens


# ── 7. Persistence ───────────────────────────────────


class TestPersistence:
    def test_compaction_json_schema(self, tmp_path):
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=80, summary="persisted")
        _add(ctx, {"role": "system", "content": "sys"})
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})
        comp = ctx.session_dir / "compaction.json"
        assert comp.is_file()
        data = json.loads(comp.read_text())
        assert data["version"] == 1
        assert "persisted" in data["summary"]
        assert isinstance(data["file_list"], list)
        assert data["compaction_count"] >= 1
        assert data["last_compacted_at"]
        assert data["dynamic_start_index"] >= 1

    def test_resume_restores_state(self, tmp_path):
        sdir = tmp_path / "s"
        sdir.mkdir(parents=True)
        (sdir / "compaction.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "summary": "prev",
                    "file_list": ["x.py"],
                    "compaction_count": 2,
                    "last_compacted_at": "2026-01-01T00:00:00Z",
                    "dynamic_start_index": 5,
                }
            )
        )
        (sdir / "history.jsonl").write_text(
            "\n".join(
                json.dumps({"role": "user", "content": f"msg{i}"}) for i in range(10)
            )
        )
        ctx = ContextManager(sdir, max_context_tokens=10_000, resume=True)
        assert ctx.summary == "prev"
        assert ctx.file_list == ["x.py"]
        assert ctx.compaction_count == 2
        # Only messages 5-9 should be in the cache (forward load).
        assert len(ctx._cache) == 5
        assert ctx._cache[0]["content"] == "msg5"

    def test_no_compaction_json_starts_empty(self, tmp_path):
        sdir = tmp_path / "s"
        sdir.mkdir(parents=True)
        (sdir / "history.jsonl").write_text("")
        ctx = ContextManager(sdir, max_context_tokens=1000, resume=True)
        assert ctx.summary == ""
        assert ctx.file_list == []
        assert ctx.compaction_count == 0

    def test_version_mismatch_ignored(self, tmp_path):
        sdir = tmp_path / "s"
        sdir.mkdir(parents=True)
        (sdir / "compaction.json").write_text(
            json.dumps({"version": 99, "summary": "ignored"})
        )
        (sdir / "history.jsonl").write_text("")
        ctx = ContextManager(sdir, max_context_tokens=1000, resume=True)
        # Unknown future version → cleared state.
        assert ctx.summary == ""

    def test_invalid_dynamic_start_index_falls_back(self, tmp_path):
        sdir = tmp_path / "s"
        sdir.mkdir(parents=True)
        (sdir / "compaction.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "summary": "keep",
                    "file_list": [],
                    "compaction_count": 1,
                    "last_compacted_at": "x",
                    "dynamic_start_index": 999,  # bigger than history
                }
            )
        )
        (sdir / "history.jsonl").write_text(
            json.dumps({"role": "user", "content": "only"}) + "\n"
        )
        ctx = ContextManager(sdir, max_context_tokens=1000, resume=True)
        # Summary still loaded.
        assert ctx.summary == "keep"
        # Cache loaded via legacy reverse-load (since offset invalid).
        assert len(ctx._cache) == 1


# ── 8. CLI flag / env var ────────────────────────────


class TestCompactionToggle:
    def test_disabled_constructor_flag_falls_through_to_fifo(self, tmp_path):
        ctx = ContextManager(
            tmp_path / "s",
            max_context_tokens=80,
            compaction_enabled=False,
        )
        called = []
        ctx.set_compactor(lambda msgs: called.append(msgs) or "should-not-be-used")
        _add(ctx, {"role": "system", "content": "sys"})
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})
        # Callback never invoked — FIFO did the work.
        assert called == []
        # Cache still under budget.
        assert ctx._cache_tokens <= ctx.max_context_tokens

    def test_env_var_off_overrides_constructor_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_COMPACTION", "off")
        ctx = ContextManager(
            tmp_path / "s",
            max_context_tokens=80,
            compaction_enabled=True,  # constructor says ON
        )
        called = []
        ctx.set_compactor(lambda msgs: called.append(msgs) or "X")
        _add(ctx, {"role": "system", "content": "sys"})
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})
        # Env var off wins → FIFO instead.
        assert called == []


# ── 9. TurnRecorder integration ──────────────────────


class TestRecorderIntegration:
    def test_compaction_event_recorded(self, tmp_path):
        from agent_cli.recovery.observability import TurnRecorder

        sdir = tmp_path / "s"
        sdir.mkdir(parents=True)
        recorder = TurnRecorder(sdir, enabled=True)
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=80)
        ctx.session_dir = sdir
        # Update history path to live alongside turns.jsonl.
        ctx._history_path = sdir / "history.jsonl"
        ctx._compaction_path = sdir / "compaction.json"
        ctx.set_recorder(recorder)
        _add(ctx, {"role": "system", "content": "sys"})
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})
        turns = sdir / "turns.jsonl"
        assert turns.is_file()
        rows = [json.loads(line) for line in turns.read_text().splitlines() if line]
        compaction_rows = [r for r in rows if r.get("event") == "compaction"]
        assert len(compaction_rows) >= 1
        first = compaction_rows[0]
        assert first["tokens_before"] > first["tokens_after"]
        assert first["evicted_count"] > 0
        assert "duration_ms" in first
        assert first["failure_signal"] is None
        assert first["fallback_used"] is False

    def test_failure_recorded_with_signal(self, tmp_path):
        from agent_cli.recovery.observability import TurnRecorder

        sdir = tmp_path / "s"
        sdir.mkdir(parents=True)
        recorder = TurnRecorder(sdir, enabled=True)
        ctx = ContextManager(sdir, max_context_tokens=80)
        ctx.set_compactor(lambda msgs: (_ for _ in ()).throw(RuntimeError("x")))
        ctx.set_recorder(recorder)
        _add(ctx, {"role": "system", "content": "sys"})
        for _ in range(15):
            _add(ctx, {"role": "user", "content": "x" * 30})
        rows = [
            json.loads(line)
            for line in (sdir / "turns.jsonl").read_text().splitlines()
            if line
        ]
        compaction_rows = [r for r in rows if r.get("event") == "compaction"]
        assert len(compaction_rows) >= 1
        first = compaction_rows[0]
        assert first["failure_signal"] == "summary_failed"
        assert first["fallback_used"] is True


# ── 10. render_compaction_progress invariant ─────────


class TestRenderHelper:
    def test_helper_routes_through_renderer_status(self, monkeypatch):
        """The helper must call ``_renderer.status`` exactly once per
        phase, with the right level. Pins the single-entry-point
        invariant the RFC mandates."""
        from agent_cli.render import render_compaction_progress

        captured: list[tuple[str, str]] = []

        def fake_status(level, msg, turn=0):
            captured.append((level, msg))

        import agent_cli.render as _r

        monkeypatch.setattr(_r._renderer, "status", fake_status)

        render_compaction_progress(phase="start", old_tokens=1000, evicted_count=5)
        render_compaction_progress(phase="done", old_tokens=1000, new_tokens=400)
        render_compaction_progress(phase="warning", reason="provider down")

        levels = [c[0] for c in captured]
        assert levels == ["info", "info", "warning"]
        assert "Compacting context" in captured[0][1]
        assert "1,000" in captured[0][1]
        assert "Compaction done" in captured[1][1]
        assert "provider down" in captured[2][1]

    def test_unknown_phase_is_silent(self, monkeypatch):
        """Typo'd phase must NOT raise — UX path shouldn't break the
        agent loop. Silent no-op is safer."""
        from agent_cli.render import render_compaction_progress

        captured = []
        import agent_cli.render as _r

        monkeypatch.setattr(_r._renderer, "status", lambda *a, **kw: captured.append(a))
        render_compaction_progress(phase="bogus")  # must not raise
        assert captured == []


# Concurrent ``add`` safety is intentionally NOT tested in v1 —
# ContextManager doesn't claim full thread-safety (compaction.json
# write and history.jsonl append both race under load). The
# AgentLoop's worker pattern serialises adds in practice; stricter
# guarantees would need an internal lock and belong to a follow-up.


# ── 11. Empty cache edge ─────────────────────────────


def test_compact_with_empty_dynamic_is_noop(tmp_path):
    """Trigger conditions with only a system message (no dynamic) →
    nothing to evict, so no callback invocation, no state change."""
    ctx, calls = _make_ctx(tmp_path, max_context_tokens=10)
    # Force the threshold via a single huge system message — but split
    # returns empty evict so _compact() returns immediately.
    ctx._cache = [{"role": "system", "content": "x" * 1000}]
    ctx._cache_tokens = 1000
    ctx._compact()
    assert calls == []
    assert ctx.summary == ""


# ── 12. AgentLoop._llm_compact_summarize integration ─


class TestAgentLoopCompactorCallback:
    """Cover the AgentLoop side of the compaction wire — the previous
    suite mocked the callback entirely, so ``_llm_compact_summarize``
    was 0% covered. Regression guard for the omlx live-test bugs:

      (1) provider.call() called with wrong signature (missing
          ``system`` / ``capabilities``).
      (2) capabilities not overridden, so ``supports_structured_output=
          True`` forced a JSON-mode response instead of plain text.
    """

    def _make_loop(self, **overrides):
        """Build a minimum-viable AgentLoop instance that exercises
        ``_llm_compact_summarize`` without dragging in the full run."""
        from agent_cli.loop import AgentLoop
        from agent_cli.providers.base import LLMResponse
        from agent_cli.providers.capabilities import ModelCapabilities

        received: dict = {}

        class FakeProvider:
            def call(self, messages, system, model, capabilities, **kwargs):
                received["messages"] = messages
                received["system"] = system
                received["model"] = model
                received["capabilities"] = capabilities
                received["kwargs"] = kwargs
                return LLMResponse(content="MOCK SUMMARY", thinking="")

        caps = ModelCapabilities(
            context_window=4096,
            max_output_tokens=512,
            supports_structured_output=True,  # default-on; the callback
            #                                  must flip it off
            supports_thinking=True,  # ditto
            thinking_budget=2048,
            supports_strict_schema=False,
        )
        loop = AgentLoop(
            query="x",
            provider=FakeProvider(),
            capabilities=caps,
            model="mock-model",
        )
        return loop, received

    def test_provider_call_signature_is_correct(self):
        """Reproduces Issue B: previously the callback called
        ``provider.call(messages=..., model=..., on_chunk=...)`` and the
        provider raised ``TypeError: missing positional arguments
        'system' and 'capabilities'``."""
        loop, received = self._make_loop()
        result = loop._llm_compact_summarize([{"role": "user", "content": "hello"}])
        assert result == "MOCK SUMMARY"
        # All four required positional args were forwarded.
        assert received["messages"] == [{"role": "user", "content": "hello"}]
        assert isinstance(received["system"], str)
        assert received["system"]  # non-empty summarisation prompt
        assert received["model"] == "mock-model"
        assert received["capabilities"] is not None

    def test_capabilities_disable_structured_output_and_thinking(self):
        """Capabilities passed to the provider for the summary call MUST
        have ``supports_structured_output=False`` (no JSON mode forced)
        and ``supports_thinking=False`` (no reasoning trace eating the
        response budget). The agent-loop's normal capabilities stay
        untouched."""
        loop, received = self._make_loop()
        loop._llm_compact_summarize([{"role": "user", "content": "x"}])

        passed_caps = received["capabilities"]
        assert passed_caps.supports_structured_output is False
        assert passed_caps.supports_thinking is False
        # Original capabilities untouched (frozen dataclass — replace
        # returns a new instance, doesn't mutate).
        assert loop.capabilities.supports_structured_output is True
        assert loop.capabilities.supports_thinking is True


# ── 13. CompactionError import sanity ────────────────


def test_compaction_error_is_exception():
    assert issubclass(CompactionError, RuntimeError)


class TestReconcileActualTokens:
    """flow 1 (part B): re-anchor cache token count to the server's
    actual input count."""

    def test_anchors_to_server_count_minus_system(self, tmp_path):
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=1_000_000)
        ctx._cache_tokens = 50  # bogus chars/4 estimate
        ctx.reconcile_actual_tokens(500, system_tokens=120)
        assert ctx._cache_tokens == 380  # 500 (system+messages) − 120 system

    def test_zero_actual_is_noop(self, tmp_path):
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=1_000_000)
        ctx._cache_tokens = 50
        ctx.reconcile_actual_tokens(0)
        assert ctx._cache_tokens == 50  # provider had no usage → estimate kept

    def test_system_larger_than_actual_floors_at_zero(self, tmp_path):
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=1_000_000)
        ctx.reconcile_actual_tokens(100, system_tokens=200)
        assert ctx._cache_tokens == 0

    def test_default_system_tokens_zero(self, tmp_path):
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=1_000_000)
        ctx.reconcile_actual_tokens(300)
        assert ctx._cache_tokens == 300


class TestEnsureWithin:
    """flow 1 (part A): preventive compaction toward an explicit target."""

    def test_noop_when_under_target(self, tmp_path):
        ctx, calls = _make_ctx(tmp_path, max_context_tokens=1_000_000)
        ctx.add({"role": "user", "content": "small"})
        ctx.ensure_within(1_000_000)
        assert calls == []  # nothing compacted
        assert len(ctx.get_raw_messages()) == 1

    def test_compacts_when_over_target(self, tmp_path):
        ctx, calls = _make_ctx(tmp_path, max_context_tokens=1_000_000)
        for i in range(20):
            ctx.add({"role": "user", "content": f"m{i} " * 10})
        ctx.ensure_within(20)  # tiny target → must summarise
        assert len(calls) >= 1

    def test_fifo_when_compaction_disabled(self, tmp_path):
        ctx, calls = _make_ctx(
            tmp_path, max_context_tokens=1_000_000, compaction_enabled=False
        )
        for i in range(20):
            ctx.add({"role": "user", "content": f"m{i} " * 10})
        before = len(ctx.get_raw_messages())
        ctx.ensure_within(20)
        assert calls == []  # no summariser
        assert len(ctx.get_raw_messages()) < before  # FIFO shed


class TestCompactNow:
    """Manual compaction trigger (the /compact command)."""

    def test_compacts_and_reports_before_after(self, tmp_path):
        ctx, calls = _make_ctx(tmp_path, max_context_tokens=1_000_000)
        ctx.add({"role": "system", "content": "sys"})
        for i in range(20):
            ctx.add({"role": "user", "content": f"m{i} " * 10})
        before, after = ctx.compact_now()
        assert len(calls) >= 1  # summariser ran
        assert after < before  # cache shrank

    def test_noop_when_nothing_to_evict(self, tmp_path):
        ctx, calls = _make_ctx(tmp_path, max_context_tokens=1_000_000)
        ctx.add({"role": "system", "content": "sys"})
        before, after = ctx.compact_now()
        assert before == after  # only anchor — nothing old enough to evict
        assert calls == []

    def test_noop_when_compaction_disabled(self, tmp_path):
        ctx, calls = _make_ctx(
            tmp_path, max_context_tokens=1_000_000, compaction_enabled=False
        )
        for i in range(20):
            ctx.add({"role": "user", "content": f"m{i} " * 10})
        before, after = ctx.compact_now()
        assert before == after  # disabled → no-op, no FIFO drop
        assert calls == []
