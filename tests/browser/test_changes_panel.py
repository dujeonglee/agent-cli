"""Δ Changes 패널 (v10.137.0 · 입구 v10.137.1) — 실브라우저 e2e.

입구 둘: 📁(`#files-btn`) 안의 숫자 배지(누르면 패널, 아이콘은 Workspace files 그대로)와
main 채널의 가장 최근 `complete` 카드 안의 한 줄. 패널 → 파일 diff → Clear(빈 상태·Undo
토스트, 파일 그대로) → Undo → 복귀, 런 끝(main 은 `worker_state` 바쁨→한가, 그 밖은
`scope_end`) 뒤 갱신, 긴 diff 가 목록을 누르지 않음. v10.138.0: 고른 행을 다시 누르면 접힘(▸/▾),
📁 Workspace files 트리의 변경 표시(파일 M·A, 폴더 ●) — 글자를 누르면 그 파일 diff. 저장소는 실제 git 스냅샷(runtime 이
세우는 것과 같은 `get_snapshot(create=True)`)이다.
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.snapshot import get_snapshot, git_available, reset_snapshot

pytestmark = pytest.mark.skipif(not git_available()[0], reason="git not installed")

# 배지에 보이는 숫자 — 숨겨져 있으면 ""
_BADGE_EXPR = (
    "(b => b.hidden ? '' : b.textContent)(document.querySelector('#changes-badge'))"
)
_BADGE = "() => " + _BADGE_EXPR
# 런 끝 줄의 상태 — 몇 개이고 어느 카드(최종답 본문)에 붙었는가
_LINES = """() => [...document.querySelectorAll('#messages .final-changes')].map(l => ({
    text: l.textContent,
    card: l.parentNode.querySelector(':scope > .final').textContent,
    prev: l.previousElementSibling.className,
}))"""


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
    page.wait_for_selector("#ctx-head > .ctx-top", timeout=8000)
    return errors


def _edit(w):
    (w / "inventory.py").write_text(
        "def restock(item, qty):\n    if qty <= 0:\n        raise ValueError\n    return qty\n"
    )
    (w / "old_orders.csv").unlink()
    (w / "tests").mkdir()
    (w / "tests" / "test_stock.py").write_text("x = 1\n")


def _drawers(page):
    return page.evaluate(
        """() => ({
            changes: document.querySelector('#changes-drawer').classList.contains('open'),
            files: document.querySelector('#download-drawer').classList.contains('open'),
        })"""
    )


def test_badge_panel_diff_clear_undo(stack, page, ws):
    _edit(ws)
    errors = _open(page, stack)
    page.wait_for_function(f"() => {_BADGE_EXPR} === '3'", timeout=8000)
    tip = page.get_attribute("#changes-badge", "title")
    assert tip.startswith("3 files changed on disk since session start · ")
    assert tip.endswith(" — open Changes")
    assert "model" not in tip.lower()
    # 📁 의 원래 툴팁은 그대로
    assert (
        page.get_attribute("#files-btn", "title")
        == "Workspace files — download / upload"
    )
    # 상단 칩은 없다
    assert page.locator("#changes-chip").count() == 0

    page.click("#changes-badge")
    page.wait_for_selector("#changes-drawer.open .chg-row", timeout=8000)
    assert _drawers(page) == {"changes": True, "files": False}
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
            badgeHidden: document.querySelector('#changes-badge').hidden,
        })"""
    )
    assert state["rows"] == 0 and state["diffHidden"] is True
    assert state["empty"].startswith("No changes since ")
    assert (
        "Files on disk were not touched — only this list was reset." in state["empty"]
    )
    assert state["since"].endswith(" · cleared")
    assert "List cleared (3 files)." in state["toast"] and "Undo" in state["toast"]
    assert state["badgeHidden"] is True  # 0 이면 숨김
    assert (ws / "inventory.py").read_text() == before  # 파일은 그대로
    assert not (ws / "old_orders.csv").exists()

    page.click("#changes-toast .chg-undo")
    page.wait_for_function(f"() => {_BADGE_EXPR} === '3'", timeout=8000)
    assert page.locator("#changes-list .chg-row").count() == 3
    assert page.inner_text("#changes-since").startswith("since session start · ")
    assert page.evaluate("() => document.querySelector('#changes-toast').hidden")
    assert errors == []


