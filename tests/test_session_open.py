"""`sessions` 는 살아 있는 것만 "in progress" 라고 한다 (v10.127.2).

종전엔 답 없이 끝난 세션(LLM 실패 포함)이 전부 ``→ (in progress)`` 였다. 신호는 이미 있었다 —
`session.lock`(flock, run·web 공통, 프로세스가 어떻게 죽든 커널이 푼다).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

import agent_cli.main as m
from agent_cli.context import session_lock
from agent_cli.context.session_lock import (
    claim_session,
    release_session,
    session_is_open,
)

_ROOT = Path(__file__).resolve().parents[1]

_HOLDER = """
import sys, time
from agent_cli.context.session_lock import claim_session
claim_session(sys.argv[1])
print("ready", flush=True)
time.sleep(60)
"""


def _hold_in_child(sdir: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(_ROOT)}
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(sdir)],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert child.stdout.readline().strip() == "ready"
    return child


def test_open_while_a_child_holds_it_and_closed_after_it_dies(tmp_path):
    sdir = tmp_path / "s1"
    child = _hold_in_child(sdir)
    try:
        assert session_is_open(sdir) is True
        assert session_is_open(sdir) is True  # 질의가 잠금을 남기지 않는다
    finally:
        child.kill()
        child.wait(timeout=10)
    assert session_is_open(sdir) is False
    # 질의 뒤에도 다른 프로세스가 곧바로 잡을 수 있다
    claim_session(sdir)
    try:
        assert session_is_open(sdir) is True  # 이 프로세스가 쥔 것(_HELD)
    finally:
        release_session(sdir)


def test_no_lock_file_is_closed_and_not_created(tmp_path):
    sdir = tmp_path / "s2"
    sdir.mkdir()
    assert session_is_open(sdir) is False
    assert not (sdir / "session.lock").exists()
    missing = tmp_path / "never-made"
    assert session_is_open(missing) is False
    assert not missing.exists()


def test_query_does_not_take_the_lock_away(tmp_path):
    """질의 직후 다른 프로세스의 claim 이 성공한다(질의가 잠금을 쥔 채 남지 않는다)."""
    sdir = tmp_path / "s3"
    sdir.mkdir()
    (sdir / "session.lock").write_text("", encoding="utf-8")
    assert session_is_open(sdir) is False
    child = _hold_in_child(sdir)
    try:
        assert session_is_open(sdir) is True
    finally:
        child.kill()
        child.wait(timeout=10)
    assert str(sdir.resolve()) not in session_lock._HELD


@pytest.mark.parametrize(
    ("is_open", "shown"), [(True, "in progress"), (False, "no answer")]
)
def test_print_session_says_in_progress_only_when_open(
    tmp_path, monkeypatch, is_open, shown
):
    from agent_cli.context import exchanges as _exchanges
    from agent_cli.context import session as _session
    from agent_cli.context.session import SessionMeta

    monkeypatch.setattr(_session, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(
        _exchanges, "session_summary", lambda s: ("do it", "(no completion)")
    )
    seen = []

    def _probe(d):
        seen.append(Path(d))
        return is_open

    monkeypatch.setattr(session_lock, "session_is_open", _probe)
    con = Console(width=80, record=True, color_system=None)
    monkeypatch.setattr(m, "console", con)
    meta = SessionMeta(session_id="123", workspace=str(tmp_path), updated_at="t")
    m._print_session(meta)
    text = con.export_text()
    assert f"→ ({shown})" in text
    assert seen == [tmp_path / "123"]


def test_answered_session_shows_the_answer(tmp_path, monkeypatch):
    from agent_cli.context import exchanges as _exchanges
    from agent_cli.context import session as _session
    from agent_cli.context.session import SessionMeta

    monkeypatch.setattr(_session, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(_exchanges, "session_summary", lambda s: ("q", "the answer"))
    con = Console(width=80, record=True, color_system=None)
    monkeypatch.setattr(m, "console", con)
    m._print_session(
        SessionMeta(session_id="9", workspace=str(tmp_path), updated_at="t")
    )
    text = con.export_text()
    assert "→ the answer" in text
    assert "in progress" not in text and "no answer" not in text
