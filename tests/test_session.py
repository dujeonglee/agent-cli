"""Tests for context/session.py — project-local session persistence."""

from __future__ import annotations


import pytest

import agent_cli.context.session as session_mod
from agent_cli.context.session import (
    append_log,
    create_session,
    finalize_session,
    get_log_path,
    get_session_dir,
    get_summary_path,
    list_sessions,
    load_session,
    load_summary,
    read_log,
    save_meta,
    save_summary,
)


@pytest.fixture(autouse=True)
def _use_tmp_sessions_dir(tmp_path, monkeypatch):
    """Redirect sessions base dir to temp for all tests."""
    monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path / ".agent-cli")


class TestCreateSession:
    def test_creates_with_defaults(self):
        meta = create_session("/tmp/workspace")
        assert meta.workspace == "/tmp/workspace"
        assert meta.session_id
        assert meta.created_at

    def test_session_id_is_timestamp(self):
        meta = create_session()
        assert meta.session_id.isdigit()

    def test_no_workspace_hash(self):
        """New format: no workspace_hash field."""
        meta = create_session("/tmp/ws")
        assert not hasattr(meta, "workspace_hash") or meta.workspace_hash == ""


class TestSessionDir:
    def test_session_dir_structure(self, tmp_path):
        """Session files live under .agent-cli/sessions/{session_id}/."""
        meta = create_session("/tmp/ws")
        sdir = get_session_dir(meta)
        assert f"sessions/{meta.session_id}" in str(sdir)

    def test_log_path_in_session_dir(self, tmp_path):
        meta = create_session("/tmp/ws")
        path = get_log_path(meta)
        assert path.name == "session.jsonl"
        assert f"sessions/{meta.session_id}" in str(path)

    def test_summary_path_in_session_dir(self, tmp_path):
        meta = create_session("/tmp/ws")
        path = get_summary_path(meta)
        assert path.name == "session.summary.md"
        assert f"sessions/{meta.session_id}" in str(path)


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


class TestSummary:
    def test_save_and_load(self, tmp_path):
        meta = create_session("/tmp/ws")
        save_summary(meta, "## Goal\nTest the system.")
        loaded = load_summary(meta)
        assert loaded == "## Goal\nTest the system."

    def test_load_missing_summary(self, tmp_path):
        meta = create_session("/tmp/ws")
        assert load_summary(meta) is None


class TestListSessions:
    def test_list_empty(self, tmp_path):
        assert list_sessions("/tmp/ws") == []

    def test_list_with_sessions(self, tmp_path):
        m1 = create_session("/tmp/ws")
        save_meta(m1)
        m2 = create_session("/tmp/ws")
        m2.session_id = str(int(m2.session_id) + 1)
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

    def test_list_all_workspaces(self, tmp_path):
        """list_sessions without workspace filter returns all."""
        m1 = create_session("/tmp/ws1")
        save_meta(m1)
        m2 = create_session("/tmp/ws2")
        m2.session_id = str(int(m2.session_id) + 1)
        save_meta(m2)

        result = list_sessions()
        assert len(result) == 2


class TestLoadSession:
    def test_load_existing(self, tmp_path):
        meta = create_session("/tmp/ws")
        save_meta(meta)
        loaded = load_session(meta.session_id)
        assert loaded is not None
        assert loaded.session_id == meta.session_id

    def test_load_nonexistent(self, tmp_path):
        assert load_session("999999999") is None


class TestFinalizeSession:
    def test_saves_ctx_as_summary(self, tmp_path):
        from unittest.mock import MagicMock

        meta = create_session("/tmp/ws")
        save_meta(meta)

        ctx = MagicMock()
        ctx.get_messages.return_value = [
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": '{"thought": "math"}'},
        ]

        finalize_session(meta, ctx)

        summary = load_summary(meta)
        assert summary is not None
        assert "What is 2+2?" in summary

    def test_no_ctx_no_crash(self, tmp_path):
        meta = create_session("/tmp/ws")
        finalize_session(meta, None)
        assert load_summary(meta) is None

    def test_empty_ctx_no_crash(self, tmp_path):
        from unittest.mock import MagicMock

        meta = create_session("/tmp/ws")
        ctx = MagicMock()
        ctx.get_messages.return_value = []
        finalize_session(meta, ctx)
        assert load_summary(meta) is None


