"""먼저 친 줄(type-ahead) — 터미널 프롬프트가 뜨기 전에 친 입력이 그 프롬프트의 답이 되는가.

세션 동안 표준입력을 쥐는 프롬프트는 없다(6d — 확인은 번호 항목이고 명령으로 답한다). 남은 프롬프트는 부팅 마법사의
``prompt_user`` 하나다 — 뜨기 전에 친 줄은 버린다(넘길 main 이 아직 없다).

표준입력 자리에 가짜 터미널(pty) 한 쌍의 slave 를 둔다(`isatty` 참). 프롬프트가 입력을 읽는 순간은 ``input`` 을
갈아 끼워 잡는다 — 그때 친 것이 "뜬 뒤에 친 것" 이다."""

from __future__ import annotations

import inspect
import io
import os
import pty
import sys

import pytest
from rich.console import Console

import agent_cli.main as M
import agent_cli.render.base as render_base
from agent_cli.render.minimal import MinimalRenderer


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


def _boot_prompt(renderer, master, monkeypatch, *, ahead: bytes, after: bytes) -> str:
    """``ahead`` 를 친 뒤 부팅 질문을 띄우고, 프롬프트가 읽기 시작할 때 ``after`` 를 친다 — 프롬프트가 받은 값."""
    os.write(master, ahead)

    def typed_after(prompt=""):
        os.write(master, after)
        return sys.stdin.readline().rstrip("\n")

    monkeypatch.setattr("builtins.input", typed_after)
    return renderer.prompt_user("Dialect? ")


def test_a_line_typed_before_the_boot_prompt_does_not_answer_it(terminal, monkeypatch):
    master, renderer = terminal
    assert (
        _boot_prompt(renderer, master, monkeypatch, ahead=b"y\n", after=b"n\n") == "n"
    )


def test_an_unfinished_line_is_dropped(terminal, monkeypatch):
    master, renderer = terminal
    got = _boot_prompt(renderer, master, monkeypatch, ahead=b"yes plea", after=b"n\n")
    assert got == "n"


def test_nothing_happens_when_the_input_is_not_a_terminal(monkeypatch):
    """파이프(하니스·`run … < 파일`)는 건드리지 않는다 — 읽지도 비우지도 않는다."""
    renderer = MinimalRenderer(Console(file=io.StringIO(), width=200))
    r, w = os.pipe()
    os.write(w, b"y\n")
    os.close(w)
    with os.fdopen(r, "r") as pipe:
        monkeypatch.setattr(sys, "stdin", pipe)
        renderer._drop_typeahead()
        assert pipe.read() == "y\n"


def test_nothing_happens_without_terminal_control(terminal, monkeypatch):
    """`termios` 가 없는 곳(Windows) — 거르지 않는다(종전 동작)."""
    master, renderer = terminal
    monkeypatch.setattr(render_base, "termios", None)
    os.write(master, b"y\n")
    renderer._drop_typeahead()
    assert sys.stdin.readline() == "y\n"


def test_run_hands_nothing_to_main_from_a_prompt():
    """6d: 세션 중 표준입력을 쥐는 프롬프트가 없어 넘김 배선이 없다 — 친 줄은 언제나 `read_terminal_line` 이 읽는다."""
    src = inspect.getsource(M)
    assert "on_typeahead" not in src and "hand_typeahead" not in src
