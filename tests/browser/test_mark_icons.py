"""관찰 줄의 ✓/✗/■ 를 실브라우저로 (IMPL-mark-icons) — `[interrupt]`(정지·재시작)는 도구 호출의 실패가 아니다.

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


def _md(page):
    return page.evaluate(
        "Array.from(document.querySelectorAll('#messages > .card')).map(c => window.__cardMarkdown(c))"
    )


def test_a_lone_interrupt_line_is_neutral(stack, page):
    """단독 `[interrupt]` 관찰은 중립 ■ — 줄에 색 클래스가 없고 카드는 실패도 성공도 아니다(종전 빨간 ✗·실패 카드)."""
    stack.emit_ready()
    _open(stack, page)
    stack.renderer.observation(
        INTERRUPT_NOTICE, turn=1, tool_name="interrupt", success=False
    )
    row = page.locator(".card-observation .row").first
    assert _wait(lambda: row.count() > 0)
    assert row.locator(".ic").inner_text().strip() == "■"
    cls = row.get_attribute("class").split()
    assert "bad" not in cls and "ok" not in cls
    card = page.locator(".card-observation").first.get_attribute("class").split()
    assert "fail" not in card and "ok" not in card
    assert _md(page)[-1].startswith("■ `interrupt`")


def test_an_interrupt_on_a_step_does_not_fail_the_step(stack, page):
    """호출 줄 뒤에 온 `[interrupt]` — 배지는 색 없는 ■, 스텝은 실패로 칠하지 않고 원인 줄도 없다."""
    stack.emit_ready()
    _open(stack, page)
    r = stack.renderer
    r.thought("run it", turn=1)
    r.action("shell", '{"command": "sleep 30"}', turn=1)
    r.observation(INTERRUPT_NOTICE, turn=1, tool_name="interrupt", success=False)
    step = page.locator(".card-assistant.step").first
    badge = step.locator(".badges .badge").last
    assert _wait(lambda: badge.count() == 1 and badge.inner_text().strip() == "■")
    assert badge.get_attribute("class").split() == ["badge"]
    assert "failed" not in step.get_attribute("class").split()
    assert step.locator(".step-why").count() == 0
    assert "■ `interrupt`" in _md(page)[-1]


def test_other_failures_keep_the_red_mark(stack, page):
    stack.emit_ready()
    _open(stack, page)
    stack.renderer.observation("boom", turn=1, tool_name="shell", success=False)
    row = page.locator(".card-observation .row").first
    assert _wait(lambda: row.count() > 0)
    assert row.locator(".ic").inner_text().strip() == "✗"
    assert "bad" in row.get_attribute("class").split()


class _Ctx:
    def __init__(self, msgs):
        self._msgs = msgs

    def get_raw_messages(self):
        return self._msgs


def test_resume_draws_the_same_marks(stack, page):
    """resume 다시 그리기 — 레코드에서 같은 함수로: `[interrupt]`(레코드 False)는 ■, 사람의 답(레코드 True)은 ✓."""
    stack.emit_ready()
    stack.renderer.replay_from_history(
        _Ctx(
            [
                {"role": "user", "content": "go", "ts": "2026-01-15T10:00:00"},
                {
                    "role": "user",
                    "tool": "ask",
                    "success": True,
                    "content": "── answer [n-3] from the user re [q-2] ──\nAnswer: v2",
                    "ts": "2026-01-15T10:00:01",
                },
                {
                    "role": "user",
                    "tool": "interrupt",
                    "success": False,
                    "content": INTERRUPT_NOTICE,
                    "ts": "2026-01-15T10:00:02",
                },
            ]
        )
    )
    _open(stack, page)
    marks = page.locator(".card-observation .row .ic")
    assert _wait(lambda: marks.count() == 2)
    assert [m.strip() for m in marks.all_inner_texts()] == ["✓", "■"]
