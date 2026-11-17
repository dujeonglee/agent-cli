"""펌프의 한가함은 큐가 빈손으로 돌아와도 꺼지지 않는다 (v10.145.0).

종전엔 `dequeue_blocking` 이 시간 초과로 None 을 돌려도 `waker.idle.clear()` 를 해, 다음 바퀴의 `mark_idle` 까지
한가함이 꺼졌다 — 그 깜박임 창에 온 메일은 main 이 바쁜 것으로 보였다(CLI 대기 줄 판정 ``busy``, 오라클 s17 이
컨테이너 cpus 0.5 에서 흔들린 원인). 대본 큐로 한 스레드에서 돌린다 — 시간을 재지 않는다.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_cli.input_queue import InputQueue
from tests.pump_support import FakeWaker, make_pump


class ScriptedQueue:
    """``dequeue_blocking`` 이 대본대로 돌려준다(None = 시간 초과)."""

    def __init__(self, items):
        self.items = list(items)

    def dequeue_blocking(self, timeout=None):
        return self.items.pop(0)


def test_idle_stays_on_through_an_empty_poll_and_goes_off_for_an_item():
    waker = FakeWaker()
    seen: list[tuple[str, bool]] = []

    class Policy:
        poll_secs = 0.05

        def should_stop(self):
            seen.append(("loop top", waker.idle.is_set()))
            return False

    def run_main(req):
        seen.append(("run", waker.idle.is_set()))
        return SimpleNamespace(output="ok", success=True, waiting=False)

    queue = ScriptedQueue([None, {"text": "hello", "id": "u-1"}, InputQueue.SHUTDOWN])
    pump = make_pump(queue=queue, run_main=run_main, waker=waker, policy=Policy())
    pump.run()
    assert seen == [
        ("loop top", False),  # 첫 바퀴 — 아직 한가함을 세우기 전
        ("loop top", True),  # 빈손으로 돌아온 뒤 — 꺼지지 않았다
        ("run", False),  # 항목이 오면 꺼진다
        ("loop top", False),  # 런 뒤 다음 바퀴 — 다시 세우기 전
    ]


class SkippingWaker(FakeWaker):
    """깨우기 문구를 꺼내면 "skip"(이미 다른 런이 배달했다)으로 판정하는 대역."""

    def handle_dequeued(self, text):
        return "skip" if text == self.WAKE_TEXT else None


def test_a_wake_that_is_skipped_leaves_idle_on():
    """건너뛸 깨우기를 꺼낸 바퀴는 런을 열지 않는다 — 한가함도 꺼지지 않는다(꺼지면 그 바퀴 동안 온 메일이 "바쁨"
    으로 보인다). 판정 뒤에 끈다."""
    waker = SkippingWaker()
    seen: list[tuple[str, bool]] = []

    class Policy:
        poll_secs = 0.05

        def should_stop(self):
            seen.append(("loop top", waker.idle.is_set()))
            return False

    def run_main(req):  # pragma: no cover — 열리면 안 된다
        seen.append(("run", waker.idle.is_set()))
        return SimpleNamespace(output="ok", success=True, waiting=False)

    queue = ScriptedQueue([{"text": waker.WAKE_TEXT}, InputQueue.SHUTDOWN])
    make_pump(queue=queue, run_main=run_main, waker=waker, policy=Policy()).run()
    assert seen == [("loop top", False), ("loop top", True)]
