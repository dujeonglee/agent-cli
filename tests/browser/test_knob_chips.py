"""범용 노브 칩 렌더러 (v10.45.0, docs/knobs/DESIGN.md §5.2) — 실브라우저 계약.

칩은 `/api/knobs` 의 상태 한 장에서 나온다: 종류·컨트롤 변형이 입력을 고르고,
배지·설명·잠김 사유·"켜짐" 은 서버가 준 글자다. 종전 칩별 IIFE 일곱을 지우고
렌더러 하나로 바꿨으므로 — 입력 한 번이 소유자(ctx·registry·renderer·auditor·
binding)까지 닿는 왕복, 다른 뷰어 동기화, 잠김, 묶음 배지, 거부 되돌리기 — 를
여기서 잰다. 레이아웃·focus 경합은 레이아웃 엔진 없이는 못 잡는 부류다.
"""

from __future__ import annotations

import json
import time

import pytest


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def _open(page, stack, chip):
    page.set_viewport_size({"width": 1500, "height": 720})
    page.goto(stack.url)
    page.wait_for_selector(f"#knob-{chip}-wrap:not([hidden])", timeout=8000)
    page.click(f"#knob-{chip}-chip")
    assert page.locator(f"#knob-{chip}-pop").is_visible()


def _select(page, knob, value):
    page.select_option(f"#knob-{knob}", value)


class TestEveryKnobBecomesAChip:
    def test_table_drives_the_header(self, stack, page):
        """표의 열 노브가 칩 일곱 + 헤더 토글 하나로 — 묶음(⏳ 한도·시도, 🧠 사고·
        노력)은 한 칩. 새 노브가 표에 들어가면 여기 수가 바뀐다."""
        from agent_cli.knobs import KNOB_GROUPS, KNOBS

        page.set_viewport_size({"width": 1500, "height": 720})
        page.goto(stack.url)
        page.wait_for_selector("#knob-model-wrap:not([hidden])", timeout=8000)
        chips = {k.ui.group or k.name for k in KNOBS if k.ui.placement == "chip"}
        for name in chips:
            page.wait_for_selector(f"#knob-{name}-wrap:not([hidden])", timeout=8000)
            assert page.locator(f"#knob-{name}-chip").is_visible(), name
        for k in KNOBS:
            if k.ui.placement == "header":
                page.wait_for_selector(f"#knob-{k.name}:not([hidden])", timeout=8000)
            else:
                assert page.locator(f"#knob-{k.name}").count() == 1, k.name
        assert page.locator("#knobs .knob").count() == len(chips)
        # 묶음 칩의 아이콘은 묶음 것
        for g, spec in KNOB_GROUPS.items():
            assert spec.icon in page.inner_text(f"#knob-{g}-chip")

    def test_badges_are_the_servers_words(self, stack, page):
        page.set_viewport_size({"width": 1500, "height": 720})
        page.goto(stack.url)
        page.wait_for_selector("#knob-model-wrap:not([hidden])", timeout=8000)
        assert page.inner_text("#knob-model-badge").strip() == "Test-Model-8bit"
        assert page.inner_text("#knob-compaction_ratio-badge").strip() == "100%"
        assert page.inner_text("#knob-max_agents-badge").strip() == "10"
        assert page.inner_text("#knob-stall-badge").strip() == "10m×4"
        assert page.inner_text("#knob-thinking-badge").strip() == "off"
        assert page.inner_text("#knob-grammar-badge").strip() == "강제"
        assert page.inner_text("#knob-auditor-badge").strip() == "끔"
        assert "확인 없이 실행" in page.inner_text("#knob-auto_approve")


