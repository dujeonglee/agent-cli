"""Monitor 배선 — 도구 · 수명 두 곳 · 턴 경계 배달 (docs/monitor/DESIGN.md §9 커밋 2).

코어(`test_monitor_core.py`)가 레지스트리 계약을 보는 곳이고, 여기는 **그게
런에 어떻게 붙는가**를 본다. 회귀 위험 1순위는 수명이다: 모니터가 살아 있는데
런이 끝나면 감시가 조용히 사라지고, 반대로 해제됐는데 안 끝나면 세션이 영영
안 죽는다.
"""

from __future__ import annotations

import time

import pytest

from agent_cli.monitor import MonitorRegistry, build


@pytest.fixture
def reg():
    r = MonitorRegistry()
    r.stop()
    yield r
    r.stop()


def _bare_loop(monitor_registry):
    """`_deliver_monitor_reports` 만 부르기 위한 최소 AgentLoop.

    `messages` 는 `_state` 를 경유하는 property 라 그 껍데기도 세운다."""
    from agent_cli.loop.core import AgentLoop
    from agent_cli.loop.state import LoopState

    loop = AgentLoop.__new__(AgentLoop)
    loop._state = LoopState.__new__(LoopState)
    loop._state.messages = []
    loop.ctx = None
    loop.turn = 1

    class _Cfg:
        pass

    _Cfg.monitor_registry = monitor_registry
    loop._config = _Cfg()
    return loop


def _watch(reg, tmp_path, **kw):
    log = tmp_path / "w.log"
    log.write_text("")
    kw.setdefault("deadline_s", 3600)
    mon = reg.add(build({"type": "match", "file": str(log), "pattern": "X"}), **kw)
    reg.tick(time.time())
    return mon, log


# ── 도구 표면 ───────────────────────────────────────────────


