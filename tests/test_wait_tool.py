"""`wait` 도구 (v10.65.0, docs/wait/DESIGN.md) — 닫지 않고 끝낸다, 깨울 것이 있을 때만.

고정하는 계약: 자리(complete 바로 앞)·터미널(문법도) · 깨울 것이 있으면 런이
``waiting`` 결과로 끝나고 열린 요청이 다음 런의 장부로 넘어간다 · 없으면 거부
관찰 + 런 계속 · 기다리려고 쓴 complete 되돌림에 `wait` 안내 · 안내 문구들이
전부 `wait` 를 가리킨다(`complete` 로 기다리라는 문장 0).
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock

import pytest

from agent_cli.context.manager import _OBS_COMPLETE_NUDGE, ContextManager
from agent_cli.dialects import get as get_dialect
from agent_cli.dialects.grammar import terminal_tools
from agent_cli.hooks.events import PRE_WAIT
from agent_cli.hooks.runner import HookRunner
from agent_cli.jobs import JobRegistry
from agent_cli.loop import dispatch as dispatch_mod
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.render import get_renderer, set_renderer
from agent_cli.session_slots import (
    get_agent_registry,
    set_agent_registry,
    set_job_registry,
)
from agent_cli.tools.registry import _BUILTIN_ORDER, TOOLS
from agent_cli.tools.virtual import WaitTool
from agent_cli.wake import Mailbox, WaitingOn, _short_wake_name, parked_debtors
from tests.loop_ports import TEST_PORTS, make_ports, queue_inbox
from tests.loopkit import make_loop
from tests.mail_support import absorbed_mail, agent_wake_lines

#: main 모양의 포트 — 입력 큐가 있어 깨울 수 있다(빈 큐).
WAKEABLE = make_ports(inbox=queue_inbox(lambda: None))


@pytest.fixture
def jobs(tmp_path):
    r = JobRegistry(tmp_path)
    r.deliver = lambda *a, **k: None
    set_job_registry(r)
    yield r
    r.stop_all()
    set_job_registry(None)


def _loop(provider, *, ctx=None, query="Q", rid="u-1"):
    return make_loop(
        ports=WAKEABLE,
        query=query,
        query_request_id=rid,
        provider=provider,
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        dialect=get_dialect("json_fc"),
        ctx=ctx,
    )


def _recording_hooks(tmp_path):
    """`PreWait` 를 받으면 턴을 한 줄 적는 Python 훅 하나 — ``(러너, 기록 파일)``."""
    d = tmp_path / "hooks"
    d.mkdir()
    log = tmp_path / "hook.log"
    (d / "00_rec.py").write_text(
        'EVENTS = ["PreWait"]\n'
        "def pre_wait(ctx):\n"
        f"    with open({str(log)!r}, 'a') as f:\n"
        "        f.write(f'{ctx.event} {ctx.turn}\\n')\n",
        encoding="utf-8",
    )
    return HookRunner(hook_dirs=[d]), log


class _WaitLineSpy:
    """렌더러 대리 — `wait_in_place` 의 인자만 적고 나머지는 진짜 렌더러에 넘긴다."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list = []

    def wait_in_place(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _DecidedMailbox(Mailbox):
    """받아들일 때 무엇을 기다리기로 했나(``on``)를 돌려주고, 그 뒤에 다시 물으면 다른 답(회신이 이미 닫힘)을 주는
    우편 창 — 대기 줄·체크인이 받아들일 때의 값 하나에서 나오는지 본다(5c-1 4b). 기다림은 곧 메일로 깬다."""

    def __init__(self, on: WaitingOn):
        super().__init__(None, "main", take=list)
        self._on = on
        self.checkin_ids: list = []

    def accept_wait(self):
        return self._on

    def awaited(self):
        return []  # 받아들인 뒤에 닫혔다 — 다시 읽으면 빈 목록

    def wake_sources(self):
        return []

    def next_checkin_s(self, ids):
        self.checkin_ids.append(("next", ids))
        return 300

    def arm_checkin(self, ids):
        self.checkin_ids.append(("arm", ids))

    def wait(self, *, stop_event, still_waiting):
        return "mail"


def _provider(*replies: dict):
    p = MagicMock()
    p.call.side_effect = [LLMResponse(content=json.dumps(r)) for r in replies]
    return p


class TestRegistration:
    def test_right_before_complete_and_terminal(self):
        order = list(_BUILTIN_ORDER)
        # v10.109.0: `tool_help` 가 `complete` 바로 앞에 끼었다 — wait 는 그 앞
        assert order[-3:] == ["wait", "tool_help", "complete"]
        assert isinstance(TOOLS["wait"], WaitTool) and TOOLS["wait"].terminal
        assert "wait" in terminal_tools(["wait", "complete", "shell"])

    def test_complete_points_at_wait(self):
        assert "call `wait`" in TOOLS["complete"].description

    def test_complete_says_the_user_sees_markdown(self):
        # v10.96.1: 사용자에게는 마크다운으로 그려진다(CLI rich·웹 final 카드) — 사용자
        # 요청 2026-11-01. 호출한 에이전트는 원문을 받으니 "사용자" 로 한정한다.
        d = TOOLS["complete"].description
        assert "The user sees it rendered as Markdown." in d

    def test_complete_says_result_is_the_delivered_answer(self):
        # v10.75.0: result 가 답으로 전달되는 유일한 글 — 답 먼저, 근거는 고정 틀
        # 두 줄. 추론 과정 전체를 요구하지 않는다(사용자 결정).
        d = TOOLS["complete"].description
        assert "`result` is the only text delivered as your answer" in d
        assert "the agent that called you" in d
        assert "\nChecked: <" in d and "\nNot checked: <" in d
        assert "nothing to check has neither line" in d
        assert "reasoning" not in d
        r = TOOLS["complete"].parameters["properties"]["result"]["description"]
        assert "Checked / Not checked" in r
        # v10.76.0: 정직성 규칙은 이 틀이 complete 순간에 맡는다 — Task Guidelines 엔 없다
        from agent_cli.prompts.system_prompt import TASK_GUIDELINES

        assert "Report outcomes honestly" not in TASK_GUIDELINES


class TestAdvertisedOnlyWhereAWakeExists:
    """스킬·일회성 루프엔 깨울 길이 없다 — 거기서 wait 로 끝낸 결과는 호출자에게
    "기다리는 중" 으로 돌아가 버린다."""

    def _tools(self, ports):
        loop = _loop(MagicMock())
        loop = make_loop(
            ports=ports,
            query="Q",
            provider=MagicMock(),
            capabilities=loop.capabilities,
            model="m",
            dialect=get_dialect("json_fc"),
        )
        return list(loop.tools_list)

    def test_unwired_ports_hide_wait(self):
        assert "wait" not in self._tools(TEST_PORTS)
        assert not TEST_PORTS.wakeable()

    def test_main_queue_and_resident_inbox_show_wait(self):
        assert "wait" in self._tools(WAKEABLE)
        assert "wait" in self._tools(make_ports(inbox=list))


@pytest.mark.parametrize(
    ("wake_id", "name"),
    [
        ("job:1", "job #1"),
        ("mon:mon-504d33", "monitor mon-504d33"),
        ("schedule", "a schedule"),
        ("agent:agt-3", ""),  # 회신은 기다리는 회신이 말한다
    ],
)
def test_wait_line_short_names(wake_id, name):
    """대기 줄의 짧은 이름(5c-1 결정 C) — `wake_items` 의 id 에서."""
    assert _short_wake_name(wake_id) == name


class _Boom(RuntimeError):
    pass


class _RaisingLineSpy(_WaitLineSpy):
    """대기 줄을 그릴 때(``True``) 터지는 렌더러 — 지울 때(``False``)는 적기만 한다."""

    def wait_in_place(self, *args, **kwargs):
        super().wait_in_place(*args, **kwargs)
        if args and args[0] is True:
            raise _Boom("drawing the wait line failed")


@pytest.mark.parametrize(
    "where", ["observation", "pre_wait", "wait_line", "arm_checkin"]
)
def test_an_error_after_the_accept_leaves_no_mark_timer_or_line(
    tmp_path, monkeypatch, where
):
    """받아들인 뒤 기다리기 전 어디서 예외가 나도(수락 문장 기록·`PreWait`·대기 줄 그리기·`arm_checkin`) 기다림 표시·
    체크인 타이머·대기 줄이 남지 않는다 — `_op_wait` 의 ``finally`` 하나가 내린다(5e1, 오라클 s42). 예외는 런 밖으로
    나간다(엔진은 삼키지 않는다)."""
    from tests.test_agents_live import make_registry

    reg = make_registry(tmp_path / "reg")
    set_agent_registry(reg)
    jobs = JobRegistry(tmp_path / "jobs")
    jobs.deliver = reg.deliver
    set_job_registry(jobs)
    spy = (_RaisingLineSpy if where == "wait_line" else _WaitLineSpy)(get_renderer())
    set_renderer(spy)
    loop = make_loop(
        ports=make_ports(inbox=queue_inbox(lambda: None), agent_registry=reg),
        query="Q",
        query_request_id="u-1",
        provider=_provider({"action": "wait"}),
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        dialect=get_dialect("json_fc"),
    )
    if where == "observation":
        real = dispatch_mod._append_observation

        def append(messages, ctx, dialect, record, obs, **kw):
            if str(obs).startswith("Observation: waiting"):
                raise _Boom("recording the accept failed")
            return real(messages, ctx, dialect, record, obs, **kw)

        monkeypatch.setattr(dispatch_mod, "_append_observation", append)
    elif where == "pre_wait":
        real_fire = loop._dispatch.tools.fire_hook

        def fire(event, **kw):
            if event == PRE_WAIT:
                raise _Boom("the PreWait hook failed")
            return real_fire(event, **kw)

        monkeypatch.setattr(loop._dispatch.tools, "fire_hook", fire)
    elif where == "arm_checkin":

        def arm(owner, ids):
            raise _Boom("arming the check-in failed")

        monkeypatch.setattr(reg, "arm_checkin", arm)
    try:
        jobs.start("sleep 30", owner="main")
        with pytest.raises(_Boom):
            loop.run()
        assert reg._post.is_waiting("main") is False  # 표시가 내려갔다
        assert "main" not in reg._checkin_armed  # 타이머가 없다
        drawn = [c for c in spy.calls if c[0][0] is True]
        assert spy.calls[-1] == ((False, []), {})  # 대기 줄을 지웠다(그렸든 아니든)
        assert len(drawn) == (1 if where in ("wait_line", "arm_checkin") else 0)
    finally:
        set_renderer(spy._inner)
        jobs.stop_all()
        set_job_registry(None)
        set_agent_registry(None)
        reg.shutdown_all()


class TestWaitStaysInTheRun:
    """`wait` 는 런을 끝내지 않는다(5c-1) — 받아들여지면 그 자리에서 깨울 것을 기다리고, 깨면 같은 런이 같은 장부로
    잇는다(종전: 런을 `waiting` 결과로 끝내고 열린 요청을 다음 런으로 넘겼다 — `park_requests`)."""

    def test_waiting_on_a_job_keeps_the_items_in_the_same_run(self, tmp_path):
        from agent_cli.session_slots import set_agent_registry
        from tests.test_agents_live import make_registry, wait_until

        reg = make_registry(tmp_path / "reg")
        set_agent_registry(reg)
        jobs = JobRegistry(tmp_path / "jobs")
        jobs.deliver = reg.deliver
        set_job_registry(jobs)
        flag = tmp_path / "go"
        ctx = ContextManager(tmp_path / "s", max_context_tokens=10000)
        seen: dict = {}

        def _answer(*_a, **_k):
            if p.call.call_count == 1:
                return LLMResponse(
                    content=json.dumps({"action": "wait", "reason": "install running"})
                )
            seen["ids"] = list(loop._state.ledger.request_ids())
            return LLMResponse(
                content=json.dumps(
                    {"action": "complete", "result": "done", "answers": seen["ids"]}
                )
            )

        p = MagicMock()
        p.call.side_effect = _answer
        runner, hook_log = _recording_hooks(tmp_path)
        spy = _WaitLineSpy(get_renderer())
        set_renderer(spy)
        loop = make_loop(
            ports=make_ports(
                inbox=queue_inbox(lambda: None), agent_registry=reg, hook_runner=runner
            ),
            query="Q",
            query_request_id="u-1",
            provider=p,
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            dialect=get_dialect("json_fc"),
            ctx=ctx,
        )
        try:
            jobs.start(f"while [ ! -f {flag} ]; do sleep 0.05; done", owner="main")
            done: list = []
            th = threading.Thread(target=lambda: done.append(loop.run()), daemon=True)
            th.start()
            assert wait_until(
                lambda: "main" in reg._checkin_armed
            )  # 제자리 대기에 들었다
            flag.write_text("", encoding="utf-8")
            th.join(10)
            (res,) = done
            # 같은 런의 둘째 호출이 같은 장부로 닫는다 — 요청 u-1 과 깨운 작업 통지(n-)
            assert res.success and res.output == "done"
            assert seen["ids"][0] == "u-1" and len(seen["ids"]) == 2
            assert seen["ids"][1].startswith("n-")
            assert loop.turn == 2
            recs = [
                json.loads(line)
                for line in (tmp_path / "s" / "history.jsonl").read_text().splitlines()
            ]
            obs = [r for r in recs if r.get("tool") == "wait"]
            assert obs and obs[0]["success"] is True and "job #1" in obs[0]["content"]
            assert "next run" not in obs[0]["content"]  # 넘길 다음 런이 없다
            # 받아들여진 `wait` 가 기다리기 직전 `PreWait` 한 번(5c-1 결정 K) — 그 턴의 값으로
            assert hook_log.read_text(encoding="utf-8") == "PreWait 1\n"
            # 대기 줄(결정 C): 회신이 없어도 그리고, 작업은 짧은 이름으로 — 깰 때 내린다
            assert spy.calls == [
                ((True, []), {"others": ["job #1"]}),
                ((False, []), {}),
            ]
        finally:
            set_renderer(spy._inner)
            jobs.stop_all()
            set_job_registry(None)
            set_agent_registry(None)
            reg.shutdown_all()

    def test_the_wait_line_and_the_checkin_come_from_the_accepted_value(self):
        """받아들여진 `wait` 하나는 무엇을 기다리기로 했나를 한 번 읽는다(5c-1 4b) — 수락 문장·대기 줄·체크인 대상이 그
        값 하나에서. 받아들인 뒤 회신이 닫혀도 대기 줄은 받아들일 때의 [r-2] 를 그리고(곧 그 회신에 깬다), 체크인은
        받아들일 때의 대상을 건다."""
        on = WaitingOn(
            items=(
                ("agent:agt-3", "agent:agt-3 working on your request"),
                ("job:1", "job #1 (`make`)"),
            ),
            awaited=({"id": "r-2", "from": "agent:agt-3", "text": "build"},),
        )
        mailbox = _DecidedMailbox(on)
        spy = _WaitLineSpy(get_renderer())
        set_renderer(spy)
        try:
            loop = make_loop(
                ports=make_ports(mailbox=mailbox),
                query="Q",
                query_request_id="u-1",
                provider=_provider(
                    {"action": "wait"},
                    {"action": "complete", "result": "ok", "answers": ["u-1"]},
                ),
                capabilities=ModelCapabilities(
                    context_window=32768,
                    max_output_tokens=4096,
                    supports_thinking=False,
                ),
                model="m",
                dialect=get_dialect("json_fc"),
            )
            assert loop.run().success
        finally:
            set_renderer(spy._inner)
        ids = frozenset({"agent:agt-3", "job:1"})
        assert spy.calls[0] == (
            (True, [{"id": "r-2", "from": "agent:agt-3", "text": "build"}]),
            {"others": ["job #1"]},
        )
        assert mailbox.checkin_ids == [("next", ids), ("arm", ids)]
        accepted = [
            str(m["content"])
            for m in loop.messages
            if "will wake on" in str(m.get("content"))
        ]
        assert accepted and (
            "will wake on: agent:agt-3 working on your request; job #1 (`make`), or in 5m"
            in accepted[0]
        )

    def test_nothing_pending_ends_the_run_without_pre_wait(self, tmp_path):
        set_job_registry(None)
        runner, hook_log = _recording_hooks(tmp_path)
        loop = make_loop(
            ports=make_ports(inbox=queue_inbox(lambda: None), hook_runner=runner),
            query="Q",
            query_request_id="u-1",
            provider=_provider(
                {"action": "wait"},
                {"action": "complete", "result": "ok", "answers": ["u-1"]},
            ),
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            dialect=get_dialect("json_fc"),
        )
        res = loop.run()
        # 깨울 것도 빚도 없다 — 받아들여 빈 결과의 성공으로 끝난다(5d). 기다리지 않으므로 `PreWait` 는 불리지 않는다
        assert (res.success, res.output) == (True, "")
        assert not hook_log.exists()
        obs = [
            m["content"]
            for m in loop.messages
            if m["role"] == "user" and "nothing to wait for" in str(m.get("content"))
        ]
        assert len(obs) == 1

    def test_agent_activity_counts_only_for_its_requester(self, tmp_path):
        """세션 전체 `has_agent_work` 로 보면 상주 에이전트 자신이 busy 라 늘 참이다
        — 요청자별로 센다: 그 주소에게 빚진 회신, 그 주소의 항목."""
        from tests.test_agents_live import make_registry

        reg = make_registry(tmp_path)
        reg.note_owes("agent:a", "main", "do X")
        assert agent_wake_lines(reg, "main") == ["agent:a owes you a reply"]
        assert agent_wake_lines(reg, "agent:a") == []  # 자기 빚은 자기를 깨우지 않는다
        assert agent_wake_lines(reg, "agent:b") == []

    def test_agent_scoped_jobs_do_not_wake_main(self, jobs):
        jobs.start("sleep 30", owner="agent:x")
        from agent_cli.wake import Mailbox

        assert Mailbox(get_agent_registry(), "main").wake_sources() == []
        assert Mailbox(get_agent_registry(), "agent:x").wake_sources() == [
            "job #1 (`sleep 30`)"
        ]


class TestAlreadyArrived:
    """기다리던 것이 wait 보다 먼저 끝난 경우 (v10.66.2) — 통지가 메일함에 와 있다.
    벤치 실측: once 모니터가 즉시 발화·은퇴한 뒤의 wait 가 "nothing pending" 으로
    거부됐다. 거부는 맞지만(런을 끝내면 바로 다시 깨울 뿐) 문구가 사실과 달랐다."""

    def _registry(self, tmp_path):
        from agent_cli.session_slots import set_agent_registry
        from tests.test_agents_live import make_registry

        reg = make_registry(tmp_path)
        set_agent_registry(reg)
        return reg

    def _monitor_mail(self, reg):
        assert (
            reg.deliver(
                "main",
                mail={"kind": "monitor", "success": True, "output": "🔔 hit"},
                text="🔔 hit",
                author="main",
                expects_reply=False,
            )
            == ""
        )

    def _mid_turn(self, reg, n, first):
        """모델 호출 **도중** 에 통지 n 건이 도착한다 — 런 시작·턴 경계에 와 있던
        메일은 모델이 보기 전에 배달되므로(`_deliver_agent_mail`), 틈은 호출
        중에만 생긴다(벤치: 모니터가 그 사이에 발화). 둘째 호출은 장부의 열린
        항목(요청 `u-1` + 배달된 통지 `n-…`)을 전부 닫는 complete."""
        p = MagicMock()
        holder: dict = {}

        def _call(*_a, **_k):
            if p.call.call_count == 1:
                for _ in range(n):
                    self._monitor_mail(reg)
                return LLMResponse(content=json.dumps(first))
            ids = list(holder["loop"]._state.ledger.request_ids())
            return LLMResponse(
                content=json.dumps(
                    {"action": "complete", "result": "ok", "answers": ids}
                )
            )

        p.call.side_effect = _call
        p.holder = holder
        return p

    def _loop(self, reg, provider):
        loop = provider.holder["loop"] = make_loop(
            ports=make_ports(inbox=queue_inbox(lambda: None), agent_registry=reg),
            query="Q",
            query_request_id="u-1",
            provider=provider,
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            dialect=get_dialect("json_fc"),
        )
        return loop

    def test_job_and_monitor_mail_count_as_arrived_not_as_wake_sources(self, tmp_path):
        from agent_cli.wake import Mailbox

        reg = self._registry(tmp_path)
        assert Mailbox(get_agent_registry(), "main").arrivals() == ()
        self._monitor_mail(reg)
        assert reg.arrived_for("main", waking_only=False, words=False) == 1 and Mailbox(
            get_agent_registry(), "main"
        ).arrivals() == ("notice",)
        # 종전의 "a reply to you not yet delivered" 는 없다 — 도착분은 깨울 것이 아니다
        assert Mailbox(get_agent_registry(), "main").wake_sources() == []
        assert reg.arrived_for("agent:nobody", waking_only=False, words=True) == 0

    def test_resident_inbox_items_count_for_that_agent(self, tmp_path):
        """받은편지함 미러 `inbox_items` 로 센다 — `SimpleQueue` 엔 `queue` 속성이
        없어 종전 `pending_for`(지금은 `wake.agent_relations`) 의 "has your request queued" 가지는 늘 비었다."""
        from tests.test_wait_checkin import _fake_agent

        reg = self._registry(tmp_path)
        # 워커 없는 에이전트 — main 의 요청이 받은편지함에 줄 서 있다(공개 길 `request`, 보낸 쪽 기록도 적힌다)
        _fake_agent(reg, "k", inbox=[{"author": "main"}])
        assert reg.arrived_for("agent:k", waking_only=False, words=True) == 1
        assert reg.arrived_for("main", waking_only=False, words=False) == 0
        assert agent_wake_lines(reg, "main") == ["agent:k has your request queued"]

    def test_wait_is_refused_and_the_mail_lands_in_the_next_turn(self, jobs, tmp_path):
        reg = self._registry(tmp_path)
        # 돌고 있는 작업이 **있어도** 도착분이 먼저다 — 읽고 나서 다시 wait
        jobs.start("sleep 30", owner="main")
        loop = self._loop(
            reg,
            self._mid_turn(reg, 1, {"action": "wait", "reason": "monitor"}),
        )
        res = loop.run()
        assert res.success and res.output == "ok"
        assert loop.turn == 2  # 거부 턴 + 닫는 턴 — 런 경계 없이 이어졌다
        users = [str(m.get("content")) for m in loop.messages if m["role"] == "user"]
        i = next(i for i, c in enumerate(users) if "wait refused" in c)
        assert "1 notice arrived while you were thinking" in users[i]
        assert "nothing you started is pending" not in users[i]
        # 그 통지는 거부 **뒤**, 다음 턴 경계에 배달됐다
        assert any("🔔 hit" in c for c in users[i + 1 :])
        assert reg.arrived_for("main", waking_only=False, words=False) == 0

    def test_two_notices_pluralize(self, tmp_path):
        reg = self._registry(tmp_path)
        loop = self._loop(
            reg,
            self._mid_turn(reg, 2, {"action": "wait"}),
        )
        assert loop.run().success
        assert any(
            "2 notices arrived while you were thinking" in str(m.get("content"))
            for m in loop.messages
            if m["role"] == "user"
        )


class TestCompleteNag:
    def test_complete_without_answers_while_a_job_runs_points_at_wait(self, jobs):
        jobs.start("sleep 30", owner="main")
        # 이 대역에는 레지스트리가 없다 — 받아들여진 `wait` 는 기다릴 메일함이 없어 곧 깨고 같은 런이 닫는다
        loop = _loop(
            _provider(
                {"action": "complete", "result": "waiting for the install"},
                {"action": "wait"},
                {"action": "complete", "result": "installed", "answers": ["u-1"]},
            )
        )
        res = loop.run()
        assert res.success
        nag = [
            m["content"]
            for m in loop.messages
            if m["role"] == "user" and "call `wait` instead" in str(m.get("content"))
        ]
        assert nag, "되돌림이 wait 를 가리키지 않았다"

    def test_no_wait_hint_when_nothing_is_pending(self):
        set_job_registry(None)
        loop = _loop(
            _provider(
                {"action": "complete", "result": "r"},
                {"action": "complete", "result": "r", "answers": ["u-1"]},
            )
        )
        loop.run()
        assert not any(
            "call `wait` instead" in str(m.get("content"))
            for m in loop.messages
            if m["role"] == "user"
        )


class TestWording:
    """기다릴 땐 `complete` 라고 말하는 문장이 하나도 남지 않는다."""

    def test_guideline_jobs_tail_and_nudge(self, tmp_path):
        from agent_cli.prompts.system_prompt import TASK_GUIDELINES

        assert "call `wait`" in TASK_GUIDELINES
        assert "do not use `complete` to wait" in TASK_GUIDELINES
        r = JobRegistry(tmp_path)
        r.deliver = lambda *a, **k: None
        try:
            r.start("sleep 30", owner="main")
            tail = r.render_tail("main")
        finally:
            r.stop_all()
        # 작업 블록 머리는 상태만 (v10.68.0) — wait 안내는 규칙(Guidelines)과
        # `## Open Items` 꼬리말 두 곳, 세 번째는 없다
        assert "do not poll" in tail and "wait" not in tail and "complete" not in tail
        # v10.76.0: 관찰 넛지는 complete 만 — wait 안내는 Open Items 꼬리말과 규칙 두 곳
        assert "wait" not in _OBS_COMPLETE_NUDGE
        assert "address every outstanding request" not in TASK_GUIDELINES

    def test_open_items_tail_points_at_wait_only_where_wait_exists(self, jobs):
        """꼬리의 `## Open Items` 끝에 "못 닫으면 wait" 한 줄 (v10.67.1) — wait 가
        광고되는 루프(main 큐·상주 받은편지함)에만. 스킬·일회성 루프의 꼬리엔 없다."""
        from agent_cli.loop.ledger import Ledger

        led = Ledger()
        led.add_request("u-1", "DJ", "do X")
        assert "Call `wait`" in led.block(can_wait=True)
        assert "never `complete` to wait" in led.block(can_wait=True)
        assert "wait" not in led.block()
        # 실제 조립: wakeable 포트의 루프 꼬리에는 있고, 미연결 포트의 루프엔 없다
        for ports, expect in ((WAKEABLE, True), (TEST_PORTS, False)):
            loop = make_loop(
                ports=ports,
                query="Q",
                query_request_id="u-1",
                provider=MagicMock(),
                capabilities=ModelCapabilities(
                    context_window=32768,
                    max_output_tokens=4096,
                    supports_thinking=False,
                ),
                model="m",
                dialect=get_dialect("json_fc"),
            )
            loop._state.ledger.add_request("u-1", "DJ", "do X")
            tail = loop._llm._build_session_state(10000)
            assert ("Call `wait` — the items stay open" in tail) is expect, tail

    def test_no_complete_and_wait_phrase_in_package(self):
        import re
        from pathlib import Path

        bad = re.compile(
            r"complete.{0,12}and wait|complete-and-wait|`complete` saying you are waiting"
        )
        hits = []
        for p in Path("agent_cli").rglob("*.py"):
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if bad.search(line) and not line.lstrip().startswith("#"):
                    hits.append(f"{p}:{i}")
        assert not hits, hits


class TestCircularWait:
    """v10.77.0 — 서로 `message` 하고 둘 다 `wait` 하면 아무도 깨지 않는다.
    잠든 채무자는 그를 깨울 것이 있을 때만 깨울 근거로 센다(거슬러 보기)."""

    @pytest.fixture
    def reg(self, tmp_path, monkeypatch):
        from agent_cli.session_slots import set_agent_registry, set_schedule_registry
        from tests.test_agents_live import make_registry, make_runner, wait_until

        set_schedule_registry(None)
        self.gate = (
            threading.Event()
        )  # 열려 있으면 런이 바로 끝난다 — 닫으면 다음 런이 붙잡힌다
        self.gate.set()
        r = make_registry(tmp_path, runner=make_runner(block=self.gate))
        keys = [r.spawn()[0] for _ in range(3)]
        assert wait_until(lambda: all(r.get(k).state == "idle" for k in keys))
        set_agent_registry(r)
        yield r, keys
        self.gate.set()
        set_agent_registry(None)
        r.shutdown_all()

    @staticmethod
    def _owes(r, debtor, to, rid):
        r.note_owes(f"agent:{debtor}", f"agent:{to}", "t", id=rid)

    def test_two_agents_waiting_on_each_other_refuse_the_second_wait(self, reg):
        from agent_cli.wake import Mailbox

        r, (a, b, _c) = reg
        self._owes(r, a, b, "r-1")  # a 는 b 에게 빚진 채 잠들었다(wait)
        r.mark_waiting(f"agent:{a}")
        self._owes(r, b, a, "r-2")  # b 는 a 의 요청으로 깨어 지금 wait 를 부른다
        tb = r.get(b)
        tb.state, tb.current_author = "busy", f"agent:{a}"
        assert Mailbox(get_agent_registry(), f"agent:{b}").wake_sources() == []
        assert parked_debtors(r.wake_facts(), f"agent:{b}") == [f"agent:{a}"]

    def test_a_chain_to_a_working_agent_still_wakes(self, reg):
        from agent_cli.wake import Mailbox

        r, (a, b, c) = reg
        # 손으로 고른 id 는 할당기가 이 테스트에서 내지 않는 값 — 아래 `request` 가 받는 id 와 겹치지 않게
        self._owes(r, a, b, "r-901")  # a 는 b 에게 빚진 채 잠들었고
        r.mark_waiting(f"agent:{a}")
        # c 가 a 의 요청을 처리 중이다 → a 는 깨어날 것이다. 그 요청은 아직 회신이 가지 않았다 (v10.143.0)
        self.gate.clear()
        from tests.test_agents_live import wait_until

        assert r.request(c, "job", author=f"agent:{a}") == ""
        assert wait_until(lambda: r.taken_by(f"agent:{c}") != ())
        got = Mailbox(get_agent_registry(), f"agent:{b}").wake_sources()
        assert got == [f"agent:{a} owes you a reply (it is waiting on others)"]


class TestRunEnded:
    """v10.77.0 — 런 끝의 유일한 정리 `AgentRegistry.run_ended`. `wait` 는 런의 끝이 아니다(5c-1) — 런이 끝나면 빚은
    회신(폴백)이나 버림으로 닫힌다."""

    @pytest.fixture
    def reg(self, tmp_path):
        from tests.test_agents_live import make_registry

        r = make_registry(tmp_path)
        yield r
        r.shutdown_all()

    def test_stopped_drops_the_debt_without_a_summary(self, reg):
        """끊긴 요청은 재시작이 새 id 로 다시 적는다 — 남기면 빚이 둘이 되고, 죽은
        채무자의 빚이 요청자의 `wait` 를 붙잡는다."""
        reg.note_owes("agent:a", "main", "일감", id="r-1")
        assert reg.run_ended("agent:a", "끊김", stopped=True) == 0
        assert reg.reply_debts("agent:a") == []
        assert not reg.arrived_for("main", waking_only=False, words=False)
        assert agent_wake_lines(reg, "main") == []

    def test_a_normal_end_sends_the_labelled_summary_and_clears(self, reg):
        from agent_cli.subagent.agent_mail import _NO_REPLY_LABEL

        reg.note_owes("agent:a", "main", "일감", id="r-1")
        assert reg.run_ended("agent:a", "요약", run="r-1", success=True) == 1
        assert reg.reply_debts("agent:a") == []
        (mail,) = absorbed_mail(reg)
        assert mail["output"] == f"{_NO_REPLY_LABEL}\n요약" and mail["re"] == "r-1"

    def test_main_run_ended_routes_through_the_same_place(self, reg):
        from agent_cli.runtime import main_run_ended

        reg.note_owes("main", "agent:a", "결정해 줘", id="r-5")
        assert (
            main_run_ended(reg, "끊김", stopped=True) == 0
        )  # 상주와 같은 자리 — 정지면 버린다
        assert reg.reply_debts("main") == []
