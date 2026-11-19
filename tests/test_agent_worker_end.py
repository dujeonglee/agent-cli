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


class TestARunJustEndedIsVisible:
    """에이전트의 회신은 그 런 **도중에** 배달된다 — 받은 쪽(main)이 곧바로 모델을 부르면 꼬리의 로스터는 보낸 에이전트를
    `[busy]` 로 보인다(아직 런을 끝내지 않았다). 런이 끝나면 `[idle]`. 우연이 아니라 알려진 동작이다."""

    def test_a_peer_that_just_messaged_reads_busy_until_its_run_ends(self, tmp_path):
        from agent_cli.prompts.system_prompt import build_live_agents_section

        gate, sent = threading.Event(), threading.Event()
        box: dict = {}

        def runner(kind, start, ctx):
            box["reg"].message_handler(f"agent:{box['key']}")(
                "main", "done here", False
            )
            sent.set()
            assert gate.wait(10)
            return _FakeLoopResult(output="ok"), 0.01

        reg = box["reg"] = make_registry(tmp_path, runner=runner)
        key, tm = _idle_agent(reg, "aa")
        box["key"] = key
        assert reg.request(key, "work") == ""
        try:
            assert sent.wait(10)  # 메일은 main 상자에 섰다 — 보낸 쪽은 아직 런 안이다
            assert reg._post.mail("main")
            assert f"`{key}` (aa) [busy]" in build_live_agents_section(
                reg, include_state=True
            )
        finally:
            gate.set()
        assert wait_until(lambda: tm.state == "idle")
        assert f"`{key}` (aa) [idle]" in build_live_agents_section(
            reg, include_state=True
        )


class TestARequesterThatIsGone:
    """요청한 쪽이 사라지면(✕·사망·과제를 마친 run) 그가 기다리던 빚을 닫는다 — 빚진 쪽의 Open Items 에서 사라지고,
    깨우지 않는 통지 한 통이 그 사실을 말한다. 그 뒤의 `reply` 는 "가지 못했다" 로 듣는다(그 런 끝까지 줄이 남는다)."""

    def test_a_kill_closes_the_debt_owed_to_it_and_notices_the_debtor(self, tmp_path):
        gate = threading.Event()
        _calls, runner = _counting_runner(gate)
        reg = make_registry(tmp_path, runner=runner)
        aa, aa_tm = _idle_agent(reg, "aa")
        bb, bb_tm = _idle_agent(reg, "bb")
        _, err = reg.message_handler(f"agent:{aa}")(bb, "slow job", True)
        assert not err
        assert wait_until(lambda: bb_tm.state == "busy")  # bb 가 집어 들고 문에서 멈춤
        (debt,) = reg.reply_debts(f"agent:{bb}")
        try:
            assert reg.kill(aa) == ""
            assert wait_until(lambda: not aa_tm.worker.is_alive())
            assert reg.reply_debts(f"agent:{bb}") == []
            (notice,) = reg._post.queued(f"agent:{bb}")
            assert notice.wakes is False
            assert notice.text == (
                f'request [{debt.id}] ("slow job") is closed — {aa} (aa) is gone; '
                "nothing is owed back."
            )
            rows = {r.rid: (r.state, r.how) for r in reg._post.rows()}
            assert rows[debt.id] == ("closed", "unsent")  # 그 런 끝까지 남는다
        finally:
            gate.set()
        assert wait_until(lambda: bb_tm.state == "idle")
        assert debt.id not in {r.rid for r in reg._post.rows()}  # 런 끝에 버려진다
        reg.shutdown_all()

    def test_a_session_end_keeps_the_debt(self, tmp_path):
        """세션 종료는 되살릴 에이전트다 — 빚을 닫지 않는다(재시작이 되살린다)."""
        gate = threading.Event()
        _calls, runner = _counting_runner(gate)
        reg = make_registry(tmp_path, runner=runner)
        aa, _ = _idle_agent(reg, "aa")
        bb, bb_tm = _idle_agent(reg, "bb")
        _, err = reg.message_handler(f"agent:{aa}")(bb, "slow job", True)
        assert not err
        assert wait_until(lambda: bb_tm.state == "busy")
        closed = []
        real = reg._post.forget_requests_of
        reg._post.forget_requests_of = lambda *a, **k: closed.append(a) or real(*a, **k)
        gate.set()
        reg.shutdown_all()
        assert closed == []

    def test_main_as_the_debtor_gets_a_quiet_notice(self, tmp_path):
        """main 이 그 요청을 흡수해(턴 경계) 빚을 졌는데 요청자가 사라진다 — main 의 빚이 닫히고, main 을 깨우지 않는
        장부 밖 통지가 main 상자에 선다(다음 턴 경계·다음 런이 읽는다)."""
        _calls, runner = _counting_runner()
        reg = make_registry(tmp_path, runner=runner)
        aa, aa_tm = _idle_agent(reg, "aa")
        _, err = reg.message_handler(f"agent:{aa}")("main", "which file?", True)
        assert not err
        assert [next(iter(i)) for i in reg.absorb_main()] == ["mail"]  # main 의 턴 경계
        (debt,) = reg.reply_debts("main")
        assert reg.kill(aa) == ""
        assert wait_until(lambda: not aa_tm.worker.is_alive())
        assert reg.reply_debts("main") == []
        (mail,) = reg._post.mail("main")
        assert mail.wakes is False
        assert f"request [{debt.id}]" in mail.text and "is gone" in mail.text
        reg.shutdown_all()
