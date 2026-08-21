"""헤더 칩·팝오버 렌더링 + confirm-stall 경고 — CSS/타이밍 부류 회귀 가드.

칩 사가(v7.1.0)의 실버그 두 개 — 칩 overflow 세로 클리핑, flex 컨테이너
ellipsis 무동작 — 는 레이아웃 엔진 없이는 원리적으로 못 잡는 부류라
실브라우저 층이 유일한 가드다.
"""

from __future__ import annotations

import time


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


class TestHeaderChips:
    def test_ws_chip_visible_and_copies(self, browser, stack):
        ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        stack.emit_ready()
        page.goto(stack.url)
        page.wait_for_selector("#chip-ws:not([hidden])", timeout=8000)
        # 칩 내용(📋 아이콘 + 경로 꼬리)이 실제로 보이는 크기로 렌더 —
        # v7.1.0 실버그: 내부 버튼이 칩 overflow 에 세로 클리핑돼 안 보임.
        box = page.locator("#chip-ws").bounding_box()
        assert box and box["height"] >= 14 and box["width"] > 40
        ic = page.locator("#ws-copy-ic").bounding_box()
        assert ic and ic["height"] >= 10  # 아이콘이 클리핑되지 않음
        page.click("#chip-ws")
        # 복사 성공 피드백 (📋 → ✓ 1초 플래시)
        assert _wait(lambda: page.inner_text("#ws-copy-ic").strip() == "✓", timeout=3)
        ctx.close()

    def test_ctx_popover_toggles_with_moved_controls(self, stack, page):
        page.goto(stack.url)
        # token_usage 가 와야 ctx 칩이 뜬다 — 렌더러로 직접 발화
        stack.renderer.token_usage(
            {"in": 5200, "out": 320, "total_out": 1800, "context_window": 262144},
            turn=1,
        )
        page.wait_for_selector("#chip-ctx:not([hidden])", timeout=8000)
        assert page.locator("#ctx-popover").is_hidden()
        page.click("#chip-ctx")
        assert page.locator("#ctx-popover").is_visible()
        # 이동 수납된 컨트롤들이 팝오버 안에서 렌더
        assert page.locator("#ctx-popover #token-usage").is_visible()
        page.keyboard.press("Escape")
        assert _wait(lambda: page.locator("#ctx-popover").is_hidden(), timeout=3)


class TestConfirmStallWarning:
    def test_warning_appears_when_starved_and_clears_on_recovery(self, browser, stack):
        """origin 당 6연결 고갈 실재현(수용된 잔여 케이스) — 클릭이 갇히면
        3초 뒤 경고, 연결이 풀리면 해결+경고 정리 (v7.2.0 ⓔ)."""
        results: list = []
        stack.start_confirm_loop(results)
        ctx = browser.new_context()
        victim = ctx.new_page()
        victim.goto(stack.url)
        victim.wait_for_selector(".ask-main .confirm-btn", timeout=8000)
        holders = []
        for _ in range(5):
            t = ctx.new_page()
            try:
                t.goto(stack.url, timeout=4000, wait_until="commit")
            except Exception:
                pass  # 풀 경계에서 로드가 밀릴 수 있음 — 보유만 하면 됨
            holders.append(t)
        victim.wait_for_timeout(800)
        victim.click(".ask-main .confirm-btn >> nth=0")
        victim.wait_for_selector(".ask-main .confirm-stall", timeout=8000)
        holders[0].close()  # 슬롯 해방 → 갇힌 POST flush
        assert _wait(lambda: bool(results), timeout=8)
        assert _wait(
            lambda: victim.locator(".ask-main .confirm-stall").count() == 0, timeout=8
        )
        ctx.close()
