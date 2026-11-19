"""글이 빈 사람의 말(이미지만 붙인 메시지)은 명령이 아니다.

웹은 글 없이 이미지만 보낼 수 있다(`/api/input` 의 ``images``). 명령 판정이 빈 글에서
예외를 내면 펌프가 런을 열기 전에 터져 웹 main 워커가 죽고, 그 뒤 입력은 줄만 선다.
런 도중(턴 경계의 주입)이면 그 런이 실패로 끝나고 이미지는 모델에게 가지 않는다.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_cli.main import _command_dispatch, _ConsoleSurface
from agent_cli.main_input import MainInput
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.pump import ForeverPolicy
from agent_cli.web.surface import WebSurface
from tests.binding_support import make_binding
from tests.loop_ports import make_ports, queue_inbox
from tests.loopkit import run_loop
from tests.pump_support import make_pump

REF = {
    "path": "a.png",
    "mime": "image/png",
    "width": 8,
    "height": 4,
    "bytes": 1,
    "sha256": "",
}


def _spec():
    return SimpleNamespace(
        binding=None, max_turns=0, verbose=False, max_depth=2, ctx=None, session=None
    )


def _web_surface():
    owners = SimpleNamespace(agent_registry=None)
    # 이미지를 보는 모델 — 런 시작 알림의 능력 조회가 프로브 없이 끝난다
    caps = ModelCapabilities(32768, 4096, False, supports_vision=True)
    return WebSurface(
        renderer=MagicMock(),
        server=MagicMock(),
        ctx=None,
        binding=make_binding(provider=MagicMock(), capabilities=caps),
        dispatch=_command_dispatch(_spec(), owners, MagicMock, graceful_interrupt=True),
    )


def _cli_surface():
    owners = SimpleNamespace(agent_registry=None)
    return _ConsoleSurface(
        result_file="",
        dispatch=_command_dispatch(
            _spec(), owners, MagicMock, graceful_interrupt=False
        ),
    )


@pytest.mark.parametrize("text", ["", "   ", "\n", " \t "])
@pytest.mark.parametrize("make", [_web_surface, _cli_surface], ids=["web", "cli"])
def test_blank_text_is_not_a_command(make, text):
    assert make().route(text) is False


def test_image_only_message_opens_a_run_on_the_web_pump():
    """실제 `WebSurface` + 펌프 — 빈 글의 이미지 메시지가 런을 열고, 다음 말도 처리된다."""
    mi = MainInput()
    mi.enqueue("c1", "", images=[REF])
    mi.enqueue("c1", "next")
    mi.close()
    ran = []
    pump = make_pump(
        main_input=mi,
        run_main=lambda start: ran.append((start.query, start.images)),
        surface=_web_surface(),
        policy=ForeverPolicy(),
    )
    pump.run()
    assert ran == [("", [REF]), ("next", [])]


def test_image_only_message_injected_mid_run_reaches_the_model(tmp_path):
    """런 도중 턴 경계에 온 이미지만 든 말 — 명령 판정을 지나 user 레코드(자리표시·이미지)가 된다."""
    from agent_cli.context.manager import ContextManager

    ctx = ContextManager(session_dir=tmp_path)
    provider = MagicMock()
    provider.call.side_effect = [
        LLMResponse(
            content=json.dumps(
                {"action": "complete", "result": "done", "answers": ["u-2"]}
            )
        )
    ]
    pending = [{"id": "u-2", "nickname": "dj", "text": "", "images": [REF]}]
    surface = _web_surface()
    result = run_loop(
        query="do Y",
        query_author="dj",
        ports=make_ports(
            inbox=queue_inbox(lambda: pending.pop(0) if pending else None),
            route_message=surface.route,
        ),
        provider=provider,
        capabilities=ModelCapabilities(32768, 4096, False),
        model="m",
        ctx=ctx,
        max_turns=3,
    )
    assert result.success
    users = [m for m in ctx.get_raw_messages() if m.get("role") == "user"]
    assert users[-1]["content"] == "[dj]: \n[image: a.png (8×4)]"
    assert users[-1]["images"] == [REF]
