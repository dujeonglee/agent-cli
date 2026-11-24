"""run/web 부트스트랩·teardown 조립기 (리뷰 §4.1 P1, v8.39.0).

세 조각을 소유한다:

- :class:`AgentRuntime` — 상주/서브 에이전트에 넘기는 provider 배선
  묶음의 **단일 정의**. 종전엔 run/web(main.py)/tool_bridge 세 곳이 같은
  dict 리터럴을 손으로 나열했고, 키 구성부터 어긋나 있었다(run/web 엔
  ``compaction_enabled`` 부재, web 은 ``hooks_config`` 를 None 으로 고정).
- :func:`build_agent_registry` / :func:`wire_agent_mail` — registry 생성+
  main 슬롯 등록, 그리고 회신 훅·restore/auto_spawn 조립의 공용화.
  두 단계로 쪼갠 이유: run 은 skill 조기-반환 **이전**에 registry 만 만들고
  restore 는 그 뒤에 하므로(순서 보존 = 등가성), 생성과 배선을 분리해야
  기존 순서를 바이트 그대로 유지한다.
- :func:`teardown_session` — 종료 시퀀스의 단일 소유자: 상주 에이전트
  전원 종료 → 스피너 정지 → MCP 해제 → 세션 저장. 종전엔 run 의 종료
  경로 4갈래 + web 1갈래가 각자 나열했고, 그 결과 2경로는 MCP 미해제
  (stdio 자식 프로세스 잔존), 1경로는 registry 미종료였다 — 모든 경로가
  이 함수 하나로 수렴하면 그 누락 클래스가 구조적으로 사라진다.
- 세션 조립 (v10.131.0) — :class:`SessionSpec` → :func:`build_session_stores` →
  :func:`build_session_owners` → :func:`wire_session` → :func:`close_session`.
  값을 돌려주는 평범한 함수 넷이라 run/web 은 위에서 아래로 읽히는 스크립트로
  남고(화면·서버 손잡이·스레드·수명만 호스트의 것), 종전 두 벌이던 조립의 순서
  차이는 한 곳의 순서가 됐다. 이 모듈은 `main` 을 import 하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from agent_cli.constants import RESTART_NOTICE, schedule_addr
from agent_cli.loop.kind import (
    LoopKind,
    PromptProfile,
    Start,
)
from agent_cli.loop.ports import (
    _RESIDENT_ROSTER,
    LoopPorts,
    _hook_runner_port,
    _hook_unwired,
)
from agent_cli.render import render_step
from agent_cli.session.records import record_restart
from agent_cli.session_slots import (
    clear_session_slots,
    set_agent_registry,
    set_hook_runner,
    set_job_registry,
    set_monitor_registry,
    set_schedule_registry,
)
from agent_cli.wake import Mailbox


@dataclass(frozen=True)
class AgentRuntime:
    """상주/서브 에이전트 실행에 필요한 provider 배선 묶음.

    소비자(에이전트 레지스트리·`agent` 도구·하니스 작업)는 dict 를 기대하므로
    경계에서는 :meth:`as_dict` 로 넘긴다 — 필드명 == 종전 dict 키
    (계약은 tests 의 키-셋 핀 테스트가 고정). ``compaction_enabled`` 는
    소비 측 기본값(True)과 같아, 키를 늘 싣는 것이 종전 run/web 의
    키-생략과 행동 동일하다.
    """

    #: 세션의 `ModelBinding` (v10.43.0) — 팀원·하니스 런이 복사본이 아니라 이것을
    #: 참조한다. 프로파일이 모델을 고정하면 `binding.pinned(model)`.
    binding: Any
    max_turns: int
    depth: int
    max_depth: int
    session: Any
    hooks_config: dict | None = None
    compaction_enabled: bool = True

    def as_dict(self) -> dict:
        # dataclasses.asdict 는 재귀 변환이라 capabilities(dataclass)까지
        # dict 로 풀어버린다 — 얕은 사상으로 객체 동일성을 보존한다.
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_loop_config(cls, cfg) -> AgentRuntime:
        """돌고 있는 루프의 종류(``cfg.kind``) → AgentRuntime (tool_bridge 의 상주 모드 인터셉트용)."""
        return cls(
            binding=cfg.kind.binding,
            max_turns=cfg.kind.max_turns,
            depth=cfg.kind.depth,
            max_depth=cfg.kind.max_depth,
            session=cfg.kind.session,
            hooks_config=cfg.kind.hooks_config,
            compaction_enabled=cfg.kind.compaction_enabled,
        )


def build_agent_registry(
    session_dir,
    runtime: AgentRuntime,
    max_agents=None,
    *,
    post,
    monitors=None,
    jobs=None,
):
    """AgentRegistry 생성 + main registry 슬롯 등록 (v7.17.0 배선 통일).

    runtime 프리필: restore/auto-spawn 된 에이전트가 도구 호출(스폰) 없이
    첫 접촉(웹 창 인간 개입 등)을 받아도 provider 배선이 있도록.

    ``max_agents`` None = 미지정 → registry 가 env/기본값을 고른다 (v8.61.0,
    CLI ``--max-agents``). ``post`` — 세션의 창구(`MainInput.post`): main 앞 사람의 말과 메일이 한 상자에 서야
    펌프가 둘을 한 자리에서 기다린다. 따로 만들면 메일이 다른 상자로 가 펌프가 깨지 않는다."""
    from agent_cli.subagent.agents_live import AgentRegistry

    registry = AgentRegistry(
        session_dir, runtime=runtime.as_dict(), max_agents=max_agents, post=post
    )
    set_agent_registry(registry)
    if monitors is not None:
        wire_monitor_delivery(registry, monitors)
    if jobs is not None:
        wire_job_delivery(registry, jobs)
    return registry


def wire_job_delivery(registry, jobs) -> None:
    """백그라운드 작업의 종료 통지를 주소 배달에 잇는다 — 모니터와 같은 양방향
    (v10.47.0): `jobs.deliver` 로 보고가 가고, 워커의 `finally` 가 `drop_owner`
    로 죽은 소유자의 작업을 죽인다."""
    jobs.deliver = registry.deliver
    registry.jobs = jobs


def build_job_registry(cwd=None):
    """백그라운드 작업 레지스트리 생성 + 프로세스 전역 등록 (run/web 공용,
    `build_monitor_registry` 의 형제). 배달 배선은 `wire_job_delivery`."""
    from agent_cli.jobs import JobRegistry

    registry = JobRegistry(cwd)
    set_job_registry(registry)
    return registry


def wire_monitor_delivery(registry, monitors) -> None:
    """모니터 보고를 주소 배달에 잇는다 — 양방향 (docs/wiring §3.3).

    **위치가 아니라 제약이 본질이다.** 이 배선은 `wire_agent_mail` 의
    `restore`/`auto_spawn` **보다 먼저** 끝나야 한다: 부활한 에이전트의
    inbox 로 질문이 재배달되고 그 워커들이 즉시 루프를 시작한다. 또 run 의
    스킬 조기-반환(`try_dispatch_agent_or_skill`)은 `wire_agent_mail` 보다
    앞서므로, 거기 배선하면 `agent-cli run "/skill …"` 에서 등록이 거부된다.
    `build_agent_registry` 직후가 두 제약을 만족하는 가장 이른 지점이다.

    역방향(`registry.monitors`)이 필요한 이유: 워커의 `finally` 가
    `drop_owner` 를 불러야 하는데, `AgentRegistry` 는 모니터 레지스트리를
    **전혀 몰랐다**. 도구가 쓰는 프로세스 전역을 집어올 수도 있지만
    주입으로 둔다 — 레지스트리 테스트가 가짜를 꽂을 수 있어야 한다.
    """
    monitors.deliver = registry.deliver
    registry.monitors = monitors


def build_hook_runner():
    """세션당 Python 훅 러너 하나 (v10.35.0) — run/web 부트스트랩이 부르고 네 포트
    종류의 `make_ports`(`MainLoop`·`ResidentLoop`·`SkillLoop`·`PrivateLoop`)가 `session_slots.get_hook_runner`
    로 같은 인스턴스를 받는다. 러너는 Python 훅만 돈다 — 셸 훅(`hooks.json`)은 종류의
    `hooks_config` 경로가 따로 돈다. `teardown_session` 이 푼다."""
    from agent_cli.hooks.runner import HookRunner

    runner = HookRunner()
    set_hook_runner(runner)
    return runner


def build_snapshot_store(session_id: str | None = None):
    """세션 스냅샷 저장소 (v10.36.0, docs/snapshot/DESIGN.md) — run/web 부트스트랩이
    `build_hook_runner` 옆에서 부른다. 여기서 만드는 이유: 루프의 tools_list 는
    LoopConfig 조립 때 정해지므로 `snapshot` 도구를 "저장소가 있을 때만" 광고하려면
    훅(OnSessionStart — 그보다 늦다)이 아니라 부트스트랩에서 저장소가 서야 한다.
    내장 훅은 이 저장소에 체크포인트·안내만 얹는다. ``session_id`` 는 시작 커밋의
    라벨에 — 저장소가 세션을 넘어 이어지므로 `list` 의 경계가 된다(v10.80.0).

    끔(`AGENT_CLI_SNAPSHOT=0`)·git 없음·init 실패 → None 을 두고 사람에게 한 줄.
    그 까닭은 `reset_snapshot(off=(cause, detail))` 로 남긴다 — 웹 Changes 입구가 읽는다
    (v10.144.2, `snapshot.snapshot_off`).
    모델에겐 도구도 안내도 가지 않는다(없는 기능을 약속하지 않는다)."""
    from pathlib import Path

    from agent_cli.constants import snapshot_knobs
    from agent_cli.render import get_renderer
    from agent_cli.snapshot import get_snapshot, reset_snapshot

    if not snapshot_knobs()["enabled"]:
        reset_snapshot(off=("disabled", ""))
        return None
    reset_snapshot()  # 까닭 없음 — 지금 세운다
    store = get_snapshot(Path.cwd(), create=True, session_id=session_id)
    if store is None or not store.enabled:
        get_renderer().status(
            "running", f"📸 Snapshots off — {store.reason if store else 'unavailable'}"
        )
        # 실패한 저장소는 남기지 않는다(도구 광고·훅이 get_snapshot 을 본다) — 까닭만 남긴다
        reset_snapshot(
            off=(store.off_cause or "init_failed", store.reason)
            if store
            else ("init_failed", "unavailable")
        )
        return None
    get_renderer().status(
        "running",
        f"📸 Snapshots ready — {store.git_dir.relative_to(store.workspace)} "
        f"({store.stats.seconds:.1f}s, {len(store.nested)} nested repos)",
    )
    return store


def build_monitor_registry(session_dir=None):
    """monitor 레지스트리 생성 + 프로세스 전역 등록 (run/web 공용).

    `build_agent_registry` 의 형제다. 도구는 세션 슬롯(`session_slots`)
    으로 닿는다 — 루프가 `LoopConfig.monitor_registry` 로도 닿던 두 번째
    경로는 주소 배달로 바뀌며 사라졌다(보고를 루프가 drain 하지 않는다).
    배달 배선은 `wire_monitor_delivery` 가 건다.
    """
    from agent_cli.monitor.registry import MonitorRegistry

    registry = MonitorRegistry(session_dir=session_dir)
    set_monitor_registry(registry)
    return registry


def build_schedule_registry(session_dir=None):
    """schedule 레지스트리 생성 + 프로세스 전역 등록 (run/web 공용).

    만들기만 한다 — 발화는 main 앞 사람의 말 자리가 필요하므로, 호출자가 ``enqueue`` 를 꽂은
    뒤 ``start()`` 를 부른다(첫 정산이 꺼져 있던 동안 지난 발화를 질문으로
    남긴다). docs/schedule/DESIGN.md §6.
    """
    from agent_cli.schedule.registry import ScheduleRegistry

    registry = ScheduleRegistry(session_dir=session_dir)
    set_schedule_registry(registry)
    return registry


def wait_for_human_words(main_input, surface):
    """main 의 제자리 대기를 깨울 사람 입력 — ``(timeout) -> bool`` (v10.142.0, web 기본).

    main 앞에 사람의 말이 도착하면 깨우되, 맨 앞이 명령(들어올 때의 표시 — ``/stop agt-3`` 등)이면 그
    자리에서 꺼내 말풍선을 내고(``surface.echo`` — 펌프가 연 차례·턴 경계와 같이 실행 앞에) 라우팅하고 깨우지
    않는다: 줄에 둔 채 깨우면 main 이 "the user wrote to you" 관찰을 읽고 그 말은 다음 흡수의 라우팅이 삼켜, 말 없는
    관찰만 남았다. 명령이 아닌 말(명령 모양의 경로 등)은 꺼내지 않고 깨운다.
    CLI ``run`` 은 터미널에서 친 줄도 main 앞에 넣고 이것으로 읽는다(`main._terminal_steer_input`). 실행이 받지
    않은 명령(문과 실행 사이에 같은 이름의 경로가 생겼다)은 사람의 말로 다시 넣고 깨운다."""

    def wait(timeout: float) -> bool:
        if not main_input.wait_for_arrival(timeout):
            return False
        item = main_input.take_first_command()
        if item is None:
            return True
        surface.echo(item)
        if surface.route(str(item.get("text") or ""), item=item):
            return (
                False  # 명령은 처리했다 — 기다림은 계속(멈춘 에이전트의 회신이 깨운다)
            )
        # 실행이 받지 않았다(문과 실행 사이에 같은 이름의 경로가 생겼다) — 사람의 말로 다시 넣는다: 문이 다시 판정해
        # (이제 경로) 말로 서고, 깨운다(다음 턴 경계가 주입한다). 상자 맨 앞에 되돌릴 길이 우편에 없어 끝에 선다.
        main_input.enqueue(
            item.get("conn_id") or None,
            str(item.get("text") or ""),
            nickname=item.get("nickname"),
            images=item.get("images") or None,
        )
        return True

    return wait


def wire_agent_mail(registry, *, on_mail_notice=None, parent_ctx=None):
    """회신 알림 훅 + restore/auto_spawn 조립 (run/web 공용).

    한가한 main 을 메일이 깨우는 일은 따로 없다 — 메일과 사람의 말이 같은 상자·같은 조건변수에 서고 펌프가 그
    둘을 기다린다(`MainInput.wait_to_open`). 모니터 보고·작업 통지도 주소 배달로 같은 상자에 든다.

    Returns ``(revived, auto)`` — 부활/auto-spawn 수는 호출자가 자기 표면(콘솔/렌더러)으로 알린다. 감사자
    핸들(`harness.auditor.build_auditor`)은 호출자가 **이 앞에서** 만들고 노브를 적용한다(`knobs.apply_all`) —
    상한 노브가 restore/auto_spawn 의 게이트이기 때문이다 (v10.43.0)."""

    if on_mail_notice is not None:

        def _on_agent_mail(reply: dict, *, idle_at_delivery: bool) -> None:
            # ``on_mail_notice(reply, busy)`` — CLI 만 건다(v10.51.0): main 이 바쁠 때만 대기 줄을 찍는다. web 은
            # 미배달 메일이 입력창 위 대기열에 사용자 요청과 같은 줄로 선다(`registry.on_pending_change`). busy 는
            # "그 메일이 도착한 순간 main 이 바빴는가" — 배달 구간에서 읽은 값이다(지금의 한가함을 읽으면 펌프가 이미
            # 이 메일로 런을 연 뒤일 수 있다).
            on_mail_notice(reply, not idle_at_delivery)

        registry.on_reply = _on_agent_mail
    # P3 (D7): resume 세션이면 이전 에이전트 자동 재생성 + 미배달 회신
    # 복원 (fresh 세션은 agents.json 이 없어 no-op).
    revived = registry.restore(parent_ctx=parent_ctx)
    auto = registry.auto_spawn(parent_ctx=parent_ctx)
    return revived, auto


def main_run_ended(
    agent_registry,
    output: str = "",
    *,
    stopped: bool = False,
    **meta,
) -> int:
    """main 의 런이 끝났다 — 남은 빚을 정리한다 (v9.22.0).

    독촉은 런 **안**에서 있었다(dispatch 의 빚 목록 — 상주와 같은 기계).
    그러고도 남은 회신 빚은 라벨 붙은 런 요약(``output``)을 그 에이전트 inbox 로
    폴백 배달한다. 판정은 상주와 같은 `AgentRegistry.run_ended` 한 곳이다.

    상주 에이전트는 워커 루프의 디스패치 수렴점에서 같은 일을 한다. main 은
    펌프가 둘(run/web)이라 호출부가 둘인데, **정의는 하나**여야 한다 —
    한쪽만 고치는 것이 이 파일이 존재하는 이유의 사고 유형이다
    (모듈 docstring 의 teardown 4갈래 참조).

    독촉은 main 의 상자로 가고 한가한 펌프가 그것으로 런을 연다. 건 수를
    돌려준다(0 = 빚 없음).

    ``stopped`` (v10.156.0): 사람이 main 을 멈췄다(■·Stop everything) — 정지 회신은 부르는 쪽이 먼저 보냈다
    (`AgentRegistry.answer_main_debts_as_stopped`), 여기는 남은 줄을 버린다(에이전트의 ■ 와 같다). ``meta``
    (v10.156.0): 실패한 런이면 ``success=False``·``error`` — 폴백 머리에 원인 한 줄(에이전트의 실패 런과 같다).
    """
    if agent_registry is None:
        return 0
    return agent_registry.run_ended("main", output or "", stopped=stopped, **meta)


def teardown_session(
    session,
    ctx,
    *,
    agent_registry=None,
    mcp_manager=None,
    monitors=None,
    schedules=None,
    harness=None,
    jobs=None,
    warn_stuck: bool = False,
    deadline: float | None = None,
) -> None:
    """공용 종료 시퀀스 — 모든 run/web 종료 경로가 여기로 수렴한다.

    순서(종전 run 메인 경로와 동일): ①(옵션) 미답 질문 경고
    ② 생산자 정지(모니터·예약 스레드) ③ 상주 에이전트 전원 종료 ④ 스피너 정지
    ⑤ MCP 해제(stdio 자식 프로세스·errlog fd 정리) ⑥ 세션 저장 ⑦ 세션 슬롯 비우기
    (앞 단계가 실패해도). 저장-완료
    메시지는 표면별로 다르므로 호출자가 출력한다.

    ``deadline`` (v10.134.1, ``time.monotonic`` 기준): SIGTERM 으로 들어온 정리만 준다 —
    감사 작업·잡·상주 워커를 기다리는 시간을 그 안으로 줄인다(보드는 8초 뒤 SIGKILL,
    `terminate.TEARDOWN_BUDGET_S`). None 이면 종전 대기 그대로."""
    if warn_stuck and agent_registry is not None:
        stuck = agent_registry.open_human_question_keys()
        if stuck:
            from agent_cli.render import C, console

            console.print(
                f"[{C['accent']}]❓ Exiting with unanswered questions from "
                f"{', '.join(stuck)} — they are dropped when the session ends[/]"
            )
    if monitors is not None:
        # **에이전트 종료보다 먼저.** 폴링 스레드는 종전에 아무도 안 멈췄고
        # (`stop()` 은 호출자가 없었다) 지금까지는 아무도 안 읽는 리스트에
        # 쌓을 뿐이라 무해했다. 이제는 **배달된다** — 종료 뒤 발화하면
        # `finalize_session` 뒤에 `agents.json` 이 다시 쓰이거나, 세션과
        # 함께 죽은 감시의 흔적이 다음 세션에 떠오른다.
        #
        # 전체 드롭이 아니라 `closed` 인 이유: `drop_owner` 가 `delete` 처럼
        # 저장하면 살아 있는 행만 쓰는 `_save` 가 `monitors.json` 을 비워,
        # 다음 세션의 "이전 세션 모니터 N건" 통지가 사라진다.
        monitors.stop()
        monitors.closed = True
    if schedules is not None:
        # 예약 스레드도 같은 자리에서 (v10.130.4) — 종전엔 두 호스트가 정리 **뒤에**
        # 따로 멈춰, 정리 도중 아무도 읽지 않을 큐로 발화하고 "발화함" 으로 적을 틈이 있었다.
        # `stop` 이 아니라 `close` (v10.133.3) — 아래 에이전트 종료를 기다리는 동안 누가
        # 예약을 추가·켜도 `start()` 가 스레드를 되살리지 않는다.
        schedules.close()
    if harness is not None:
        # 진행 중 감사를 세운다 (v10.42.0) — 팀원 종료와 같은 자리
        if deadline is None:
            harness.stop()
        else:
            harness.stop(deadline=deadline)
    if jobs is not None:
        # 우리가 띄운 자식이다 — 고아로 남기지 않는다 (v10.47.0). 죽인 것은 적는다.
        killed = (
            jobs.stop_all() if deadline is None else jobs.stop_all(deadline=deadline)
        )
        if killed:
            from agent_cli.render import C, console

            names = ", ".join(f"#{j.id} pid {j.pid}" for j in killed)
            console.print(
                f"[{C['muted']}]🧵 Session ended — stopped {len(killed)} running "
                f"background job(s) ({names})[/]"
            )
    if agent_registry is not None:
        if deadline is None:
            agent_registry.shutdown_all()
        else:
            agent_registry.shutdown_all(deadline=deadline)
    try:
        from agent_cli.render import render_spinner_stop

        render_spinner_stop()
        from agent_cli.browser import reset_browser

        reset_browser()  # 세션 브라우저 종료 (없으면 no-op)
        if mcp_manager:
            mcp_manager.disconnect_all()
        if session is not None:
            from agent_cli.session.store import finalize_session

            finalize_session(session, ctx)
    finally:
        # ⑦ 세션 슬롯 다섯을 비운다 (v10.133.25) — 맨 마지막: 위 단계(에이전트 종료의
        # release·잡 정리)는 아직 자리를 읽는다. 종전엔 훅 러너만 여기서 비웠고 나머지 넷은
        # 닫힌 객체를 계속 가리켰다. 뒤늦게 도는 스레드는 None 을 읽는다(꺼내는 쪽이 처리한다).
        clear_session_slots()


# ── 세션 조립 — run·web 이 같은 함수 넷 (v10.131.0) ─────────────────────────────
#
# 종전엔 `main.run()`·`main.web()` 이 같은 세션을 두 번 조립했다 — 세션에 뭔가
# 붙을 때마다 두 곳을 고치고, 한쪽만 고친 비대칭이 버그가 됐다(v10.31.2·v10.32.1,
# `pump.py` 머리말). 지금은 값을 돌려주는 평범한 함수 넷이고, 호스트는 위에서
# 아래로 읽히는 스크립트로 남는다: 호스트마다 다른 것(화면·서버 손잡이·스레드·
# 수명)은 호스트에, 호스트마다 다른 값은 기본값 None 의 키워드 인자로만 들어온다.
# 이 함수들은 어느 호스트에서 불리는지 모른다.


@dataclass(frozen=True, kw_only=True)
class SessionSpec:
    """세션이 무엇인가 — 부트 단계(공급자·MCP·세션 고르기·ctx)가 정한 것. 살아 있는
    것(스레드·레지스트리)은 없다. 디스크 훅은 spec 을 만들 때 읽는다
    (:meth:`for_session`)."""

    binding: Any
    session: Any
    ctx: Any
    mcp_manager: Any
    knob_args: dict | None
    hooks_config: dict | None
    max_turns: int
    max_depth: int
    verbose: bool
    record_turns: bool

    @classmethod
    def for_session(
        cls,
        *,
        binding,
        session,
        ctx,
        mcp_manager,
        knob_args: dict | None,
        max_turns: int,
        max_depth: int,
        verbose: bool,
        record_turns: bool,
    ) -> SessionSpec:
        """부트가 정한 값 + 디스크 훅(`.agent-cli/hooks.json`, 순수 읽기·캐시) — 그 훅이
        `AgentRuntime`(상주 에이전트)과 `MainLoop`(main 루프) 양쪽에 닿는다."""
        from agent_cli.hooks import load_hooks

        return cls(
            binding=binding,
            session=session,
            ctx=ctx,
            mcp_manager=mcp_manager,
            knob_args=knob_args,
            hooks_config=load_hooks() or None,
            max_turns=max_turns,
            max_depth=max_depth,
            verbose=verbose,
            record_turns=record_turns,
        )

    @property
    def session_dir(self):
        return self.ctx.session_dir if self.ctx else None


@dataclass(frozen=True, kw_only=True)
class SessionStores:
    """스레드를 띄우지 않는 저장소들 — 모니터·백그라운드 작업·예약."""

    monitors: Any
    jobs: Any
    schedules: Any
    #: main 앞 사람의 말(`MainInput`) — 그 창구가 세션의 창구다(레지스트리가 받는다)
    main_input: Any


@dataclass(frozen=True, kw_only=True)
class SessionOwners:
    """세션을 소유하는 둘 — 에이전트 레지스트리와 감사자. 아직 아무것도 살아 있지
    않다(복원·auto-spawn 은 :func:`wire_session`)."""

    agent_registry: Any
    auditor: Any


@dataclass(frozen=True, kw_only=True)
class WiredSession:
    """배선이 끝난 세션 — ``pump.run()`` 만 남았다. ``revived``·``auto`` 는 호스트가
    자기 화면으로 알린다."""

    pump: Any
    revived: int
    auto: int


def build_session_stores(
    spec: SessionSpec, *, main_input, on_jobs_change=None, on_schedules_change=None
) -> SessionStores:
    """모니터 → 작업 → 훅 러너 → 스냅샷 저장소 → 예약. 예약의 발화 자리(``enqueue``)는
    여기서 ``main_input`` 에 묶는다 — 스레드는 :func:`wire_session` 의 끝에서 켠다.
    ``on_*_change`` 는 화면이 있는 호스트의 관찰자(web 칩·status.json)."""
    monitors = build_monitor_registry(spec.session_dir)
    jobs = build_job_registry()  # 🧵 백그라운드 작업 (v10.47.0)
    if on_jobs_change is not None:
        jobs.on_change = on_jobs_change  # 띄움·멈춤·끝남 → 스티키 하나 (v10.121.0)
    build_hook_runner()  # Python 훅 — 네 루프가 같은 러너 (v10.35.0)
    # 📸 저장소가 서야 `snapshot` 도구가 광고된다 (v10.36.0)
    build_snapshot_store(spec.session.session_id)
    schedules = build_schedule_registry(spec.session_dir)
    # 예약 발화는 사용자 요청처럼 main 앞 사람의 말로 들어온다 (docs/schedule/DESIGN.md §5·§6.3)
    schedules.enqueue = lambda prompt, nickname, schedule_id: main_input.enqueue(
        None, prompt, nickname=nickname, src=schedule_addr(schedule_id)
    )
    if on_schedules_change is not None:
        schedules.on_change = on_schedules_change
    return SessionStores(
        monitors=monitors, jobs=jobs, schedules=schedules, main_input=main_input
    )


def apply_knobs(knob_args, *, ctx, registry, renderer, auditor) -> None:
    """부팅 노브 적용 — 소유자 넷이 선 뒤, 상주 에이전트 복원·auto-spawn **전에**
    (상한이 그 게이트다, v10.43.0)."""
    from agent_cli.knobs import KnobSession, apply_all, resolve_all

    apply_all(
        KnobSession(ctx=ctx, registry=registry, renderer=renderer, auditor=auditor),
        resolve_all(knob_args),
    )


def build_session_owners(
    spec: SessionSpec, stores: SessionStores, *, renderer, on_harness_change=None
) -> SessionOwners:
    """에이전트 레지스트리 → 감사자 → 노브 적용. 레지스트리는 main 루프에만 주입된다
    (서브에이전트는 도구가 자동으로 빠진다). 모니터 보고·작업 종료 통지의 주소 배달은
    여기서 — 어떤 루프보다 먼저 서야 한다(`wire_monitor_delivery`).
    ``on_harness_change`` — 감사 진행 수가 바뀔 때(web: status.json ``auditing``)."""
    from agent_cli.harness.auditor import build_auditor

    registry = build_agent_registry(
        spec.session_dir,
        AgentRuntime(
            binding=spec.binding,
            max_turns=spec.max_turns,
            depth=0,
            max_depth=spec.max_depth,
            session=spec.session,
            hooks_config=spec.hooks_config,
        ),
        post=stores.main_input.post,
        monitors=stores.monitors,
        jobs=stores.jobs,
    )
    auditor = build_auditor(registry, parent_ctx=spec.ctx, on_change=on_harness_change)
    apply_knobs(
        spec.knob_args,
        ctx=spec.ctx,
        registry=registry,
        renderer=renderer,
        auditor=auditor,
    )
    return SessionOwners(agent_registry=registry, auditor=auditor)


def wire_session(
    spec: SessionSpec,
    stores: SessionStores,
    owners: SessionOwners,
    *,
    surface,
    policy,
    on_mail_notice=None,
    on_wait_input=None,
    on_idle_input=None,
) -> WiredSession:
    """메일 복원·자동 스폰(`wire_agent_mail`) → `MainLoop` → `MainActor` → 예약 스레드.
    **소비자를 다 세운 뒤에 생산자를 켠다** 가 이 함수 하나에서 보장된다 — 도중에
    죽으면 예약은 켜지지 않는다. ``surface``·``policy`` 는 호스트의 것(펌프 표면·수명),
    ``on_mail_notice`` 는 main 이 바쁠 때의 메일 알림(run 만 — web 은 대기열이 그 자리),
    ``on_wait_input`` 은 제자리 대기를 깨울 사람 입력을 기다리는 법(없으면 main 앞 사람의 말 — run 은
    터미널 줄도). 사람의 말은 ``stores.main_input`` 이다."""
    from agent_cli.pump import MainActor, PumpDeps

    registry = owners.agent_registry
    main_input = stores.main_input
    # 재시작(5c-0): main 컨텍스트가 디스크에서 올라왔으면 끝에 사실 기록 한 줄 — 복원·펌프보다 먼저, 사람의 첫 말이나
    # 예약이 연 런이 그것을 읽는다. Stop 처럼 화면에도 관찰 카드로(웹의 대화창 재생은 이보다 먼저 돌았다).
    if spec.ctx is not None and record_restart(spec.ctx):
        render_step(
            "observation",
            RESTART_NOTICE,
            0,
            tool_name="interrupt",
            success=False,
            hidx=spec.ctx.last_ordinal,
        )
    revived, auto = wire_agent_mail(
        registry, on_mail_notice=on_mail_notice, parent_ctx=spec.ctx
    )
    # main 루프의 종류 (v10.87.0) — 세션에 하나. 런 도중 주입: 사람의 말·예약 발화가 다음 턴
    # 경계에 요청으로 합류한다(펌프는 아래서 만들어지므로 라우팅은 호출 시점에 푸는 콜러블).
    main_kind = MainLoop(
        binding=spec.binding,
        ctx=spec.ctx,
        session=spec.session,
        agent_registry=registry,
        mcp_manager=spec.mcp_manager,
        enqueue_user_message=lambda text, nickname: main_input.enqueue(
            None, text, nickname=nickname
        ),
        route_message=lambda text, item: pump.route(text, item),
        wait_for_user_input=on_wait_input or wait_for_human_words(main_input, surface),
        session_closed=main_input.closed,
        max_turns=spec.max_turns,
        max_depth=spec.max_depth,
        verbose=spec.verbose,
        record_turns=spec.record_turns,
        hooks_config=spec.hooks_config,
        graceful_interrupt=surface.graceful_interrupt,
    )
    pump = MainActor(
        PumpDeps(
            main_input=main_input,
            agent_registry=registry,
            main=main_kind,
            surface=surface,
            auditor=owners.auditor,
            policy=policy,
            read_terminal=on_idle_input,
        )
    )
    # 첫 정산이 꺼져 있던 동안 지난 발화를 질문으로 남긴다. 펌프가 돌기 전에 발화한
    # 예약은 main 상자에 줄 설 뿐이다 (v10.130.5).
    stores.schedules.start()
    return WiredSession(pump=pump, revived=revived, auto=auto)


def close_session(
    spec: SessionSpec,
    stores: SessionStores | None,
    owners: SessionOwners | None,
    *,
    warn_stuck: bool,
    deadline: float | None = None,
) -> None:
    """세션 정리 — `teardown_session` 으로. 조립 도중 죽었으면 ``stores``·``owners`` 가
    None 이다: 있는 것만 정리하고, MCP 해제·세션 마감은 늘 돈다.

    ``owners`` 는 통째로만 선다 — 레지스트리를 만든 뒤 감사자·노브에서 죽으면 그
    레지스트리는 여기 닿지 않는다(종전 호스트는 `shutdown_all` 까지 불렀다). 복원
    전이라 멈출 에이전트가 없고, `shutdown_all` 의 마지막 저장이 빈 명단으로
    ``agents.json`` 을 덮어쓰는 일도 그 길에서는 없다."""
    teardown_session(
        spec.session,
        spec.ctx,
        agent_registry=owners.agent_registry if owners is not None else None,
        mcp_manager=spec.mcp_manager,
        monitors=stores.monitors if stores is not None else None,
        schedules=stores.schedules if stores is not None else None,
        harness=owners.auditor if owners is not None else None,
        jobs=stores.jobs if stores is not None else None,
        warn_stuck=warn_stuck,
        deadline=deadline,
    )


def session_has_live_work(
    *,
    post,
    agent_registry=None,
    monitors=None,
    schedules=None,
    harness=None,
    jobs=None,
) -> bool:
    """세션이 아직 할 일을 들고 있는가 — **판정 하나** (v10.32.1, 5b-1 에서 에이전트·main 상자 항까지).

    `run` 의 끝(`pump.QuietPolicy.should_stop`)과 `web` 의 한가함 정리(`main.web_instance_is_active`)가 같은 항을
    이 함수로 본다. 호스트마다 다른 것은 호스트 항뿐이다(웹: 접속한 뷰어·바쁜 main 워커·열린 사람 질문 —
    사람의 답은 웹에서만 온다, `run` 이 세면 끝나지 않는다).

    - main 상자에 깨우는 봉투 — 사람의 말과 깨우는 메일(``post`` 는 main 의 입력 큐와 레지스트리가 쓰는 창구).
      깨우지 않는 메일("Stop everything" 의 정지 회신)은 일이 아니다 — 남아 있어도 끝나고, 세션이 끝나면 버려진다.
    - 에이전트 몫(`AgentRegistry.has_agent_work` — 일하는 중이거나 할 일이 쌓였다). 레지스트리는 웹 부트스트랩이
      늦게 채워 None 일 수 있다.
    - 살아 있는 모니터·진행 중인 하니스 작업(감사 — 결과를 main 메일함에 넣어야 끝난다, v10.42.0)·돌고 있는
      백그라운드 작업(v10.47.0)·켜진 예약. 종전엔 이 항이 붙을 때마다 두 곳에 한 줄씩 넣어야 했고, 초판 모니터는
      web 쪽을 빠뜨려 뷰어 없는 인스턴스가 살아 있는 모니터를 데리고 자기를 거뒀다.
    """
    if post.arrived("main", waking_only=True, words=True):
        return True
    if agent_registry is not None and agent_registry.has_agent_work():
        return True
    if monitors is not None and monitors.has_active_work():
        return True
    if harness is not None and harness.in_flight:
        return True
    if jobs is not None and jobs.has_active_work():
        return True  # v10.47.0: 돌고 있는 백그라운드 작업 — 종료 통지까지 기다린다
    return schedules is not None and schedules.has_active_work()


def _main_questions(agent_registry):
    """main 의 답변 수단 (docs/agent-ask/DESIGN.md §4)."""
    return agent_registry.question_port(None) if agent_registry else None


class MainLoop(LoopKind):
    """main 루프의 종류 — 세션에 하나, ``run(start)`` 은 런을 연 것마다 (v10.87.0).

    run 과 web 이 같다(v10.33.0, docs/pump/DESIGN.md): 펌프가 main 상자에서 꺼낸 것을 `start(item, stop_event)`
    로 넘기고, 결과·`run_ended`·표면 알림은 펌프가 한다(호스트의 것). 깊이 0, 도구 전부(MCP 포함),
    세션 ctx 그대로, 런마다 거둘 것 없음(세션 teardown 이 전부). ``route_message`` 는 펌프의
    메서드라 세션 수명이고 펌프보다 먼저 만들어지므로 호출 시점에 푸는 콜러블로 받는다.
    """

    def __init__(
        self,
        *,
        binding,
        ctx,
        session,
        agent_registry,
        mcp_manager,
        enqueue_user_message,
        route_message,
        max_turns: int,
        max_depth: int,
        verbose: bool,
        record_turns: bool,
        hooks_config: dict | None,
        graceful_interrupt: bool,
        wait_for_user_input=None,
        session_closed=None,
    ) -> None:
        self.owner = "main"
        self.binding = binding
        self.ctx = ctx
        self.session = session
        self.max_turns = max_turns
        self.max_depth = max_depth
        self.verbose = verbose
        self.record_turns = record_turns
        self.hooks_config = hooks_config
        self.graceful_interrupt = graceful_interrupt
        self._session_closed = session_closed
        self.ports = self.make_ports(
            agent_registry=agent_registry,
            mcp_manager=mcp_manager,
            enqueue_user_message=enqueue_user_message,
            route_message=route_message,
            wait_for_user_input=wait_for_user_input,
        )

    @staticmethod
    def make_ports(
        *,
        agent_registry,
        mcp_manager,
        enqueue_user_message,
        route_message,
        wait_for_user_input=None,
    ) -> LoopPorts:
        """``wait_for_user_input`` (v10.141.0): ``(timeout) -> bool`` — 사람 입력이 도착했는가
        (꺼내지 않는다; 호스트의 것 — web 은 main 앞 사람의 말, run 은 터미널 줄 + 그것). 기다리는
        회신을 둔 제자리 `wait` 를 사람의 말이 깨운다."""
        return LoopPorts(
            owner="main",
            questions=_main_questions(agent_registry),
            agent_registry=agent_registry,
            mcp_manager=mcp_manager,
            enqueue_user_message=enqueue_user_message,
            route_message=route_message,
            # main 도 `message` 로 요청·통지를 보낸다 (v10.141.0 — 종전 `agent` "request")
            message_handler=(
                agent_registry.message_handler("main")
                if agent_registry is not None
                else None
            ),
            hook_runner=_hook_runner_port(),
            peer_roster=None,
            # 턴 경계의 흡수는 main 상자 하나(도착 순서 — `AgentRegistry.absorb_main`)
            mailbox=Mailbox(
                agent_registry,
                "main",
                take=agent_registry.absorb_main if agent_registry is not None else None,
                wait_input=wait_for_user_input,
            ),
            unwired={
                **(
                    {"message_handler": "레지스트리가 없는 세션 — 보낼 상대가 없다"}
                    if agent_registry is None
                    else {}
                ),
                **_hook_unwired(),
                "peer_roster": _RESIDENT_ROSTER,
            },
        )

    # ── 엔진이 묻는 것 (v10.88.0) ──
    def session_ended(self) -> bool:
        """main 상자가 닫혔는가(`MainInput.closed`) — 세션이 끝나는 중이다(5e1)."""
        return bool(self._session_closed is not None and self._session_closed())

    def intake(self) -> list[dict]:
        """main 상자의 줄 선 것 전부 — 도착 순서(사람의 말의 라우팅은 엔진, ``mailbox.take``)."""
        mailbox = self.ports.mailbox
        return list(mailbox.take() or []) if mailbox.has_inbox else []

    def prompt_profile(self) -> PromptProfile:
        return PromptProfile(
            role="",  # 기본 ROLE_PROMPT
            peer_section="",
            ask_blocking=True,
            reports_to_caller=False,
        )

    def tail_roster(self, tools: list[str]) -> str:
        registry = self.ports.agent_registry
        if registry is None or "agent" not in tools:
            return ""
        from agent_cli.prompts.system_prompt import build_live_agents_section

        return build_live_agents_section(registry, include_state=True)

    def renders(self) -> bool:
        return True

    @property
    def tracks_run_authors(self) -> bool:
        return (
            True  # 웹 다중 사용자 귀속 — 렌더러와 레지스트리의 요청자 스냅샷을 비춘다
        )

    @staticmethod
    def start(opened: dict, stop_event) -> Start:
        """펌프가 꺼낸 봉투(`MainInput.wait_to_open` 의 모양) → 시작. 사람의 말이면 그 말이 시작 메시지(작성자 =
        닉네임, 없으면 run 의 argv 질의). 메일이면 시작 메시지가 없고 사람의 요청이 아니다 — 그 메일이 런의 첫
        입력이다(엔진이 턴 경계의 흡수와 같은 길로 넣는다)."""
        item = opened.get("message")
        if item is None:
            return Start(
                query="", author_is_user=False, opening=opened, stop_event=stop_event
            )
        return Start(
            query=str(item.get("text") or ""),
            images=list(item.get("images") or []),
            author=item.get("nickname") or None,
            author_is_user=True,
            request_id=str(item.get("id") or ""),
            stop_event=stop_event,
        )
