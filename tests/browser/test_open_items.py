"""📋 열린 항목 띠 (v10.119.0, docs/ledger/DESIGN.md §15) — 실브라우저.

정적 검사는 JS 에 이름이 있는지까지만 본다. 띠의 등장·닫힘 영수증(✓ closed 뒤
사라짐)·예산 0 주황, 늦게 접속한 뷰어가 스티키 스냅샷으로 같은 띠를 받는지는 여기서만
잡힌다.
"""

from __future__ import annotations

import time

from agent_cli.loop.ledger import NAG_BUDGET


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _item(iid, **kw):
    base = {
        "id": iid,
        "source": "user",
        "who": "Sneaky Fox",
        "head": "make the build script idempotent",
        "closer": "complete",
        "turns": 7,
        "hidx": None,
    }
    base.update(kw)
    return base


def _snap(*items, left=6):
    return {"items": list(items), "budget": {"left": left, "total": NAG_BUDGET}}


def _rows(page):
    return page.locator("#open-items .oi-item")


class TestOpenItemsStrip:
    def test_strip_appears_closes_with_a_receipt_and_hides(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        strip = page.locator("#open-items")
        assert strip.is_hidden()
        stack.renderer.open_items(
            _snap(
                _item(
                    "r-1",
                    source="debt",
                    who="agt-code-analyst",
                    closer="reply",
                    head="asked for the list of touched files",
                    turns=None,
                ),
                _item("u-2"),
                _item(
                    "n-1",
                    source="mail",
                    who="mail:monitor",
                    turns=2,
                    head='monitor "ci-log" fired: 3 new lines',
                ),
            )
        )
        assert _wait(lambda: _rows(page).count() == 3)
        text = strip.inner_text()
        assert "📋 open items · the model owes these" in text
        assert "budget 6/8" in text
        assert '→ reply "r-1"' in text and "→ complete, answers" in text
        assert "7 turns" in text and "2 turns" in text
        # 띠 순서 ❓ → 📋 → ⏳
        order = page.evaluate(
            "() => [...document.querySelector('footer').children].map(e => e.id)"
        )
        assert (
            order.index("ask-tray")
            < order.index("open-items")
            < order.index("queue-list")
        )

        # u-2 가 닫힌다 → ✓ closed 영수증, 1.5초 뒤 사라짐
        stack.renderer.open_items(
            _snap(
                _item("r-1", source="debt", closer="reply", turns=None),
                _item("n-1", source="mail", turns=2),
                left=0,
            )
        )
        closed = page.locator("#open-items .oi-item.oi-closed")
        assert _wait(lambda: closed.count() == 1)
        assert "✓ closed" in closed.inner_text() and "u-2" in closed.inner_text()
        assert page.locator("#open-items .oi-budget.oi-budget-out").inner_text() == (
            "budget 0/8"
        )
        assert _wait(lambda: closed.count() == 0, timeout=4.0)
        assert _rows(page).count() == 2

        # 전부 닫힘 → 영수증 뒤 띠가 숨는다
        stack.renderer.open_items(_snap(left=0))
        assert _wait(lambda: strip.is_hidden(), timeout=4.0)

    def test_late_join_gets_the_same_strip_and_row_jumps(self, stack, page):
        stack.emit_ready()
        stack.renderer.push_user_message(
            "[Sneaky Fox]: make the build script idempotent",
            author="Sneaky Fox",
            hidx=4,
        )
        stack.renderer.note_request("u-2", 4)
        stack.renderer.open_items(_snap(_item("u-2")))
        page.goto(stack.url)  # 띠가 선 뒤에 접속 — 스티키 스냅샷으로 받는다
        assert _wait(lambda: _rows(page).count() == 1)
        row = _rows(page).first
        assert "u-2" in row.inner_text() and "oi-jump" in row.get_attribute("class")
        row.click()
        card = page.locator('#messages .card-user[data-hidx="4"]')
        assert _wait(lambda: "tv-nav-hl" in (card.get_attribute("class") or ""))
