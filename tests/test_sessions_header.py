"""`agent-cli sessions` 의 머리 줄 (v10.127.6) · 세션 요약 줄 (v10.127.8).

경로가 든 두 줄(``Sessions for <ws>:`` · ``No sessions found for <ws>``)이 마크업 문자열이라, 긴
경로는 rich 가 80칸에서 단어 중간을 끊어 복사할 수 없었고 ``[x]`` 가 든 경로는 마크업으로 먹혔다.
CI(Linux)는 80칸이다 — 폭을 고정하고 경로가 출력에 한 덩어리로 그대로 있는지 본다(공백 정규화 없음).

요약 줄(``↳`` 사용자 요청 · ``→`` 결과)도 글을 마크업에 끼워 찍어 ``[bold]`` 가 서식으로 먹히고
``[/nope]`` 같은 닫는 태그는 ``MarkupError`` 로 ``sessions``·resume 프롬프트를 죽였다.
"""

from __future__ import annotations

import json

import pytest
from rich.console import Console

import agent_cli.main as m
import agent_cli.session.store as session_mod
from agent_cli.session.store import SessionMeta, save_meta

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


_TRICKY = ["[bold]x[/bold]", "[/nope]", "[x]"]


def _session_with(ws: str, user: str, result: str) -> SessionMeta:
    meta = SessionMeta(session_id="1800000000", workspace=ws, updated_at="2026-11-12")
    save_meta(meta)
    hist = session_mod._SESSIONS_DIR / meta.session_id / "history.jsonl"
    lines = [
        {"role": "user", "content": user},
        {"role": "assistant", "action": "complete", "action_input": {"result": result}},
    ]
    hist.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return meta


@pytest.mark.parametrize("tricky", _TRICKY)
def test_summary_lines_print_user_and_result_text_verbatim(out, tricky):
    _session_with("/ws", f"ask {tricky} please", f"done {tricky} ok")
    m.sessions(workspace="/ws")
    text = out.export_text()
    assert f"↳ ask {tricky} please" in text
    assert f"→ done {tricky} ok" in text


def test_resume_prompt_shows_the_same_text_verbatim(out):
    _session_with("/ws", "ask [/nope] please", "done [bold]x[/bold] ok")
    _session, is_resume = m._maybe_resume_recent("/ws", lambda _p: "n")
    assert is_resume is False
    text = out.export_text()
    assert "↳ ask [/nope] please" in text
    assert "→ done [bold]x[/bold] ok" in text
