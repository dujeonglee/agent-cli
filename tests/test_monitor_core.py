"""Monitor 코어 — 조건 3종 · 수명 · 폭주 · 보고문 (docs/monitor/DESIGN.md §9).

배선은 없다(커밋 1). 여기서 고정하는 것은 **레지스트리와 조건의 계약**이고,
특히 초판 설계가 틀렸던 자리들이다:

- `exit`/`interval` 은 **없다** — 조건은 셋뿐이다(§4.2)
- 해제 **세 경로 전부** 마지막 보고를 남긴다(초판은 `max_wakes` 만 그랬다)
- `silence` 는 `max(mtime, registered_at)` 기준 — mtime 만 쓰면 등록 즉시 발화
- `match` 커서는 **등록 시점 EOF** 에서 시작 — 0 이면 과거 로그가 통째로 매치
- 로그로테이트(rename)를 `st_ino` 로 본다 — 크기만 보면 한 틱 안에 넘어설 때 놓친다
"""

from __future__ import annotations

import time

import pytest

from agent_cli.monitor import MonitorRegistry, build, known_types
from agent_cli.monitor import registry as reg_mod
from tests.monitor_delivery import RecordingDelivery


@pytest.fixture
def reg():
    r = MonitorRegistry()
    r.deliver = RecordingDelivery()
    r.stop()  # 테스트는 tick() 을 직접 몬다 — 스레드 타이밍에 의존하지 않는다
    yield r
    r.stop()


def _add(reg, spec, **kw):
    kw.setdefault("deadline_s", 3600)
    kw.setdefault("owner", "main")
    return reg.add(build(spec), **kw)


# ── 조건 레지스트리 ─────────────────────────────────────────


class TestConditionRegistry:
    def test_exactly_three_types(self):
        """`exit`(구현 불가)와 `interval`(주기 command 가 상위집합)은 없다."""
        assert known_types() == ["command", "match", "silence"]

    @pytest.mark.parametrize("gone", ["exit", "interval"])
    def test_removed_types_are_rejected_with_a_helpful_error(self, gone):
        with pytest.raises(ValueError, match="unknown condition type"):
            build({"type": gone})

    @pytest.mark.parametrize(
        ("spec", "msg"),
        [
            ({"type": "match", "file": "/tmp/x"}, "pattern"),
            ({"type": "match", "pattern": "E"}, "file"),
            ({"type": "match", "file": "/tmp/x", "pattern": "([="}, "bad regex"),
            ({"type": "silence", "file": "/tmp/x"}, "seconds"),
            ({"type": "silence", "file": "/tmp/x", "seconds": 0}, "positive"),
            ({"type": "command"}, "command"),
            ("not-a-dict", "object"),
        ],
    )
    def test_bad_specs_raise_valueerror(self, spec, msg):
        """**ValueError 로 통일** — 도구가 ToolResult 로 바꾸는데 예외 종류가
        갈리면 변환부가 둘이 된다."""
        with pytest.raises(ValueError, match=msg):
            build(spec)


# ── match ───────────────────────────────────────────────────


