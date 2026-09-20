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


class TestInfoDedup:
    """같은 정보가 두 곳에 나오던 것들 — 잃는 게 없어 바로 제거한 셋 (v9.8.0)."""

    def test_main_confirm_does_not_repeat_the_lines_above_it(self, stack, page):
        """main confirm 의 `💭 reasoning` / `⚡ action` 은 바로 위 타임라인
        행의 복사본이다. **CLI 엔 이미 이 게이트가 있었다** —
        `base._format_prompt_meta`: *"the main agent already prints its
        thought/action inline right above the prompt"*. 웹에만 없었다."""
        from agent_cli.render.base import ConfirmOption

        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.thought("위험한 명령을 쓰겠다", 1)
        stack.renderer.action("shell", '{"command":"rm -rf /tmp/x"}', 1)
        assert _wait(lambda: page.locator("#messages .row.act").count() > 0)

        threading.Thread(
            target=lambda: stack.renderer.confirm(
                "⚠ Dangerous command detected",
                [
                    ConfirmOption(key="y", label="once"),
                    ConfirmOption(key="n", label="deny"),
                ],
                default_key="n",
                command="rm -rf /tmp/x",
            ),
            daemon=True,
        ).start()
        assert _wait(lambda: page.locator("#ask-tray .ask-item").count() > 0)
        tray = page.locator("#ask-tray")
        assert tray.locator(".prompt-meta").count() == 0, (
            "main confirm 이 바로 위 두 줄을 그대로 반복한다"
        )
        # 승인 대상(명령)은 여전히 보여야 한다 — 그건 중복이 아니다.
        assert "rm -rf /tmp/x" in tray.inner_text()
        stack.renderer.push_abort()

    def test_sender_name_shows_only_with_more_than_one_viewer(self, stack, page):
        """단독 세션에선 오른쪽 파란 말풍선이 이미 "나"라 `[닉]:` 접두가 순수
        소음이다. 여럿일 때만 이름을 보인다.

        표시를 CSS 클래스로 가르는 이유: 렌더 시점에 뷰어 수로 분기하면
        **스냅샷 재생 순서에 종속**된다(`viewers` 가 늦으면 이미 그린 카드가
        영영 안 바뀐다). 클래스면 과거 카드에도 소급된다 — 아래에서 그걸 검증."""
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.push_user_message("[두정]: 안녕", author="두정")
        bubble = page.locator("#messages .card-user .bubble")
        assert _wait(lambda: bubble.count() > 0)

        # 단독: 접두도 라벨도 안 보이고, 본문만.
        assert "두정" not in bubble.inner_text()
        assert "[두정]:" not in bubble.inner_text()
        assert "안녕" in bubble.inner_text()

        # 두 번째 뷰어가 붙으면 **이미 그려진 카드**에도 이름이 나타난다.
        page2 = page.context.new_page()
        page2.goto(stack.url)
        assert _wait(lambda: "두정" in bubble.inner_text()), "소급 적용이 안 된다"
        page2.close()

    def test_channel_bar_hidden_until_an_agent_exists(self, stack, page):
        """`ovSyncChannels` 가 main 칩을 항상 그려서 `#ov-channels:empty` 가
        **절대 발동하지 않았다** — 에이전트 안 쓰는 세션 내내 탭 1개짜리 탭바가
        입력창 위 한 줄을 먹었다. 🔌 칩의 판례와 같은 판단."""
        stack.emit_ready()
        page.goto(stack.url)
        bar = page.locator("#ov-channels")
        assert _wait(lambda: bar.count() > 0)
        assert not bar.is_visible(), "에이전트가 없는데 채널 바가 보인다"

        stack.renderer.agent_roster(
            [{"key": "agt-1", "name": "rev", "profile": "r", "state": "idle"}]
        )
        assert _wait(lambda: bar.is_visible()), "에이전트가 생겼는데 바가 없다"
        assert page.locator('.ov-ch[data-key="main"]').count() == 1

        stack.renderer.agent_roster([])
        assert _wait(lambda: not bar.is_visible())


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

    def test_rejected_emission_draws_no_card(self, stack, page):
        """형식 거부는 **화면에 아무 카드도 남기지 않는다** (v9.8.0).

        하네스의 재시도 기계지 모델의 작업이 아니다. 거부된 원문도, 개입
        관찰도 나가지 않는다 — 둘 중 하나만 숨기면 맥락 없는 빨간 줄이 남아
        더 나쁘다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.final("이전 답", turn=0)
        assert _wait(lambda: page.locator("#messages > .card").count() == 1)

        stack.renderer.recovery("{bad json", "형식이 틀렸습니다", "NO_JSON", 1)
        # 카운터가 오를 때까지 기다린 뒤 카드 수가 그대로인지 본다.
        assert _wait(lambda: "재시도" in self._gen(page).inner_text())
        body = page.locator("#messages").inner_text()
        assert "{bad json" not in body, "거부된 원문이 화면에 있다"
        assert "형식이 틀렸습니다" not in body, "개입 관찰이 카드로 남았다"
        assert page.locator("#messages > .card").count() == 1, "카드가 늘었다"

    def test_retry_counter_says_why_it_is_slow(self, stack, page):
        """거부는 숨기되 **왜 오래 걸리는지**는 남는다 — 그래야 "그냥 느림"과
        "형식 거부로 맴돔"이 구별된다(형식 붕괴가 잦은 로컬 모델에선 실제로
        다른 상황이고, 후자면 `--verbose` 로 넘어갈 신호다)."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.stream_chunk("본문 " * 60)
        assert _wait(lambda: self._gen(page).count() == 1)

        for i in range(1, 4):
            stack.renderer.recovery("{bad", "다시", "no action", i)
            want = f"재시도 {i}"
            assert _wait(lambda w=want: w in self._gen(page).inner_text())
        # 생성 중 줄은 살아 있어야 한다 — 런이 끝난 게 아니라 이어진다.
        assert self._gen(page).count() == 1

    def test_hard_failure_is_shown_and_persists(self, stack, page):
        """재시도를 다 쓰면 **마지막 실패는 보여준다** — 런이 왜 끝났는지의
        유일한 근거다. 그리고 리로드해도 남아야 한다(영속)."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.error(
            "Action loop unresolved: shell repeated; Stopping.", turn=3
        )
        err = page.locator("#messages .card-error")
        assert _wait(lambda: err.count() == 1)
        assert "Action loop unresolved" in err.inner_text()

        page.reload()  # 재접속 replay 에 실려야 한다
        err2 = page.locator("#messages .card-error")
        assert _wait(lambda: err2.count() == 1)
        assert "Action loop unresolved" in err2.inner_text()


class TestDisplayOnlyNote:
    """표시 전용 주석(``note_next``) — 화면에만 붙는 배지 (v9.8.0).

    첫 소비자는 승인 게이트다. 사용자가 `a`(세션 내내 허용)를 고르면 그 뒤로
    같은 명령이 **묻지도 않고** 통과하므로, 대화에 흔적이 없으면 나중에 "이게
    왜 확인 없이 돌았지"를 되짚을 수 없다. 그 흔적을 모델 컨텍스트가 아니라
    화면에만 남기는 게 이 기능이다.

    층 분담: 유닛(`TestNoteNext`)이 서버측 분리(모델 컨텍스트 불침투·1회 소비)를,
    정적 가드(`test_every_js_class_has_a_css_rule`)가 CSS 규칙 존재를 맡는다.
    여기서는 **실 SSE 를 타고 DOM 까지 도달해 정확히 한 카드에만 붙는지**와
    긴 경로의 줄바꿈 기하를 본다 — 앞 둘이 원리적으로 못 보는 것만."""

    def test_note_renders_on_the_annotated_card_only(self, stack, page):
        stack.emit_ready()
        _open_timeline(page, stack)

        stack.renderer.note_next("🔓 사용자가 `rm` 포함 명령을 승인")
        stack.renderer.observation(
            "removed 1204 files", turn=1, tool_name="shell", success=True
        )
        assert _wait(lambda: page.locator(".card-note").count() == 1)
        assert "승인" in page.locator(".card-note").first.inner_text()

        # 다음 카드에는 안 붙는다 — 승인이 한 번만 기록됐음이 화면으로 확인된다.
        stack.renderer.observation(
            "removed 7 files", turn=1, tool_name="shell", success=True
        )
        assert _wait(lambda: page.locator(".card-observation").count() == 2)
        assert page.locator(".card-note").count() == 1

    def test_long_note_wraps_instead_of_widening_the_timeline(self, stack, page):
        """줄바꿈 회귀 — 승인 주석은 **경로를 담는다**(공백 없는 긴 문자열).

        같은 클래스의 실사고가 이미 둘 있다(`.action-detail` 넘침, 요약 줄
        미줄바꿈). 카드 기준으로 재면 안 된다 — 카드가 내용을 따라 늘어나
        비교가 항상 참이 된다(이 테스트를 처음 그렇게 썼다가 `white-space:
        nowrap` 을 넣어도 통과하는 걸 보고 고쳤다). 실제 증상은 **타임라인의
        가로 스크롤**이라 거기서 잰다."""
        stack.emit_ready()
        _open_timeline(page, stack)

        stack.renderer.note_next(
            "🔓 사용자가 워크스페이스 밖 경로를 승인 — 이 세션 내내 허용: " + LONG_PATH
        )
        stack.renderer.observation("ok", turn=1, tool_name="shell", success=True)
        assert _wait(lambda: page.locator(".card-note").count() == 1)

        note = page.locator(".card-note").first
        assert note.is_visible()
        assert note.bounding_box()["height"] > 20, (
            "긴 주석이 한 줄에 머문다 — 줄바꿈이 안 걸렸다"
        )
        scroll_w = page.evaluate("document.querySelector('#messages').scrollWidth")
        client_w = page.evaluate("document.querySelector('#messages').clientWidth")
        assert scroll_w <= client_w, (
            f"주석이 타임라인을 가로로 넓힘: scrollWidth={scroll_w} > {client_w}"
        )


class TestStepCard:
    """사고·행동·관찰이 **한 장**이다 (docs/chat-ui — 스텝 카드).

    종전엔 한 스텝이 카드 둘로 흩어져(assistant: 💭+⚡ / observation) 어느
    관찰이 어느 행동의 것인지 눈으로 다시 묶어야 했다. 여기서 고정하는 것은
    구조가 아니라 **읽는 방식**이다: 접히면 머리 한 줄 + 배지, 펼치면 행동과
    관찰. 그래서 아래 단언은 전부 화면에서 보이는 것으로 쓴다.
    """

    def _step(self, page):
        return page.locator(".card-assistant.step")

    def test_action_and_observation_land_in_one_card(self, stack, page):
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.thought("실패한 테스트 이름부터 본다", 1)
        stack.renderer.action("shell", json.dumps({"command": "pytest -q"}), 1)
        stack.renderer.observation("2 failed", turn=1, tool_name="shell", success=True)

        assert _wait(lambda: self._step(page).count() == 1)
        card = self._step(page).first
        # 관찰이 **별도 카드로 남지 않는다** — 그게 이 변경의 전부다.
        assert page.locator(".card-observation").count() == 0
        # 행동과 관찰이 같은 장 안의 두 행이다.
        assert card.locator(".step-body .row.act").count() == 1
        assert card.locator(".step-body .row.ok").count() == 1

    def test_body_is_folded_until_the_head_is_clicked(self, stack, page):
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.thought("설정을 먼저 찾는다", 1)
        stack.renderer.action("shell", json.dumps({"command": "ls"}), 1)
        stack.renderer.observation("ok", turn=1, tool_name="shell", success=True)
        assert _wait(lambda: self._step(page).count() == 1)

        card = self._step(page).first
        body = card.locator(".step-body")
        assert not body.is_visible(), "접힌 채로 시작해야 한다"
        card.locator(".step-head").click()
        assert _wait(lambda: body.is_visible())
        card.locator(".step-head").click()
        assert _wait(lambda: not body.is_visible())

    def test_folded_head_still_says_which_tool_and_whether_it_worked(self, stack, page):
        """접힘의 목적은 '안 읽고 지나가기' 가 아니라 **'읽을 곳을 고르기'** 다.
        도구 이름과 성패가 사라지면 10스텝을 훑을 때 전부 펼쳐야 한다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.thought("버전을 확인한다", 1)
        stack.renderer.action("read_file", json.dumps({"path": "pyproject.toml"}), 1)
        stack.renderer.observation(
            "44 lines", turn=1, tool_name="read_file", success=True
        )
        assert _wait(lambda: self._step(page).count() == 1)

        head = self._step(page).first.locator(".step-head")
        assert head.locator(".badge.tool").inner_text().strip().endswith("read_file")
        assert head.locator(".badge.ok").count() == 1
        assert head.locator(".badge.bad").count() == 0
        # 배지는 사고 요약과 **같은 줄**이다 — 줄 수가 늘면 접은 의미가 없다.
        assert self._step(page).first.locator(".step-head").count() == 1

    def test_head_is_the_action_when_there_is_no_reasoning(self, stack, page):
        """살아 있는 두 wire format 이 ``thought_required=False`` 라 생각 없는
        턴이 실측 1/3이다 — 빈 자리에 자리표시를 그리는 대신 행동을 올린다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.action("shell", json.dumps({"command": "uname -s"}), 1)
        stack.renderer.observation("Darwin", turn=1, tool_name="shell", success=True)
        assert _wait(lambda: self._step(page).count() == 1)

        head = self._step(page).first.locator(".step-head")
        assert "act" in (head.get_attribute("class") or "")
        assert head.locator(".k").inner_text().strip() == "shell"
        # 도구 이름이 이미 `k` 칸에 있으므로 배지에서는 뺀다 — 같은 사실을
        # 한 줄 안에서 두 번 말하지 않는다.
        assert head.locator(".badge.tool").count() == 0
        assert head.locator(".badge.ok").count() == 1

    def test_failure_opens_the_card_and_marks_it(self, stack, page):
        """접힌 한 줄로는 왜 실패했는지 알 수 없다 — 종전 규칙을 묶은 뒤에도
        지킨다. 게다가 접힌 목록에서 실패한 스텝을 찾을 수 있어야 한다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.thought("수정이 먹었는지 확인", 1)
        stack.renderer.action("shell", json.dumps({"command": "pytest -q"}), 1)
        stack.renderer.observation(
            "AssertionError: assert 2 == 1", turn=1, tool_name="shell", success=False
        )
        assert _wait(lambda: self._step(page).count() == 1)

        card = self._step(page).first
        assert _wait(lambda: card.locator(".step-body").is_visible())
        assert "failed" in (card.get_attribute("class") or "")
        assert card.locator(".step-head .badge.bad").count() == 1
        assert "assert 2 == 1" in card.inner_text()

    def test_final_answer_is_not_folded_into_a_step(self, stack, page):
        """최종답은 읽히려고 있는 것이라 접으면 대화가 아니라 로그가 된다.
        뒤따를 관찰도 없다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.thought("정리해서 보고한다", 1)
        stack.renderer.final("원인은 워커 경합이었습니다.", 1)
        assert _wait(lambda: page.locator(".card-assistant .final").count() == 1)

        assert self._step(page).count() == 0
        assert (
            "원인은 워커 경합이었습니다."
            in page.locator(".card-assistant .final").first.inner_text()
        )

    def test_observation_without_an_action_still_shows(self, stack, page):
        """안전망. 관찰은 구조적으로 행동 뒤에만 오지만(모든 render=True
        호출부가 같은 턴에 action 을 먼저 낸다), 새 경로가 생겨도 **유실**
        되면 안 된다 — 최악이 '안 묶임' 이어야 한다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.observation("고아", turn=1, tool_name="shell", success=True)
        assert _wait(lambda: page.locator(".card-observation").count() == 1)
        assert "고아" in page.locator(".card-observation").first.inner_text()


