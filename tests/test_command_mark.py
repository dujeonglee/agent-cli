"""명령은 들어올 때 한 번 판정해 표시한다 (5b-3 "가", 제안서 g72/PROPOSAL-cmd.md).

main 이 생각하는 동안 사람이 친 명령(``/stop agt-3`` …)은 main 상자에 사람의 말로 선다. 명령은 하니스가 처리하고
모델에게 가지 않는데, `wait` 판정(`Mailbox.arrivals`)이 그것을 "the user wrote to you … right below" 로 센다 — 아래에
글이 없다. 이 파일은 그 판정의 자리마다 값을 고정한다.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.dialects import get as get_dialect
from agent_cli.jobs import JobRegistry
from agent_cli.main_input import MainInput
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.session_slots import (
    set_agent_registry,
    set_job_registry,
    set_schedule_registry,
)
from agent_cli.wake import Mailbox
from tests.loop_ports import make_ports, queue_inbox
from tests.loopkit import make_loop
from tests.test_agents_live import make_registry

COMMAND = "/stop agt-3"


@pytest.fixture
def reg(tmp_path):
    set_schedule_registry(None)
    r = make_registry(tmp_path)
    set_agent_registry(r)
    yield r
    set_agent_registry(None)


@pytest.fixture
def jobs(tmp_path):
    r = JobRegistry(tmp_path / "jobs")
    r.deliver = lambda *a, **k: None
    set_job_registry(r)
    yield r
    r.stop_all()
    set_job_registry(None)


def _box(reg) -> MainInput:
    """레지스트리와 같은 창구를 쓰는 main 앞 입력 — 제품의 조립과 같다(`build_session_stores`)."""
    return MainInput(post=reg._post)


# ── `wait` 판정의 도착분 ─────────────────────────────────────────

#: (이름, 넣는 글, 도착분의 종류)
ARRIVALS = [
    ("command_only", [COMMAND], ("user",)),
    ("command_then_words", [COMMAND, "MAIN look"], ("user", "user")),
    ("words_then_command", ["MAIN look", COMMAND], ("user", "user")),
]


@pytest.mark.parametrize("name,texts,kinds", ARRIVALS, ids=[a[0] for a in ARRIVALS])
def test_arrivals_of_main(reg, name, texts, kinds):
    box = _box(reg)
    for text in texts:
        box.enqueue("c1", text, nickname="dj")
    assert Mailbox(reg, "main").arrivals() == kinds


def test_a_schedule_whose_prompt_is_a_command(reg):
    _box(reg).enqueue(None, COMMAND, nickname="nightly", src="schedule:s-1")
    assert Mailbox(reg, "main").arrivals() == ("schedule:s-1",)


def test_a_queued_command_is_live_work(reg):
    """줄 선 명령은 실행될 일이다 — `run` 이 명령을 남긴 채 끝나면 안 된다(그대로)."""
    from agent_cli.runtime import session_has_live_work

    _box(reg).enqueue("c1", COMMAND, nickname="dj")
    assert session_has_live_work(post=reg._post, agent_registry=reg)


# ── 명령만 줄에 있을 때의 `wait` (루프) ─────────────────────────────


def _provider_that_types_a_command_then_waits(reg, *, then_complete=True):
    """첫 모델 호출 **도중** 사람이 명령을 친다(입력창 → main 상자) — 그리고 모델은 `wait` 를 낸다. 둘째 호출은
    열린 요청을 닫는 `complete`."""
    p = MagicMock()
    holder: dict = {}

    def _call(*_a, **_k):
        if p.call.call_count == 1:
            _box(reg).enqueue("c1", COMMAND, nickname="dj")
            return LLMResponse(content=json.dumps({"action": "wait"}))
        ids = list(holder["loop"]._state.ledger.request_ids())
        return LLMResponse(
            content=json.dumps({"action": "complete", "result": "ok", "answers": ids})
        )

    p.call.side_effect = _call
    p.holder = holder
    return p


def _loop(reg, provider):
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


def _wait_observation(loop) -> str:
    users = [str(m.get("content")) for m in loop.messages if m["role"] == "user"]
    return next(c for c in users if "Observation: wait" in c or "waiting" in c)


def test_only_a_command_queued_and_nothing_to_wake(reg):
    """장면 D — 깨울 것이 없다."""
    loop = _loop(reg, _provider_that_types_a_command_then_waits(reg))
    res = loop.run()
    assert res.success and not res.waiting
    assert (
        "wait refused — the user wrote to you while you were thinking; their message "
        "is right below." in _wait_observation(loop)
    )


def test_only_a_command_queued_while_a_job_runs(reg, jobs):
    """장면 A′ — main 이 작업만 기다린다(런을 끝내는 `wait`)."""
    jobs.start("sleep 30", owner="main")
    loop = _loop(reg, _provider_that_types_a_command_then_waits(reg))
    res = loop.run()
    assert res.success and not res.waiting
    assert (
        "wait refused — the user wrote to you while you were thinking; their message "
        "is right below." in _wait_observation(loop)
    )
