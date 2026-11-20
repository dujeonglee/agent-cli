"""에이전트 워커가 런을 열 것을 기다리고 끝나는 법 — 순서는 테스트가 쥔다.

- 기다리는 워커는 레지스트리 조건변수(= 창구 조건변수, 재진입 락)를 쥔 채 기다린다. 기다리는 동안 락은 모든 깊이가
  풀린다 — 다른 스레드의 배달·정지가 막히지 않는다.
- 끝내기(✕ ``kill`` · 세션 종료 ``shutdown_all``)는 기다리는 워커를 깨워 내보낸다. 끝낸 뒤에는 줄 선 요청이 있어도
  런을 열지 않는다 — 끝내기가 워커의 기다림보다 먼저 왔어도 같다.
"""

from __future__ import annotations

import threading

import pytest

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


class TestAParentThatDies:
    def test_its_run_agent_is_stopped_with_it(self, tmp_path, monkeypatch):
        """부모 에이전트의 워커가 오류로 죽으면 그가 띄운 run 에이전트도 멈춘다(✕ 와 같이 — 결과를 받을 부모가 없다).
        멈춘 쪽은 하니스다(``stopped by harness``). 그 run 이 부모에게 진 빚은 부모가 사라질 때 먼저 닫혔다."""
        from agent_cli.subagent import agent_stop
        from agent_cli.subagent.resident_loop import ResidentLoop

        gate = threading.Event()
        _calls, runner = _counting_runner(gate)
        reg = make_registry(tmp_path, runner=runner)
        aa, aa_tm = _idle_agent(reg, "aa")
        kid, err = reg.spawn(name="kid", ends_after_task=True, parent=f"agent:{aa}")
        assert not err
        kid_tm = reg.get(kid)
        assert wait_until(lambda: kid_tm.state == "idle")
        _, err = reg.message_handler(f"agent:{aa}")(kid, "child work", True)
        assert not err
        assert wait_until(lambda: kid_tm.state == "busy")  # 문에서 멈춘 채 일한다
        real_open = ResidentLoop.open_scope

        def crashing(self, start):
            if self.tm.key == aa:
                raise MemoryError("worker machinery")
            return real_open(self, start)

        monkeypatch.setattr(ResidentLoop, "open_scope", crashing)
        assert reg.request(aa, "go") == ""
        assert wait_until(lambda: not aa_tm.worker.is_alive())
        assert aa_tm.error.startswith("MemoryError")
        assert kid_tm.stop_cause == agent_stop.STOPPED
        assert kid_tm.stop_by == agent_stop.harness_by(aa, agent_stop.PARENT_DIED)
        assert reg.reply_debts(f"agent:{kid}") == []  # 부모에게 진 빚은 닫혔다
        gate.set()
        assert wait_until(lambda: not kid_tm.worker.is_alive())
        assert kid_tm.state == "dead"
        reg.shutdown_all()

    def test_its_other_requester_hears_why(self, tmp_path, monkeypatch):
        """그 run 이 다른 쪽(main)의 요청을 일하던 중이면 main 은 하니스의 정지 회신을 듣는다 — 왜 멈췄는지(띄운 부모가
        죽었다)를 한 문장으로, 표지는 ``stopped by harness``."""
        from agent_cli.subagent import agent_stop
        from agent_cli.subagent.resident_loop import ResidentLoop

        gate = threading.Event()
        _calls, runner = _counting_runner(gate)
        reg = make_registry(tmp_path, runner=runner)
        aa, aa_tm = _idle_agent(reg, "aa")
        kid, err = reg.spawn(name="kid", ends_after_task=True, parent=f"agent:{aa}")
        assert not err
        kid_tm = reg.get(kid)
        assert wait_until(lambda: kid_tm.state == "idle")
        _, err = reg.message_handler("main")(kid, "main work", True)
        assert not err
        assert wait_until(lambda: kid_tm.state == "busy")
        (debt,) = reg.reply_debts(f"agent:{kid}")
        real_open = ResidentLoop.open_scope

        def crashing(self, start):
            if self.tm.key == aa:
                raise MemoryError("worker machinery")
            return real_open(self, start)

        monkeypatch.setattr(ResidentLoop, "open_scope", crashing)
        try:
            assert reg.request(aa, "go") == ""
            assert wait_until(lambda: not aa_tm.worker.is_alive())
            assert kid_tm.stop_by == agent_stop.harness_by(aa, agent_stop.PARENT_DIED)
        finally:
            gate.set()  # 정지 회신은 그 런이 내려올 때 간다

        def replies():
            return [m for m in reg._post.mail("main") if f"[{debt.id}]" in m.text]

        assert wait_until(replies)
        (mail,) = replies()
        assert mail.mark == "stopped by harness"
        assert mail.text.splitlines()[0] == (
            f"{kid} was stopped because {aa} (the agent that started it) died, "
            f'while it was working on your request [{debt.id}] ("main work").'
        )
        assert wait_until(lambda: not kid_tm.worker.is_alive())
        reg.shutdown_all()

    def test_a_resident_child_is_left_alone(self, tmp_path, monkeypatch):
        """상주(``ends_after_task`` 아님)는 부모와 수명을 같이하지 않는다 — ✕ 와 같은 규칙."""
        from agent_cli.subagent.resident_loop import ResidentLoop

        _calls, runner = _counting_runner()
        reg = make_registry(tmp_path, runner=runner)
        aa, aa_tm = _idle_agent(reg, "aa")
        res, err = reg.spawn(name="res", parent=f"agent:{aa}")
        assert not err
        res_tm = reg.get(res)
        assert wait_until(lambda: res_tm.state == "idle")
        real_open = ResidentLoop.open_scope
        monkeypatch.setattr(
            ResidentLoop,
            "open_scope",
            lambda self, start: (
                (_ for _ in ()).throw(MemoryError("x"))
                if self.tm.key == aa
                else real_open(self, start)
            ),
        )
        assert reg.request(aa, "go") == ""
        assert wait_until(lambda: not aa_tm.worker.is_alive())
        assert res_tm.stop_cause == "" and res_tm.worker.is_alive()
        reg.shutdown_all()