class TestMatchCondition:
    def test_cursor_starts_at_eof_not_zero(self, reg, tmp_path):
        """이미 수십 MB 쌓인 로그에 걸면 과거 전체가 한꺼번에 매치된다."""
        log = tmp_path / "a.log"
        log.write_text("ERROR: old one\nERROR: old two\n")
        _add(reg, {"type": "match", "file": str(log), "pattern": "ERROR"})
        reg.tick(time.time())
        assert not reg.deliver.calls, "등록 전의 과거 줄이 매치됐다"

        log.write_text("ERROR: old one\nERROR: old two\nERROR: new\n")
        reg.tick(time.time())
        reports = reg.deliver.take()
        assert len(reports) == 1 and "new" in reports[0]
        assert "old one" not in reports[0]

    def test_missing_file_is_wait_not_death(self, reg, tmp_path):
        """스크립트가 **나중에** 만드는 로그가 정상 경로다."""
        log = tmp_path / "later.log"
        _add(reg, {"type": "match", "file": str(log), "pattern": "GO"})
        reg.tick(time.time())
        assert not reg.deliver.calls

        log.write_text("GO\n")
        reg.tick(time.time())  # 첫 관측 → EOF 고정
        log.write_text("GO\nGO again\n")
        reg.tick(time.time())
        assert reg.deliver.calls

    def test_truncation_resets_the_cursor(self, reg, tmp_path):
        log = tmp_path / "t.log"
        log.write_text("x" * 500 + "\n")
        _add(reg, {"type": "match", "file": str(log), "pattern": "HIT"})
        reg.tick(time.time())
        log.write_text("HIT\n")  # 축소 — 오프셋보다 작아졌다
        reg.tick(time.time())
        assert "HIT" in "".join(reg.deliver.take())

    def test_rotation_by_rename_is_caught_via_inode(self, reg, tmp_path):
        """크기만 보면 새 파일이 한 틱 안에 옛 오프셋을 넘어설 때 **줄을 통째로
        건너뛴다**. `st_ino` 는 필드 하나다."""
        log = tmp_path / "r.log"
        log.write_text("a" * 100 + "\n")
        _add(reg, {"type": "match", "file": str(log), "pattern": "HIT"})
        reg.tick(time.time())

        log.rename(tmp_path / "r.log.1")  # logrotate 기본 동작
        log.write_text("b" * 80 + "\nHIT here\n" + "c" * 80 + "\n")  # 옛 오프셋 초과
        reg.tick(time.time())
        assert "HIT here" in "".join(reg.deliver.take()), "rename 로테이션을 놓쳤다"

    def test_only_matching_lines_are_reported(self, reg, tmp_path):
        log = tmp_path / "m.log"
        log.write_text("")
        _add(reg, {"type": "match", "file": str(log), "pattern": "ERROR"}, once=False)
        reg.tick(time.time())
        log.write_text("info\nERROR: x\ndebug\n")
        reg.tick(time.time())
        out = "".join(reg.deliver.take())
        assert "ERROR: x" in out and "debug" not in out


# ── silence ─────────────────────────────────────────────────


class TestSilenceCondition:
    def test_does_not_fire_immediately_on_a_stale_or_absent_file(self, reg, tmp_path):
        """mtime 만 기준으로 하면 오래된(또는 아직 없는) 로그에 거는 순간
        **즉시 발화**한다 — `max(mtime, registered_at)` 이어야 한다."""
        old = tmp_path / "old.log"
        old.write_text("x\n")
        import os

        os.utime(old, (time.time() - 9999, time.time() - 9999))

        now = time.time()
        _add(reg, {"type": "silence", "file": str(old), "seconds": 60})
        _add(
            reg, {"type": "silence", "file": str(tmp_path / "none.log"), "seconds": 60}
        )
        reg.tick(now)
        assert not reg.deliver.calls, "등록 직후 즉시 발화했다"

    def test_fires_after_the_quiet_window(self, reg, tmp_path):
        log = tmp_path / "q.log"
        log.write_text("x\n")
        mon = _add(reg, {"type": "silence", "file": str(log), "seconds": 60})
        reg.tick(time.time())
        assert not reg.deliver.calls
        reg.tick(time.time() + 61)
        assert "no change for" in "".join(reg.deliver.take())
        assert reg.get(mon.id).retired  # once=True 기본

    def test_writing_keeps_it_quiet(self, reg, tmp_path):
        """쓰기가 계속되면 침묵 창이 다시 시작된다."""
        import os

        log = tmp_path / "w.log"
        log.write_text("x\n")
        t = time.time()
        _add(reg, {"type": "silence", "file": str(log), "seconds": 60})
        reg.tick(t)
        # t+40 에 쓴 것으로 — t+61 시점에서 조용한 지 21s 뿐이다
        log.write_text("x\ny\n")
        os.utime(log, (t + 40, t + 40))
        reg.tick(t + 61)
        assert not reg.deliver.calls
        reg.tick(t + 101)  # 쓰기 후 61s — 이제 발화
        assert reg.deliver.calls


