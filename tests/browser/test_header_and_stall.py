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

    def test_token_usage_shows_in_header(self, stack, page):
        # v8.57.0: 토큰 상세는 헤더에 상시 노출 (구 ctx 게이지 칩·팝오버 폐기).
        page.goto(stack.url)
        stack.renderer.token_usage(
            {"in": 5200, "out": 320, "total_out": 1800, "context_window": 262144},
            turn=1,
        )
        page.wait_for_selector("#token-usage:not([hidden])", timeout=8000)
        # ↑(턴 입력)은 ctx 분자와 동일 값이라 생략, ↓·Σ↓ 는 표시
        txt = page.inner_text("#token-usage")
        assert "ctx" in txt and "↓" in txt and "↑" not in txt
        box = page.locator("#token-usage").bounding_box()
        assert box and box["height"] >= 10 and box["width"] > 40  # 클리핑 없음

    def test_knob_chip_popup_toggles(self, stack, page):
        # 노브 칩 클릭 → 전용 팝업 열림, Escape 로 닫힘 (한 번에 하나).
        # 넓은 뷰포트 — 노브가 보따리로 수납되지 않은 상태를 보장.
        page.set_viewport_size({"width": 1500, "height": 720})
        page.goto(stack.url)
        page.wait_for_selector("#stall-wrap:not([hidden])", timeout=8000)
        page.wait_for_timeout(300)
        assert page.locator("#stall-pop").is_hidden()
        page.click("#stall-chip")
        assert page.locator("#stall-pop").is_visible()
        assert page.locator("#stall-pop #stall-input").is_visible()
        page.keyboard.press("Escape")
        assert _wait(lambda: page.locator("#stall-pop").is_hidden(), timeout=3)

    def test_pouch_stows_and_restores_on_resize(self, stack, page):
        # 좁히면 오버플로 아이템이 보따리로 수납(🎒 노출), 넓히면 복원.
        # ResizeObserver 가 자동 reflow 하므로 뷰포트 변경 후 대기만 하면 된다.
        page.set_viewport_size({"width": 1500, "height": 720})
        page.goto(stack.url)
        page.wait_for_selector("#stall-wrap:not([hidden])", timeout=8000)
        assert _wait(lambda: page.locator("#pouch-wrap").is_hidden(), timeout=3)
        page.set_viewport_size({"width": 440, "height": 720})
        assert _wait(lambda: not page.locator("#pouch-wrap").is_hidden(), timeout=4)
        # 🎒 클릭 → 수납 패널에 아이템 존재
        page.click("#pouch-btn")
        assert _wait(
            lambda: page.locator("#pouch-panel [data-overflow]").count() >= 1, timeout=2
        )
        # 다시 넓히면 보따리가 비고 사라진다
        page.set_viewport_size({"width": 1500, "height": 720})
        assert _wait(lambda: page.locator("#pouch-wrap").is_hidden(), timeout=4)


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