def test_the_harness_stop_reply_is_one_constant():
    """하니스가 멈춘 run 의 회신 첫 줄은 정지 이유 상수 한 자리(`agent_stop.HARNESS_CAUSE`)에 일하던 중·시작 전이
    붙는다 — 부모가 어떻게 끝났는지(죽음·정지)만 갈린다. 표지는 ``stopped by harness``."""
    from agent_cli.subagent import agent_stop

    for how, ended in (
        (agent_stop.PARENT_DIED, "died"),
        (agent_stop.PARENT_STOPPED, "was stopped"),
    ):
        by = agent_stop.harness_by("agt-X", how)
        working = agent_stop.stopped_reply("agt-Y", "r-3", "child work", [], "", by=by)
        queued = agent_stop.unstarted_reply(
            "agt-Y", "r-4", "later", by=by, cause=agent_stop.STOPPED
        )
        cause = f"agt-Y was stopped because agt-X (the agent that started it) {ended}"
        assert (
            agent_stop.HARNESS_CAUSE.format(
                agent="agt-Y", parent="agt-X", parent_ended=ended
            )
            == cause
        )
        assert working.splitlines()[0] == (
            f'{cause}, while it was working on your request [r-3] ("child work").'
        )
        assert queued.splitlines()[0] == (
            f'{cause}, before it started your request [r-4] ("later"). '
            "Nothing was done for it."
        )
        assert agent_stop.stop_mark(agent_stop.STOPPED, by) == "stopped by harness"


