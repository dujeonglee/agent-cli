"""⚠ 폴백 줄은 빚 하나당 한 줄 (v10.165.0).

회신 없이 끝난 런의 폴백은 빚마다 간다. 창의 ⚠ 줄도 빚마다 하나, 그 빚의 상대를 꼬리에 단다 — 종전엔 런을 연 첫 항목의
상대로 한 줄뿐이었다. 런 도중 흡수된 요청의 줄도 같은 블록에 선다(머리 줄이 그 id 를 블록에 매어 둔다). 진짜 레지스트리·
진짜 웹 렌더러·헤드리스 크롬.
"""

from __future__ import annotations

import threading

import agent_cli.render as render_mod
from agent_cli.subagent.agents_live import AgentRegistry
from tests.browser.test_channel_jump import _chip, _wait
from tests.test_agents_live import _FakeLoopResult


def test_a_run_that_ends_owing_two_replies_shows_two_fallback_rows(
    stack, page, monkeypatch, tmp_path
):
    monkeypatch.setattr(render_mod, "_renderer", stack.renderer)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: stack.renderer)
    absorb, door = threading.Event(), threading.Event()
    box: dict = {}

    def runner(kind, start, ctx):
        if kind.tm.key == box.get("helper"):
            absorb.wait(10)
            box["reg"].absorb_pending(kind.tm)
        else:
            door.wait(10)
        return _FakeLoopResult(output="ran out of turns"), 0.01

    reg = box["reg"] = AgentRegistry(
        tmp_path, runtime={"model": "m", "timeout": 5}, runner=runner
    )
    try:
        stack.emit_ready()
        key, _ = reg.spawn(name="helper")
        peer, _ = reg.spawn(name="peer")
        box["helper"] = key
        assert _wait(lambda: reg.get(key).state == reg.get(peer).state == "idle")
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, key).count() > 0)
        _chip(page, key).click()

        assert reg.request(key, "TASK-A", author="main", item_id="r-901") == ""
        assert _wait(lambda: reg.get(key).state == "busy")
        assert reg.request(key, "TASK-B", author=f"agent:{peer}", item_id="r-902") == ""
        absorb.set()
        assert _wait(lambda: reg.get(key).state == "idle")

        def tails():
            rows = page.locator(
                f'#messages > .card-run[data-task-id="{key}#r-901"] .run-body > .card-fallback'
            )
            assert _wait(lambda: rows.count() == 2)
            assert page.locator("#messages .card-fallback").count() == 2
            return [rows.nth(i).locator(".peer").inner_text() for i in range(2)]

        live = tails()
        assert live[0] == "main" and live[1].endswith(" peer")
        page.reload()
        assert _wait(lambda: _chip(page, key).count() > 0)
        _chip(page, key).click()
        assert tails() == live  # 다시 붙어도 같은 글자 — 로스터가 줄보다 먼저 온다
    finally:
        absorb.set()
        door.set()
        reg.shutdown_all()
