"""관찰 줄의 ✓/✗ — 그 줄의 레코드 ``success`` 를 그린다, `[interrupt]` 는 중립 (IMPL-mark-icons).

빨간 ✗ 는 도구 호출이 실패했을 때만이다. 정지·재시작의 `[interrupt]` 기록은 사실이지 실패가 아니다 — 레코드의
``success: False`` 는 그대로 두고(읽어서 동작이 갈리는 곳이 없다), 그리는 쪽이 도구 이름으로 중립 ``■`` 를 그린다.
"""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

import agent_cli.loop.core as core_mod
import agent_cli.render as render_mod
from agent_cli.constants import INTERRUPT_NOTICE
from agent_cli.render.minimal import MinimalRenderer
from tests.oracle import driver


def _drawn_mail_lines(monkeypatch, tmp_path, scenario):
    """시나리오를 실제 조립으로 돌리며 main 의 우편 흡수(`_inject_mail`)가 그린 관찰 ``(tool, success, 내용)``."""
    drawn: list[tuple[str, bool, str]] = []
    real = core_mod.render_step

    def spy(kind, content, turn, *a, **kw):
        if kind == "observation" and kw.get("tool_name") == "ask":
            drawn.append((kw["tool_name"], bool(kw.get("success", True)), content))
        return real(kind, content, turn, *a, **kw)

    monkeypatch.setattr(core_mod, "render_step", spy)
    monkeypatch.chdir(tmp_path)
    scenario(tmp_path)
    return drawn


def _records_with(tmp_path, content: str) -> list[dict]:
    out = []
    for h in tmp_path.rglob("history.jsonl"):
        for line in h.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("content") == content:
                    out.append(r)
    return out


class TestPersonsAnswerLine:
    def test_the_answer_line_is_drawn_unlike_its_record(self, monkeypatch, tmp_path):
        """지금 값 — main 에 온 사람의 답은 레코드가 성공(True)인데 화면은 우편의 칸(없음 → False)을 읽어 ✗ 다."""
        drawn = _drawn_mail_lines(monkeypatch, tmp_path, driver.s44_main_asks_and_waits)
        assert drawn, "the answer line was not drawn"
        for _tool, shown, content in drawn:
            (rec,) = _records_with(tmp_path, content)
            assert rec["success"] is True
            assert shown is False


def _console():
    buf = io.StringIO()
    return MinimalRenderer(Console(file=buf, width=200)), buf


class TestTerminalInterrupt:
    def test_main_line_is_a_failure_mark(self):
        """지금 값 — `[interrupt]` 는 터미널에 ``✗ interrupt  error`` 로 찍힌다."""
        r, buf = _console()
        r.observation(INTERRUPT_NOTICE, 1, "interrupt", success=False)
        assert "  ✗ interrupt  error" in buf.getvalue()

    def test_agent_line_is_a_failure_mark(self):
        r, buf = _console()
        r.begin_agent_work(key="agt-3", item="r-1", profile="", message="go")
        r.observation(INTERRUPT_NOTICE, 1, "interrupt", success=False)
        assert "  agt-3 │ interrupt  ✗" in buf.getvalue()


@pytest.mark.parametrize("success, mark", [(True, "✓"), (False, "✗")])
def test_other_observations_keep_their_mark(success, mark):
    r, buf = _console()
    r.observation("x", 1, "shell", success=success)
    assert f"  {mark} shell  {'success' if success else 'error'}" in buf.getvalue()


@pytest.fixture(autouse=True)
def _keep_the_renderer():
    """오라클 조립(`Session`)이 전역 렌더러를 바꾼다 — 다음 테스트 파일의 화면 출력이 사라지지 않게 되돌린다."""
    prev = render_mod.get_renderer()
    yield
    render_mod.set_renderer(prev)
