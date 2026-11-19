"""엔진 통합 P2 — 결과 = 회신 · message/reply 한 쌍 · 제자리 대기 (docs/agent-engine/DESIGN.md §3).

실제 레지스트리·실제 `MainLoop`·실제 엔진(`run_loop`)에 가짜 provider 하나를 꽂는다. provider 는
배우(main·run 에이전트)를 **첫 사용자 메시지의 표지**로 가른다 — 배우마다 대본이 있고, 대본의
한 줄은 글이거나(그대로 응답) 함수다(그 호출의 메시지를 보고 응답을 만든다: 꼬리에 적힌 요청
id 로 `reply` 하기, 사람이 끼어들 때까지 붙잡기).
"""

from __future__ import annotations

import json
import re
import threading
import time

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.loop.kind import Start
from agent_cli.main_input import MainInput
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.runtime import MainLoop
from agent_cli.session_slots import set_agent_registry
from agent_cli.subagent.agents_live import AgentRegistry
from tests.binding_support import make_binding
from tests.mail_support import absorbed_mail

CAPS = ModelCapabilities(
    context_window=65536, max_output_tokens=4096, supports_thinking=False
)


def call(action: str, **kw) -> str:
    return json.dumps({"action": action, **kw})


def owed_id(messages) -> str:
    """꼬리의 `## Open Items` 에서 갚을 요청 id — ``→ `reply` with `id` "r-5"``."""
    text = "\n".join(str(m.get("content") or "") for m in messages[-2:])
    m = re.search(r'`reply` with `id` "(r-\d+)"', text)
    assert m, text[-800:]
    return m.group(1)


def complete_all(result: str):
    """꼬리가 말하는 닫을 id 전부로 `complete` — main 앞 메일(회신 도착 ``n-``)도 장부 항목이다."""

    def step(messages):
        tail = str(messages[-1].get("content") or "")
        m = re.search(r"listing in `answers` the ids it covers \(([^)]*)\)", tail)
        ids = re.findall(r'"([a-z]-\d+)"', m.group(1)) if m else []
        return call("complete", result=result, answers=ids)

    return step


def main_waits_in_place(reg) -> bool:
    """main 이 지금 제자리 대기 중인가 — `_wait_in_place` 가 막기 직전에 체크인을 건다. 회신·사람
    입력의 도착을 테스트가 이 뒤로 미뤄 "wait 보다 먼저 도착" 경주를 없앤다."""
    return "main" in reg._checkin_armed


def reply_after_main_waits(holder: dict, text: str):
    """main 이 제자리 대기에 들어간 **뒤** 회신하고 끝내는 대본 — ``holder["reg"]`` 는 세션의 것."""

    def step(msgs):
        assert wait_until(lambda: main_waits_in_place(holder["reg"]))
        return call("reply", id=owed_id(msgs), text=text)

    return [step, call("complete", result="replied")]


def reply_then_complete(text: str):
    return [
        lambda msgs: call("reply", id=owed_id(msgs), text=text),
        call("complete", result="replied"),
    ]


class Provider:
    """배우별 대본. 대본이 다 떨어지면 ``complete`` (붙잡지 않는다)."""

    def __init__(self, scripts: dict[str, list]):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: dict[str, list] = {k: [] for k in scripts}
        self._lock = threading.Lock()

    def actor(self, messages) -> str:
        users = [m for m in messages if m.get("role") == "user"]
        first = str(users[0].get("content") or "") if users else ""
        # 꼬리(하니스가 붙인 것 — 동료 로스터에 남의 과제 글이 있다)는 보지 않는다
        first = first.split("── end of message", 1)[0]
        for tag in self.scripts:
            if tag != "MAIN" and tag in first:
                return tag
        return "MAIN"

    def call(self, messages, system, model, capabilities, **kw):
        a = self.actor(messages)
        with self._lock:
            self.calls[a].append(list(messages))
            step = self.scripts[a].pop(0) if self.scripts[a] else None
        if step is None:
            step = call("complete", result=f"{a} done", answers=[])
        if callable(step):
            step = step(messages)
        return LLMResponse(content=step)


