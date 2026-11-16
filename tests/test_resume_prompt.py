"""Tests for the resume prompt — ``main._pick_recent_session``.

When ``chat`` / ``web`` start WITHOUT ``--resume``, the user is offered the
most recent session ([y/N], default new). This is exactly the kind of feature
where a silent breakage is the worst case (the user would just always get a
new session and never notice), so the branches are pinned here:

  - prompt_fn returns "y"     → resume the most recent session
  - prompt_fn returns "n"/""  → None (the caller starts a NEW session after boot)
  - prompt_fn is None (pipe)  → never prompt, None
  - no sessions exist         → None (no prompt even on a TTY)

v10.144.0: 질문은 부트 **전에** 하고(이어 갈 세션의 서버로 부트하므로) 세션을 만들지 않는다 —
"아니오" 가 빈 세션 디렉터리를 남기지 않는다(부트가 실패해도).
"""

from __future__ import annotations

import pytest

import agent_cli.session.store as session_mod
from agent_cli.main import _pick_recent_session
from agent_cli.session.store import create_session, save_meta


@pytest.fixture(autouse=True)
def _use_tmp_sessions_dir(tmp_path, monkeypatch):
    """Redirect the sessions base dir to a temp dir for every test."""
    monkeypatch.setattr(
        session_mod, "_SESSIONS_DIR", tmp_path / ".agent-cli" / "sessions"
    )


def _make_session(workspace: str) -> str:
    """Persist a real session for ``workspace`` and return its id."""
    meta = create_session(workspace)
    save_meta(meta)
    return meta.session_id


class TestPickRecentSession:
    def test_yes_resumes_most_recent(self, tmp_path):
        ws = str(tmp_path / "ws")
        sid = _make_session(ws)

        session = _pick_recent_session(ws, lambda _p: "y")

        assert session.session_id == sid

    def test_yes_is_case_insensitive_and_trims(self, tmp_path):
        ws = str(tmp_path / "ws")
        sid = _make_session(ws)

        session = _pick_recent_session(ws, lambda _p: "  Y\n")

        assert session.session_id == sid

    def test_no_starts_new_session(self, tmp_path):
        ws = str(tmp_path / "ws")
        _make_session(ws)

        assert _pick_recent_session(ws, lambda _p: "n") is None
        assert (
            len(list(session_mod._SESSIONS_DIR.iterdir())) == 1
        )  # 새 세션을 안 만든다

    def test_empty_default_starts_new_session(self, tmp_path):
        """Enter (empty string) takes the safe default: a new session."""
        ws = str(tmp_path / "ws")
        _make_session(ws)

        assert _pick_recent_session(ws, lambda _p: "") is None

    def test_non_interactive_never_prompts(self, tmp_path):
        """prompt_fn=None (pipe / cron): always new, and the prompt reader is
        never consulted even though a recent session exists."""
        ws = str(tmp_path / "ws")
        _make_session(ws)

        assert _pick_recent_session(ws, None) is None

    def test_no_sessions_starts_new_without_prompting(self, tmp_path):
        """An empty workspace must NOT prompt — there is nothing to resume."""
        ws = str(tmp_path / "empty-ws")

        def _fail_prompt(_p):
            raise AssertionError("must not prompt when there is no session")

        assert _pick_recent_session(ws, _fail_prompt) is None

    def test_picks_latest_of_several(self, tmp_path):
        """list_sessions sorts ascending by id (timestamp); the resume offer
        is the LAST one. Build ids by hand so ordering is deterministic."""
        ws = str(tmp_path / "ws")
        base = session_mod._SESSIONS_DIR
        for sid in ("1700000001", "1700000002", "1700000003"):
            from agent_cli.session.store import SessionMeta

            d = base / sid
            d.mkdir(parents=True, exist_ok=True)
            save_meta(
                SessionMeta(
                    session_id=sid,
                    workspace=ws,
                    updated_at="2026-01-01 00:00:00",
                )
            )

        session = _pick_recent_session(ws, lambda _p: "y")

        assert session.session_id == "1700000003"
