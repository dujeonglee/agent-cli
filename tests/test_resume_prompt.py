"""Tests for the resume prompt — ``main._maybe_resume_recent``.

When ``chat`` / ``web`` start WITHOUT ``--resume``, the user is offered the
most recent session ([y/N], default new). This is exactly the kind of feature
where a silent breakage is the worst case (the user would just always get a
new session and never notice), so the branches are pinned here:

  - prompt_fn returns "y"     → resume the most recent session
  - prompt_fn returns "n"/""  → start a NEW session
  - prompt_fn is None (pipe)  → never prompt, start NEW
  - no sessions exist         → start NEW (no prompt even on a TTY)
"""

from __future__ import annotations

import pytest

import agent_cli.context.session as session_mod
from agent_cli.context.session import create_session, save_meta
from agent_cli.main import _maybe_resume_recent


@pytest.fixture(autouse=True)
def _use_tmp_sessions_dir(tmp_path, monkeypatch):
    """Redirect the sessions base dir to a temp dir for every test."""
    monkeypatch.setattr(session_mod, "_SESSIONS_BASE", tmp_path / ".agent-cli")


def _make_session(workspace: str) -> str:
    """Persist a real session for ``workspace`` and return its id."""
    meta = create_session(workspace, response_format="react")
    save_meta(meta)
    return meta.session_id


class TestMaybeResumeRecent:
    def test_yes_resumes_most_recent(self, tmp_path):
        ws = str(tmp_path / "ws")
        sid = _make_session(ws)

        session, is_resume = _maybe_resume_recent(ws, "react", lambda _p: "y")

        assert is_resume is True
        assert session.session_id == sid

    def test_yes_is_case_insensitive_and_trims(self, tmp_path):
        ws = str(tmp_path / "ws")
        sid = _make_session(ws)

        session, is_resume = _maybe_resume_recent(ws, "react", lambda _p: "  Y\n")

        assert is_resume is True
        assert session.session_id == sid

    def test_no_starts_new_session(self, tmp_path):
        # NOTE: don't compare session ids — create_session derives the id from
        # int(time.time()), so a new session made in the same second as the
        # pre-existing one collides. is_resume is the real contract.
        ws = str(tmp_path / "ws")
        _make_session(ws)

        session, is_resume = _maybe_resume_recent(ws, "react", lambda _p: "n")

        assert is_resume is False
        assert session.session_id

    def test_empty_default_starts_new_session(self, tmp_path):
        """Enter (empty string) takes the safe default: a new session."""
        ws = str(tmp_path / "ws")
        _make_session(ws)

        session, is_resume = _maybe_resume_recent(ws, "react", lambda _p: "")

        assert is_resume is False
        assert session.session_id

    def test_non_interactive_never_prompts(self, tmp_path):
        """prompt_fn=None (pipe / cron): always new, and the prompt reader is
        never consulted even though a recent session exists."""
        ws = str(tmp_path / "ws")
        _make_session(ws)

        session, is_resume = _maybe_resume_recent(ws, "react", None)

        assert is_resume is False
        assert session.session_id

    def test_no_sessions_starts_new_without_prompting(self, tmp_path):
        """An empty workspace must NOT prompt — there is nothing to resume."""
        ws = str(tmp_path / "empty-ws")

        def _fail_prompt(_p):
            raise AssertionError("must not prompt when there is no session")

        session, is_resume = _maybe_resume_recent(ws, "react", _fail_prompt)

        assert is_resume is False
        assert session.session_id

    def test_new_session_carries_response_format(self, tmp_path):
        ws = str(tmp_path / "ws")
        session, is_resume = _maybe_resume_recent(ws, "react", lambda _p: "n")

        assert is_resume is False
        assert session.response_format == "react"

    def test_picks_latest_of_several(self, tmp_path):
        """list_sessions sorts ascending by id (timestamp); the resume offer
        is the LAST one. Build ids by hand so ordering is deterministic."""
        ws = str(tmp_path / "ws")
        base = session_mod._SESSIONS_BASE / "sessions"
        for sid in ("1700000001", "1700000002", "1700000003"):
            from agent_cli.context.session import SessionMeta

            d = base / sid
            d.mkdir(parents=True, exist_ok=True)
            save_meta(
                SessionMeta(
                    session_id=sid,
                    workspace=ws,
                    updated_at="2026-01-01 00:00:00",
                    response_format="react",
                )
            )

        session, is_resume = _maybe_resume_recent(ws, "react", lambda _p: "y")

        assert is_resume is True
        assert session.session_id == "1700000003"