# ── 주기 command ────────────────────────────────────────────


class TestCommandCondition:
    def test_exit_zero_fires_with_stdout_as_the_body(self, reg):
        _add(reg, {"type": "command", "command": "echo hello", "every": "1m"})
        reg.tick(time.time())
        assert "hello" in "".join(reg.deliver.take())

    def test_nonzero_exit_does_not_fire(self, reg):
        _add(reg, {"type": "command", "command": "exit 3", "every": "1m"})
        reg.tick(time.time())
        assert not reg.deliver.calls

    def test_every_is_honoured_and_clamped_to_the_minimum(self, reg):
        """`every` 하한 60s — 주기 실행은 **변화가 없어도 매 틱 한 턴을
        태운다**(§3.1 이 cron 을 기각한 그 이유)."""
        from agent_cli.constants import MONITOR_INTERVAL_MIN_S

        c = build({"type": "command", "command": "true", "every": "1"})
        assert c.every == MONITOR_INTERVAL_MIN_S

        mon = _add(
            reg, {"type": "command", "command": "echo x", "every": "1m"}, once=False
        )
        t = time.time()
        reg.tick(t)
        reg.deliver.take()
        reg.tick(t + 5)  # 주기 전 — 안 돈다
        assert not reg.deliver.calls
        reg.tick(t + 61)
        assert reg.deliver.calls
        assert reg.get(mon.id).alive

    def test_timeout_is_reported_not_silent(self, reg, monkeypatch):
        monkeypatch.setattr(reg_mod, "MIN_INTERVAL_S", 0)
        from agent_cli.monitor import conditions

        monkeypatch.setattr(conditions, "COMMAND_TIMEOUT_S", 1)
        _add(reg, {"type": "command", "command": "sleep 5", "every": "1m"})
        reg.tick(time.time())
        out = "".join(reg.deliver.take())  # drain 은 1회성 — 두 번 부르면 둘째는 빈다
        assert "did not finish" in out, out


# ── 수명: 해제 세 경로가 전부 보고한다 ──────────────────────


class TestRetirementAlwaysReports:
    """초판은 `max_wakes` 만 알리고 `deadline` 만료는 조용히 끝나게 뒀다 —
    `run` 에선 펌프가 그냥 종료돼 세션이 아무 말 없이 끝난다."""

    def test_once_retirement_is_reported(self, reg, tmp_path):
        log = tmp_path / "o.log"
        log.write_text("")
        mon = _add(reg, {"type": "match", "file": str(log), "pattern": "X"})
        reg.tick(time.time())
        log.write_text("X\n")
        reg.tick(time.time())
        out = "".join(reg.deliver.take())
        assert "retired" in out and reg.get(mon.id).retired

    def test_deadline_expiry_is_reported(self, reg, tmp_path):
        log = tmp_path / "d.log"
        log.write_text("")
        mon = _add(
            reg, {"type": "match", "file": str(log), "pattern": "X"}, deadline_s=60
        )
        reg.tick(time.time())
        assert not reg.deliver.calls
        reg.tick(time.time() + 61)
        out = "".join(reg.deliver.take())
        assert "expired" in out, f"만료가 조용히 지나갔다: {out!r}"
        assert not reg.get(mon.id).alive

    def test_max_wakes_exhaustion_is_reported(self, reg, tmp_path, monkeypatch):
        monkeypatch.setattr(reg_mod, "MAX_WAKES", 3)
        monkeypatch.setattr(reg_mod, "MIN_INTERVAL_S", 0)
        log = tmp_path / "k.log"
        log.write_text("")
        mon = _add(reg, {"type": "match", "file": str(log), "pattern": "X"}, once=False)
        reg.tick(time.time())
        for i in range(5):
            with log.open("a") as f:  # **append** — 덮어쓰면 크기가 같아 커서가
                f.write(f"X {i}\n")  # 안 움직인다(append-only 로그가 전제다)
            reg.tick(time.time() + i)
            if not reg.get(mon.id).alive:
                break
        out = "".join(reg.deliver.take())
        assert "cap" in out and not reg.get(mon.id).alive

    def test_deadline_is_clamped_not_rejected(self, reg, tmp_path):
        """값이 크다고 거부하면 모델이 '얼마가 맞는지' 탐색하느라 턴을 태운다."""
        from agent_cli.constants import MONITOR_DEADLINE_MAX_S, MONITOR_DEADLINE_MIN_S

        log = tmp_path / "c.log"
        log.write_text("")
        spec = {"type": "match", "file": str(log), "pattern": "X"}
        big = reg.add(build(spec), deadline_s=999_999, owner="main")
        small = reg.add(build(spec), deadline_s=1, owner="main")
        assert big.deadline_at - big.created_at == pytest.approx(
            MONITOR_DEADLINE_MAX_S, abs=2
        )
        assert small.deadline_at - small.created_at == pytest.approx(
            MONITOR_DEADLINE_MIN_S, abs=2
        )


