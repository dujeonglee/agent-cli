"""사람의 말이 명령인가 — 웹·CLI 표면의 `route` 가 지금 돌려주는 답을 글 묶음으로 고정한다.

`route` 는 판정과 실행이 붙어 있다. 여기서는 실행을 대역으로 막고(에이전트·스킬·정지·`/sh` 실행) 반환만 본다.
표의 값이 "명령인가" 의 정본이다 — 판정을 실행과 떼어 낸 함수도 같은 표를 지나야 한다.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent_cli.main as M
from agent_cli.main import _command_dispatch, _ConsoleSurface
from agent_cli.web.surface import WebSurface

#: 글 → (웹, CLI). 빈 글(이미지만 보낸 메시지)은 명령이 아니다 — v10.147.1 전에는 `/stop` 판정이 없는 첫 토큰을 꺼내 예외를 냈다.
TABLE: dict[str, tuple] = {
    "/help": (True, True),
    "/help me": (True, True),
    "/compact": (True, True),
    "/compact now": (True, True),
    "/compactx": (True, True),
    "/sh": (True, False),
    "/sh ls": (True, False),
    "/shell x": (True, False),
    "/stop": (True, True),
    "/stop agt-1": (True, True),
    "@": (True, True),
    "@ x": (True, True),
    "@agents": (True, True),
    "@agt-1234abcd": (True, True),
    "@agt-1234abcd hi": (True, True),
    "@x": (True, True),
    "@x do it": (True, True),
    "@x-spawn": (True, True),
    "/skills": (True, True),
    "/foo": (True, True),
    "/foo-bar x": (True, True),
    "/1x": (True, True),
    "/tmp/a.py": (False, False),
    "/Users": (False, False),
    "/etc look": (False, False),
    "/ leading": (False, False),
    "//x": (False, False),
    "hello": (False, False),
    "hello /x": (False, False),
    " /x": (False, False),
    "": (False, False),
    "   ": (False, False),
}


def _spec():
    return SimpleNamespace(
        binding=None, max_turns=0, verbose=False, max_depth=2, ctx=None, session=None
    )


def _web():
    return WebSurface(
        renderer=MagicMock(),
        server=MagicMock(),
        ctx=None,
        binding=None,
        dispatch=_command_dispatch(
            _spec(),
            SimpleNamespace(agent_registry=None),
            MagicMock,
            graceful_interrupt=True,
        ),
    )


def _cli():
    return _ConsoleSurface(
        result_file="",
        dispatch=_command_dispatch(
            _spec(),
            SimpleNamespace(agent_registry=None),
            MagicMock,
            graceful_interrupt=False,
        ),
    )


def _answer(surface, text):
    with (
        patch.object(M, "_dispatch_agent", return_value=("ok", True)),
        patch.object(M, "_dispatch_skill", return_value=("ok", True)),
        patch.object(M, "_dispatch_stop"),
        patch("agent_cli.web.slash._handle_sh", return_value=True),
    ):
        return surface.route(text)


@pytest.mark.parametrize("text", sorted(TABLE))
def test_web_route_answers_as_the_table(text):
    assert _answer(_web(), text) == TABLE[text][0]


@pytest.mark.parametrize("text", sorted(TABLE))
def test_cli_route_answers_as_the_table(text):
    assert _answer(_cli(), text) == TABLE[text][1]


@pytest.mark.parametrize("text", sorted(TABLE))
def test_web_is_command_says_what_route_does(text):
    assert _web().is_command(text) is TABLE[text][0]


@pytest.mark.parametrize("text", sorted(TABLE))
def test_cli_is_command_says_what_route_does(text):
    assert _cli().is_command(text) is TABLE[text][1]
