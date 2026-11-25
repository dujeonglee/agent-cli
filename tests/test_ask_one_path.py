"""`ask` 는 누가 묻든 한 길 (6b) — 질문을 올리고 돌아오고, 사람이 답하거나 닫아서 닫힌다.

이 파일은 6b 의 지금 값과 바뀐 값을 함께 든다: 고정(①)으로 시작해 각 고침이 뒤집는다."""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

import agent_cli.render as render_mod
from agent_cli.command_table import command_kind
from agent_cli.loop.ledger import OPEN_QUESTIONS_HEAD, Ledger
from agent_cli.main import CLOSE_USAGE
from agent_cli.render.minimal import MinimalRenderer
from agent_cli.render.web import WebRenderer
from agent_cli.skills.executor import SkillLoop
from agent_cli.subagent.agent_mail import build_reply_record
from agent_cli.wake import wake_items
from agent_cli.web.server import WebServer, create_app
from tests.mail_support import absorbed_mail
from tests.test_agent_questions import _FakeLoopResult, spawn_idle
from tests.test_agents_live import make_registry, wait_until
from tests.test_answer_paths import _dispatch


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


def test_the_person_closes_a_question_without_an_answer(reg):
    """닫기는 답과 같은 길 — 묻는 쪽에게 사실의 봉투(``closed``, 줄 없는 ``n-``)가 가고 질문 줄은 닫힌다."""
    key = spawn_idle(reg)
    qid, err = reg.question_port(key).ask("which directory?")
    assert not err
    seen: list = []

    def runner(kind, start, ctx):
        seen.append(start.query)
        return _FakeLoopResult(output="ok"), 0.01

    reg._runner = runner
    assert reg.close_question(qid, by="user:dj") == ""
    assert reg.open_human_questions() == []
    assert wait_until(lambda: seen)
    assert seen[0] == (
        f"── closed [{seen[0].split('[')[1].split(']')[0]}] from the user re [{qid}] ──\n"
        "Your question: which directory?\n"
        "(The user closed it without an answer. Go on with what you have, and say what you assumed.)"
    )
    assert seen[0].startswith("── closed [n-")
    assert (
        reg.close_question(qid, by="user:dj")
        == f'unknown or already-closed question "{qid}"'
    )
    assert reg.answer_question(qid, "x", by="user:dj").startswith(
        "unknown or already-answered"
    )


def test_only_the_person_closes_a_question(reg):
    key = spawn_idle(reg)
    qid, _ = reg.question_port(key).ask("which directory?")
    assert (
        reg.close_question(qid, by="agent:x")
        == f'question "{qid}" is addressed to the person, not "agent:x"'
    )
    assert [q.id for q in reg.open_human_questions()] == [qid]


def test_close_is_a_question_command():
    assert command_kind("/close q-1") == "question"


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


# ── `/close` 와 질문 길 ───────────────────────────────────────


def test_close_command_needs_the_number_and_closes_only_that(reg):
    """번호가 없으면 사용법만 — 질문이 하나뿐이어도 짐작해 닫지 않는다(답과 같은 규칙)."""
    key = spawn_idle(reg)
    qid, _ = reg.question_port(key).ask("which directory?")
    assert _dispatch("/close", reg).lines == [(CLOSE_USAGE, False)]
    assert _dispatch(f"/close {qid} now", reg).lines == [(CLOSE_USAGE, False)]
    assert [q.id for q in reg.open_human_questions()] == [qid]
    assert _dispatch(f"/close {qid}", reg).lines == [(f"closed {qid}", True)]
    assert reg.open_human_questions() == []
    assert _dispatch(f"/close {qid}", reg).lines == [
        (f'/close: unknown or already-closed question "{qid}"', False)
    ]


def _client(reg):
    renderer = WebRenderer()
    server = WebServer(renderer, token="t")
    server.agent_registry = reg
    return TestClient(create_app(server))


def test_the_question_path_answers_and_closes(reg):
    key = spawn_idle(reg)
    q1, _ = reg.question_port(key).ask("first?")
    q2, _ = reg.question_port(key).ask("second?")
    client = _client(reg)
    r = client.post(f"/api/questions/{q1}/answer?token=t", json={"content": "yes"})
    assert r.status_code == 200
    r = client.post(f"/api/questions/{q2}/close?token=t", json={})
    assert r.status_code == 200
    assert reg.open_human_questions() == []
    assert client.post(f"/api/questions/{q2}/close?token=t", json={}).status_code == 409
    assert (
        client.post(
            f"/api/questions/{q1}/answer?token=t", json={"content": "x"}
        ).status_code
        == 409
    )
    assert client.post(f"/api/questions/{q1}/nope?token=t", json={}).status_code == 404


# ── main 도 같은 길로 묻는다 ──────────────────────────────────


def test_main_reads_the_answer_and_the_close_as_envelopes(reg):
    """main 의 질문에 온 답·닫힘은 메일로 — 에이전트가 읽는 것과 같은 봉투·같은 글, 줄 없는 ``n-``."""
    q1, _ = reg.question_port(None).ask("which directory?")
    q2, _ = reg.question_port(None).ask("json or yaml?")
    assert reg.answer_question(q1, "use src/util", by="user:dj") == ""
    assert reg.close_question(q2, by="user:dj") == ""
    answer, closed = [build_reply_record(m)["content"] for m in absorbed_mail(reg)]
    a_id = answer.split("[", 1)[1].split("]", 1)[0]
    c_id = closed.split("[", 1)[1].split("]", 1)[0]
    assert a_id.startswith("n-") and c_id.startswith("n-")
    assert answer == (
        f"── answer [{a_id}] from the user re [{q1}] ──\n"
        "Your question: which directory?\nAnswer: use src/util\n"
        "(Continue the work it was blocking.)"
    )
    assert closed == (
        f"── closed [{c_id}] from the user re [{q2}] ──\n"
        "Your question: json or yaml?\n"
        "(The user closed it without an answer. Go on with what you have, and say what you assumed.)"
    )


def test_mains_open_question_rides_mains_tail_and_wakes_main(reg):
    qid, _ = reg.question_port(None).ask("which directory?")
    led = Ledger()
    led.bind_questions(reg.question_port(None))
    assert led.questions_block(can_wait=True) == (
        f'{OPEN_QUESTIONS_HEAD}\n- "{qid}" · "which directory?"'
    )
    assert wake_items("main", agents=reg, with_schedule=False) == [
        (f"q:{qid}", f'your question "{qid}" to the user')
    ]


def test_mains_question_has_a_terminal_line_and_is_listed(reg, monkeypatch):
    buf = io.StringIO()
    r = MinimalRenderer(Console(file=buf, width=200))
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    key = spawn_idle(reg)
    q1, _ = reg.question_port(None).ask("which directory?")
    q2, _ = reg.question_port(key).ask("json or yaml?")
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    i = lines.index(f"❓ [main → user] {q1}")
    assert lines[i + 1] == "which directory?"
    assert (
        lines[i + 2] == f"  answer with: /answer {q1} <text> · close with: /close {q1}"
    )
    ((listing, ok),) = _dispatch("/questions", reg).lines
    assert ok and listing.splitlines()[0] == "Open questions:"
    assert f"{q1}  main  which directory?" in listing.splitlines()
    assert any(
        ln.startswith(f"{q2}  ") and f"({key})" in ln for ln in listing.splitlines()
    )