@pytest.fixture
def session(tmp_path):
    """main 세션 하나 — 레지스트리(세션 슬롯)·입력 큐·`MainLoop`. ``run(provider, text)`` 가 런 하나."""
    built: dict = {}

    def make(provider, *, max_depth=2):
        binding = make_binding(provider=provider, capabilities=CAPS, model="m")
        sdir = tmp_path / "s"
        ctx = ContextManager(sdir, max_context_tokens=60000, binding=binding)
        queue = MainInput()
        reg = AgentRegistry(
            sdir,
            post=queue.post,
            runtime={
                "binding": binding,
                "max_turns": 20,
                "depth": 0,
                "max_depth": max_depth,
            },
        )
        set_agent_registry(reg)
        main = MainLoop(
            binding=binding,
            ctx=ctx,
            session=None,
            agent_registry=reg,
            mcp_manager=None,
            enqueue_user_message=lambda text, nickname: queue.enqueue(None, text),
            route_message=lambda text: False,
            max_turns=30,
            max_depth=max_depth,
            verbose=False,
            record_turns=True,
            hooks_config=None,
            graceful_interrupt=False,
            wait_for_user_input=queue.wait_for_arrival,
        )
        built.update(reg=reg, ctx=ctx, queue=queue, main=main)

        def run(text="MAIN go", stop_event=None):
            start = Start(
                query=text,
                author_is_user=True,
                request_id="u-0",
                stop_event=stop_event or threading.Event(),
            )
            return main.run(start)

        built["run"] = run
        return built

    yield make
    reg = built.get("reg")
    if reg is not None:
        reg.shutdown_all()
    set_agent_registry(None)


def records(ctx) -> list[dict]:
    return list(ctx.get_raw_messages())


def contents(ctx) -> str:
    return "\n".join(str(r.get("content") or "") for r in records(ctx))


def wait_until(pred, timeout=10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class TestResultIsAReply:
    def test_main_runs_waits_in_place_and_reads_the_reply_in_the_same_run(
        self, session
    ):
        holder: dict = {}
        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-A find callers"),
                    call("wait", reason="agent"),
                    complete_all("main finished"),
                ],
                # 회신은 main 이 제자리 대기에 든 뒤에 — 먼저 오면 wait 가 "이미 도착" 으로 돌아간다
                "TASK-A": reply_after_main_waits(holder, "3 callers: a.py, b.py, c.py"),
            }
        )
        s = session(p)
        holder["reg"] = s["reg"]
        result = s["run"]()
        assert result.success and result.output.startswith("main finished")
        assert not getattr(result, "waiting", False)  # 제자리 — 런을 끝내지 않았다
        text = contents(s["ctx"])
        assert 'started agent "agt-' in text and 'on request "r-' in text
        assert "will wake on: agent:agt-" in text
        assert "3 callers: a.py, b.py, c.py" in text and "── reply [" in text
        # run 에이전트는 회신 뒤 내려왔다 — 되살릴 대상이 아니다
        (tm,) = s["reg"]._agents.values()
        assert wait_until(lambda: tm.state == "dead") and tm.revivable is False
        assert tm.ends_after_task and tm.handled == 1

    def test_reply_comes_without_explicit_reply_as_the_run_summary(self, session):
        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-B summarize"),
                    call("wait"),
                    complete_all("ok"),
                ],
                # `reply` 없이 끝내면 빚 독촉이 되돌린다 — 예산이 다할 때까지 같은 답
                "TASK-B": [call("complete", result="the summary only")] * 9,
            }
        )
        s = session(p)
        assert s["run"]().success
        text = contents(s["ctx"])
        assert "(no explicit reply — this is the agent's run summary)" in text
        assert "the summary only" in text

    def test_complete_with_an_awaited_reply_is_turned_back(self, session):
        """기다리는 회신이 남으면 `complete` 는 되돌아온다(장부 예산) — 꼬리와 되돌림 관찰에
        "by arrival" 절이 선다."""
        gate = threading.Event()

        def slow_reply(msgs):
            gate.wait(10)
            return call("reply", id=owed_id(msgs), text="late answer")

        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-C slow"),
                    complete_all("premature"),
                    lambda msgs: (gate.set(), call("wait"))[1],
                    complete_all("final"),
                ],
                "TASK-C": [slow_reply, call("complete", result="x")],
            }
        )
        s = session(p)
        result = s["run"]()
        assert result.success and result.output.startswith("final")
        text = contents(s["ctx"])
        assert "your result was delivered, but 1 item is still open" in text
        assert (
            "2. by arrival — `wait`; each reply closes its item when it comes" in text
        )
        assert "reply awaited:" in text
        assert "late answer" in text

    def test_budget_exhausted_closes_with_the_replies_not_yet_arrived(self, session):
        gate = threading.Event()

        def blocked(msgs):
            gate.wait(20)
            return call("complete", result="never mind")

        p = Provider(
            {
                "MAIN": [call("agent", mode="run", task="TASK-D forever")]
                + [complete_all("done?")] * 9,
                "TASK-D": [blocked],
            }
        )
        s = session(p)
        try:
            result = s["run"]()
        finally:
            gate.set()
        assert result.success
        assert "repl" in result.output and "had not arrived when this run closed" in (
            result.output
        )
        assert re.search(
            r"\[r-\d+\] \(agt-[0-9a-f]+\) \"TASK-D forever\"", result.output
        )

    def test_parallel_runs_each_reply_wakes_main(self, session):
        holder: dict = {}

        def until_both(msgs):
            seen = contents_of(msgs)
            if "E-reply" in seen and "F-reply" in seen:
                return complete_all("both")(msgs)
            return call("wait")

        p = Provider(
            {
                "MAIN": [
                    json.dumps(
                        [
                            {"action": "agent", "mode": "run", "task": "TASK-E one"},
                            {"action": "agent", "mode": "run", "task": "TASK-F two"},
                        ]
                    ),
                    until_both,
                    until_both,
                    until_both,
                    until_both,
                ],
                # 회신은 main 이 제자리에서 기다리는 동안에만 — 순서·시각에 기대지 않는다
                "TASK-E": reply_after_main_waits(holder, "E-reply"),
                "TASK-F": reply_after_main_waits(holder, "F-reply"),
            }
        )
        s = session(p)
        holder["reg"] = s["reg"]
        result = s["run"]()
        assert result.success and result.output.startswith("both")
        assert not getattr(result, "waiting", False)
        text = contents(s["ctx"])
        assert "E-reply" in text and "F-reply" in text
        assert len(s["reg"]._agents) == 2


