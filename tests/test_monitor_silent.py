"""`match` 의 조용함 한도 (v10.47.0, docs/monitor/DESIGN.md §12).

셸 A/B 실측: 끝 줄을 안 쓰는 로그, 죽은 producer 의 로그에 건 `match` 는 deadline
(2시간)까지 run 을 붙잡았다. 파일이 `silent` 동안 안 바뀌면(없는 채로도) 보고를
남기고 물러난다 — 멈춤이 깨우기로. 기본 10분, 도구 인자 `silent` 로 바꾸고 0 은 끔.
"""

from __future__ import annotations

from agent_cli.constants import MONITOR_SILENT_DEFAULT_S
from agent_cli.monitor.conditions import MatchCondition, build
from agent_cli.monitor.registry import MonitorRegistry
from tests.monitor_delivery import RecordingDelivery


def _reg(tmp_path):
    r = MonitorRegistry(session_dir=tmp_path)
    r.deliver = RecordingDelivery()
    return r


class TestSpec:
    def test_default_is_ten_minutes(self):
        c = build({"type": "match", "file": "x.log", "pattern": "^EXIT:"})
        assert (
            isinstance(c, MatchCondition)
            and c.silent_s == MONITOR_SILENT_DEFAULT_S == 600
        )

    def test_custom_and_off(self):
        assert (
            build(
                {"type": "match", "file": "x", "pattern": "a", "silent": "30m"}
            ).silent_s
            == 1800
        )
        assert (
            build(
                {"type": "match", "file": "x", "pattern": "a", "silent": "90"}
            ).silent_s
            == 90
        )
        assert (
            build({"type": "match", "file": "x", "pattern": "a", "silent": 0}).silent_s
            == 0
        )

    def test_bad_value_is_a_spec_error(self):
        import pytest

        with pytest.raises(ValueError, match="silent"):
            build({"type": "match", "file": "x", "pattern": "a", "silent": "soon"})

    def test_tool_passes_silent_into_the_spec(self, tmp_path, monkeypatch):
        from agent_cli.monitor import runtime as mrt
        from agent_cli.tools.monitor_tool import MonitorTool

        reg = _reg(tmp_path)
        monkeypatch.setattr(mrt, "_MAIN", reg)
        r = MonitorTool()._run(
            {
                "mode": "add",
                "when": {"type": "match", "file": str(tmp_path / "l"), "pattern": "x"},
                "silent": "2m",
            }
        )
        assert r.success, r.error
        assert reg.list_all()[0].cond.silent_s == 120
        props = MonitorTool.parameters["properties"]
        assert "silent" in props and "10m" in props["silent"]["description"]
        assert "`silent`" in MonitorTool.description


class TestRetire:
    def test_file_that_never_appears_retires_after_the_limit(self, tmp_path):
        reg = _reg(tmp_path)
        cond = MatchCondition(str(tmp_path / "never.log"), "^EXIT:", silent_s=60)
        mon = reg.add(cond, owner="main", deadline_s=7200)
        t0 = mon.created_at
        reg.tick(t0 + 30)
        assert mon.alive
        reg.tick(t0 + 61)
        assert not mon.alive and "silent for 1m" in mon.retired
        text = reg.deliver.reports[-1]
        assert "never appeared" in text and "Retiring" in text and "`silent`" in text

    def test_unchanged_file_retires_but_a_changing_one_does_not(self, tmp_path):
        reg = _reg(tmp_path)
        log = tmp_path / "run.log"
        log.write_text("start\n")
        cond = MatchCondition(str(log), "^EXIT:", silent_s=60)
        mon = reg.add(cond, owner="main", deadline_s=7200)
        t0 = mon.created_at
        reg.tick(t0 + 1)  # 첫 관측 — EOF 커서
        reg.tick(t0 + 40)
        with log.open("a") as f:
            f.write("progress\n")
        reg.tick(t0 + 41)  # 바뀜 → 시계가 다시 돈다
        reg.tick(t0 + 100)
        assert mon.alive
        reg.tick(t0 + 102)
        assert not mon.alive and "unchanged since" in reg.deliver.reports[-1]

    def test_a_match_still_wins_before_the_limit(self, tmp_path):
        reg = _reg(tmp_path)
        log = tmp_path / "run.log"
        log.write_text("")
        cond = MatchCondition(str(log), "^EXIT:", silent_s=60)
        mon = reg.add(cond, owner="main", deadline_s=7200)
        t0 = mon.created_at
        reg.tick(t0 + 1)
        with log.open("a") as f:
            f.write("EXIT:0\n")
        reg.tick(t0 + 2)
        assert not mon.alive and "fired once" in mon.retired
        assert "EXIT:0" in reg.deliver.reports[-1]

    def test_zero_disables_the_limit(self, tmp_path):
        reg = _reg(tmp_path)
        cond = MatchCondition(str(tmp_path / "never.log"), "^EXIT:", silent_s=0)
        mon = reg.add(cond, owner="main", deadline_s=7200)
        reg.tick(mon.created_at + 3600)
        assert mon.alive

    def test_silence_type_is_untouched(self, tmp_path):
        """`silence` 는 조건 자체다 — 한도와 무관하게 종전대로 발화한다."""
        reg = _reg(tmp_path)
        log = tmp_path / "s.log"
        log.write_text("x\n")
        cond = build({"type": "silence", "file": str(log), "seconds": 30})
        mon = reg.add(cond, owner="main", deadline_s=7200)
        reg.tick(mon.created_at + 31)
        assert not mon.alive and "fired once" in mon.retired

    def test_persisted_description_is_unchanged(self, tmp_path):
        reg = _reg(tmp_path)
        cond = MatchCondition(str(tmp_path / "a.log"), "x", silent_s=600)
        reg.add(cond, owner="main", deadline_s=7200)
        assert cond.describe() == f"match · {tmp_path / 'a.log'}"
        assert (tmp_path / "monitors.json").exists()