# ── 폭주 / 메일박스 / 보고문 ────────────────────────────────


class TestCoalescingAndMailbox:
    def test_matches_within_min_interval_become_one_report(self, reg, tmp_path):
        log = tmp_path / "b.log"
        log.write_text("")
        _add(reg, {"type": "match", "file": str(log), "pattern": "X"}, once=False)
        t = time.time()
        reg.tick(t)
        for i in range(3):
            log.write_text(f"X{i}\n")
            reg.tick(t + i)  # min_interval(30s) 안
        reports = reg.deliver.take()
        assert len(reports) <= 1, f"합쳐지지 않고 {len(reports)}건이 됐다"

    def test_live_monitor_holds_the_run(self, reg, tmp_path):
        """살아 있는 감시가 있으면 런이 끝나면 안 된다.

        C2 이후 보고는 **즉시 배달**되므로 "미배달 보고" 라는 상태가 없다 —
        배달된 뒤의 보존은 받은 쪽(메일박스/inbox)의 생존 판정이 진다.
        여기 남는 것은 아직 발화하지 않은 감시와 **배달 중**뿐이다.
        """
        log = tmp_path / "h.log"
        log.write_text("")
        _add(reg, {"type": "match", "file": str(log), "pattern": "X"})
        assert reg.has_active_work()
        reg.tick(time.time())
        log.write_text("X\n")
        reg.tick(time.time())  # once=True → 발화하고 은퇴
        assert reg.deliver.reports, "발화했는데 배달이 없다"
        assert not reg.has_active_work()

    def test_delivery_in_flight_holds_the_run(self, reg, tmp_path):
        """배달 **중**에는 런이 끝나면 안 된다.

        `run` 부작용은 서브프로세스라 `COMMAND_TIMEOUT_S` 까지 걸린다. 그
        사이 생존 판정이 거짓이 되면 펌프가 보고를 날리며 종료한다 —
        `_retire` 가 `retired` 를 먼저 세우므로 "살아 있는 감시" 로는 이
        구간이 안 잡힌다.
        """
        seen = []

        def slow_deliver(addr, **kw):
            seen.append(reg.has_active_work())
            return ""

        reg.deliver = slow_deliver
        log = tmp_path / "f.log"
        log.write_text("")
        _add(reg, {"type": "match", "file": str(log), "pattern": "X"})
        reg.tick(time.time())
        log.write_text("X\n")
        reg.tick(time.time())
        assert seen == [True], "배달 중인데 유휴로 보였다"
        assert not reg.has_active_work()

    @pytest.mark.parametrize("once", [True, False], ids=["retire", "flush"])
    def test_counter_survives_a_raising_delivery(self, reg, tmp_path, once):
        """배달이 터져도 카운터가 새면 세션이 **영영** 안 끝난다.

        `_loop` 는 `tick` 의 예외를 삼킨다 — 한 번만 새도 `has_active_work`
        가 영구히 참이 되어 `_quiet()` 이 참이 안 된다. 2판 설계의 누수가
        카운터만 바꿔 되살아난 자리다.

        **발화 경로가 둘**이라 둘 다 돈다: `once=True` 는 `_retire`,
        `once=False` 는 `_flush`. 한쪽만 보면 다른 쪽의 `finally` 가 없어도
        통과한다(실제로 그랬다 — 사보타주가 잡았다).
        """

        def boom(addr, **kw):
            raise RuntimeError("배달 실패")

        reg.deliver = boom
        log = tmp_path / "b.log"
        log.write_text("")
        _add(reg, {"type": "match", "file": str(log), "pattern": "X"}, once=once)
        reg.tick(time.time())
        log.write_text("X\n")
        with pytest.raises(RuntimeError):
            reg.tick(time.time())
        # 카운터를 직접 본다 — `once=False` 면 감시가 **살아남는 것이 정상**
        # 이라 `has_active_work()` 로는 누수와 생존이 구별되지 않는다.
        assert reg._inflight == 0, "in-flight 카운터가 샜다"

    def test_drain_is_once(self, reg, tmp_path):
        log = tmp_path / "x.log"
        log.write_text("")
        _add(reg, {"type": "match", "file": str(log), "pattern": "X"})
        reg.tick(time.time())
        log.write_text("X\n")
        reg.tick(time.time())
        assert reg.deliver.take() and reg.deliver.take() == []

    def test_delete_removes_it(self, reg, tmp_path):
        log = tmp_path / "del.log"
        log.write_text("")
        mon = _add(reg, {"type": "match", "file": str(log), "pattern": "X"})
        assert reg.delete(mon.id) and not reg.delete(mon.id)
        assert reg.list_all() == []


