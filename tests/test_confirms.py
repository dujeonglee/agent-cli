"""사람에게 건 확인 `c-N` (6d-1) — 그 자리에서 기다리는 호출, 깨우는 셋(답·정지·세션 종료), 답하는 명령 하나.

규칙(사용자 결정): 확인의 답은 번호를 적은 명령(``/approve``·``/allow``·``/deny``)과 웹 트레이의 버튼뿐이다. 그 밖의
입력은 언제나 main 의 요청이다 — 터미널의 ``y`` 도. 명령은 들어오는 문에서 바로 답이 된다.

기다림은 실제 레지스트리의 `ask_confirm` 을 다른 스레드에서 돌리고, 표면 사건(`open_confirms`)이 서는 것을 Event 로
기다린다 — 시간 판정이 없다.
"""

from __future__ import annotations

import io
import select
import threading

import pytest
from rich.console import Console

import agent_cli.main as M
import agent_cli.render as render_mod
from agent_cli import confirm, item_ids, session_slots
from agent_cli.command_table import command_kind
from agent_cli.main_input import MainInput
from agent_cli.render.minimal import MinimalRenderer
from agent_cli.render.web import _needs_answer
from agent_cli.tools import _confine, human_note, shell
from tests.test_agents_live import make_registry


class _Surface(MinimalRenderer):
    """진짜 CLI 렌더러(출력은 버퍼) + 열린 확인 목록이 바뀔 때마다 알린다."""

    def __init__(self):
        self.buf = io.StringIO()
        super().__init__(Console(file=self.buf, width=200))
        self.lists: list[list[dict]] = []
        self.opened = threading.Event()

    def can_prompt(self) -> bool:
        return True

    def open_confirms(self, confirms):
        self.lists.append(confirms)
        if confirms:
            self.opened.set()


@pytest.fixture
def surface(monkeypatch):
    r = _Surface()
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    return r


@pytest.fixture
def reg(tmp_path):
    r = make_registry(tmp_path)
    session_slots.set_agent_registry(r)
    yield r
    session_slots.set_agent_registry(None)
    r.shutdown_all()


def _confirm(choices=None, **kw) -> confirm.Confirm:
    return confirm.Confirm(
        id=item_ids.next_id(item_ids.CONFIRM),
        asker="main",
        title="Dangerous command detected (`rm`)",
        detail="",
        choices=choices
        or confirm.safety_choices(
            "once (allow this command)", "always allow `rm` this session"
        ),
        command="rm -rf build/",
        **kw,
    )


class _Waiting:
    """다른 스레드에서 ``ask_confirm`` 을 돌린다 — 열린 것이 표면에 선 뒤 돌아온다."""

    def __init__(self, reg, surface, c, *, stopped=None, wait_input=None):
        self.result: list = []
        self.t = threading.Thread(
            target=lambda: self.result.append(
                reg.ask_confirm(c, stopped=stopped, wait_input=wait_input)
            ),
            daemon=True,
        )
        self.t.start()
        assert surface.opened.wait(5)

    def decision(self) -> confirm.Decision:
        self.t.join(5)
        assert not self.t.is_alive()
        return self.result[0]


# ── 기다림과 깨우기 ─────────────────────────────────────────