class TestAChainOfRuns:
    """부모가 죽으면 하니스가 그 run 을 멈추고, 그 run 이 내려오면서 자기가 띄운 run 을 멈춘다 — 각 단의 회신은 바로 위
    부모가 어떻게 끝났는지를 말한다: 죽은 부모의 자식은 "died", 멈춘 부모의 자식은 "was stopped"."""

    def test_three_levels(self, tmp_path, monkeypatch):
        from agent_cli.subagent import agent_stop
        from agent_cli.subagent.resident_loop import ResidentLoop

        gates = {"kid work": threading.Event(), "gc work": threading.Event()}

        def runner(kind, start, ctx):
            for word, gate in gates.items():
                if word in start.query:
                    assert gate.wait(10)
            return _FakeLoopResult(output="ok"), 0.01

        reg = make_registry(tmp_path, runner=runner)
        aa, aa_tm = _idle_agent(reg, "aa")
        kid, err = reg.spawn(name="kid", ends_after_task=True, parent=f"agent:{aa}")
        assert not err
        gc, err = reg.spawn(name="gc", ends_after_task=True, parent=f"agent:{kid}")
        assert not err
        kid_tm, gc_tm = reg.get(kid), reg.get(gc)
        assert wait_until(lambda: kid_tm.state == "idle" and gc_tm.state == "idle")
        assert reg.message_handler("main")(kid, "kid work", True)[1] == ""
        assert reg.message_handler("main")(gc, "gc work", True)[1] == ""
        assert wait_until(lambda: kid_tm.state == "busy" and gc_tm.state == "busy")
        (kid_debt,) = reg.reply_debts(f"agent:{kid}")
        (gc_debt,) = reg.reply_debts(f"agent:{gc}")
        real_open = ResidentLoop.open_scope

        def crashing(self, start):
            if self.tm.key == aa:
                raise MemoryError("worker machinery")
            return real_open(self, start)

        monkeypatch.setattr(ResidentLoop, "open_scope", crashing)
        try:
            assert reg.request(aa, "go") == ""
            assert wait_until(lambda: not aa_tm.worker.is_alive())
            assert kid_tm.stop_by == agent_stop.harness_by(aa, agent_stop.PARENT_DIED)
            assert gc_tm.stop_by == ""  # 손자는 kid 가 내려올 때 멈춘다
            gates["kid work"].set()
            assert wait_until(lambda: not kid_tm.worker.is_alive())
            assert gc_tm.stop_by == agent_stop.harness_by(
                kid, agent_stop.PARENT_STOPPED
            )
        finally:
            for gate in gates.values():
                gate.set()
        assert wait_until(lambda: not gc_tm.worker.is_alive())

        def reply_to(debt):
            return [m for m in reg._post.mail("main") if m.re == debt.id]

        assert wait_until(lambda: reply_to(kid_debt) and reply_to(gc_debt))
        (kid_mail,), (gc_mail,) = reply_to(kid_debt), reply_to(gc_debt)
        assert kid_mail.mark == gc_mail.mark == "stopped by harness"
        assert kid_mail.text == (
            f"{kid} was stopped because {aa} (the agent that started it) died, "
            f'while it was working on your request [{kid_debt.id}] ("kid work").\n'
            "Decide whether to continue another way or report back to the user."
        )
        assert gc_mail.text == (
            f"{gc} was stopped because {kid} (the agent that started it) was stopped, "
            f'while it was working on your request [{gc_debt.id}] ("gc work").\n'
            "Decide whether to continue another way or report back to the user."
        )
        reg.shutdown_all()


