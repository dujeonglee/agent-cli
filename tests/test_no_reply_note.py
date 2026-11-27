"""답 없이 끝난 요청 — 요청자가 받는 것 (감사 1-1, 제안서 PROP-no-result-fallback 1절 A·B·C·J).

런이 요청(``r-N``)에 `reply` 없이 끝나면 하니스가 그 요청을 닫는다. 런 끝 정리(`end_run`)를 테스트가 직접 부른다 —
워커는 문(``door``)에서 멈춘다.
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

LABEL = "(no explicit reply — this is the agent's run summary)"


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
    assert reg.end_run(f"agent:{a}", "the summary", success=True) == 1
    (content,) = _main_mail(reg)
    assert content.splitlines()[0].endswith(f"re [{debt.id}] ──")
    assert content.splitlines()[1:3] == [LABEL, "the summary"]


def test_agent_owes_another_agent(two_agents):
    """B — 에이전트가 다른 에이전트에게 빚진 채 끝났다."""
    reg, a, b = two_agents
    debt = reg.note_owes(f"agent:{a}", f"agent:{b}", "check the tests")
    assert reg.end_run(f"agent:{a}", "the summary", success=True) == 1
    (item,) = _inbox(reg, b)
    assert item["kind"] == "reply" and item["re"] == debt.id
    assert "mark" not in item
    assert item["text"] == f"{LABEL}\nthe summary"


def test_main_owes_an_agent(two_agents):
    """C — main 이 에이전트의 요청에 답하지 않고 런을 끝냈다: 사용자에게 한 답이 회신으로 간다."""
    reg, a, _b = two_agents
    debt = reg.note_owes("main", f"agent:{a}", "which branch?")
    assert reg.end_run("main", "main's answer to the user") == 1
    (item,) = _inbox(reg, a)
    assert item["re"] == debt.id
    assert item["text"] == f"{LABEL}\nmain's answer to the user"


def test_a_failed_run_owes_main(two_agents):
    """J — 실패한 런: 원인 줄이 머리에."""
    reg, a, _b = two_agents
    reg.note_owes(f"agent:{a}", "main", "do it")
    reg.end_run(f"agent:{a}", "", success=False, error="Max turns (40) reached")
    (content,) = _main_mail(reg)
    assert content.splitlines()[1:3] == [
        "(the run ended with an error: Max turns (40) reached)",
        LABEL,
    ]


def test_at_profile_command_whose_agent_does_not_reply(tmp_path, monkeypatch):
    """D — 사람의 `@<profile> 과제`: 일회용 에이전트가 `reply` 없이 끝났다. 지금은 라벨을 뗀 런의 결과가 명령의
    답(``ok`` 참)이다."""

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
    assert (answer, ok) == ("the run's own result", True)