class TestWaitInPlace:
    def test_an_answer_wakes_the_call_with_its_note_and_author(self, reg, surface):
        c = _confirm()
        w = _Waiting(reg, surface, c)
        assert [x.id for x in reg.open_human_confirms()] == [c.id]
        assert (
            reg.answer_confirm(c.id, confirm.DENY, "  wrong dir ", by="user:dj") == ""
        )
        assert w.decision() == confirm.Decision(confirm.DENY, "wrong dir", "user:dj")
        assert reg.open_human_confirms() == [] and surface.lists[-1] == []

    def test_a_stop_wakes_the_call_and_closes_the_item(self, reg, surface):
        stop = threading.Event()
        c = _confirm()
        w = _Waiting(reg, surface, c, stopped=stop.is_set)
        stop.set()
        assert w.decision() == confirm.Decision(confirm.STOPPED)
        assert reg.open_human_confirms() == []
        # 멈춘 뒤 온 답은 닫힌 번호의 답이다
        assert reg.answer_confirm(c.id, confirm.APPROVE, "", by="user") == (
            f'unknown or already-answered confirmation "{c.id}"'
        )

    def test_the_wait_reads_input_between_looks(self, reg, surface):
        """기다리는 동안 사람 입력을 보는 법을 매 조각 부른다 — 터미널에서는 그 자리에서 명령이 답이 된다."""
        c = _confirm()
        calls: list[float] = []

        def read(timeout):
            calls.append(timeout)
            if len(calls) == 3:
                reg.answer_confirm(c.id, confirm.APPROVE, "", by="user")

        w = _Waiting(reg, surface, c, wait_input=read)
        assert w.decision().action == confirm.APPROVE
        assert len(calls) == 3 and set(calls) == {confirm.STOP_POLL_S}

    def test_two_confirms_are_open_together_and_answered_each(self, reg, surface):
        """동시 확인은 각자 답한다 — always 를 받은 것이 열린 다른 확인을 닫지 않는다(E6)."""
        c1, c2 = _confirm(), _confirm()
        w1 = _Waiting(reg, surface, c1)
        surface.opened.clear()
        w2 = _Waiting(reg, surface, c2)
        assert [x.id for x in reg.open_human_confirms()] == [c1.id, c2.id]
        assert reg.answer_confirm(c1.id, confirm.ALLOW, "", by="user") == ""
        assert w1.decision().action == confirm.ALLOW
        assert [x.id for x in reg.open_human_confirms()] == [c2.id]
        reg.answer_confirm(c2.id, confirm.APPROVE, "", by="user")
        assert w2.decision().action == confirm.APPROVE

    def test_only_one_of_two_answers_counts(self, reg, surface):
        c = _confirm()
        w = _Waiting(reg, surface, c)
        assert reg.answer_confirm(c.id, confirm.APPROVE, "", by="user:a") == ""
        assert reg.answer_confirm(c.id, confirm.DENY, "", by="user:b") != ""
        assert w.decision().by == "user:a"


class TestAnswerRefusals:
    def test_wrong_numbers_and_kinds_are_refused_and_nothing_closes(self, reg, surface):
        c = _confirm(
            choices=(
                confirm.Choice(confirm.APPROVE, "Continue"),
                confirm.Choice(confirm.DENY, "Stop"),
            )
        )
        w = _Waiting(reg, surface, c)
        qid, err = reg.question_port().ask("which branch?")
        assert not err
        assert reg.answer_confirm("c-999", confirm.APPROVE, "", by="user") == (
            'unknown or already-answered confirmation "c-999"'
        )
        assert reg.answer_confirm(qid, confirm.APPROVE, "", by="user") == (
            f'unknown or already-answered confirmation "{qid}"'
        )
        assert reg.answer_confirm(c.id, confirm.APPROVE, "", by="agent:agt-1") == (
            f'confirmation "{c.id}" is addressed to the person, not "agent:agt-1"'
        )
        assert reg.answer_confirm(c.id, confirm.ALLOW, "", by="user") == (
            f'confirmation "{c.id}" has no always option'
        )
        assert [x.id for x in reg.open_human_confirms()] == [c.id]
        assert [q.id for q in reg.open_human_questions()] == [qid]
        reg.answer_confirm(c.id, confirm.DENY, "", by="user")
        assert w.decision().action == confirm.DENY


# ── 명령 하나 ───────────────────────────────────────────────


