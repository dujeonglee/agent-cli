"""main 앞 메일은 입력창 위 대기열에 선다 (v10.51.0) — 실브라우저.

사용자 요청이 main 이 바쁠 때 서는 그 자리, 같은 ⏳ → ✓ 주입됨. 종전엔 한 번의
도착이 세 군데(도착 힌트 · 🤝 깨우기 카드 · 관찰 줄)에 나왔다. 정적 검사는 JS 에
이름이 있는지까지만 보므로, 유예(놀 때 깜빡임 없음)·취소 불가·빠짐 = 주입은
여기서만 잡힌다.
"""

from __future__ import annotations

import time


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _wire(stack):
    """실 부트(main.py web)와 같은 배선."""
    reg = stack.server.agent_registry
    stack.server.mail_source = reg.pending_mail
    reg.on_pending_change = stack.server.broadcast_queue
    return reg


JOB = {
    "kind": "job",
    "success": True,
    "output": "🧵 job #1 finished rc=0 after 3m00s\nlog tail",
}


def _items(page):
    return page.locator("#queue-list .queue-item")


class TestMailInTheQueue:
    def test_busy_mail_waits_then_shows_injected(self, stack, page):
        reg = _wire(stack)
        stack.emit_ready()
        page.goto(stack.url)
        reg._push_reply(dict(JOB))
        assert _wait(lambda: _items(page).count() == 1)
        row = _items(page).first
        assert (
            row.locator(".queue-text").inner_text()
            == "⏳ [job] 🧵 job #1 finished rc=0 after 3m00s"
        )
        assert row.locator(".queue-cancel").count() == 0  # 메일은 아무도 취소 못 한다

        reg.drain_replies()  # 턴 경계 흡수
        assert _wait(
            lambda: (
                "Injected · applies to the next answer"
                in page.locator("#queue-list").inner_text()
            )
        )
        assert "q-injected" in (_items(page).first.get_attribute("class") or "")
        assert _wait(lambda: page.locator("#queue-list").is_hidden(), timeout=5)

    def test_idle_mail_never_flashes(self, stack, page):
        """main 이 놀면 1초 안에 흡수된다 — 줄도 영수증도 나오지 않는다(종전엔
        깨우기가 대기열에 떴다 사라졌다, 사용자 지적)."""
        reg = _wire(stack)
        stack.emit_ready()
        page.goto(stack.url)
        page.evaluate(
            """() => { window.__qSeen = 0;
              new MutationObserver(() => {
                window.__qSeen += document.querySelectorAll('#queue-list .queue-item').length;
              }).observe(document.getElementById('queue-list'), {childList: true, subtree: true}); }"""
        )
        reg._push_reply(dict(JOB))
        time.sleep(0.3)
        reg.drain_replies()
        time.sleep(2.0)  # 유예가 지난 뒤에도
        assert page.evaluate("() => window.__qSeen") == 0
        assert page.locator("#queue-list").is_hidden()

    def test_mail_queues_behind_user_requests_and_survives_reconnect(self, stack, page):
        reg = _wire(stack)
        stack.emit_ready()
        stack.server.enqueue(None, "user ask")
        reg._push_reply(dict(JOB))
        page.goto(stack.url)  # 대기 중에 연결 — sticky 대기열로 다시 선다
        assert _wait(lambda: _items(page).count() == 2)
        texts = [
            t.strip() for t in _items(page).locator(".queue-text").all_inner_texts()
        ]
        assert texts[0].endswith("user ask") and texts[1].startswith("⏳ [job]")

    def test_questions_stay_in_the_tray_not_the_queue(self, stack, page):
        reg = _wire(stack)
        stack.emit_ready()
        page.goto(stack.url)
        reg._push_reply(
            {"kind": "question", "key": "agt-x", "id": "q1", "output": "A or B?"}
        )
        reg._push_reply(dict(JOB))
        assert _wait(lambda: _items(page).count() == 1)
        assert "[job]" in _items(page).first.inner_text()

    def test_no_hint_line_or_wake_card_in_the_timeline(self, stack, page):
        reg = _wire(stack)
        stack.emit_ready()
        page.goto(stack.url)
        reg._push_reply(dict(JOB))
        assert _wait(lambda: _items(page).count() == 1)
        msgs = page.locator("#messages").inner_text()
        assert "Background job finished" not in msgs and "🤝" not in msgs
