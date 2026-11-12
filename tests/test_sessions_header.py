"""`agent-cli sessions` 의 머리 줄 (v10.127.6).

경로가 든 두 줄(``Sessions for <ws>:`` · ``No sessions found for <ws>``)이 마크업 문자열이라, 긴
경로는 rich 가 80칸에서 단어 중간을 끊어 복사할 수 없었고 ``[x]`` 가 든 경로는 마크업으로 먹혔다.
CI(Linux)는 80칸이다 — 폭을 고정하고 경로가 출력에 한 덩어리로 그대로 있는지 본다(공백 정규화 없음).
"""

from __future__ import annotations

import pytest
from rich.console import Console

import agent_cli.context.session as session_mod
import agent_cli.main as m
from agent_cli.context.session import SessionMeta, save_meta

_LONG = "/work/" + "/".join(["very-long-directory-name"] * 5) + "/project"
_BRACKETS = "/work/[x]/proj [bold]tag[/bold]"


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(session_mod, "_SESSIONS_DIR", tmp_path / "sessions")
    con = Console(width=80, record=True, color_system=None)
    monkeypatch.setattr(m, "console", con)
    return con


def test_long_path_is_over_the_width():
    assert len(_LONG) > 100


@pytest.mark.parametrize("ws", [_LONG, _BRACKETS])
def test_no_sessions_line_keeps_the_path_whole(out, ws):
    m.sessions(workspace=ws)
    assert f"No sessions found for {ws}" in out.export_text()


@pytest.mark.parametrize("ws", [_LONG, _BRACKETS])
def test_sessions_header_keeps_the_path_whole(out, ws):
    save_meta(
        SessionMeta(session_id="1800000000", workspace=ws, updated_at="2026-11-12")
    )
    m.sessions(workspace=ws)
    text = out.export_text()
    assert f"Sessions for {ws}:" in text
    assert "1800000000" in text
