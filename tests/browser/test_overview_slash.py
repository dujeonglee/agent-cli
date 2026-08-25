"""슬래시 명령 출력이 개요(요약)에 그대로 뜬다 (v8.43.0).

배경: ``/sh``·``/help``·``/compact``·``/skills``·``@agents`` 는 LLM 루프를
타지 않고 ``observation`` 이벤트 **하나만** 쏜다. 사용자 입력은
``push_user_message`` 로 개요에 남는데 결과는 타임라인에만 갔으므로,
개요에는 요청만 뜨고 답이 안 붙는 상태였다.

계약: 화이트리스트 도구의 관찰만 개요 블록이 되고 · 본문은 **마크다운 없이
그대로**(셸 출력의 `**`/`|`/`#` 보존) · 실패는 ✗ · [전체 대화] 버튼은 없음
(관찰 카드에 nav 앵커가 없어 해소 불가) · 일반 도구 관찰은 종전대로 미표시.
"""

from __future__ import annotations

import time


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _blocks(page):
    return page.locator("#overview .ov-block.resp")


class TestOverviewSlashOutput:
    def test_sh_output_shown_raw(self, stack, page):
        """/sh 출력이 개요에 뜨고, 마크다운으로 변환되지 않는다."""
        r = stack.renderer
        r.header("openai", "Test-Model", max_turns=0)
        page.goto(stack.url)
        page.wait_for_selector("#overview", timeout=8000)

        # 마크다운이 태워지면 뭉개질 문자들 (셸 출력에 흔함)
        raw = "total 12\n**not bold**\n| a | b |\n# not a heading"
        r.observation(raw, turn=0, tool_name="sh", success=True)

        assert _wait(lambda: _blocks(page).count() > 0), "개요에 /sh 블록 미표시"
        blk = _blocks(page).last
        txt = blk.inner_text()
        assert "**not bold**" in txt, "마크다운이 적용돼 원문이 뭉개짐"
        assert "# not a heading" in txt
        assert "| a | b |" in txt
        # 마크다운 요소가 실제로 생성되지 않았는지 (구조로 확인)
        assert blk.locator("strong").count() == 0
        assert blk.locator("table").count() == 0
        assert blk.locator("h1").count() == 0
        # 고정폭 컨테이너 + 배지
        assert blk.locator(".ov-tx.ov-mono").count() == 1
        assert "⚡ sh" in blk.locator(".ov-src").inner_text()

    def test_only_copy_button(self, stack, page):
        """a안: [복사]만 노출, [전체 대화] 점프 버튼 없음."""
        r = stack.renderer
        r.header("openai", "Test-Model", max_turns=0)
        page.goto(stack.url)
        page.wait_for_selector("#overview", timeout=8000)
        r.observation("ok", turn=0, tool_name="sh", success=True)
        assert _wait(lambda: _blocks(page).count() > 0)

        blk = _blocks(page).last
        assert blk.locator(".ov-copy").count() == 1
        assert blk.locator(".ov-open").count() == 0

    def test_failure_shows_fail_badge(self, stack, page):
        """exit code 비0 → ✗ 배지 (개요 블록은 종전 항상 ✓ 였다)."""
        r = stack.renderer
        r.header("openai", "Test-Model", max_turns=0)
        page.goto(stack.url)
        page.wait_for_selector("#overview", timeout=8000)
        r.observation("boom\n[exit code: 1]", turn=0, tool_name="sh", success=False)
        assert _wait(lambda: _blocks(page).count() > 0)

        blk = _blocks(page).last
        assert blk.locator(".ov-st.fail").count() == 1
        assert blk.locator(".ov-st.done").count() == 0

    def test_all_listing_commands_shown(self, stack, page):
        """/help·/compact·/skills·@agents (인자 없는 정보성 명령) 전부 표시."""
        r = stack.renderer
        r.header("openai", "Test-Model", max_turns=0)
        page.goto(stack.url)
        page.wait_for_selector("#overview", timeout=8000)

        for tool, label in (
            ("help", "/help"),
            ("compact", "/compact"),
            ("skills", "/skills"),
            ("agents", "@agents"),
        ):
            r.observation(f"{tool} 결과 본문", turn=0, tool_name=tool, success=True)
        assert _wait(lambda: _blocks(page).count() == 4), (
            f"블록 수 {_blocks(page).count()} != 4"
        )
        labels = page.locator("#overview .ov-block.resp .ov-src").all_inner_texts()
        assert labels == ["/help", "/compact", "/skills", "@agents"], labels

    def test_regular_tool_observation_not_shown(self, stack, page):
        """일반 도구 관찰(read_file 등)과 @agent 런 결과는 개요에 안 들어온다 —
        화이트리스트가 좁게 유지되는지(이중 표시 방지)."""
        r = stack.renderer
        r.header("openai", "Test-Model", max_turns=0)
        page.goto(stack.url)
        page.wait_for_selector("#overview", timeout=8000)

        r.observation("파일 내용", turn=1, tool_name="read_file", success=True)
        r.observation("에이전트 결과", turn=0, tool_name="agent", success=True)
        # 타임라인엔 도착했는데 개요엔 없어야 한다
        page.click("#vt-detail-toggle")
        assert _wait(lambda: page.locator(".card-observation").count() == 2)
        assert _blocks(page).count() == 0, "화이트리스트 밖 관찰이 개요에 표시됨"


