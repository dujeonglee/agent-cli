"""질문의 답이 오는 길 (6c) — 답할 사람이 없는 곳의 `ask`, 터미널·웹의 명령, 질문 줄, 터미널 읽기와 프롬프트의 잠금.

규칙(사용자 결정): 질문의 답이 되는 길은 번호를 적은 명령(`/answer q-N <글>`)과 웹 트레이뿐이다. 그 밖의 입력은 열린
질문이 있든 없든 main 에게 가는 보통의 요청이다 — 번호 없는 말을 답으로 짐작해 붙이지 않는다.

표준입력은 대역(`_Tty`)이고 `select` 는 "친 줄이 있으면 곧바로 읽을 수 있다" 로 묶는다 — 시간 판정이 없다."""

from __future__ import annotations

import io
import json
import select
import threading

import pytest
from rich.console import Console

import agent_cli.main as M
import agent_cli.render as render_mod
from agent_cli.command_table import command_kind
from agent_cli.context.manager import ContextManager
from agent_cli.main_input import MainInput
from agent_cli.render.base import interactive_lock
from agent_cli.render.minimal import MinimalRenderer
from tests.test_agent_questions import _run_scripted, spawn_idle
from tests.test_agents_live import make_registry


class _Tty(io.StringIO):
    def isatty(self):
        return True


class _Output(M.DispatchOutput):
    """명령의 결과 — 한 줄씩 적는다(성공 여부와 함께)."""

    def __init__(self):
        self.lines: list[tuple[str, bool]] = []
        self.skills_not_found: list[str] = []

    def agent_dispatch_result(self, text, success):
        self.lines.append((text, success))

    def skill_not_found(self, name):
        self.skills_not_found.append(name)


@pytest.fixture
def reg(tmp_path):
    r = make_registry(tmp_path)
    yield r
    r.shutdown_all()


@pytest.fixture
def no_viewer(monkeypatch):
    """답할 사람이 없는 표면 — 터미널도 웹 뷰어도 없는 `run`."""
    r = MinimalRenderer(Console(file=io.StringIO(), width=200))
    monkeypatch.setattr(type(r), "can_prompt", lambda self: False)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    return r


def _ask(question: str = "which directory should I use?") -> str:
    return json.dumps({"action": "ask", "question": question})


def _observations(ctx) -> list[dict]:
    return [m for m in ctx.get_raw_messages() if m.get("tool") == "ask"]


def _open_question(reg, key: str, text: str = "which directory should I use?") -> str:
    qid, err = reg.question_port(key).ask(text)
    assert not err, err
    return qid


def _dispatch(text: str, reg, *, author: str = "user") -> _Output:
    out = _Output()
    handled = M.try_dispatch_agent_or_skill(
        text,
        out,
        binding=None,
        max_turns=1,
        verbose=False,
        max_depth=1,
        ctx=None,
        session=None,
        mcp_manager=None,
        agent_registry=reg,
        author=author,
    )
    assert handled
    return out


# ── 답할 사람이 없는 곳의 `ask` ─────────────────────────────


class TestAskWhereNobodyCanAnswer:
    def test_main_gets_no_response_as_its_answer(self, reg, tmp_path, no_viewer):
        ctx = ContextManager(tmp_path / "s", max_context_tokens=30_000)
        _run_scripted(ctx, [_ask()], questions=reg.question_port(None))
        (obs,) = _observations(ctx)
        assert "A: (no response)" in obs["content"]

    def test_an_agents_question_goes_up_with_nobody_to_answer_it(
        self, reg, tmp_path, no_viewer
    ):
        key = spawn_idle(reg)
        ctx = ContextManager(tmp_path / "s", max_context_tokens=30_000)
        _run_scripted(ctx, [_ask()], questions=reg.question_port(key))
        (obs,) = _observations(ctx)
        assert 'sent — "which directory should I use?"' in obs["content"]
        assert [q.text for q in reg.open_human_questions()] == [
            "which directory should I use?"
        ]


# ── 명령 ────────────────────────────────────────────────────


class TestAnswerCommand:
    def test_answer_is_read_as_a_skill_name(self):
        assert command_kind("/answer q-3 use src/util") == "skill"
        assert command_kind("/questions") == "skill"

    def test_answer_does_not_reach_the_question(self, reg):
        key = spawn_idle(reg)
        qid = _open_question(reg, key)
        out = _dispatch(f"/answer {qid} use src/util", reg)
        assert out.skills_not_found == ["answer"]
        assert [q.id for q in reg.open_human_questions()] == [qid]


# ── 터미널의 질문 줄 ─────────────────────────────────────────


def test_the_terminal_question_line_has_no_number(reg, monkeypatch):
    buf = io.StringIO()
    r = MinimalRenderer(Console(file=buf, width=200))
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    key = spawn_idle(reg)
    qid = _open_question(reg, key)
    out = buf.getvalue()
    assert "which directory should I use?" in out
    assert qid not in out and "/answer" not in out


# ── 터미널 읽기와 프롬프트의 잠금 ───────────────────────────


class _HeldPrompt:
    """다른 스레드가 프롬프트처럼 `interactive_lock` 을 쥐고 있다 — 풀 때까지."""

    def __enter__(self):
        self._held, self._release = threading.Event(), threading.Event()

        def hold():
            with interactive_lock:
                self._held.set()
                self._release.wait()

        self._t = threading.Thread(target=hold, daemon=True)
        self._t.start()
        assert self._held.wait(5)
        return self

    def __exit__(self, *exc):
        self._release.set()
        self._t.join(5)


def _steer(monkeypatch, typed: str):
    q = MainInput(is_command=lambda t: t.startswith("/"))

    class _Surface:
        def echo(self, item):
            pass

        def route(self, text, *, item=None):
            return True

    stdin = _Tty(typed)
    monkeypatch.setattr(M.sys, "stdin", stdin)
    monkeypatch.setattr(
        select,
        "select",
        lambda r, w, x, t: (r if stdin.tell() < len(typed) else [], [], []),
    )
    return q, stdin, M._terminal_steer_input(q, _Surface())


def test_a_line_typed_while_a_prompt_holds_the_terminal_is_taken_by_the_wait(
    monkeypatch,
):
    q, stdin, wait = _steer(monkeypatch, "y\n")
    with _HeldPrompt():
        assert wait(0) is True
    assert [i["text"] for i in q.snapshot()] == ["y"]
    assert stdin.read() == ""