class TestToolSurface:
    def test_monitor_is_a_native_tool_always_registered(self):
        """`schedule` 과 달리 env 게이트가 없다 — 시간을 재는 것도 발화도
        이 프로세스 안에서 일어나므로 board 가 필요 없다."""
        from agent_cli.tools.registry import _ALL_TOOLS, TOOLS

        assert "monitor" in TOOLS
        # KV 캐시 안정: 기존 도구 순서를 건드리지 않고 **끝에** 붙는다.
        names = [t.name for t in _ALL_TOOLS]
        assert names.index("monitor") > names.index("agent")

    def test_description_carries_the_launch_idioms(self):
        """35B 대상에서 도구 설명은 산문이 아니라 **제품**이다 (§4.4).

        `2>&1` 을 빠뜨리면 `shell` 이 120s 막히고(실측), 생산자 버퍼링을
        안 풀면 건강한 스크립트가 침묵으로 보인다."""
        from agent_cli.tools.registry import TOOLS

        d = TOOLS["monitor"].description
        # `2>&1` 이 문서 어딘가에 있는지가 아니라 **nohup 줄 자체에** 있는지를
        # 본다 — 처음엔 전자로 썼다가, 첫 줄에서 빼도 `EXIT:$?` 관용구의
        # `2>&1` 때문에 통과하는 걸 사보타주로 발견했다.
        nohup = [ln for ln in d.splitlines() if "nohup" in ln]
        assert nohup, "백그라운드 실행 관용구가 없다"
        assert all("2>&1" in ln for ln in nohup), (
            f"nohup 줄에 2>&1 가 없다 — shell 이 120s 막힌다: {nohup}"
        )
        assert "-u" in d or "PYTHONUNBUFFERED" in d, "버퍼링 해제 안내가 없다"
        assert "EXIT:$?" in d, "종료 코드 관용구가 없다 (exit 조건을 뺀 대체재)"
        assert "schedule" in d, "board 세션 유도 문구가 없다"

    def test_removed_condition_types_are_not_advertised(self):
        """`exit`(구현 불가)·`interval`(주기 command 가 상위집합)을 설명이
        광고하면 모델이 계속 시도한다."""
        from agent_cli.tools.registry import TOOLS

        d = TOOLS["monitor"].description
        assert "'exit'" not in d and "'interval'" not in d

    @pytest.mark.parametrize(
        ("args", "msg"),
        [
            ({}, "mode"),
            ({"mode": "nope"}, "mode"),
            ({"mode": "add"}, "when"),
            ({"mode": "delete"}, "id"),
        ],
    )
    def test_validate_rejects_bad_shapes(self, args, msg):
        from agent_cli.tools.registry import TOOLS

        assert msg in (TOOLS["monitor"].validate(args) or "")

    def test_bad_condition_becomes_a_toolresult_not_an_exception(self, monkeypatch):
        """도구는 예외를 못 던진다 — `constants.parse_duration` 과 같은 분업."""
        from agent_cli.monitor.runtime import set_monitor_registry
        from agent_cli.tools.registry import TOOLS

        r = MonitorRegistry()
        r.stop()
        set_monitor_registry(r)
        try:
            res = TOOLS["monitor"].run(
                {"mode": "add", "when": {"type": "match", "file": "/tmp/x"}}
            )
            assert not res.success and "pattern" in (res.error or "")
        finally:
            set_monitor_registry(None)
            r.stop()

    def test_add_list_delete_round_trip(self, tmp_path):
        from agent_cli.monitor.runtime import set_monitor_registry
        from agent_cli.tools.registry import TOOLS

        r = MonitorRegistry()
        r.stop()
        set_monitor_registry(r)
        try:
            log = tmp_path / "rt.log"
            log.write_text("")
            add = TOOLS["monitor"].run(
                {
                    "mode": "add",
                    "when": {"type": "match", "file": str(log), "pattern": "E"},
                    "deadline": "30m",
                }
            )
            assert add.success and "do NOT poll" in add.output
            mon_id = add.output.split()[1]

            lst = TOOLS["monitor"].run({"mode": "list"})
            assert mon_id in lst.output

            assert TOOLS["monitor"].run({"mode": "delete", "id": mon_id}).success
            assert "No active monitors" in TOOLS["monitor"].run({"mode": "list"}).output
        finally:
            set_monitor_registry(None)
            r.stop()

    def test_deadline_omitted_defaults_instead_of_failing(self, tmp_path):
        """필수로 두면 35B 가 상시 빠뜨려 등록이 실패한다 — clamp 를 택한
        §10.1 의 논리가 누락에 더 강하게 적용된다."""
        from agent_cli.constants import MONITOR_DEADLINE_DEFAULT_S
        from agent_cli.monitor.runtime import set_monitor_registry
        from agent_cli.tools.registry import TOOLS

        r = MonitorRegistry()
        r.stop()
        set_monitor_registry(r)
        try:
            log = tmp_path / "d.log"
            log.write_text("")
            res = TOOLS["monitor"].run(
                {
                    "mode": "add",
                    "when": {"type": "match", "file": str(log), "pattern": "E"},
                }
            )
            assert res.success
            mon = r.list_all()[0]
            assert mon.deadline_at - mon.created_at == pytest.approx(
                MONITOR_DEADLINE_DEFAULT_S, abs=2
            )
        finally:
            set_monitor_registry(None)
            r.stop()


# ── 보안: 등록 시점 게이트, shell 과 같은 정책 ──────────────


