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
        # v10.119.1: 닫힘은 ✓ closed 글자로만 — 제목은 긋지 않는다
        assert (
            closed.locator(".oi-head").evaluate(
                "e => getComputedStyle(e).textDecorationLine"
            )
            == "none"
        )
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


def _many(n, **kw):
    return [_item(f"u-{i + 1}", head=f"request number {i + 1}", **kw) for i in range(n)]


def _ids(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('#open-items .oi-item')].map(e => e.dataset.id)"
    )


class TestOpenItemsFold:
    """v10.122.0 접기(§15.7) — 3행까지, 넘치면 "+N more", 펼쳐도 높이 상한."""

    def test_five_items_fold_to_three_and_expand_and_survive_snapshots(
        self, stack, page
    ):
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.open_items(_snap(*_many(5)))
        more = page.locator("#open-items .oi-more")
        assert _wait(lambda: more.count() == 1)
        assert _ids(page) == ["u-1", "u-2", "u-3"]
        assert more.inner_text().strip() == "+2 more ▾"
        assert more.get_attribute("aria-expanded") == "false"
        assert more.get_attribute("aria-controls") == "oi-list"

        more.click()
        assert _wait(lambda: _rows(page).count() == 5)
        assert _ids(page) == ["u-1", "u-2", "u-3", "u-4", "u-5"]
        assert more.inner_text().strip() == "Show less ▴"
        assert more.get_attribute("aria-expanded") == "true"

        # 스냅샷이 다시 와도(턴 수만 바뀜) 펼침 유지
        stack.renderer.open_items(_snap(*_many(5, turns=8)))
        assert _wait(lambda: "8 turns" in _rows(page).first.inner_text())
        assert _rows(page).count() == 5
        assert more.get_attribute("aria-expanded") == "true"

        # 접기
        more.click()
        assert _wait(lambda: _rows(page).count() == 3)
        assert more.inner_text().strip() == "+2 more ▾"

    def test_three_or_fewer_has_no_toggle_and_resets_to_folded(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.open_items(_snap(*_many(3)))
        assert _wait(lambda: _rows(page).count() == 3)
        assert page.locator("#open-items .oi-more").count() == 0

        # 넘쳤다가 펼친 뒤 3개로 줄면 토글이 사라지고, 다시 넘치면 접힌 채로 선다
        stack.renderer.open_items(_snap(*_many(4)))
        more = page.locator("#open-items .oi-more")
        assert _wait(lambda: more.count() == 1)
        assert more.inner_text().strip() == "+1 more ▾"
        more.click()
        assert _wait(lambda: _rows(page).count() == 4)
        stack.renderer.open_items(_snap(*_many(4)[1:]))  # u-1 닫힘 → 열린 3 + 영수증
        assert _wait(lambda: more.count() == 0)
        stack.renderer.open_items(_snap(*_many(6)))
        assert _wait(lambda: more.count() == 1)
        assert more.get_attribute("aria-expanded") == "false"
        assert _ids(page) == ["u-1", "u-2", "u-3"]

    def test_receipts_only_take_spare_rows_and_are_not_counted(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.open_items(_snap(*_many(5)))
        assert _wait(lambda: page.locator("#open-items .oi-more").count() == 1)
        # u-5 가 닫힌다 — 열린 4개가 3행을 채우므로 영수증은 접힌 띠에 안 보이고 N 에도 안 든다
        stack.renderer.open_items(_snap(*_many(4)))
        more = page.locator("#open-items .oi-more")
        assert _wait(lambda: more.inner_text().strip() == "+1 more ▾")
        assert _ids(page) == ["u-1", "u-2", "u-3"]
        assert page.locator("#open-items .oi-closed").count() == 0
        # 펼치면 영수증도 보인다(1.5초 안)
        more.click()
        assert _wait(lambda: page.locator("#open-items .oi-closed").count() == 1)
        assert _ids(page) == ["u-1", "u-2", "u-3", "u-4", "u-5"]

        # 열린 2개 + 영수증 둘 → 한도 3 안에 영수증 하나만, 토글 없음
        stack.renderer.open_items(_snap(*_many(2)))
        assert _wait(lambda: more.count() == 0)
        assert _rows(page).count() == 3
        assert page.locator("#open-items .oi-closed").count() == 1
        # 영수증이 다 거둬지면 열린 2개만
        assert _wait(lambda: _rows(page).count() == 2, timeout=4.0)

    def test_expanded_list_is_capped_and_scrolls_with_header_visible(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.open_items(_snap(*_many(40)))
        more = page.locator("#open-items .oi-more")
        assert _wait(lambda: more.count() == 1)
        assert more.inner_text().strip() == "+37 more ▾"
        more.click()
        assert _wait(lambda: _rows(page).count() == 40)
        m = page.evaluate(
            """() => { const l = document.querySelector('#open-items .oi-list');
                const s = document.getElementById('open-items').getBoundingClientRect();
                return {h: l.clientHeight, sh: l.scrollHeight, vh: innerHeight,
                        oy: getComputedStyle(l).overflowY, strip: s.height}; }"""
        )
        assert m["oy"] == "auto"
        assert m["sh"] > m["h"]
        assert m["h"] <= m["vh"] * 0.35 + 1
        assert m["strip"] < m["vh"] * 0.5  # 띠 전체가 대화 창을 다 가리지 않는다
        # 목록을 끝까지 내려도 머리와 토글은 화면 안
        page.evaluate(
            "() => { const l = document.querySelector('#open-items .oi-list'); l.scrollTop = l.scrollHeight; }"
        )
        assert page.locator("#open-items .oi-hd").is_visible()
        assert more.is_visible()
        # 스냅샷이 다시 와도 스크롤 위치를 지킨다
        before = page.evaluate(
            "() => document.querySelector('#open-items .oi-list').scrollTop"
        )
        stack.renderer.open_items(_snap(*_many(40, turns=9)))
        assert _wait(lambda: "9 turns" in _rows(page).first.inner_text())
        after = page.evaluate(
            "() => document.querySelector('#open-items .oi-list').scrollTop"
        )
        assert before > 0 and abs(after - before) <= 1

    def test_toggle_works_from_the_keyboard_and_keeps_focus(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.open_items(_snap(*_many(5)))
        more = page.locator("#open-items .oi-more")
        assert _wait(lambda: more.count() == 1)
        more.focus()
        page.keyboard.press("Enter")
        assert _wait(lambda: _rows(page).count() == 5)
        assert more.get_attribute("aria-expanded") == "true"
        # 다시 그려도 초점이 토글에 남아 Space 로 바로 접는다
        assert page.evaluate(
            "() => document.activeElement.classList.contains('oi-more')"
        )
        page.keyboard.press("Space")
        assert _wait(lambda: _rows(page).count() == 3)
        assert more.get_attribute("aria-expanded") == "false"
        stack.renderer.open_items(_snap(*_many(5, turns=3)))
        assert _wait(lambda: "3 turns" in _rows(page).first.inner_text())
        assert page.evaluate(
            "() => document.activeElement.classList.contains('oi-more')"
        )
