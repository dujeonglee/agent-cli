"""🤝 채널의 재시작 줄 (v10.165.0).

재시작이 되살린 상주의 채널에 흐린 한 줄이 선다(`agent_message` 의 ``direction: "restarted"`` — `conversation.jsonl`
에 남아 resume 재생이 다시 보낸다). resume 은 옛 런 블록을 먼저 재생하고 대화 줄을 뒤에 보낸다 — 그래서 줄은 자기
시각보다 늦게 시작한 첫 런 블록 앞에 선다(없으면 끝). ✕ 로 채널을 비우면 줄도 지워진다(되살리면 재생이 다시 그린다).
"""

from __future__ import annotations

from tests.browser.test_channel_jump import _chip, _roster, _wait

AGT = "agt-rev"
TEXT = "↻ Session restarted — work in progress was cut off"

ROOT_ORDER = """(ch) => Array.from(document.querySelectorAll("#messages > .card"))
  .filter((c) => c.dataset.ch === ch)
  .map((c) => c.classList.contains("card-restarted") ? "restart" : c.dataset.taskId)"""


def _run(stack, item, ts):
    """끝난 런 블록 하나 — 시작 시각 ``ts``."""
    task_id = f"{AGT}#{item}"
    stack.renderer.begin_scope(
        task_id=task_id,
        kind="run",
        label=f"task {item}",
        agent=AGT,
        parent="",
        ctx_dir=f"agents/{AGT}",
        ts=ts,
    )
    stack.renderer.end_scope(task_id=task_id, kind="run", success=True)


def _restarted(stack, ts):
    stack.renderer.agent_message(
        key=AGT, direction="restarted", author="", text="", to=AGT, ts=ts
    )


def test_restart_rows_stand_between_the_runs_they_separate(stack, page):
    stack.emit_ready()
    _roster(stack, AGT)
    # resume 의 순서: 옛 런 블록 셋이 먼저, 그 뒤 대화 줄(재시작 둘)
    for item, ts in (("r-1", 100.0), ("r-2", 300.0), ("r-3", 500.0)):
        _run(stack, item, ts)
    _restarted(stack, 200.0)
    _restarted(stack, 400.0)
    page.goto(stack.url)
    assert _wait(lambda: _chip(page, AGT).count() > 0)
    _chip(page, AGT).click()
    rows = page.locator("#messages > .card-restarted")
    assert _wait(lambda: rows.count() == 2)
    assert page.evaluate(ROOT_ORDER, AGT) == [
        f"{AGT}#r-1",
        "restart",
        f"{AGT}#r-2",
        "restart",
        f"{AGT}#r-3",
    ]
    assert rows.first.is_visible() and rows.first.inner_text().startswith(TEXT)
    # main 채널에는 보이지 않는다 — 그 에이전트의 줄이다
    _chip(page, "main").click()
    assert not rows.first.is_visible()
    _chip(page, AGT).click()

    # 라이브: 지금 재시작 — 뒤에 시작한 블록이 없으니 끝에 선다. 그다음 런은 그 뒤에
    _restarted(stack, 600.0)
    assert _wait(lambda: rows.count() == 3)
    _run(stack, "r-4", 700.0)
    assert _wait(
        lambda: page.evaluate(ROOT_ORDER, AGT)[-2:] == ["restart", f"{AGT}#r-4"]
    )

    # ✕ 가 채널의 대화 줄을 비운다 — 재시작 줄도. 런 블록은 남는다
    stack.renderer.clear_agent_conversation(AGT)
    assert _wait(lambda: rows.count() == 0)
    assert page.locator(f'#messages > .card-run[data-ch="{AGT}"]').count() == 4