class TestRegistrationTimeGate:
    """발화 시점이 아닌 이유 둘: 새벽 3시엔 사람이 없고, **폴링 스레드에서
    `renderer.confirm` 을 부르면 `interactive_lock` 교착**이다."""

    def test_shares_the_shell_confirmation_function(self):
        """소스 핀 — 정책을 따로 구현하면 갈라진다. 초판 설계가 '똑같이
        거부'라고 적었지만 실제로는 달랐던 그 자리다."""
        import inspect

        from agent_cli.tools import monitor_tool

        src = inspect.getsource(monitor_tool)
        assert "confirm_dangerous" in src
        assert "_confine.guard" in src

    def test_same_env_bypass_as_shell(self, tmp_path, monkeypatch):
        """모니터만 모든 명령에 확인을 요구하면 harbor/CI 에서 못 쓰는데,
        **그 headless 환경이 monitor 를 만드는 근거였다**(§3.1)."""
        from agent_cli.monitor.runtime import set_monitor_registry
        from agent_cli.tools.registry import TOOLS

        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "0")
        monkeypatch.setenv("AGENT_CLI_WORKSPACE_CONFINE", "0")
        r = MonitorRegistry()
        r.stop()
        set_monitor_registry(r)
        try:
            res = TOOLS["monitor"].run(
                {
                    "mode": "add",
                    "when": {
                        "type": "command",
                        "command": "rm -rf /tmp/x",
                        "every": "5m",
                    },
                }
            )
            assert res.success, f"headless 우회가 안 먹었다: {res.error}"
        finally:
            set_monitor_registry(None)
            r.stop()

    def test_dangerous_command_refused_without_a_prompt_surface(
        self, tmp_path, monkeypatch
    ):
        from agent_cli.monitor.runtime import set_monitor_registry
        from agent_cli.render import get_renderer
        from agent_cli.tools.registry import TOOLS

        monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
        monkeypatch.setattr(type(get_renderer()), "can_prompt", lambda self: False)
        r = MonitorRegistry()
        r.stop()
        set_monitor_registry(r)
        try:
            res = TOOLS["monitor"].run(
                {
                    "mode": "add",
                    "when": {
                        "type": "match",
                        "file": str(tmp_path / "a"),
                        "pattern": "X",
                    },
                    "run": "rm -rf /tmp/whatever",
                }
            )
            assert not res.success and "Refused" in (res.error or "")
        finally:
            set_monitor_registry(None)
            r.stop()

    def test_notify_only_monitor_needs_no_confirmation(self, tmp_path, monkeypatch):
        """새 권한이 아니다 — 파일을 보는 것뿐이다."""
        from agent_cli.monitor.runtime import set_monitor_registry
        from agent_cli.render import get_renderer
        from agent_cli.tools.registry import TOOLS

        monkeypatch.setattr(type(get_renderer()), "can_prompt", lambda self: False)
        r = MonitorRegistry()
        r.stop()
        set_monitor_registry(r)
        try:
            log = tmp_path / "n.log"
            log.write_text("")
            res = TOOLS["monitor"].run(
                {
                    "mode": "add",
                    "when": {"type": "match", "file": str(log), "pattern": "X"},
                }
            )
            assert res.success
        finally:
            set_monitor_registry(None)
            r.stop()


# ── 수명: **두 곳** (회귀 위험 1순위) ───────────────────────


