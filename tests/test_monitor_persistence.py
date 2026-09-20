"""Monitor 영속 — **기록하되 부활시키지 않는다** (docs/monitor/DESIGN.md §8).

설계 초판은 `--resume` 시 복원 + 재검증(PID·파일·deadline)이었다. 잘라낸
근거를 테스트로 고정한다 — 특히 **부활하지 않는다**는 쪽이 중요하다:

1. 커서가 낡는다 — 되감으면 옛 매치를 다시 보고하고 건너뛰면 잃는데,
   둘 다 틀렸고 어느 쪽이 맞는지 알 방법이 없다
2. **권한 구멍** — `monitors.json` 이 워크스페이스 안이라 에이전트가
   `write_file` 로 고칠 수 있다. 되살리면 아무도 승인하지 않은 `command`/`run`
   이 실행된다(파일 안의 플래그는 "사람이 답했다"를 증명하지 못한다)
"""

from __future__ import annotations

import json

import pytest

from agent_cli.monitor import build
from agent_cli.monitor.registry import MonitorRegistry, describe_previous


@pytest.fixture
def reg(tmp_path):
    r = MonitorRegistry(session_dir=tmp_path)
    r.stop()
    yield r
    r.stop()


def _add(reg, tmp_path, **kw):
    log = tmp_path / "p.log"
    log.write_text("")
    kw.setdefault("deadline_s", 3600)
    return reg.add(build({"type": "match", "file": str(log), "pattern": "X"}), **kw)


class TestWriteOnlyPersistence:
    def test_add_writes_the_file(self, reg, tmp_path):
        _add(reg, tmp_path)
        data = json.loads((tmp_path / "monitors.json").read_text())
        assert len(data["monitors"]) == 1
        assert data["monitors"][0]["desc"].startswith("match")

    def test_delete_rewrites_it(self, reg, tmp_path):
        mon = _add(reg, tmp_path)
        reg.delete(mon.id)
        data = json.loads((tmp_path / "monitors.json").read_text())
        assert data["monitors"] == []

    def test_retired_monitors_are_not_persisted(self, reg, tmp_path):
        """은퇴한 것까지 적으면 다음 세션이 이미 끝난 감시를 알린다."""
        import time

        mon = _add(reg, tmp_path)
        reg.tick(time.time())
        (tmp_path / "p.log").write_text("X\n")
        reg.tick(time.time())
        assert not reg.get(mon.id).alive

        # 은퇴한 것을 **지우지 않고** 저장을 다시 유발한다 — delete 까지 하면
        # `_monitors` 가 비어 필터가 검증되지 않는다(사보타주로 발견).
        other = tmp_path / "other.log"
        other.write_text("")
        live = reg.add(
            build({"type": "match", "file": str(other), "pattern": "Y"}),
            deadline_s=60,
        )
        rows = json.loads((tmp_path / "monitors.json").read_text())["monitors"]
        ids = [r["id"] for r in rows]
        assert ids == [live.id], f"은퇴한 모니터가 기록에 남았다: {ids}"

    def test_no_session_dir_means_no_file(self, tmp_path):
        """headless/서브에이전트 — 디스크를 안 건드린다."""
        r = MonitorRegistry()
        r.stop()
        log = tmp_path / "n.log"
        log.write_text("")
        r.add(build({"type": "match", "file": str(log), "pattern": "X"}), deadline_s=60)
        r.stop()
        assert not (tmp_path / "monitors.json").exists()

    def test_save_failure_does_not_break_registration(self, tmp_path, monkeypatch):
        """감시는 보조 기능이다 — 디스크 오류로 런을 죽일 이유가 없다."""
        log = tmp_path / "s.log"
        log.write_text("")  # 패치 **전에** — 전역 패치는 이 쓰기도 막는다
        r = MonitorRegistry(session_dir=tmp_path / "nope" / "deeper")
        r.stop()
        monkeypatch.setattr(
            "pathlib.Path.write_text",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        mon = r.add(
            build({"type": "match", "file": str(log), "pattern": "X"}), deadline_s=60
        )
        assert mon.alive  # 등록은 성공했다
        r.stop()


class TestResumeDoesNotRevive:
    def test_describe_previous_lists_without_reviving(self, reg, tmp_path):
        _add(reg, tmp_path)
        rows = describe_previous(tmp_path)
        assert len(rows) == 1 and "match" in rows[0]

        # 새 프로세스를 흉내 — 목록은 읽히지만 **아무것도 살아나지 않는다**
        fresh = MonitorRegistry(session_dir=tmp_path)
        fresh.stop()
        assert fresh.list_all() == []
        assert not fresh.has_active_work()

    def test_no_api_to_revive_exists(self):
        """소스 핀 — 되살리는 진입점이 생기면 권한 구멍이 함께 돌아온다."""
        import inspect

        from agent_cli.monitor import registry as mod

        src = inspect.getsource(mod)
        assert "def _load" not in src and "def restore" not in src
        assert "def revive" not in src

    def test_run_command_monitors_are_listed_but_not_restored(self, reg, tmp_path):
        """가장 중요한 경우 — 승인이 필요한 모니터야말로 되살아나면 안 된다."""
        log = tmp_path / "r.log"
        log.write_text("")
        reg.add(
            build({"type": "match", "file": str(log), "pattern": "X"}),
            deadline_s=60,
            run="rm -rf /tmp/whatever",
        )
        rows = describe_previous(tmp_path)
        assert any("rm -rf" in r for r in rows), "무엇이 있었는지는 알려야 한다"

        fresh = MonitorRegistry(session_dir=tmp_path)
        fresh.stop()
        assert fresh.list_all() == [], "승인 없이 되살아났다"

    @pytest.mark.parametrize(
        "content", ["", "not json", '{"monitors": "wrong"}', '{"other": 1}']
    )
    def test_corrupt_file_is_silent(self, tmp_path, content):
        """에이전트가 워크스페이스 안의 이 파일을 망가뜨릴 수 있다."""
        (tmp_path / "monitors.json").write_text(content)
        assert describe_previous(tmp_path) == []

    def test_missing_file_is_silent(self, tmp_path):
        assert describe_previous(tmp_path) == []


class TestResumeNotice:
    def test_notice_names_each_monitor(self, reg, tmp_path):
        from agent_cli.main import _previous_monitors_notice

        _add(reg, tmp_path)
        msg = _previous_monitors_notice(tmp_path)
        assert "복원되지 않았습니다" in msg and "match" in msg

    def test_no_notice_when_there_were_none(self, tmp_path):
        from agent_cli.main import _previous_monitors_notice

        assert _previous_monitors_notice(tmp_path) == ""
        assert _previous_monitors_notice(None) == ""
