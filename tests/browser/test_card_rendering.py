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
import threading
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

    def test_long_summary_wraps_instead_of_being_clipped(self, stack, page):
        """긴 요약은 **줄바꿈된다** (v9.4.0 — 사용자 지적으로 방침 전환).

        ③ 에서는 한 줄 고정(ellipsis)이었다. 그런데 잘린 줄은 폭을 아무리 넓혀도
        다 안 보이고, 생각처럼 **펼칠 본문이 없는 줄은 읽을 방법이 아예 없었다**.
        대화가 밀리는 것보다 못 읽는 게 나쁘다. 공백 없는 긴 문자열도 끊긴다
        (`overflow-wrap: anywhere`) — 안 끊으면 그리드 트랙이 밀려 카드가 넘친다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.action("shell", json.dumps({"command": "echo " + "x" * 400}), 1)
        assert _wait(lambda: self._rows(page).count() > 0)
        s = self._rows(page).first.locator(".s")
        assert s.evaluate("e => e.scrollWidth <= e.clientWidth + 1"), "여전히 잘린다"
        h = s.evaluate("e => e.getBoundingClientRect().height")
        assert h > 30, f"공백 없는 긴 문자열이 줄바꿈되지 않음 ({h}px)"

    def test_timestamp_never_overlaps_row_text(self, stack, page):
        """시각과 글자가 **겹치지 않는다** (사용자 지적).

        코너 배지(`.card-time`)는 absolute 라 그 아래 행의 글자를 덮었다. 행이
        있는 카드는 시각을 **같은 그리드 안**(`.row-time`)으로 들여 겹칠 자리를
        없앤다. 소스로는 안 보이고 기하로만 드러나는 부류."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.observation(
            "결과 " * 200, turn=1, tool_name="shell", success=True
        )
        assert _wait(lambda: self._rows(page).count() > 0)
        row = self._rows(page).first

        # 행이 있는 카드에 코너 배지가 남아 있으면 그 자체가 회귀다.
        assert page.locator(".card-observation .card-time").count() == 0
        summary = row.locator(".s").bounding_box()
        tm = row.locator(".row-time").bounding_box()
        assert summary and tm
        assert summary["x"] + summary["width"] <= tm["x"] + 0.5, (
            f"요약이 시각 밑으로 들어감: s={summary}, time={tm}"
        )


