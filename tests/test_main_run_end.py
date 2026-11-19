"""main 의 런이 끝나는 길마다 main 에게 요청한 에이전트가 받는 것 — 에이전트의 런 끝과 같은 규칙.

실제 조립(오라클 `Session`, web) — 모델 없이. 상주 helper 가 main 에게 회신을 기대하는 `message` 를 보내고(그 핸들러를
직접 부른다), 그 메일이 연 main 의 런이 각 길로 끝난다. 순서는 조건 대기로 쥔다."""

from __future__ import annotations

import json

import pytest

from agent_cli.render import get_renderer, set_renderer
from tests.oracle.driver import Provider, Session, until


@pytest.fixture(autouse=True)
def _keep_renderer():
    before = get_renderer()
    yield
    set_renderer(before)


def _helper_reads(tm) -> list[str]:
    rows = [
        json.loads(line)
        for line in tm.ctx.history_path.read_text(encoding="utf-8").splitlines()
    ]
    return [r.get("content", "") for r in rows if r.get("role") == "user"]


def _session(tmp_path, monkeypatch, script):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
    s = Session(tmp_path, Provider({"MAIN": script}), path="web")
    s.pump_in_thread()
    key, err = s.reg.spawn(name="helper")
    assert not err
    until(lambda: s.agent_state(key) == "idle")
    return s, key


def test_a_failed_main_run_heads_its_fallback_with_the_cause(tmp_path, monkeypatch):
    """LLM 오류로 실패한 main 런 — 폴백 회신 머리에 원인 한 줄(에이전트의 실패 런과 같은 `run_error_line`)."""

    def broken(msgs):
        raise RuntimeError("upstream 500: model server down")

    s, key = _session(tmp_path, monkeypatch, [broken] * 20)
    try:
        _, err = s.reg.message_handler(f"agent:{key}")("main", "which file?", True)
        assert not err
        tm = s.reg.get(key)
        until(
            lambda: tm.handled >= 1 and tm.state == "idle", "the fallback opened a run"
        )
        (got,) = _helper_reads(tm)
        assert got.splitlines()[:3] == [
            got.splitlines()[0],
            "(the run ended with an error: LLM call failed: upstream 500: model server down)",
            "(no explicit reply — this is the agent's run summary)",
        ]
        assert got.startswith("── reply [n-") and " from main re [r-" in got
    finally:
        s.close()
