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
        assert row["seq"] == 0
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

    def test_seq_is_monotonic(self, session_dir):
        recorder = TurnRecorder(session_dir=session_dir, enabled=True)
        for _ in range(4):
            recorder.record(model="m", parse_stage=1)
        rows = _read_jsonl(session_dir / "turns.jsonl")
        assert [r["seq"] for r in rows] == [0, 1, 2, 3]

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

    def test_two_recorders_share_path_but_not_seq(self, session_dir):
        """Independent TurnRecorder instances each maintain their own
        seq counter. Crossing instances within one session would be a
        bug at the call-site — recorder is owned by AgentLoop."""
        a = TurnRecorder(session_dir=session_dir, enabled=True)
        b = TurnRecorder(session_dir=session_dir, enabled=True)
        a.record(model="m", parse_stage=1)
        b.record(model="m", parse_stage=1)
        rows = _read_jsonl(session_dir / "turns.jsonl")
        # Both wrote seq=0 because each instance counts independently
        assert [r["seq"] for r in rows] == [0, 0]


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
            "seq",
            "model",
            "timestamp",
            "parse_stage",
            "failure_signal",
            "primitives_applied",
        }

    def test_failure_signals_are_stable_strings(self):
        # Constants are part of the public schema — anything reading
        # turns.jsonl must be able to grep for these. Don't rename
        # without coordinated migration.
        assert FAILURE_NO_JSON == "NO_JSON"
        assert FAILURE_NO_ACTION == "NO_ACTION"

    def test_dataclass_default_primitives_is_empty_list(self):
        rec = TurnRecord(seq=0, model="m", timestamp="t", parse_stage=1)
        assert rec.primitives_applied == []
        assert rec.failure_signal is None
