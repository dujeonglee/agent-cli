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


def test_a_failed_main_run_heads_its_no_reply_note_with_the_cause(
    tmp_path, monkeypatch
):
    """LLM 오류로 실패한 main 런 — 답 없음 회신 머리에 원인 한 줄(에이전트의 실패 런과 같은 `run_error_line`)."""

    def broken(msgs):
        raise RuntimeError("upstream 500: model server down")

    s, key = _session(tmp_path, monkeypatch, [broken] * 20)
    try:
        _, err = s.reg.message_handler(f"agent:{key}")("main", "which file?", True)
        assert not err
        tm = s.reg.get(key)
        until(
            lambda: tm.handled >= 1 and tm.state == "idle",
            "the no-reply note opened a run",
        )
        (got,) = _helper_reads(tm)
        assert got.splitlines()[:3] == [
            got.splitlines()[0],
            "(the run ended with an error: LLM call failed: upstream 500: model server down)",
            got.splitlines()[2],
        ]
        assert got.splitlines()[2].startswith(
            "main ended its run without a `reply` to your request [r-"
        ) and got.splitlines()[2].endswith(' ("which file?").')
        assert got.startswith("── reply [n-") and " from main re [r-" in got
        assert got.splitlines()[0].endswith(" · no reply ──")
    finally:
        s.close()


def test_a_main_run_that_raises_settles_its_debts_on_the_spot(tmp_path, monkeypatch):
    """web main 런이 예외로 끝난다(엔진 안의 버그 — `_execute_turn` 이 한 번 RuntimeError, 표면이 삼킴). 그 런이 진 빚은
    그 자리에서 원인 한 줄을 단 폴백으로 갚힌다: 줄·든 봉투가 남지 않고, 다음 런(사람의 다른 말)의 꼬리에 그 빚이 없고,
    상관없는 다음 런의 요약이 그 요청의 회신으로 가지 않는다."""
    import re

    from agent_cli.loop import core
    from tests.oracle.driver import call, closing_ids

    real = core.AgentLoop._execute_turn
    fired: list = []

    def boom(self):
        if self._config.kind.owner == "main" and not fired:
            fired.append(1)
            raise RuntimeError("engine bug")
        return real(self)

    monkeypatch.setattr(core.AgentLoop, "_execute_turn", boom)
    script = [
        lambda m: call("complete", result="the next run", answers=closing_ids(m))
    ] * 4
    s, key = _session(tmp_path, monkeypatch, script)
    failed: list = []
    s.surface.run_failed = lambda exc: failed.append(exc) or True  # web 처럼 삼킨다
    try:
        _, err = s.reg.message_handler(f"agent:{key}")("main", "which file?", True)
        assert not err
        tm = s.reg.get(key)
        until(lambda: failed and tm.handled >= 1 and tm.state == "idle", "settled")
        assert s.reg.reply_debts("main") == []
        assert s.reg.awaited_replies(f"agent:{key}") == []
        assert {r.rid: r.state for r in s.reg._post.rows()} == {}
        assert s.reg._post.taken("main") == ()
        (got,) = _helper_reads(tm)
        assert (
            "(the run ended with an error: internal error: RuntimeError: engine bug)"
            in got
        )
        n = len(s.provider.calls.get("MAIN", []))
        s.say("MAIN next", nickname="dj")
        until(lambda: len(s.provider.calls.get("MAIN", [])) > n, "the next run called")
        tail = s.provider.calls["MAIN"][n]["last"]
        items = re.search(r"## Open Items.*?(?=\n## |\Z)", tail, re.DOTALL).group(0)
        assert "reply owed" not in items and "MAIN next" in items
        # 예외 런의 끝 정리(실패 결과) 하나 + 다음 런의 끝 — 그 뒤에 본다
        until(lambda: len(s.surface.results) >= 2, "the next run ended")
        assert s.surface.results[-1].output == "the next run"
        assert len(_helper_reads(tm)) == 1  # 다음 런의 요약이 회신으로 가지 않는다
        assert s.reg.get(key).handled == 1
    finally:
        s.close()
