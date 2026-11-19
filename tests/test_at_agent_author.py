"""main 입력창의 ``@agt-…`` 요청의 작성자 (v10.149.4) — 에이전트 채널과 같은 규칙(`main_input.human_addr`).

종전엔 main 입력창에서 보낸 ``@agt-<key> 글`` 이 늘 ``user`` 로 가 에이전트가 ``[user]:`` 를 받았고, 같은 사람이
그 채널에 쓰면 ``[user:<별명>]:`` 를 받았다. 이제 별명이 있으면 ``user:<별명>``, 없으면(``run`` 의 argv 질의·터미널
줄) ``user``.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_cli.main import _command_dispatch
from agent_cli.main_input import human_addr
from agent_cli.runtime import MainLoop
from agent_cli.web.surface import WebSurface
from tests.test_agents_live import (  # noqa: F401
    _FakeLoopResult,
    make_registry,
    renderer,
)
from tests.test_engine_p2 import wait_until


@pytest.fixture
def live(tmp_path):
    seen: list = []

    def runner(kind, start, ctx):
        seen.append(start.query)
        return _FakeLoopResult(output="ok"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    key, _ = reg.spawn(name="a")
    assert wait_until(lambda: reg.get(key).state == "idle")
    spec = SimpleNamespace(
        binding=None, max_turns=0, verbose=False, max_depth=2, ctx=None, session=None
    )
    surface = WebSurface(
        renderer=MagicMock(),
        server=MagicMock(),
        ctx=None,
        binding=None,
        dispatch=_command_dispatch(
            spec,
            SimpleNamespace(agent_registry=reg),
            MagicMock,
            graceful_interrupt=True,
        ),
    )
    yield SimpleNamespace(reg=reg, key=key, surface=surface, seen=seen)
    reg.shutdown_all()


def _route(live, text, nickname):
    item = {"id": "u-1", "text": text, "nickname": nickname, "images": []}
    start = MainLoop.start(item, threading.Event())
    assert live.surface.route(start.query, start, item=item) is True


def test_human_addr():
    assert human_addr("dj") == "user:dj"
    assert human_addr(None) == "user" and human_addr("") == "user"


@pytest.mark.parametrize(("nickname", "label"), [("dj", "[user:dj]"), (None, "[user]")])
def test_at_agent_request_is_labelled_like_the_channel(live, nickname, label):
    _route(live, f"@{live.key} look at this", nickname)
    assert wait_until(lambda: live.seen)
    assert live.seen[-1] == f"{label}: look at this"
    # 같은 사람이 그 채널에 쓴 것과 같다(채널 엔드포인트의 작성자 = human_addr(별명))
    assert live.reg.request(live.key, "look at this", author=human_addr(nickname)) == ""
    assert wait_until(lambda: len(live.seen) == 2)
    assert live.seen[-1] == live.seen[-2]


def test_spawn_task_is_labelled_too(tmp_path, monkeypatch):
    seen: list = []

    def runner(kind, start, ctx):
        seen.append(start.query)
        return _FakeLoopResult(output="ok"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    real = reg.spawn
    monkeypatch.setattr(reg, "spawn", lambda profile="", **kw: real(name=profile))
    monkeypatch.setattr(
        "agent_cli.main._parse_at_profile",
        lambda name: (name[: -len("-spawn")], "spawn"),
    )
    spec = SimpleNamespace(
        binding=None, max_turns=0, verbose=False, max_depth=2, ctx=None, session=None
    )
    dispatch = _command_dispatch(
        spec, SimpleNamespace(agent_registry=reg), MagicMock, graceful_interrupt=True
    )
    try:
        item = {
            "id": "u-1",
            "text": "@helper-spawn look",
            "nickname": "dj",
            "images": [],
        }
        assert dispatch(item["text"], None, item) is True
        assert wait_until(lambda: seen)
        assert seen[-1] == "[user:dj]: look"
    finally:
        reg.shutdown_all()


def test_the_reply_still_goes_to_the_window_not_to_main(live, renderer):  # noqa: F811
    """작성자가 ``user:<별명>`` 이 되어도 회신은 그 채널 창으로(사람 주소) — main 메일함에는 아무것도 서지 않는다."""
    with renderer.lock:
        renderer.calls.clear()
    _route(live, f"@{live.key} hi", "dj")
    assert wait_until(
        lambda: any(
            c[1].get("direction") == "out" for c in renderer.named("agent_message")
        )
    )
    outs = [
        c[1] for c in renderer.named("agent_message") if c[1].get("direction") == "out"
    ]
    assert outs[-1]["to"] == "user:dj"
    assert [e.text for e in live.reg._post.queued("main")] == []