def test_folder_icon_still_opens_workspace_files(stack, page, ws):
    """배지 밖(아이콘)을 누르면 지금처럼 Workspace files 드로어 — Changes 는 안 열린다."""
    _edit(ws)
    errors = _open(page, stack)
    page.wait_for_function(f"() => {_BADGE_EXPR} === '3'", timeout=8000)
    page.click("#files-btn", position={"x": 6, "y": 14})  # 배지는 오른쪽 위
    page.wait_for_selector("#download-drawer.open", timeout=8000)
    assert _drawers(page) == {"changes": False, "files": True}
    assert errors == []


def test_badge_recounts_at_run_end(stack, page, ws):
    errors = _open(page, stack)
    # 로드 때의 셈(스냅샷 재생 끝에 한 번 더 — RECOUNT_MS 로 합친다)이 다 지나간 뒤에
    # 디스크를 바꾼다 — 그래야 아래 갱신이 런 끝 신호 덕인지 가린다
    page.wait_for_timeout(1000)
    assert page.evaluate(_BADGE) == ""
    r = stack.renderer
    # main 런: 바쁨 → (디스크가 바뀜) → 한가
    r.worker_busy()
    (ws / "inventory.py").write_text("changed\n")
    r.worker_idle()
    page.wait_for_function(f"() => {_BADGE_EXPR} === '1'", timeout=8000)

    # 에이전트·스킬 런: scope_end
    def body():
        r.begin_scope(task_id="t1", kind="run", index=0, agent="tester", label="t")
        (ws / "new.md").write_text("x\n")
        r.end_scope(task_id="t1", kind="run", success=True, duration_s=0.1)

    t = threading.Thread(target=body)
    t.start()
    t.join()
    page.wait_for_function(f"() => {_BADGE_EXPR} === '2'", timeout=8000)
    # 패널의 ↻ 도 센다
    page.click("#changes-badge")
    page.wait_for_selector("#changes-drawer.open .chg-row", timeout=8000)
    (ws / "third.txt").write_text("3\n")
    page.click("#changes-refresh")
    page.wait_for_function(f"() => {_BADGE_EXPR} === '3'", timeout=8000)
    assert page.locator("#changes-list .chg-row").count() == 3
    assert errors == []


def test_long_diff_does_not_cover_the_list(stack, page, ws):
    """긴 새 파일의 diff 를 열어도 목록의 모든 행이 목록 상자 안에 보인다 (v10.137.1).

    종전 `.chg-list { flex: 0 1 auto }` 는 긴 diff 가 목록을 최소 높이 0 까지 눌러
    파일 줄이 diff 밑에 반쯤 깔렸다(실제 방, +198줄 새 파일)."""
    (ws / "big_module.py").write_text("".join(f"line_{i} = {i}\n" for i in range(240)))
    (ws / "inventory.py").write_text("changed\n")
    (ws / "notes.md").write_text("n\n")
    errors = _open(page, stack)
    page.wait_for_function(f"() => {_BADGE_EXPR} === '3'", timeout=8000)
    page.click("#changes-badge")
    page.wait_for_selector("#changes-drawer.open .chg-row", timeout=8000)
    page.click('.chg-row[data-path="big_module.py"]')
    page.wait_for_selector("#changes-diff:not([hidden]) .rich-green", timeout=8000)
    page.wait_for_timeout(350)  # 슬라이드-인이 끝난 뒤의 배치
    geo = page.evaluate(
        """() => {
            const list = document.querySelector('#changes-list').getBoundingClientRect();
            const diff = document.querySelector('#changes-diff');
            const panel = document.querySelector('#changes-drawer');
            return {
                rows: [...document.querySelectorAll('#changes-list .chg-row')].map(r => {
                    const b = r.getBoundingClientRect();
                    return [b.top, b.bottom];
                }),
                list: [list.top, list.bottom],
                diffTop: diff.getBoundingClientRect().top,
                diffScrolls: diff.scrollHeight > diff.clientHeight,
                panel: [panel.scrollHeight, panel.clientHeight],
            };
        }"""
    )
    assert len(geo["rows"]) == 3
    for top, bottom in geo["rows"]:
        assert top >= geo["list"][0] - 0.5 and bottom <= geo["list"][1] + 0.5, geo
    assert geo["list"][1] <= geo["diffTop"] + 0.5, geo
    assert geo["diffScrolls"] is True  # 긴 diff 는 자기 상자 안에서 스크롤
    assert geo["panel"][0] == geo["panel"][1], geo  # 패널 자체는 스크롤하지 않는다
    assert errors == []


