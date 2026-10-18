"""감사 런은 접힌 🔎 카드 안에만 (v10.53.0) — 실브라우저.

종전엔 하니스 작업이 스코프를 열지 않아 감사자의 도구 호출이 main 타임라인에
main 의 것처럼 그려졌다(Task 3 확인). 이제 감사 스레드가 `kind="harness"`
스코프를 열고, 그 스레드의 카드는 전부 그 카드 안에 들어간다.
"""

from __future__ import annotations

import threading
import time


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


TID = "harness-auditor-1-41"


def _audit_thread(stack, gate: threading.Event):
    """하니스 스레드 흉내: 스코프를 열고, 단계를 내고, 닫는다."""

    def run():
        r = stack.renderer
        r.begin_scope(
            task_id=TID,
            kind="harness",
            label="1-41",
            agent="auditor",
            parent="",
            ctx_dir="harness/auditor/1-41",
        )
        r.action("history", '{"query": "SELECT seq FROM history"}', turn=1)
        r.observation("seq 1..41", turn=1, tool_name="history", success=True)
        r.action("read_file", '{"path": "REPORT.md"}', turn=2)
        gate.wait(5)
        r.end_scope(task_id=TID, kind="harness", success=True, duration_s=37.2)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


class TestHarnessCard:
    def test_audit_steps_live_inside_a_collapsed_card(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        gate = threading.Event()
        _audit_thread(stack, gate)
        card = page.locator(f'.card-task-group[data-task-id="{TID}"]')
        assert _wait(lambda: card.count() == 1)
        assert card.get_attribute("data-kind") == "harness"
        assert "scope-harness" in (card.get_attribute("class") or "")
        title = card.locator(".task-title").inner_text()
        assert title == "🔎 auditor · seq 1-41"
        body = card.locator(".task-body")
        assert _wait(lambda: body.locator(".card").count() >= 2)
        assert not body.is_visible()  # 기본 접힘 — 한 줄
        # ★ 회귀 가드: 감사자의 단계가 main 타임라인(카드 밖)에 0장
        outside = page.locator("#messages > .card:not(.card-task-group)").filter(
            has_text="SELECT seq FROM history"
        )
        assert outside.count() == 0
        gate.set()
        assert _wait(lambda: "✓" in card.locator(".task-header").inner_text())

    def test_expanding_shows_what_the_audit_did(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        gate = threading.Event()
        _audit_thread(stack, gate)
        card = page.locator(f'.card-task-group[data-task-id="{TID}"]')
        assert _wait(lambda: card.count() == 1)
        card.locator(".task-header").click()
        body = card.locator(".task-body")
        assert _wait(lambda: body.is_visible())
        assert _wait(lambda: "REPORT.md" in body.inner_text())
        gate.set()
