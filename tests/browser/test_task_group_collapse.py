"""delegate 그룹 카드 접기 UX (기능②) — sticky 헤더 + 본문 클릭.

레이아웃/스크롤 부류라 실브라우저 층이 유일한 가드. 카드 DOM 은
delegate 이벤트(begin_scope→observation…)로 실렌더러가 만든다.
"""

from __future__ import annotations

import time


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def _build_tall_task_group(stack, page):
    """실 렌더러로 긴 본문의 delegate 그룹 카드 하나 생성 후 그 헤더를
    반환. begin_scope 로 그룹을 열고 observation 여러 개로 본문을
    뷰포트보다 길게 채운다."""
    r = stack.renderer
    # begin_scope 는 호출 스레드를 task 에 등록 — 이후 같은 스레드의
    # observation 이 그 그룹 카드로 라우팅된다(_thread_to_task).
    r.begin_scope(task_id="t1", kind="run", index=0, agent="explorer", label="dig in")
    for i in range(30):
        r.observation(
            f"관찰 {i}: " + ("긴 내용 " * 12),
            turn=1,
            tool_name="read_file",
            success=True,
        )
    # v8.12.0 부터 기본 뷰가 개요라 타임라인(#messages)은 닫힌 전문 드로어 안에 있다 —
    # 전문 탭으로 드로어를 열어 카드를 노출시킨다(카드 자체는 뷰와 무관하게 #messages 에
    # 추가되지만, 접기/스크롤 상호작용을 보려면 보여야 한다).
    page.click("#vt-detail-toggle")
    page.wait_for_selector(".card-task-group .task-header", timeout=8000)


class TestTaskGroupCollapse:
    def test_header_toggles_and_body_padding_click_collapses(self, stack, page):
        page.goto(stack.url)
        _build_tall_task_group(stack, page)
        header = page.locator(".card-task-group .task-header")
        body = page.locator(".card-task-group .task-body")
        # 기본 접힘 → 헤더 클릭으로 펼침
        assert body.is_hidden()
        header.click()
        assert _wait(lambda: body.is_visible())
        # 본문 자체(패딩) 클릭으로 접힘 — 좌상단 모서리(중첩 카드 아님)
        box = body.bounding_box()
        page.mouse.click(box["x"] + 3, box["y"] + 3)
        assert _wait(lambda: body.is_hidden())

    def test_nested_card_click_does_not_collapse(self, stack, page):
        page.goto(stack.url)
        _build_tall_task_group(stack, page)
        page.click(".card-task-group .task-header")
        body = page.locator(".card-task-group .task-body")
        assert _wait(lambda: body.is_visible())
        # 중첩 관찰 카드 클릭은 접기를 유발하지 않는다(텍스트 읽기/선택 보호)
        page.click(".card-task-group .task-body .card >> nth=0")
        page.wait_for_timeout(400)
        assert body.is_visible()

    def test_header_is_sticky_while_scrolling_long_body(self, stack, page):
        page.goto(stack.url)
        _build_tall_task_group(stack, page)
        page.click(".card-task-group .task-header")
        page.locator(".card-task-group .task-body").wait_for(state="visible")
        # #messages(스크롤 컨테이너) 를 본문 끝까지 내려도 헤더가 컨테이너
        # 상단에 붙어 뷰포트 안에 남는다(sticky) — 없으면 위로 밀려 나감.
        rect = (
            "() => { const m = document.querySelector('#messages');"
            " const mr = m.getBoundingClientRect();"
            " const h = document.querySelector('.card-task-group .task-header');"
            " const hr = h.getBoundingClientRect();"
            " return {rel: hr.top - mr.top, top: hr.top, h: hr.height,"
            " mtop: mr.top, mbot: mr.bottom}; }"
        )
        page.eval_on_selector("#messages", "m => m.scrollTo(0, m.scrollHeight)")
        page.wait_for_timeout(300)
        pos = page.evaluate(rect)
        # 헤더가 컨테이너 상단(rel≈padding 16px)에 고정 — 밖으로 안 밀림.
        # 없으면 본문 끝 스크롤 시 rel 이 큰 음수(헤더가 위로 사라짐).
        assert -2 <= pos["rel"] <= 20, pos
        # 그리고 실제 뷰포트 안에 보인다
        assert pos["mtop"] <= pos["top"] < pos["mbot"], pos
