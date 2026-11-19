"""사람이 main 을 멈추면(■·Stop everything) main 에게 요청한 에이전트는 정지 회신을 받는다 — 에이전트의 ■ 와 같은
문장·표지(`agent_stop.stopped_reply`, ``· stopped by user``). Stop everything 이면 그 회신이 받는 쪽을 깨우지 않는다.

실제 조립(오라클 `Session`, web) — 모델 없이. 순서는 테스트가 쥔다: main 의 첫 호출 안에서 멈춤을 누르고, 그 호출이
돌아온 뒤 런이 멈춘다(도구 실행 직전 검사)."""

from __future__ import annotations

import json
import threading

import pytest

from agent_cli.render import get_renderer, set_renderer
from tests.oracle.driver import Provider, Session, call, until


@pytest.fixture(autouse=True)
def _keep_renderer():
    """오라클 `Session` 은 세션의 렌더러를 전역에 건다 — 테스트 뒤 되돌린다."""
    before = get_renderer()
    yield
    set_renderer(before)


def _scene(tmp_path, monkeypatch, *, everything: bool):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
    box: dict = {}
    entered = threading.Event()

    def main_works(msgs):
        entered.set()
        until(lambda: box.get("stopped"), "stop pressed")
        return call("shell", command="true")

    s = box["s"] = Session(tmp_path, Provider({"MAIN": [main_works]}), path="web")
    s.pump_in_thread()
    key, err = s.reg.spawn(name="helper")
    assert not err
    until(lambda: s.agent_state(key) == "idle")
    _, err = s.reg.message_handler(f"agent:{key}")("main", "which file?", True)
    assert not err
    assert entered.wait(10)
    if everything:
        s.reg.main_stop_wakes = (
            False  # 서버의 Stop everything 이 main 을 멈추기 앞에 하는 일
        )
    s.surface.stop.set()
    if everything:
        s.reg.stop_all_runs(wakes_requesters=False)
    box["stopped"] = True
    until(lambda: s.reg.reply_debts("main") == [], "main's debt answered")
    return s, key


def _history_users(tm) -> list[str]:
    path = tm.ctx.history_path if tm.ctx is not None else None
    if path is None or not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r.get("content", "") for r in rows if r.get("role") == "user"]


def test_a_stopped_main_answers_its_requester_with_a_stop_reply_that_wakes(
    tmp_path, monkeypatch
):
    s, key = _scene(tmp_path, monkeypatch, everything=False)
    try:
        tm = s.reg.get(key)
        until(lambda: tm.handled >= 1 and tm.state == "idle", "the reply opened a run")
        (got,) = _history_users(tm)
        assert got.startswith("── reply [n-") and "· stopped by user ──" in got
        assert "The user stopped main while it was working on your request [r-" in got
        assert "(no explicit reply" not in got
        assert {r.rid: r.state for r in s.reg._post.rows()} == {}
    finally:
        s.close()


def test_stop_everything_does_not_wake_the_requester(tmp_path, monkeypatch):
    s, key = _scene(tmp_path, monkeypatch, everything=True)
    try:
        queued = s.reg._post.queued(f"agent:{key}")
        assert [e.wakes for e in queued] == [False]
        assert "The user stopped main" in queued[0].text
        assert s.reg.get(key).handled == 0  # 런을 열지 않았다
        assert s.reg.main_stop_wakes is False  # 다음 main 런이 열리면 다시 선다
    finally:
        s.close()


@pytest.mark.parametrize("everything", [False, True])
def test_the_next_main_run_resets_the_quiet_mark(tmp_path, monkeypatch, everything):
    s, _key = _scene(tmp_path, monkeypatch, everything=everything)
    try:
        s.say("MAIN next", nickname="dj")
        until(lambda: s.reg.main_stop_wakes is True, "the next run reset it")
    finally:
        s.close()
