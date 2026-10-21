"""🌐 정보원 서랍 (v10.57.0) — 체크·추가·삭제·기본으로가 파일과 왕복한다.

실서버 + 헤드리스 크롬: 체크박스 → POST api/web-sources → .agent-cli/web-sources.json
(바꾼 것만) → 다시 그린 목록.
"""

from __future__ import annotations

import json
import time

import pytest

from agent_cli import web_sources


def _wait(cond, timeout=6.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path / ".agent-cli" / web_sources.FILE_NAME


def _open(stack, page):
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#websrc-btn", timeout=8000)
    page.click("#websrc-btn")
    page.wait_for_selector("#websrc-drawer.open", timeout=3000)
    page.wait_for_selector(".websrc-item", timeout=3000)


def _saved(f):
    """저장 파일의 내용 — 폴링 술어라 쓰는 도중의 모양도 견딘다(None)."""
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:
        return None


class TestWebSourcesDrawer:
    def test_defaults_are_all_checked_and_grouped(self, stack, page, proj):
        _open(stack, page)
        n = len(web_sources.entries())
        assert page.locator(".websrc-item input:checked").count() == n
        groups = page.locator(".websrc-group-name").all_inner_texts()
        assert groups[0] == "search" and "wifi" in groups
        assert f"{n}/{n} 켜짐" in page.inner_text("#websrc-total")
        assert not proj.exists()  # 아무것도 안 바꿨다

    def test_uncheck_saves_only_that_change(self, stack, page, proj):
        _open(stack, page)
        page.click(".websrc-item[data-id='vulns.nvd'] input")
        assert _wait(lambda: _saved(proj) == {"disabled": ["vulns.nvd"], "added": []})
        page.wait_for_selector(".websrc-item.off[data-id='vulns.nvd']", timeout=3000)
        assert "nvd.nist.gov" not in web_sources.render()

    def test_add_then_delete_a_source(self, stack, page, proj):
        _open(stack, page)
        page.fill("#websrc-group", "wifi")
        page.fill("#websrc-text", "gerrit.mycorp.com/changes/?q=… — 사내 Gerrit")
        page.click("#websrc-add")
        page.wait_for_selector(".websrc-item[data-id='user-1']", timeout=3000)
        assert _wait(lambda: (_saved(proj) or {}).get("added"))
        assert _saved(proj)["added"][0]["group"] == "wifi"
        assert page.input_value("#websrc-text") == ""
        # wifi 그룹 맨 끝에 붙는다
        last = page.locator(".websrc-group[data-group='wifi'] .websrc-item").last
        assert last.get_attribute("data-id") == "user-1"

        page.click(".websrc-item[data-id='user-1'] .websrc-del")
        assert _wait(lambda: not proj.exists())  # 기본과 같아져 파일이 사라진다
        page.wait_for_selector(
            ".websrc-item[data-id='user-1']", state="detached", timeout=3000
        )

    def test_reset_returns_to_defaults(self, stack, page, proj):
        web_sources.save(["vulns.nvd", "wifi.lore"], [])
        _open(stack, page)
        assert page.locator(".websrc-item.off").count() == 2
        page.click("#websrc-reset")
        assert _wait(lambda: not proj.exists())
        page.wait_for_function(
            "document.querySelectorAll('.websrc-item.off').length === 0", timeout=3000
        )

    def test_bad_input_is_reported_not_saved(self, stack, page, proj):
        _open(stack, page)
        page.fill("#websrc-group", "x" * 12)
        page.fill("#websrc-text", "")
        page.click("#websrc-add")
        assert "둘 다" in page.inner_text("#websrc-status")
        assert not proj.exists()
