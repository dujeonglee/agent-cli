"""모니터 소유자 라우팅 — G1 (docs/wiring/DESIGN.md §0, §3).

사용자 요구: **"main이 설치하면 main한테, agent가 설치하면 agent한테."**

종전 구조는 그 요구가 "안 되는" 게 아니라 **정반대로** 동작했다. 등록은
프로세스 전역으로 가서 서브에이전트에서도 됐는데, 배달은 main 의 턴 경계가
`drain()` 으로 **전부** 가져갔다. 주소라는 개념이 아예 없었다.

여기 있는 것은 그 주소가 (a) 도구까지 **닿고** (b) 중첩 루프로 **상속되고**
(c) 소유자가 죽으면 **정리되는지** 를 고정한다.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from agent_cli.loop.ports import LoopPorts
from agent_cli.monitor.registry import MonitorRegistry, MonitorUnavailable
from agent_cli.tools import RunContext
from agent_cli.tools.monitor_tool import MonitorTool
from tests.monitor_delivery import RecordingDelivery


@pytest.fixture
def reg():
    r = MonitorRegistry()
    r.stop()
    r.deliver = RecordingDelivery()
    yield r
    r.stop()


def _add(reg, tmp_path, owner, *, name="w.log", **kw):
    from agent_cli.monitor.conditions import build

    log = tmp_path / name
    log.write_text("")
    mon = reg.add(
        build({"type": "match", "file": str(log), "pattern": "X"}),
        owner=owner,
        deadline_s=3600,
        **kw,
    )
    reg.tick(__import__("time").time())
    return mon, log


# ── ① 도구가 자기 주소를 안다 ──────────────────────────


class TestToolKnowsItsAddress:
    def test_tool_stamps_the_run_contexts_owner(self, reg, tmp_path, monkeypatch):
        """`MonitorTool` 은 `ctx.owner` 로 등록한다 — 새 전역이 아니라
        이미 있는 per-call `RunContext` 의 필드다."""
        monkeypatch.setattr(
            "agent_cli.monitor.runtime.get_monitor_registry", lambda: reg
        )
        log = tmp_path / "t.log"
        log.write_text("")
        res = MonitorTool().run(
            {
                "mode": "add",
                "when": {"type": "match", "file": str(log), "pattern": "X"},
            },
            ctx=RunContext(owner="agent:k9"),
        )
        assert res.success, res.error
        assert [m.owner for m in reg.list_all()] == ["agent:k9"]

    def test_no_ctx_falls_back_to_main(self, reg, tmp_path, monkeypatch):
        """직접 호출(루프 밖)은 main — 그 경우 상주 루프가 아니다."""
        monkeypatch.setattr(
            "agent_cli.monitor.runtime.get_monitor_registry", lambda: reg
        )
        log = tmp_path / "t.log"
        log.write_text("")
        MonitorTool().run(
            {
                "mode": "add",
                "when": {"type": "match", "file": str(log), "pattern": "X"},
            },
            ctx=None,
        )
        assert [m.owner for m in reg.list_all()] == ["main"]

    def test_unwired_registry_surfaces_as_a_tool_error(self, tmp_path, monkeypatch):
        """예외가 도구 경계를 넘지 않는다 — ToolResult 로."""
        bare = MonitorRegistry()
        bare.stop()
        monkeypatch.setattr(
            "agent_cli.monitor.runtime.get_monitor_registry", lambda: bare
        )
        log = tmp_path / "t.log"
        log.write_text("")
        res = MonitorTool().run(
            {
                "mode": "add",
                "when": {"type": "match", "file": str(log), "pattern": "X"},
            },
            ctx=RunContext(),
        )
        assert not res.success and "배달 배선" in res.error


# ── ② 주소가 중첩 루프로 상속된다 ──────────────────────

#: 중첩 seam 전수 (docs/wiring §3.4). 여기 없는 경로로 루프가 열리면
#: 그 안에서 건 감시가 조용히 main 으로 간다.
NESTED_SEAMS = {
    "run_loop",
    "_handle_run_skill",
    "execute_skill",
    "tool_delegate",
    "_run_single",
    "_run_parallel",
    "run_subagent_message",
}

#: 이 둘은 주소를 **`ports` 안에** 담아 나른다 — 루프를 직접 만드는
#: 지점이라 포트 묶음 전체를 받는 쪽이 자연스럽다. 나머지 다섯은 아직
#: 자기 포트를 짓기 전이라 `owner` 스칼라를 받는다.
CARRIES_VIA_PORTS = {"run_loop", "run_subagent_message"}


class TestOwnerIsInherited:
    def test_every_nested_seam_requires_an_owner(self):
        """기본값이 있으면 상속 누락이 **조용하다** — 상주 에이전트 안의
        스킬이 main 으로 등록되고 아무 에러도 안 난다. 이 저장소의 배선
        사고와 정확히 같은 모양이라, 빠뜨리면 TypeError 가 나야 한다.

        (`run_loop` 은 `ports.owner` 로 받으므로 `ports` 가 그 자리다.)
        """
        missing = []
        for f in pathlib.Path("agent_cli").rglob("*.py"):
            tree = ast.parse(f.read_text())
            for n in ast.walk(tree):
                if not isinstance(n, ast.FunctionDef) or n.name not in NESTED_SEAMS:
                    continue
                want = "ports" if n.name in CARRIES_VIA_PORTS else "owner"
                args = n.args
                named = [a.arg for a in args.args + args.kwonlyargs]
                if want not in named:
                    missing.append(f"{f}:{n.lineno} {n.name} — {want} 없음")
                    continue
                # 기본값이 붙어 있으면 누락이 조용해진다.
                if want in [a.arg for a in args.kwonlyargs]:
                    i = [a.arg for a in args.kwonlyargs].index(want)
                    if args.kw_defaults[i] is not None:
                        missing.append(f"{f}:{n.lineno} {n.name} — {want} 에 기본값")
        assert missing == [], missing

    def test_every_nested_call_passes_an_owner(self):
        """선언만 무기본값이면 호출부가 빠뜨릴 때 TypeError 가 나지만,
        그건 **런타임**이다. 소스에서도 고정해 둔다 — 드물게 도는 경로가
        릴리스 뒤에야 터지는 것이 이 클래스의 사고 방식이었다."""
        bad = []
        for f in pathlib.Path("agent_cli").rglob("*.py"):
            tree = ast.parse(f.read_text())
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                fn = (
                    n.func.id
                    if isinstance(n.func, ast.Name)
                    else getattr(n.func, "attr", None)
                )
                if fn not in NESTED_SEAMS:
                    continue
                names = {k.arg for k in n.keywords}
                want = "ports" if fn in CARRIES_VIA_PORTS else "owner"
                if want not in names and None not in names:
                    bad.append(f"{f}:{n.lineno} {fn} — {want} 없음")
        assert bad == [], bad

    def test_builders_carry_the_parent_owner(self):
        from agent_cli.runtime import ports_for_oneshot, ports_for_skill

        assert ports_for_skill(agent_registry=None, owner="agent:p").owner == "agent:p"
        assert ports_for_oneshot(owner="agent:p").owner == "agent:p"

    def test_the_chain_survives_a_real_loop(self):
        """`ports.owner` → `LoopConfig.owner` → `RunContext.owner` 를
        **실제 루프로** 관통해 고정한다.

        두 데이터클래스 hop 은 `owner` 에 `"main"` 기본값이 있다(직접
        생성하는 테스트 13+N 곳 때문에). 프로덕션에서 값을 세우는 곳은
        `core.py` 의 `owner=ports.owner` 와 `tool_bridge.py` 의
        `owner=self.cfg.owner` **각 한 줄**뿐이다. 둘 중 하나를 지워도
        전체 스위트가 초록인 채로 **상주 에이전트의 보고가 전부 main 으로
        간다** — 이 저장소가 없애려는 바로 그 조용한 누락이라, 객체를 따로
        만들어 비교하는 동어반복이 아니라 루프를 지나게 해야 한다.
        """
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop
        from agent_cli.providers.capabilities import ModelCapabilities
        from tests.loop_ports import make_ports

        loop = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=ModelCapabilities(
                context_window=32768,
                max_output_tokens=4096,
                supports_thinking=False,
            ),
            model="m",
            ports=make_ports(owner="agent:k1"),
        )
        assert loop._config.owner == "agent:k1", "ports.owner 가 안 흘렀다"
        assert loop._tools._run_ctx().owner == "agent:k1", (
            "cfg.owner 가 도구까지 안 닿았다"
        )

    def test_ports_owner_has_no_default(self):
        with pytest.raises(TypeError):
            LoopPorts(
                questions=None,
                message_handler=None,
                agent_registry=None,
                mcp_manager=None,
                hook_runner=None,
                route_message=None,
                dequeue_user_message=None,
            )


# ── ③ 소유자가 죽으면 정리된다 ─────────────────────────


class TestOwnerDeath:
    def test_drop_owner_retires_only_that_address(self, reg, tmp_path):
        _add(reg, tmp_path, "agent:k1", name="a.log")
        _add(reg, tmp_path, "main", name="b.log")
        assert reg.drop_owner("agent:k1") == 1
        assert [m.owner for m in reg.list_all()] == ["main"]

    def test_dropped_is_distinguishable_from_retired(self, reg, tmp_path):
        """`retired` 로는 구분이 안 된다 — `_retire` 가 배달 **전에** 사유를
        세우므로 은퇴하는 모니터는 전부 truthy 다."""
        mon, _ = _add(reg, tmp_path, "agent:k1")
        reg.drop_owner("agent:k1")
        assert mon.retired and mon.dropped

    def test_a_dropped_monitors_final_report_is_not_delivered(self, reg, tmp_path):
        """폐기된 감시의 마지막 보고는 갈 곳이 없다 — 보내면 죽은 주소로
        가거나 main 에 유령 통지가 뜬다."""
        import time

        mon, log = _add(reg, tmp_path, "agent:k1")
        reg.drop_owner("agent:k1")
        reg.deliver.take()
        log.write_text("X\n")
        reg.tick(time.time())
        assert reg.deliver.calls == []
        assert not mon.alive

    def test_drop_during_delivery_stops_the_send(self, reg, tmp_path):
        """배달 **직전**의 재확인이 지키는 것 — 소유자가 부작용 실행 중에
        죽는 창이다. `tick` 은 live 를 스냅샷한 뒤 락 밖에서 돌고, `run`
        서브프로세스는 몇 초가 걸린다. 스냅샷만 믿으면 이미 죽은
        에이전트의 inbox 로 보고를 밀어 넣는다.
        """
        import time

        _mon, log = _add(reg, tmp_path, "agent:k1")

        real_side_effect = reg._run_side_effect

        def drop_midway(m):
            # 부작용이 도는 사이에 소유자가 죽었다.
            reg.drop_owner("agent:k1")
            return real_side_effect(m)

        reg._run_side_effect = drop_midway
        reg.deliver.take()
        log.write_text("X\n")
        reg.tick(time.time())
        assert reg.deliver.calls == [], "죽은 소유자에게 배달했다"

    def test_delete_also_blocks_a_snapshotted_fire(self, reg, tmp_path):
        """`tick` 은 live 를 스냅샷한 뒤 락 밖에서 돈다 — pop 만으로는
        삭제된 감시가 한 번 더 발화한다."""
        import time

        mon, log = _add(reg, tmp_path, "main")
        reg.delete(mon.id)
        reg.deliver.take()
        log.write_text("X\n")
        reg.tick(time.time())
        assert reg.deliver.calls == []

    def test_closed_registry_refuses_and_stops_delivering(self, reg, tmp_path):
        import time

        _, log = _add(reg, tmp_path, "main")
        reg.closed = True
        log.write_text("X\n")
        reg.tick(time.time())
        assert reg.deliver.calls == []
        with pytest.raises(MonitorUnavailable):
            _add(reg, tmp_path, "main", name="z.log")


# ── ④ 레지스트리 수명과의 접합 ─────────────────────────


class TestAgentRegistryIntegration:
    """`AgentRegistry` 가 죽은 소유자의 감시를 실제로 지우는가."""

    def _wire(self, tmp_path, reg):
        from tests.test_agents_live import make_registry

        agents = make_registry(tmp_path)
        agents.monitors = reg
        reg.deliver = agents.deliver
        return agents

    def test_kill_drops_that_agents_monitors(self, reg, tmp_path):
        agents = self._wire(tmp_path, reg)
        try:
            key, err = agents.spawn()
            assert not err, err
            _add(reg, tmp_path, f"agent:{key}")
            _add(reg, tmp_path, "main", name="m.log")
            agents.kill(key)
            assert [m.owner for m in reg.list_all()] == ["main"]
        finally:
            agents.shutdown_all()

    def test_shutdown_all_drops_them_too(self, reg, tmp_path):
        agents = self._wire(tmp_path, reg)
        key, err = agents.spawn()
        assert not err, err
        _add(reg, tmp_path, f"agent:{key}")
        agents.shutdown_all()
        assert reg.list_all() == []

    def test_unwired_registry_is_a_noop_not_a_crash(self, tmp_path):
        """배선 안 된 레지스트리(테스트 픽스처 다수)에서 kill 이 터지면
        안 된다 — 22개 픽스처가 이 경로를 지난다."""
        from tests.test_agents_live import make_registry

        agents = make_registry(tmp_path)
        assert agents.monitors is None
        try:
            key, _ = agents.spawn()
            assert agents.kill(key) == ""
        finally:
            agents.shutdown_all()

    def test_stop_requested_agent_is_treated_as_dead(self, tmp_path):
        """`state="dead"` 는 워커의 `finally` 에서야 찍힌다 — busy 런이면
        몇 분 뒤다. 그 창에 큐에 넣은 항목은 `_SHUTDOWN` 뒤에 줄 서서
        **영영 안 읽힌다**. 그래서 `stop_event` 도 사망으로 본다.
        """
        from tests.test_agents_live import make_registry

        agents = make_registry(tmp_path)
        try:
            key, _ = agents.spawn()
            tm = agents.get(key)
            tm.stop_event.set()  # kill 의 첫 단계만 재현 (state 는 아직 산 채)
            assert tm.state != "dead"
            err = agents.request(key, "일감")
            assert "is dead" in err, "죽어가는 에이전트가 항목을 받았다"
        finally:
            agents.shutdown_all()

    def test_monitor_added_while_dying_is_still_dropped(self, reg, tmp_path):
        """워커 `finally` 의 정본 드롭이 **실제로 지키는 창**.

        `kill()` 의 조기 드롭은 `stop_event.set()` 앞에서 돈다. 그런데 busy
        에이전트는 stop 뒤에도 현재 턴을 마저 돌고(`join` 은 best-effort),
        그 턴에서 `monitor add` 를 부를 수 있다 — **드롭 뒤에** 생긴
        고아다. 조기 드롭만 있으면 그 감시가 죽은 소유자를 물고 남는다.
        """
        import threading

        from tests.test_agents_live import make_registry, make_runner, wait_until

        gate = threading.Event()
        agents = make_registry(tmp_path, runner=make_runner(block=gate))
        agents.monitors = reg
        reg.deliver = agents.deliver
        try:
            key, err = agents.spawn()
            assert not err, err
            agents.request(key, "오래 걸리는 일감")
            assert wait_until(lambda: agents.get(key).state == "busy")

            agents.kill(key)  # 조기 드롭 + stop_event — 워커는 아직 돈다
            # 죽어가는 턴이 감시를 하나 더 건다.
            _add(reg, tmp_path, f"agent:{key}", name="late.log")
            assert reg.list_all(), "사전 조건: 고아가 실제로 생겨야 한다"

            gate.set()  # 턴 종료 → 워커 finally
            assert wait_until(lambda: agents.get(key).state == "dead")
            assert wait_until(lambda: reg.list_all() == []), (
                "조기 드롭 뒤에 생긴 감시가 남았다 — finally 정본이 비었다"
            )
        finally:
            gate.set()
            agents.shutdown_all()


class TestTeardown:
    def test_teardown_stops_polling_and_closes(self, reg):
        """종료가 폴링 스레드를 멈춰야 한다. 종전엔 `stop()` 에 **호출자가
        하나도 없었다** — 지금까지는 아무도 안 읽는 리스트에 쌓을 뿐이라
        무해했지만, 이제는 종료 뒤 발화가 **배달**된다.
        """
        from agent_cli.runtime import teardown_session

        assert not reg.closed
        teardown_session(None, None, monitors=reg)
        assert reg.closed, "종료 뒤 등록·배달이 계속 허용된다"
        assert reg._stop.is_set(), "폴링 스레드가 안 멈췄다"

    def test_teardown_does_not_wipe_the_previous_session_notice(self, reg, tmp_path):
        """전체 드롭이 아니라 `closed` 인 이유 — `drop_owner` 가 `delete`
        처럼 저장하면 살아 있는 행만 쓰는 `_save` 가 정상 종료마다
        `monitors.json` 을 비워, 다음 세션의 "이전 세션 모니터 N건" 통지가
        조용히 사라진다(문서화된 §8 동작의 회귀).
        """
        import json

        from agent_cli.monitor.registry import MonitorRegistry
        from agent_cli.runtime import teardown_session

        persisted = MonitorRegistry(session_dir=tmp_path)
        persisted.stop()
        persisted.deliver = RecordingDelivery()
        _add(persisted, tmp_path, "main")
        path = tmp_path / "monitors.json"
        assert json.loads(path.read_text()), "등록이 기록되지 않았다"

        teardown_session(None, None, monitors=persisted)
        assert json.loads(path.read_text()), "종료가 이전 세션 통지를 지웠다"


class TestDeliveryFailureIsVisible:
    @pytest.mark.parametrize("once", [True, False], ids=["retire", "flush"])
    def test_failure_is_logged_not_silent(self, reg, tmp_path, monkeypatch, once):
        """배달 실패는 **도달 불가가 목표**인 경로다 — 조기 드롭 둘과 배달
        직전 재확인이 막는다. 그래서 UI 통지를 만들지 않는다(도달 불가
        분기를 사용자에게 남기지 않는다). 대신 로그는 남겨야 한다 —
        안 그러면 미래의 회귀가 **조용하다**, 이 저장소가 없애려는 그것.
        """
        seen = []
        monkeypatch.setattr(
            "agent_cli.verbose.debug_log", lambda msg, *a, **k: seen.append(msg)
        )
        reg.deliver = RecordingDelivery(error="배달 실패")
        # 발화 경로가 둘이다 — `once=True` 는 `_retire`, `False` 는 `_flush`.
        # 한쪽만 보면 다른 쪽의 로그가 없어도 통과한다(실제로 그랬다).
        _add(reg, tmp_path, "agent:k1", once=once)
        (tmp_path / "w.log").write_text("X\n")
        import time

        reg.tick(time.time())
        assert any("배달 실패" in m for m in seen), "실패가 조용히 사라졌다"
