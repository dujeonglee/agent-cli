"""질문의 답이 오는 길 (6c) — 답할 사람이 없는 곳의 `ask`, 터미널·웹의 명령, 질문 줄, 터미널 읽기와 프롬프트의 잠금.

규칙(사용자 결정): 질문의 답이 되는 길은 번호를 적은 명령(`/answer q-N <글>`)과 웹 트레이뿐이다. 그 밖의 입력은 열린
질문이 있든 없든 main 에게 가는 보통의 요청이다 — 번호 없는 말을 답으로 짐작해 붙이지 않는다.

표준입력은 대역(`_Tty`)이고 `select` 는 "친 줄이 있으면 곧바로 읽을 수 있다" 로 묶는다 — 시간 판정이 없다."""

from __future__ import annotations

import inspect
import io
import json
import select
import threading
from types import SimpleNamespace

import pytest
from rich.console import Console

import agent_cli.loop.dispatch as dispatch_mod
import agent_cli.main as M
import agent_cli.render as render_mod
from agent_cli.agent_icon import agent_display_name
from agent_cli.command_table import command_kind
from agent_cli.context.manager import ContextManager
from agent_cli.loop.dispatch import ASK_REFUSED_NOBODY_CAN_ANSWER
from agent_cli.main_input import MainInput
from agent_cli.pump import QuietPolicy
from agent_cli.render.base import interactive_lock
from agent_cli.render.minimal import MinimalRenderer
from agent_cli.runtime import session_has_live_work, session_is_busy
from tests.oracle.driver import Provider, Session, call, closing_ids, until
from tests.pump_support import FakeSurface, make_pump
from tests.test_agent_questions import _run_scripted, spawn_idle
from tests.test_agents_live import _RecordingRunner, make_registry, wait_until


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


@pytest.fixture(autouse=True)
def _keep_renderer():
    """오라클 세션은 전역 렌더러를 자기 기록 렌더러로 갈아 끼운다 — 다음 테스트로 새지 않게 되돌린다."""
    before = render_mod.get_renderer()
    yield
    render_mod.set_renderer(before)


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
    """답이 올 수 없는 표면(`can_prompt` 거짓)에서는 누가 묻든 같은 거절 — 갈래(막음/올림) 앞 한 곳."""

    def test_main_is_refused_and_no_prompt_opens(
        self, reg, tmp_path, no_viewer, monkeypatch
    ):
        monkeypatch.setattr(dispatch_mod, "_handle_ask", _must_not_prompt)
        ctx = ContextManager(tmp_path / "s", max_context_tokens=30_000)
        _run_scripted(ctx, [_ask()], questions=reg.question_port(None))
        (obs,) = _observations(ctx)
        assert obs["content"] == f"Observation: {ASK_REFUSED_NOBODY_CAN_ANSWER}"
        assert obs["success"] is False

    def test_an_agents_question_is_refused_and_not_opened(
        self, reg, tmp_path, no_viewer
    ):
        key = spawn_idle(reg)
        ctx = ContextManager(tmp_path / "s", max_context_tokens=30_000)
        _run_scripted(ctx, [_ask()], questions=reg.question_port(key))
        (obs,) = _observations(ctx)
        assert obs["content"] == f"Observation: {ASK_REFUSED_NOBODY_CAN_ANSWER}"
        assert obs["success"] is False
        assert reg.open_human_questions() == []

    def test_an_ask_carrying_several_questions_opens_none(
        self, reg, tmp_path, no_viewer
    ):
        key = spawn_idle(reg)
        ctx = ContextManager(tmp_path / "s", max_context_tokens=30_000)
        two = json.dumps({"action": "ask", "questions": ["first?", "second?"]})
        _run_scripted(ctx, [two], questions=reg.question_port(key))
        (obs,) = _observations(ctx)
        assert obs["content"] == f"Observation: {ASK_REFUSED_NOBODY_CAN_ANSWER}"
        assert reg.open_human_questions() == []

    def test_where_an_answer_can_come_the_agent_still_asks(
        self, reg, tmp_path, monkeypatch
    ):
        r = MinimalRenderer(Console(file=io.StringIO(), width=200))
        monkeypatch.setattr(type(r), "can_prompt", lambda self: True)
        monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
        key = spawn_idle(reg)
        ctx = ContextManager(tmp_path / "s", max_context_tokens=30_000)
        _run_scripted(ctx, [_ask()], questions=reg.question_port(key))
        (obs,) = _observations(ctx)
        assert obs["success"] is True and "sent" in obs["content"]
        assert len(reg.open_human_questions()) == 1


