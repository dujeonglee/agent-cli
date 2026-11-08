"""🔁 제자리 고리 띠 (v10.120.0, docs/stale-guard/DESIGN.md §5) — 실브라우저.

정적 검사는 JS 에 이름이 있는지까지만 본다. 띠의 등장·숨김, Stop 버튼이 실제로 런의
중단 핸들에 닿는지, 두 번째 발화의 확인이 ❓ 트레이에 코멘트 칸과 함께 뜨고 그 코멘트와
뷰어 닉네임이 워커에 도착하는지는 여기서만 잡힌다.
"""

from __future__ import annotations

import threading
import time

from agent_cli.render.base import ConfirmOption


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _info(n, *, asking=False, fires=1):
    return {"turns_without_new": n, "since_turn": 4, "fires": fires, "asking": asking}


class TestStaleBand:
    def test_band_line_stop_button_and_hide(self, stack, page):
        stack.emit_ready()
        stop = threading.Event()
        stack.server.set_stop_handle(stop)
        page.goto(stack.url)
        band = page.locator("#stuck-band")
        assert band.is_hidden()
        stack.renderer.stale_loop(_info(3))
        assert _wait(lambda: band.is_visible())
        assert "🔁 No new results for 3 turns — the model may be going in circles." in (
            band.inner_text()
        )
        # 입력창 바로 위 — 띠 순서의 마지막(대기열 뒤)
        order = page.evaluate(
            "() => [...document.querySelector('footer').children].map(e => e.id)"
        )
        assert (
            order.index("queue-list")
            < order.index("stuck-band")
            < order.index("input-area")
        )
        stack.renderer.stale_loop(_info(4))  # N 이 는다
        assert _wait(lambda: "for 4 turns" in band.inner_text())
        page.click("#stuck-band .stuck-stop")
        assert _wait(stop.is_set)  # Stop 버튼과 같은 /api/stop
        assert _wait(lambda: "Stopping…" in band.inner_text())
        stack.renderer.stale_loop(_info(0))  # 런 끝 — 띠가 내려간다
        assert _wait(lambda: band.is_hidden())
        stack.server.set_stop_handle(None)

    def test_second_fire_tray_confirm_carries_the_comment_and_the_viewer(
        self, stack, page
    ):
        """두 번째 발화 = 기존 confirm. 트레이의 코멘트 칸이 Hint, 답한 뷰어가 작성자."""
        stack.emit_ready()
        got: dict = {}

        def worker():
            while not stack.renderer.hub.has_live_connections():
                time.sleep(0.05)
            stack.renderer.stale_loop(_info(6, asking=True, fires=2))
            try:
                got["answer"] = stack.renderer.confirm(
                    "🔁 No new results for 6 turns (since turn 4) — the model may be "
                    "going in circles.\nRepeated calls:\n  shell ls /app | wc -l → 17",
                    [ConfirmOption("c", "Continue"), ConfirmOption("s", "Stop")],
                    default_key="c",
                )
            except EOFError:
                return
            got["author"] = stack.renderer.confirm_author()

        t = threading.Thread(target=worker, daemon=True)
        stack._workers.append(t)
        t.start()
        page.goto(stack.url)
        page.wait_for_selector(".ask-main .confirm-btn", timeout=8000)
        why = page.inner_text(".ask-main .confirm-why")
        assert "No new results for 6 turns" in why and "→ 17" in why
        assert "The run is paused" in page.inner_text("#stuck-band")
        buttons = page.locator(".ask-main .confirm-btn")
        assert [buttons.nth(i).inner_text() for i in range(buttons.count())] == [
            "c — Continue",
            "s — Stop",
        ]
        nick = stack.renderer.hub.nickname_for(stack.renderer.hub._connections[0].id)
        page.fill(".ask-main .ask-answer", "read the WAL file instead")
        page.click(".ask-main .confirm-btn >> nth=0")
        assert _wait(lambda: "author" in got)
        assert got["answer"] == ("c", "read the WAL file instead")
        assert got["author"] == nick and nick not in ("", "?")
        t.join(timeout=3)

    def test_band_stop_while_asking_answers_the_confirm(self, stack, page):
        """멈춰 묻는 중엔 워커가 턴 경계에 닿지 못한다 — 띠의 Stop 은 확인에 s 로 답한다."""
        stack.emit_ready()
        got: dict = {}

        def worker():
            while not stack.renderer.hub.has_live_connections():
                time.sleep(0.05)
            stack.renderer.stale_loop(_info(6, asking=True, fires=2))
            try:
                got["answer"] = stack.renderer.confirm(
                    "🔁 paused",
                    [ConfirmOption("c", "Continue"), ConfirmOption("s", "Stop")],
                    default_key="c",
                )
            except EOFError:
                return

        t = threading.Thread(target=worker, daemon=True)
        stack._workers.append(t)
        t.start()
        page.goto(stack.url)
        page.wait_for_selector(".ask-main .confirm-btn", timeout=8000)
        page.click("#stuck-band .stuck-stop")
        assert _wait(lambda: "answer" in got)
        assert got["answer"] == ("s", "")
        t.join(timeout=3)
