"""세션에 살아 있는 일이 있나 — run 의 끝(`QuietPolicy.should_stop`)과 웹의 한가함 정리(`web_instance_is_active`)를
같은 사실 표로 (단일 구조 5b-1).

호스트의 공개 판정 둘만 부른다 — 그 안의 술어가 하나로 합쳐져도 이 표는 그대로 지난다. 표의 값이 정본이다.
세션처럼 main 의 입력 큐와 에이전트 레지스트리가 창구 하나를 쓴다. 에이전트는 워커 없이 명단에만 올린다.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_cli.main import web_instance_is_active
from agent_cli.main_input import MainInput
from agent_cli.post import envelope as E
from agent_cli.pump import QuietPolicy
from agent_cli.session_slots import set_agent_registry
from agent_cli.subagent.agents_live import AgentInstance, AgentRegistry

#: 표의 칸 — 사실 하나를 세션에 세운다
FACTS = (
    "words",  # main 앞 사람의 말
    "waking_mail",  # main 앞 깨우는 메일
    "quiet_mail",  # main 앞 깨우지 않는 메일(Stop everything 의 정지 회신)
    "agent_starting",  # 막 띄운 에이전트(ctx 를 만드는 중)
    "agent_busy",
    "agent_idle",
    "agent_backlog",  # 한가한 에이전트 앞 깨우는 봉투
    "agent_quiet",  # 한가한 에이전트 앞 깨우지 않는 봉투만
    "dead_backlog",  # 죽은 에이전트 앞 봉투
    "human_question",  # 에이전트가 사람에게 물은 열린 질문
    "monitor",
    "job",
    "schedule",
    "harness",
)

#: 사실 → (run 이 끝나나, 웹이 살아 있다고 보나)
TABLE = {
    "nothing": (True, False),
    "words": (False, True),
    "waking_mail": (False, True),  # [5b-1] 웹도 센다 — 종전 웹은 세지 않았다
    "quiet_mail": (True, False),
    "agent_starting": (False, True),  # [5b-1] run 도 센다 — 종전 run 은 `busy` 만 셌다
    "agent_busy": (False, True),
    "agent_idle": (True, False),
    "agent_backlog": (False, True),
    "agent_quiet": (True, False),
    "dead_backlog": (True, False),
    "human_question": (True, True),
    "monitor": (False, True),
    "job": (False, True),
    "schedule": (False, True),
    "harness": (False, True),
}


def _agent(reg, key, state):
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
    tm.state = state
    reg._agents[key] = tm
    return tm


def _live(on: bool):
    return SimpleNamespace(has_active_work=lambda: on)


def _session(tmp_path, fact: str):
    """그 사실 하나만 선 세션 — (입력 큐, 레지스트리, 모니터·예약·잡·하니스)."""
    mi = MainInput()
    reg = AgentRegistry(tmp_path / "s", runtime={"model": "m"}, post=mi.post)
    set_agent_registry(reg)
    side = {
        "monitors": _live(fact == "monitor"),
        "schedules": _live(fact == "schedule"),
        "jobs": _live(fact == "job"),
        "harness": SimpleNamespace(in_flight=1 if fact == "harness" else 0),
    }
    post = mi.post
    if fact == "words":
        mi.enqueue(None, "hello")
    elif fact == "waking_mail":
        post.deliver(E.notice("n-1", "job", "main", "done"))
    elif fact == "quiet_mail":
        post.deliver(E.reply("n-1", "agent:b", "main", "x", re="r-0", wakes=False))
    elif fact == "agent_starting":
        _agent(reg, "a", "starting")
    elif fact == "agent_busy":
        _agent(reg, "a", "busy")
    elif fact == "agent_idle":
        _agent(reg, "a", "idle")
    elif fact == "agent_backlog":
        _agent(reg, "a", "idle")
        post.deliver(E.request("r-1", "main", "agent:a", "do"))
    elif fact == "agent_quiet":
        _agent(reg, "a", "idle")
        post.deliver(E.reply("n-1", "agent:b", "agent:a", "x", re="r-0", wakes=False))
    elif fact == "dead_backlog":
        _agent(reg, "a", "dead")
        post.deliver(E.request("r-1", "main", "agent:a", "do"))
    elif fact == "human_question":
        _agent(reg, "a", "idle")
        qid, err = reg.register_question("a", "which branch?")
        assert qid and not err
    return mi, reg, side


def _run_stops(mi, reg, side) -> bool:
    return QuietPolicy(main_input=mi, agent_registry=reg, **side).should_stop()


def _web_active(mi, reg, side) -> bool:
    renderer = SimpleNamespace(
        hub=SimpleNamespace(has_live_connections=lambda: False),
        worker_is_busy=lambda: False,
    )
    server = SimpleNamespace(main_input=mi, pending_count=mi.pending_count)
    return web_instance_is_active(renderer, server, reg, **side)


@pytest.fixture(autouse=True)
def _slot():
    yield
    set_agent_registry(None)


@pytest.mark.parametrize("fact", list(TABLE), ids=list(TABLE))
def test_one_fact(tmp_path, fact):
    mi, reg, side = _session(tmp_path, fact)
    assert (_run_stops(mi, reg, side), _web_active(mi, reg, side)) == TABLE[fact]


def test_table_covers_every_fact():
    assert set(TABLE) == {"nothing", *FACTS}


def test_web_host_terms(tmp_path):
    """웹만의 항 — 접속한 뷰어와 바쁜 main 워커는 그것만으로 살아 있다."""
    mi, reg, side = _session(tmp_path, "nothing")
    server = SimpleNamespace(main_input=mi, pending_count=mi.pending_count)
    for conn, busy in ((True, False), (False, True)):
        renderer = SimpleNamespace(
            hub=SimpleNamespace(has_live_connections=lambda c=conn: c),
            worker_is_busy=lambda b=busy: b,
        )
        assert web_instance_is_active(renderer, server, reg, **side) is True


def test_web_before_the_registry_exists(tmp_path):
    """웹 부트스트랩은 레지스트리를 늦게 채운다 — 없을 때도 main 상자와 곁의 일은 본다."""
    mi, _reg, side = _session(tmp_path, "words")
    renderer = SimpleNamespace(
        hub=SimpleNamespace(has_live_connections=lambda: False),
        worker_is_busy=lambda: False,
    )
    server = SimpleNamespace(main_input=mi, pending_count=mi.pending_count)
    assert web_instance_is_active(renderer, server, None, **side) is True
