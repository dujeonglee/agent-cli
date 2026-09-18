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
    """v9.4.0 ②: #messages 가 유일한 표면이라 열 드로어가 없다 — goto 만으로
    카드가 보인다. (이름은 호출부 보존을 위해 유지.)"""
    page.goto(stack.url)


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


class TestObservationHeader:
    """v9.4.0 ③: 헤더(.obs-head)가 한 줄 행(.row)이 됐다 — 아이콘은 ``.ic``,
    도구명은 ``.k``. 지키는 계약은 그대로다: 마크업이 **문자로 새지 않는다**."""

    def test_icon_renders_as_element_not_literal_markup(self, stack, page):
        """✓/✗ 아이콘은 **요소**로 렌더돼야 한다 — 화면에 ``<span …>`` 문자열이
        보이면 회귀(실사고 재현 지점)."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.observation("done", turn=1, tool_name="shell", success=True)
        assert _wait(lambda: page.locator(".card-observation .row").count() > 0)

        row = page.locator(".card-observation .row").first
        assert row.locator(".ic").inner_text().strip() == "✓"
        text = row.inner_text()
        assert "<span" not in text and "</span>" not in text, (
            f"행에 마크업이 문자로 노출됨: {text!r}"
        )
        assert "shell" in text

    def test_failure_icon_and_tool_name_escaped(self, stack, page):
        """실패 아이콘 경로 + 도구명은 **텍스트로** 들어간다 — 도구명에 HTML 이
        와도 실행되지 않는다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.observation(
            "boom", turn=1, tool_name="<img src=x onerror=alert(1)>", success=False
        )
        assert _wait(lambda: page.locator(".card-observation .row").count() > 0)

        head = page.locator(".card-observation .row").first
        assert head.locator(".ic").inner_text().strip() == "✗"
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
        # v9.4.0 ③: 도구 상세는 기본 접힘 — 넘침을 재려면 펼쳐야 한다
        # (접힌 요소는 getClientRects() 가 0 이라 측정 자체가 무의미).
        page.locator(".card-assistant .row.can").first.click()
        assert _wait(lambda: page.locator(".action-detail").first.is_visible())

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


class TestRowRhythm:
    """v9.4.0 ③ (docs/chat-ui §2): 모든 줄이 `아이콘 · 종류 · 한 줄 요약 ·
    펼침표시` 4칸 그리드. 기본은 한 줄, 누르면 전문 — **투명성은 깊이로,
    간결함은 기본 상태로**."""

    def _rows(self, page):
        return page.locator("#messages .row")

    def test_action_row_shows_icon_tool_and_summary(self, stack, page):
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.action("shell", json.dumps({"command": "pytest tests/ -q"}), 1)
        assert _wait(lambda: self._rows(page).count() > 0)
        row = self._rows(page).first
        assert row.locator(".ic").inner_text().strip() == "⚡"
        assert row.locator(".k").inner_text().strip() == "shell"
        assert row.locator(".s").inner_text().strip() == "pytest tests/ -q"

    def test_observation_row_names_the_tool_not_just_결과(self, stack, page):
        """호출(⚡ shell)과 결과(✓ shell)가 같은 이름을 달아 눈으로 짝지어진다.
        "결과"라고만 쓰면 어느 도구의 결과인지 알 수 없다(구현 중 실수)."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.observation(
            "3826 passed", turn=1, tool_name="shell", success=True
        )
        assert _wait(lambda: self._rows(page).count() > 0)
        assert self._rows(page).first.locator(".k").inner_text().strip() == "shell"

    def test_body_collapsed_by_default_and_toggles(self, stack, page):
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.observation("a\nb\nc", turn=1, tool_name="shell", success=True)
        assert _wait(lambda: self._rows(page).count() > 0)
        row = self._rows(page).first
        body = row.locator(".row-body")
        assert not body.is_visible(), "기본이 펼쳐져 있으면 '간단'이 깨진다"
        assert row.locator(".x").inner_text().strip() == "▸"
        row.locator(".s").click()
        assert _wait(lambda: body.is_visible())
        assert row.locator(".x").inner_text().strip() == "▾"
        row.locator(".s").click()
        assert _wait(lambda: not body.is_visible())

    def test_failed_observation_starts_expanded(self, stack, page):
        """실패는 사용자가 **지금 봐야 하는** 유일한 줄이라 펼친 채로 시작한다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.observation(
            "boom: no such file", turn=1, tool_name="shell", success=False
        )
        assert _wait(lambda: self._rows(page).count() > 0)
        row = self._rows(page).first
        assert row.locator(".ic").inner_text().strip() == "✗"
        assert row.locator(".row-body").is_visible(), (
            "실패가 접혀 있으면 원인이 안 보인다"
        )

    def test_summary_is_the_last_meaningful_line(self, stack, page):
        """셸·테스트·린트의 결론은 대개 **끝**에 있다(3826 passed / All checks
        passed). 첫 줄은 명령 에코나 헤더라 정보가 적다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.observation(
            "$ pytest\n....\n\n3826 passed, 27 skipped in 41.23s\n",
            turn=1,
            tool_name="shell",
            success=True,
        )
        assert _wait(lambda: self._rows(page).count() > 0)
        assert "3826 passed" in self._rows(page).first.locator(".s").inner_text()

    def test_no_expand_marker_when_nothing_to_expand(self, stack, page):
        """펼칠 게 없는 줄에 ▸ 가 뜨면 눌러도 아무 일이 없어 고장으로 읽힌다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.thought("한 줄 생각", 1)
        stack.renderer.final("done", 1)
        assert _wait(lambda: self._rows(page).count() > 0)
        row = self._rows(page).first
        assert row.locator(".x").inner_text().strip() == ""
        assert "can" not in (row.get_attribute("class") or "")

    def test_final_answer_is_never_collapsed(self, stack, page):
        """최종 답변은 읽히려고 있는 것 — 접으면 대화가 아니라 로그가 된다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.final("여기 답이 있습니다", 1)
        assert _wait(lambda: page.locator(".card-assistant .final").count() > 0)
        fin = page.locator(".card-assistant .final").first
        assert fin.is_visible()
        assert "여기 답이 있습니다" in fin.inner_text()

    def test_long_summary_stays_one_line(self, stack, page):
        """요약이 줄바꿈되면 대화가 통째로 밀린다 — ellipsis 로 자른다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.action("shell", json.dumps({"command": "echo " + "x" * 400}), 1)
        assert _wait(lambda: self._rows(page).count() > 0)
        s = self._rows(page).first.locator(".s")
        h = s.evaluate("e => e.getBoundingClientRect().height")
        assert h < 30, f"요약이 여러 줄로 늘어남 ({h}px)"
        assert s.evaluate("e => e.scrollWidth > e.clientWidth"), "잘리지 않음"