def slow_step(i: int):
    """성공하는 서로 다른 호출 하나(조금 느리게) — 실패나 같은 호출 반복으로 스스로 끝나지 않게."""

    def step(messages):
        time.sleep(0.05)
        return call("shell", command=f"ls -d . >/dev/null # step {i}")

    return step


def contents_of(messages) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages)


class TestWaitInPlaceWakes:
    def test_user_input_wakes_main_who_steers_the_agent_and_waits_again(self, session):
        """시안 B: 대기 중 사람이 입력 → main 이 깬다(같은 런) → `message` 통지로 지시를
        바꾼다 → 다시 기다린다 → 회신."""
        steered = threading.Event()
        holder: dict = {}

        def agent_step(msgs):
            # 지시가 바뀔 때까지 붙잡는다 — 흡수된 통지가 이 런의 메시지로 들어온다
            if "Scope change" not in contents_of(msgs):
                time.sleep(0.05)
                # 매번 다른 파일 — 같은 호출 반복은 제자리 고리 확인(StaleGuard)이 멈춘다
                return call("shell", command=f"ls -d . >/dev/null # step {len(msgs)}")
            if steered.is_set():
                return call("complete", result="replied")
            # 회신은 main 이 **다시** 제자리 대기에 든 뒤에 — 그 전에 오면 main 의 다음 경계가 회신을 먼저
            # 읽고, 아직 내려오지 않은 이 에이전트를 기다릴 근거로 삼아 `wait` 가 런을 끝낸다(느린 CI 에서
            # 실제로 났다). 첫 대기의 체크인은 사람 입력에 깨며 풀렸으니 이 참은 둘째 대기의 것이다.
            assert wait_until(lambda: main_waits_in_place(s["reg"]))
            steered.set()
            return call("reply", id=owed_id(msgs), text="only src/ callers: 2")

        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-G callers incl tests"),
                    call("wait"),
                    lambda msgs: call(
                        "message",
                        to=holder["key"],
                        text="Scope change: ignore tests/",
                        expects_reply=False,
                    ),
                    call("wait"),
                    call(
                        "complete",
                        result="steered",
                        answers=["u-0", holder.get("u", "")],
                    ),
                ],
                "TASK-G": [agent_step] * 40,
            }
        )
        s = session(p)

        def interject():
            assert wait_until(lambda: len(s["reg"]._agents) == 1)
            holder["key"] = next(iter(s["reg"]._agents))
            # main 이 제자리 대기에 들어간 뒤 사람이 친다
            assert wait_until(lambda: main_waits_in_place(s["reg"]))
            item = s["queue"].enqueue(None, "Skip tests/ — only src/ matters")
            holder["u"] = item["id"]

        t = threading.Thread(target=interject)
        t.start()
        result = s["run"]()
        t.join()
        assert result.success
        text = contents(s["ctx"])
        assert "the wait was interrupted — the user wrote to you" in text
        assert "Skip tests/ — only src/ matters" in text
        assert 'delivered as notice "n-' in text
        assert steered.is_set() and "only src/ callers: 2" in text
        # 한 런 안이다 — main 의 런은 하나, 사람 입력도 같은 런의 요청으로 들었다
        assert not getattr(result, "waiting", False)

    def test_checkin_wakes_the_in_place_wait(self, session):
        gate = threading.Event()

        def blocked(msgs):
            gate.wait(10)
            return call("reply", id=owed_id(msgs), text="after checkin")

        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-H slow"),
                    call("wait"),
                    lambda msgs: (gate.set(), call("wait"))[1],
                    # 회신이 올 때까지 기다린다 — 체크인이 한 번 더 와도 같은 자리로
                    *[
                        lambda msgs: (
                            complete_all("ok")(msgs)
                            if "after checkin" in contents_of(msgs)
                            else call("wait")
                        )
                    ]
                    * 6,
                ],
                "TASK-H": [blocked, call("complete", result="x")],
            }
        )
        s = session(p)
        # 체크인 타이머를 곧바로 — 간격은 바꾸지 않고 시계만 당긴다
        s["reg"].checkin_timer_factory = lambda interval, fn, args: threading.Timer(
            0.2, fn, args=args
        )
        result = s["run"]()
        assert result.success
        text = contents(s["ctx"])
        assert "⏰ Check-in after" in text and "after checkin" in text

    def test_an_agent_that_main_owes_gets_its_checkin(self, session):
        """main 이 run 에이전트에게 회신을 빚진 채 그 에이전트가 `wait` — 실제 타이머 스레드가 만든 체크인이
        에이전트에게 가고, 그 줄은 ``main · owes you a reply`` 하나다(걸음 수·kill 안내 없음). 종전엔 그 줄을
        그리다(`checkin_agent_lines`) 타이머 스레드가 IndexError 로 죽어 체크인이 오지 않았다(v10.146.3)."""
        seen: dict = {}

        def until_checkin(msgs):
            text = contents_of(msgs)
            if "⏰ Check-in" in text and "checkin" not in seen:
                seen["checkin"] = text[text.rindex("⏰ Check-in") :]
            return call("wait")

        def answer(msgs):
            assert wait_until(lambda: "checkin" in seen)
            return call("reply", id=owed_id(msgs), text="src/lexer.py")

        def relay(msgs):
            if "src/lexer.py" not in contents_of(msgs):
                return call("wait")
            return call("reply", id=owed_id(msgs), text="K: src/lexer.py")

        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-K ask main"),
                    call("wait"),
                    answer,
                    *[
                        lambda msgs: (
                            complete_all("ok")(msgs)
                            if "K: src/lexer.py" in contents_of(msgs)
                            else call("wait")
                        )
                    ]
                    * 6,
                ],
                "TASK-K": [
                    call("message", to="main", text="which file?"),
                    call("wait"),
                    until_checkin,
                    *[relay] * 6,
                    call("complete", result="k"),
                ],
            }
        )
        s = session(p)
        # 에이전트의 체크인 타이머만 곧바로 — main 의 것은 제 간격(런이 시작되면 풀린다)
        s["reg"].checkin_timer_factory = lambda interval, fn, args: threading.Timer(
            0.2 if args[0].startswith("agent:") else interval, fn, args=args
        )
        result = s["run"]()
        assert result.success
        assert "K: src/lexer.py" in contents(s["ctx"])
        head, line, closing = seen["checkin"].splitlines()[:3]
        assert head == "⏰ Check-in after 5m — still running:"
        assert line == "main · owes you a reply"
        assert closing == (
            "If they are making progress, call `wait` again; if nothing reports, you "
            "are woken again in 10m."
        )

    def test_wait_without_awaited_reply_still_ends_the_run(self, session, tmp_path):
        """작업·모니터만 기다리면 지금처럼 런을 끝낸다(범위 밖)."""
        from agent_cli.jobs import JobRegistry
        from agent_cli.session_slots import set_job_registry

        p = Provider({"MAIN": [call("wait")]})
        s = session(p)
        jobs = JobRegistry(tmp_path)
        jobs.deliver = s["reg"].deliver
        set_job_registry(jobs)
        try:
            jobs.start("sleep 5", owner="main")
            result = s["run"]()
            assert getattr(result, "waiting", False) is True
        finally:
            jobs.stop_all()
            set_job_registry(None)

    def test_stop_during_the_wait_ends_main_only(self, session):
        """방 ■ 는 main 만 (v10.142.0, 사용자 결정) — run 은 계속 일하고 자기 ■ 로 멈춘다."""
        stop = threading.Event()
        p = Provider(
            {
                "MAIN": [call("agent", mode="run", task="TASK-I loop"), call("wait")],
                "TASK-I": [slow_step(i) for i in range(400)],
            }
        )
        s = session(p)

        def stop_when_waiting():
            assert wait_until(lambda: main_waits_in_place(s["reg"]))
            stop.set()  # 방 ■ — main 이 제자리에서 기다리는 중에

        threading.Thread(target=stop_when_waiting, daemon=True).start()
        result = s["run"](stop_event=stop)
        assert not result.success  # main 의 런은 interrupt 로 끝났다
        (tm,) = s["reg"]._agents.values()
        assert tm.state != "dead" and not tm.stop_event.is_set()  # run 은 계속
        assert s["reg"].stop_run(tm.key) == ""
        assert wait_until(lambda: tm.state == "dead")


