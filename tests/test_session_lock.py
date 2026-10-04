"""One session, one process (docs/schedule/DESIGN.md §4.3).

Before v10.12.0 a session could be opened twice — two ``--resume`` processes
appended to the same history, and with in-process schedules both would fire.
The second process is now refused at startup.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from agent_cli.context.session_lock import (
    SessionBusy,
    claim_session,
    release_session,
)

_HOLD = textwrap.dedent(
    """
    import sys, time
    from agent_cli.context.session_lock import claim_session
    claim_session(sys.argv[1])
    print("held", flush=True)
    time.sleep(60)
    """
)


@pytest.fixture
def holder(tmp_path):
    """A second process that holds the session until killed."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(tmp_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "held"
    yield proc
    proc.kill()
    proc.wait()


class TestClaimSession:
    def test_first_claim_succeeds_and_records_pid(self, tmp_path):
        claim_session(tmp_path)
        try:
            assert (tmp_path / "session.lock").read_text() == str(os.getpid())
        finally:
            release_session(tmp_path)

    def test_same_process_may_claim_again(self, tmp_path):
        """The invariant is about processes — a resume pre-check followed by
        the real open is one process claiming twice."""
        claim_session(tmp_path)
        try:
            claim_session(tmp_path)
        finally:
            release_session(tmp_path)

    def test_second_process_is_refused_with_the_holders_pid(self, tmp_path, holder):
        with pytest.raises(SessionBusy) as e:
            claim_session(tmp_path)
        assert e.value.pid == holder.pid
        assert f"pid {holder.pid}" in str(e.value)

    def test_lock_is_free_once_the_holder_dies(self, tmp_path, holder):
        """No stale lock: the kernel drops it with the process, however it
        died (here: SIGKILL)."""
        holder.kill()
        holder.wait()
        claim_session(tmp_path)
        release_session(tmp_path)


class TestCliRefusesABusySession:
    def test_resume_of_a_held_session_exits_before_the_handshake(
        self, tmp_path, monkeypatch, holder
    ):
        """``--resume`` of a session another process holds ends with exit 1
        and names the holder — it never reaches the provider."""
        import typer

        from agent_cli import main
        from agent_cli.context import session as session_mod

        meta = session_mod.SessionMeta(
            session_id="s1", workspace=str(tmp_path), updated_at=""
        )
        monkeypatch.setattr(session_mod, "load_session", lambda sid: meta)
        monkeypatch.setattr(session_mod, "get_session_dir", lambda m: tmp_path)
        with pytest.raises(typer.Exit) as e:
            main._load_resume_session("s1")
        assert e.value.exit_code == 1
