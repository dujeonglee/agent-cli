"""Δ Changes 패널 (v10.137.0) — 실브라우저 e2e.

칩 → 패널 → 파일 diff → Clear(빈 상태·Undo 토스트, 파일 그대로) → Undo → 복귀, 그리고
런 끝(main 은 `worker_state` 바쁨→한가, 그 밖은 `scope_end`) 뒤 칩 갱신. 저장소는
실제 git 스냅샷(runtime 이 세우는 것과 같은 `get_snapshot(create=True)`)이다.
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.snapshot import get_snapshot, git_available, reset_snapshot

pytestmark = pytest.mark.skipif(not git_available()[0], reason="git not installed")

_CHIP = "() => document.querySelector('#changes-chip').textContent"


@pytest.fixture
def ws(tmp_path, stack):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "inventory.py").write_text("def restock(item, qty):\n    return qty\n")
    (w / "old_orders.csv").write_text("a,b\n1,2\n")
    reset_snapshot()
    store = get_snapshot(w, create=True, session_id="sess-browser")
    assert store.enabled, store.reason
    stack.server.workspace = w.resolve()
    yield w
    reset_snapshot()


def _open(page, stack):
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#changes-chip:not([hidden])", timeout=8000)
    return errors


def _edit(w):
    (w / "inventory.py").write_text(
        "def restock(item, qty):\n    if qty <= 0:\n        raise ValueError\n    return qty\n"
    )
    (w / "old_orders.csv").unlink()
    (w / "tests").mkdir()
    (w / "tests" / "test_stock.py").write_text("x = 1\n")


def test_chip_panel_diff_clear_undo(stack, page, ws):
    _edit(ws)
    errors = _open(page, stack)
    page.wait_for_function(_CHIP + " === 'Δ 3 files'", timeout=8000)
    tip = page.get_attribute("#changes-chip", "title")
    assert tip.startswith("Files changed on disk since session start · ")
    assert "model" not in tip.lower()

    page.click("#changes-chip")
    page.wait_for_selector("#changes-drawer.open .chg-row", timeout=8000)
    rows = page.evaluate(
        """() => [...document.querySelectorAll('#changes-list .chg-row')].map(r =>
            [r.querySelector('.chg-st').textContent, r.querySelector('.chg-path').textContent,
             r.querySelector('.chg-n').textContent])"""
    )
    assert rows == [
        ["M", "inventory.py", "+2"],
        ["D", "old_orders.csv", "−2"],
        ["A", "tests/test_stock.py", "+1"],
    ]
    assert page.inner_text("#changes-since").startswith("since session start · ")

    page.click('.chg-row[data-path="inventory.py"]')
    page.wait_for_selector("#changes-diff:not([hidden]) .rich-green", timeout=8000)
    diff = page.inner_text("#changes-diff")
    assert "+    if qty <= 0:" in diff and "--- a/inventory.py" in diff

    before = (ws / "inventory.py").read_text()
    page.click("#changes-clear")
    page.wait_for_selector("#changes-toast:not([hidden])", timeout=8000)
    state = page.evaluate(
        """() => ({
            empty: document.querySelector('#changes-list .chg-empty').innerText,
            toast: document.querySelector('#changes-toast').innerText,
            since: document.querySelector('#changes-since').textContent,
            rows: document.querySelectorAll('#changes-list .chg-row').length,
            diffHidden: document.querySelector('#changes-diff').hidden,
            chip: document.querySelector('#changes-chip').textContent,
            zero: document.querySelector('#changes-chip').classList.contains('chg-zero'),
        })"""
    )
    assert state["rows"] == 0 and state["diffHidden"] is True
    assert state["empty"].startswith("No changes since ")
    assert (
        "Files on disk were not touched — only this list was reset." in state["empty"]
    )
    assert state["since"].endswith(" · cleared")
    assert "List cleared (3 files)." in state["toast"] and "Undo" in state["toast"]
    assert state["chip"] == "Δ 0" and state["zero"] is True
    assert (ws / "inventory.py").read_text() == before  # 파일은 그대로
    assert not (ws / "old_orders.csv").exists()

    page.click("#changes-toast .chg-undo")
    page.wait_for_function(_CHIP + " === 'Δ 3 files'", timeout=8000)
    assert page.locator("#changes-list .chg-row").count() == 3
    assert page.inner_text("#changes-since").startswith("since session start · ")
    assert page.evaluate("() => document.querySelector('#changes-toast').hidden")
    assert errors == []


def test_chip_recounts_at_run_end(stack, page, ws):
    errors = _open(page, stack)
    page.wait_for_function(_CHIP + " === 'Δ 0'", timeout=8000)
    # 로드 때의 셈(스냅샷 재생 끝에 한 번 더 — RECOUNT_MS 로 합친다)이 다 지나간 뒤에
    # 디스크를 바꾼다 — 그래야 아래 갱신이 런 끝 신호 덕인지 가린다
    page.wait_for_timeout(1000)
    r = stack.renderer
    # main 런: 바쁨 → (디스크가 바뀜) → 한가
    r.worker_busy()
    (ws / "inventory.py").write_text("changed\n")
    r.worker_idle()
    page.wait_for_function(_CHIP + " === 'Δ 1 file'", timeout=8000)

    # 에이전트·스킬 런: scope_end
    def body():
        r.begin_scope(task_id="t1", kind="run", index=0, agent="tester", label="t")
        (ws / "new.md").write_text("x\n")
        r.end_scope(task_id="t1", kind="run", success=True, duration_s=0.1)

    t = threading.Thread(target=body)
    t.start()
    t.join()
    page.wait_for_function(_CHIP + " === 'Δ 2 files'", timeout=8000)
    # 패널의 ↻ 도 센다
    page.click("#changes-chip")
    page.wait_for_selector("#changes-drawer.open .chg-row", timeout=8000)
    (ws / "third.txt").write_text("3\n")
    page.click("#changes-refresh")
    page.wait_for_function(_CHIP + " === 'Δ 3 files'", timeout=8000)
    assert page.locator("#changes-list .chg-row").count() == 3
    assert errors == []


def test_snapshots_off_hides_the_chip(stack, page):
    reset_snapshot()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#ctx-head > .ctx-top", timeout=8000)
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.querySelector('#changes-chip').hidden")
    page.evaluate("() => document.querySelector('#changes-chip').click()")
    page.wait_for_selector("#changes-drawer.open", timeout=8000)
    page.wait_for_function(
        "() => /Snapshots are off in this room/.test("
        "document.querySelector('#changes-list').textContent)",
        timeout=8000,
    )
    assert page.evaluate("() => document.querySelector('#changes-clear').disabled")
    assert errors == []
