"""대화창 따라가기 — 세 규칙 (v9.26.0, 사용자가 시안으로 확인).

1. 처음 열면 맨 아래.
2. 바닥에서 48px 안으로 오면 그 자리에서 바닥에 붙고, 이후 새 내용을 따라간다.
3. 그보다 멀어지면 보던 줄이 움직이지 않고 "↓ 새 메시지 n" 이 쌓인다.

종전 코드는 근처에서 플래그만 바꿔 다음 이벤트가 와야 따라갔고, 버튼이
없었다. jsdom 으로는 스크롤 기하가 없어 실브라우저에서만 검증된다.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.browser

DIST = (
    "() => { const m = document.getElementById('messages');"
    " return m.scrollHeight - m.scrollTop - m.clientHeight; }"
)


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _fill(stack, n, start=0):
    for i in range(start, start + n):
        stack.renderer.final(f"메인 응답 {i}\n" + ("본문 " * 40), turn=i)


def _dist(page):
    return page.evaluate(DIST)


def _cards(page):
    return page.locator("#messages > *").count()


def _scroll_to(page, top):
    page.evaluate(f"document.getElementById('messages').scrollTop = {top}")


class TestFollowScroll:
    def test_opens_at_the_bottom(self, stack, page):
        """규칙 1 — 스냅샷을 재생한 뒤 바닥에 있다. v10.8.1: 재생 중엔 타임라인이
        숨겨져 있고(`body.loading`), 공개되는 순간 이미 바닥이다 — 내려가는
        스크롤이 보이지 않는다."""
        stack.emit_ready()
        _fill(stack, 40)
        page.goto(stack.url)
        page.wait_for_function(
            "!document.body.classList.contains('loading')", timeout=8000
        )
        # 공개 직후 — 기다리지 않고 바로 — 바닥이고 카드는 다 있다
        assert _dist(page) < 2, f"공개 순간 바닥이 아니다: {_dist(page)}"
        assert _cards(page) >= 40
        assert (
            page.evaluate(
                "getComputedStyle(document.getElementById('messages')).visibility"
            )
            == "visible"
        )
        assert page.locator("#jump-new").is_hidden()

    def test_scrolled_up_holds_and_counts(self, stack, page):
        """규칙 3 — 위로 올린 상태에서 새 카드가 와도 위치는 그대로, 버튼에 개수."""
        stack.emit_ready()
        _fill(stack, 40)
        page.goto(stack.url)
        assert _wait(lambda: _cards(page) >= 40 and _dist(page) < 2)

        _scroll_to(page, 300)
        # 규칙 3 판정은 scroll 이벤트(다음 렌더 단계)에서 — 그때까지 기다린다
        assert _wait(
            lambda: page.locator("#messages").get_attribute("data-pinned") == "0"
        )
        before = page.evaluate("document.getElementById('messages').scrollTop")

        _fill(stack, 3, start=40)
        assert _wait(lambda: _cards(page) >= 43)
        assert _wait(
            lambda: (
                page.locator("#jump-new").is_visible()
                and page.locator("#jump-new").inner_text().endswith("3")
            )
        ), (
            page.locator("#jump-new").inner_text(),
            page.locator("#messages").get_attribute("data-pinned"),
            _dist(page),
        )
        after = page.evaluate("document.getElementById('messages').scrollTop")
        assert after == before, f"위치 고정인데 움직였다: {before} → {after}"

        # 버튼 → 맨 아래, 버튼 사라짐, 다시 따라간다
        page.locator("#jump-new").click()
        assert _wait(lambda: _dist(page) < 2)
        assert _wait(lambda: page.locator("#jump-new").is_hidden())
        _fill(stack, 1, start=43)
        assert _wait(lambda: _cards(page) >= 44)
        assert _wait(lambda: _dist(page) < 2), "버튼으로 붙인 뒤 새 카드를 안 따라간다"

    def test_near_bottom_snaps_and_follows(self, stack, page):
        """규칙 2 — 48px 안으로 오면 그 자리에서 바닥에 붙고, 이후를 따라간다."""
        stack.emit_ready()
        _fill(stack, 40)
        page.goto(stack.url)
        assert _wait(lambda: _cards(page) >= 40 and _dist(page) < 2)

        _scroll_to(page, 0)
        assert _wait(lambda: _dist(page) > 48)
        assert page.evaluate(
            "document.getElementById('messages').scrollHeight"
        ) > page.evaluate("document.getElementById('messages').clientHeight")

        # 바닥에서 20px 위 — "근처". 이벤트 없이도 80ms 뒤 바닥에 붙는다.
        page.evaluate(
            "() => { const m = document.getElementById('messages');"
            " m.scrollTop = m.scrollHeight - m.clientHeight - 20; }"
        )
        assert _wait(lambda: _dist(page) < 2), f"근처인데 안 붙는다: {_dist(page)}"
        assert page.locator("#jump-new").is_hidden()

        _fill(stack, 2, start=40)
        assert _wait(lambda: _cards(page) >= 42)
        assert _wait(lambda: _dist(page) < 2), "붙은 뒤 새 카드를 안 따라간다"

    def test_far_scroll_does_not_snap(self, stack, page):
        """규칙 3 의 경계 — 48px 보다 멀면 붙이지 않는다."""
        stack.emit_ready()
        _fill(stack, 40)
        page.goto(stack.url)
        assert _wait(lambda: _cards(page) >= 40 and _dist(page) < 2)
        page.evaluate(
            "() => { const m = document.getElementById('messages');"
            " m.scrollTop = m.scrollHeight - m.clientHeight - 120; }"
        )
        time.sleep(0.4)
        d = _dist(page)
        assert 100 < d < 140, f"멀리 있는데 움직였다: {d}"
