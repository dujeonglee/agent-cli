"""런 도중 흡수된 항목은 런 블록 머리에 **한 줄** — 서버가 기록 순서대로 내기 때문이다 (N12).

웹 `ovOnAgentMsg` 는 같은 항목의 ``absorbed`` 가 ``in`` 보다 먼저 오면 머리에 두 줄을 붙인다(프런트는 그대로 둔다).
N12 전엔 서버가 ``in`` 을 배달 구간 **뒤**에 내 그 순서가 실제로 났다 — 넣자마자 턴 경계가 꺼내면. 이제 웹 렌더러는
배달과 같은 ``_cv`` 구간에서 내므로 ``in`` 이 언제나 먼저다. 진짜 레지스트리·진짜 웹 렌더러·헤드리스 크롬으로, 흡수를
배달 구간을 나온 **직후**(``in`` 을 내기 전이던 자리)에 끼워 본다.
"""

from __future__ import annotations

import threading

import agent_cli.render as render_mod
from agent_cli.post.office import PostOffice
from agent_cli.subagent.agents_live import AgentRegistry
from tests.browser.test_channel_jump import _chip, _wait
from tests.test_agents_live import _FakeLoopResult
from tests.test_mail_notice_at_delivery import HookedCondition


def _head_rows(page, key, run_item, item):
    return page.locator(
        f'.card-run[data-task-id="{key}#{run_item}"] .run-head .run-item[data-item="{item}"]'
    )


def test_an_item_absorbed_right_after_delivery_has_one_head_row(
    stack, page, monkeypatch, tmp_path
):
    monkeypatch.setattr(render_mod, "get_renderer", lambda: stack.renderer)
    cv = HookedCondition()
    door = threading.Event()

    def runner(kind, start, ctx):
        door.wait(10)
        return _FakeLoopResult(output="done"), 0.01

    reg = AgentRegistry(
        tmp_path,
        runtime={"model": "m", "timeout": 5},
        runner=runner,
        post=PostOffice(cv=cv),
    )
    try:
        stack.emit_ready()
        key, _ = reg.spawn(name="helper")
        assert _wait(lambda: reg.get(key).state == "idle")
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, key).count() > 0)
        _chip(page, key).click()

        assert reg.request(key, "long job", author="main", item_id="r-901") == ""
        assert _wait(lambda: reg.get(key).state == "busy")
        assert _wait(lambda: _head_rows(page, key, "r-901", "r-901").count() == 1)

        me, fired = threading.get_ident(), []

        def absorb_right_after_delivery():
            if fired or threading.get_ident() != me:
                return
            if not reg.queued_for(f"agent:{key}"):
                return
            fired.append(True)
            reg.absorb_pending(reg.get(key))  # 런의 턴 경계가 곧바로 꺼낸다

        cv.after_exit = absorb_right_after_delivery
        assert (
            reg.request(key, "fyi", author="main", expects_reply=False, item_id="n-902")
            == ""
        )
        cv.after_exit = None
        assert fired
        # 표지 하나를 더 넣고 그것이 그려질 때까지 — SSE 는 순서대로 오므로 그 앞의 이벤트는 모두 그려졌다
        assert reg.request(key, "marker", author="main", item_id="n-903") == ""
        marker = page.locator('#messages > .card-queued[data-item="n-903"]')
        assert _wait(lambda: marker.count() == 1)
        assert _head_rows(page, key, "r-901", "n-902").count() == 1
        assert page.locator('#messages > .card-queued[data-item="n-902"]').count() == 0
    finally:
        cv.after_exit = None
        door.set()
        reg.shutdown_all()
