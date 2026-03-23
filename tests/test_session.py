"""Tests for context/session.py — file-based session persistence."""

from __future__ import annotations


import pytest

import agent_cli.context.session as session_mod
from agent_cli.context.session import (
    append_log,
    create_session,
    find_latest_summary,
    get_log_path,
    get_summary_path,
    list_sessions,
    load_session,
    load_summary,
    read_log,
    save_meta,
    save_summary,
)


@pytest.fixture(autouse=True)
def _use_tmp_context_dir(tmp_path, monkeypatch):
    """Redirect context dir to temp for all tests."""
    monkeypatch.setattr(session_mod, "_CONTEXT_DIR", tmp_path)


class TestCreateSession:
    def test_creates_with_defaults(self):
        meta = create_session("/tmp/workspace")
        assert meta.workspace == "/tmp/workspace"
        assert meta.workspace_hash
        assert meta.session_id
        assert meta.created_at

    def test_session_id_is_timestamp(self):
        meta = create_session()
        assert meta.session_id.isdigit()


class TestLogOperations:
    def test_append_and_read(self, tmp_path):
        meta = create_session("/tmp/ws")
        save_meta(meta)
        append_log(meta, {"iter": 1, "action": "shell", "observation": "ok"})
        append_log(meta, {"iter": 2, "action": "read_file", "observation": "content"})

        entries = read_log(meta)
        # First entry is _meta header
        assert len(entries) == 3
        assert entries[1]["action"] == "shell"
        assert entries[2]["action"] == "read_file"

    def test_read_empty_log(self, tmp_path):
        meta = create_session("/tmp/ws")
        assert read_log(meta) == []

    def test_log_path_contains_workspace_hash(self, tmp_path):
        meta = create_session("/tmp/ws")
        path = get_log_path(meta)
        assert meta.workspace_hash in path.name
        assert meta.session_id in path.name
        assert path.suffix == ".jsonl"


class TestSummary:
    def test_save_and_load(self, tmp_path):
        meta = create_session("/tmp/ws")
        save_summary(meta, "## Goal\nTest the system.")
        loaded = load_summary(meta)
        assert loaded == "## Goal\nTest the system."

    def test_load_missing_summary(self, tmp_path):
        meta = create_session("/tmp/ws")
        assert load_summary(meta) is None

    def test_summary_path(self, tmp_path):
        meta = create_session("/tmp/ws")
        path = get_summary_path(meta)
        assert path.suffix == ".md"
        assert "summary" in path.name


class TestListSessions:
    def test_list_empty(self, tmp_path):
        assert list_sessions("/tmp/ws") == []

    def test_list_with_sessions(self, tmp_path):
        m1 = create_session("/tmp/ws")
        save_meta(m1)
        m2 = create_session("/tmp/ws")
        m2.session_id = str(int(m2.session_id) + 1)  # ensure different
        save_meta(m2)

        result = list_sessions("/tmp/ws")
        assert len(result) == 2

    def test_list_filters_by_workspace(self, tmp_path):
        m1 = create_session("/tmp/ws1")
        save_meta(m1)
        m2 = create_session("/tmp/ws2")
        save_meta(m2)

        result = list_sessions("/tmp/ws1")
        assert len(result) == 1
        assert result[0].workspace == "/tmp/ws1"


class TestLoadSession:
    def test_load_existing(self, tmp_path):
        meta = create_session("/tmp/ws")
        save_meta(meta)
        loaded = load_session(meta.session_id)
        assert loaded is not None
        assert loaded.session_id == meta.session_id

    def test_load_nonexistent(self, tmp_path):
        assert load_session("999999999") is None


class TestFindLatestSummary:
    def test_no_sessions(self, tmp_path):
        assert find_latest_summary("/tmp/ws") is None

    def test_returns_latest(self, tmp_path):
        m1 = create_session("/tmp/ws")
        save_meta(m1)
        save_summary(m1, "First session summary")

        m2 = create_session("/tmp/ws")
        m2.session_id = str(int(m2.session_id) + 1)
        save_meta(m2)
        save_summary(m2, "Second session summary")

        result = find_latest_summary("/tmp/ws")
        assert result == "Second session summary"


class TestFinalizeSession:
    def test_saves_ctx_as_summary(self, tmp_path):
        """finalize_session saves ctx messages as summary."""
        from unittest.mock import MagicMock

        from agent_cli.context.session import finalize_session

        meta = create_session("/tmp/ws")
        save_meta(meta)

        ctx = MagicMock()
        ctx.get_messages.return_value = [
            {"role": "user", "content": "What is 2+2?"},
            {
                "role": "assistant",
                "content": '{"thought": "math", "action": "complete"}',
            },
        ]

        finalize_session(meta, ctx)

        summary = load_summary(meta)
        assert summary is not None
        assert "What is 2+2?" in summary
        assert "User" in summary or "user" in summary

    def test_no_ctx_no_crash(self, tmp_path):
        """finalize_session with ctx=None does nothing."""
        from agent_cli.context.session import finalize_session

        meta = create_session("/tmp/ws")
        finalize_session(meta, None)  # should not raise
        assert load_summary(meta) is None

    def test_empty_ctx_no_crash(self, tmp_path):
        """finalize_session with empty messages does nothing."""
        from unittest.mock import MagicMock

        from agent_cli.context.session import finalize_session

        meta = create_session("/tmp/ws")
        ctx = MagicMock()
        ctx.get_messages.return_value = []
        finalize_session(meta, ctx)
        assert load_summary(meta) is None


class TestSubagentNoSession:
    def test_depth_zero_logs(self, tmp_path):
        """At depth=0, _log_to_session writes entries."""
        from agent_cli.loop import _log_to_session

        meta = create_session("/tmp/ws")
        save_meta(meta)
        _log_to_session(meta, {"iter": 1, "action": "test"})
        entries = read_log(meta)
        assert len(entries) == 2  # _meta + log entry

    def test_no_session_no_log(self, tmp_path):
        """When session is None, _log_to_session is a no-op."""
        from agent_cli.loop import _log_to_session

        _log_to_session(None, {"iter": 1, "action": "test"})
        # No error, no file created