class TestAgentToAgent:
    def test_agent_runs_an_agent_and_relays_the_reply_to_main(self, session):
        """agent←run: 에이전트가 run 을 부르고(깊이 +1), 그 회신을 받아 자기 과제의 회신을 낸다."""
        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-OUTER delegate inward"),
                    call("wait"),
                    complete_all("chain done"),
                ],
                "TASK-OUTER": [
                    call("agent", mode="run", task="TASK-INNER do it"),
                    call("wait"),
                    lambda msgs: call(
                        "reply",
                        id=owed_id(msgs),
                        text="outer relays: "
                        + ("inner-42" if "inner-42" in contents_of(msgs) else "?"),
                    ),
                    call("complete", result="outer"),
                ],
                "TASK-INNER": reply_then_complete("inner-42"),
            }
        )
        s = session(p, max_depth=3)
        result = s["run"]()
        assert result.success
        assert "outer relays: inner-42" in contents(s["ctx"])
        depths = sorted(
            (tm.caller_runtime or {}).get("depth", 0)
            for tm in s["reg"]._agents.values()
        )
        assert depths == [0, 1]  # main 이 띄운 것, 에이전트가 띄운 것

    def test_run_agent_is_a_message_target_and_listed(self, session):
        """run 에이전트도 명단·주소에 있다 — `message` 상대(이름·키), status 에 run 표지."""
        from agent_cli.prompts.system_prompt import build_live_agents_section

        gate = threading.Event()
        p = Provider(
            {
                "MAIN": [call("agent", mode="run", task="TASK-J hold")],
                "TASK-J": [lambda m: (gate.wait(10), call("complete", result="x"))[1]],
            }
        )
        s = session(p)
        threading.Thread(target=s["run"], daemon=True).start()
        reg = s["reg"]
        assert wait_until(lambda: len(reg._agents) == 1)
        key = next(iter(reg._agents))
        try:
            assert reg.resolve_key(key) == key
            assert "[anon · run]" in reg.format_status()
            roster = build_live_agents_section(reg, include_state=True)
            assert f"`{key}` (run)" in roster
            # 사람의 채널 직접 입력(웹 `/api/agent/<key>/input`·`@agt-` 와 같은 길)
            assert reg.request(key, "hi from the window", author="user:ana") == ""
        finally:
            gate.set()


