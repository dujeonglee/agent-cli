"""main 이 바쁠 때 보낸 사람의 입력 — 입력창 위 대기열의 줄과 떠날 때의 영수증 (실브라우저).

영수증은 서버가 보내지 않는다. 프런트가 추론한다: 대기열에서 빠진 줄과 같은 글의 말풍선(`user_message`)이 곧
오면 주입, 아니면 취소. 그래서 줄을 꺼내는 자리마다 말풍선을 내야 "주입" 으로 보인다. 아래는 v10.146.6 의 동작
그대로다 — 제자리 대기 중 명령으로 꺼낸 줄은 말풍선이 없어 "Cancelled" 로 보인다.
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


def _items(page):
    return page.locator("#queue-list .queue-item")


def _queue_text(page) -> str:
    return page.locator("#queue-list").inner_text()


def _send(page, text: str) -> None:
    page.fill("#input", text)
    page.click("#send")


def _take_next(stack) -> dict:
    """턴 경계가 하듯 맨 앞 줄을 꺼낸다(말풍선은 호출자가 낸다)."""
    return stack.server.queue.dequeue_nowait()


def _take_first_command(stack) -> dict:
    """제자리 대기가 하듯 맨 앞 줄을 명령으로 꺼낸다 — 말풍선 없이."""
    return stack.server.queue.take_first_human_if(lambda text: True)


def _busy_page(stack, page):
    stack.emit_ready()
    page.goto(stack.url)
    assert stack.wait_connected()
    stack.renderer.worker_busy()


class TestReceipts:
    def test_my_line_injected_at_a_turn_boundary_reads_applied(self, stack, page):
        _busy_page(stack, page)
        _send(page, "please also check y")
        assert _wait(lambda: _items(page).count() == 1)
        row = _items(page).first
        assert row.locator(".queue-text").inner_text().endswith("please also check y")
        assert row.locator(".queue-cancel").count() == 1  # 내 줄 — 취소할 수 있다
        item = _take_next(stack)
        stack.renderer.push_user_message(
            f"[{item['nickname']}]: {item['text']}", author=item["nickname"]
        )
        assert _wait(lambda: "✓ My request applied" in _queue_text(page))
        assert _wait(lambda: page.locator("#queue-list").is_hidden(), timeout=5)

    def test_my_cancel_reads_cancelled_and_the_line_is_gone(self, stack, page):
        _busy_page(stack, page)
        _send(page, "never mind this")
        assert _wait(lambda: _items(page).count() == 1)
        _items(page).first.locator(".queue-cancel").click()
        assert _wait(lambda: "Cancelled" in _queue_text(page))
        assert stack.server.queue.snapshot() == []

    def test_another_viewers_line_has_no_cancel(self, stack, page):
        _busy_page(stack, page)
        stack.server.enqueue("someone-else", "their request")
        assert _wait(lambda: _items(page).count() == 1)
        assert _items(page).first.locator(".queue-cancel").count() == 0

    def test_a_command_taken_while_main_waits_reads_cancelled(self, stack, page):
        """제자리 대기의 명령은 말풍선 없이 꺼내 라우팅한다 — 프런트는 그 줄을 취소로 그린다."""
        _busy_page(stack, page)
        _send(page, "/noop now")
        assert _wait(lambda: _items(page).count() == 1)
        assert _take_first_command(stack)["text"] == "/noop now"
        assert _wait(lambda: "Cancelled" in _queue_text(page))
        assert "My request applied" not in _queue_text(page)
