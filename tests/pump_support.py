"""세션 펌프 테스트 대역 — run 수명(QuietPolicy)으로 펌프를 조립하는 헬퍼.

monitor·schedule·agents_live 의 펌프 테스트가 같은 대역을 쓴다(종전엔
``main._run_message_pump`` 를 직접 불렀다).
"""

from __future__ import annotations

import threading


class FakeSurface:
    """호출을 기록만 하는 표면. ``routed`` 에 든 텍스트는 명령으로 처리한 척한다."""

    graceful_interrupt = False

    def __init__(self, *, routed=(), swallow_errors=False):
        self.calls: list[tuple] = []
        self.routed = set(routed)
        self.swallow_errors = swallow_errors
        self.stop_events: list = []

    def idle(self):
        self.calls.append(("idle",))

    def busy(self):
        self.calls.append(("busy",))

    def echo(self, item, *, wake):
        self.calls.append(("echo", item.get("text"), wake))

    def bind_stop(self, event):
        self.calls.append(("bind_stop", event is not None))
        self.stop_events.append(event)

    def route(self, text):
        self.calls.append(("route", text))
        return text in self.routed

    def run_ended(self, result, req):
        self.calls.append(("run_ended", result))

    def run_failed(self, exc):
        self.calls.append(("run_failed", exc))
        return self.swallow_errors


class FakeWaker:
    """깨우기 없음 — 모든 아이템이 사람 메시지."""

    WAKE_TEXT = "WAKE"

    def __init__(self):
        self.idle = threading.Event()

    def mark_idle(self):
        self.idle.set()

    def handle_dequeued(self, text):
        return None

    def on_run_end(self):
        pass


class FakeRegistry:
    def __init__(self):
        self.authors: list[list[str]] = []

    def has_active_work(self):
        return False

    def set_current_run_authors(self, authors):
        self.authors.append(list(authors))

    def end_run(self, owner, output=""):
        return 0


def make_pump(
    *,
    queue,
    run_main,
    waker=None,
    registry=None,
    surface=None,
    monitors=None,
    schedules=None,
    on_wait=None,
    poll_secs=0.05,
):
    """run 수명 정책의 펌프. ``run_main`` 은 ``RunRequest`` 를 받는다."""
    from agent_cli.pump import PumpDeps, QuietPolicy, SessionPump

    registry = registry if registry is not None else FakeRegistry()
    return SessionPump(
        PumpDeps(
            queue=queue,
            waker=waker if waker is not None else FakeWaker(),
            agent_registry=registry,
            run_main=run_main,
            surface=surface if surface is not None else FakeSurface(),
            policy=QuietPolicy(
                queue=queue,
                agent_registry=registry,
                monitors=monitors,
                schedules=schedules,
                on_wait=on_wait,
                poll_secs=poll_secs,
            ),
        )
    )


def run_pump_in_thread(pump) -> threading.Event:
    """끝나지 않을 수 있는 펌프는 스레드로 — 반환된 Event 가 종료 신호."""
    done = threading.Event()

    def _go():
        try:
            pump.run()
        finally:
            done.set()

    threading.Thread(target=_go, daemon=True).start()
    return done
