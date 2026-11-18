"""부활(`mode:"resume"`) 재생과 그 사이에 든 배달 — 창에는 그 줄이 한 번만 (N12).

`resume_teammate` 는 워커를 먼저 세우고 `_replay_conversation` 으로 창을 지운 뒤 ``conversation.jsonl`` 을 다시 낸다. 지우기와
파일 읽기 사이에 다른 스레드의 배달이 끝나면(파일에 적고 창에 냄) 재생이 그 줄을 파일에서 한 번 더 냈다(같은 줄 두 번).
재생이 배달과 같은 락(``_cv``) 구간이면 그 배달은 재생 뒤로 밀린다.

순서는 지우기(`clear_agent_conversation`) 훅에서 강제한다: 다른 스레드가 배달을 시작하고, 그 배달이 끝나거나 **락을 기다리기
시작할 때까지**(`WatchedRLock` 가 다툼을 적는다) 재생을 세운다. 시간을 재지 않는다.
"""

from __future__ import annotations

import threading

import pytest

import agent_cli.render as render_mod
from agent_cli.post.office import PostOffice
from agent_cli.render.web import WebRenderer
from agent_cli.subagent.agents_live import AgentRegistry
from tests.test_agents_live import _FakeLoopResult
from tests.test_engine_p2 import wait_until


class WatchedRLock:
    """재진입 락 — 이미 남이 쥐어 기다리게 된 스레드를 ``waiting`` 에 적는다(조건변수의 내부 훅은 그대로 넘긴다)."""

    def __init__(self):
        self._lock = threading.RLock()
        self.waiting: set[int] = set()

    def acquire(self, blocking=True, timeout=-1):
        if self._lock.acquire(False):
            return True
        if not blocking:
            return False
        self.waiting.add(threading.get_ident())
        return self._lock.acquire(True, timeout)

    __enter__ = acquire

    def release(self):
        self._lock.release()

    def __exit__(self, *exc):
        self.release()

    def _is_owned(self):
        return self._lock._is_owned()

    def _release_save(self):
        return self._lock._release_save()

    def _acquire_restore(self, state):
        self._lock._acquire_restore(state)


@pytest.fixture
def web(tmp_path, monkeypatch):
    renderer = WebRenderer(workspace=str(tmp_path))
    monkeypatch.setattr(render_mod, "get_renderer", lambda: renderer)
    lock = WatchedRLock()

    def runner(kind, start, ctx):
        return _FakeLoopResult(output="ok"), 0.01

    reg = AgentRegistry(
        tmp_path,
        runtime={"model": "m", "timeout": 5},
        runner=runner,
        post=PostOffice(cv=threading.Condition(lock)),
    )
    try:
        yield reg, renderer, lock
    finally:
        reg.shutdown_all()


def _shown(renderer, key, text) -> int:
    with renderer.hub._lock:
        buf = list(renderer.hub._event_buffer)
    return sum(
        1
        for event, data in buf
        if event == "agent_msg" and data.get("key") == key and data.get("text") == text
    )


def test_a_delivery_during_the_revive_replay_shows_once(web):
    reg, renderer, lock = web
    key, _ = reg.spawn(name="helper")
    assert wait_until(lambda: reg.get(key).state == "idle")
    assert reg.request(key, "first job", author="main", expects_reply=False) == ""
    assert wait_until(lambda: reg.get(key).state == "idle" and reg.get(key).handled)
    assert reg.kill(key) == ""
    assert _shown(renderer, key, "first job") == 0  # kill = 창 정리

    clear = renderer.clear_agent_conversation
    late: list[threading.Thread] = []

    def clear_then_a_late_delivery(k):
        clear(k)
        if late:
            return
        t = threading.Thread(
            target=reg.request,
            args=(k, "late news"),
            kwargs={"author": "main", "expects_reply": False},
        )
        late.append(t)
        t.start()
        # 그 배달이 끝나거나(재생이 락 밖) 락을 기다리기 시작할 때까지(재생이 락 안) 재생을 세운다
        assert wait_until(lambda: not t.is_alive() or t.ident in lock.waiting)

    renderer.clear_agent_conversation = clear_then_a_late_delivery
    assert reg.resume_teammate(key) == ""
    late[0].join(10)
    assert not late[0].is_alive()
    assert _shown(renderer, key, "first job") == 1  # 재생이 되살린 줄
    assert _shown(renderer, key, "late news") == 1
