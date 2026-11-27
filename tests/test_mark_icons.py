"""관찰 줄의 ✓/✗ — 그 줄의 레코드 ``success`` 를 그린다, `[interrupt]` 는 중립 (IMPL-mark-icons).

빨간 ✗ 는 도구 호출이 실패했을 때만이다. 정지·재시작의 `[interrupt]` 기록은 사실이지 실패가 아니다 — 레코드의
``success: False`` 는 그대로 두고(읽어서 동작이 갈리는 곳이 없다), 그리는 쪽이 도구 이름으로 중립 ``■`` 를 그린다.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from rich.console import Console

import agent_cli.loop.core as core_mod
import agent_cli.render as render_mod
from agent_cli.constants import INTERRUPT_NOTICE
from agent_cli.render.minimal import MinimalRenderer
from agent_cli.subagent.agent_mail import build_reply_record
from tests.oracle import driver


def _drawn_observations(monkeypatch, tmp_path, scenario, tool=None):
    """시나리오를 실제 조립으로 돌리며 루프(`loop.core`)가 그린 관찰 ``(tool, success, 내용)`` — ``tool`` 이면 그 도구만."""
    drawn: list[tuple[str, bool, str]] = []
    real = core_mod.render_step

    def spy(kind, content, turn, *a, **kw):
        if kind == "observation" and tool in (None, kw.get("tool_name")):
            drawn.append((kw["tool_name"], bool(kw.get("success", True)), content))
        return real(kind, content, turn, *a, **kw)

    monkeypatch.setattr(core_mod, "render_step", spy)
    monkeypatch.chdir(tmp_path)
    scenario(tmp_path)
    return drawn


def _history_files(root: Path) -> list[Path]:
    """``root`` 아래의 history.jsonl — 제품 스냅샷 저장소(``.agent-cli/snapshot.git``)는 훑지 않는다: history 가 거기
    있을 리 없고, 그 안은 git 의 자동 정리가 훑는 사이 디렉터리를 지운다(간헐 FileNotFoundError)."""
    found = []
    for here, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "snapshot.git"]
        if "history.jsonl" in files:
            found.append(Path(here) / "history.jsonl")
    return sorted(found)


def _records_with(tmp_path, content: str) -> list[dict]:
    out = []
    for h in _history_files(tmp_path):
        for line in h.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("content") == content:
                    out.append(r)
    return out


class TestMailLinesDrawTheirRecord:
    def test_the_answer_line_is_drawn_as_its_record(self, monkeypatch, tmp_path):
        """main 에 온 사람의 답 — 레코드가 성공(True)이고 화면도 ✓ 다. 종전엔 우편의 칸(없음 → False)을 읽어 ✗ 였다."""
        drawn = _drawn_observations(
            monkeypatch, tmp_path, driver.s44_main_asks_and_waits, tool="ask"
        )
        assert drawn, "the answer line was not drawn"
        for _tool, shown, content in drawn:
            (rec,) = _records_with(tmp_path, content)
            assert rec["success"] is True
            assert shown is True

    @pytest.mark.parametrize(
        "scenario",
        [
            driver.s02_fallback_reply,  # 런 끝 폴백(성공)
            driver.s13_wait_refusals_and_wakes,  # job
            driver.s23_checkin_main_in_place,  # checkin
            driver.s26_auditor_range,  # harness(감사)
            driver.s34_parent_dies_with_its_run_agent,  # died(False)
            driver.s42_run_crashes_while_waiting,  # 런 끝 폴백(실패, False)
            driver.s44_main_asks_and_waits,  # answer
        ],
        ids=lambda f: f.__name__.split("_")[0],
    )
    def test_every_mail_line_is_drawn_as_its_record(
        self, monkeypatch, tmp_path, scenario
    ):
        """main 이 흡수한 우편의 관찰 줄은 모두 그 레코드의 ``success`` 로 그려진다(IMPL-mark-icons §1.2 의 실측)."""
        drawn = _drawn_observations(monkeypatch, tmp_path, scenario)
        pairs = [
            (tool, shown, rec["success"])
            for tool, shown, content in drawn
            for rec in _records_with(tmp_path, content)[:1]
        ]
        assert pairs
        assert all(shown == recorded for _t, shown, recorded in pairs), pairs


@pytest.mark.parametrize(
    "mail, recorded",
    [
        # 우편은 만드는 자리의 모양 그대로 (IMPL-mark-icons §1.2)
        ({"kind": "answer", "re": "q-1", "output": "Answer: v2", "todo": "x"}, True),
        (
            {"kind": "closed", "re": "q-1", "output": "Your question: x", "todo": "x"},
            True,
        ),
        ({"kind": "monitor", "success": True, "output": "📡 hit"}, True),
        ({"kind": "checkin", "ledger": "none", "success": True, "output": "⏰"}, True),
        ({"kind": "job", "success": False, "output": "🧵 job #1 failed"}, False),
        ({"kind": "harness", "job": "auditor", "success": False, "output": "x"}, False),
        ({"kind": "died", "key": "agt-1", "success": False, "output": "boom"}, False),
        (
            {
                "kind": "peer_message",
                "key": "agt-1",
                "success": True,
                "output": "hi",
                "expects_reply": False,
                "notice": True,
            },
            True,
        ),
        (
            {
                "kind": "reply",
                "key": "agt-1",
                "success": False,
                "output": "x",
                "re": "r-1",
            },
            False,
        ),
    ],
    ids=lambda v: v["kind"] if isinstance(v, dict) else str(v),
)
def test_each_mail_kind_records_its_outcome(mail, recorded):
    """화면이 읽는 값 = 레코드의 값 — 종류마다 레코드가 무엇을 싣는가."""
    assert build_reply_record(mail)["success"] is recorded


def _console():
    buf = io.StringIO()
    return MinimalRenderer(Console(file=buf, width=200)), buf


class TestTerminalInterrupt:
    def test_main_line_is_neutral(self):
        """`[interrupt]` 는 ✓/✗ 도 상태 낱말도 없이 ``■ interrupt`` — 종전 ``✗ interrupt  error``."""
        r, buf = _console()
        r.observation(INTERRUPT_NOTICE, 1, "interrupt", success=False)
        assert "  ■ interrupt\n" in buf.getvalue()
        assert "✗" not in buf.getvalue() and "error" not in buf.getvalue()

    def test_agent_line_is_neutral(self):
        r, buf = _console()
        r.begin_agent_work(key="agt-3", item="r-1", profile="", message="go")
        r.observation(INTERRUPT_NOTICE, 1, "interrupt", success=False)
        assert "  agt-3 │ interrupt  ■" in buf.getvalue()


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