def _main_run(stack, ws, answer, fname, requests=None):
    r = stack.renderer
    r.worker_busy()
    (ws / fname).write_text(answer + "\n")
    r.final(answer, 1, requests=requests)
    r.worker_idle()


def test_run_end_line_moves_to_the_latest_main_complete(stack, page, ws):
    errors = _open(page, stack)
    page.wait_for_timeout(1000)  # 로드 때의 셈이 지나간 뒤
    assert page.evaluate(_LINES) == []

    _main_run(stack, ws, "first answer", "a.txt")
    page.wait_for_function(f"() => {_BADGE_EXPR} === '1'", timeout=8000)
    page.wait_for_function(
        "() => document.querySelectorAll('.final-changes').length === 1"
    )
    assert page.evaluate(_LINES) == [
        {
            "text": "Δ 1 file changed · View changes",
            "card": "first answer",
            "prev": "final",
        }
    ]
    tip = page.get_attribute(".final-changes", "title")
    assert tip.startswith("1 file changed on disk since session start · ")

    # 두 번째 런 — 답한 요청 줄이 있으면 그 뒤에, 옛 카드에선 빠진다
    _main_run(
        stack,
        ws,
        "second answer",
        "b.txt",
        requests=[{"id": "u-2", "author": "kim", "text": "do b"}],
    )
    page.wait_for_function(f"() => {_BADGE_EXPR} === '2'", timeout=8000)
    page.wait_for_function(
        "() => document.querySelector('.final-changes').textContent"
        " === 'Δ 2 files changed · View changes'"
    )
    assert page.evaluate(_LINES) == [
        {
            "text": "Δ 2 files changed · View changes",
            "card": "second answer",
            "prev": "final-answers",
        }
    ]

    # 상주 에이전트 채널의 complete 에는 붙지 않는다(런 블록 안의 카드)
    def agent():
        r = stack.renderer
        r.begin_scope(task_id="bot#u-1", kind="run", agent="bot", label="t", parent="")
        r.final("agent answer", turn=1)
        (ws / "c.txt").write_text("c\n")
        r.end_scope(task_id="bot#u-1", kind="run", success=True, duration_s=0.1)

    t = threading.Thread(target=agent)
    t.start()
    t.join()
    page.wait_for_function(f"() => {_BADGE_EXPR} === '3'", timeout=8000)
    page.wait_for_function(
        "() => document.querySelector('.final-changes').textContent.startsWith('Δ 3 ')"
    )
    lines = page.evaluate(_LINES)
    assert len(lines) == 1 and lines[0]["card"] == "second answer"

    # 화면 전용 — 대화 복사에 들어가지 않는다
    md = page.evaluate("() => window.__timelineMarkdown()")
    assert "second answer" in md and "View changes" not in md and "Δ" not in md

    # 줄을 누르면 패널
    page.click(".final-changes")
    page.wait_for_selector("#changes-drawer.open .chg-row", timeout=8000)
    assert _drawers(page) == {"changes": True, "files": False}

    # Clear → 0 → 줄도 사라진다, Undo 로 돌아온다
    page.click("#changes-clear")
    page.wait_for_selector("#changes-toast:not([hidden])", timeout=8000)
    assert page.evaluate(_LINES) == []
    page.click("#changes-toast .chg-undo")
    page.wait_for_function(f"() => {_BADGE_EXPR} === '3'", timeout=8000)
    assert len(page.evaluate(_LINES)) == 1
    assert errors == []


