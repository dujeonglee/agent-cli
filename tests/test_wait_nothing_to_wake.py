"""깨울 것이 없는 `wait` — 갚지 않은 회신(`r-`)이 있나 하나로 갈린다 (5d).

판정이 읽는 것은 둘이다: 깨울 것(`Mailbox.accept_wait` — `wake.wake_items`)이 있나, 갚지 않은 회신(`ledger.debts`)이
있나. 사람의 요청(`u-`)은 빚이 아니다. 엔진을 대본 공급자로 돌리고 모델이 읽는 관찰 글·결과·호출 수로 본다.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.dialects import get as get_dialect
from agent_cli.loop.kind import PrivateLoop
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.session_slots import (
    set_agent_registry,
    set_job_registry,
    set_monitor_registry,
    set_schedule_registry,
)
from agent_cli.tools.virtual import WaitTool
from tests.loop_ports import make_ports, queue_inbox
from tests.loopkit import make_loop
from tests.test_agents_live import _FakeLoopResult, make_registry, wait_until
from tests.test_baseline_wait_sentences import (  # noqa: F401 — 픽스처
    renderer,
    resident,
)
from tests.test_engine_p2 import call, owed_id

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)
WAIT = {"action": "wait"}
#: 빚도 깨울 것도 없다 — 받아들여 작업 구간이 끝난다. 누구에게나 같은 글이다(종류마다 갈리던 끝맺음이 없다)
NOTHING_TO_WAIT_FOR = (
    "Observation: nothing to wait for: no job or monitor of yours is running, no agent "
    "owes you a reply, and you owe none. This run has ended."
)


def owed(*ids: str) -> str:
    return (
        "Observation: wait refused — nothing would wake you, and you still owe a "
        f"`reply` [{', '.join(ids)}]. Reply first, or keep working."
    )


@pytest.fixture
def reg(tmp_path):
    set_job_registry(None)
    set_monitor_registry(None)
    set_schedule_registry(None)
    r = make_registry(tmp_path)
    set_agent_registry(r)
    yield r
    set_agent_registry(None)
    r.shutdown_all()


def _provider(*replies: dict):
    p = MagicMock()
    p.call.side_effect = [LLMResponse(content=json.dumps(r)) for r in replies]
    return p


def _run(ports, provider, *, rid="u-1"):
    loop = make_loop(
        ports=ports,
        query="Q",
        query_request_id=rid,
        provider=provider,
        capabilities=CAPS,
        model="m",
        dialect=get_dialect("json_fc"),
    )
    return loop, loop.run()


def _wait_observations(loop) -> list[str]:
    return [
        str(m.get("content"))
        for m in loop.messages
        if m.get("role") == "user"
        and str(m.get("content")).startswith(
            ("Observation: wait", "Observation: nothing")
        )
    ]


class TestJudgment:
    def test_a_persons_request_is_not_a_debt(self, reg):
        """main 이 사람의 요청(u-1)만 연 채 `wait` — 빚이 없다. 받아들여 빈 결과의 성공으로 끝난다(모델 호출 1).
        그 작업 구간은 u-1 을 들었다(감사·`run` 의 판정이 읽는 값)."""
        p = _provider(WAIT, {"action": "complete", "result": "ok", "answers": ["u-1"]})
        loop, result = _run(
            make_ports(inbox=queue_inbox(lambda: None), agent_registry=reg), p
        )
        assert _wait_observations(loop) == [NOTHING_TO_WAIT_FOR]
        assert (result.success, result.output) == (True, "")
        assert result.human_requests == ("u-1",)
        assert p.call.call_count == 1
        assert reg._post.is_waiting("main") is False

    def test_an_unpaid_reply_is_a_debt(self, reg):
        """main 이 에이전트에게 회신을 빚졌고(r-7) 그 에이전트는 한가하다 — 깨울 것도 고리도 없다."""
        key, _ = reg.spawn()
        assert wait_until(lambda: reg.get(key).state == "idle")
        reg.note_owes("main", f"agent:{key}", "which file?", id="r-7")
        p = _provider(
            WAIT,
            {"action": "reply", "id": "r-7", "text": "a.py"},
            {"action": "complete", "result": "ok", "answers": ["u-1"]},
        )
        loop, result = _run(
            make_ports(
                inbox=queue_inbox(lambda: None),
                agent_registry=reg,
                questions=reg.question_port(None),
            ),
            p,
        )
        # 거절이 그 빚을 가리키고, 회신한 뒤 끝낸다
        assert _wait_observations(loop) == [owed("r-7")]
        assert (result.success, result.output) == (True, "ok")
        assert p.call.call_count == 3

    def test_every_unpaid_reply_is_named(self, reg):
        key, _ = reg.spawn()
        assert wait_until(lambda: reg.get(key).state == "idle")
        reg.note_owes("main", f"agent:{key}", "one", id="r-7")
        reg.note_owes("main", f"agent:{key}", "two", id="r-9")
        p = _provider(
            WAIT,
            {"action": "reply", "id": "r-7", "text": "a"},
            {"action": "reply", "id": "r-9", "text": "b"},
            WAIT,
        )
        loop, result = _run(
            make_ports(
                inbox=queue_inbox(lambda: None),
                agent_registry=reg,
                questions=reg.question_port(None),
            ),
            p,
        )
        # 갚은 뒤의 `wait` 는 받아들여져 끝난다
        assert _wait_observations(loop) == [owed("r-7", "r-9"), NOTHING_TO_WAIT_FOR]
        assert (result.success, result.output) == (True, "")

    def test_a_harness_job_with_nothing_owed(self, reg):
        """하니스 작업도 같은 규칙이다(결정 5) — 빈 결과로 끝난다."""
        p = _provider(WAIT, {"action": "complete", "result": "done"})
        loop, result = _run(PrivateLoop.make_ports("harness:t"), p, rid="")
        assert _wait_observations(loop) == [NOTHING_TO_WAIT_FOR]
        assert (result.success, result.output) == (True, "")


class TestPersonInAnAgentWindow:
    def test_the_run_result_is_the_line_back_to_that_person(self, tmp_path, renderer):  # noqa: F811
        """사람이 창에서 연 상주 구간 — 그 결과 글이 그 사람에게 가는 발신 줄이다. 결과가 빈 글이면(기다릴 것 없는
        `wait` 의 끝) 그 사람에게 한 말이 없어 줄을 내지 않는다(종전: 빈 발신 줄)."""
        outputs = iter(["hello", ""])
        reg = make_registry(
            tmp_path,
            runner=lambda kind, start, ctx: (
                _FakeLoopResult(output=next(outputs)),
                0.01,
            ),
        )
        key, _ = reg.spawn()
        assert wait_until(lambda: reg.get(key).state == "idle")
        for n, text in enumerate(("first words", "second words"), start=1):
            assert reg.request(key, text, author="user:dj") == ""
            assert wait_until(lambda n=n: reg.get(key).handled >= n)
        out = [
            kw["text"]
            for name, kw in renderer.calls
            if name == "agent_message" and kw.get("direction") == "out"
        ]
        assert out == ["hello"]
        reg.shutdown_all()


class TestRunAgent:
    def test_paid_then_wait(self, resident):  # noqa: F811
        """run 에이전트가 과제에 회신한 뒤 `wait` — 빚도 깨울 것도 없다. 작업 구간이 끝나고 내려온다(뒤의 `complete`
        대본은 쓰이지 않는다)."""
        text = resident["run"](
            {
                "TASK-R1": [
                    lambda m: call("reply", id=owed_id(m), text="done"),
                    call("wait"),
                    call("complete", result="ok"),
                ]
            },
            ends_after_task=True,
        )
        assert text.endswith(NOTHING_TO_WAIT_FOR)
        assert resident["reg"].get(resident["key"]).state == "dead"


def test_wait_description_ends_with_what_happens_when_nothing_wakes():
    assert WaitTool.description.endswith(
        "Refused when nothing will wake you: then keep working, or call `complete`."
    )
