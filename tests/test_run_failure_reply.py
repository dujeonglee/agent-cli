"""실패로 끝난 에이전트 런의 회신에 원인 한 줄 (v10.143.0, docs/agent-engine/DESIGN.md §3).

실제 레지스트리·실제 엔진(`run_loop`)·가짜 provider. 갚지 않은 채 런이 실패하면(턴 상한·provider 오류)
산출물이 비어 있다 — 종전엔 엔진의 사유(`ToolResult.error`)가 버려져 요청자가 "왜" 를 몰랐고, 에이전트
요청자는 실패 표시조차 받지 못했다. 요청자가 main 이든 에이전트든, run 이든 상주든 같은 줄을 받는다.
기다림은 상태(에이전트 dead·idle, 메일 도착)로만 본다 — 시각에 기대는 단언은 없다.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from agent_cli.loop.run import run_loop
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.session_slots import set_agent_registry
from agent_cli.subagent.agent_mail import build_reply_record
from agent_cli.subagent.agents_live import AgentRegistry
from agent_cli.tools.result import ToolResult
from tests.binding_support import make_binding

CAPS = ModelCapabilities(
    context_window=65536, max_output_tokens=4096, supports_thinking=False
)
LABEL = "(no explicit reply — this is the agent's run summary)"


class _Provider:
    """``mode``: "loop"(매 턴 같은 읽기 — 턴 상한까지) · "down"(provider 오류) · "reply"(곧바로 `complete`)."""

    def __init__(self, mode: str):
        self.mode = mode

    def call(self, messages, system, model, capabilities, **kw):
        if self.mode == "down":
            raise RuntimeError("upstream 502 Bad Gateway")
        if self.mode == "reply":
            return LLMResponse(
                content=json.dumps({"action": "complete", "result": "all good"})
            )
        return LLMResponse(
            content=json.dumps({"action": "read_file", "path": "missing.txt"})
        )


def _until(pred, timeout=20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """레지스트리 하나 — 요청자 에이전트(``asker``)는 받은 글만 적고 끝나는 가짜 엔진, 그 밖은 실제 엔진."""
    import agent_cli.render as render_mod

    class _Quiet:
        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(render_mod, "get_renderer", lambda: _Quiet())
    built: dict = {"asked": []}

    def make(mode: str):
        binding = make_binding(provider=_Provider(mode), capabilities=CAPS, model="m")

        def engine(kind, start, ctx):
            if kind.tm.key == built.get("asker"):
                built["asked"].append(start.query)
                return ToolResult(True, output="noted"), 0.0
            return run_loop(kind, start, ctx), 0.0

        reg = AgentRegistry(
            tmp_path / "s",
            runtime={"binding": binding, "max_turns": 3, "depth": 0, "max_depth": 2},
            runner=engine,
        )
        set_agent_registry(reg)
        built["reg"] = reg
        return reg

    yield built, make
    if built.get("reg") is not None:
        built["reg"].shutdown_all()
    set_agent_registry(None)


def _asker(built, reg) -> str:
    key, err = reg.spawn(name="asker")
    assert not err
    assert _until(lambda: reg.get(key).state == "idle")
    built["asker"] = key
    return f"agent:{key}"


def _ask(reg, author: str, *, run: bool) -> str:
    key, err = reg.spawn(
        ends_after_task=run, name="" if run else "worker", parent=author
    )
    assert not err
    if not run:
        assert _until(lambda: reg.get(key).state == "idle")
    reg.request(key, "find every caller of parse_tokens", author=author)
    return key


def _main_reply(reg) -> str:
    assert _until(reg.has_pending_replies)
    (mail,) = reg.drain_replies()
    return build_reply_record(mail, registry=reg)["content"]


class TestMainRequester:
    def test_turn_limit_cause_reaches_main(self, world):
        _, make = world
        reg = make("loop")
        key = _ask(reg, "main", run=True)
        assert _until(lambda: reg.get(key).state == "dead")
        body = _main_reply(reg)
        assert f"(the run ended with an error: Max turns (3) reached)\n{LABEL}" in body

    def test_provider_error_cause_reaches_main(self, world):
        _, make = world
        reg = make("down")
        key = _ask(reg, "main", run=True)
        assert _until(lambda: reg.get(key).state == "dead")
        body = _main_reply(reg)
        assert (
            "(the run ended with an error: LLM call failed: upstream 502 Bad Gateway)"
            in body
        )

    def test_resident_failed_run_names_the_cause_too(self, world):
        _, make = world
        reg = make("loop")
        key = _ask(reg, "main", run=False)
        body = _main_reply(reg)
        assert "(the run ended with an error: Max turns (3) reached)" in body
        assert _until(lambda: reg.get(key).state == "idle")  # 상주는 남는다

    def test_mail_keeps_the_cause_only_on_failure(self, world):
        """agents.json 미러의 메일 모양 — ``error`` 는 실패한 런에만 더해진다."""
        _, make = world
        reg = make("reply")
        key = _ask(reg, "main", run=True)
        assert _until(lambda: reg.get(key).state == "dead")
        assert _until(reg.has_pending_replies)
        (mail,) = reg.drain_replies()
        assert mail["success"] is True and "error" not in mail
        assert "run ended with an error" not in build_reply_record(mail)["content"]


class TestAgentRequester:
    def test_turn_limit_cause_reaches_the_requesting_agent(self, world):
        built, make = world
        reg = make("loop")
        asker = _asker(built, reg)
        key = _ask(reg, asker, run=True)
        assert _until(lambda: reg.get(key).state == "dead")
        assert _until(lambda: built["asked"])
        (got,) = built["asked"]
        head, line, label = got.splitlines()[:3]
        assert head.startswith("── reply [n-") and f"from {key}" in head
        assert line == "(the run ended with an error: Max turns (3) reached)"
        assert label == LABEL

    def test_provider_error_cause_reaches_the_requesting_agent(self, world):
        built, make = world
        reg = make("down")
        asker = _asker(built, reg)
        _ask(reg, asker, run=True)
        assert _until(lambda: built["asked"])
        assert (
            "(the run ended with an error: LLM call failed: upstream 502 Bad Gateway)\n"
            in built["asked"][0]
        )

    def test_crash_marks_the_reply_failed_without_a_cause_line(
        self, world, monkeypatch
    ):
        """엔진 예외 — 사유는 산출물("agent internal error: …")에 있으니 머리는 접두만."""
        built, make = world
        reg = make("reply")
        asker = _asker(built, reg)

        def boom(kind, start, ctx):
            raise RuntimeError("tool registry exploded")

        monkeypatch.setattr("tests.test_run_failure_reply.run_loop", boom)
        _ask(reg, asker, run=True)
        assert _until(lambda: built["asked"])
        lines = built["asked"][0].splitlines()
        assert lines[1:4] == [
            "(the run ended with an error)",
            LABEL,
            "agent internal error: RuntimeError: tool registry exploded",
        ]

    def test_successful_fallback_has_no_error_line(self, world):
        built, make = world
        reg = make("reply")
        asker = _asker(built, reg)
        _ask(reg, asker, run=True)
        assert _until(lambda: built["asked"])
        assert "run ended with an error" not in built["asked"][0]
        assert f"{LABEL}\nall good" in built["asked"][0]


def test_human_stop_is_a_stopped_reply_not_an_error(world):
    """사람의 ■ 는 실패가 아니다 — P3 의 stopped 회신 그대로(원인은 머리 표지), 오류 줄 없음."""
    built, make = world
    gate = threading.Event()
    reg = make("loop")
    real = reg._runner

    def held(kind, start, ctx):
        if kind.tm.key != built.get("asker"):
            gate.wait(10)
        return real(kind, start, ctx)

    reg._runner = held
    asker = _asker(built, reg)
    key = _ask(reg, asker, run=True)
    assert _until(lambda: reg.get(key).state == "busy")
    assert reg.stop_run(key) == ""
    gate.set()
    assert _until(lambda: built["asked"])
    got = built["asked"][0]
    assert "· stopped by user ──" in got.splitlines()[0]
    assert "run ended with an error" not in got