class TestComeDown:
    """run 에이전트는 런이 `complete` 로 끝날 때 내려온다 (사용자 결정 2026-11-15) — `wait` 로 끝난
    런은 깨어 계속하고, 런 중간의 `reply` 는 끝이 아니다."""

    def test_run_agent_stays_alive_after_its_reply_until_it_completes(self, session):
        gate = threading.Event()
        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-N reply early"),
                    call("wait"),
                    complete_all("ok"),
                ],
                "TASK-N": [
                    lambda msgs: call("reply", id=owed_id(msgs), text="N early answer"),
                    lambda msgs: (gate.wait(10), call("complete", result="n"))[1],
                ],
            }
        )
        s = session(p)
        reg = s["reg"]
        th = threading.Thread(target=s["run"], daemon=True)
        th.start()
        assert wait_until(lambda: len(reg._agents) == 1)
        (n,) = reg._agents.values()
        # 회신은 나갔다(main 이 받았다) — 그래도 complete 전이라 살아서 일하는 중
        assert wait_until(lambda: "N early answer" in contents(s["ctx"]))
        assert n.state == "busy" and n.worker.is_alive()
        gate.set()
        th.join(10)
        assert wait_until(lambda: n.state == "dead") and n.revivable is False

    def test_run_agent_that_waits_stays_and_comes_down_at_complete(self, session):
        gate = threading.Event()
        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-K two step"),
                    call("wait"),
                    complete_all("ok"),
                ],
                # 첫 런: 하위 run 을 띄우고 wait 로 끝냄 — 그 회신에 깨어 갚고 complete
                "TASK-K": [
                    call("agent", mode="run", task="TASK-L leaf"),
                    call("wait"),
                    lambda msgs: call("reply", id=owed_id(msgs), text="K done"),
                    call("complete", result="k"),
                ],
                "TASK-L": [
                    lambda msgs: (
                        gate.wait(10),
                        call("reply", id=owed_id(msgs), text="L"),
                    )[1],
                    call("complete", result="l"),
                ],
            }
        )
        s = session(p, max_depth=3)
        reg = s["reg"]
        th = threading.Thread(target=s["run"], daemon=True)
        th.start()
        assert wait_until(lambda: len(reg._agents) == 2)
        k = next(t for t in reg._agents.values() if "TASK-K" in t.description)
        # K 의 런은 wait 로 끝났다 — 내려오지 않고 받은편지함을 기다린다
        assert wait_until(lambda: k.state == "idle")
        assert k.worker.is_alive() and k.revivable is True
        gate.set()
        th.join(10)
        assert wait_until(lambda: k.state == "dead") and k.revivable is False
        assert "K done" in contents(s["ctx"])