class TestTwoEndsAtOnce:
    def test_two_agents_that_wait_on_each_other_die_together(
        self, tmp_path, monkeypatch
    ):
        """aa 와 bb 가 서로에게 회신을 기다리는 채로 둘이 함께 죽는다 — 각자의 정리가 상대의 빚을 닫는 순서가 어느 쪽이든
        장부는 비고(열린 줄 없음) 불변식이 지켜지며, 정리는 예외 없이 끝난다(둘 다 dead)."""
        from agent_cli.subagent.resident_loop import ResidentLoop

        gate = threading.Event()
        _calls, runner = _counting_runner(gate)
        reg = make_registry(tmp_path, runner=runner)
        aa, aa_tm = _idle_agent(reg, "aa")
        bb, bb_tm = _idle_agent(reg, "bb")
        assert not reg.message_handler(f"agent:{aa}")(bb, "for bb", True)[1]
        assert not reg.message_handler(f"agent:{bb}")(aa, "for aa", True)[1]
        assert wait_until(lambda: aa_tm.state == "busy" and bb_tm.state == "busy")
        real_open = ResidentLoop.open_scope
        monkeypatch.setattr(
            ResidentLoop,
            "open_scope",
            lambda self, start: (
                (_ for _ in ()).throw(MemoryError("x"))
                if self.tm.key in (aa, bb)
                else real_open(self, start)
            ),
        )
        # 지금 런을 끝내면 다음 런 시작(각자에게 온 통지)에서 둘이 함께 죽는다
        assert reg.request(aa, "fyi", author="main", expects_reply=False) == ""
        assert reg.request(bb, "fyi", author="main", expects_reply=False) == ""
        gate.set()
        assert wait_until(
            lambda: not aa_tm.worker.is_alive() and not bb_tm.worker.is_alive()
        )
        assert aa_tm.state == bb_tm.state == "dead"
        assert [r.rid for r in reg._post.rows() if r.state != "closed"] == []
        reg._post.check_consistency()
        reg.shutdown_all()


def _gated_runner(*words: str):
    """``words`` 중 하나를 담은 런은 그 문에서 멈춘다 — 문은 테스트가 열거나, 그 런의 정지(■·세션 종료)가 연다."""
    gates = {w: threading.Event() for w in words}
    calls: list[str] = []

    def runner(kind, start, ctx):
        calls.append(start.query)
        stop = kind._run_stop
        for word, gate in gates.items():
            if word in start.query:
                assert wait_until(lambda g=gate: g.is_set() or stop.is_set())
        return _FakeLoopResult(output="ok"), 0.01

    return gates, calls, runner


def _crash_on_open(monkeypatch, key):
    from agent_cli.subagent.resident_loop import ResidentLoop

    real_open = ResidentLoop.open_scope

    def crashing(self, start):
        if self.tm.key == key:
            raise MemoryError("worker machinery")
        return real_open(self, start)

    monkeypatch.setattr(ResidentLoop, "open_scope", crashing)


