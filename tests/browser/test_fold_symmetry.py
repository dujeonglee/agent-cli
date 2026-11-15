"""접히는 채팅 요소의 대칭 — 실브라우저 (v10.143.0, 사용자 요청).

1. 클릭 대칭(`dom.foldOnClick`): 줄·스텝 카드는 머리든 본문이든 눌러 펴고 접는다. 토글하지 않는 것: 그 요소
   안에 사람이 고른 글이 있을 때, 링크·버튼(복사 ⧉ 포함) 클릭, 더 안쪽의 접히는 요소(스텝 안의 줄)를 누른 것.
2. 서식 동일: 펼칠 본문이 있는 줄의 요약은 그 본문의 글꼴·크기·굵기·색·줄높이를 쓴다 — 관찰 <pre> 면 고정폭
   12px, 마크다운 메일이면 시스템 글꼴. 생각 줄은 흐림이 같고, 기울임은 요약만의 의도된 표지다.
기다림은 DOM 상태로만 본다.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

#: 스크린샷 디렉터리 — 비면(기본) 찍지 않는다.
SHOTS = os.environ.get("AGENT_CLI_SHOTS", "")
STYLE = (
    "(e) => { const c = getComputedStyle(e); return {family: c.fontFamily, "
    "size: c.fontSize, weight: c.fontWeight, style: c.fontStyle, color: c.color, "
    "line: c.lineHeight}; }"
)


@pytest.fixture
def page(browser):
    """2배 화면 — 스크린샷이 글자를 읽을 수 있게(conftest 의 것과 같은 크기)."""
    ctx = browser.new_context(
        viewport={"width": 1100, "height": 750}, device_scale_factor=2
    )
    yield ctx.new_page()
    ctx.close()


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / name))


def _obs_rows(page):
    return page.locator(".card-observation .row")


def _open(row):
    return "open" in (row.get_attribute("class") or "")


def _select_text_in(page, locator):
    """그 요소의 첫 글 마디를 고른다 — 사람이 끌어 고른 것과 같은 Selection."""
    locator.evaluate(
        "(e) => { const w = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);"
        " const t = w.nextNode(); const r = document.createRange();"
        " r.setStart(t, 0); r.setEnd(t, Math.min(5, t.length));"
        " const s = getSelection(); s.removeAllRanges(); s.addRange(r); }"
    )


def _clear_selection(page):
    page.evaluate("() => getSelection().removeAllRanges()")


def _style(page, locator):
    page.mouse.move(1, 1)  # hover 색을 빼고 잰다
    return locator.evaluate(STYLE)


def _rows_room(stack, page):
    stack.emit_ready()
    page.goto(stack.url)
    r = stack.renderer
    r.observation(
        "collected 412 items\n412 passed", turn=1, tool_name="shell", success=True
    )
    r.observation(
        "Delivered to agt-1 — see ![the chart](https://example.com/chart.png)\n**done**",
        turn=1,
        tool_name="agent",
        success=True,
    )
    r.observation("woke on mail\nfrom agt-1", turn=1, tool_name="wait", success=True)
    r.thought("first thought line\n\nsecond paragraph", 1)
    r.final("done", 1)
    assert _wait(lambda: _obs_rows(page).count() == 3)
    return _obs_rows(page)


class TestClickSymmetry:
    def test_head_opens_body_closes_head_reopens(self, stack, page):
        rows = _rows_room(stack, page)
        row = rows.nth(0)
        row.locator(".s").click()
        assert _wait(lambda: row.locator(".row-body").is_visible() and _open(row))
        row.locator(".row-body").click()  # 본문을 눌러도 접힌다
        assert _wait(lambda: not row.locator(".row-body").is_visible())
        assert not _open(row)
        row.locator(".k").click()
        assert _wait(lambda: row.locator(".row-body").is_visible())

    def test_selecting_text_then_clicking_does_not_toggle(self, stack, page):
        row = _rows_room(stack, page).nth(0)
        row.locator(".s").click()
        body = row.locator(".row-body")
        assert _wait(lambda: body.is_visible())
        _select_text_in(page, body)
        body.dispatch_event("click")
        assert body.is_visible() and _open(row)  # 고르던 글 — 접지 않는다
        _clear_selection(page)
        body.click()
        assert _wait(lambda: not body.is_visible())

    def test_selection_elsewhere_does_not_block(self, stack, page):
        rows = _rows_room(stack, page)
        rows.nth(0).locator(".s").click()
        assert _wait(lambda: _open(rows.nth(0)))
        _select_text_in(page, rows.nth(2).locator(".s"))  # 다른 줄의 선택
        rows.nth(0).locator(".row-body").dispatch_event("click")
        assert _wait(lambda: not _open(rows.nth(0)))

    def test_link_click_does_not_toggle(self, stack, page):
        rows = _rows_room(stack, page)
        mail = rows.nth(1)
        mail.locator(".s").click()
        link = mail.locator(".row-body a")
        assert _wait(lambda: link.count() == 1 and link.is_visible())
        # 이동은 막고(테스트 페이지를 지키려고) 클릭은 그대로 위로 올린다
        page.evaluate(
            "() => document.addEventListener('click', (e) => {"
            " if (e.target.closest('a')) e.preventDefault(); }, true)"
        )
        link.click()
        assert _open(mail) and mail.locator(".row-body").is_visible()

    def test_step_card_body_click_folds_but_inner_row_and_copy_do_not(
        self, stack, page
    ):
        stack.emit_ready()
        page.goto(stack.url)
        r = stack.renderer
        r.thought("look at the parser first\nthen the lexer tests", 1)
        r.action("shell", json.dumps({"command": "pytest -q tests/lexer"}), 1)
        r.observation("3 passed\nok", turn=1, tool_name="shell", success=True)
        card = page.locator(".card-assistant.step").first
        assert _wait(lambda: card.locator(".step-body .row").count() >= 2)
        head = card.locator(".step-head")
        body = card.locator(".step-body")
        head.click()
        assert _wait(lambda: body.is_visible() and _open(card))
        # 안쪽 줄은 그 줄의 몫 — 카드는 그대로
        inner = body.locator(".row.can").last
        inner.locator(".s").click()
        assert _wait(lambda: _open(inner))
        assert _open(card) and body.is_visible()
        inner.locator(".row-body").click()
        assert _wait(lambda: not _open(inner))
        assert _open(card)
        # 복사 ⧉ 는 버튼 — 카드를 접지 않는다
        copy = card.locator(":scope > .card-copy")
        assert copy.count() == 1
        copy.click(force=True)
        assert _open(card) and body.is_visible()
        # 생각 본문(줄이 아닌 본문)을 누르면 카드가 접힌다
        body.locator(":scope > .md").first.click()
        assert _wait(lambda: not body.is_visible())
        assert not _open(card)
        head.click()
        assert _wait(lambda: body.is_visible())


class TestSameTypeface:
    """접힌 요약(.s)과 펼친 본문(.row-body)의 계산 스타일 — 줄 종류별, 속성별."""

    def _pair(self, page, row, *, open_by=".s"):
        summary = _style(page, row.locator(".s"))
        row.locator(open_by).click()
        assert _wait(lambda: row.locator(".row-body").is_visible())
        return summary, _style(page, row.locator(".row-body"))

    def test_shell_observation_pre(self, stack, page):
        rows = _rows_room(stack, page)
        _shot(page, "fold-1-collapsed.png")
        s, b = self._pair(page, rows.nth(0))
        _shot(page, "fold-2-shell-open.png")
        for prop in ("family", "size", "weight", "style", "color", "line"):
            assert s[prop] == b[prop], (prop, s, b)
        assert "Jetendard" in s["family"] and s["size"] == "12px"

    def test_wait_observation_pre(self, stack, page):
        rows = _rows_room(stack, page)
        s, b = self._pair(page, rows.nth(2))
        for prop in ("family", "size", "weight", "style", "color", "line"):
            assert s[prop] == b[prop], (prop, s, b)

    def test_agent_mail_markdown(self, stack, page):
        rows = _rows_room(stack, page)
        s, b = self._pair(page, rows.nth(1))
        _shot(page, "fold-3-mail-open.png")
        for prop in ("family", "size", "weight", "style", "color", "line"):
            assert s[prop] == b[prop], (prop, s, b)
        assert "Jetendard" not in s["family"] and s["size"] == "13px"

    def test_thought_keeps_its_muted_italic_mark(self, stack, page):
        """생각 줄(최종답 카드 — 펼친 채 온다): 글꼴·크기·굵기·색(흐림)은 같고, 기울임은 접힌 요약만의
        의도된 표지다."""
        _rows_room(stack, page)
        think = page.locator(".card-assistant .row.think").first
        assert _wait(lambda: think.locator(".row-body").is_visible())
        b = _style(page, think.locator(".row-body"))
        think.locator(".row-body").click()  # 본문을 눌러 접는다
        assert _wait(lambda: think.locator(".s").is_visible())
        s = _style(page, think.locator(".s"))
        for prop in ("family", "size", "weight", "color", "line"):
            assert s[prop] == b[prop], (prop, s, b)
        assert s["style"] == "italic" and b["style"] == "normal"
