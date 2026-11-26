"""🔁 제자리 고리 띠 (v10.120.0, docs/stale-guard/DESIGN.md §5) — 실브라우저.

정적 검사는 JS 에 이름이 있는지까지만 본다. 띠의 등장·숨김, Stop 버튼이 실제로 런의
중단 핸들에 닿는지, 두 번째 발화의 확인(`c-N`)이 ❓ 트레이에 코멘트 칸과 함께 뜨고 그 코멘트와
뷰어 닉네임이 워커에 도착하는지, 멈춰 묻는 중의 띠 Stop 이 정지로 그 확인을 깨우는지는 여기서만 잡힌다.
"""

from __future__ import annotations

import threading
import time

from agent_cli import confirm


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
        """두 번째 발화 = 확인(`c-N`). 트레이의 코멘트 칸이 Hint, 답한 뷰어가 작성자."""
        stack.emit_ready()
        got: dict = {}

        def worker():
            if not stack.wait_connected():
                return
            stack.renderer.stale_loop(_info(6, asking=True, fires=2))
            _cid, got["decision"] = confirm.ask_person(
                "🔁 No new results for 6 turns (since turn 4) — the model may be "
                "going in circles.",
                "Repeated calls:\n  shell ls /app | wc -l → 17",
                (
                    confirm.Choice(confirm.APPROVE, "Continue"),
                    confirm.Choice(confirm.DENY, "Stop"),
                ),
                owner="main",
                stopped=lambda: stack._uv.should_exit,
            )

        t = threading.Thread(target=worker, daemon=True)
        stack._workers.append(t)
        t.start()
        page.goto(stack.url)
        page.wait_for_selector(".ask-confirm .confirm-btn", timeout=8000)
        why = page.inner_text(".ask-confirm .confirm-why")
        assert "No new results for 6 turns" in why and "→ 17" in why
        assert "The run is paused" in page.inner_text("#stuck-band")
        buttons = page.locator(".ask-confirm .confirm-btn")
        assert [buttons.nth(i).inner_text() for i in range(buttons.count())] == [
            "Continue",
            "Stop",
        ]
        nick = stack.renderer.hub.nickname_for(stack.renderer.hub._connections[0].id)
        page.fill(".ask-confirm .ask-answer", "read the WAL file instead")
        page.click(".ask-confirm .confirm-btn >> nth=0")
        assert _wait(lambda: "decision" in got)
        d = got["decision"]
        assert (d.action, d.note) == ("approve", "read the WAL file instead")
        assert d.by == f"user:{nick}" and nick not in ("", "?")
        t.join(timeout=3)

    def test_band_stop_while_asking_stops_the_run_and_wakes_the_confirm(
        self, stack, page
    ):
        """멈춰 묻는 중에도 띠의 Stop 은 정지 하나(`/api/stop`) — 그 정지가 기다리는 확인을 깨운다(6d)."""
        stack.emit_ready()
        stop = threading.Event()
        stack.server.set_stop_handle(stop)
        got: dict = {}

        def worker():
            if not stack.wait_connected():
                return
            stack.renderer.stale_loop(_info(6, asking=True, fires=2))
            _cid, got["decision"] = confirm.ask_person(
                "🔁 paused",
                "",
                (
                    confirm.Choice(confirm.APPROVE, "Continue"),
                    confirm.Choice(confirm.DENY, "Stop"),
                ),
                owner="main",
                stopped=stop.is_set,
            )

        t = threading.Thread(target=worker, daemon=True)
        stack._workers.append(t)
        t.start()
        page.goto(stack.url)
        page.wait_for_selector(".ask-confirm .confirm-btn", timeout=8000)
        page.click("#stuck-band .stuck-stop")
        assert _wait(lambda: "decision" in got)
        assert got["decision"].action == "stopped"
        assert _wait(lambda: page.locator(".ask-confirm").count() == 0)
        t.join(timeout=3)
        stack.server.set_stop_handle(None)
