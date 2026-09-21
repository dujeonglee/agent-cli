"""모델이 읽는 텍스트는 영어다 (하네스 주입 프롬프트 계약).

사람이 보는 표면(콘솔·웹 UI·확인 프롬프트)은 한국어가 **의도**다. 하지만
모델에게 들어가는 것 — 시스템 프롬프트·관찰 레코드·도구 결과·주입 안내 —
은 영어여야 한다. 한 메시지 안에 두 언어가 섞이는 것이 특히 나쁘다.

v9.14.0 직전 실측으로 세 군데가 새고 있었다:

- `_AGENT_BATCH_NOTICE` — 에이전트의 **query 로 그대로** 들어간다
- `_with_human_notice` — 에이전트 출력 끝에 붙어 main 이 읽는다
- **monitor 통째로** — 보고 머리말·은퇴 사유·조건 설명·도구 에러. 모니터가
  v9.11.0 이래 한 번도 제대로 동작한 적이 없어 아무도 못 봤다.

소스를 훑지 않고 **실제로 생산된 문자열**을 본다 — 소스 스캔은 콘솔용
한국어(정상)와 구별을 못 해 250건을 오탐한다.
"""

from __future__ import annotations

import re
import time

import pytest

from agent_cli.monitor.conditions import build
from agent_cli.monitor.registry import MonitorRegistry
from tests.monitor_delivery import RecordingDelivery

HANGUL = re.compile(r"[가-힣]")


def _assert_ascii_ish(text: str, what: str) -> None:
    found = HANGUL.findall(text or "")
    assert not found, f"{what} 에 한글이 섞였다: {text!r}"


@pytest.fixture
def reg():
    r = MonitorRegistry()
    r.stop()
    r.deliver = RecordingDelivery()
    yield r
    r.stop()


class TestMonitorReports:
    """모델이 관찰로 읽는 보고 전체."""

    def _fire(self, reg, tmp_path, spec, *, write=None, **kw):
        kw.setdefault("owner", "main")
        kw.setdefault("deadline_s", 3600)
        mon = reg.add(build(spec), **kw)
        reg.tick(time.time())
        if write is not None:
            (tmp_path / "w.log").write_text(write)
        reg.tick(time.time())
        return mon

    def test_match_report(self, reg, tmp_path):
        log = tmp_path / "w.log"
        log.write_text("")
        self._fire(
            reg,
            tmp_path,
            {"type": "match", "file": str(log), "pattern": "X"},
            write="X boom\n",
        )
        for r in reg.deliver.reports:
            _assert_ascii_ish(r, "match 보고")

    def test_deadline_expiry_report(self, reg, tmp_path):
        log = tmp_path / "w.log"
        log.write_text("")
        mon = reg.add(
            build({"type": "match", "file": str(log), "pattern": "X"}),
            owner="main",
            deadline_s=60,
        )
        mon.deadline_at = time.time() - 1
        reg.tick(time.time())
        assert reg.deliver.reports, "만료 보고가 없다"
        for r in reg.deliver.reports:
            _assert_ascii_ish(r, "deadline 만료 보고")

    def test_silence_report(self, reg, tmp_path):
        import os

        log = tmp_path / "s.log"
        log.write_text("x")
        old = time.time() - 600
        # `silence` 는 `max(mtime, registered_at)` 를 본다 — 파일 시각도
        # 되돌려야 침묵이 성립한다.
        os.utime(log, (old, old))
        mon = reg.add(
            build({"type": "silence", "file": str(log), "seconds": 1}),
            owner="main",
            deadline_s=3600,
        )
        mon.state["registered_at"] = old
        reg.tick(time.time())
        assert reg.deliver.reports, "침묵 보고가 없다"
        for r in reg.deliver.reports:
            _assert_ascii_ish(r, "silence 보고")

    def test_owner_gone_reason_is_english(self, reg, tmp_path):
        log = tmp_path / "w.log"
        log.write_text("")
        mon = reg.add(
            build({"type": "match", "file": str(log), "pattern": "X"}),
            owner="agent:k1",
            deadline_s=3600,
        )
        reg.drop_owner("agent:k1")
        _assert_ascii_ish(mon.retired, "소유자 종료 사유")

    def test_delete_reason_is_english(self, reg, tmp_path):
        log = tmp_path / "w.log"
        log.write_text("")
        mon = reg.add(
            build({"type": "match", "file": str(log), "pattern": "X"}),
            owner="main",
            deadline_s=3600,
        )
        reg.delete(mon.id)
        _assert_ascii_ish(mon.retired, "삭제 사유")

    @pytest.mark.parametrize(
        "spec",
        [
            {"type": "match", "file": "/tmp/x.log", "pattern": "Y"},
            {"type": "silence", "file": "/tmp/x.log", "seconds": 30},
            {"type": "command", "command": "true", "every": "30s"},
        ],
    )
    def test_condition_describe(self, spec):
        """`describe()` 는 보고 머리말에 박힌다 — 모델이 읽는다."""
        _assert_ascii_ish(build(spec).describe(), f"{spec['type']} describe()")


class TestToolErrors:
    """도구 에러는 모델이 읽고 고쳐야 하는 텍스트다."""

    def test_registration_refusals(self, tmp_path):
        from agent_cli.monitor.registry import MonitorUnavailable

        log = tmp_path / "t.log"
        log.write_text("")
        cond = build({"type": "match", "file": str(log), "pattern": "X"})

        bare = MonitorRegistry()
        bare.stop()
        with pytest.raises(MonitorUnavailable) as e1:
            bare.add(cond, owner="main", deadline_s=60)
        _assert_ascii_ish(str(e1.value), "배선 없음 거부")

        closed = MonitorRegistry()
        closed.stop()
        closed.deliver = RecordingDelivery()
        closed.closed = True
        with pytest.raises(MonitorUnavailable) as e2:
            closed.add(cond, owner="main", deadline_s=60)
        _assert_ascii_ish(str(e2.value), "종료 중 거부")


class TestInjectedNotices:
    """프롬프트에 직접 주입되는 안내문."""

    def test_agent_batch_notice(self):
        from agent_cli.subagent.agents_live import _AGENT_BATCH_NOTICE

        _assert_ascii_ish(_AGENT_BATCH_NOTICE, "에이전트 배치 안내")

    def test_owed_reminder(self):
        from agent_cli.subagent.agents_live import _OWED_REMINDER

        _assert_ascii_ish(_OWED_REMINDER, "독촉 안내")

    def test_wake_text(self):
        from agent_cli.subagent.agents_live import MailWaker

        _assert_ascii_ish(MailWaker.WAKE_TEXT, "wake 마커")

    def test_human_question_notice(self, tmp_path):
        """에이전트 출력 끝에 붙어 main 이 읽는다."""
        from agent_cli.subagent.agents_live import Question
        from tests.test_agents_live import make_registry

        reg = make_registry(tmp_path)
        try:
            key, _ = reg.spawn()
            q = Question(
                id="q1", asker=key, target="user", text="which?", delivered_seq=1
            )
            q.asked_seq = 0
            reg._questions[q.id] = q
            tm = reg.get(key)
            out = reg._with_human_notice(tm, 0, "done")
            assert "q1" in out, "사전 조건: 안내가 실제로 붙어야 한다"
            _assert_ascii_ish(out, "미답 질문 안내")
        finally:
            reg.shutdown_all()