class TestSubagentNoSession:
    def test_depth_zero_logs(self, tmp_path):
        from agent_cli.loop import _log_to_session

        meta = create_session("/tmp/ws")
        save_meta(meta)
        _log_to_session(meta, {"iter": 1, "action": "test"})
        entries = read_log(meta)
        assert len(entries) == 2  # _meta + log entry

    def test_no_session_no_log(self, tmp_path):
        from agent_cli.loop import _log_to_session

        _log_to_session(None, {"iter": 1, "action": "test"})


class TestLogToolToSession:
    """Test _log_tool_to_session helper."""

    def test_logs_at_depth_zero(self, tmp_path):
        from agent_cli.loop import _log_tool_to_session

        meta = create_session("/tmp/ws")
        save_meta(meta)
        _log_tool_to_session(
            meta, depth=0, iteration=1, action="shell", observation="output here"
        )
        entries = read_log(meta)
        assert len(entries) == 2  # _meta + log entry
        assert entries[1]["action"] == "shell"
        assert entries[1]["observation"] == "output here"
        assert entries[1]["iter"] == 1

    def test_skips_at_depth_nonzero(self, tmp_path):
        from agent_cli.loop import _log_tool_to_session

        meta = create_session("/tmp/ws")
        save_meta(meta)
        _log_tool_to_session(
            meta, depth=1, iteration=1, action="shell", observation="output"
        )
        entries = read_log(meta)
        assert len(entries) == 1  # _meta only, no log entry

    def test_includes_thought_and_action_input(self, tmp_path):
        from agent_cli.loop import _log_tool_to_session

        meta = create_session("/tmp/ws")
        save_meta(meta)
        _log_tool_to_session(
            meta,
            depth=0,
            iteration=2,
            action="read_file",
            observation="file content",
            thought="need to read",
            action_input='{"path": "a.py"}',
        )
        entries = read_log(meta)
        assert entries[1]["thought"] == "need to read"
        assert entries[1]["action_input"] == '{"path": "a.py"}'

    def test_truncates_observation(self, tmp_path):
        from agent_cli.loop import _log_tool_to_session

        meta = create_session("/tmp/ws")
        save_meta(meta)
        long_obs = "x" * 1000
        _log_tool_to_session(
            meta, depth=0, iteration=1, action="shell", observation=long_obs
        )
        entries = read_log(meta)
        assert len(entries[1]["observation"]) == 500


class TestSessionScratchpadCoexistence:
    """Verify session files and scratchpad coexist in the same directory."""

    def test_scratchpad_in_session_dir(self, tmp_path):
        """Scratchpad files live alongside session files."""
        from agent_cli.context.scratchpad import init_scratchpad, load_scratchpad

        meta = create_session("/tmp/ws")
        save_meta(meta)

        sdir = get_session_dir(meta)
        init_scratchpad("Test goal", sdir)

        # Both session.jsonl and scratchpad.md exist in same dir
        assert (sdir / "session.jsonl").is_file()
        assert (sdir / "scratchpad.md").is_file()
        assert "Test goal" in load_scratchpad(sdir)

    def test_context_manager_uses_session_dir(self, tmp_path):
        """ContextManager with session_id uses same dir as session files."""
        from unittest.mock import MagicMock

        from agent_cli.context.manager import ContextManager
        from agent_cli.providers.compat import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=False,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        provider = MagicMock()

        meta = create_session("/tmp/ws")
        save_meta(meta)

        ctx = ContextManager(
            provider,
            "test",
            caps,
            session_id=meta.session_id,
            scratchpad_base=tmp_path / ".agent-cli",
        )
        ctx.init_task("Test task")

        sdir = get_session_dir(meta)
        assert (sdir / "scratchpad.md").is_file()
