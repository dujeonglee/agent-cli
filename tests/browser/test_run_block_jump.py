"""런 블록 머리 줄 → 몸통의 그 카드 (v10.165.0).

런 도중 흡수된 배달은 블록 머리의 한 줄과 몸통의 관찰 카드 둘로 선다. 머리 줄 끝의 ↓ 가 같은 블록 몸통에서 그 카드를
찾아간다 — 열쇠는 그 배달의 id(`data-item`)다. 카드가 없는 줄(런을 연 첫 항목)에는 ↓ 가 없다. 진짜 레지스트리·진짜 웹
렌더러·헤드리스 크롬으로: 가짜 런이 턴 경계처럼 `absorb_pending` 을 부르고 엔진(`AgentLoop._inject_item`)과 같은 인자로
관찰을 그린다.
"""

from __future__ import annotations

import threading

import agent_cli.render as render_mod
from agent_cli.render import render_step
from agent_cli.subagent.agents_live import AgentRegistry
from tests.browser.test_channel_jump import _chip, _roster, _wait
from tests.test_agents_live import _FakeLoopResult

IN_VIEW = """(el) => {
  const box = document.getElementById("messages").getBoundingClientRect();
  const r = el.getBoundingClientRect();
  return r.top >= box.top && r.bottom <= box.bottom;
}"""


def _block(key, run_item):
    return f'#messages > .card-run[data-task-id="{key}#{run_item}"]'


def _head(page, key, run_item, item):
    return page.locator(
        f'{_block(key, run_item)} .run-head .run-item[data-item="{item}"]'
    )


def _card(page, key, run_item, item):
    return page.locator(f'{_block(key, run_item)} .run-body [data-item="{item}"]')


def test_the_arrow_on_a_head_row_goes_to_its_card_in_the_same_block(
    stack, page, monkeypatch, tmp_path
):
    monkeypatch.setattr(render_mod, "_renderer", stack.renderer)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: stack.renderer)
    absorb, absorbed, door = threading.Event(), threading.Event(), threading.Event()
    box: dict = {}

    def runner(kind, start, ctx):
        absorb.wait(10)
        # 흡수 카드를 블록 한가운데에 둔다 — 머리 줄이 보일 때도, 바닥을 따라갈 때도 화면 밖이다
        for n in range(60):
            render_step("observation", f"before {n}", 1, tool_name="shell")
        for it in box["reg"].absorb_pending(box["reg"].get(box["key"])):
            rec = it["record"]
            ctx.add(dict(rec))
            render_step(
                "observation",
                rec["content"],
                1,
                tool_name=rec["tool"],
                success=True,
                item=rec["item"],
            )
        for n in range(60):
            render_step("observation", f"after {n}", 2, tool_name="shell")
        absorbed.set()
        door.wait(10)
        return _FakeLoopResult(output="done"), 0.01

    reg = box["reg"] = AgentRegistry(
        tmp_path, runtime={"model": "m", "timeout": 5}, runner=runner
    )
    try:
        stack.emit_ready()
        key, _ = reg.spawn(name="helper")
        box["key"] = key
        assert _wait(lambda: reg.get(key).state == "idle")
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, key).count() > 0)
        _chip(page, key).click()

        assert reg.request(key, "TASK-A long job", author="main", item_id="r-901") == ""
        assert _wait(lambda: reg.get(key).state == "busy")
        assert reg.request(key, "TASK-B second", author="main", item_id="r-902") == ""
        assert (
            reg.request(key, "fyi", author="main", expects_reply=False, item_id="n-903")
            == ""
        )
        absorb.set()
        assert absorbed.wait(10)

        def check(page):
            assert _wait(lambda: _card(page, key, "r-901", "n-903").count() == 1)
            assert _card(page, key, "r-901", "r-902").count() == 1
            # 첫 항목은 카드가 없다 — ↓ 도 없다. 흡수된 둘에는 하나씩
            assert (
                _head(page, key, "r-901", "r-901").locator(".run-item-jump").count()
                == 0
            )
            for item in ("r-902", "n-903"):
                assert (
                    _head(page, key, "r-901", item).locator(".run-item-jump").count()
                    == 1
                )
            last = page.locator(f"{_block(key, 'r-901')} .run-body > .card").last
            assert _wait(lambda: last.evaluate(IN_VIEW))  # 바닥을 따라가 있다
            for item, text in (
                ("n-903", "fyi"),
                ("r-902", "request [r-902] from main"),
            ):
                card = _card(page, key, "r-901", item)
                arrow = _head(page, key, "r-901", item).locator(".run-item-jump")
                arrow.scroll_into_view_if_needed()  # 머리 줄이 보이는 자리 — 카드는 60줄 아래다
                assert not card.evaluate(IN_VIEW)
                arrow.click()
                assert _wait(lambda card=card: card.evaluate(IN_VIEW))
                assert text in card.inner_text()

        check(page)
        # 다시 붙어도(버퍼 재생 — 머리 줄과 카드가 스냅샷 순서로 온다) 같다
        page.reload()
        assert _wait(lambda: _chip(page, key).count() > 0)
        _chip(page, key).click()
        check(page)
    finally:
        absorb.set()
        door.set()
        reg.shutdown_all()


def test_a_head_row_that_arrives_after_its_card_still_gets_the_arrow(stack, page):
    """resume 의 순서 — history 재생이 관찰 카드를 먼저 그리고, `conversation.jsonl` 재생이 머리 줄을 뒤에 보낸다."""
    agt = "agt-rev"
    stack.emit_ready()
    _roster(stack, agt)
    r = stack.renderer
    r.begin_agent_work(key=agt, item="r-1", profile="", message="TASK-A", items=["r-1"])
    r.observation("── request [r-2] from main ──\nTASK-B", 1, "agent", item="r-2")
    r.end_agent_work(key=agt, item="r-1", success=True, duration_s=0.1)
    for item, text in (("r-1", "TASK-A"), ("r-2", "TASK-B")):
        r.agent_message(key=agt, direction="in", author="main", text=text, item=item)
    r.agent_message(
        key=agt,
        direction="absorbed",
        author="main",
        text="TASK-B",
        item="r-2",
        to=agt,
        absorbed_into="r-1",
    )
    page.goto(stack.url)
    assert _wait(lambda: _chip(page, agt).count() > 0)
    _chip(page, agt).click()
    arrow = _head(page, agt, "r-1", "r-2").locator(".run-item-jump")
    assert _wait(lambda: arrow.count() == 1)
    assert _head(page, agt, "r-1", "r-1").locator(".run-item-jump").count() == 0
    assert arrow.get_attribute("title") == "Go to this message in the run"