class TestRepliesAndNoticesAreNotItems:
    """사용자 결정 (v10.141.0): 회신(명시 `reply`·런 끝 폴백)과 통지는 장부 항목을 만들지 않는다 —
    회신은 기다리던 항목을 닫고 내용만 전한다. 요청만 받는 쪽에 빚을 만든다. 그 밖의 메일 종류는
    그대로(tests/test_ledger_mail.py)."""

    def _main_completes_once(self, p, s):
        result = s["run"]()
        assert result.success and "not closed" not in result.output
        assert "had not arrived" not in result.output
        text = contents(s["ctx"])
        assert "still open" not in text  # complete 가 한 번도 되돌아오지 않았다
        assert "(mail)" not in text  # 메일 항목 줄이 꼬리·되돌림 어디에도 없다
        return result

    def test_explicit_reply_closes_the_wait_and_adds_no_item(self, session):
        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-P answer"),
                    call("wait"),
                    # 회신의 id 를 answers 에 넣지 않는다 — 넣을 항목이 없다
                    call("complete", result="ok", answers=["u-0"]),
                ],
                "TASK-P": reply_then_complete("P-ANSWER"),
            }
        )
        s = session(p)
        self._main_completes_once(p, s)
        assert "P-ANSWER" in contents(s["ctx"])  # 내용은 그대로 전해진다
        assert s["reg"].awaited_replies("main") == []
        assert len(p.calls["MAIN"]) == 3

    def test_fallback_summary_reply_is_the_same(self, session):
        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-Q summary"),
                    call("wait"),
                    call("complete", result="ok", answers=["u-0"]),
                ],
                "TASK-Q": [call("complete", result="Q-SUMMARY")] * 9,
            }
        )
        s = session(p)
        self._main_completes_once(p, s)
        text = contents(s["ctx"])
        assert "(no explicit reply — this is the agent's run summary)" in text
        assert "Q-SUMMARY" in text

    def test_notice_to_main_adds_no_item(self, session):
        """통지는 읽으면 끝 — main 의 꼬리에 항목이 서지 않고 `complete` 는 사람 요청만 닫는다.
        (빚진 요청자에게 보낸 통지는 그 빚을 갚는다 — 그건 종전 규칙이라 여기선 동료가 보낸다.)"""
        p = Provider({"MAIN": [call("complete", result="ok", answers=["u-0"])]})
        s = session(p)
        reg = s["reg"]
        key, _ = reg.spawn()
        out, err = reg.message_handler(f"agent:{key}")(
            "main", "S-FYI halfway", expects_reply=False
        )
        assert err == "" and out.startswith('delivered as notice "n-')
        self._main_completes_once(p, s)
        assert "── notice [" in contents(s["ctx"])
        first_tail = str(p.calls["MAIN"][0][-1]["content"])
        assert "## Open Items" in first_tail and "(mail)" not in first_tail
        assert len(p.calls["MAIN"]) == 1

    def test_request_to_main_is_still_a_debt(self, session):
        """요청(`expects_reply` true)만 받는 쪽에 빚을 만든다 — main 의 꼬리에 `reply owed`."""
        p = Provider(
            {
                "MAIN": [
                    lambda msgs: call(
                        "reply",
                        id=re.search(
                            r'`reply` with `id` "(r-\d+)"', contents_of(msgs)
                        ).group(1),
                        text="use src/ only",
                    ),
                    call("complete", result="ok", answers=["u-0"]),
                ],
            }
        )
        s = session(p)
        reg = s["reg"]
        key, _ = reg.spawn()
        out, err = reg.message_handler(f"agent:{key}")("main", "T-QUESTION which dirs?")
        assert err == "" and out.startswith('delivered as request "r-')
        result = s["run"]()
        assert result.success
        first_tail = str(p.calls["MAIN"][0][-1]["content"])
        assert "reply owed:" in first_tail and "T-QUESTION" in first_tail
        assert reg.reply_debts("main") == []  # 갚았다

    def test_agent_runs_started_by_a_reply_or_notice_carry_no_items(self, tmp_path):
        """에이전트(run·spawn)도 같다 — 회신·통지로 시작한 런엔 장부 요청이 없다."""
        from tests.test_agents_live import make_registry

        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        from agent_cli.subagent.resident_loop import ResidentLoop

        kind = ResidentLoop(reg.get(key), reg, disp="anon")
        for item in (
            {
                "id": "n-9",
                "text": "x",
                "author": "main",
                "expects_reply": False,
                "kind": "reply",
                "re": "r-1",
            },
            {
                "id": "n-10",
                "text": "y",
                "author": "main",
                "expects_reply": False,
                "kind": "notice",
            },
        ):
            start = kind.start([item])
            assert start.user_requests == [] and start.request_id == ""
        reg.shutdown_all()