class TestCommands:
    @pytest.mark.parametrize("text", ["/approve c-1", "/allow c-1 ok", "/deny c-1 no"])
    def test_they_are_confirm_commands(self, text):
        assert command_kind(text) == "confirm"

    @pytest.mark.parametrize(
        ("text", "usage"),
        [
            ("/approve", "usage: /approve <c-N> [note]"),
            ("/allow", "usage: /allow <c-N> [note]"),
            ("/deny", "usage: /deny <c-N> [reason]"),
        ],
    )
    def test_without_a_number_only_the_usage_even_with_one_open(
        self, reg, surface, text, usage
    ):
        c = _confirm()
        w = _Waiting(reg, surface, c)
        assert confirm.answer_command(text, reg, "user") == (usage, False)
        assert [x.id for x in reg.open_human_confirms()] == [c.id]
        reg.answer_confirm(c.id, confirm.DENY, "", by="user")
        w.decision()

    @pytest.mark.parametrize(
        ("name", "action", "done"),
        [
            ("/approve", confirm.APPROVE, "approved"),
            ("/allow", confirm.ALLOW, "allowed"),
            ("/deny", confirm.DENY, "denied"),
        ],
    )
    def test_the_answer_and_its_line(self, reg, surface, name, action, done):
        c = _confirm()
        w = _Waiting(reg, surface, c)
        assert confirm.answer_command(f"{name} {c.id} keep it short", reg, "user") == (
            f"{done} {c.id}",
            True,
        )
        assert w.decision() == confirm.Decision(action, "keep it short", "user")

    def test_an_unknown_number_names_the_command(self, reg):
        assert confirm.answer_command("/deny c-9 x", reg, "user") == (
            '/deny: unknown or already-answered confirmation "c-9"',
            False,
        )

    def test_a_confirm_number_is_not_a_question(self, reg, surface):
        c = _confirm()
        w = _Waiting(reg, surface, c)
        out = _Lines()
        M.try_dispatch_agent_or_skill(
            f"/answer {c.id} yes", out, binding=None, max_turns=1, verbose=False,
            max_depth=1, ctx=None, session=None, mcp_manager=None, agent_registry=reg,
            author="user",
        )  # fmt: skip
        assert out.lines == [
            (f'/answer: unknown or already-answered question "{c.id}"', False)
        ]
        assert [x.id for x in reg.open_human_confirms()] == [c.id]
        reg.answer_confirm(c.id, confirm.DENY, "", by="user")
        w.decision()

    def test_the_queued_path_is_the_same_function(self, reg):
        """큐를 지난 확인 명령(`run` 의 argv 질의)도 같은 함수로 답한다."""
        out = _Lines()
        assert M.try_dispatch_agent_or_skill(
            "/approve c-9", out, binding=None, max_turns=1, verbose=False, max_depth=1,
            ctx=None, session=None, mcp_manager=None, agent_registry=reg, author="user",
        )  # fmt: skip
        assert out.lines == [
            ('/approve: unknown or already-answered confirmation "c-9"', False)
        ]


class _Lines(M.DispatchOutput):
    def __init__(self):
        self.lines: list[tuple[str, bool]] = []

    def agent_dispatch_result(self, text, success):
        self.lines.append((text, success))


# ── 터미널의 문 ─────────────────────────────────────────────


class _Tty(io.StringIO):
    def isatty(self):
        return True


def _typed(monkeypatch, text: str) -> MainInput:
    stdin = _Tty(text)
    monkeypatch.setattr(M.sys, "stdin", stdin)
    monkeypatch.setattr(
        select,
        "select",
        lambda r, w, x, t: (r if stdin.tell() < len(text) else [], [], []),
    )
    return MainInput(is_command=lambda t: t.startswith("/"))


class TestTerminalDoor:
    def test_a_typed_confirm_command_answers_where_it_is_read(
        self, reg, surface, monkeypatch
    ):
        c = _confirm()
        w = _Waiting(reg, surface, c)
        printed: list[str] = []
        monkeypatch.setattr(
            M._ConsoleDispatchOutput,
            "agent_dispatch_result",
            lambda self, text, ok: printed.append(text),
        )
        q = _typed(monkeypatch, f"/approve {c.id} go\n")
        assert M.read_terminal_line(q, 0) is False  # 줄 세우지 않았다
        assert q.snapshot() == []
        assert printed == [f"approved {c.id}"]
        assert w.decision() == confirm.Decision(confirm.APPROVE, "go", "user")

    def test_y_is_a_request_to_main_and_the_confirm_stays_open(
        self, reg, surface, monkeypatch
    ):
        """E1: 터미널의 ``y`` 는 답이 아니다 — main 의 요청이다."""
        c = _confirm()
        w = _Waiting(reg, surface, c)
        q = _typed(monkeypatch, "y\n")
        assert M.read_terminal_line(q, 0) is True
        assert [(i["text"], i["command"]) for i in q.snapshot()] == [("y", False)]
        assert [x.id for x in reg.open_human_confirms()] == [c.id]
        reg.answer_confirm(c.id, confirm.DENY, "", by="user")
        w.decision()


# ── 화면 ────────────────────────────────────────────────────


