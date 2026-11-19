"""채널 칩의 ↻(되살리기)가 거절되면 사유를 알린다 (v10.155.0, 실브라우저·실제 레지스트리).

종전엔 응답을 버려 한도(409 ``agent limit reached …``)에 걸린 ↻ 가 아무 표시 없이 끝났다(실측). 이제 입력창 아래에
``Not revived — <사유>`` 가 서고 ↻ 는 다시 누를 수 있다.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

# 입력창 아래 알림 줄을 모두 적는다(4초 뒤 지워져도 놓치지 않게)
NOTICES_JS = """() => {
  window.__notices = [];
  new MutationObserver(() => {
    const n = document.querySelector('#attach-strip .attach-msg');
    if (n) window.__notices.push(n.textContent);
  }).observe(document.getElementById('attach-strip'),
             {childList: true, subtree: true, characterData: true});
}"""


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _notices(page) -> list:
    return page.evaluate("() => window.__notices || []")


@pytest.fixture
def two_agents_one_dead(stack, monkeypatch, tmp_path):
    from agent_cli import render as render_mod
    from agent_cli.subagent.agents_live import AgentRegistry

    monkeypatch.setattr(render_mod, "_renderer", stack.renderer)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: stack.renderer)
    reg = AgentRegistry(
        tmp_path / "a",
        runtime={"model": "m"},
        runner=lambda kind, start, ctx: (
            SimpleNamespace(success=True, output="ok"),
            0.01,
        ),
    )
    stack.server.agent_registry = reg
    alive, _ = reg.spawn(name="alive")
    dead, _ = reg.spawn(name="gone")
    assert _wait(
        lambda: reg.get(alive).state == "idle" and reg.get(dead).state == "idle"
    )
    reg.kill(dead)
    yield SimpleNamespace(reg=reg, dead=dead)
    reg.shutdown_all()


def _revive_button(stack, page, key):
    stack.emit_ready()
    page.goto(stack.url)
    assert stack.wait_connected()
    page.evaluate(NOTICES_JS)
    btn = page.locator(f'.ov-ch[data-key="{key}"] .ov-ch-ctl[data-act="resume"]')
    assert _wait(lambda: btn.count() == 1)
    return btn


def test_limit_refusal_is_said_and_the_button_works_again(
    stack, page, two_agents_one_dead
):
    reg, dead = two_agents_one_dead.reg, two_agents_one_dead.dead
    reg.set_max_agents(1)  # 산 것이 하나 — 되살리면 넘는다(실제 409)
    btn = _revive_button(stack, page, dead)
    with page.expect_response(lambda r: r.url.endswith("/resume")) as info:
        btn.click()
    assert info.value.status == 409
    want = (
        'Not revived — agent limit reached (1 alive). Kill one first (`mode` "kill").'
    )
    assert _wait(lambda: want in _notices(page))
    assert _wait(lambda: not btn.is_disabled())
    # 한도를 풀면 같은 ↻ 가 되살린다
    reg.set_max_agents(4)
    btn.click()
    assert _wait(lambda: reg.get(dead).state != "dead")


def test_unreachable_revive_is_said(stack, page, two_agents_one_dead):
    btn = _revive_button(stack, page, two_agents_one_dead.dead)
    page.route("**/api/agent/*/resume", lambda route: route.abort())
    btn.click()
    assert _wait(lambda: "Not revived — could not reach the server" in _notices(page))
    assert _wait(lambda: not btn.is_disabled())
