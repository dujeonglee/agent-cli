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
from agent_cli.loop.prompt import SystemPromptSvc
from agent_cli.main import CLOSE_USAGE
from agent_cli.main_input import MainInput
from agent_cli.render.minimal import MinimalRenderer
from agent_cli.render.web import WebRenderer
from agent_cli.runtime import commands_during_nested_wait
from agent_cli.skills.executor import SkillLoop
from agent_cli.subagent.agent_mail import build_reply_record
from agent_cli.wake import wake_items
from agent_cli.web.server import WebServer, create_app
from tests.loopkit import make_config
from tests.mail_support import absorbed_mail
from tests.pump_support import FakeSurface
from tests.test_agent_questions import _FakeLoopResult, spawn_idle
from tests.test_agents_live import make_registry, wait_until
from tests.test_answer_paths import _dispatch


@pytest.fixture
def reg(tmp_path):
    r = make_registry(tmp_path)
    yield r
    r.shutdown_all()


# ── 스킬 ─────────────────────────────────────────────────────


def test_a_skill_run_asks_under_its_own_address(reg):
    """스킬 런은 자기 주소로 묻고 그 주소의 상자를 읽는다(6b, 결정 B 라) — 그래서 `wait` 가 붙는다. 작업·모니터는
    호출자(``owner``)에게 보고한다."""
    ports = SkillLoop.make_ports(agent_registry=reg, owner="main", address="skill:s-1")
    assert ports.owner == "main"
    assert ports.questions.asker == "skill:s-1" and ports.questions.me == "skill:s-1"
    assert ports.mailbox.owner == "skill:s-1" and ports.mailbox.has_inbox


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


@pytest.mark.parametrize("asker", ["main", "agent", "skill"])
def test_the_board_sees_an_open_question(reg, tmp_path, monkeypatch, asker):
    """status.json·health 의 ``awaiting_input`` ("답변 필요") 은 확인 대화가 떠 있거나 열린 질문이 있을 때 참이다(6b 결정
    G) — 누가 물었든. 닫히면 내린다. 막는 물음만 보는 `is_awaiting_input` (Ctrl+C 의 갈래)은 그대로 거짓."""
    r = WebRenderer(session_dir=str(tmp_path))
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    port = {
        "main": lambda: reg.question_port(None),
        "agent": lambda: reg.question_port(spawn_idle(reg)),
        "skill": lambda: reg.question_port(address="skill:s-1"),
    }[asker]()
    qid, err = port.ask("which directory?")
    assert not err

    def board():
        return json.loads((tmp_path / "status.json").read_text())["awaiting_input"]

    assert board() is True and r.needs_answer() is True
    assert r.is_awaiting_input() is False
    server = WebServer(r, token="t")
    assert TestClient(create_app(server)).get("/api/health").json()["awaiting_input"]
    assert reg.close_question(qid, by="user:dj") == ""
    assert board() is False and r.needs_answer() is False


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


# ── 스킬 런이 묻고 기다리는 동안 (특례 ②) ─────────────────────


def test_while_a_skill_waits_only_commands_are_routed():
    """스킬은 main 의 차례 안에서 돈다 — 기다리는 동안 줄 선 명령만 그 자리에서 라우팅하고(질문의 답은 그 길로 스킬의
    상자에 든다), 보통의 말은 main 의 것이라 그대로 둔다. 어느 쪽이든 스킬을 깨우지 않는다."""
    q = MainInput(is_command=lambda t: t.startswith("/"))
    surface = FakeSurface(routed={"/answer q-1 yes"})
    q.enqueue(None, "and also check the logs")
    q.enqueue(None, "/answer q-1 yes")
    wait = commands_during_nested_wait(q, surface)
    assert wait(0.0) is False
    assert ("route", "/answer q-1 yes") in surface.calls
    assert [e.text for e in q.post.words("main")] == ["and also check the logs"]


def test_on_a_terminal_the_skills_wait_reads_the_typed_line():
    q = MainInput(is_command=lambda t: t.startswith("/"))
    surface = FakeSurface(routed={"/close q-1"})

    def typed(timeout):
        q.enqueue(None, "/close q-1")
        return True

    assert commands_during_nested_wait(q, surface, read_line=typed)(0.1) is False
    assert ("route", "/close q-1") in surface.calls


def test_a_skill_runs_questions_close_when_the_skill_ends(reg):
    """스킬 런이 끝나면 그 런이 건 열린 질문은 닫힌다 — 답을 받을 쪽이 없다(죽은 에이전트와 같은 규칙)."""
    port = reg.question_port(address="skill:s-1")
    qid, err = port.ask("which name?")
    assert not err
    assert [(q.id, q.asker) for q in reg.open_human_questions()] == [(qid, "skill:s-1")]
    reg.close_questions_of("skill:s-1")
    assert reg.open_human_questions() == []
    assert reg.answer_question(qid, "x", by="user:dj").startswith("unknown")