class TestLifetimeInBothRuntimes:
    def test_run_pump_does_not_exit_while_a_monitor_lives(self, reg, tmp_path):
        """모니터가 살아 있는데 런이 끝나면 감시가 조용히 사라진다."""
        from agent_cli.input_queue import InputQueue
        from agent_cli.main import _run_message_pump

        class _Waker:
            idle = __import__("threading").Event()

            def mark_idle(self):
                pass

            def handle_dequeued(self, text):
                return None

            def on_run_end(self):
                pass

        class _Reg:
            def has_active_work(self):
                return False

        _watch(reg, tmp_path)
        q = InputQueue()
        calls = []

        # 모니터가 살아 있으면 펌프가 안 끝난다 → 별도 스레드로 돌리고
        # 해제한 뒤 끝나는지 본다.
        import threading

        done = threading.Event()

        def pump():
            _run_message_pump(
                q,
                _Waker(),
                _Reg(),
                lambda t, wake: calls.append(t),
                monitors=reg,
                poll_secs=0.05,
            )
            done.set()

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        assert not done.wait(0.4), "모니터가 살아 있는데 펌프가 끝났다"

        for m in reg.list_all():
            reg.delete(m.id)
        reg.drain()
        assert done.wait(2.0), "모니터가 사라졌는데 펌프가 안 끝난다"

    def test_pump_still_exits_with_no_monitors_at_all(self, tmp_path):
        """회귀 가드 — monitors=None 이면 종전 그대로."""
        from agent_cli.input_queue import InputQueue
        from agent_cli.main import _run_message_pump

        class _Waker:
            idle = __import__("threading").Event()

            def mark_idle(self):
                pass

            def handle_dequeued(self, text):
                return None

            def on_run_end(self):
                pass

        class _Reg:
            def has_active_work(self):
                return False

        import threading

        done = threading.Event()
        t = threading.Thread(
            target=lambda: (
                _run_message_pump(
                    InputQueue(),
                    _Waker(),
                    _Reg(),
                    lambda *a, **k: None,
                    poll_secs=0.05,
                ),
                done.set(),
            ),
            daemon=True,
        )
        t.start()
        assert done.wait(2.0), "모니터 없이도 펌프가 안 끝난다 (회귀)"

    def test_undelivered_report_also_holds_the_pump(self, reg, tmp_path):
        """은퇴했어도 보고가 남았는데 런이 끝나면 그 보고가 사라진다."""
        mon, log = _watch(reg, tmp_path)
        log.write_text("X\n")
        reg.tick(time.time())
        assert not reg.get(mon.id).alive and reg.has_active_work()
        reg.drain()
        assert not reg.has_active_work()

    def test_web_self_reap_predicate_knows_about_monitors(self, reg, tmp_path):
        """설계 초판은 "web 은 무한 대기"라 봤지만 **틀렸다** — board 가 띄운
        인스턴스는 뷰어가 없으면 자기를 거두고 모니터가 조용히 죽는다."""
        from agent_cli.main import web_instance_is_active

        class _R:
            def has_live_connections(self):
                return False

            def worker_is_busy(self):
                return False

        class _S:
            def pending_count(self):
                return 0

        assert not web_instance_is_active(_R(), _S(), None, None)
        _watch(reg, tmp_path)
        assert web_instance_is_active(_R(), _S(), None, reg), (
            "살아 있는 모니터를 데리고 인스턴스가 자기수확한다"
        )


# ── 턴 경계 배달 ────────────────────────────────────────────


class TestTurnBoundaryDelivery:
    def test_reports_become_tool_monitor_observation_records(self, reg, tmp_path):
        """큐가 아니라 **관찰 레코드**다 (§6.1) — 큐에 태우면 보고가 사람
        메시지로 위장되고, run 에선 배달이 런 종료 후로 밀린다."""

        _, log = _watch(reg, tmp_path)
        log.write_text("X boom\n")
        reg.tick(time.time())

        loop = _bare_loop(reg)
        loop._deliver_monitor_reports()
        assert len(loop.messages) == 1 and "boom" in loop.messages[0]["content"]

    def test_record_shape_needs_no_registration_anywhere(self):
        """`tool="monitor"` 가 기존 관찰 경로를 그대로 탄다는 계약.

        - 재생은 `tool` 키가 있으면 관찰로 취급
        - `is_format_intervention` 은 `tool == ""` 일 때만 (그래서 빈 문자열 금지)
        """
        from agent_cli.context.records import _classify_record, is_format_intervention

        rec = {"role": "user", "tool": "monitor", "success": True, "content": "🔔 x"}
        kind, tools, _ = _classify_record(rec)
        assert kind == "observation" and tools == ["monitor"]
        # `tool == ""` 만 형식-개입이다 — 그래서 빈 문자열을 쓰면 안 된다.
        assert not is_format_intervention(rec)
        assert is_format_intervention(
            {"role": "user", "tool": "", "success": False, "content": "x"}
        )

    def test_drain_is_called_at_the_turn_boundary(self):
        """소스 핀 — `_deliver_agent_mail` 형제로 같은 자리에 있어야 한다."""
        import inspect

        from agent_cli.loop.core import AgentLoop

        src = inspect.getsource(AgentLoop)
        assert (
            "self._deliver_agent_mail()\n                self._deliver_monitor_reports()"
            in src
        )

    def test_no_report_no_record(self, reg):

        loop = _bare_loop(reg)
        loop._deliver_monitor_reports()
        assert loop.messages == []

    def test_absent_registry_is_a_noop(self):
        """서브에이전트/headless — 배달 없음."""

        loop = _bare_loop(None)
        loop._deliver_monitor_reports()
        assert loop.messages == []
