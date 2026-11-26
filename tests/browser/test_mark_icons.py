"""관찰 줄의 ✓/✗ 를 실브라우저로 (IMPL-mark-icons) — `[interrupt]`(정지·재시작)는 도구 호출의 실패가 아니다.

라이브(SSE)·resume 다시 그리기·복사 마크다운이 같은 함수(`renderObservation`)를 지난다.
"""

from __future__ import annotations

import time

from agent_cli.constants import INTERRUPT_NOTICE


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _open(stack, page):
    page.goto(stack.url)
    assert stack.wait_connected()


def test_a_lone_interrupt_line_is_a_failure_mark(stack, page):
    """지금 값 — 단독 `[interrupt]` 관찰은 빨간 ✗(`.row.bad`)·실패 카드다."""
    stack.emit_ready()
    _open(stack, page)
    stack.renderer.observation(
        INTERRUPT_NOTICE, turn=1, tool_name="interrupt", success=False
    )
    row = page.locator(".card-observation .row").first
    assert _wait(lambda: row.count() > 0)
    assert row.locator(".ic").inner_text().strip() == "✗"
    assert "bad" in row.get_attribute("class")
    assert "fail" in page.locator(".card-observation").first.get_attribute("class")


def test_an_interrupt_on_a_step_marks_the_step_failed(stack, page):
    """지금 값 — 호출 줄 뒤에 온 `[interrupt]` 는 그 스텝을 실패로 칠한다(빨간 배지·원인 줄)."""
    stack.emit_ready()
    _open(stack, page)
    r = stack.renderer
    r.thought("run it", turn=1)
    r.action("shell", '{"command": "sleep 30"}', turn=1)
    r.observation(INTERRUPT_NOTICE, turn=1, tool_name="interrupt", success=False)
    step = page.locator(".card-assistant.step").first
    assert _wait(lambda: step.locator(".badge.bad").count() == 1)
    assert "failed" in step.get_attribute("class")
    assert step.locator(".step-why").count() == 1
