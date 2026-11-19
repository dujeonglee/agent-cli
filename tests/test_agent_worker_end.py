"""에이전트 워커가 런을 열 것을 기다리고 끝나는 법 — 순서는 테스트가 쥔다.

- 기다리는 워커는 레지스트리 조건변수(= 창구 조건변수, 재진입 락)를 쥔 채 기다린다. 기다리는 동안 락은 모든 깊이가
  풀린다 — 다른 스레드의 배달·정지가 막히지 않는다.
- 끝내기(✕ ``kill`` · 세션 종료 ``shutdown_all``)는 기다리는 워커를 깨워 내보낸다. 끝낸 뒤에는 줄 선 요청이 있어도
  런을 열지 않는다 — 끝내기가 워커의 기다림보다 먼저 왔어도 같다.
"""

from __future__ import annotations

import threading

from agent_cli.post import envelope as E
from tests.test_agents_live import _FakeLoopResult, make_registry
from tests.test_engine_p2 import wait_until


def _counting_runner(gate: threading.Event | None = None):
    calls: list[str] = []

    def runner(kind, start, ctx):
        calls.append(start.query)
        if gate is not None:
            assert gate.wait(10)
        return _FakeLoopResult(output="ok"), 0.01

    return calls, runner


def _idle_agent(reg, name="a"):
    key, err = reg.spawn(name=name)
    assert not err, err
    assert wait_until(lambda: reg.get(key).state == "idle")
    return key, reg.get(key)


class TestTheWaitReleasesTheLock:
    def test_the_registry_and_the_post_office_share_one_reentrant_condition(
        self, tmp_path
    ):
        reg = make_registry(tmp_path)
        assert reg._cv is reg._post.cv

    def test_a_nested_hold_is_released_at_every_level_while_waiting(self, tmp_path):
        reg = make_registry(tmp_path)
        entered, woke = threading.Event(), threading.Event()

        def waiter():
            with reg._cv, reg._cv:  # 두 깊이로 쥔다
                entered.set()
                reg._cv.wait(10)
                woke.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        assert entered.wait(5)
        assert reg._cv.acquire(timeout=5), "기다리는 동안 락을 다 풀지 않았다"
        try:
            reg._cv.notify_all()
        finally:
            reg._cv.release()
        assert woke.wait(5)
        t.join(5)

    def test_a_waiting_worker_does_not_hold_the_lock(self, tmp_path):
        _calls, runner = _counting_runner()
        reg = make_registry(tmp_path, runner=runner)
        _idle_agent(reg)
        assert reg._cv.acquire(timeout=5)
        reg._cv.release()
        reg.shutdown_all()


class TestEndingAWorker:
    def test_a_kill_ends_a_waiting_worker_without_opening_a_run(self, tmp_path):
        calls, runner = _counting_runner()
        reg = make_registry(tmp_path, runner=runner)
        key, tm = _idle_agent(reg)
        assert reg.kill(key) == ""
        assert wait_until(lambda: not tm.worker.is_alive())
        assert calls == [] and tm.state == "dead"

    def test_a_request_queued_before_the_kill_is_never_run(self, tmp_path):
        gate = threading.Event()
        calls, runner = _counting_runner(gate)
        reg = make_registry(tmp_path, runner=runner)
        key, tm = _idle_agent(reg)
        assert reg.request(key, "first") == ""
        assert wait_until(lambda: len(calls) == 1)  # 첫 런이 돈다(문에서 멈춤)
        assert reg.request(key, "second") == ""  # 줄에 선다
        killer = threading.Thread(target=reg.kill, args=(key,), daemon=True)
        killer.start()
        assert wait_until(lambda: tm.stop_event.is_set())
        gate.set()  # 첫 런이 끝난다 — 다음 차례는 열리지 않아야 한다
        killer.join(10)
        assert wait_until(lambda: not tm.worker.is_alive())
        assert len(calls) == 1, calls

    def test_after_the_end_the_take_returns_at_once_even_with_mail_queued(
        self, tmp_path
    ):
        calls, runner = _counting_runner()
        reg = make_registry(tmp_path, runner=runner)
        key, tm = _idle_agent(reg)
        assert reg.kill(key) == ""
        assert wait_until(lambda: not tm.worker.is_alive())
        # 끝낸 뒤 상자에 요청이 선다 — 끝내기가 기다림보다 먼저 온 경우와 같다
        reg._post.deliver(E.request("r-90", "main", f"agent:{key}", "late"))
        got: list = []
        t = threading.Thread(target=lambda: got.append(reg._next_run_items(tm)))
        t.start()
        t.join(5)
        assert not t.is_alive(), "끝난 에이전트의 꺼내기가 기다렸다"
        assert got == [[]] and calls == []

    def test_shutdown_ends_every_waiting_worker(self, tmp_path):
        calls, runner = _counting_runner()
        reg = make_registry(tmp_path, runner=runner)
        tms = [_idle_agent(reg, name=n)[1] for n in ("a", "b", "c")]
        reg.shutdown_all()
        assert all(not tm.worker.is_alive() for tm in tms)
        assert calls == []