def _must_not_prompt(questions):
    raise AssertionError("the blocking prompt opened where nobody can answer")


# ── 명령 ────────────────────────────────────────────────────


class TestAnswerCommand:
    """`/answer <q-N> <글>` 은 그 번호의 질문에만 답한다 — 웹 트레이와 같은 함수. 번호가 없으면 아무것도 닫지 않는다."""

    def test_the_commands_are_commands(self):
        assert command_kind("/answer q-3 use src/util") == "question"
        assert command_kind("/questions") == "question"

    def test_answer_closes_that_question_and_the_asker_gets_the_answer(self, tmp_path):
        runner = _RecordingRunner()
        reg = make_registry(tmp_path, runner=runner)
        try:
            key = spawn_idle(reg)
            qid = _open_question(reg, key)
            out = _dispatch(f"/answer {qid} use src/util", reg)
            assert out.lines == [(f"answered {qid}", True)]
            assert reg.open_human_questions() == []
            assert wait_until(lambda: runner.seen)
            assert f"from the user re [{qid}]" in runner.seen[0]
            assert "Answer: use src/util" in runner.seen[0]
        finally:
            reg.shutdown_all()

    def test_the_web_author_is_a_person_too(self, reg):
        key = spawn_idle(reg)
        qid = _open_question(reg, key)
        out = _dispatch(f"/answer {qid} yes", reg, author="user:dj")
        assert out.lines == [(f"answered {qid}", True)]

    @pytest.mark.parametrize(
        "typed", ["/answer", "/answer yes", "/answer use src/util"]
    )
    def test_without_a_number_nothing_is_closed_even_with_one_question_open(
        self, reg, typed
    ):
        """질문이 하나뿐이어도 번호 없는 답을 그 질문에 붙이지 않는다(사용자 결정). 인자가 모자라면 사용법만 말하고,
        첫 낱말이 번호가 아니면 없는 번호라고 말한다 — 어느 쪽도 그 하나뿐인 질문을 닫지 않는다."""
        key = spawn_idle(reg)
        qid = _open_question(reg, key)
        out = _dispatch(typed, reg)
        assert [ok for _, ok in out.lines] == [False]
        assert [q.id for q in reg.open_human_questions()] == [qid]
        assert reg._post.queued(f"agent:{key}") == ()

    def test_a_wrong_number_closes_nothing(self, reg):
        key = spawn_idle(reg)
        qid = _open_question(reg, key)
        out = _dispatch("/answer q-999 use src/util", reg)
        assert out.lines == [
            ('/answer: unknown or already-answered question "q-999"', False)
        ]
        assert [q.id for q in reg.open_human_questions()] == [qid]

    def test_questions_lists_them_like_the_question_line(self, reg):
        key = spawn_idle(reg)
        qid = _open_question(reg, key)
        ((text, ok),) = _dispatch("/questions", reg).lines
        assert ok and text.splitlines() == [
            "Open questions:",
            f"{qid}  {agent_display_name(key)} ({key})  which directory should I use?",
        ]

    def test_no_open_questions(self, reg):
        assert _dispatch("/questions", reg).lines == [("no open questions", True)]