class TestInputsReachTheOwners:
    def test_choice_select_reaches_ctx(self, stack, page):
        _open(page, stack, "thinking")
        _select(page, "thinking", "on")
        assert _wait(lambda: stack.ctx.thinking_override.get("enable_thinking") is True)
        _select(page, "reasoning_effort", "high")
        assert _wait(
            lambda: stack.ctx.thinking_override.get("reasoning_effort") == "high"
        )
        assert _wait(lambda: page.inner_text("#knob-thinking-badge").strip() == "high")

    def test_grammar_off_reaches_ctx_and_drops_the_on_colour(self, stack, page):
        _open(page, stack, "grammar")
        assert page.evaluate(
            "() => document.getElementById('knob-grammar-wrap').classList.contains('is-on')"
        )
        _select(page, "grammar", "off")
        assert _wait(lambda: stack.ctx.grammar_override is False)
        assert _wait(lambda: page.inner_text("#knob-grammar-badge").strip() == "끔")
        assert not page.evaluate(
            "() => document.getElementById('knob-grammar-wrap').classList.contains('is-on')"
        )
        assert "싣지 않습니다" in page.inner_text("#knob-grammar-note")

    def test_range_reaches_ctx_in_ratio_not_percent(self, stack, page):
        _open(page, stack, "compaction_ratio")
        page.evaluate(
            "() => { var r=document.getElementById('knob-compaction_ratio');"
            " r.value='85'; r.dispatchEvent(new Event('change', {bubbles:true})); }"
        )
        assert _wait(lambda: abs(stack.ctx.compaction_ratio - 0.85) < 1e-9)
        assert _wait(
            lambda: page.inner_text("#knob-compaction_ratio-badge").strip() == "85%"
        )

    def test_unlimited_checkbox_sends_zero(self, stack, page):
        _open(page, stack, "max_agents")
        page.check("#knob-max_agents-unlimited")
        assert _wait(lambda: stack.server.agent_registry.max_agents == 0)
        assert _wait(lambda: page.inner_text("#knob-max_agents-badge").strip() == "∞")
        assert page.locator("#knob-max_agents").is_disabled()
        page.uncheck("#knob-max_agents-unlimited")
        assert _wait(
            lambda: stack.server.agent_registry.max_agents == 10
        )  # 마지막 유한값
        page.fill("#knob-max_agents", "3")
        page.dispatch_event("#knob-max_agents", "change")
        assert _wait(lambda: stack.server.agent_registry.max_agents == 3)

    def test_bool_select_reaches_the_auditor(self, stack, page):
        _open(page, stack, "auditor")
        _select(page, "auditor", "on")
        assert _wait(lambda: stack.auditor.enabled is True)
        assert _wait(lambda: page.inner_text("#knob-auditor-badge").strip() == "켬")
        assert page.evaluate(
            "() => document.getElementById('knob-auditor-wrap').classList.contains('is-on')"
        )

    def test_header_toggle_reaches_the_renderer(self, stack, page):
        page.goto(stack.url)
        page.wait_for_selector("#knob-auto_approve:not([hidden])", timeout=8000)
        page.click("#knob-auto_approve")
        assert _wait(lambda: stack.renderer.auto_approve is True)
        assert _wait(
            lambda: page.evaluate(
                "() => document.getElementById('knob-auto_approve').classList.contains('on')"
            )
        )
        assert page.get_attribute("#knob-auto_approve", "aria-pressed") == "true"
        page.click("#knob-auto_approve")
        assert _wait(lambda: stack.renderer.auto_approve is False)


