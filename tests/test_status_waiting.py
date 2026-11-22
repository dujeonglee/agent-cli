"""status.json 이 기다림을 어떻게 말하는가 (표시 조각 A1) — 실제 조립(오라클 `Session`, web)에 진짜 `WebRenderer`,
모델 없이.

장면 넷(제안서 §2.2 의 표): main 이 에이전트의 회신을 기다림 / main 이 작업만 기다림 / main 한가·에이전트가 다른
에이전트의 회신을 기다림 / main 한가·에이전트가 작업만 기다림. 동기 지점은 제품이 판정에 쓰는 값이다 — 호출 안(대본이
잡은 모델 호출), 기다림 표시(`PostOffice.is_waiting`), 작업 목록, 한가함. 파일은 그 뒤 제품이 쓴 것을 읽는다.

기다리는 쪽은 "일하는 중" 에 세지 않는다(`agents.working`) — 작업 상태 ``busy`` 는 그대로다. main 의 `waiting` 은
받아들여진 `wait` 의 대기 줄과 함께 선다.
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.instance_file import read_status_file
from agent_cli.render import get_renderer, set_renderer
from agent_cli.render.web import WebRenderer
from tests.oracle.driver import (
    Provider,
    Session,
    _agent_key,
    call,
    complete_all,
    until,
)

JOB = "sleep 60"


@pytest.fixture(autouse=True)
def _keep_renderer():
    before = get_renderer()
    yield
    set_renderer(before)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
    return tmp_path


class Scene:
    """대본 하나를 web 조립으로 돌린다 — 그리는 쪽은 진짜 `WebRenderer`(status.json 을 쓴다)."""

    def __init__(self, root, scripts: dict):
        self.gate = threading.Event()
        self.inside: set[str] = set()
        scripts = {
            tag: [*steps, *([complete_all(f"{tag} more")] * 6)]
            for tag, steps in scripts.items()
        }
        self.s = Session(root, Provider(scripts), path="web")
        web = WebRenderer(session_dir=str(self.s.sdir))
        self.s.recorder._inner = web
        self.s.surface.idle = web.worker_idle
        self.s.surface.busy = web.worker_busy
        self.pump = None

    def held(self, tag: str, step):
        """그 배우의 모델 호출을 ``gate`` 까지 잡는다 — "일하는 중" 의 순간."""

        def go(_messages):
            self.inside.add(tag)
            self.gate.wait(60)
            return step

        return go

    def start(self) -> None:
        self.s.say("MAIN go", nickname="dj")
        self.pump = self.s.pump_in_thread()

    def waiting(self, addr: str) -> bool:
        return self.s.main_input.post.is_waiting(addr)

    def agent(self, name: str) -> str:
        return f"agent:{_agent_key(self.s, name)}"

    def status(self) -> dict:
        return read_status_file(self.s.sdir) or {}

    def status_when(self, pred, what: str) -> dict:
        """제품이 쓴 status.json 이 ``pred`` 를 채울 때의 값 — 전이마다 다시 쓰는 파일이라 그 값을 기다린다."""
        until(lambda: pred(self.status()), what)
        return self.status()

    def close(self) -> None:
        self.gate.set()
        self.s.stores.jobs.stop_all()
        self.s.close()
        if self.pump is not None:
            self.pump.join(20)


def agents_of(st: dict) -> tuple:
    a = st.get("agents") or {}
    return (
        a.get("working"),
        a.get("alive"),
        sorted((x["name"], x["state"]) for x in a.get("list", [])),
    )


def waiting_of(st: dict) -> tuple:
    a = st.get("agents") or {}
    return (a.get("waiting"), sorted(x["name"] for x in a["list"] if x["waiting"]))


@pytest.fixture
def scene(home):
    made: list[Scene] = []

    def make(scripts: dict) -> Scene:
        sc = Scene(home, scripts)
        made.append(sc)
        return sc

    yield make
    for sc in made:
        sc.close()


def test_main_waits_for_a_reply_while_the_agent_works(scene):
    box: dict = {}
    sc = box["sc"] = scene(
        {
            "MAIN": [
                call("agent", mode="spawn", name="aa"),
                lambda m: call("message", to="aa", text="TASK-A work"),
                call("wait"),
            ],
            "TASK-A": [
                lambda m: box["sc"].held(
                    "aa", call("complete", result="aa done", answers=[])
                )(m)
            ],
        }
    )
    sc.start()
    until(lambda: "aa" in sc.inside and sc.waiting("main"), "main waits, aa in a call")
    st = sc.status_when(
        lambda st: (
            st.get("waiting") is True and (st.get("agents") or {}).get("working") == 1
        ),
        "main waiting, aa counted working",
    )
    assert st["busy"] is True
    assert agents_of(st) == (1, 1, [("aa", "busy")])
    assert waiting_of(st) == (0, [])


def test_main_waits_for_a_job_only(scene):
    sc = scene({"MAIN": [call("shell", command=JOB, background=True), call("wait")]})
    sc.start()
    until(
        lambda: bool(sc.s.stores.jobs.running()) and sc.waiting("main"),
        "main waits on a job",
    )
    st = sc.status_when(lambda st: st.get("waiting") is True, "main waiting")
    assert st["busy"] is True  # 기다림은 작업 구간 안이다
    assert "agents" not in st


def test_idle_main_and_an_agent_waiting_for_another_agent(scene):
    box: dict = {}
    sc = box["sc"] = scene(
        {
            "MAIN": [
                call("agent", mode="spawn", name="aa"),
                call("agent", mode="spawn", name="bb"),
                lambda m: call(
                    "message", to="aa", text="TASK-A ask bb", expects_reply=False
                ),
                lambda m: call("complete", result="handed off", answers=[]),
            ],
            "TASK-A": [
                lambda m: call("message", to="bb", text="TASK-B compute"),
                call("wait"),
            ],
            "TASK-B": [
                lambda m: box["sc"].held(
                    "bb", call("complete", result="bb done", answers=[])
                )(m)
            ],
        }
    )
    sc.start()
    until(
        lambda: "bb" in sc.inside and sc.s.main_idle() and sc.waiting(sc.agent("aa")),
        "aa waits, bb in a call, main idle",
    )
    st = sc.status_when(
        lambda st: (
            st.get("busy") is False and (st.get("agents") or {}).get("waiting") == 1
        ),
        "main idle, aa counted waiting",
    )
    assert "waiting" not in st  # main 은 기다리지 않는다
    assert agents_of(st) == (1, 2, [("aa", "busy"), ("bb", "busy")])
    assert waiting_of(st) == (1, ["aa"])


def test_idle_main_and_an_agent_waiting_for_a_job_only(scene):
    sc = scene(
        {
            "MAIN": [
                call("agent", mode="spawn", name="aa"),
                lambda m: call(
                    "message", to="aa", text="TASK-A run a job", expects_reply=False
                ),
                lambda m: call("complete", result="handed off", answers=[]),
            ],
            "TASK-A": [call("shell", command=JOB, background=True), call("wait")],
        }
    )
    sc.start()
    until(
        lambda: (
            bool(sc.s.stores.jobs.running())
            and sc.s.main_idle()
            and sc.waiting(sc.agent("aa"))
        ),
        "aa waits on a job, main idle",
    )
    st = sc.status_when(
        lambda st: (
            st.get("busy") is False and (st.get("agents") or {}).get("waiting") == 1
        ),
        "main idle, aa counted waiting",
    )
    assert "waiting" not in st
    assert agents_of(st) == (0, 1, [("aa", "busy")])
    assert waiting_of(st) == (1, ["aa"])
