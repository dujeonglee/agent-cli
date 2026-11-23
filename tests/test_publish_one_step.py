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


def test_an_older_roster_overwrites_a_newer_one(tmp_path, renderer, monkeypatch):  # noqa: F811
    """[한 걸음에서 바뀜] 명단 — 먼저 뜬 명단이 나중에 나가, 기다림 표시가 선 것을 덮는다."""
    reg = make_registry(tmp_path)
    key, _ = reg.spawn(name="aa")
    assert wait_until(lambda: reg.get(key).state == "idle")
    ov, real = Overlap(), reg.roster_snapshot
    monkeypatch.setattr(reg, "roster_snapshot", lambda: (real(), ov.hold_first())[0])
    try:
        ov.run(reg._notify_roster, lambda: reg.mark_waiting(f"agent:{key}"))
        shown = renderer.named("agent_roster")[-1][1]["roster"][0]
        assert real()[0]["waiting"] is True
        assert shown["waiting"] is False
    finally:
        reg.shutdown_all()