def test_run_end_line_after_reload(stack, page, ws):
    """재접속 — 다시 그린 카드 중 마지막 main complete 에 다시 붙는다."""
    errors = _open(page, stack)
    page.wait_for_timeout(1000)
    _main_run(stack, ws, "first answer", "a.txt")
    _main_run(stack, ws, "second answer", "b.txt")
    page.wait_for_function(
        "() => document.querySelectorAll('.final-changes').length === 1"
    )
    page.reload()
    page.wait_for_function(f"() => {_BADGE_EXPR} === '2'", timeout=8000)
    page.wait_for_function(
        "() => document.querySelectorAll('.final-changes').length === 1"
    )
    lines = page.evaluate(_LINES)
    assert [x["card"] for x in lines] == ["second answer"]
    assert page.locator("#messages > .card-assistant > .final").count() == 2
    assert errors == []


def test_snapshots_off_hides_the_badge(stack, page):
    reset_snapshot()
    errors = _open(page, stack)
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.querySelector('#changes-badge').hidden")
    stack.renderer.final("answer", 1)
    page.wait_for_selector("#messages > .card-assistant > .final", timeout=8000)
    page.wait_for_timeout(300)
    assert page.evaluate(_LINES) == []
    page.evaluate("() => document.querySelector('#changes-badge').click()")
    page.wait_for_selector("#changes-drawer.open", timeout=8000)
    page.wait_for_function(
        "() => /Snapshots are off in this room/.test("
        "document.querySelector('#changes-list').textContent)",
        timeout=8000,
    )
    assert page.evaluate("() => document.querySelector('#changes-clear').disabled")
    assert _drawers(page)["files"] is False
    assert errors == []


def _rows_state(page):
    return page.evaluate(
        """() => ({
            rows: [...document.querySelectorAll('#changes-list .chg-row')].map(r => [
                r.dataset.path, r.querySelector('.chg-caret').textContent,
                r.classList.contains('sel'), r.getAttribute('aria-expanded')]),
            diffHidden: document.querySelector('#changes-diff').hidden,
            diff: document.querySelector('#changes-diff').textContent,
        })"""
    )


def test_selected_row_collapses_on_second_click(stack, page, ws):
    _edit(ws)
    errors = _open(page, stack)
    page.wait_for_function(f"() => {_BADGE_EXPR} === '3'", timeout=8000)
    page.click("#changes-badge")
    page.wait_for_selector("#changes-drawer.open .chg-row", timeout=8000)
    st = _rows_state(page)
    assert [r[1:] for r in st["rows"]] == [["▸", False, "false"]] * 3
    assert st["diffHidden"] is True

    page.click('.chg-row[data-path="inventory.py"]')
    page.wait_for_selector("#changes-diff:not([hidden]) .rich-green", timeout=8000)
    st = _rows_state(page)
    assert st["rows"][0] == ["inventory.py", "▾", True, "true"]
    assert all(r[1:] == ["▸", False, "false"] for r in st["rows"][1:])

    # 같은 행을 다시 누르면 접힌다 — diff 닫힘·선택 없음
    page.click('.chg-row[data-path="inventory.py"]')
    st = _rows_state(page)
    assert st["diffHidden"] is True and st["diff"] == ""
    assert [r[1:] for r in st["rows"]] == [["▸", False, "false"]] * 3
    # 닫힌 뒤에도 늦게 온 diff 응답이 다시 열지 않는다
    page.wait_for_timeout(400)
    assert _rows_state(page)["diffHidden"] is True

    # 다른 행 → 그 diff
    page.click('.chg-row[data-path="tests/test_stock.py"]')
    page.wait_for_function(
        "() => /\\+\\+\\+ b\\/tests\\/test_stock\\.py/.test("
        "document.querySelector('#changes-diff').textContent)",
        timeout=8000,
    )
    st = _rows_state(page)
    assert [r[1] for r in st["rows"]] == ["▸", "▸", "▾"]
    assert errors == []


