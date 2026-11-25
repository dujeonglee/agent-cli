"""`ask` 는 누가 묻든 한 길 (6b) — 질문을 올리고 돌아오고, 사람이 답하거나 닫아서 닫힌다.

이 파일은 6b 의 지금 값과 바뀐 값을 함께 든다: 고정(①)으로 시작해 각 고침이 뒤집는다."""

from __future__ import annotations

import json

import pytest

import agent_cli.render as render_mod
from agent_cli.command_table import command_kind
from agent_cli.render.web import WebRenderer
from agent_cli.skills.executor import SkillLoop
from tests.test_agent_questions import spawn_idle
from tests.test_agents_live import make_registry


@pytest.fixture
def reg(tmp_path):
    r = make_registry(tmp_path)
    yield r
    r.shutdown_all()


# ── 스킬 ─────────────────────────────────────────────────────


def test_a_skill_run_has_no_question_port_and_no_box():
    """스킬 루프에는 질문 포트도 자기 상자도 없다 — `ask` 는 막는 프롬프트, `wait` 는 붙지 않는다."""
    ports = SkillLoop.make_ports(agent_registry=None, owner="main")
    assert ports.questions is None
    assert not ports.mailbox.has_inbox


# ── 닫기 ─────────────────────────────────────────────────────


def test_there_is_no_way_to_close_a_question_without_an_answer(reg):
    assert command_kind("/close q-1") != "question"
    assert not hasattr(reg, "close_question")


# ── 보드의 "답변 필요" ───────────────────────────────────────


def test_the_board_does_not_see_an_agents_open_question(reg, tmp_path, monkeypatch):
    """status.json 의 ``awaiting_input`` 은 막는 프롬프트가 떠 있을 때만 참이다 — 에이전트의 열린 질문은 세지 않는다."""
    r = WebRenderer(session_dir=str(tmp_path))
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    key = spawn_idle(reg)
    _, err = reg.question_port(key).ask("which directory?")
    assert not err
    r._publish_status()
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["awaiting_input"] is False
    assert r.is_awaiting_input() is False