class TestModelChip:
    @pytest.fixture
    def models(self, tmp_path, monkeypatch):
        import agent_cli.config as _config
        import agent_cli.dialects as _dialects

        target = tmp_path / "models.json"
        target.write_text(
            json.dumps(
                {
                    "models": {
                        "Test-Model-8bit": {
                            "context_window": 32768,
                            "dialect": "json_fc",
                        },
                        "other-json": {"context_window": 20000, "dialect": "json_fc"},
                        "foreign-xml": {"context_window": 8000, "dialect": "xml_fc"},
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
        monkeypatch.setattr(_config, "_cached_registry", None)
        monkeypatch.setattr(_dialects, "_override", None)

    def test_options_are_the_models_bound_to_this_dialect(self, models, stack, page):
        _open(page, stack, "model")
        opts = page.evaluate(
            "() => Array.from(document.querySelectorAll('#knob-model option')).map(o => o.value)"
        )
        assert opts == ["Test-Model-8bit", "other-json"]  # xml_fc 모델은 없다

    def test_switch_applies_to_the_binding(self, models, stack, page):
        _open(page, stack, "model")
        _select(page, "model", "other-json")
        assert _wait(lambda: stack.binding.model == "other-json")
        assert stack.binding.capabilities.context_window == 20000
        assert _wait(
            lambda: page.inner_text("#knob-model-badge").strip() == "other-json"
        )
        assert "20,000" in page.inner_text("#knob-model-note")


class TestLockedAndGrouped:
    def test_locked_controls_are_disabled_with_the_reason(self, stack, page):
        """모델이 사고 미지원이면 서버가 잠김 사유를 주고, 칩은 비활성 + 배지
        "미지원" — 숨기지 않는다(사실을 말한다)."""
        from agent_cli.providers.capabilities import ModelCapabilities

        stack.binding.capabilities = ModelCapabilities(
            32768, 4096, False, supports_grammar=None
        )
        _open(page, stack, "thinking")
        assert page.locator("#knob-thinking").is_disabled()
        assert page.locator("#knob-reasoning_effort").is_disabled()
        assert page.inner_text("#knob-thinking-badge").strip() == "미지원"
        title = page.get_attribute("#knob-thinking-wrap", "title")
        assert title == "this model does not support thinking"  # 두 멤버, 사유는 한 번
        assert page.evaluate(
            "() => document.getElementById('knob-thinking-wrap').classList.contains('is-disabled')"
        )
        # 📐 미확인(None) — 셀렉트 잠김, 배지 "미확인"
        assert page.locator("#knob-grammar").is_disabled()
        assert page.inner_text("#knob-grammar-badge").strip() == "미확인"

    def test_group_badge_and_note_follow_both_members(self, stack, page):
        _open(page, stack, "stall")
        page.fill("#knob-stall", "0")
        page.dispatch_event("#knob-stall", "change")
        assert _wait(lambda: stack.ctx.stream_idle_timeout_s == 0)
        assert _wait(lambda: page.inner_text("#knob-stall-badge").strip() == "off")
        assert page.locator("#knob-stall_attempts").is_disabled()
        assert "꺼져" in page.inner_text("#knob-stall-note")
        page.fill("#knob-stall", "3")
        page.dispatch_event("#knob-stall", "change")
        assert _wait(lambda: page.inner_text("#knob-stall-badge").strip() == "3m×4")
        assert page.locator("#knob-stall_attempts").is_enabled()
        assert "최대 12분" in page.inner_text("#knob-stall-note")

    def test_one_popup_at_a_time(self, stack, page):
        _open(page, stack, "grammar")
        page.click("#knob-auditor-chip")
        assert page.locator("#knob-auditor-pop").is_visible()
        assert page.locator("#knob-grammar-pop").is_hidden()
        page.keyboard.press("Escape")
        assert _wait(lambda: page.locator("#knob-auditor-pop").is_hidden(), timeout=3)


class TestViewersStayInSync:
    def test_second_viewer_sees_the_change(self, browser, stack):
        a = browser.new_page()
        b = browser.new_page()
        for p in (a, b):
            p.set_viewport_size({"width": 1500, "height": 720})
            p.goto(stack.url)
            p.wait_for_selector("#knob-auditor-wrap:not([hidden])", timeout=8000)
        a.click("#knob-auditor-chip")
        a.select_option("#knob-auditor", "on")
        assert _wait(lambda: b.inner_text("#knob-auditor-badge").strip() == "켬")
        assert b.evaluate("() => document.getElementById('knob-auditor').value") == "on"
        a.close()
        b.close()

    def test_harness_progress_shows_as_auditing(self, stack, page):
        """감사 런이 돌면 렌더러가 🔎 상태 한 장을 다시 받아 싣는다 (note_harness)."""
        page.goto(stack.url)
        page.wait_for_selector("#knob-auditor-wrap:not([hidden])", timeout=8000)
        stack.auditor.in_flight = 1
        stack.renderer.note_harness(1)
        assert _wait(
            lambda: page.inner_text("#knob-auditor-badge").strip() == "감사 중"
        )
        stack.auditor.in_flight = 0
        stack.renderer.note_harness(0)
        assert _wait(lambda: page.inner_text("#knob-auditor-badge").strip() == "끔")

    def test_rejected_value_reverts_on_screen(self, stack, page):
        """서버가 거부하면(미등록 모델) 화면도 마지막 상태로 돌아간다 — 바뀐 척하지 않는다."""
        _open(page, stack, "model")
        page.evaluate(
            "() => { var s=document.getElementById('knob-model');"
            " var o=document.createElement('option'); o.value='nope'; o.textContent='nope';"
            " s.appendChild(o); s.value='nope'; s.dispatchEvent(new Event('change', {bubbles:true})); }"
        )
        page.wait_for_timeout(400)
        assert stack.binding.model == "Test-Model-8bit"
        assert (
            page.evaluate("() => document.getElementById('knob-model').value")
            == "Test-Model-8bit"
        )
        assert page.inner_text("#knob-model-badge").strip() == "Test-Model-8bit"
