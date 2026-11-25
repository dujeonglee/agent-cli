"""기다리는 회신 · 제자리 대기 · `message` 카드 — 실브라우저 (v10.141.0, docs/agent-engine/DESIGN.md §2.6).

설계 §5 시안 A·B: 📋 띠의 머리가 "Waiting for N reply · r-21 from agt-3" 를 말하고, main 이 제자리에서
기다리는 동안엔 띠 끝에 "main is waiting for agt-3 — type to steer it or give new work" 가 선다(깨면
내려간다). main 의 `message` 는 발신 카드(→ 상대). 늦게 접속한 뷰어도 스티키로 같은 화면을 받는다.
"""

from __future__ import annotations

import json
import time

from agent_cli.loop.ledger import NAG_BUDGET


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _awaiting(iid="r-21", who="agt-3", head="find every caller of parse_tokens"):
    return {
        "id": iid,
        "source": "awaiting",
        "who": who,
        "head": head,
        "closer": "",
        "turns": None,
        "hidx": None,
    }


def _snap(*items):
    return {"items": list(items), "budget": {"left": 8, "total": NAG_BUDGET}}


AWAITED = [{"id": "r-21", "from": "agent:agt-3", "text": "find every caller"}]


class TestWaitingBand:
    def test_band_title_and_wait_line_then_the_line_goes_on_wake(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        band = page.locator("#open-items")
        stack.renderer.open_items(_snap(_awaiting()))
        assert _wait(band.is_visible)
        title = band.locator(".oi-title")
        assert title.inner_text() == "📋 Waiting for 1 reply · r-21 from agt-3"
        row = band.locator(".oi-item").first
        assert "r-21" in row.inner_text() and "← reply" in row.inner_text()
        stack.renderer.wait_in_place(True, AWAITED)
        line = band.locator(".oi-wait")
        assert _wait(lambda: line.count() == 1)
        assert line.inner_text() == (
            "main is waiting for agt-3 — type to steer it or give new work"
        )
        stack.renderer.wait_in_place(False, [])  # 사람 입력·회신에 깼다
        assert _wait(lambda: line.count() == 0)
        assert band.is_visible()  # 회신은 아직 — 띠는 남는다

    def test_job_only_wait_draws_the_line_by_itself(self, stack, page):
        """5c-1 결정 C: 열린 항목 없이 작업만 기다려도 대기 줄이 선다 — 짧은 이름으로, 깨면 띠가 내려간다."""
        stack.emit_ready()
        page.goto(stack.url)
        band = page.locator("#open-items")
        stack.renderer.wait_in_place(True, [], others=["job #1"])
        line = band.locator(".oi-wait")
        assert _wait(lambda: line.count() == 1)
        assert line.inner_text() == (
            "main is waiting for job #1 — type to steer it or give new work"
        )
        assert band.locator(".oi-title").count() == 0  # 항목이 없으니 머리도 없다
        stack.renderer.wait_in_place(False, [])
        assert _wait(lambda: not band.is_visible())

    def test_mixed_wait_line_names_replies_then_jobs(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.open_items(_snap(_awaiting()))
        stack.renderer.wait_in_place(True, AWAITED, others=["job #1", "monitor m-2"])
        line = page.locator("#open-items .oi-wait")
        assert _wait(lambda: line.count() == 1)
        assert line.inner_text() == (
            "main is waiting for agt-3, job #1, monitor m-2 — type to steer it or give "
            "new work"
        )

    def test_mixed_band_says_the_model_owes_the_rest(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        user = {
            "id": "u-2",
            "source": "user",
            "who": "Ana",
            "head": "skip tests",
            "closer": "complete",
            "turns": 1,
            "hidx": None,
        }
        stack.renderer.open_items(
            _snap(_awaiting(), _awaiting("r-22", "agt-4", "review"), user)
        )
        title = page.locator("#open-items .oi-title")
        assert _wait(lambda: "Waiting for 2 replies" in title.inner_text())
        assert title.inner_text() == (
            "📋 Waiting for 2 replies · r-21 from agt-3 +1 · the model owes the rest"
        )

    def test_late_viewer_gets_the_wait_line_from_the_sticky(self, stack, page):
        stack.emit_ready()
        stack.renderer.open_items(_snap(_awaiting()))
        stack.renderer.wait_in_place(True, AWAITED)
        page.goto(stack.url)
        line = page.locator("#open-items .oi-wait")
        assert _wait(lambda: line.count() == 1)
        assert "main is waiting for agt-3" in line.inner_text()


class TestMessageCard:
    def test_main_message_is_a_send_card_to_the_agent(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.agent_roster(
            [
                {
                    "key": "agt-3",
                    "profile": "explorer",
                    "name": "",
                    "state": "busy",
                    "handled": 0,
                    "pending_requests": 0,
                    "est_tokens": 0,
                    "error": "",
                    "ends_after_task": True,
                }
            ]
        )
        stack.renderer.action(
            "message",
            json.dumps(
                {
                    "to": "agt-3",
                    "text": "Scope change: ignore tests/",
                    "expects_reply": False,
                }
            ),
            1,
        )
        card = page.locator("#messages .card").last
        assert _wait(lambda: "Scope change: ignore tests/" in card.inner_text())
        assert "→" in card.inner_text()
        assert "explorer" in card.inner_text()  # 상대의 이름표