@pytest.fixture
def tree_ws(tmp_path, stack):
    """트리 표시용 — src/a.py 수정, src/sub/new.txt 추가, old.csv 삭제(기준점 뒤)."""
    w = tmp_path / "tws"
    (w / "src").mkdir(parents=True)
    (w / "docs").mkdir()
    (w / "src" / "a.py").write_text("a = 1\n")
    (w / "src" / "keep.py").write_text("k = 1\n")
    (w / "docs" / "readme.md").write_text("r\n")
    (w / "old.csv").write_text("x,y\n")
    (w / "top.txt").write_text("t\n")
    reset_snapshot()
    store = get_snapshot(w, create=True, session_id="sess-tree")
    assert store.enabled, store.reason
    stack.server.workspace = w.resolve()
    (w / "src" / "a.py").write_text("a = 2\n")
    (w / "src" / "sub").mkdir()
    (w / "src" / "sub" / "new.txt").write_text("n\n")
    (w / "old.csv").unlink()
    yield w
    reset_snapshot()


# 트리 행마다 [rel, 표시 글자, 툴팁] — 표시가 없으면 글자 ""
_TREE = """() => [...document.querySelectorAll('#dl-tree .dl-row')].map(r => {
    const m = r.querySelector(':scope > .chg-mark');
    return [r.dataset.rel, m ? m.textContent : '', m ? m.title : ''];
})"""


def _tree(page):
    return {rel: (mark, tip) for rel, mark, tip in page.evaluate(_TREE)}


def _expand(page, rel):
    page.click(f'#dl-tree .dl-row[data-rel="{rel}"] > .dl-toggle')


def test_workspace_tree_marks(stack, page, tree_ws):
    errors = _open(page, stack)
    page.wait_for_function(f"() => {_BADGE_EXPR} === '3'", timeout=8000)
    page.click("#files-btn", position={"x": 6, "y": 14})
    page.wait_for_selector('#dl-tree .dl-row[data-rel="src"]', timeout=8000)
    t = _tree(page)
    dot = ("●", "Contains changed files")
    assert t[""] == dot and t["src"] == dot  # 루트·접힌 src
    assert t["docs"] == ("", "") and t["top.txt"] == ("", "")
    assert "old.csv" not in t  # 삭제된 파일은 트리에 없다 — 루트 점으로만

    _expand(page, "src")
    page.wait_for_selector('#dl-tree .dl-row[data-rel="src/a.py"] > .chg-mark')
    t = _tree(page)
    assert t["src/a.py"][0] == "M"
    assert t["src/a.py"][1].startswith("Modified since session start · ")
    assert t["src/sub"] == dot and t["src/keep.py"] == ("", "")
    _expand(page, "src/sub")
    page.wait_for_selector('#dl-tree .dl-row[data-rel="src/sub/new.txt"] > .chg-mark')
    t = _tree(page)
    assert t["src/sub/new.txt"][0] == "A"
    assert t["src/sub/new.txt"][1].startswith("Added since session start · ")
    marked = sorted(rel for rel, (m, _) in t.items() if m)
    assert marked == ["", "src", "src/a.py", "src/sub", "src/sub/new.txt"]
    # 글자는 패널과 같은 색 클래스
    cls = page.get_attribute('.dl-row[data-rel="src/sub/new.txt"] > .chg-mark', "class")
    assert "chg-st-A" in cls.split()

    # 파일 글자 → 그 파일 diff 를 연 Changes 패널, 행 동작(체크·펼침·업로드 대상)은 그대로
    page.check('.dl-row[data-rel="src/a.py"] > input[type=checkbox]')
    before = page.evaluate(
        """() => ({
            target: document.querySelector('#ul-target').textContent,
            targetRow: document.querySelector('#dl-tree .dl-row.target').dataset.rel,
            count: document.querySelector('#dl-count').textContent,
            checks: [...document.querySelectorAll('#dl-tree .dl-row > input')].map(c => c.checked),
            toggles: [...document.querySelectorAll('#dl-tree .dl-toggle')].map(x => x.textContent),
        })"""
    )
    page.click('.dl-row[data-rel="src/a.py"] > .chg-mark')
    page.wait_for_selector("#changes-drawer.open", timeout=8000)
    page.wait_for_function(
        "() => /\\+\\+\\+ b\\/src\\/a\\.py/.test("
        "document.querySelector('#changes-diff').textContent)",
        timeout=8000,
    )
    st = _rows_state(page)
    assert [r[0] for r in st["rows"] if r[2]] == ["src/a.py"]
    after = page.evaluate(
        """() => ({
            target: document.querySelector('#ul-target').textContent,
            targetRow: document.querySelector('#dl-tree .dl-row.target').dataset.rel,
            count: document.querySelector('#dl-count').textContent,
            checks: [...document.querySelectorAll('#dl-tree .dl-row > input')].map(c => c.checked),
            toggles: [...document.querySelectorAll('#dl-tree .dl-toggle')].map(x => x.textContent),
        })"""
    )
    assert after == before and before["targetRow"] == ""
    # Workspace 드로어는 그대로 — 패널을 닫으면 보던 트리로 돌아온다
    assert _drawers(page) == {"changes": True, "files": True}

    # Clear → 트리 표시 모두 사라짐, Undo → 돌아옴
    page.click("#changes-clear")
    page.wait_for_selector("#changes-toast:not([hidden])", timeout=8000)
    assert page.locator("#dl-tree .chg-mark").count() == 0
    page.click("#changes-toast .chg-undo")
    page.wait_for_function(f"() => {_BADGE_EXPR} === '3'", timeout=8000)
    assert sorted(r for r, (m, _) in _tree(page).items() if m) == marked

    # 폴더 점 → 패널만 (선택 없음)
    page.click("#changes-close")
    page.wait_for_function(
        "() => !document.querySelector('#changes-drawer').classList.contains('open')"
    )
    page.wait_for_timeout(300)
    page.click('.dl-row[data-rel="src/sub"] > .chg-mark')
    page.wait_for_selector("#changes-drawer.open .chg-row", timeout=8000)
    page.wait_for_timeout(300)
    st = _rows_state(page)
    assert st["diffHidden"] is True and not any(r[2] for r in st["rows"])
    assert (
        page.evaluate("() => document.querySelector('#ul-target').textContent")
        == (before["target"])
    )
    assert errors == []


