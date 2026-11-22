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


def _alive_timers() -> set[threading.Timer]:
    return {
        t
        for t in threading.enumerate()
        if isinstance(t, threading.Timer) and t.is_alive()
    }


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
    """main 상자가 닫히면 제자리 대기가 끝나고 런은 세션 종료로 끝난다 — 닫는 도중 모델을 다시 부르지 않고, 마지막 레코드는
    받아들여진 `wait` 의 관찰이다. `[interrupt]` 기록·정지 회신·체크인 없음. (종전: 정리가 에이전트를 멈추면 깨울 것이
    사라져 "nothing you were waiting on is running any more." 를 붙이고 모델을 한 번 더 불렀다 — 그 답은 받을 이가 없었다.)"""
    timers_before = (
        _alive_timers()
    )  # 같은 프로세스의 다른 테스트가 남긴 타이머는 이 장면의 것이 아니다
    s, calls = _close_while_main_waits(home, mode=mode)
    assert calls == 0
    records = _main_records(s)
    last = records[-1]
    assert last.get("tool") == "wait"
    assert str(last.get("content")).startswith("Observation: waiting — will wake on:")
    assert not any(r.get("tool") == "interrupt" for r in records)
    assert "run_ended" not in [r[0] for r in s.recorder.lanes.get("pump", [])]
    left = _alive_timers() - timers_before  # 이 세션이 건 체크인 타이머가 남지 않았다
    assert not left, [(t.function, t.args, t.interval) for t in left]


def test_closing_main_s_box_alone_ends_the_in_place_wait(home):
    """정리(에이전트 멈춤) 전에 끝난다 — 상자가 닫힌 것만으로 제자리 대기가 끝나고 펌프가 끝난다. 에이전트는 아직 살아
    모델 호출 안에 있다."""
    hold = threading.Event()

    def aa_works(m):
        until(hold.is_set, "released")
        return call("reply", id=owed_id(m), text="late")

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="aa"),
                call("message", to="aa", text="TASK-A look"),
                call("wait"),
            ]
            + [complete_all("main after")] * 4,
            "TASK-A": [aa_works],
        }
    )
    s = Session(home, p, path="web")
    s.say("MAIN go", nickname="dj")
    pump = s.pump_in_thread()
    assert s.recorder.main_waiting.wait(20)
    before = len(p.calls["MAIN"])
    s.stop_pump()
    until(lambda: not pump.is_alive(), "the in-place wait ended with the box")
    assert len(p.calls["MAIN"]) == before
    assert (
        s.agent_state(_agent_key(s, "aa")) == "busy"
    )  # 정리 전 — 에이전트는 아직 일한다
    hold.set()
    s.close()


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


# ── 단위 — 메일함의 닫힘·런 끝의 종류·런 끝 정리 ──────────────────────────


def test_mailbox_wait_reports_closed_only_with_a_closed_callable():
    from unittest.mock import MagicMock

    from agent_cli.wake import Mailbox

    reg = MagicMock()
    still = lambda: True
    reg.wait_mail.return_value = "nothing"
    assert (
        Mailbox(reg, "agent:a").wait(stop_event=None, still_waiting=still) == "nothing"
    )
    assert (
        reg.wait_mail.call_args.kwargs["still_waiting"] is still
    )  # 닫힘이 없는 메일함은 그대로 넘긴다
    shut = Mailbox(reg, "main", closed=lambda: True)
    assert shut.wait(stop_event=None, still_waiting=still) == "closed"
    assert (
        reg.wait_mail.call_args.kwargs["still_waiting"]() is False
    )  # 닫혔으면 더 기다리지 않는다
    open_box = Mailbox(reg, "main", closed=lambda: False)
    assert open_box.wait(stop_event=None, still_waiting=still) == "nothing"


def test_a_session_ended_run_is_session_end_and_its_end_settles_nothing():
    from unittest.mock import MagicMock

    from agent_cli.actor import RunEnd
    from agent_cli.pump import MainActor
    from agent_cli.tools.result import ToolResult

    end = RunEnd.of(ToolResult(True, session_ended=True), stopped=True)
    assert end.outcome == "session_end" and not end.ok
    deps = MagicMock()
    actor = MainActor(deps)
    assert actor.ended(MagicMock(), end) is False
    deps.agent_registry.answer_main_debts_as_stopped.assert_not_called()
    deps.agent_registry.run_ended.assert_not_called()
    deps.auditor.run_ended.assert_not_called()
    deps.surface.run_ended.assert_not_called()
    deps.surface.bind_stop.assert_called_once_with(None)


# ── 일하는 중에 닫힐 때 (5e1 — 지금 값 고정) ──────────────────────────────


