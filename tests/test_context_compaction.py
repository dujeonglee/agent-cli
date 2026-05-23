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


# ── 1. Trigger ───────────────────────────────────────


class TestCompactionTrigger:
    def test_triggers_above_90_percent(self, tmp_path):
        """``add`` fires ``_compact`` when cache > 0.9 * budget."""
        ctx, calls = _make_ctx(tmp_path, max_context_tokens=100)
        ctx.add({"role": "system", "content": "sys"})
        # ~10 tokens per message → 11 messages ~ 110 tokens > 90.
        for i in range(20):
            ctx.add({"role": "user", "content": f"x{i}" * 8})
        assert len(calls) >= 1, "summariser must have been invoked"

    def test_skipped_below_threshold(self, tmp_path):
        """Small cache (well under threshold) doesn't trigger."""
        ctx, calls = _make_ctx(tmp_path, max_context_tokens=10_000)
        ctx.add({"role": "system", "content": "sys"})
        ctx.add({"role": "user", "content": "small"})
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
        ctx.add({"role": "system", "content": "sys"})
        for _ in range(15):
            ctx.add({"role": "user", "content": "x" * 30})
        assert "first-summary" in ctx.summary

    def test_recursive_summarisation_passes_prior_context(self, tmp_path):
        """Second compaction: the input messages to the callback MUST
        include the prior summary as a context message (single-call
        recursive design — no separate merge step)."""
        ctx, calls = _make_ctx(tmp_path, max_context_tokens=80)
        ctx.add({"role": "system", "content": "sys"})
        # Force first compaction.
        for _ in range(15):
            ctx.add({"role": "user", "content": "x" * 30})
        assert len(calls) >= 1
        first_call = calls[0]
        # First call has no "Running summary" header.
        assert not any(
            "Running summary of earlier conversation" in m.get("content", "")
            for m in first_call
        )

        # Force second compaction.
        for _ in range(15):
            ctx.add({"role": "user", "content": "y" * 30})
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
        ctx.add({"role": "system", "content": "sys"})
        for _ in range(15):
            ctx.add({"role": "user", "content": "x" * 30})
        from agent_cli.context.manager import _SUMMARY_CHAR_CAP

        assert len(ctx.summary) == _SUMMARY_CHAR_CAP


# ── 4. File list ─────────────────────────────────────


class TestFileList:
    def test_paths_accumulate_across_compactions(self, tmp_path):
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=80)
        ctx.add({"role": "system", "content": "sys"})
        ctx.add(
            {
                "role": "user",
                "tool": "write_file",
                "args": {"path": "a.py"},
                "content": "written",
            }
        )
        # Add padding to force compaction.
        for _ in range(15):
            ctx.add({"role": "user", "content": "x" * 30})
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
                "action": "read_symbols",
                "action_input": {"path": "x.py", "mode": "list"},
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


# ── 6. Fallback ──────────────────────────────────────


class TestFallback:
    def test_llm_failure_falls_back_to_fifo(self, tmp_path):
        """A summariser exception triggers ``CompactionError`` and the
        belt-and-braces FIFO drop. Cache ends up within the budget."""
        ctx = ContextManager(tmp_path / "s", max_context_tokens=80)
        ctx.set_compactor(lambda msgs: (_ for _ in ()).throw(RuntimeError("boom")))
        ctx.add({"role": "system", "content": "sys"})
        for _ in range(15):
            ctx.add({"role": "user", "content": "x" * 30})
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
        ctx.add({"role": "system", "content": "sys"})
        for _ in range(15):
            ctx.add({"role": "user", "content": "x" * 30})
        # First call raised.
        assert call_count[0] >= 1
        # Add more to exceed threshold again.
        for _ in range(15):
            ctx.add({"role": "user", "content": "y" * 30})
        assert call_count[0] >= 2
        assert "later success" in ctx.summary

    def test_belt_and_braces_when_summary_does_not_shrink_enough(self, tmp_path):
        """If the rebuilt cache (anchor + summary + retained) is itself
        over threshold (small budget + large summary), the FIFO
        fallback still brings it down. No infinite trigger."""
        # Budget tiny so even the summary dominates.
        ctx = ContextManager(tmp_path / "s", max_context_tokens=40)
        ctx.set_compactor(lambda msgs: "S" * 500)  # forces big summary
        ctx.add({"role": "system", "content": "sys"})
        for _ in range(15):
            ctx.add({"role": "user", "content": "x" * 30})
        # Final cache fits.
        assert ctx._cache_tokens <= ctx.max_context_tokens


# ── 7. Persistence ───────────────────────────────────


class TestPersistence:
    def test_compaction_json_schema(self, tmp_path):
        ctx, _ = _make_ctx(tmp_path, max_context_tokens=80, summary="persisted")
        ctx.add({"role": "system", "content": "sys"})
        for _ in range(15):
            ctx.add({"role": "user", "content": "x" * 30})
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
        ctx.add({"role": "system", "content": "sys"})
        for _ in range(15):
            ctx.add({"role": "user", "content": "x" * 30})
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
        ctx.add({"role": "system", "content": "sys"})
        for _ in range(15):
            ctx.add({"role": "user", "content": "x" * 30})
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
        ctx.add({"role": "system", "content": "sys"})
        for _ in range(15):
            ctx.add({"role": "user", "content": "x" * 30})
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
        ctx.add({"role": "system", "content": "sys"})
        for _ in range(15):
            ctx.add({"role": "user", "content": "x" * 30})
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


# ── 13. CompactionError import sanity ────────────────


def test_compaction_error_is_exception():
    assert issubclass(CompactionError, RuntimeError)
