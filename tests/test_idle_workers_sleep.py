"""한가한 워커들은 잠든다 — 서로를 깨우지 않는다 (v10.145.0 고침).

1b'(워커 대기를 우편의 조건변수로) 부터 빈손의 `start_run` 이 `notify_all` 을 불러, 같은 레지스트리의 한가한
워커 둘 이상이 서로를 영원히 깨웠다(CPU 한 코어, CI 수트 270→1282초). 시간 창을 재지 않는다: 조건변수를 감싼
스파이로 (1) 워커가 모두 `wait` 안에 들어갔는지, (2) 워커 스레드가 부른 `notify_all` 이 몇 번인지를 본다 —
배달이 없으면 워커가 깨울 이유가 없으니 0 이어야 한다(옛 판은 첫 빈손 `start_run` 부터 1 이상).
"""

from __future__ import annotations

import threading

from agent_cli.post.office import PostOffice
from agent_cli.subagent.agents_live import AgentRegistry
from tests.test_agents_live import _FakeLoopResult
from tests.test_engine_p2 import wait_until


class SpyCondition(threading.Condition):
    """`wait` 안에 있는 스레드와 스레드별 `notify_all` 수를 기록하는 조건변수."""

    def __init__(self):
        super().__init__()
        self.waiting: set[str] = set()
        self.notified_by: dict[str, int] = {}

    def wait(self, timeout=None):
        name = threading.current_thread().name
        self.waiting.add(name)
        try:
            return super().wait(timeout)
        finally:
            self.waiting.discard(name)

    def notify_all(self):
        name = threading.current_thread().name
        self.notified_by[name] = self.notified_by.get(name, 0) + 1
        super().notify_all()


def test_idle_workers_wait_without_waking_each_other(tmp_path):
    cv = SpyCondition()

    def runner(kind, start, ctx):
        return _FakeLoopResult(output="ok"), 0.01

    reg = AgentRegistry(
        tmp_path,
        runtime={"model": "m", "timeout": 5},
        runner=runner,
        post=PostOffice(cv=cv),
    )
    try:
        keys = [reg.spawn(name=n)[0] for n in ("a", "b", "c")]
        workers = {reg.get(k).worker.name for k in keys}
        # 셋 다 같은 조건변수의 `wait` 안에 있다 — 배달 없이 한가하다
        assert wait_until(lambda: workers <= set(cv.waiting))
        assert all(reg.get(k).state == "idle" for k in keys)
        with cv:  # 락을 쥔 채 본다 — 그 사이 누구도 깨거나 깨우지 못한다
            assert workers <= cv.waiting
            assert {w: cv.notified_by.get(w, 0) for w in workers} == dict.fromkeys(
                workers, 0
            ), "빈손의 워커가 다른 워커를 깨웠다"
    finally:
        reg.shutdown_all()
