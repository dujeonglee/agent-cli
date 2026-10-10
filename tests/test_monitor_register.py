"""`match` 등록 시점 처리 (v10.34.0, docs/monitor/DESIGN.md §4.2) — 파일이 이미
있을 때 "역사냐 사건이냐" 를 하니스가 판정하지 않고 근거를 준다:

- 모델의 마지막 도구 결과 **이후** 수정 + 작은 파일 → 커서 0, 다음 틱 발화
- 그 외 → EOF + 등록 관찰에 mtime·이후/이전·마지막 매칭 줄
- 노브 `AGENT_CLI_MONITOR_REPLAY_MAX_BYTES`: 0 = 항상 정보만, -1 = 항상 발화
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

import pytest

from agent_cli.monitor import MonitorRegistry, build
from agent_cli.monitor.conditions import MatchCondition
from tests.monitor_delivery import RecordingDelivery


def _match(path, pattern="READY") -> MatchCondition:
    return build({"type": "match", "file": str(path), "pattern": pattern})


@pytest.fixture
def reg():
    r = MonitorRegistry()
    r.deliver = RecordingDelivery()
    r.stop()
    yield r
    r.stop()


class TestRegister:
    def test_absent_file_waits_silently(self, tmp_path):
        state, note = _match(tmp_path / "later.txt").register(observed_at=time.time())
        assert state == {} and note == ""

    def test_modified_after_last_observation_and_small_replays(self, reg, tmp_path):
        """cand-4: `done.txt` 가 등록 2초 전에 써졌다 — 모델이 못 본 내용이고
        작으니 전부 새 줄. 커서 0 으로 심어 다음 틱에 정상 발화한다."""
        f = tmp_path / "done.txt"
        observed = time.time() - 10
        f.write_text("READY-7731\n")
        cond = _match(f)
        state, note = cond.register(observed_at=observed)
        assert state["offset"] == 0 and "ino" in state
        assert "after your last tool result" in note and "count as new" in note

        reg.add(cond, owner="main", deadline_s=3600, state=state)
        reg.tick(time.time())
        assert "READY-7731" in "".join(reg.deliver.take()), (
            "심은 커서가 발화하지 않았다"
        )

    def test_modified_before_last_observation_is_history(self, reg, tmp_path):
        """어제 멈춘 로그 — 모델이 이미 봤을 수 있는 내용은 보고하지 않고
        근거(mtime·이전·마지막 매칭 줄)만 준다."""
        f = tmp_path / "build.log"
        f.write_text("info\nERROR old one\nERROR old two\ninfo\n")
        old = time.time() - 3600
        os.utime(f, (old, old))
        cond = _match(f, "ERROR")
        state, note = cond.register(observed_at=time.time())
        assert state == {}
        assert "before your last tool result" in note
        assert "NOT reported" in note
        assert "'ERROR old two'" in note and "not a new event" in note

        reg.add(cond, owner="main", deadline_s=3600, state=state)
        reg.tick(time.time())
        assert not reg.deliver.calls, "옛 줄이 발화했다"

    def test_no_observation_yet_says_before_registration(self, tmp_path):
        f = tmp_path / "x.log"
        f.write_text("READY\n")
        _, note = _match(f).register(observed_at=None)
        assert "before this registration" in note

    def test_no_matching_line_is_said_so(self, tmp_path):
        f = tmp_path / "x.log"
        f.write_text("nothing here\n")
        old = time.time() - 60
        os.utime(f, (old, old))
        _, note = _match(f).register(observed_at=time.time())
        assert "No existing line matches" in note

    def test_large_file_modified_after_observation_gets_info_only(
        self, reg, tmp_path, monkeypatch
    ):
        """누적 로그: mtime 은 방금이지만 앞쪽은 옛것 — 0 부터 읽으면 옛 ERROR
        가 새 사건으로 보고된다. 크기 상한을 넘으면 EOF + 정보."""
        monkeypatch.setenv("AGENT_CLI_MONITOR_REPLAY_MAX_BYTES", "1024")
        f = tmp_path / "big.log"
        f.write_text("ERROR early\n" + ("x" * 80 + "\n") * 40 + "ERROR latest\n")
        assert f.stat().st_size > 1024
        cond = _match(f, "ERROR")
        state, note = cond.register(observed_at=time.time() - 10)
        assert state == {}
        assert "after your last tool result" in note
        assert "'ERROR latest'" in note, "꼬리 스캔이 마지막 매칭 줄을 못 찾았다"
        reg.add(cond, owner="main", deadline_s=3600, state=state)
        reg.tick(time.time())
        assert not reg.deliver.calls

    def test_tail_scan_drops_the_cut_first_line(self, tmp_path, monkeypatch):
        """꼬리를 바이트로 자르면 첫 줄이 반쪽일 수 있다 — 그 줄은 버린다."""
        monkeypatch.setenv("AGENT_CLI_MONITOR_REPLAY_MAX_BYTES", "0")
        f = tmp_path / "t.log"
        f.write_text("READY-cut\n" + "z" * 5000 + "\nplain\n")
        last = _match(f)._last_matching_line(f, f.stat().st_size, 4096)
        assert last is None  # READY 줄은 꼬리 밖, 잘린 z 줄은 버려진다

    @pytest.mark.parametrize(
        ("knob", "expect_replay"),
        [("0", False), ("-1", True), ("", True), ("garbage", True)],
    )
    def test_replay_knob(self, tmp_path, monkeypatch, knob, expect_replay):
        """0 = 항상 정보만, -1 = 크기 무관 발화, 빈/오타 = 기본(64 KB)."""
        if knob:
            monkeypatch.setenv("AGENT_CLI_MONITOR_REPLAY_MAX_BYTES", knob)
        else:
            monkeypatch.delenv("AGENT_CLI_MONITOR_REPLAY_MAX_BYTES", raising=False)
        f = tmp_path / "s.txt"
        f.write_text("READY\n")
        state, _ = _match(f).register(observed_at=time.time() - 10)
        assert (state.get("offset") == 0) is expect_replay

    def test_unlimited_knob_replays_even_a_large_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_MONITOR_REPLAY_MAX_BYTES", "-1")
        f = tmp_path / "big.txt"
        f.write_text("READY\n" + "x" * 200_000)
        state, _ = _match(f).register(observed_at=time.time() - 10)
        assert state.get("offset") == 0

    def test_other_conditions_register_as_no_op(self, tmp_path):
        s = build({"type": "silence", "file": str(tmp_path / "a"), "seconds": 5})
        assert s.register(observed_at=time.time()) == ({}, "")
        c = build({"type": "command", "command": "true", "every": 60})
        assert c.register(observed_at=None) == ({}, "")


class TestKnobParsing:
    def test_values(self, monkeypatch):
        from agent_cli.constants import (
            MONITOR_REPLAY_MAX_BYTES,
            monitor_replay_max_bytes,
        )

        monkeypatch.delenv("AGENT_CLI_MONITOR_REPLAY_MAX_BYTES", raising=False)
        assert monitor_replay_max_bytes() == MONITOR_REPLAY_MAX_BYTES == 65536
        monkeypatch.setenv("AGENT_CLI_MONITOR_REPLAY_MAX_BYTES", "4096")
        assert monitor_replay_max_bytes() == 4096
        monkeypatch.setenv("AGENT_CLI_MONITOR_REPLAY_MAX_BYTES", "-7")
        assert monitor_replay_max_bytes() == -1  # 음수는 전부 무제한
        monkeypatch.setenv("AGENT_CLI_MONITOR_REPLAY_MAX_BYTES", "lots")
        assert monitor_replay_max_bytes() == MONITOR_REPLAY_MAX_BYTES


class TestToolRegistration:
    """`monitor add` 가 등록 근거를 관찰에 붙이고, 초기 커서를 공개 전에 심는다."""

    def _registry(self):
        from agent_cli.monitor.runtime import set_monitor_registry

        r = MonitorRegistry()
        r.deliver = RecordingDelivery()
        r.stop()
        set_monitor_registry(r)
        return r

    def test_add_carries_the_note_and_uses_observed_at(self, tmp_path):
        from agent_cli.monitor.runtime import set_monitor_registry
        from agent_cli.tools.base import RunContext
        from agent_cli.tools.registry import TOOLS

        r = self._registry()
        try:
            f = tmp_path / "done.txt"
            f.write_text("READY-1\n")
            ctx = RunContext(owner="main", observed_at=lambda: time.time() - 10)
            res = TOOLS["monitor"].run(
                {
                    "mode": "add",
                    "when": {"type": "match", "file": str(f), "pattern": "READY"},
                },
                ctx=ctx,
            )
            assert res.success and "do NOT poll" in res.output
            assert "count as new" in res.output
            mon = r.list_all()[0]
            assert mon.state["offset"] == 0, "초기 커서가 공개 전에 심기지 않았다"
            r.tick(time.time())
            assert "READY-1" in "".join(r.deliver.take())
        finally:
            set_monitor_registry(None)
            r.stop()

    def test_add_without_ctx_still_registers(self, tmp_path):
        from agent_cli.monitor.runtime import set_monitor_registry
        from agent_cli.tools.registry import TOOLS

        r = self._registry()
        try:
            f = tmp_path / "x.log"
            f.write_text("READY\n")
            res = TOOLS["monitor"].run(
                {
                    "mode": "add",
                    "when": {"type": "match", "file": str(f), "pattern": "READY"},
                }
            )
            assert res.success and "before this registration" in res.output
        finally:
            set_monitor_registry(None)
            r.stop()

    def test_registry_add_seeds_state_before_publishing(self):
        r = MonitorRegistry()
        r.deliver = RecordingDelivery()
        r.stop()
        try:
            cond = MagicMock(describe=MagicMock(return_value="x"))
            mon = r.add(
                cond, owner="main", deadline_s=3600, state={"offset": 0, "ino": 1}
            )
            assert mon.state["offset"] == 0 and mon.state["ino"] == 1
            assert "registered_at" in mon.state
        finally:
            r.stop()


class TestObservedAtPlumbing:
    def test_bridge_stamps_the_last_tool_result_and_the_run_ctx_reads_it_live(self):
        """RunContext 는 캐시되지만 `observed_at` 은 콜러블이라 브리지의 현재 값을
        본다 — 도구 호출 전엔 None, 뒤엔 그 결과의 시각."""
        from agent_cli.loop import LoopConfig, LoopState, ToolBridge

        bridge = ToolBridge(
            LoopConfig(tools_list=["shell"]), LoopState(), ctx=None, provider=None
        )
        ctx = bridge._run_ctx()
        assert ctx.observed_at() is None
        before = time.time()
        bridge._dispatch_tool_with_hooks("shell", {"command": "echo hi"})
        assert bridge._run_ctx() is ctx  # 캐시는 그대로
        assert ctx.observed_at() is not None and ctx.observed_at() >= before