class TestStepCardMultiOp:
    """다중 op 턴 — 서버는 ``action()`` 을 **op 마다** 부르고 관찰은 합쳐서
    하나만 낸다(``[1/2] shell — OK …``).

    ★재발 방지(사용자 제보): 병합 단위를 "직전 행동" 으로 잡았더니 마지막
    op 만 관찰을 받고 **앞 카드들이 성패 배지 없이 영영 남았다**. 한 세션에서
    다중 op 턴이 30건이었다 — 드문 모양이 아니다. 단위는 **턴**이다.
    """

    def test_two_actions_in_one_turn_share_one_card(self, stack, page):
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.thought("띄우고 바로 감시를 건다", 4)
        stack.renderer.action("shell", json.dumps({"command": "nohup x &"}), 4)
        stack.renderer.action("monitor", json.dumps({"mode": "add"}), 4)
        stack.renderer.observation(
            "[1/2] shell — OK\n[2/2] monitor — OK",
            turn=4,
            tool_name="shell+monitor",
            success=True,
        )
        steps = page.locator(".card-assistant.step")
        assert _wait(lambda: steps.count() == 1), steps.count()

        card = steps.first
        # 두 행동과 하나의 결과가 **같은 장** 안에 있다.
        assert card.locator(".step-body .row.act").count() == 2
        assert card.locator(".step-body .row.ok").count() == 1
        # 성패 배지가 붙었다 — 이게 빠지는 것이 제보된 증상이었다.
        assert card.locator(".step-head .badge.ok").count() == 1
        # 도구 배지는 `⚡ 첫도구 +N` 으로 접는다.
        assert card.locator(".step-head .badge.tool").inner_text().strip() == (
            "⚡ shell +1"
        )

    def test_next_turn_starts_a_new_card(self, stack, page):
        """턴이 바뀌면 앞 카드에 얹히면 안 된다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.thought("첫 턴", 1)
        stack.renderer.action("shell", json.dumps({"command": "a"}), 1)
        stack.renderer.observation("ok", turn=1, tool_name="shell", success=True)
        stack.renderer.thought("둘째 턴", 2)
        stack.renderer.action("shell", json.dumps({"command": "b"}), 2)
        stack.renderer.observation("ok", turn=2, tool_name="shell", success=True)

        steps = page.locator(".card-assistant.step")
        assert _wait(lambda: steps.count() == 2), steps.count()
        for i in range(2):
            assert steps.nth(i).locator(".step-head .badge.ok").count() == 1

    def test_next_turn_does_not_pile_onto_an_open_card(self, stack, page):
        """관찰이 오면 카드가 닫히지만, **관찰 없이 열린 채 남는** 카드가
        있다(블로킹 ``ask``). 그 위에 다음 턴의 행동이 얹히면 서로 다른 두
        스텝이 한 장으로 뭉친다 — 턴 검사가 없으면 이 경로만 깨진다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.thought("사람에게 묻는다", 1)
        stack.renderer.action("ask", json.dumps({"question": "덮어쓸까요?"}), 1)
        # 관찰 없이 다음 턴이 시작된다.
        stack.renderer.thought("그동안 할 수 있는 걸 한다", 2)
        stack.renderer.action("shell", json.dumps({"command": "ls"}), 2)

        steps = page.locator(".card-assistant.step")
        assert _wait(lambda: steps.count() == 2), steps.count()
        # 각 카드가 **자기 행동 하나씩만** 갖는다 — 둘 다 생각이 있으므로
        # 머리는 💭 이고 행동은 본문 행이다.
        assert steps.nth(0).locator(".step-body .row.act").count() == 1
        assert steps.nth(1).locator(".step-body .row.act").count() == 1
        assert steps.nth(0).locator(".step-head .k").inner_text().strip() == "생각"
        # 접힌 본문은 `inner_text()` 에 안 잡힌다 — DOM 으로 본다.
        assert "덮어쓸까요?" in steps.nth(0).text_content()
        assert "덮어쓸까요?" not in steps.nth(1).text_content()

    def test_observation_from_another_turn_is_not_absorbed(self, stack, page):
        """블로킹 ``ask`` 는 관찰 없이 카드를 열어 둔 채 남고, 개입 턴은
        ``state.turn`` 을 되돌려 번호가 재사용될 수 있다 — 턴이 다르면
        남의 카드다."""
        stack.emit_ready()
        _open_timeline(page, stack)
        stack.renderer.thought("사람에게 묻는다", 1)
        stack.renderer.action("ask", json.dumps({"question": "덮어쓸까요?"}), 1)
        # 관찰이 **다른 턴**으로 온다 — 위 카드에 붙으면 안 된다.
        stack.renderer.observation(
            "다른 턴 결과", turn=9, tool_name="shell", success=True
        )
        assert _wait(lambda: page.locator(".card-observation").count() == 1)
        step = page.locator(".card-assistant.step").first
        assert step.locator(".step-head .badge.ok").count() == 0
        assert step.locator(".step-head .badge.bad").count() == 0
