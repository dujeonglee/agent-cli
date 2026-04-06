"""Tests for context/session.py — project-local session persistence."""

from __future__ import annotations


import pytest

import agent_cli.context.session as session_mod
from agent_cli.context.session import (
    create_session,
    load_session,
    save_meta,
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
        assert meta.updated_at

    def test_session_id_is_timestamp(self):
        meta = create_session()
        assert meta.session_id.isdigit()

    def test_no_workspace_hash(self):
        """New format: no workspace_hash field."""
        meta = create_session("/tmp/ws")
        assert not hasattr(meta, "workspace_hash") or meta.workspace_hash == ""


class TestLoadSession:
    def test_load_existing(self, tmp_path):
        meta = create_session("/tmp/ws")
        save_meta(meta)
        loaded = load_session(meta.session_id)
        assert loaded is not None
        assert loaded.session_id == meta.session_id

    def test_load_nonexistent(self, tmp_path):
        assert load_session("999999999") is None