def test_workspace_tree_marks_follow_recount(stack, page, tree_ws):
    """목록이 바뀌면(런 끝 갱신) 이미 그려진 트리를 지우고 다시 꾸민다."""
    errors = _open(page, stack)
    page.wait_for_function(f"() => {_BADGE_EXPR} === '3'", timeout=8000)
    page.click("#files-btn", position={"x": 6, "y": 14})
    page.wait_for_selector('#dl-tree .dl-row[data-rel="docs"]', timeout=8000)
    assert _tree(page)["docs"] == ("", "")
    r = stack.renderer
    r.worker_busy()
    (tree_ws / "docs" / "readme.md").write_text("changed\n")
    r.worker_idle()
    page.wait_for_function(f"() => {_BADGE_EXPR} === '4'", timeout=8000)
    assert _tree(page)["docs"] == ("●", "Contains changed files")
    # 마크는 한 행에 하나 — 다시 꾸밀 때 붙였던 것을 지운다
    assert page.locator('.dl-row[data-rel=""] > .chg-mark').count() == 1
    assert errors == []


def test_snapshots_off_no_tree_marks(stack, page, tmp_path):
    reset_snapshot()
    w = tmp_path / "off"
    (w / "src").mkdir(parents=True)
    (w / "src" / "a.py").write_text("a\n")
    stack.server.workspace = w.resolve()
    errors = _open(page, stack)
    page.click("#files-btn", position={"x": 6, "y": 14})
    page.wait_for_selector('#dl-tree .dl-row[data-rel="src"]', timeout=8000)
    page.wait_for_timeout(400)
    assert page.locator("#dl-tree .chg-mark").count() == 0
    assert errors == []