class TestResume:
    def _entry(self, key, **kw):
        base = {
            "key": key,
            "profile": "",
            "name": "",
            "description": "",
            "instructions": "",
            "role_prompt": "",
            "allowed_tools": None,
            "model": "",
            "hooks_config": None,
            "context_mode": "none",
            "created_at": 1.0,
            "handled": 0,
            "state": "idle",
            "error": "",
            "inbox": [],
            "in_flight": [],
        }
        base.update(kw)
        return base

    def _write(self, sdir, agents):
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "agents.json").write_text(
            json.dumps(
                {"version": 2, "agents": agents, "pending": [], "questions": []}
            ),
            encoding="utf-8",
        )

    def test_unfinished_run_agent_is_revived_and_finishes_its_reply(self, tmp_path):
        from tests.test_agents_live import make_runner

        sdir = tmp_path / "s"
        task = {
            "id": "r-7",
            "text": "TASK-R recount",
            "author": "main",
            "expects_reply": True,
        }
        self._write(
            sdir,
            [
                self._entry(
                    "agt-run1", ends_after_task=True, task_id="r-7", in_flight=[task]
                )
            ],
        )
        reg = AgentRegistry(sdir, runner=make_runner("recounted"))
        try:
            assert reg.restore() == 1
            tm = reg.get("agt-run1")
            assert tm.ends_after_task and tm.task_id != "r-7"  # 새 id 로 다시 들어갔다
            assert wait_until(lambda: reg.arrived_for("main"))
            (mail,) = absorbed_mail(reg)
            assert "recounted" in mail["output"] and mail["re"] == tm.task_id
            assert wait_until(lambda: tm.state == "dead") and tm.revivable is False
        finally:
            reg.shutdown_all()

    def test_requester_gone_routes_the_reply_to_main(self, tmp_path):
        from tests.test_agents_live import make_runner

        sdir = tmp_path / "s"
        task = {
            "id": "r-9",
            "text": "TASK-S orphan",
            "author": "agent:agt-gone",
            "expects_reply": True,
        }
        self._write(
            sdir,
            [
                self._entry("agt-gone", state="dead"),
                self._entry(
                    "agt-run2", ends_after_task=True, task_id="r-9", in_flight=[task]
                ),
            ],
        )
        reg = AgentRegistry(sdir, runner=make_runner("orphan done"))
        try:
            assert reg.restore() == 1
            assert wait_until(lambda: reg.arrived_for("main"))
            (mail,) = absorbed_mail(reg)
            assert "orphan done" in mail["output"] and mail["key"] == "agt-run2"
        finally:
            reg.shutdown_all()

    def test_old_p1_file_with_run_dir_home_reads_as_a_tombstone(self, tmp_path):
        """하위 호환: v10.140.0 의 agents.json(run 에이전트 = ``home`` 이 ``run_…``, dead)."""
        sdir = tmp_path / "s"
        self._write(
            sdir,
            [
                self._entry(
                    "agt-old",
                    ends_after_task=True,
                    home="run_task_ab12_20260101T000000000",
                    state="dead",
                )
            ],
        )
        reg = AgentRegistry(sdir)
        assert reg.restore() == 0
        tm = reg.get("agt-old")
        assert tm.state == "dead" and tm.ends_after_task
        assert tm.home_dir == sdir / "run_task_ab12_20260101T000000000"

    def test_hooks_survive_the_round_trip(self, tmp_path):
        """에이전트의 훅은 agents.json 에 JSON 으로 남고 되살 때 객체로 돌아온다 (v10.141.0 —
        종전엔 객체를 그대로 넘겨 저장이 TypeError 로 깨졌다)."""
        from agent_cli.hooks.shell import HookEntry, HookMatcher

        sdir = tmp_path / "s"
        reg = AgentRegistry(sdir, runtime={"hooks_config": None})
        reg.restore()
        key, err = reg.spawn(
            runtime={
                "hooks_config": {
                    "PreToolUse": [HookMatcher(matcher="", hooks=[HookEntry("echo x")])]
                }
            }
        )
        assert not err
        reg.shutdown_all()
        data = json.loads((sdir / "agents.json").read_text())
        assert data["agents"][0]["hooks_config"] == {
            "PreToolUse": [
                {"matcher": "", "hooks": [{"command": "echo x", "timeout": 30}]}
            ]
        }
        reg2 = AgentRegistry(sdir)
        reg2.restore()
        try:
            hooks = reg2.get(key).hooks_config
            assert isinstance(hooks["PreToolUse"][0], HookMatcher)
        finally:
            reg2.shutdown_all()


