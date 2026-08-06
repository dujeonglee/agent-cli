"""관전(읽기 전용) 모드의 프런트 계약 — 실브라우저.

서버 쪽 403/401 은 유닛(``test_web_server.py::TestSpectatorRouteTable``)이
전수로 잡는다. 여기서 잡는 것은 유닛이 원리적으로 못 보는 쪽이다: 관전
링크로 실제 페이지를 열었을 때 **입력 표면이 사라지고 이유가 표시되는가**,
그리고 같은 세션을 전권 링크로 연 탭은 **아무 영향도 안 받는가**.

실행: ``AGENT_CLI_BROWSER_TESTS=1 pytest tests/browser/`` (기본 스위트 skip).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def _open(page, url):
    page.goto(url)
    page.wait_for_selector("#messages", timeout=10_000)


class TestSpectatorPage:
    def test_watch_link_hides_the_composer_and_says_why(self, page, spectator_stack):
        spectator_stack.emit_ready()
        _open(page, spectator_stack.watch_url)
        # identity.readonly 가 첫 이벤트라 안내는 즉시 떠야 한다.
        page.wait_for_selector("#spectator-note", state="visible", timeout=5_000)
        assert "read-only" in page.inner_text("#spectator-note")
        assert page.locator("#input-area").is_hidden()

    def test_full_token_page_is_untouched(self, page, spectator_stack):
        # 관전 모드를 켰다는 사실 자체가 전권 뷰어의 화면을 바꾸면 안 된다.
        spectator_stack.emit_ready()
        _open(page, spectator_stack.url)
        page.wait_for_selector("#input-area", state="visible", timeout=5_000)
        assert page.locator("#spectator-note").is_hidden()

    def test_spectator_sees_the_transcript(self, page, spectator_stack):
        # 관전이 곧 기능이다 — 스트림은 전권 뷰어와 동일해야 한다.
        spectator_stack.emit_ready()
        spectator_stack.renderer.push_user_message("visible to the watcher")
        _open(page, spectator_stack.watch_url)
        page.wait_for_selector("text=visible to the watcher", timeout=5_000)

    def test_full_token_surfaces_are_removed(self, page, spectator_stack):
        # 403 날 컨트롤을 보여주고 클릭에서 실패시키지 않는다.
        spectator_stack.emit_ready()
        _open(page, spectator_stack.watch_url)
        page.wait_for_selector("#spectator-note", state="visible", timeout=5_000)
        for sel in ("#inspector-btn", "#export-btn", "#files-btn", "#rename-btn"):
            assert page.locator(sel).is_hidden(), sel

    def test_roster_marks_the_spectator(self, page, spectator_stack):
        spectator_stack.emit_ready()
        _open(page, spectator_stack.watch_url)
        page.wait_for_selector("#spectator-note", state="visible", timeout=5_000)
        page.wait_for_function(
            "() => (document.getElementById('viewers')||{}).textContent"
            ".includes('(you)')",
            timeout=5_000,
        )
        # 👁 접두어가 "보고만 있는 사람"을 구분한다.
        assert "👁" in page.inner_text("#viewers")

    def test_nickname_bar_is_not_offered(self, page, spectator_stack):
        # POST /api/nickname 은 변이(403) — 답할 수 없는 요청을 하지 않는다.
        spectator_stack.emit_ready()
        _open(page, spectator_stack.watch_url)
        page.wait_for_selector("#spectator-note", state="visible", timeout=5_000)
        assert page.locator("#name-bar").is_hidden()
