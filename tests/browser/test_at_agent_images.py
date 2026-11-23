"""main 입력창의 명령에 붙인 이미지 (v10.149.3) — 실브라우저, 실제 웹 표면·펌프·레지스트리.

📎 + ``@agt-<key> 글`` 을 main 에 보내면 그 에이전트의 런이 이미지와 함께 열리고 그 채널의 런 머리에 썸네일이
선다. 글 없는 명령(``/help`` 등)에 📎 를 붙이면 명령은 그대로 돌고 "보내지 않았다" 한 줄이 선다.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_cli.command_table import IMAGES_NOT_TAKEN
from tests.browser import test_image_attach as _ia

_png_bytes, _wait = _ia._png_bytes, _ia._wait
ws = _ia.ws  # 워크스페이스 = tmp 픽스처


@pytest.fixture
def main_pump(stack, ws, monkeypatch, tmp_path):
    """실제 WebSurface + 펌프(main 런은 가짜) + 실제 레지스트리(에이전트 런은 가짜, 시작을 적는다)."""
    from agent_cli import render as render_mod
    from agent_cli.main import _command_dispatch
    from agent_cli.providers.capabilities import ModelCapabilities
    from agent_cli.pump import ForeverPolicy
    from agent_cli.subagent.agents_live import AgentRegistry
    from agent_cli.web.slash import WebDispatchOutput
    from agent_cli.web.surface import WebSurface
    from tests.binding_support import make_binding
    from tests.pump_support import make_pump

    monkeypatch.setattr(render_mod, "_renderer", stack.renderer)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: stack.renderer)
    seen: list = []

    def runner(kind, start, ctx):
        seen.append((start.query, [i["path"] for i in start.images]))
        stack.renderer.final("looked", 1)
        return SimpleNamespace(success=True, output="looked"), 0.01

    reg = AgentRegistry(tmp_path / "agents", runtime={"model": "m"}, runner=runner)
    stack.server.agent_registry = reg
    caps = ModelCapabilities(32768, 4096, True, supports_vision=True)
    stack.server.binding.capabilities = caps
    spec = SimpleNamespace(
        binding=None,
        max_turns=0,
        verbose=False,
        max_depth=2,
        ctx=None,
        session=None,
        mcp_manager=None,
    )
    output = WebDispatchOutput(stack.renderer)
    surface = WebSurface(
        renderer=stack.renderer,
        server=stack.server,
        ctx=stack.ctx,
        binding=make_binding(provider=MagicMock(), capabilities=caps),
        dispatch=_command_dispatch(
            spec,
            SimpleNamespace(agent_registry=reg),
            lambda: output,
            graceful_interrupt=True,
        ),
    )
    main_runs: list = []

    def run_main(start):
        main_runs.append(start.query)
        return SimpleNamespace(output="", success=True, waiting=False)

    pump = make_pump(
        main_input=stack.server.main_input,
        run_main=run_main,
        registry=reg,
        surface=surface,
        policy=ForeverPolicy(),
    )
    worker = threading.Thread(target=pump.run, daemon=True)
    worker.start()
    yield SimpleNamespace(reg=reg, seen=seen, main_runs=main_runs)
    stack.server.main_input.close()
    worker.join(timeout=5)
    reg.shutdown_all()


def _attach(page):
    page.set_input_files(
        "#attach-input",
        {"name": "shot.png", "mimeType": "image/png", "buffer": _png_bytes()},
    )
    page.wait_for_selector("#attach-strip .attach-chip:not(.uploading)", timeout=8000)


def test_at_agent_message_with_an_image_reaches_that_agents_run(stack, page, main_pump):
    reg = main_pump.reg
    key, _ = reg.spawn(name="alpha")
    assert _wait(lambda: reg.get(key).state == "idle")
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#attach", timeout=8000)
    _attach(page)
    page.fill("#input", f"@{key} what is in it?")
    page.press("#input", "Enter")
    assert _wait(lambda: main_pump.seen), "에이전트 런이 열리지 않았다"
    query, paths = main_pump.seen[0]
    assert query.endswith("what is in it?")
    assert len(paths) == 1 and paths[0].startswith(".agent-cli/tmp/uploads/")
    assert main_pump.main_runs == []  # 명령이었다 — main 런은 열리지 않았다
    thumb = f'.card-run[data-ch="{key}"] .run-head .mini-thumbs img'
    page.wait_for_selector(thumb, state="attached", timeout=8000)


def test_command_without_a_destination_says_the_image_was_not_sent(
    stack, page, main_pump
):
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#attach", timeout=8000)
    _attach(page)
    page.fill("#input", "/help")
    page.press("#input", "Enter")
    assert _wait(lambda: IMAGES_NOT_TAKEN in page.inner_text("#messages"))
    assert "Web mode commands:" in page.inner_text("#messages")  # 명령은 그대로 돌았다
    assert main_pump.main_runs == [] and main_pump.seen == []
