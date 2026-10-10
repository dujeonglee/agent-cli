"""세션 펌프 — run(텍스트 UI) 과 web 이 **같은 루프**를 돈다 (docs/pump/DESIGN.md).

큐에서 하나 꺼내 → 깨우기 판정 → 라우팅 → 런 → 런 종료 처리 → 정지 판정.
호스트가 꽂는 것은 둘뿐이다:

- :class:`LifetimePolicy` — "왜 살아 있는가". run 은 큐·에이전트·모니터·예약이
  전부 조용하면 끝나고(:class:`QuietPolicy`), web 은 SHUTDOWN 까지 산다
  (:class:`ForeverPolicy`).
- :class:`PumpSurface` — 표면이 다른 것 전부(유휴/바쁨 신호, 에코, 중단 핸들,
  명령 라우팅, 런 결과·실패 처리).

종전엔 ``main._run_message_pump``(run, ~65줄) 와 ``web._worker_loop``(~200줄)
두 벌이었고, 수명 정책이 run 에만 있어 ``--result-file`` 이 펌프 뒤에 써지는
구멍(v10.31.2)과 모니터·예약이 붙을 때마다 두 곳을 고치는 비대칭(v10.32.1)이
여기서 나왔다. 스레드는 호스트가 정한다 — run 은 메인 스레드, web 은 워커.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from agent_cli.input_queue import InputQueue


@dataclass(frozen=True, kw_only=True)
class RunRequest:
    """런 하나의 입력 — ``run_loop`` 의 ``query*``·``stop_event`` 인자 묶음."""

    text: str
    author: str | None  # None = 단일 사용자(run 의 argv 질의) — run_loop 기본값
    author_is_user: bool  # 합성 깨우기(wake)는 사람 발화가 아니다
    request_id: str  # 큐가 발급한 id ("" = 없음) — 런이 "무엇에 답하는지"
    stop_event: threading.Event  # 이 런만의 중단 핸들 (web Stop 버튼)
    wake: bool


class LifetimePolicy(Protocol):
    """펌프가 언제 끝나는가."""

    #: 큐 대기 타임아웃. None 이면 아이템이 올 때까지 막고 ``should_stop`` 은
    #: 불리지 않는다(끝은 SHUTDOWN 뿐).
    poll_secs: float | None

    def should_stop(self) -> bool: ...


class ForeverPolicy:
    """web: 다음 메시지를 영원히 기다린다 — 끝은 서버 종료(SHUTDOWN)."""

    poll_secs: float | None = None

    def should_stop(self) -> bool:
        return False


class QuietPolicy:
    """run: 큐·에이전트·모니터·예약이 전부 조용하면 끝난다.

    에이전트 항은 ``has_active_work()``(미배달 회신 **포함**) — run 은 회신을
    배달하고 끝나야 한다(web 의 idle self-reap 은 ``any_activity`` 로 다르다,
    ``runtime.session_has_live_work`` 참조). 예약만 남아 기다릴 땐
    ``on_schedule_wait`` 로 **한 번** 알린다 — 예약은 모니터와 달리 기한이
    없어 Ctrl-C 까지 기다리는데, 조용히 기다리면 멈춘 것처럼 보인다.
    """

    def __init__(
        self,
        *,
        queue: InputQueue,
        agent_registry,
        monitors=None,
        schedules=None,
        on_schedule_wait: Callable[[], None] | None = None,
        poll_secs: float = 0.5,
    ):
        self.queue = queue
        self.agent_registry = agent_registry
        self.monitors = monitors
        self.schedules = schedules
        self.on_schedule_wait = on_schedule_wait
        self.poll_secs: float | None = poll_secs
        self._announced = False

    def should_stop(self) -> bool:
        from agent_cli.runtime import session_has_live_work

        if self.agent_registry.has_active_work() or session_has_live_work(
            pending_count=self.queue.pending_count(), monitors=self.monitors
        ):
            return False
        if self.schedules is None or not self.schedules.has_active_work():
            self._announced = False
            return True
        if not self._announced and self.on_schedule_wait is not None:
            self.on_schedule_wait()
        self._announced = True
        return False


class PumpSurface(Protocol):
    """호스트 표면 — 펌프가 사람/프런트에 알리거나 호스트 정책을 묻는 자리."""

    #: ``run_loop(graceful_interrupt=)`` — web True(Stop 버튼), run False(Ctrl-C)
    graceful_interrupt: bool

    def idle(self) -> None:
        """큐 대기 직전 — web 은 전송 버튼을 연다."""

    def busy(self) -> None:
        """아이템을 받아 런을 열기 직전."""

    def echo(self, item: dict, *, wake: bool) -> None:
        """꺼낸 아이템을 화면에 — web 은 말풍선/깨우기 카드, run 은 한 줄."""

    def bind_stop(self, event: threading.Event | None) -> None:
        """이 런의 중단 핸들을 표면에 건다(None = 해제)."""

    def route(self, text: str) -> bool:
        """``/``·``@`` 명령이면 처리하고 True — 런을 열지 않는다."""

    def run_ended(self, result: Any) -> None:
        """런이 끝났다(ToolResult) — run 은 ``--result-file``."""

    def run_failed(self, exc: Exception) -> bool:
        """런이 예외로 죽었다. True = 삼키고 계속(web), False = 전파(run)."""


@dataclass(frozen=True, kw_only=True)
class PumpDeps:
    """세션 수명 객체 — 한 번 조립해 펌프에 준다."""

    queue: InputQueue
    waker: Any  # MailWaker: mark_idle / idle / handle_dequeued / on_run_end / WAKE_TEXT
    agent_registry: Any
    run_main: Callable[[RunRequest], Any]  # run_loop 바인딩 → ToolResult
    surface: PumpSurface
    policy: LifetimePolicy


class SessionPump:
    def __init__(self, deps: PumpDeps):
        self.deps = deps

    def route(self, text: str) -> bool:
        """런 **도중** 주입된 아이템의 라우팅(``LoopPorts.route_message``).

        합성 깨우기(``WAKE_TEXT``)는 여기서 소비한다 — 메일은 같은 턴 경계의
        메일박스가 배달하므로 텍스트를 컨텍스트에 넣을 이유가 없고, 무엇보다
        ``handle_dequeued`` 를 거치지 않으면 waker 의 무장이 영영 풀리지 않아
        이후 깨우기가 전부 드롭된다(``--resume`` 로 미배달 회신이 있으면 첫
        질의 앞에 무장돼 큐가 ``[질의, WAKE]`` 가 되는 것이 실제 순서).
        """
        waker = self.deps.waker
        if text == waker.WAKE_TEXT:
            waker.handle_dequeued(text)
            return True
        return self.deps.surface.route(text)

    def run(self) -> None:
        """``KeyboardInterrupt`` 는 잡지 않는다 — 호출자 정책(run 은 중단 안내,
        web 은 uvicorn 이 받는다)."""
        d = self.deps
        while True:
            if d.policy.poll_secs is not None and d.policy.should_stop():
                return
            d.surface.idle()
            # mark_idle (not bare idle.set): a reply that landed in the
            # on_run_end()→idle window re-arms here, else web parks forever.
            d.waker.mark_idle()
            item = d.queue.dequeue_blocking(timeout=d.policy.poll_secs)
            d.waker.idle.clear()
            if item is InputQueue.SHUTDOWN:
                return
            if item is None:
                continue  # timeout — 정지 조건을 다시 판정
            text = item.get("text") or ""
            verdict = d.waker.handle_dequeued(text)
            if verdict == "skip":
                continue  # 이미 다른 런이 배달한 깨우기 — 빈 런을 열지 않는다
            wake = verdict == "run"
            d.surface.busy()
            d.surface.echo(item, wake=wake)
            author = None if wake else (item.get("nickname") or None)
            # 귀속 승계의 런-시작 스냅샷: run_loop 를 안 타는 라우팅 명령이
            # 만든 요청도 이 런의 요청자를 물려받는다 — run_loop 는 주입마다
            # 재갱신한다.
            d.agent_registry.set_current_run_authors([author] if author else [])
            stop_event = threading.Event()
            d.surface.bind_stop(stop_event)
            try:
                if not wake and d.surface.route(text):
                    continue
                try:
                    result = d.run_main(
                        RunRequest(
                            text=text,
                            author=author,
                            author_is_user=not wake,
                            request_id=item.get("id") or "",
                            stop_event=stop_event,
                            wake=wake,
                        )
                    )
                    from agent_cli.runtime import main_run_ended

                    main_run_ended(
                        d.agent_registry, getattr(result, "output", "") or ""
                    )
                    d.surface.run_ended(result)
                except Exception as exc:
                    if not d.surface.run_failed(exc):
                        raise
            finally:
                d.surface.bind_stop(None)
                d.waker.on_run_end()  # 런 종료 직후 도착분 레이스 봉합