class TestReportFormat:
    def _report(self, reg, tmp_path, lines):
        log = tmp_path / "f.log"
        log.write_text("")
        _add(reg, {"type": "match", "file": str(log), "pattern": "X"})
        reg.tick(time.time())
        log.write_text("\n".join(lines) + "\n")
        reg.tick(time.time())
        return reg.deliver.take()[0]

    def test_caps_at_five_lines_with_a_remainder_note(self, reg, tmp_path):
        rep = self._report(reg, tmp_path, [f"X{i}" for i in range(9)])
        assert "… 4 more" in rep
        assert rep.count("X") <= 9  # 머리줄 카운트 포함해도 전부 싣지 않는다

    def test_truncates_long_lines(self, reg, tmp_path):
        rep = self._report(reg, tmp_path, ["X" + "y" * 2000])
        assert max(len(ln) for ln in rep.splitlines()) < 600

    def test_header_carries_id_type_and_elapsed(self, reg, tmp_path):
        rep = self._report(reg, tmp_path, ["X hit"])
        head = rep.splitlines()[0]
        assert "mon-" in head and "match" in head and "ago" in head

    def test_report_stays_far_under_the_oversized_cap(self, reg, tmp_path):
        """5줄 × 500자 ≈ 2.5KB — 과대 출력 캡에 **닿는다면 상한이 고장난 것**."""
        rep = self._report(reg, tmp_path, ["X" + "y" * 3000 for _ in range(50)])
        assert len(rep) < 4000


class TestRunSideEffect:
    def test_run_executes_before_the_report_and_its_result_is_included(
        self, reg, tmp_path
    ):
        """notify 없는 shell 을 표현할 수 없게 만든 것이 설계 의도다 — 새벽
        3시에 명령이 돌았는데 아무도 모르는 상태를 막는다."""
        marker = tmp_path / "ran.txt"
        log = tmp_path / "s.log"
        log.write_text("")
        _add(
            reg,
            {"type": "match", "file": str(log), "pattern": "X"},
            run=f"touch {marker}",
        )
        reg.tick(time.time())
        log.write_text("X\n")
        reg.tick(time.time())
        rep = reg.deliver.take()[0]
        assert marker.exists(), "run 이 실행되지 않았다"
        assert "run" in rep and "exit 0" in rep, f"보고에 실행 흔적이 없다: {rep!r}"
