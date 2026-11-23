"""대기열 ✕ — 서버의 답을 본 뒤에 영수증 (v10.155.0, 실브라우저).

종전엔 ✕ 를 누르는 즉시 "내가 취소" 로 확정해, 그 사이 워커가 그 말을 꺼내 처리해도(서버는 ``cancelled: false``)
영수증이 "Cancelled" 였다 — 화면이 사실과 반대. 이제 서버가 뺐다고 답한 뒤에만 "Cancelled", 못 뺐으면 입력창 아래에
``Not cancelled — it was already sent`` 가 서고 그 말은 평소 길("✓ My request applied")로 간다. 답과 대기열 이벤트의
순서는 둘 다 있다 — `page.route` 로 답을 붙잡아 순서를 만든다(시간 단언 없음).
"""

from __future__ import annotations

from tests.browser.conftest import wait_held
from tests.browser.test_input_queue_receipts import (
    _busy_page,
    _items,
    _queue_text,
    _wait,
)


def _notice(page) -> str:
    page.wait_for_selector("#attach-strip .attach-msg", timeout=8000)
    return page.inner_text("#attach-strip .attach-msg")


def _my_line(stack, page, text="please do X"):
    conn = page.evaluate("() => window.AGENTCLI_CONN_ID")
    item = stack.server.enqueue(conn, text)
    stack.server.broadcast_queue()
    assert _wait(lambda: _items(page).locator(".queue-cancel").count() == 1)
    return item


def _taken_and_echoed(stack, item):
    """워커가 그 말을 꺼냈다 — 대기열에서 빠지고 그 말의 말풍선이 선다(펌프가 연 차례와 같다)."""
    taken = stack.server.main_input.wait_to_open(0)
    assert taken["id"] == item["id"]
    stack.server.broadcast_queue()
    nick = str(item.get("nickname"))
    stack.renderer.push_user_message(f"[{nick}]: {item['text']}", author=nick)


def test_queue_left_before_the_answer_then_refused_reads_applied(stack, page):
    """경주(실측 순서): ✕ → 워커가 꺼냄(대기열 이벤트·말풍선) → 서버의 답 ``cancelled: false``."""
    _busy_page(stack, page)
    item = _my_line(stack, page)
    held: list = []
    page.route("**/api/queue/cancel", lambda route: held.append(route))
    _items(page).first.locator(".queue-cancel").click()
    assert wait_held(page, held, 1)
    _taken_and_echoed(stack, item)
    assert _wait(
        lambda: "Cancelling…" in _queue_text(page)
    )  # 판정 전 — 취소됐다고 하지 않는다
    with page.expect_response(lambda r: "queue/cancel" in r.url) as info:
        held[0].continue_()
    assert info.value.json() == {"cancelled": False}
    assert _notice(page) == "Not cancelled — it was already sent"
    assert _wait(lambda: "My request applied" in _queue_text(page))
    assert "Cancelled" not in _queue_text(page)


def test_answer_before_the_queue_left_then_refused_reads_applied(stack, page):
    """답이 먼저: 서버는 이미 꺼낸 뒤라 ``cancelled: false`` — 그 뒤 대기열 이벤트와 말풍선."""
    _busy_page(stack, page)
    item = _my_line(stack, page)
    held: list = []
    page.route("**/api/queue/cancel", lambda route: held.append(route))
    _items(page).first.locator(".queue-cancel").click()
    assert wait_held(page, held, 1)
    # 꺼냈다 — 그 대기열 이벤트가 화면에 닿기 전에 답이 먼저 온다(SSE 와 응답은 다른 연결이다). 꺼냄의 알림을
    # 잠시 막아 그 순서를 만든다.
    mi = stack.server.main_input
    notify, mi.on_change = mi.on_change, None
    taken = mi.wait_to_open(0)
    mi.on_change = notify
    assert taken["id"] == item["id"]
    with page.expect_response(lambda r: "queue/cancel" in r.url) as info:
        held[0].continue_()
    assert info.value.json() == {"cancelled": False}
    assert _notice(page) == "Not cancelled — it was already sent"
    assert _wait(lambda: not _items(page).first.locator(".queue-cancel").is_disabled())
    stack.server.broadcast_queue()
    nick = str(item.get("nickname"))
    stack.renderer.push_user_message(f"[{nick}]: {item['text']}", author=nick)
    assert _wait(lambda: "My request applied" in _queue_text(page))
    assert "Cancelled" not in _queue_text(page)


def test_accepted_cancel_reads_cancelled_only_after_the_answer(stack, page):
    _busy_page(stack, page)
    _my_line(stack, page)
    held: list = []
    page.route("**/api/queue/cancel", lambda route: held.append(route))
    _items(page).first.locator(".queue-cancel").click()
    assert wait_held(page, held, 1)
    assert "Cancelled" not in _queue_text(page)  # 답 전 — 줄은 그대로, ✕ 는 잠김
    assert _items(page).first.locator(".queue-cancel").is_disabled()
    held[0].continue_()
    assert _wait(lambda: "Cancelled" in _queue_text(page))
    assert stack.server.main_input.pending_count() == 0
    assert page.locator("#attach-strip .attach-msg").count() == 0


def test_network_failure_keeps_the_line_and_the_cancel_button(stack, page):
    _busy_page(stack, page)
    _my_line(stack, page)
    page.route("**/api/queue/cancel", lambda route: route.abort())
    _items(page).first.locator(".queue-cancel").click()
    assert _notice(page) == "Not cancelled — could not reach the server"
    assert _wait(lambda: not _items(page).first.locator(".queue-cancel").is_disabled())
    assert "Cancelled" not in _queue_text(page)
    assert stack.server.main_input.pending_count() == 1


def test_another_viewers_cancel_still_reads_cancelled(stack, page):
    """다른 뷰어가 자기 말을 취소했다 — 그 줄은 말풍선 없이 떠나 종전처럼 "Cancelled"."""
    _busy_page(stack, page)
    other = stack.server.enqueue("other-conn", "their message")
    stack.server.broadcast_queue()
    assert _wait(lambda: "their message" in _queue_text(page))
    assert stack.server.cancel_pending("other-conn", other["id"])
    stack.server.broadcast_queue()
    assert _wait(lambda: "Cancelled" in _queue_text(page))
