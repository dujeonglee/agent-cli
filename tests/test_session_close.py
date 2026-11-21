"""세션을 닫을 때 main 의 런 (5c-pre) — 실제 조립(오라클 `Session`, web), 모델 없이.

닫기는 웹 호스트와 같은 순서다: main 상자를 닫고(`stop_pump` — `WebServer.shutdown`), 정리(`teardown_session` — 에이전트를
멈춘다). 대본의 동기 지점은 제품이 판정에 쓰는 값이다 — main 의 제자리 대기 시작(`renderer.wait_in_place(True)` → 기록기의
``main_waiting``), 에이전트의 평생 정지(``stop_event``), 상자의 닫힘(`stop_pump` 가 돌아온 뒤).
"""

from __future__ import annotations

import json
import threading

import pytest

from agent_cli.render import get_renderer, set_renderer
from tests.oracle.driver import (
    Provider,
    Session,
    _agent_key,
    call,
    complete_all,
    owed_id,
    until,
)


@pytest.fixture(autouse=True)
def _keep_renderer():
    """오라클 `Session` 은 세션의 렌더러를 전역에 건다 — 테스트 뒤 되돌린다."""
    before = get_renderer()
    yield
    set_renderer(before)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
    return tmp_path


def _main_records(s) -> list[dict]:
    path = s.sdir / "history.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _close_while_main_waits(home, *, mode: str):
    """main 이 에이전트(``mode`` "spawn" 은 상주, "run" 은 일회용)의 회신을 제자리에서 기다리는 중 세션을 닫는다.
    에이전트는 모델 호출 안에서 붙잡혀 있다가, 정리가 그를 멈추면(평생 정지) 풀린다. 돌려주는 것:
    ``(s, 닫는 도중 main 의 모델 호출 수)``."""
    hold = threading.Event()

    def agent_works(m):
        until(hold.is_set, "released")
        return call("reply", id=owed_id(m), text="late")

    if mode == "spawn":
        main = [
            call("agent", mode="spawn", name="aa"),
            call("message", to="aa", text="TASK-A look"),
            call("wait"),
        ]
    else:
        main = [call("agent", mode="run", task="TASK-A look"), call("wait")]
    p = Provider(
        {"MAIN": main + [complete_all("main after")] * 4, "TASK-A": [agent_works]}
    )
    s = Session(home, p, path="web")
    s.say("MAIN go", nickname="dj")
    pump = s.pump_in_thread()
    assert s.recorder.main_waiting.wait(20)  # main 이 제자리 대기에 들었다
    before = len(p.calls["MAIN"])
    s.stop_pump()  # 웹 서버가 내려간다 — main 상자가 닫힌다
    closer = threading.Thread(target=s.close, daemon=True)  # 정리 — 에이전트를 멈춘다
    closer.start()
    key = _agent_key(s, "aa") if mode == "spawn" else s.agent_keys()[0]
    until(lambda: s.reg.get(key).stop_event.is_set(), "agent stopped by the close")
    hold.set()
    until(lambda: not closer.is_alive() and not pump.is_alive(), "closed")
    return s, len(p.calls["MAIN"]) - before


@pytest.mark.parametrize("mode", ["spawn", "run"])
def test_closing_while_main_waits_for_a_reply(home, mode):
    """`[5c-pre 에서 바뀜]` 지금: 정리가 에이전트를 멈추면 main 의 기다림에서 깨울 것이 사라져("nothing") 런이 이어지고,
    닫는 도중 모델을 한 번 더 부른다 — 그 답은 아무에게도 가지 않고 history 에만 남는다."""
    s, calls = _close_while_main_waits(home, mode=mode)
    assert calls == 1
    tail = [(r.get("role"), r.get("tool"), r.get("content")) for r in _main_records(s)]
    assert (
        "user",
        "wait",
        "Observation: nothing you were waiting on is running any more.",
    ) in tail[-2:]
    assert not any(tool == "interrupt" for _, tool, _ in tail)


def test_a_working_main_goes_on_while_the_session_closes(home):
    """일하는 중(모델 호출 도중)이던 main — 닫힌 뒤 그 호출이 돌아오면 도구가 돌고 한 번 더 부른다. 5c-pre 는 이 길을
    바꾸지 않는다(고칠 후보 — 5e)."""
    gate = threading.Event()
    inside = threading.Event()

    def main_works(m):
        inside.set()
        until(gate.is_set, "released")
        return call("shell", command="true")

    p = Provider({"MAIN": [main_works] + [complete_all("main after")] * 3})
    s = Session(home, p, path="web")
    s.say("MAIN go", nickname="dj")
    pump = s.pump_in_thread()
    assert inside.wait(20)
    before = len(p.calls["MAIN"])
    s.stop_pump()
    closer = threading.Thread(target=s.close, daemon=True)
    closer.start()
    gate.set()
    until(lambda: not closer.is_alive() and not pump.is_alive(), "closed")
    assert len(p.calls["MAIN"]) - before == 1
    tools = [r.get("tool") for r in _main_records(s) if r.get("role") == "user"]
    assert "shell" in tools and "interrupt" not in tools