class TestOverviewTurnError:
    """런-레벨 실패(turn_error)가 요약에도 뜬다 (v8.44.0).

    실사고: 보드 게시글이 LLM 서버에 없는 모델로 고정돼 **모든 호출이 404**
    였는데, 개요(기본 뷰)에는 요청만 남고 아무 반응이 없어 "쿼리가 안 나간다"
    로 보였다. 서버는 turn_error 를 정상 방출하고 전문 타임라인엔 카드가
    떴지만, 개요가 그 이벤트를 안 받고 있었다.
    """

    def test_llm_failure_shown_in_overview(self, stack, page):
        r = stack.renderer
        r.header("openai", "Test-Model", max_turns=0)
        page.goto(stack.url)
        page.wait_for_selector("#overview", timeout=8000)

        msg = (
            "LLM call failed (model=NoSuchModel, iter=1): 404 Client Error: "
            'Not Found: {"error":{"message":"Model not found."}}'
        )
        r.error(msg, 1)

        assert _wait(lambda: _blocks(page).count() > 0), "개요에 오류 블록 미표시"
        blk = _blocks(page).last
        assert "⚠ 오류" in blk.locator(".ov-src").inner_text()
        assert blk.locator(".ov-st.fail").count() == 1  # ✗ 배지
        txt = blk.inner_text()
        assert "404" in txt and "NoSuchModel" in txt
        # 원문 그대로 — JSON/URL 이 마크다운으로 뭉개지지 않는다
        assert blk.locator(".ov-tx.ov-mono").count() == 1
        assert blk.locator(".ov-open").count() == 0  # 복사만

    def test_timeline_card_still_rendered(self, stack, page):
        """전문 카드는 종전대로 유지 — 개요 추가가 대체가 아니라 병행."""
        r = stack.renderer
        r.header("openai", "Test-Model", max_turns=0)
        page.goto(stack.url)
        page.click("#vt-detail-toggle")
        r.error("boom", 1)
        assert _wait(lambda: page.locator(".card-error").count() == 1)
        assert _blocks(page).count() == 1  # 개요에도

    def test_subagent_error_not_in_overview(self, stack, page):
        """서브에이전트 스코프(task_id) 실패는 개요에 안 올린다 — 그 실패는
        부모에게 관찰로 전달되므로 main 요약을 오염시키면 안 된다."""
        r = stack.renderer
        r.header("openai", "Test-Model", max_turns=0)
        page.goto(stack.url)
        page.wait_for_selector("#overview", timeout=8000)
        r.begin_scope(task_id="t1", kind="run", index=0, agent="explorer", label="dig")
        r.error("서브에이전트 실패", 1)  # 스코프 스레드 → task_id 부착
        time.sleep(1.0)
        assert _blocks(page).count() == 0, "서브에이전트 실패가 개요에 표시됨"