def _records(s, key: str | None = None) -> list[dict]:
    """main(``key`` 가 None) 또는 에이전트 ``key`` 의 history 레코드."""
    if key is None:
        return _main_records(s)
    path = next(p for p in s.sdir.rglob("history.jsonl") if key in str(p))
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _close_mid_work(home, scene: str):
    """세션을 일하는 중에 닫는다 — 웹 호스트와 같은 순서(main 상자를 닫고 정리). ``scene``:

    - ``main_call``: main 이 모델 호출 도중이고, aa 가 물어 main 이 회신을 빚졌다.
    - ``main_shell``: main 이 전경 `shell` 도중.
    - ``resident``: 상주 aa 가 모델 호출 도중, main 은 한가.
    - ``run_agent``: 일회용 에이전트가 모델 호출 도중, main 은 그 회신을 제자리에서 기다림.

    돌려주는 것: ``(s, 닫기 시작한 뒤의 배우별 모델 호출 수, 에이전트 키들)``."""
    box: dict = {}
    gate, inside = threading.Event(), threading.Event()
    flag = home / "shell.flag"

    def slow(step):
        def go(m):
            inside.set()
            until(gate.is_set, "released")
            return step

        return go

    def after_aa_asked(m):
        until(
            lambda: any(
                "request [" in str(x.get("text")) for x in box["s"].reg.pending_mail()
            ),
            "aa asked main",
        )
        return call("shell", command="true")

    scripts = {
        "main_call": {
            "MAIN": [
                call("agent", mode="spawn", name="aa"),
                lambda m: call(
                    "message",
                    to="aa",
                    text="TASK-A ask me which file",
                    expects_reply=False,
                ),
                after_aa_asked,
                slow(call("shell", command="true")),
            ]
            + [complete_all("main after")] * 12,
            "TASK-A": [
                lambda m: call("message", to="main", text="which file should I use?"),
                lambda m: call("complete", result="asked main", answers=[]),
            ]
            + [complete_all("aa more")] * 4,
        },
        "main_shell": {
            "MAIN": [call("shell", command=f"touch {flag}; sleep 2")]
            + [complete_all("main after")] * 6
        },
        "resident": {
            "MAIN": [
                call("agent", mode="spawn", name="aa"),
                lambda m: call("message", to="aa", text="TASK-A work"),
            ]
            + [complete_all("main done")] * 10,
            "TASK-A": [slow(call("shell", command="true"))]
            + [complete_all("aa after")] * 4,
        },
        "run_agent": {
            "MAIN": [
                call("agent", mode="run", task="TASK-R count things"),
                call("wait"),
            ]
            + [complete_all("main after")] * 6,
            "TASK-R": [slow(call("shell", command="true"))]
            + [complete_all("r after")] * 4,
        },
    }
    p = Provider(scripts[scene])
    s = box["s"] = Session(home, p, path="web")
    s.say("MAIN go", nickname="dj")
    pump = s.pump_in_thread()
    if scene == "main_call":
        until(
            lambda: inside.is_set() and len(s.reg.reply_debts("main")) == 1,
            "main in a call, owing aa",
        )
    elif scene == "main_shell":
        until(flag.exists, "foreground shell running")
    else:
        until(
            lambda: inside.is_set() and (scene != "resident" or s.main_idle()),
            "agent in a call",
        )
    keys = s.agent_keys()
    before = {k: len(v) for k, v in p.calls.items()}
    closer = threading.Thread(target=s.close, daemon=True)
    closer.start()
    until(
        lambda: (
            s.main_input.closed()
            and (
                scene.startswith("main")
                or all(s.reg.get(k).stop_event.is_set() for k in keys)
            )
        ),
        "the close reached its stop",
    )
    gate.set()
    until(lambda: not closer.is_alive() and not pump.is_alive(), "closed")
    calls = {k: len(v) - before.get(k, 0) for k, v in p.calls.items()}
    return s, calls, keys


def test_closing_while_main_is_in_a_model_call_owing_a_reply(home):
    """`[5e1 에서 바뀜]` 지금: 그 호출이 돌아오면 도구가 돌고, 빚 때문에 `complete` 가 되돌아와 되묻기 예산을 다 쓴다 —
    닫는 도중 모델 호출 9번, history 끝은 되묻기 관찰."""
    s, calls, _ = _close_mid_work(home, "main_call")
    assert calls["MAIN"] == 9
    last_user = [r for r in _records(s) if r.get("role") == "user"][-1]
    assert last_user.get("tool") == "complete"
    assert str(last_user.get("content")).startswith(
        "Observation: your result was delivered, but 1 item is still open:"
    )


def test_closing_while_main_runs_a_foreground_shell(home):
    """`[5e1 에서 바뀜]` 지금: 명령이 끝까지 돌고("(no output)") 모델을 한 번 더 부른다."""
    s, calls, _ = _close_mid_work(home, "main_shell")
    assert calls["MAIN"] == 1
    shell = [r for r in _records(s) if r.get("tool") == "shell"][-1]
    assert shell.get("content") == "Observation: (no output)"


@pytest.mark.parametrize("scene", ["resident", "run_agent"])
def test_closing_while_an_agent_is_in_a_model_call(home, scene):
    """`[5e1 에서 바뀜]` 지금: 에이전트는 모델을 다시 부르지 않지만, history 끝에 사람의 정지와 같은 기록
    "⚡ User interrupted. Waiting for new instructions." 이 남고(사람은 멈추지 않았다) 처리 수가 1 이다."""
    s, calls, (key,) = _close_mid_work(home, scene)
    assert sum(calls.values()) == 0
    last = _records(s, key)[-1]
    assert last.get("tool") == "interrupt"
    assert last.get("content") == "⚡ User interrupted. Waiting for new instructions."
    (row,) = s.reg.roster_snapshot()
    assert row["handled"] == 1