class TestAuditCoversTheWaitedRun:
    def test_reply_inside_the_in_place_wait_is_in_the_audited_range(self, session):
        from agent_cli.harness.auditor import SessionAuditor

        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-M audit me"),
                    call("wait"),
                    complete_all("ok"),
                ],
                "TASK-M": reply_then_complete("M-REPLY-TEXT"),
            }
        )
        s = session(p)

        class _Jobs:
            stopped = False
            in_flight = 0

            def __init__(self):
                self.runs = []

            def run(self, job, label, text):
                self.runs.append(label)
                return ""

        jobs = _Jobs()
        auditor = SessionAuditor(jobs, s["ctx"], enabled=True)
        s["run"]()
        start = Start(
            query="MAIN go",
            author_is_user=True,
            request_id="u-0",
            stop_event=threading.Event(),
        )
        assert auditor.run_ended(start, ok=True)
        a, b = (int(x) for x in jobs.runs[0].split("-"))
        lines = s["ctx"].history_path.read_text(encoding="utf-8").splitlines()
        seq = next(i + 1 for i, ln in enumerate(lines) if "M-REPLY-TEXT" in ln)
        assert a <= seq < b  # 회신이 같은 사용자 런 안에 든다


class TestNoHiddenSideEffects:
    def test_awaited_replies_reads_only(self, tmp_path):
        """장부가 매 턴 부르는 조회는 저장·로스터·id 할당을 건드리지 않는다."""
        from agent_cli import item_ids
        from tests.test_agents_live import make_registry

        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        assert wait_until(lambda: reg.get(key).state == "idle")
        saves = []
        reg._save_state = lambda: saves.append(1)  # type: ignore[method-assign]
        before = item_ids.next_id(item_ids.MAIL)
        assert reg.awaited_replies("main") == []
        assert reg.question_port(None).awaited() == []
        after = item_ids.next_id(item_ids.MAIL)
        assert (
            saves == [] and item_ids.number_of(after) == item_ids.number_of(before) + 1
        )
        reg.shutdown_all()

    def test_input_queue_wait_for_arrival_does_not_dequeue(self):
        q = MainInput()
        assert q.wait_for_arrival(0.01) is False
        q.enqueue(None, "hello")
        assert q.wait_for_arrival(0.01) is True
        assert q.pending_count() == 1  # 꺼내지 않았다
        q.close()
        assert q.wait_for_arrival(0.01) is False

    def test_input_queue_wait_for_arrival_ignores_mail(self):
        from agent_cli.post import envelope as post_envelope

        q = MainInput()
        q.post.deliver(
            post_envelope.notice("n-1", "job", "main", "done")
        )  # 메일은 세지 않는다
        assert q.wait_for_arrival(0.01) is False
        q.enqueue(None, "hello")
        assert q.wait_for_arrival(0.01) is True
        assert q.pending_count() == 1  # 꺼내지 않았다
