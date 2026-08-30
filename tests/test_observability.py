"""Tests for the per-turn observability layer (TurnRecord + TurnRecorder).

The recorder is a small, append-only JSONL writer scoped to a session
directory. Tests cover:
- Disabled paths (no session_dir, opt-out flag)
- Schema fidelity (fields written match the dataclass)
- Sequence monotonicity
- Failure-signal labels
- Crash-tolerance assumptions (one line per record, complete or absent)

See docs/robust-harness/DESIGN.md §3.3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.providers.base import TokenUsage
from agent_cli.recovery.observability import (
    FAILURE_NO_ACTION,
    FAILURE_NO_JSON,
    TurnRecord,
    TurnRecorder,
)


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    d = tmp_path / "session-1234"
    d.mkdir()
    return d


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestTurnRecorderDisabled:
    def test_no_session_dir_is_no_op(self, tmp_path):
        recorder = TurnRecorder(session_dir=None, enabled=True)
        assert not recorder.enabled
        recorder.record(model="m", parse_stage=1)  # must not raise
        # Nothing should have been written anywhere
        assert list(tmp_path.glob("**/*.jsonl")) == []

    def test_opt_out_is_no_op(self, session_dir):
        recorder = TurnRecorder(session_dir=session_dir, enabled=False)
        assert not recorder.enabled
        recorder.record(model="m", parse_stage=1)
        assert _read_jsonl(session_dir / "turns.jsonl") == []

    def test_record_when_disabled_does_not_create_file(self, session_dir):
        recorder = TurnRecorder(session_dir=session_dir, enabled=False)
        recorder.record(model="m", parse_stage=0, failure_signal=FAILURE_NO_JSON)
        assert not (session_dir / "turns.jsonl").exists()


class TestTurnRecorderEnabled:
    def test_enabled_when_session_dir_and_flag(self, session_dir):
        recorder = TurnRecorder(session_dir=session_dir, enabled=True)
        assert recorder.enabled

    def test_records_a_success_row(self, session_dir):
        recorder = TurnRecorder(session_dir=session_dir, enabled=True)
        recorder.record(model="qwen3.5", parse_stage=1)
        rows = _read_jsonl(session_dir / "turns.jsonl")
        assert len(rows) == 1
        row = rows[0]
        assert row["model"] == "qwen3.5"
        assert row["parse_stage"] == 1
        assert row["failure_signal"] is None
        assert row["primitives_applied"] == []
        # Timestamp is present and ISO 8601-ish
        assert "T" in row["timestamp"]

    def test_records_a_failure_row_with_primitives(self, session_dir):
        recorder = TurnRecorder(session_dir=session_dir, enabled=True)
        recorder.record(
            model="m",
            parse_stage=0,
            failure_signal=FAILURE_NO_JSON,
            primitives_applied=["echo_prior_output", "constrain_format_json"],
        )
        rows = _read_jsonl(session_dir / "turns.jsonl")
        assert len(rows) == 1
        row = rows[0]
        assert row["failure_signal"] == FAILURE_NO_JSON
        assert row["primitives_applied"] == [
            "echo_prior_output",
            "constrain_format_json",
        ]

    def test_appends_across_record_calls(self, session_dir):
        """Each record() call must add exactly one line — no buffering,
        no batched flush. This is the contract crash-tolerance relies on."""
        recorder = TurnRecorder(session_dir=session_dir, enabled=True)
        recorder.record(model="m", parse_stage=1)
        # File should be readable already (no buffering)
        rows1 = _read_jsonl(session_dir / "turns.jsonl")
        recorder.record(model="m", parse_stage=0, failure_signal=FAILURE_NO_ACTION)
        rows2 = _read_jsonl(session_dir / "turns.jsonl")
        assert len(rows1) == 1
        assert len(rows2) == 2

    def test_records_accumulate_across_recorder_instances(self, session_dir):
        """A fresh TurnRecorder per run_loop call (web spawns one per user
        message) appends to the SAME session turns.jsonl — rows accumulate
        across instances. There is no per-row counter: row ordering is by
        timestamp, not seq (seq was removed — it was run-local and collided
        across run_loop invocations)."""
        a = TurnRecorder(session_dir=session_dir, enabled=True)
        b = TurnRecorder(session_dir=session_dir, enabled=True)
        a.record(model="m", parse_stage=1)
        b.record(model="m", parse_stage=1)
        rows = _read_jsonl(session_dir / "turns.jsonl")
        assert len(rows) == 2
        assert all("seq" not in r for r in rows)
        assert all("timestamp" in r for r in rows)

    def test_record_recreates_session_dir_if_removed(self, session_dir):
        """Same parallel-delegate cleanup race that hits
        ContextManager._append_to_history: if the session dir gets
        wiped between recorder construction and the first record()
        call (external `rm -rf .agent-cli/sessions/`), record() must
        defensively re-mkdir rather than crash."""
        import shutil

        recorder = TurnRecorder(session_dir=session_dir, enabled=True)
        shutil.rmtree(session_dir)
        assert not session_dir.is_dir()
        recorder.record(model="m", parse_stage=0)
        assert (session_dir / "turns.jsonl").is_file()

    def test_record_compaction_recreates_session_dir_if_removed(self, session_dir):
        """Same guard for the compaction event path."""
        import shutil

        recorder = TurnRecorder(session_dir=session_dir, enabled=True)
        shutil.rmtree(session_dir)
        recorder.record_compaction(
            tokens_before=1000,
            tokens_after=500,
            evicted_count=4,
            fallback_used=False,
        )
        assert (session_dir / "turns.jsonl").is_file()


class TestSchemaInvariants:
    def test_record_omits_no_prompt_or_response_text(self, session_dir):
        """Privacy contract: no LLM-generated content or user input
        appears in TurnRecord. Only structural metadata."""
        recorder = TurnRecorder(session_dir=session_dir, enabled=True)
        recorder.record(
            model="m",
            parse_stage=0,
            failure_signal=FAILURE_NO_JSON,
            primitives_applied=["echo_prior_output"],
        )
        rows = _read_jsonl(session_dir / "turns.jsonl")
        # Exactly the expected keys, nothing else
        assert set(rows[0].keys()) == {
            "model",
            "timestamp",
            "parse_stage",
            "failure_signal",
            "primitives_applied",
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        }

    def test_failure_signals_are_stable_strings(self):
        # Constants are part of the public schema — anything reading
        # turns.jsonl must be able to grep for these. Don't rename
        # without coordinated migration.
        assert FAILURE_NO_JSON == "NO_JSON"
        assert FAILURE_NO_ACTION == "NO_ACTION"

    def test_dataclass_default_primitives_is_empty_list(self):
        rec = TurnRecord(model="m", timestamp="t", parse_stage=1)
        assert rec.primitives_applied == []
        assert rec.failure_signal is None


class TestTokenUsage:
    """Per-turn provider usage rides on the record (v8.49.0) — the only
    on-disk source for session cost, so field names/semantics mirror
    ``TokenUsage`` verbatim."""

    def test_usage_fields_copied_verbatim(self, session_dir):
        recorder = TurnRecorder(session_dir=session_dir, enabled=True)
        recorder.record(
            model="m",
            parse_stage=1,
            usage=TokenUsage(
                input_tokens=1200,
                output_tokens=45,
                cache_read_input_tokens=900,
                cache_creation_input_tokens=100,
            ),
        )
        row = _read_jsonl(session_dir / "turns.jsonl")[0]
        assert row["input_tokens"] == 1200
        assert row["output_tokens"] == 45
        assert row["cache_read_input_tokens"] == 900
        assert row["cache_creation_input_tokens"] == 100

    def test_no_usage_records_zero_counts(self, session_dir):
        # Providers/mocks without a usage block → zeros, never missing keys
        # (readers sum rows; a missing key would break the aggregate).
        recorder = TurnRecorder(session_dir=session_dir, enabled=True)
        recorder.record(model="m", parse_stage=1, usage=None)
        row = _read_jsonl(session_dir / "turns.jsonl")[0]
        assert (
            row["input_tokens"],
            row["output_tokens"],
            row["cache_read_input_tokens"],
            row["cache_creation_input_tokens"],
        ) == (0, 0, 0, 0)

    def test_session_totals_are_a_sum_over_rows(self, session_dir):
        recorder = TurnRecorder(session_dir=session_dir, enabled=True)
        for i in (1, 2, 3):
            recorder.record(
                model="m",
                parse_stage=1,
                usage=TokenUsage(input_tokens=10 * i, output_tokens=i),
            )
        rows = _read_jsonl(session_dir / "turns.jsonl")
        assert sum(r["input_tokens"] for r in rows) == 60
        assert sum(r["output_tokens"] for r in rows) == 6
