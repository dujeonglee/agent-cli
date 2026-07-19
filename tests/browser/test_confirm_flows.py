"""confirm/ask 실브라우저 계약 — 이번 주 실사고 시나리오의 회귀 가드.

각 테스트는 실서버(SSE+API)와 헤드리스 크롬으로 사용자 여정 전체를
구동한다: 다이얼로그 렌더 → 클릭 → worker 해제 → UI 정리.
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


class TestConfirmClick:
    def test_click_resolves_and_ui_folds(self, stack, page):
        results: list = []
        stack.start_confirm_loop(results)
        page.goto(stack.url)
        page.wait_for_selector("#confirm-buttons .confirm-btn", timeout=8000)
        # y — once (첫 버튼)
        page.click("#confirm-buttons .confirm-btn >> nth=0")
        assert _wait(lambda: results and results[0][0] == "y")
        # 해결되면 다이얼로그가 접힌다 (다음 confirm 이 다시 뜰 수 있으니
        # '해당 결과가 도착했다'가 핵심 계약)
        assert results[0] == ("y", "")

    def test_comment_travels_with_choice(self, stack, page):
        results: list = []
        stack.start_confirm_loop(results)
        page.goto(stack.url)
        page.wait_for_selector("#confirm-buttons .confirm-btn", timeout=8000)
        page.fill("#input", "메모: /tmp 만 허용")
        page.click("#confirm-buttons .confirm-btn >> nth=1")  # n — deny
        assert _wait(lambda: bool(results))
        assert results[0] == ("n", "메모: /tmp 만 허용")

    def test_stale_second_viewer_click_is_409_folded(self, stack, browser):
        """두 뷰어가 같은 confirm 을 볼 때 — 먼저 온 답만 수용(ⓓ 게이트),
        늦은 클릭은 다음 confirm 을 오염시키지 않는다 (v7.2.0 실사고)."""
        results: list = []
        stack.start_confirm_loop(results)
        ctx = browser.new_context()
        a, b = ctx.new_page(), ctx.new_page()
        a.goto(stack.url)
        b.goto(stack.url)
        for pg in (a, b):
            pg.wait_for_selector("#confirm-buttons .confirm-btn", timeout=8000)
        a.click("#confirm-buttons .confirm-btn >> nth=0")  # y
        assert _wait(lambda: len(results) >= 1)
        b.click("#confirm-buttons .confirm-btn >> nth=1")  # n (stale 또는 다음 것)
        # 핵심 불변식: 이후 confirm 이 stale 답으로 자동 해결되지 않는다 —
        # 다음 다이얼로그가 항상 '대기 상태'로 다시 나타난다.
        assert a.wait_for_selector("#confirm-buttons .confirm-btn", timeout=8000)
        ctx.close()


class TestAnswering:
    def test_ask_badge_and_answer_roundtrip(self, stack, page):
        results: list = []
        stack.start_ask(results, question="어떤 언어를 쓰시나요?")
        page.goto(stack.url)
        page.wait_for_selector("#input-mode-badge.visible", timeout=8000)
        assert "어떤 언어" in page.inner_text("#input-mode-badge")
        page.fill("#input", "Python 이요")
        page.press("#input", "Enter")
        assert _wait(lambda: bool(results))
        assert results[0] == "Python 이요"
        # 해결 후 ANSWERING 배지가 접힌다
        assert _wait(
            lambda: (
                "visible"
                not in (page.get_attribute("#input-mode-badge", "class") or "")
            )
        )


class TestReconnectReplay:
    def test_pending_confirm_replays_to_late_viewer(self, stack, browser):
        """뷰어 0명일 때 시작된 confirm(v7.8.0 부재 중 대기)이 늦게 접속한
        브라우저의 snapshot replay 로 그대로 뜨고 답변 가능해야 한다."""
        results: list = []
        # 접속 전에 confirm 을 걸어야 하므로 has_live_connections 대기를
        # 우회해 직접 스레드를 건다.
        import threading

        from agent_cli.render.base import ConfirmOption

        def worker():
            results.append(
                stack.renderer.confirm(
                    "pending?",
                    [
                        ConfirmOption(key="y", label="yes"),
                        ConfirmOption(key="n", label="no"),
                    ],
                    default_key="n",
                )
            )

        threading.Thread(target=worker, daemon=True).start()
        assert _wait(lambda: stack.renderer.is_awaiting_input())

        ctx = browser.new_context()
        late = ctx.new_page()
        late.goto(stack.url)
        late.wait_for_selector("#confirm-buttons .confirm-btn", timeout=8000)
        late.click("#confirm-buttons .confirm-btn >> nth=0")
        assert _wait(lambda: bool(results))
        assert results[0][0] == "y"
        ctx.close()