def test_plain_words_on_the_web_go_to_main_and_the_question_stays_open(
    tmp_path, monkeypatch
):
    """열린 에이전트 질문이 있어도 웹 채팅 칸의 보통의 말은 main 의 요청이다 — 질문의 답이 되지 않는다."""
    monkeypatch.chdir(
        tmp_path
    )  # 세션 조립이 작업 폴더에 무엇을 만들든 저장소 밖에서 — 오라클 드라이버와 같다
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
    read: list[str] = []

    def main_reads(messages):
        read.append(str(messages[-1].get("content") or ""))
        return call("complete", result="ok", answers=closing_ids(messages))

    s = Session(tmp_path, Provider({"MAIN": [main_reads]}), path="web")
    key = spawn_idle(s.reg)
    qid = _open_question(s.reg, key)
    s.say("use src/util", nickname="dj")
    t = s.pump_in_thread()
    try:
        until(lambda: read, "main read the words")
    finally:
        s.close()
        t.join(20)
    assert "use src/util" in read[0] and qid not in read[0]
    assert [q.id for q in s.reg.open_human_questions()] == [qid]
    assert s.reg.backlog_of(key) == 0  # 묻는 쪽에게 간 것이 없다


# ── 터미널의 질문 줄 ─────────────────────────────────────────


def test_the_terminal_question_line_carries_its_number_and_how_to_answer(
    reg, monkeypatch
):
    buf = io.StringIO()
    r = MinimalRenderer(Console(file=buf, width=200))
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    key = spawn_idle(reg)
    qid = _open_question(reg, key)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    i = next(n for n, ln in enumerate(lines) if ln.startswith("❓"))
    assert lines[i].endswith(f"→ user] {qid}")
    assert lines[i + 1] == "which directory should I use?"
    assert (
        lines[i + 2]
        == f"  answer with: /answer {qid} <text> · close with: /close {qid}"
    )


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


def test_a_line_typed_while_a_prompt_holds_the_terminal_is_left_to_the_prompt(
    monkeypatch,
):
    """프롬프트가 `interactive_lock` 을 쥔 동안 친 줄은 프롬프트의 것이다 — 제자리 대기 읽기는 가져가지 않는다."""
    q, stdin, wait = _steer(monkeypatch, "y\n")
    with _HeldPrompt():
        assert wait(0) is False
    assert q.snapshot() == []
    assert stdin.read() == "y\n"


class TestReadTerminalLine:
    def test_a_typed_line_goes_to_main(self, monkeypatch):
        q, _, _ = _steer(monkeypatch, "skip the tests\n")
        assert M.read_terminal_line(q, 0) is True
        assert [i["text"] for i in q.snapshot()] == ["skip the tests"]

    def test_nothing_typed(self, monkeypatch):
        q, _, _ = _steer(monkeypatch, "")
        assert M.read_terminal_line(q, 0) is False

    def test_not_read_while_a_prompt_holds_the_terminal(self, monkeypatch):
        q, stdin, _ = _steer(monkeypatch, "y\n")
        with _HeldPrompt():
            assert M.read_terminal_line(q, 0) is None
        assert stdin.read() == "y\n" and q.snapshot() == []

    def test_not_read_when_the_input_is_not_a_terminal(self, monkeypatch):
        q, _, _ = _steer(monkeypatch, "hello\n")
        monkeypatch.setattr(M.sys, "stdin", io.StringIO("hello\n"))
        assert M.read_terminal_line(q, 0) is None
        assert q.snapshot() == []


# ── `run` 이 한가한 동안의 터미널 읽기 ─────────────────────────