class TestRevivalAfterTheEnd:
    def test_a_killed_agent_revived_in_the_same_session_takes_requests_again(
        self, tmp_path
    ):
        """끝내기가 닫은 상자는 같은 주소의 새 워커가 서면 다시 열린다 — 되살린 에이전트가 곧바로 끝나지 않는다."""
        calls, runner = _counting_runner()
        reg = make_registry(tmp_path, runner=runner)
        key, tm = _idle_agent(reg)
        assert reg.kill(key) == ""
        assert wait_until(lambda: not tm.worker.is_alive())
        assert reg.resume_teammate(key) == ""
        fresh = reg.get(key)
        assert wait_until(lambda: fresh.state == "idle")
        assert reg.request(key, "again") == ""
        assert wait_until(lambda: len(calls) == 1)
        assert fresh.worker.is_alive()
        reg.shutdown_all()


def test_every_lifetime_stop_also_closes_the_box():
    """평생 정지(``tm.stop_event``)를 세우는 자리는 `_stop_worker` 하나다 — 세우기만 하고 상자를 닫지 않으면 기다리는
    워커가 깨지 않는다(꺼내기는 닫힘으로 끝난다)."""
    import ast
    import inspect

    import agent_cli.subagent.agents_live as mod

    tree = ast.parse(inspect.getsource(mod))
    setters = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for n in ast.walk(fn):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "set"
                and isinstance(n.func.value, ast.Attribute)
                and n.func.value.attr == "stop_event"
            ):
                setters.append(fn.name)
    assert setters == ["_stop_worker"], setters


class TestStartingIsVisible:
    """ctx 를 여는 동안의 에이전트는 `starting` 이다 — 그 사이 main·동료가 모델을 부르면 꼬리의 로스터와 회신 끝의 상태
    줄이 그것을 말한다(재시작 직후 되살리는 에이전트가 그렇다). 우연이 아니라 알려진 동작이다."""

    def test_an_agent_opening_its_context_reads_as_starting(
        self, tmp_path, monkeypatch
    ):
        from agent_cli.prompts.system_prompt import build_live_agents_section
        from agent_cli.subagent.agent_mail import build_reply_record
        from agent_cli.subagent.resident_loop import ResidentLoop

        door, entered = threading.Event(), threading.Event()
        real_open = ResidentLoop.open

        def held_open(self, parent_ctx):
            entered.set()
            assert door.wait(10)
            return real_open(self, parent_ctx)

        monkeypatch.setattr(ResidentLoop, "open", held_open)
        _calls, runner = _counting_runner()
        reg = make_registry(tmp_path, runner=runner)
        key, err = reg.spawn(name="slow")
        assert not err
        try:
            assert entered.wait(5)
            assert reg.get(key).state == "starting"
            assert f"`{key}` (slow) [starting]" in build_live_agents_section(
                reg, include_state=True
            )
            reply = {
                "kind": "reply",
                "key": key,
                "item_id": "n-1",
                "re": "r-1",
                "success": True,
                "output": "earlier answer",
            }
            content = build_reply_record(reply, registry=reg)["content"]
            assert "(agent status: starting · 0 queued" in content
        finally:
            door.set()
        assert wait_until(lambda: reg.get(key).state == "idle")
        reg.shutdown_all()
