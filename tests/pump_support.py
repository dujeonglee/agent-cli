"""세션 펌프 테스트 대역 — run 수명(QuietPolicy)으로 펌프를 조립하는 헬퍼.

monitor·schedule·agents_live 의 펌프 테스트가 같은 대역을 쓴다(종전엔
``main._run_message_pump`` 를 직접 불렀다).
"""

from __future__ import annotations

import threading


class FakeSurface:
    """호출을 기록만 하는 표면. ``routed`` 에 든 텍스트는 명령으로 처리한 척한다."""

    graceful_interrupt = False

    def __init__(
        self, *, routed=(), swallow_errors=False, fail_on_start=False, fail_on_route=()
    ):
        self.calls: list[tuple] = []
        self.fail_on_start = fail_on_start
        self.fail_on_route = set(fail_on_route)
        self.routed = set(routed)
        self.swallow_errors = swallow_errors
        self.stop_events: list = []

    def idle(self):
        self.calls.append(("idle",))

    def busy(self):
        self.calls.append(("busy",))

    def echo(self, item):
        self.calls.append(("echo", item.get("text")))

    def bind_stop(self, event):
        self.calls.append(("bind_stop", event is not None))
        self.stop_events.append(event)

    def is_command(self, text):
        return text in self.routed

    def route(self, text, start=None, *, item=None):
        self.calls.append(("route", text))
        if text in self.fail_on_route:
            raise RuntimeError(f"route failed: {text}")
        return text in self.routed

    def run_starting(self, start):
        self.calls.append(("run_starting", start.query))
        if self.fail_on_start:
            raise RuntimeError("run_starting failed")

    def run_ended(self, result, start):
        self.calls.append(("run_ended", result))

    def run_failed(self, exc):
        self.calls.append(("run_failed", exc))
        return self.swallow_errors


class FakeRegistry:
    def __init__(self):
        self.authors: list[list[str]] = []
        self.ended: list[tuple] = []  # run_ended 호출 (owner, output, waiting)

    def has_agent_work(self):
        return False

    def set_current_run_authors(self, authors):
        self.authors.append(list(authors))

    def run_ended(self, owner, output="", *, waiting=False, **kw):
        self.ended.append((owner, output, waiting))
        return 0


def make_pump(
    *,
    main_input,
    run_main,
    registry=None,
    surface=None,
    monitors=None,
    schedules=None,
    on_wait=None,
    poll_secs=0.05,
    auditor=None,
    policy=None,
):
    """run 수명 정책의 펌프. ``run_main`` 은 시작(`Start` — `MainLoop.start` 가 꺼낸 것에서 짓는다)을 받는다.
    ``policy`` 를 주면 그 수명 정책으로(기본은 run 의 `QuietPolicy`)."""
    from types import SimpleNamespace

    from agent_cli.pump import MainActor, PumpDeps, QuietPolicy
    from agent_cli.runtime import MainLoop

    registry = registry if registry is not None else FakeRegistry()
    return MainActor(
        PumpDeps(
            main_input=main_input,
            agent_registry=registry,
            main=SimpleNamespace(start=MainLoop.start, run=run_main),
            surface=surface if surface is not None else FakeSurface(),
            auditor=auditor,
            policy=policy
            if policy is not None
            else QuietPolicy(
                main_input=main_input,
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
