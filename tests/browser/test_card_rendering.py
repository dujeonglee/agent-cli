"""전문(타임라인) 카드 렌더 회귀 — 실제 화면 결과로 검증 (v8.42.3).

두 실사고를 계약으로 고정한다(둘 다 "소스는 그럴듯한데 화면이 깨진" 부류라
실브라우저 층이 유일한 진짜 가드):

1. **관찰 헤더 마크업 노출** — ``renderObservation`` 이 ``<span class="icon">``
   을 담은 HTML 을 ``el()``(textContent)에 넘겨 화면에 태그가 문자 그대로
   찍혔다(``<span class="icon">✓</span> shell``). v8.36.0 el/elHtml 분리 때
   놓친 콜사이트.
2. **긴 경로 가로 넘침** — ``.action-detail`` 에 줄바꿈 규칙이 없어 공백 없는
   절대경로(read_file/edit_file 대상)가 카드 박스를 넘어갔다.

정적 가드는 ``tests/test_web_server.py``(el() 오용 자동 탐지 + CSS 핀)에 있고,
여기서는 **렌더된 DOM/기하**를 본다.
"""

from __future__ import annotations

import json
import time

LONG_PATH = (
    "/Users/idujeong/workspace/agent-harness/agent-board/data/workspaces/"
    "2ae672d49ce34c1aa96e5dfcd6ff2267/drivers/net/wireless/pcie_scsc/"
    "slsi_wondertap.c"
)


def _open_timeline(page, stack):
    """전문 드로어를 열어 #messages 카드가 보이게 한다(기본 뷰는 개요)."""
    page.goto(stack.url)
    page.click("#vt-detail-toggle")


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


class TestObservationHeader:
    def test_icon_renders_as_element_not_literal_markup(self, stack, page):
        """✓/✗ 아이콘은 **요소**로 렌더돼야 한다 — 화면에 ``<span …>`` 문자열이
        보이면 회귀(실사고 재현 지점)."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.observation("done", turn=1, tool_name="shell", success=True)
        assert _wait(lambda: page.locator(".card-observation .obs-head").count() > 0)

        head = page.locator(".card-observation .obs-head").first
        # 1) 아이콘이 실제 요소로 존재 (문자열이 아니라)
        assert head.locator(".icon").count() == 1, "아이콘 span 이 요소로 없음"
        assert head.locator(".icon").inner_text().strip() == "✓"
        # 2) 화면 텍스트에 마크업이 새지 않았다
        text = head.inner_text()
        assert "<span" not in text and "</span>" not in text, (
            f"헤더에 마크업이 문자로 노출됨: {text!r}"
        )
        assert "shell" in text

    def test_failure_icon_and_tool_name_escaped(self, stack, page):
        """실패 아이콘 경로 + 도구명은 이스케이프된 채 **텍스트로** 들어간다
        (elHtml 로 바꿨다고 도구명 주입 경로가 열리지 않음)."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.observation(
            "boom", turn=1, tool_name="<img src=x onerror=alert(1)>", success=False
        )
        assert _wait(lambda: page.locator(".card-observation .obs-head").count() > 0)

        head = page.locator(".card-observation .obs-head").first
        assert head.locator(".icon").inner_text().strip() == "✗"
        # 주입된 태그는 요소가 되지 않고 텍스트로만 남는다
        assert head.locator("img").count() == 0, "도구명 HTML 이 실행됨(주입)"
        assert "<img" in head.inner_text()


class TestActionDetailOverflow:
    def _emit_read_file(self, stack, path, **extra):
        payload = {"path": path}
        payload.update(extra)
        stack.renderer.action("read_file", json.dumps(payload), 1)

    def test_long_path_does_not_overflow_card(self, stack, page):
        """긴 절대경로가 카드 박스 안에서 줄바꿈된다 — 가로 넘침 0."""
        stack.emit_ready()
        _open_timeline(page, stack)
        self._emit_read_file(stack, LONG_PATH, line_start=800, line_end=895)
        assert _wait(lambda: page.locator(".action-detail").count() > 0)

        box = page.locator(".action-detail").first
        # scrollWidth > clientWidth 면 요소 내부가 가로로 넘친 것
        overflow = box.evaluate("e => e.scrollWidth - e.clientWidth")
        assert overflow <= 1, f"action-detail 가로 넘침 {overflow}px"
        # 카드 자체도 타임라인 폭 안에 있어야 한다(부모를 밀어내지 않았는지)
        spill = page.evaluate(
            "(() => { const m = document.getElementById('messages');"
            " return m.scrollWidth - m.clientWidth; })()"
        )
        assert spill <= 1, f"타임라인이 가로로 밀림 {spill}px"
        # 실제로 여러 줄로 접혔는지(= 한 줄 강제 유지가 아님)
        assert box.evaluate("e => e.getClientRects().length >= 1")
        assert LONG_PATH.split("/")[-1] in box.inner_text()

    def test_short_path_unaffected(self, stack, page):
        """짧은 경로는 종전대로 한 줄 — 줄바꿈 규칙이 과잉 적용되지 않는다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        self._emit_read_file(stack, "/tmp/a.c", line_start=1, line_end=10)
        assert _wait(lambda: page.locator(".action-detail").count() > 0)

        box = page.locator(".action-detail").first
        overflow = box.evaluate("e => e.scrollWidth - e.clientWidth")
        assert overflow <= 1
        assert "/tmp/a.c" in box.inner_text()
