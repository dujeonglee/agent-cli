"""읽은 값을 내는 일은 읽기와 내기가 한 걸음이다 — 옛 값이 새 값을 덮지 않는다.

다른 곳의 상태를 떠서 화면(스티키)·status.json 으로 내는 자리 다섯: 에이전트 명단(`AgentRegistry._notify_roster`),
입력창 위 대기열(`WebServer.broadcast_queue`), 백그라운드 작업(`JobRegistry._changed`), 세션 노브
(`WebServer.broadcast_knobs`), status.json(`WebRenderer._publish_status`). 여러 스레드가 부른다. "뜬다" 와 "낸다" 사이가
묶여 있지 않으면, 먼저 뜬 쪽이 나중에 내 옛 값이 남는다 — 다음 변화가 올 때까지 방과 보드가 낡은 상태를 본다.

각 테스트는 그 엇갈림을 조건 대기로 세운다(sleep 없음): 첫째 스레드가 뜬 뒤 붙들린 사이 둘째가 상태를 바꿔 새 값을
내려 한다.

``[한 걸음에서 바뀜]`` 표지가 붙은 테스트는 그 자리가 아직 묶이지 않은 지금 값이다 — 그 자리를 고치는 커밋에서 뒤집힌다.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from agent_cli.context.manager import ContextManager
from agent_cli.instance_file import read_status_file
from agent_cli.jobs import JobRegistry
from agent_cli.knobs import KNOB_BY_NAME, web_state, web_states
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.render.web import WebRenderer
from agent_cli.web.server import WebServer
from tests.binding_support import make_binding
from tests.test_agents_live import (
    make_registry,
    renderer,  # noqa: F401
    wait_until,
)


class Overlap:
    """묶이지 않은 자리의 엇갈림 — 첫째 스레드를 "뜬 뒤" 에 붙들고, 둘째가 새 값을 다 낸 뒤 놓는다."""

    def __init__(self):
        self.first_read = threading.Event()
        self.second_done = threading.Event()

    def hold_first(self) -> None:
        """뜨는 함수 안에서 부른다 — 첫째 스레드면 붙든다."""
        if threading.current_thread().name == "first":
            self.first_read.set()
            assert self.second_done.wait(10)

    def run(self, first, second) -> None:
        t = threading.Thread(target=first, name="first")
        t.start()
        assert self.first_read.wait(10)
        second()  # 상태를 바꾸고 새 값을 낸다
        self.second_done.set()
        t.join(10)
        assert not t.is_alive()


class Step:
    """제품의 "한 걸음" 락 자리에 끼운다 — 누가 그 걸음 안에 있을 때 다른 스레드가 오면 ``knocked`` 를 세우고 기다린다."""

    def __init__(self):
        self._lock = threading.Lock()
        self.knocked = threading.Event()

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self.knocked.set()
            self._lock.acquire()

    def __exit__(self, *exc):
        self._lock.release()


class OneStep:
    """묶인 자리의 엇갈림 — 첫째 스레드가 걸음 안에서 "뜬 뒤" 붙들린 사이, 둘째가 상태를 바꾸고 그 걸음의 문을
    두드린다(기다린다). 그때 첫째를 놓는다: 첫째가 뜬 값을 내고 나가면 둘째가 새로 떠서 낸다."""

    def __init__(self):
        self.step = Step()
        self.first_read = threading.Event()

    def hold_first(self) -> None:
        """뜨는 함수 안에서 부른다 — 첫째 스레드면 둘째가 문을 두드릴 때까지 붙든다."""
        if threading.current_thread().name == "first":
            self.first_read.set()
            assert self.step.knocked.wait(10)

    def run(self, first, second) -> None:
        a = threading.Thread(target=first, name="first")
        a.start()
        assert self.first_read.wait(10)
        b = threading.Thread(target=second, name="second")
        b.start()
        for t in (a, b):
            t.join(10)
            assert not t.is_alive()


def test_the_roster_shown_last_is_the_newest(tmp_path, renderer, monkeypatch):  # noqa: F811
    """명단 — 한 알림이 명단을 뜬 뒤 내기 전에 다른 스레드가 기다림 표시를 세워도, 마지막에 나간 명단이 지금의
    명단이다. 종전엔 먼저 뜬 명단이 나중에 나가 표시가 선 것을 덮었다(다음 알림까지 방·보드가 옛 상태)."""
    reg = make_registry(tmp_path)
    key, _ = reg.spawn(name="aa")
    assert wait_until(lambda: reg.get(key).state == "idle")
    ov, real = OneStep(), reg.roster_snapshot
    reg._roster_step = ov.step
    monkeypatch.setattr(reg, "roster_snapshot", lambda: (real(), ov.hold_first())[0])
    try:
        ov.run(reg._notify_roster, lambda: reg.mark_waiting(f"agent:{key}"))
        shown = renderer.named("agent_roster")[-1][1]["roster"][0]
        assert real()[0]["waiting"] is True
        assert shown["waiting"] is True
    finally:
        reg.shutdown_all()


def test_the_queue_shown_last_is_the_newest(monkeypatch):
    """대기열 — 한 알림이 대기열을 뜬 뒤 내기 전에 펌프가 그 말을 꺼내도, 마지막에 나간 대기열이 지금의 것이다.
    종전엔 이미 꺼낸 말이 입력창 위에 남았다."""
    r = WebRenderer()
    server = WebServer(r, token="t")
    server.enqueue("c1", "hi")
    ov, real = OneStep(), server.queue_snapshot
    server._queue_step = ov.step
    monkeypatch.setattr(server, "queue_snapshot", lambda: (real(), ov.hold_first())[0])
    ov.run(server.broadcast_queue, lambda: server.main_input.wait_to_open(0))
    assert real() == []
    assert r.hub.sticky_payload("queue")["pending"] == []


def test_the_job_list_shown_last_is_the_newest(tmp_path, monkeypatch):
    """작업 — 한 알림이 작업 목록을 뜬 뒤 내기 전에 그 작업이 끝나도, 마지막에 나간 목록이 지금의 것이다. 종전엔
    끝난 작업이 🧵 칩과 status.json 에 남았다."""
    r = WebRenderer(session_dir=str(tmp_path))
    jobs = JobRegistry(tmp_path)
    jobs.deliver = lambda *a, **k: ""
    jobs.on_change = r.jobs_changed
    try:
        job = jobs.start("sleep 60", owner="main")
        assert r.hub.sticky_payload("jobs")["count"] == 1
        ov, real = OneStep(), jobs.view
        jobs._view_step = ov.step
        monkeypatch.setattr(jobs, "view", lambda: (real(), ov.hold_first())[0])
        ov.run(jobs._changed, lambda: jobs._finish(job, 0, notify=False))
        assert real()["count"] == 0
        assert r.hub.sticky_payload("jobs")["count"] == 0
        assert "jobs" not in read_status_file(tmp_path)
    finally:
        jobs.stop_all()


def _knob_server(tmp_path):
    r = WebRenderer(session_dir=str(tmp_path))
    caps = ModelCapabilities(32768, 4096, True, supports_grammar=True)
    server = WebServer(
        r,
        token="t",
        ctx=ContextManager(tmp_path / "s", max_context_tokens=10_000),
        binding=make_binding(capabilities=caps, model="m1"),
    )
    server.auditor = SimpleNamespace(enabled=False, in_flight=0)
    return server, r


def test_an_older_knob_state_overwrites_a_newer_one(tmp_path):
    """[한 걸음에서 바뀜] 노브 — 감사 진행 수가 바뀌어 🔎 상태 한 장을 다시 내는 것(하니스 스레드)과 사람이 🔎 를
    켜는 것(`/api/knobs` 처리기의 두 걸음)이 엇갈리면, 켜기 전에 뜬 상태가 나중에 나간다."""
    server, r = _knob_server(tmp_path)
    auditor = KNOB_BY_NAME["auditor"]
    ov = Overlap()

    def read_one(name):
        state = web_state(KNOB_BY_NAME[name], server.knob_session())
        ov.hold_first()
        return state

    r.knob_state = read_one
    r.broadcast_knobs(web_states(server.knob_session()))

    def person_turns_it_on():
        session = server.knob_session()
        auditor.set(session, auditor.parse(True))
        r.broadcast_knobs(web_states(session))

    ov.run(lambda: r.note_harness(0), person_turns_it_on)
    assert web_state(auditor, server.knob_session())["value"] is True
    assert r.hub.sticky_payload("knob:auditor")["value"] is False
    assert read_status_file(tmp_path)["knobs"]["auditor"] is False