class TestTerminalLine:
    def test_the_line_carries_its_number_what_and_how_to_answer(self, reg, surface):
        c = _confirm()
        c.danger_spans = [(0, 2)]
        w = _Waiting(reg, surface, c)
        lines = [ln for ln in surface.buf.getvalue().splitlines() if ln.strip()]
        assert lines == [
            f"⚠ [main → user] {c.id}  Dangerous command detected (`rm`)",
            "  $ rm -rf build/",
            f"  approve with: /approve {c.id} · always: /allow {c.id} · deny with: /deny {c.id} <reason>",
        ]
        reg.answer_confirm(c.id, confirm.DENY, "", by="user")
        w.decision()

    def test_a_path_confirm_shows_its_listing(self, reg, surface):
        c = _confirm()
        c.command = None
        c.title = "write_file touches path(s) outside the workspace"
        c.detail = "  (workspace: /w):\n  /etc/hosts\n    ↳ outside the workspace"
        w = _Waiting(reg, surface, c)
        lines = [ln for ln in surface.buf.getvalue().splitlines() if ln.strip()]
        assert lines == [
            f"⚠ [main → user] {c.id}  write_file touches path(s) outside the workspace",
            "  (workspace: /w):",
            "  /etc/hosts",
            "    ↳ outside the workspace",
            f"  approve with: /approve {c.id} · always: /allow {c.id} · deny with: /deny {c.id} <reason>",
        ]
        reg.answer_confirm(c.id, confirm.DENY, "", by="user")
        w.decision()

    def test_the_web_list_carries_what_the_tray_draws(self, reg, surface):
        c = _confirm()
        w = _Waiting(reg, surface, c)
        (item,) = surface.lists[0]
        assert (
            item["id"] == c.id and item["asker"] == "main" and item["label"] == "main"
        )
        assert item["title"] == "Dangerous command detected (`rm`)"
        assert item["command"] == "rm -rf build/"
        assert item["choices"] == [
            {"action": "approve", "label": "once (allow this command)"},
            {"action": "deny", "label": "deny"},
            {"action": "allow", "label": "always allow `rm` this session"},
        ]
        assert {"agent", "reasoning", "action"} <= set(item)
        assert _needs_answer({"open_confirms": {"confirms": surface.lists[0]}})
        assert not _needs_answer({"open_confirms": {"confirms": []}})
        reg.answer_confirm(c.id, confirm.DENY, "", by="user")
        w.decision()


# ── 도구의 확인 ─────────────────────────────────────────────


class TestToolsAsk:
    """도구가 연 확인 — 승인·거절·정지 글은 모델이 읽는다. 승인·거절은 종전 글 그대로, 정지는 새 문장 하나."""

    @pytest.fixture(autouse=True)
    def _dangerous(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_confine, "temp_targets_only", lambda c, k: False)
        monkeypatch.setattr(shell, "_session_allowlist", set())
        human_note.clear()

    def _ask_in_thread(self, reg, surface, **kw):
        out: list = []
        notes: list = []

        def ask():
            out.append(shell.confirm_dangerous("rm -rf build", **kw))
            notes.extend(human_note.take())  # 남긴 말은 그 스레드의 보관함에 쌓인다

        t = threading.Thread(target=ask, daemon=True)
        self.notes = notes
        t.start()
        assert surface.opened.wait(5)
        (c,) = reg.open_human_confirms()
        return c, t, out

    def test_stopped_while_waiting_is_the_one_new_sentence(self, reg, surface):
        stop = threading.Event()
        c, t, out = self._ask_in_thread(reg, surface, stopped=stop.is_set)
        stop.set()
        t.join(5)
        assert out == [
            f"This run was stopped while this call was waiting for approval [{c.id}] — it did not run."
        ]
        assert self.notes == []

    def test_deny_is_the_old_text_and_the_reason_is_a_note(self, reg, surface):
        c, t, out = self._ask_in_thread(reg, surface)
        reg.answer_confirm(c.id, confirm.DENY, "not that dir", by="user")
        t.join(5)
        assert out == ["User denied command containing `rm`: rm -rf build"]
        assert self.notes == ["[User note when denying: not that dir]"]

    def test_allow_opens_the_keyword_for_the_session(self, reg, surface):
        c, t, out = self._ask_in_thread(reg, surface)
        reg.answer_confirm(c.id, confirm.ALLOW, "", by="user")
        t.join(5)
        assert out == [None] and "rm" in shell._session_allowlist
        assert c.asker == "main"

    def test_an_agents_confirm_is_labelled_by_its_owner(self, reg, surface):
        c, t, out = self._ask_in_thread(reg, surface, owner="agent:agt-1")
        assert c.asker == "agt-1"
        assert f"[agt-1 → user] {c.id}" in surface.buf.getvalue()
        reg.answer_confirm(c.id, confirm.APPROVE, "", by="user")
        t.join(5)
        assert out == [None]

    def test_nowhere_to_answer_is_refused_as_before(self, monkeypatch, surface):
        session_slots.set_agent_registry(None)
        assert shell.confirm_dangerous("rm -rf build").startswith(
            "Refused: command contains `rm` but this interface can't prompt"
        )