class TestIdleTerminalRead:
    """main 이 결과를 낸 뒤에도 `run` 은 친 줄을 읽는다 — 보통의 말은 main 의 새 요청, `/answer` 는 명령."""

    def _actor(self, q, surface=None, read=None):
        return make_pump(
            main_input=q,
            run_main=lambda start: None,
            surface=surface,
            read_terminal=read
            if read is not None
            else (lambda t: M.read_terminal_line(q, t)),
        )

    def test_plain_words_open_main_work_and_the_question_stays_open(
        self, reg, monkeypatch
    ):
        key = spawn_idle(reg)
        qid = _open_question(reg, key)
        q, _, _ = _steer(monkeypatch, "use src/util\n")
        got = self._actor(q).take(0.5)
        assert got["message"]["text"] == "use src/util"
        assert not got["message"].get("command")
        assert [x.id for x in reg.open_human_questions()] == [qid]

    def test_a_typed_answer_is_routed_as_a_command(self, monkeypatch):
        q, _, _ = _steer(monkeypatch, "/answer q-3 use src/util\n")
        surface = FakeSurface(routed={"/answer q-3 use src/util"})
        actor = self._actor(q, surface=surface)
        got = actor.take(0.5)
        assert got["message"]["command"] is True
        assert actor.open(got) is None  # 런을 열지 않는다
        assert ("route", "/answer q-3 use src/util") in surface.calls

    def test_when_nothing_is_read_the_whole_wait_goes_to_the_box(self, monkeypatch):
        q = MainInput()
        waited: list = []
        monkeypatch.setattr(q, "wait_to_open", lambda timeout: waited.append(timeout))
        self._actor(q, read=lambda t: None).take(0.5)
        assert waited == [0.5]

    def test_after_reading_the_rest_of_the_wait_goes_to_the_box(self, monkeypatch):
        q = MainInput()
        waited: list = []
        monkeypatch.setattr(q, "wait_to_open", lambda timeout: waited.append(timeout))
        self._actor(q, read=lambda t: False).take(0.5)
        assert waited == [0.25]


def test_only_run_wires_the_idle_read():
    """조립: 한가한 동안 읽기를 거는 곳은 run 의 `wire_session` 하나다 — 웹(두 번째 `wire_session`)은 걸지 않는다(조립
    함수는 호스트를 모른다). `run`·`web` 은 typer 데코레이터에 싸여 있어 모듈 원문으로 센다."""
    src = inspect.getsource(M)
    assert src.count("wire_session(") == 2
    assert src.count("on_idle_input=") == 1
    assert "on_idle_input=lambda t: read_terminal_line(main_input, t)" in src


# ── 열린 사람 질문은 세션의 할 일 ───────────────────────────


class TestOpenQuestionsKeepTheSession:
    """답이 올 수 있는 세션만 질문이 열린다(①) — 그래서 열린 질문은 조건 없이 할 일이다. `run` 은 그것만 남으면 끝나지
    않고 대기 알림을 한 번 찍는다."""

    def _policy(self, qs, work, seen):
        reg = SimpleNamespace(
            has_agent_work=lambda: work[0], open_human_questions=lambda: list(qs)
        )
        return QuietPolicy(
            main_input=MainInput(), agent_registry=reg, on_wait=seen.append
        )

    def test_the_wait_notice_comes_once_per_wait(self):
        qs, work, seen = [], [False], []
        policy = self._policy(qs, work, seen)
        steps = []

        def judge():
            steps.append((policy.should_stop(), list(seen)))

        judge()  # 질문 없음
        qs.append("q-3")
        judge()  # 질문만 남음 — 한 번
        judge()  # 같은 대기
        qs.append("q-5")
        judge()  # 같은 대기 중 질문이 늘었다 — 다시 찍지 않는다
        work[0] = True
        judge()  # 답이 와 묻는 쪽이 일한다
        work[0] = False
        judge()  # 다시 질문만 — 새 대기
        qs.clear()
        judge()  # 질문이 다 닫혔다
        assert steps == [
            (True, []),
            (False, ["questions"]),
            (False, ["questions"]),
            (False, ["questions"]),
            (False, ["questions"]),
            (False, ["questions", "questions"]),
            (True, ["questions", "questions"]),
        ]

    def test_the_wait_notice_names_every_open_question(self, reg):
        key = spawn_idle(reg)
        q1 = _open_question(reg, key, "first?")
        q2 = _open_question(reg, key, "second?")
        assert M._questions_wait_notice(reg) == (
            f"❓ Waiting for your answer to {q1}, {q2} — type /answer <q-N> <text> or /close <q-N>, "
            "or press Ctrl-C to quit."
        )

    def test_an_open_question_is_live_work_and_a_killed_askers_is_not(self, reg):
        key = spawn_idle(reg)
        _open_question(reg, key)
        assert session_has_live_work(post=reg._post, agent_registry=reg)
        assert not session_is_busy(post=reg._post, agent_registry=reg)
        assert reg.kill(key) == ""
        assert reg.open_human_questions() == []
        assert not session_has_live_work(post=reg._post, agent_registry=reg)
