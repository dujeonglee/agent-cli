"""세션 펌프 — run(텍스트 UI) 과 web 이 **같은 루프**를 돈다 (docs/pump/DESIGN.md).

main 상자에서 런을 열 것을 꺼내(사람의 말 먼저, 없으면 깨우는 메일) → 라우팅 → 런 → 런 종료 처리 → 정지 판정.
호스트가 꽂는 것은 둘뿐이다:

- :class:`LifetimePolicy` — "왜 살아 있는가". run 은 큐·에이전트·모니터·예약이
  전부 조용하면 끝나고(:class:`QuietPolicy`), web 은 SHUTDOWN 까지 산다
  (:class:`ForeverPolicy`).
- :class:`PumpSurface` — 표면이 다른 것 전부(유휴/바쁨 신호, 에코, 중단 핸들,
  명령 라우팅, 런 시작 알림, 런 결과·실패 처리).

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

from agent_cli.actor import RunEnd, run_actor
from agent_cli.main_input import MainInput


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
    """run: main 앞 사람의 말·에이전트·모니터·예약이 전부 조용하면 끝난다.

    에이전트 항은 ``has_active_work()``(미배달 회신 **포함**) — run 은 회신을
    배달하고 끝나야 한다(web 의 idle self-reap 은 ``any_activity`` 로 다르다,
    ``runtime.session_has_live_work`` 참조). 모니터나 예약만 남아 기다릴 땐
    ``on_wait("monitors"|"harness"|"schedules")`` 로 **한 번** 알린다 — 조용히 기다리면
    멈춘 것처럼 보인다(예약은 기한이 없어 Ctrl-C 까지, 모니터는 deadline 까지
    — v10.34.0 전엔 모니터 대기는 알리지 않는 비대칭이 있었다).
    """

    def __init__(
        self,
        *,
        main_input: MainInput,
        agent_registry,
        monitors=None,
        schedules=None,
        harness=None,
        jobs=None,
        on_wait: Callable[[str], None] | None = None,
        poll_secs: float = 0.5,
    ):
        self.main_input = main_input
        self.agent_registry = agent_registry
        self.monitors = monitors
        self.schedules = schedules
        self.harness = harness  # v10.42.0: 진행 중 감사는 결과 배달까지 기다린다
        self.jobs = jobs  # v10.47.0: 돌고 있는 백그라운드 작업은 종료 통지까지
        self.on_wait = on_wait
        self.poll_secs: float | None = poll_secs
        self._announced: str | None = None  # 지금 알린 대기의 종류

    def should_stop(self) -> bool:
        from agent_cli.runtime import session_has_live_work

        # 깨우는 메일은 `has_active_work` 가 센다(main 상자의 메일)
        if self.agent_registry.has_active_work() or self.main_input.pending_count():
            self._announced = None
            return False
        if not session_has_live_work(
            pending_count=0,
            monitors=self.monitors,
            schedules=self.schedules,
            harness=self.harness,
            jobs=self.jobs,
        ):
            self._announced = None
            return True
        # 모니터/감사/예약만 남았다 — 종류가 바뀔 때마다 한 번씩 알린다.
        if self.jobs is not None and self.jobs.has_active_work():
            kind = "jobs"
        elif self.monitors is not None and self.monitors.has_active_work():
            kind = "monitors"
        elif self.harness is not None and self.harness.in_flight:
            kind = "harness"
        else:
            kind = "schedules"
        if self._announced != kind:
            self._announced = kind
            if self.on_wait is not None:
                self.on_wait(kind)
        return False


class PumpSurface(Protocol):
    """호스트 표면 — 펌프가 사람/프런트에 알리거나 호스트 정책을 묻는 자리."""

    #: `MainLoop.graceful_interrupt` — web True(Stop 버튼), run False(Ctrl-C)
    graceful_interrupt: bool

    def idle(self) -> None:
        """런을 열 것을 기다리기 직전 — web 은 전송 버튼을 연다."""

    def busy(self) -> None:
        """아이템을 받아 런을 열기 직전."""

    def echo(self, item: dict) -> None:
        """사람의 말로 연 차례를 화면에 — web 은 말풍선. 메일이 연 런은 부르지 않는다(관찰 줄이 말한다)."""

    def bind_stop(self, event: threading.Event | None) -> None:
        """이 런의 중단 핸들을 표면에 건다(None = 해제)."""

    def is_command(self, text: str) -> bool:
        """``text`` 가 이 표면의 명령인가 — 실행하지 않고 판정만(`route` 가 받는 글 그대로)."""

    def route(self, text: str, start: Any = None, *, item: dict | None = None) -> bool:
        """명령이면 처리하고 True — 런을 열지 않는다. ``start`` 는 펌프가 이 차례를 열 때의 시작(`Start`)이고, 턴
        경계·제자리 대기의 라우팅은 None 이다(run 은 argv 질의였던 명령만 결과 파일·종료 코드에 쓴다). ``item`` 은 그
        글을 실어 온 사람의 말 항목(``{text, nickname, images, …}``, 없으면 None — 터미널 줄) — 그 말에 딸린 것(이미지)은
        그 말이 가는 곳으로 간다 (v10.149.3)."""

    def run_starting(self, start: Any) -> None:
        """런을 열기 직전 — 런 바로 앞, 같은 try 안이라 예외는 ``run_failed`` 로 간다. web 은 못 보는 모델에 이미지가
        온 것을 알리고, run 은 할 일이 없다(부트에서 막았다, ``_require_vision``)."""

    def run_ended(self, result: Any, start: Any) -> None:
        """런이 끝났다(ToolResult) — run 은 ``--result-file``. ``start`` 로 그 런이 무엇이었는지(argv 질의 = 작성자
        없는 사람의 요청 / 메일 런 = 사람의 요청이 아님 / 예약 = 그 이름) 안다."""

    def run_failed(self, exc: Exception) -> bool:
        """런이 예외로 죽었다. True = 삼키고 계속(web), False = 전파(run)."""


@dataclass(frozen=True, kw_only=True)
class PumpDeps:
    """세션 수명 객체 — 한 번 조립해 펌프에 준다."""

    main_input: MainInput
    agent_registry: Any
    #: main 루프의 종류(`runtime.MainLoop`) — ``start(item, stop_event)`` 로 시작을 짓고 ``run(start)`` 으로 돈다
    main: Any
    surface: PumpSurface
    policy: LifetimePolicy
    #: 세션 감사자 (v10.42.0, `runtime.SessionAuditor`) — main 의 사용자 런이
    #: 끝날 때마다 그 런의 history 구간을 감사 요청으로 보낸다. None = 꺼짐.
    auditor: Any = None


class MainActor:
    """main 의 액터 (`actor.run_actor`) — main 상자에서 런을 열 것(사람의 말 먼저, 없으면 깨우는 메일)을 꺼내, 명령이면
    그 자리에서 라우팅하고 아니면 런을 돌린다. 수명은 세션이다 — 정책(`LifetimePolicy`)이 끝을 정하거나 상자가 닫힌다.

    런의 예외는 표면이 정한다(`run_failed` — web 은 알리고 계속, run 은 전파). 그 길에서는 런 끝 정리(빚 정리·감사·
    표면의 `run_ended`)를 부르지 않는다(`tests/test_actor_lifetime.py` 의 웹 main 런 예외 — 끝내기 규칙을 모으는 단계의
    자리)."""

    def __init__(self, deps: PumpDeps):
        self.deps = deps

    def route(self, text: str, item: dict | None = None) -> bool:
        """런 **도중** 주입된 아이템의 라우팅(``LoopPorts.route_message``) — 표면의 명령 라우팅 그대로."""
        return self.deps.surface.route(text, item=item)

    def run(self) -> None:
        """``KeyboardInterrupt`` 는 잡지 않는다 — 호출자 정책(run 은 중단 안내,
        web 은 uvicorn 이 받는다)."""
        run_actor(self)

    # ── `actor.Actor` ───────────────────────────────

    @property
    def poll_secs(self) -> float | None:
        return self.deps.policy.poll_secs

    def should_stop(self) -> bool:
        return self.deps.policy.should_stop()

    def idle(self) -> None:
        self.deps.surface.idle()

    def take(self, timeout: float | None):
        """사람의 말과 메일이 같은 상자·같은 조건변수 — 판정과 대기가 한 락이라 그 사이 온 것을 놓치지 않는다. 사람의 말
        항목, 빈 묶음(메일이 연 런 — 메일은 꺼내지 않았다, 그 첫 턴 경계가 흡수한다), `CLOSED`, 또는 None(시한 —
        한가함은 그대로: 끄면 그 사이 온 메일이 "바쁨" 으로 보인다)."""
        return self.deps.main_input.wait_to_open(timeout=timeout)

    def open(self, got):
        """그 차례의 시작 — 사람의 말이면 말풍선을 내고, 명령이면 그 자리에서 라우팅하고 None(런을 열지 않는다)."""
        d = self.deps
        item = got if got else None
        d.surface.busy()
        if item is not None:
            d.surface.echo(item)
        # 귀속 승계의 런-시작 스냅샷: run_loop 를 안 타는 라우팅 명령이
        # 만든 요청도 이 런의 요청자를 물려받는다 — run_loop 는 주입마다
        # 재갱신한다. 메일 런엔 귀속할 사용자가 없다.
        author = (item or {}).get("nickname") or None
        d.agent_registry.set_current_run_authors([author] if author else [])
        stop_event = threading.Event()
        # "Stop everything" 의 조용함은 그 정지 하나의 것 — 새 런은 깨우는 정지 회신으로 시작한다
        if d.agent_registry is not None:
            d.agent_registry.main_stop_wakes = True
        d.surface.bind_stop(stop_event)
        start = d.main.start(item, stop_event)
        if item is None:
            return start
        # 명령의 라우팅도 런과 같은 보호 안 — 그 예외도 `run_failed` 로 간다(web 은 알리고 계속)
        try:
            routed = d.surface.route(start.query, start, item=item)
        except Exception as exc:
            try:
                if not d.surface.run_failed(exc):
                    raise
            finally:
                d.surface.bind_stop(None)
            return None
        if routed:
            d.surface.bind_stop(None)
            return None
        return start

    def run_start(self, start) -> RunEnd:
        """런 하나 — 그 끝의 사실(`RunEnd`). 런의 예외는 표면이 정한다(`run_failed`: web 은 삼켜 ``crashed`` 로,
        run 은 전파). 예외 길은 여기서 중단 핸들을 푼다 — 정상 길은 끝 정리(`ended`) 뒤에 푼다."""
        d = self.deps
        ctx = getattr(d.main, "ctx", None)
        self._run_from = ctx.next_ordinal if ctx is not None else 0
        try:
            d.surface.run_starting(start)
            result = d.main.run(start)
        except Exception as exc:
            try:
                if not d.surface.run_failed(exc):
                    raise
            finally:
                d.surface.bind_stop(None)
            return RunEnd.crashed(exc)
        stop = getattr(start, "stop_event", None)
        return RunEnd.of(result, stopped=bool(stop is not None and stop.is_set()))

    def _answer_stopped(self) -> None:
        """멈춘 런의 빚에 정지 회신 — 그 런이 기록한 레코드에서 마지막 호출·생각을 뜬다(레지스트리가)."""
        ctx = getattr(self.deps.main, "ctx", None)
        records = (
            ctx.records_since(getattr(self, "_run_from", 0)) if ctx is not None else []
        )
        self.deps.agent_registry.answer_main_debts_as_stopped(records)

    def ended(self, start, end: RunEnd) -> bool:
        """런 끝 정리 — 빚 정리·감사·표면의 런 끝, 같은 보호 안(예외는 `run_failed`). 끝나면 중단 핸들을 푼다.
        main 의 수명은 세션이다 — 런 하나로 끝나지 않는다.

        예외로 끝난 런(``crashed``)은 정리하지 않는다 — 지금 동작(`tests/test_actor_lifetime.py`), 끝내기 규칙을
        모으는 단계가 바꾼다. 사람이 멈춘 런(``stopped`` — ■·Stop everything)은 에이전트의 ■ 처럼 빚진 상대 모두에게
        정지 회신을 보낸다(그 런의 마지막 호출·생각을 실어)."""
        if end.outcome == "crashed":
            return False
        d = self.deps
        try:
            try:
                from agent_cli.runtime import main_run_ended

                stopped = end.outcome == "stopped"
                if stopped and d.agent_registry is not None:
                    self._answer_stopped()
                # `wait` 로 끝난 런의 판정은 레지스트리 한 곳(`run_ended`, v10.77.0)
                # 실패한 런(턴 상한·LLM 오류)은 그 사유를 — 폴백 머리에 원인 한 줄
                failed = (
                    {
                        "success": False,
                        "error": str(getattr(end.result, "error", "") or ""),
                    }
                    if end.outcome == "failed"
                    else {}
                )
                main_run_ended(
                    d.agent_registry,
                    end.output,
                    waiting=end.waiting,
                    stopped=stopped,
                    **failed,
                )
                if d.auditor is not None:
                    d.auditor.run_ended(
                        start, ok=bool(getattr(end.result, "success", False))
                    )
                d.surface.run_ended(end.result, start)
            except Exception as exc:
                if not d.surface.run_failed(exc):
                    raise
        finally:
            d.surface.bind_stop(None)
        return False
