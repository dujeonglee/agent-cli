"""답 없이 끝난 요청 — 요청자가 받는 것 (감사 1-1, 제안서 PROP-no-result-fallback 1절 A·B·C·D·J).

요청(``r-N``)의 답은 `reply` 하나다(사용자 결정 2026-11-27). 런이 `reply` 없이 끝나면 하니스가 "답하지 않았다" 는
사실 문장 하나로 그 요청을 닫는다 — 런의 결과(`complete` 의 `result`)는 요청자에게 가지 않는다. 종전엔 라벨 붙은 런
요약이 회신으로 갔다(C 는 main 이 사용자에게 한 답이었다). 런 끝 정리(`end_run`)를 테스트가 직접 부른다 — 워커는
문(``door``)에서 멈춘다.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.main import _dispatch_agent
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.subagent.agent_mail import build_reply_record
from tests.binding_support import make_binding
from tests.mail_support import absorbed_mail
from tests.test_agents_live import _FakeLoopResult, make_registry
from tests.test_engine_p2 import wait_until

NEXT = "Decide whether to continue another way or report back to the user."


@pytest.fixture
def two_agents(tmp_path):
    door = threading.Event()

    def runner(kind, start, ctx):
        door.wait()
        return _FakeLoopResult(output="ok"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    a, _ = reg.spawn(name="alpha")
    b, _ = reg.spawn(name="beta")
    assert wait_until(lambda: reg.get(a).state == "idle" and reg.get(b).state == "idle")
    try:
        yield reg, a, b
    finally:
        door.set()
        reg.shutdown_all()


def _main_mail(reg) -> list[str]:
    return [build_reply_record(m, registry=reg)["content"] for m in absorbed_mail(reg)]


def _inbox(reg, key) -> list[dict]:
    """``key`` 가 받은 회신 — 줄 선 것과 런이 집어 든 것(회신이 받는 쪽의 런을 연다)."""
    tm = reg.get(key)

    def got():
        return [
            it for it in reg._queued(tm) + reg._taken(tm) if it.get("kind") == "reply"
        ]

    assert wait_until(lambda: got())
    return got()


def test_agent_owes_main(two_agents):
    """A — 에이전트가 main 에게 빚진 채 끝났다."""
    reg, a, _b = two_agents
    debt = reg.note_owes(f"agent:{a}", "main", "review the parser next")
    activity = ['`read_file` "src/parser.py" ✓']
    assert reg.end_run(f"agent:{a}", activity, success=True) == 1
    (content,) = _main_mail(reg)
    assert content.splitlines()[0].endswith(f"re [{debt.id}] · no reply ──")
    assert content.splitlines()[1:4] == [
        (
            f"{a} ended its run without a `reply` to your request [{debt.id}] "
            '("review the parser next").'
        ),
        'Last activity: `read_file` "src/parser.py" ✓',
        NEXT,
    ]
    # 살아 있는 상주 — 다시 물을 수 있다는 사실(상태 꼬리)은 그대로
    assert content.splitlines()[4] == "(agent status: idle — ready for new requests.)"


def test_agent_owes_another_agent(two_agents):
    """B — 에이전트가 다른 에이전트에게 빚진 채 끝났다."""
    reg, a, b = two_agents
    debt = reg.note_owes(f"agent:{a}", f"agent:{b}", "check the tests")
    assert reg.end_run(f"agent:{a}", [], success=True) == 1
    (item,) = _inbox(reg, b)
    assert item["kind"] == "reply" and item["re"] == debt.id
    assert item["mark"] == "no reply"
    assert item["text"] == (
        f'{a} ended its run without a `reply` to your request [{debt.id}] ("check the '
        f'tests").\n{NEXT}'
    )


def test_main_owes_an_agent(two_agents):
    """C — main 이 에이전트의 요청에 답하지 않고 런을 끝냈다: 사용자에게 한 답은 가지 않는다."""
    reg, a, _b = two_agents
    debt = reg.note_owes("main", f"agent:{a}", "which branch?")
    assert reg.end_run("main", []) == 1
    (item,) = _inbox(reg, a)
    assert item["re"] == debt.id and item["mark"] == "no reply"
    assert item["text"] == (
        f'main ended its run without a `reply` to your request [{debt.id}] ("which '
        f'branch?").\n{NEXT}'
    )


def test_a_failed_run_owes_main(two_agents):
    """J — 실패한 런: 원인 줄이 머리에."""
    reg, a, _b = two_agents
    debt = reg.note_owes(f"agent:{a}", "main", "do it")
    reg.end_run(f"agent:{a}", [], success=False, error="Max turns (40) reached")
    (content,) = _main_mail(reg)
    assert content.splitlines()[1:4] == [
        "(the run ended with an error: Max turns (40) reached)",
        f'{a} ended its run without a `reply` to your request [{debt.id}] ("do it").',
        NEXT,
    ]


def test_at_profile_command_whose_agent_does_not_reply(tmp_path, monkeypatch):
    """D — 사람의 `@<profile> 과제`: 일회용 에이전트가 `reply` 없이 끝났다. 답 자리에 하니스의 사실 문장이 서고
    명령은 실패다(``--result-file`` 도 실패로 적는다) — 런의 결과는 답이 아니다."""

    def runner(kind, start, ctx):
        return _FakeLoopResult(output="the run's own result"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    real_spawn = reg.spawn

    def spawn(*, profile="", **kw):  # 프로파일 파일 없이
        kw.pop("name", None)
        return real_spawn(name=profile, **kw)

    monkeypatch.setattr(reg, "spawn", spawn)
    monkeypatch.setattr("agent_cli.main._parse_at_profile", lambda name: (name, "run"))
    try:
        answer, ok = _dispatch_agent(
            "@helper find the bug",
            make_binding(
                provider=MagicMock(), capabilities=ModelCapabilities(32768, 4096, False)
            ),
            ctx=ContextManager(session_dir=tmp_path / "ctx"),
            agent_registry=reg,
            mcp_manager=None,
        )
    finally:
        reg.shutdown_all()
    assert ok is False and "the run's own result" not in answer
    assert answer.startswith("agt-") and answer.endswith(NEXT)
    assert (
        ' finished its task without a `reply` to your request [r-1] ("find the bug")'
        in (answer)
        and answer.split("\n")[0].endswith(" and is gone.")
    )


def test_a_one_shot_agent_owes_main(two_agents):
    """A(일회용) — 내려가는 에이전트의 답 없음 회신. 지금은 "… and is gone." 과 "ready for new requests" 상태 꼬리가
    한 회신에 함께 선다(배달 순간엔 아직 내려오기 전이다 — 고정)."""
    reg, a, _b = two_agents
    reg.get(a).ends_after_task = True
    debt = reg.note_owes(f"agent:{a}", "main", "summarize the logs")
    assert reg.end_run(f"agent:{a}", [], success=True) == 1
    (content,) = _main_mail(reg)
    lines = content.splitlines()
    assert lines[1] == (
        f"{a} finished its task without a `reply` to your request [{debt.id}] "
        '("summarize the logs") and is gone.'
    )
    assert lines[-1] == "(agent status: idle — ready for new requests.)"