class TestAQueuedRequestOfAGoneRequester:
    """요청한 쪽이 사라지면(✕·사망) 그가 보낸 요청 중 아직 아무 런도 집지 않은 것은 빚진 쪽 상자에서 빠진다 — 그
    요청으로 런이 열리지 않고(아무도 받지 않을 일을 끝까지 하지 않는다), 대기열·로스터 수가 줄고, 통지는 없다(본 적 없는
    id 다). 재시작도 그것을 되살리지 않는다. 세션 종료는 이 길을 지나지 않는다."""

    @pytest.mark.parametrize("end", ["kill", "death"])
    def test_it_never_opens_a_run(self, tmp_path, monkeypatch, end):
        gates, calls, runner = _gated_runner("job1", "go")
        reg = make_registry(tmp_path, runner=runner)
        aa, aa_tm = _idle_agent(reg, "aa")
        bb, bb_tm = _idle_agent(reg, "bb")
        assert reg.message_handler("main")(bb, "job1", True)[1] == ""
        assert wait_until(lambda: bb_tm.state == "busy")
        _, err = reg.message_handler(f"agent:{aa}")(bb, "job2 for aa", True)
        assert not err
        (queued,) = reg.queued_for(f"agent:{bb}")
        assert reg.backlog_of(bb) == 1
        try:
            if end == "kill":
                assert reg.kill(aa) == ""
            else:
                _crash_on_open(monkeypatch, aa)
                assert reg.request(aa, "go") == ""
            assert wait_until(lambda: not aa_tm.worker.is_alive())
            assert reg.queued_for(f"agent:{bb}") == ()
            assert reg.backlog_of(bb) == 0
            (snap,) = [s for s in reg.roster_snapshot() if s["key"] == bb]
            assert snap["pending_requests"] == 0
            assert queued.id not in {r.rid for r in reg._post.rows()}
        finally:
            for gate in gates.values():
                gate.set()
        assert wait_until(lambda: bb_tm.state == "idle")
        assert calls == ["job1"]  # 그 요청으로 연 런은 없다
        assert reg._post.queued(f"agent:{bb}") == ()  # 통지도 없다
        reg.shutdown_all()

    def test_main_as_the_debtor_loses_it_from_the_mail_queue(self, tmp_path):
        """main 상자에 줄 선(아직 흡수하지 않은) 요청 — 메일에서 빠지고 입력창 위 대기열이 다시 그려진다."""
        _calls, runner = _counting_runner()
        reg = make_registry(tmp_path, runner=runner)
        redraws = []
        reg.on_pending_change = lambda: redraws.append(len(reg.pending_mail()))
        aa, aa_tm = _idle_agent(reg, "aa")
        _, err = reg.message_handler(f"agent:{aa}")("main", "which file?", True)
        assert not err
        assert len(reg.pending_mail()) == 1
        assert reg.kill(aa) == ""
        assert wait_until(lambda: not aa_tm.worker.is_alive())
        assert reg._post.mail("main") == () and reg.pending_mail() == []
        assert reg.reply_debts("main") == [] and reg._post.rows() == ()
        assert redraws and redraws[-1] == 0
        reg.shutdown_all()

    def test_a_restart_does_not_bring_it_back(self, tmp_path, monkeypatch):
        """요청자가 죽은 것이 저장됐는데 그 요청을 빼기 전에 프로세스가 끝났다(그 사이) — 재시작은 그 요청을 되넣지
        않는다: 에이전트 받은편지함에도, main 메일에도."""
        from agent_cli.subagent import agent_stop

        gates, _calls, runner = _gated_runner("job1")
        reg = make_registry(tmp_path, runner=runner)
        aa, aa_tm = _idle_agent(reg, "aa")
        bb, bb_tm = _idle_agent(reg, "bb")
        assert reg.message_handler("main")(bb, "job1", True)[1] == ""
        assert wait_until(lambda: bb_tm.state == "busy")
        send = reg.message_handler(f"agent:{aa}")
        assert send(bb, "job2 for aa", True)[1] == ""
        assert send("main", "which file?", True)[1] == ""
        monkeypatch.setattr(agent_stop, "close_debts_owed_to", lambda *a: None)
        assert reg.kill(aa) == ""
        assert wait_until(lambda: not aa_tm.worker.is_alive())
        assert len(reg.queued_for(f"agent:{bb}")) == 1  # 그 사이 — 아직 빼지 않았다
        reg.shutdown_all()
        gates["job1"].set()
        monkeypatch.undo()

        again = make_registry(
            tmp_path, runner=_gated_runner("job2")[2]
        )  # 되넣은 job2 는 문에 선다
        assert again.restore() == 1
        # 되넣기는 restore 안에서 끝난다 — 되넣었다면 줄에 섰거나 이미 집혔다
        assert again.queued_for(f"agent:{bb}") == ()
        assert again.taken_by(f"agent:{bb}") == ()
        assert again._post.mail("main") == () and again._post.rows() == ()
        again.shutdown_all()

    def test_a_session_end_drops_it(self, tmp_path):
        """세션 종료 뒤 재시작은 명단만 올린다 — 줄 선 요청은 남지 않는다(5c-0; 종전엔 디스크에 남아 재시작이 되넣었다)."""
        gates, _calls, runner = _gated_runner("job1")
        reg = make_registry(tmp_path, runner=runner)
        aa, _ = _idle_agent(reg, "aa")
        bb, bb_tm = _idle_agent(reg, "bb")
        assert reg.message_handler("main")(bb, "job1", True)[1] == ""
        assert wait_until(lambda: bb_tm.state == "busy")
        assert reg.message_handler(f"agent:{aa}")(bb, "job2 for aa", True)[1] == ""
        reg.shutdown_all()
        gates["job1"].set()

        again = make_registry(
            tmp_path, runner=_gated_runner("job2")[2]
        )  # 되넣은 job2 는 문에 선다
        assert again.restore() == 2
        assert again.queued_for(f"agent:{bb}") == ()
        assert again.taken_by(f"agent:{bb}") == ()
        again.shutdown_all()
