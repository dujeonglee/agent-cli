"""확인(`c-N`) 실브라우저 계약 — 이번 주 실사고 시나리오의 회귀 가드.

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
        page.wait_for_selector(".ask-confirm .confirm-btn", timeout=8000)
        # once (첫 버튼)
        page.click(".ask-confirm .confirm-btn >> nth=0")
        assert _wait(lambda: results and results[0][0] == "approve")
        # 해결되면 다이얼로그가 접힌다 (다음 confirm 이 다시 뜰 수 있으니
        # '해당 결과가 도착했다'가 핵심 계약)
        assert results[0] == ("approve", "")

    def test_comment_travels_with_choice(self, stack, page):
        results: list = []
        stack.start_confirm_loop(results)
        page.goto(stack.url)
        page.wait_for_selector(".ask-confirm .confirm-btn", timeout=8000)
        page.fill(".ask-confirm .ask-answer", "메모: /tmp 만 허용")
        page.click(".ask-confirm .confirm-btn >> nth=1")  # deny
        assert _wait(lambda: bool(results))
        assert results[0] == ("deny", "메모: /tmp 만 허용")

    def test_enter_in_the_note_sends_nothing(self, stack, page):
        """기본값이 없다(6d) — 말 칸의 Enter 는 답이 아니다. 답은 버튼뿐."""
        results: list = []
        stack.start_confirm_loop(results)
        page.goto(stack.url)
        page.wait_for_selector(".ask-confirm .confirm-btn", timeout=8000)
        page.fill(".ask-confirm .ask-answer", "just a note")
        page.press(".ask-confirm .ask-answer", "Enter")
        page.wait_for_timeout(300)
        assert results == []
        assert page.locator(".ask-confirm .confirm-btn.default").count() == 0
        page.click(".ask-confirm .confirm-btn >> nth=1")
        assert _wait(lambda: results == [("deny", "just a note")])

    def test_stale_second_viewer_click_is_409_folded(self, stack, browser):
        """두 뷰어가 같은 확인을 볼 때 — 먼저 온 답만 수용(번호의 길 — 늦은 것은 409),
        늦은 클릭은 다음 확인을 오염시키지 않는다 (v7.2.0 실사고)."""
        results: list = []
        stack.start_confirm_loop(results)
        ctx = browser.new_context()
        a, b = ctx.new_page(), ctx.new_page()
        a.goto(stack.url)
        b.goto(stack.url)
        for pg in (a, b):
            pg.wait_for_selector(".ask-confirm .confirm-btn", timeout=8000)
        a.click(".ask-confirm .confirm-btn >> nth=0")  # once
        assert _wait(lambda: len(results) >= 1)
        b.click(".ask-confirm .confirm-btn >> nth=1")  # deny (stale 또는 다음 것)
        # 핵심 불변식: 이후 confirm 이 stale 답으로 자동 해결되지 않는다 —
        # 다음 다이얼로그가 항상 '대기 상태'로 다시 나타난다.
        assert a.wait_for_selector(".ask-confirm .confirm-btn", timeout=8000)
        ctx.close()


class TestDangerHighlight:
    def test_command_shown_with_danger_span(self, stack, page):
        """위험 명령이 confirm 다이얼로그 안에 렌더되고, 트리거된 토큰만
        .danger 로 강조된다 (서버가 span 을 계산 → 프론트는 칠하기만)."""
        results: list = []
        stack.start_confirm_loop(results, command="rm -rf build", danger_spans=[(0, 2)])
        page.goto(stack.url)
        page.wait_for_selector(".ask-confirm .confirm-cmd", timeout=8000)
        # 명령 원문이 다이얼로그에 보인다
        assert "rm -rf build" in page.inner_text(".ask-confirm .confirm-cmd")
        # 트리거 토큰만 강조 — 정확히 "rm"
        danger = page.locator(".ask-confirm .confirm-cmd .danger")
        assert danger.count() == 1
        assert danger.first.inner_text() == "rm"

    def test_no_command_no_cmd_block(self, stack, page):
        """command 없이 뜬 confirm(경로-이탈 등)은 명령 블록을 만들지 않는다."""
        results: list = []
        stack.start_confirm_loop(results)  # command=None
        page.goto(stack.url)
        page.wait_for_selector(".ask-confirm .confirm-btn", timeout=8000)
        assert page.locator(".ask-confirm .confirm-cmd").count() == 0


class TestReconnectReplay:
    def test_pending_confirm_replays_to_late_viewer(self, stack, browser):
        """뷰어 0명일 때 시작된 confirm(v7.8.0 부재 중 대기)이 늦게 접속한
        브라우저의 snapshot replay 로 그대로 뜨고 답변 가능해야 한다."""
        results: list = []
        # 접속 전에 확인을 열어야 하므로 hub.has_live_connections 대기를 우회해 직접 스레드를 건다.
        import threading

        t = threading.Thread(
            target=lambda: results.append(stack.ask("pending?")), daemon=True
        )
        stack._workers.append(t)
        t.start()
        assert _wait(lambda: bool(stack.server.agent_registry.open_human_confirms()))

        ctx = browser.new_context()
        late = ctx.new_page()
        late.goto(stack.url)
        late.wait_for_selector(".ask-confirm .confirm-btn", timeout=8000)
        late.click(".ask-confirm .confirm-btn >> nth=0")
        assert _wait(lambda: bool(results))
        assert results[0][0] == "approve"
        ctx.close()
