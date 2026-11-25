"""먼저 친 줄(type-ahead) — 터미널 프롬프트(확인 대화·`ask`)가 뜨기 전에 친 입력이 그 프롬프트의 답이 되는가.

표준입력 자리에 가짜 터미널(pty) 한 쌍의 slave 를 둔다(`isatty` 참). ``ahead`` 는 프롬프트를 부르기 전에 친 것,
``after`` 는 프롬프트가 입력을 기다리기 시작한 뒤(`Renderer.is_awaiting_input` — 제품이 읽는 값) 친 것이다."""

from __future__ import annotations

import inspect
import io
import os
import pty
import sys
import threading

import pytest
from rich.console import Console

import agent_cli.main as M
import agent_cli.render.base as render_base
from agent_cli.main_input import MainInput
from agent_cli.render.base import ConfirmOption
from agent_cli.render.minimal import MinimalRenderer
from tests.test_agents_live import wait_until

_OPTIONS = [ConfirmOption("y", "once"), ConfirmOption("n", "deny")]


@pytest.fixture
def terminal(monkeypatch):
    """가짜 터미널 — ``(쓰기 fd, 렌더러)``. 렌더러의 출력은 버린다."""
    master, slave = pty.openpty()
    stdin = os.fdopen(slave, "r")
    monkeypatch.setattr(sys, "stdin", stdin)
    renderer = MinimalRenderer(Console(file=io.StringIO(), width=200))
    yield master, renderer
    stdin.close()
    os.close(master)


def _prompt(renderer, master, ask, *, ahead: bytes, after: bytes = b""):
    """``ahead`` 를 친 뒤 프롬프트를 띄우고, 입력을 기다리기 시작하면 ``after`` 를 친다 — 프롬프트가 받은 값."""
    if ahead:
        os.write(master, ahead)
    got: dict = {}
    t = threading.Thread(target=lambda: got.update(value=ask(renderer)), daemon=True)
    t.start()
    assert wait_until(lambda: renderer.is_awaiting_input() or not t.is_alive())
    if after and t.is_alive():
        os.write(master, after)
    t.join(5)
    assert not t.is_alive()
    return got["value"]


def _confirm(renderer):
    return renderer.confirm("go? ", _OPTIONS, default_key="n")


def _answer(renderer):
    return renderer.prompt_user("Your answer: ", multiline=True)


def test_a_line_typed_before_the_confirm_does_not_decide_it(terminal):
    """먼저 친 `y` 는 승인이 아니다 — main 에게 넘어가고, 결정은 뜬 뒤에 친 `n` 이다."""
    master, renderer = terminal
    handed: list[str] = []
    renderer.on_typeahead = handed.extend
    assert _prompt(renderer, master, _confirm, ahead=b"y\n", after=b"n\n") == ("n", "")
    assert handed == ["y"]


def test_lines_typed_before_the_ask_prompt_are_not_its_answer(terminal):
    master, renderer = terminal
    handed: list[str] = []
    renderer.on_typeahead = handed.extend
    got = _prompt(
        renderer, master, _answer, ahead=b"use the old dir\n", after=b"use src/util\n"
    )
    assert got == "use src/util"
    assert handed == ["use the old dir"]


def test_a_paste_after_the_prompt_is_kept_whole(terminal):
    master, renderer = terminal
    handed: list[str] = []
    renderer.on_typeahead = handed.extend
    got = _prompt(renderer, master, _answer, ahead=b"", after=b"line one\nline two\n")
    assert got == "line one\nline two"
    assert handed == []


def test_an_unfinished_line_is_dropped(terminal):
    master, renderer = terminal
    handed: list[str] = []
    renderer.on_typeahead = handed.extend
    got = _prompt(renderer, master, _confirm, ahead=b"yes plea", after=b"n\n")
    assert got == ("n", "")
    assert handed == []


def test_with_nobody_to_take_them_the_lines_are_dropped(terminal):
    """넘길 곳이 없으면(웹·부트 질문) 버린다 — 그래도 프롬프트의 답이 되지는 않는다."""
    master, renderer = terminal
    assert renderer.on_typeahead is None
    assert _prompt(renderer, master, _confirm, ahead=b"y\n", after=b"n\n") == ("n", "")


def test_nothing_happens_when_the_input_is_not_a_terminal(monkeypatch):
    """파이프(하니스·`run … < 파일`)는 건드리지 않는다 — 읽지도 비우지도 않는다."""
    renderer = MinimalRenderer(Console(file=io.StringIO(), width=200))
    handed: list[str] = []
    renderer.on_typeahead = handed.extend
    r, w = os.pipe()
    os.write(w, b"y\n")
    os.close(w)
    with os.fdopen(r, "r") as pipe:
        monkeypatch.setattr(sys, "stdin", pipe)
        renderer._hand_over_typeahead()
        assert handed == [] and pipe.read() == "y\n"


def test_nothing_happens_without_terminal_control(terminal, monkeypatch):
    """`termios` 가 없는 곳(Windows) — 거르지 않는다(종전 동작)."""
    master, renderer = terminal
    handed: list[str] = []
    renderer.on_typeahead = handed.extend
    monkeypatch.setattr(render_base, "termios", None)
    os.write(master, b"y\n")
    renderer._hand_over_typeahead()
    assert handed == []
    assert sys.stdin.readline() == "y\n"


# ── run: main 에게 넘긴다 ───────────────────────────────────


def test_handed_lines_go_to_main_and_each_says_so(monkeypatch):
    printed: list[str] = []
    monkeypatch.setattr(M.console, "print", lambda text, **kw: printed.append(text))
    q = MainInput(is_command=lambda t: t.startswith("/"))
    M.hand_typeahead_to_main(q, ["y", "/stop agt-3"])
    items = q.snapshot()
    assert [i["text"] for i in items] == ["y", "/stop agt-3"]
    assert [bool(i.get("command")) for i in items] == [False, True]
    assert printed == [
        M.TYPEAHEAD_SENT_TO_MAIN.format(line="y"),
        M.TYPEAHEAD_SENT_TO_MAIN.format(line="/stop agt-3"),
    ]
    assert M.TYPEAHEAD_SENT_TO_MAIN.format(line="y") == (
        "  ↪ typed before this prompt — sent to main as a new message: y"
    )


def test_only_run_hands_typeahead_to_main():
    """조립: 넘길 곳을 거는 것은 `run` 하나다 — 웹은 걸지 않는다(웹 서버는 표준입력을 읽지 않는다). `run`·`web` 은
    typer 데코레이터에 싸여 있어 모듈 원문으로 센다."""
    src = inspect.getsource(M)
    assert src.count(".on_typeahead = ") == 1
    assert "get_renderer().on_typeahead = lambda lines: hand_typeahead_to_main(" in src