class TestAuditFixes:
    """감사에서 나온 "소스는 멀쩡, 테스트는 초록, 화면은 비어 있음" 부류.

    전부 **렌더된 기하/가시성**으로 본다 — 속성이나 클래스 존재만 보면 이
    부류는 그대로 통과한다(실제로 그렇게 통과하고 있었다)."""

    def test_jump_highlight_is_actually_animated(self, stack, page):
        """`.tv-nav-hl` 에 규칙이 없어 점프해도 **아무것도 안 보였다**.
        기존 TC 는 클래스 부착만 검사해 초록이었다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.final("답", turn=1)
        assert _wait(lambda: page.locator("#messages .card-assistant").count() > 0)
        card = page.locator("#messages .card-assistant").first
        card.evaluate("e => e.classList.add('tv-nav-hl')")
        name = card.evaluate("e => getComputedStyle(e).animationName")
        assert name == "tv-nav-flash", f"하이라이트 애니메이션이 없다: {name!r}"

    def test_hidden_header_chips_are_actually_hidden(self, stack, page):
        """`.hd-chip{display:inline-flex}` 가 UA 의 `[hidden]` 을 이겨서
        빈 칩이 첫 페인트부터 보였다. 형제 클래스들엔 가드가 다 있었다."""
        stack.emit_ready()
        page.goto(stack.url)
        for cid in ("#chip-ws", "#confirm-mode-btn"):
            el = page.locator(cid)
            if el.get_attribute("hidden") is not None:
                assert not el.is_visible(), f"{cid} 가 hidden 인데 보인다"

    def test_unknown_tool_json_wraps_inside_the_card(self, stack, page):
        """`pre.args` 는 규칙이 0개라 UA 기본 `<pre>`(줄바꿈 없음)로 떨어져
        넓은 JSON 이 카드를 가로로 뚫었다. **미지 도구 전부**가 이 경로다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        payload = json.dumps(
            {"what": "wide json", "nested": {"a": "x" * 400, "b": "y" * 400}}
        )
        stack.renderer.action("some_unknown_tool", payload, 1)
        row = page.locator("#messages .row.act")
        assert _wait(lambda: row.count() > 0)
        row.click()  # 펼쳐야 본문이 보인다 (행 전체가 토글)
        body = page.locator("#messages pre.args")
        assert _wait(lambda: body.count() > 0 and body.is_visible())
        assert body.evaluate("e => getComputedStyle(e).whiteSpace") == "pre-wrap"
        # 카드를 가로로 뚫지 않는다.
        assert page.evaluate(
            "() => { const m = document.getElementById('messages');"
            " return m.scrollWidth <= m.clientWidth + 1; }"
        ), "본문이 타임라인을 가로로 넘쳤다"

    def test_status_reaches_the_screen(self, stack, page):
        """`status` 는 리스너가 없어 10곳의 호출이 통째로 버려졌다 —
        HTTP 재시도·컨텍스트 오버플로 재시도가 전부 암전이었다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.status(
            "running", "LLM request failed (ConnectionError) — retrying (2/5)"
        )
        line = page.locator("#messages .card-sys")
        assert _wait(lambda: line.count() > 0)
        assert "retrying (2/5)" in line.first.inner_text()

    def test_confirm_says_what_it_is_asking_about(self, stack, page):
        """워크스페이스 이탈 승인에서 **위반이 위반으로 고지**되어야 한다.
        종전엔 `data.prompt` 를 아무도 안 읽어 헤더+버튼만 남았다."""
        from agent_cli.render.base import ConfirmOption

        stack.emit_ready()
        page.goto(stack.url)
        reason = (
            "\n⚠ edit_file touches path(s) OUTSIDE the workspace\n"
            "  (workspace: /ws):\n  /etc/hosts\n"
        )
        threading.Thread(
            target=lambda: stack.renderer.confirm(
                reason,
                [
                    ConfirmOption(key="y", label="once"),
                    ConfirmOption(key="n", label="deny"),
                ],
                default_key="n",
            ),
            daemon=True,
        ).start()
        why = page.locator("#ask-tray .confirm-why")
        assert _wait(lambda: why.count() > 0)
        text = why.inner_text()
        assert "OUTSIDE the workspace" in text and "/etc/hosts" in text
        stack.renderer.push_abort()


class TestGeneratingIndicator:
    """v9.4.0 ④ (docs/chat-ui §2): 스트리밍을 버리고 **한 줄**만 남긴다 —
    `● 생성 중 · N tokens · 💭 사고 M`. 숫자가 오르면 살아 있다는 뜻이고,
    본문이 흐르지 않으므로 카드가 자라며 화면이 튀지 않는다."""

    def _gen(self, page):
        return page.locator("#messages .gen")

    def test_appears_with_token_count(self, stack, page):
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.stream_chunk("x" * 4000)  # ≈1K tokens
        assert _wait(lambda: self._gen(page).count() == 1)
        txt = self._gen(page).inner_text()
        assert "생성 중" in txt and "tokens" in txt

    def test_body_text_never_reaches_the_page(self, stack, page):
        """버린 것의 핵심 — 본문은 오지 않는다(트래픽·화면 튐 둘 다 해결)."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.stream_chunk("비밀스러운 본문 텍스트")
        assert _wait(lambda: self._gen(page).count() == 1)
        assert "비밀스러운" not in page.inner_text("#messages")

    def test_thinking_tokens_shown_separately(self, stack, page):
        """사고 토큰이 따로 잡히면 러너웨이가 바로 보인다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.thinking_chunk("t" * 8000)
        assert _wait(lambda: self._gen(page).count() == 1)
        assert "💭" in self._gen(page).inner_text()

    def test_disappears_on_stream_end(self, stack, page):
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.stream_chunk("x" * 400)
        assert _wait(lambda: self._gen(page).count() == 1)
        stack.renderer.stream_end()
        assert _wait(lambda: self._gen(page).count() == 0)

    def test_only_one_indicator_regardless_of_tick_count(self, stack, page):
        """틱마다 줄이 생기면 그게 곧 스트리밍이다 — 한 줄이 제자리 갱신."""
        stack.emit_ready()
        _open_timeline(page, stack)
        for _ in range(5):
            stack.renderer._last_stream_emit = 0.0  # 스로틀 무시하고 강제 방출
            stack.renderer.stream_chunk("x" * 400)
        assert _wait(lambda: self._gen(page).count() == 1)
        assert self._gen(page).count() == 1

    def test_failed_emission_still_shows_raw(self, stack, page):
        """거부된 원문은 여전히 봐야 한다 — 모델이 무엇을 뱉었는지."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.recovery("{bad json", "형식이 틀렸습니다", "NO_JSON", 1)
        assert _wait(lambda: page.locator(".card-failed").count() > 0)
        card = page.locator(".card-failed").first
        assert "{bad json" in card.inner_text()
        assert "⚠" in card.inner_text()
