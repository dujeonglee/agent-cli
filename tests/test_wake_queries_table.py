"""깨우는 판정의 자리마다 지금 값 — 조회 둘(창구의 도착 조회, `wake` 의 깨울 것)로 모은 뒤에도 같아야 한다 (5a).

자리마다 제품의 종류가 만드는 포트(`MainLoop`·`ResidentLoop`·`PrivateLoop` 의 ``make_ports``)로 묻는다 — 어긋남을
남겨 둔 한 줄(main 의 ``Mailbox`` 생성, `Mailbox.arrived`, 예약의 근거)이 바뀌면 여기서 걸린다.

``[5b]`` 표지의 칸은 알려진 어긋남을 지금 동작으로 고정한 것이다 — 5b 가 그 한 줄을 뒤집을 때 이 칸이 바뀐다:
- 깨우지 않는 봉투도 `wait` 의 도착분이다(거절 2번·제자리 대기를 "mail" 로 깨움).
- main 의 도착분은 사람의 말을 세지 않는다 — 회신을 기다리는 main 은 받아들인 뒤 "input" 으로 깬다(A-4).
- 에이전트의 `wait` 도 예약을 깨울 근거로 받는다(변화 8).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent_cli.loop.kind import PrivateLoop
from agent_cli.post import envelope as E
from agent_cli.runtime import MainLoop
from agent_cli.session_slots import (
    set_agent_registry,
    set_job_registry,
    set_monitor_registry,
    set_schedule_registry,
)
from agent_cli.subagent.agents_live import AgentInstance
from agent_cli.subagent.resident_loop import ResidentLoop
from agent_cli.wake import AgentFact, WakeFacts, wake_source_ids
from tests.test_agents_live import make_registry

A = "agent:a"


def _mailbox(owner: str, registry, *, wait_input=None):
    """제품의 종류가 그 주소에 꽂는 메일함 포트."""
    if owner == "main":
        return MainLoop.make_ports(
            agent_registry=registry,
            mcp_manager=None,
            enqueue_user_message=None,
            route_message=None,
            wait_for_user_input=wait_input,
        ).mailbox
    if owner.startswith("agent:"):
        return ResidentLoop.make_ports(
            key=owner.split(":", 1)[1],
            message_handler=None,
            questions=None,
            inbox=list,
            peer_roster=None,
            agent_registry=None,
        ).mailbox
    return PrivateLoop.make_ports(owner).mailbox


def _workerless(reg, key):
    """워커 없는 한가한 에이전트 — 명단에만 오른다."""
    tm = AgentInstance(
        key,
        profile_name="explorer",
        role_prompt="",
        allowed_tools=None,
        model="m",
        hooks_config=None,
        context_mode="fresh",
        home_dir=reg.session_dir / key,
    )
    tm.state = "idle"
    reg._agents[key] = tm
    return tm


@pytest.fixture
def slots():
    set_job_registry(None)
    set_monitor_registry(None)
    set_schedule_registry(None)
    yield
    set_agent_registry(None)
    set_schedule_registry(None)


@pytest.fixture
def reg(tmp_path, slots):
    r = make_registry(tmp_path)
    set_agent_registry(r)
    return r


# 봉투
W = lambda i: E.words(f"u-{i}", "user:dj", "main", f"w{i}")
N = lambda i: E.notice(f"n-{i}", "job", "main", "done")
Q = lambda i: E.reply(f"n-{i}", "agent:b", "main", "x", re="r-0", wakes=False)
H = lambda i: E.human(f"u-{i}", "user:dj", A, f"h{i}")
R = lambda i: E.request(f"r-{i}", "main", A, f"do {i}")
S = lambda i: E.reply(f"n-{i}", "agent:b", A, "x", re="r-0", wakes=False)


# ── `wait` 의 도착분 (거절 2번·제자리 대기를 깨움) ─────────────────────────

#: (이름, 주소, 명단에 올림, 넣는 봉투, 도착분)
ARRIVED = [
    ("main_empty", "main", False, [], 0),
    ("main_words_not_counted", "main", False, [W(1)], 0),  # [5b] A-4
    ("main_waking_mail", "main", False, [N(1)], 1),
    ("main_quiet_reply_counted", "main", False, [Q(1)], 1),  # [5b] 깨우지 않는 봉투
    ("main_words_and_quiet", "main", False, [W(1), Q(2)], 1),
    ("agent_human_counted", A, True, [H(1)], 1),
    ("agent_quiet_reply_counted", A, True, [S(1)], 1),  # [5b] 깨우지 않는 봉투
    ("agent_request", A, True, [R(1)], 1),
    ("agent_human_and_quiet", A, True, [H(1), S(2)], 2),
    ("agent_off_roster", A, False, [S(1)], 0),
]


@pytest.mark.parametrize(
    "name,owner,on_roster,envs,count", ARRIVED, ids=[r[0] for r in ARRIVED]
)
def test_wait_arrived_per_address(reg, name, owner, on_roster, envs, count):
    if on_roster:
        _workerless(reg, owner.split(":", 1)[1])
    for env in envs:
        reg._post.deliver(env)
    assert _mailbox(owner, reg).arrived() == count


def test_private_mail_is_arrived(reg):
    reg.deliver(
        "harness:x",
        mail={"kind": "job", "output": "done"},
        text="done",
        author="main",
        expects_reply=False,
    )
    assert _mailbox("harness:x", reg).arrived() == 1


def test_no_registry_arrives_nothing(slots):
    assert _mailbox("harness:x", None).arrived() == 0


class TestMainInPlaceWake:
    """회신을 기다리는 main 의 제자리 대기 — 무엇이 어떤 이유로 깨우나."""

    def test_words_wake_it_as_input_not_mail(self, reg):  # [5b] A-4
        reg._post.deliver(W(1))
        mb = _mailbox("main", reg, wait_input=lambda timeout: True)
        why = mb.wait(stop_event=threading.Event(), still_waiting=lambda: True)
        assert why == "input"

    def test_a_quiet_reply_wakes_it_as_mail(self, reg):  # [5b] 깨우지 않는 봉투
        reg._post.deliver(Q(1))
        mb = _mailbox("main", reg, wait_input=lambda timeout: False)
        why = mb.wait(stop_event=threading.Event(), still_waiting=lambda: True)
        assert why == "mail"


# ── 세션 수명 판정 (run 의 끝·web 의 한가함 정리) ─────────────────────────

#: (이름, main 앞 봉투, run 을 붙잡나)
MAIN_WORK = [
    ("empty", [], False),
    ("waking_mail", [N(1)], True),
    ("quiet_reply", [Q(1)], False),
    ("words_only", [W(1)], False),  # 사람의 말은 run 이 `pending_count` 로 따로 센다
]


@pytest.mark.parametrize("name,envs,busy", MAIN_WORK, ids=[r[0] for r in MAIN_WORK])
def test_main_box_and_run_lifetime(reg, name, envs, busy):
    for env in envs:
        reg._post.deliver(env)
    assert reg.has_active_work() is busy
    assert reg.any_activity() is False  # web 의 한가함 정리는 main 상자를 보지 않는다


#: (이름, 한가한 에이전트 앞 봉투, 할 일 수)
AGENT_WORK = [
    ("empty", [], 0),
    ("request", [R(1)], 1),
    ("human", [H(1)], 1),
    ("quiet_reply", [S(1)], 0),
    ("quiet_then_request", [S(1), R(2)], 1),
]


@pytest.mark.parametrize(
    "name,envs,backlog", AGENT_WORK, ids=[r[0] for r in AGENT_WORK]
)
def test_agent_backlog_and_lifetime(reg, name, envs, backlog):
    _workerless(reg, "a")
    for env in envs:
        reg._post.deliver(env)
    assert reg.backlog_of("a") == backlog
    assert reg.has_active_work() is (backlog > 0)
    assert reg.any_activity() is (backlog > 0)


def test_dead_agents_backlog_is_not_work(reg):
    tm = _workerless(reg, "a")
    reg._post.deliver(R(1))
    tm.state = "dead"
    assert reg.backlog_of("a") == 1
    assert reg.has_active_work() is False and reg.any_activity() is False


# ── 깨울 것 (`wake`) — 예약의 근거·체크인 대상 ─────────────────────────────


def _fake_agents(facts: WakeFacts):
    """사본만 주는 레지스트리 — 깨울 것의 에이전트 몫은 `wake_facts` 하나만 읽는다."""
    return SimpleNamespace(
        wake_facts=lambda: facts,
        message_handler=lambda sender: None,
        absorb_main=list,
        question_port=lambda key=None: None,
    )


NO_AGENTS = WakeFacts((), (), frozenset())
#: 한가한 B 가 main 에게 회신을 빚진 채 잠들었다
PARKED_B = WakeFacts(
    (AgentFact("agent:b", False, False, (), ()),),
    (("agent:b", "main"),),
    frozenset({("main", "r-1")}),
)

#: (이름, 주인, 사본, 예약 켜짐, `wait` 의 깨울 것, 체크인 대상)
WAKERS = [
    ("main_schedule", "main", NO_AGENTS, True, ["a schedule"], frozenset()),
    ("main_nothing", "main", NO_AGENTS, False, [], frozenset()),
    ("agent_schedule", A, NO_AGENTS, True, ["a schedule"], frozenset()),  # [5b] 변화 8
    ("agent_nothing", A, NO_AGENTS, False, [], frozenset()),
    ("private_ignores_schedule", "harness:x", NO_AGENTS, True, [], frozenset()),
    (
        "parked_debtor_waits_on_schedule",  # [5b] 변화 8 — 거슬러 본 에이전트도 예약을 근거로
        "main",
        PARKED_B,
        True,
        ["agent:b owes you a reply (it is waiting on others)", "a schedule"],
        frozenset({"agent:b"}),
    ),
    ("parked_debtor_nothing", "main", PARKED_B, False, [], frozenset()),
]


@pytest.mark.parametrize(
    "name,owner,facts,schedule_on,sources,ids", WAKERS, ids=[r[0] for r in WAKERS]
)
def test_wakers(slots, name, owner, facts, schedule_on, sources, ids):
    agents = _fake_agents(facts)
    set_agent_registry(agents)
    if schedule_on:
        set_schedule_registry(SimpleNamespace(has_active_work=lambda: True))
    assert _mailbox(owner, agents).wake_sources() == sources
    assert wake_source_ids(owner, agents=agents) == ids