@pytest.mark.parametrize("asker", ["main", "agent", "skill"])
def test_plain_words_in_the_web_chat_box_stay_mains_request(reg, asker):
    """사용자 규칙: 답은 번호를 적은 명령·트레이로만 — 질문이 열려 있어도(누가 물었든) 웹 채팅 칸의 보통의 말은 main 앞에
    줄 서는 사람의 말이고, 질문은 열린 채다."""
    port = {
        "main": lambda: reg.question_port(None),
        "agent": lambda: reg.question_port(spawn_idle(reg)),
        "skill": lambda: reg.question_port(address="skill:s-1"),
    }[asker]()
    qid, _ = port.ask("which directory?")
    server = WebServer(WebRenderer(), token="t")
    server.agent_registry = reg
    r = TestClient(create_app(server)).post(
        "/api/input?token=t", json={"kind": "chat", "content": "use src/util"}
    )
    assert r.status_code == 200
    assert [q.id for q in reg.open_human_questions()] == [qid]
    assert server.main_input.wait_to_open(0)["message"]["text"] == "use src/util"


def test_a_skill_runs_question_has_a_terminal_line_and_one_label(reg, monkeypatch):
    """대화 창이 없는 묻는 쪽(main·스킬 런)은 터미널 질문 줄을 낸다. 스킬 런의 이름표는 ``skill <스킬 이름>`` 하나 —
    질문 줄·`/questions`·❓ 트레이(질문 목록의 ``label``)가 같은 글이다(`asker_label`)."""
    r = WebRenderer()
    seen: list = []
    monkeypatch.setattr(r, "open_questions", seen.append)
    buf = io.StringIO()
    term = MinimalRenderer(Console(file=buf, width=200))
    monkeypatch.setattr(term, "open_questions", seen.append)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: term)
    ports = SkillLoop.make_ports(
        agent_registry=reg, owner="main", address="skill:s-1", name="demo"
    )
    qid, err = ports.questions.ask("which name?")
    assert not err
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    i = lines.index(f"❓ [skill demo → user] {qid}")
    assert lines[i + 1 : i + 3] == [
        "which name?",
        f"  answer with: /answer {qid} <text> · close with: /close {qid}",
    ]
    ((listing, ok),) = _dispatch("/questions", reg).lines
    assert ok and f"{qid}  skill demo  which name?" in listing.splitlines()
    assert [q["label"] for q in seen[-1]] == ["skill demo"]


def _system_prompt_for(port) -> str:
    svc = SystemPromptSvc(
        make_config(questions=port, tools_list=["ask", "wait", "complete"]), None
    )
    svc.rebuild()
    return svc.system


def test_only_agents_get_the_ask_guide(reg):
    """`ask` 안내는 에이전트만(결정 C) — 술어는 포트의 `ask_guide` 하나. main·스킬 런의 시스템 프롬프트에는 없다(스킬에는
    `message` 가 없어 안내의 "For an agent, send a `message`" 가 거짓이다)."""
    mark = "`ask` does not block you."
    agent = reg.question_port(spawn_idle(reg))
    skill = SkillLoop.make_ports(
        agent_registry=reg, owner="main", address="skill:s-1", name="demo"
    )
    assert mark in _system_prompt_for(agent)
    assert mark not in _system_prompt_for(reg.question_port(None))
    assert mark not in _system_prompt_for(skill.questions)
    assert not skill.ask_guide()


def test_a_command_the_route_refuses_while_a_skill_waits_goes_back_as_words():
    """스킬이 기다리는 동안 실행이 받지 않은 명령(문과 실행 사이에 같은 이름의 경로가 생겼다)은 버리지 않는다 — main 의
    제자리 대기와 같이 사람의 말로 다시 넣는다(main 의 것이다). 스킬은 깨우지 않는다."""
    q = MainInput(is_command=lambda t: t.startswith("/") and t != "/tmp/x")
    surface = FakeSurface(routed=())  # 실행이 받지 않는다
    q.enqueue(None, "/tmp/x")  # 들어올 때는 명령이 아니다 — 대조
    q._is_command = lambda t: t.startswith("/")
    q.enqueue(None, "/tmp/y")
    q._is_command = lambda t: t.startswith("/") and not t.startswith("/tmp/")
    assert commands_during_nested_wait(q, surface)(0.0) is False
    assert ("route", "/tmp/y") in surface.calls
    assert [e.text for e in q.post.words("main")] == ["/tmp/x", "/tmp/y"]
