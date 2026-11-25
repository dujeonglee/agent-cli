"""먼저 친 줄(type-ahead) — 터미널 프롬프트(확인 대화·`ask`)가 뜨기 전에 친 입력이 그 프롬프트의 답이 되는가.

표준입력 자리에 가짜 터미널(pty) 한 쌍의 slave 를 둔다(`isatty` 참). ``ahead`` 는 프롬프트를 부르기 전에 친 것,
``after`` 는 프롬프트가 입력을 기다리기 시작한 뒤(`Renderer.is_awaiting_input` — 제품이 읽는 값) 친 것이다."""

from __future__ import annotations

import io
import os
import pty
import sys
import threading

import pytest
from rich.console import Console

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


def test_a_line_typed_before_the_confirm_decides_it(terminal):
    master, renderer = terminal
    assert _prompt(renderer, master, _confirm, ahead=b"y\n", after=b"n\n") == ("y", "")


def test_lines_typed_before_the_ask_prompt_become_its_answer(terminal):
    master, renderer = terminal
    got = _prompt(renderer, master, _answer, ahead=b"use the old dir\nuse src/util\n")
    assert got == "use the old dir\nuse src/util"


def test_an_unfinished_line_joins_what_is_typed_after(terminal):
    master, renderer = terminal
    got = _prompt(renderer, master, _confirm, ahead=b"yes plea", after=b"n\n")
    assert got == ("n", "yes plean")
