"""run/web 부트스트랩·teardown 조립기 (리뷰 §4.1 P1, v8.39.0).

세 조각을 소유한다:

- :class:`AgentRuntime` — 상주/서브 에이전트에 넘기는 provider 배선
  13키의 **단일 정의**. 종전엔 run/web(main.py)/tool_bridge 세 곳이 같은
  dict 리터럴을 손으로 나열했고, 키 구성부터 어긋나 있었다(run/web 엔
  ``compaction_enabled`` 부재, web 은 ``hooks_config`` 를 None 으로 고정).
- :func:`build_agent_registry` / :func:`wire_agent_mail` — registry 생성+
  main 슬롯 등록, 그리고 waker·회신 훅·restore/auto_spawn 조립의 공용화.
  두 단계로 쪼갠 이유: run 은 skill 조기-반환 **이전**에 registry 만 만들고
  restore 는 그 뒤에 하므로(순서 보존 = 등가성), 생성과 배선을 분리해야
  기존 순서를 바이트 그대로 유지한다.
- :func:`teardown_session` — 종료 시퀀스의 단일 소유자: 상주 에이전트
  전원 종료 → 스피너 정지 → MCP 해제 → 세션 저장. 종전엔 run 의 종료
  경로 4갈래 + web 1갈래가 각자 나열했고, 그 결과 2경로는 MCP 미해제
  (stdio 자식 프로세스 잔존), 1경로는 registry 미종료였다 — 모든 경로가
  이 함수 하나로 수렴하면 그 누락 클래스가 구조적으로 사라진다.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from agent_cli.loop.ports import LoopPorts


@dataclass(frozen=True)
class AgentRuntime:
    """상주/서브 에이전트 실행에 필요한 provider 배선 묶음.

    소비자(AgentRegistry._run_message / runner)는 dict 를 기대하므로
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
        """LoopConfig → AgentRuntime (tool_bridge 의 상주 모드 인터셉트용)."""
        return cls(
            binding=cfg.binding,
            max_turns=cfg.max_turns,
            depth=cfg.depth,
            max_depth=cfg.max_depth,
            session=cfg.session,
            hooks_config=cfg.hooks_config,
            compaction_enabled=cfg.compaction_enabled,
        )


def build_agent_registry(
    session_dir, runtime: AgentRuntime, max_agents=None, *, monitors=None, jobs=None
):
    """AgentRegistry 생성 + main registry 슬롯 등록 (v7.17.0 배선 통일).

    runtime 프리필: restore/auto-spawn 된 에이전트가 도구 호출(스폰) 없이
    첫 접촉(웹 창 인간 개입 등)을 받아도 provider 배선이 있도록.

    ``max_agents`` None = 미지정 → registry 가 env/기본값을 고른다 (v8.61.0,
    CLI ``--max-agents``)."""
    from agent_cli.subagent.agents_live import AgentRegistry, set_main_registry

    registry = AgentRegistry(
        session_dir, runtime=runtime.as_dict(), max_agents=max_agents
    )
    set_main_registry(registry)
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
    from agent_cli.jobs import JobRegistry, set_job_registry

    registry = JobRegistry(cwd)
    set_job_registry(registry)
    return registry


def build_py_registry(cwd=None):
    """`python` 도구의 워커 레지스트리 생성 + 프로세스 전역 등록 (v10.66.0,
    `build_job_registry` 의 형제). 워커 자체는 첫 호출 때 뜬다. 끄기:
    ``AGENT_CLI_PYTHON_TOOL=0`` — 그러면 도구가 프롬프트에서 빠진다."""
    import os

    from agent_cli.pyworker import PyWorkerRegistry, set_py_registry

    if os.environ.get("AGENT_CLI_PYTHON_TOOL", "1") == "0":
        set_py_registry(None)
        return None
    registry = PyWorkerRegistry(cwd)
    set_py_registry(registry)
    return registry


def python_resource():
    """`Tool.requires_handler="python"` 이 보는 세션 자원 — 레지스트리, 아니면 None."""
    from agent_cli.pyworker import get_py_registry

    reg = get_py_registry()
    return reg if reg is not None and not reg.closed else None


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
    빌더(`ports_for_main/resident/skill/oneshot`)가 `hooks.runtime.get_hook_runner`
    로 같은 인스턴스를 받는다. 셸 훅(`hooks.json`)은 `LoopConfig.hooks_config`
    경로가 따로 돌므로 러너엔 주지 않는다(두 번 실행 방지). `teardown_session` 이
    푼다."""
    from agent_cli.hooks.runner import HookRunner
    from agent_cli.hooks.runtime import set_hook_runner

    runner = HookRunner(shell_hooks_config=None)
    set_hook_runner(runner)
    return runner


def build_snapshot_store():
    """세션 스냅샷 저장소 (v10.36.0, docs/snapshot/DESIGN.md) — run/web 부트스트랩이
    `build_hook_runner` 옆에서 부른다. 여기서 만드는 이유: 루프의 tools_list 는
    LoopConfig 조립 때 정해지므로 `snapshot` 도구를 "저장소가 있을 때만" 광고하려면
    훅(OnSessionStart — 그보다 늦다)이 아니라 부트스트랩에서 저장소가 서야 한다.
    내장 훅은 이 저장소에 체크포인트·안내만 얹는다.

    끔(`AGENT_CLI_SNAPSHOT=0`)·git 없음·init 실패 → None 을 두고 사람에게 한 줄.
    모델에겐 도구도 안내도 가지 않는다(없는 기능을 약속하지 않는다)."""
    from pathlib import Path

    from agent_cli.constants import snapshot_knobs
    from agent_cli.render import get_renderer
    from agent_cli.snapshot import get_snapshot, reset_snapshot

    reset_snapshot()
    if not snapshot_knobs()["enabled"]:
        return None
    store = get_snapshot(Path.cwd(), create=True)
    if store is None or not store.enabled:
        get_renderer().status(
            "running", f"📸 Snapshots off — {store.reason if store else 'unavailable'}"
        )
        reset_snapshot()
        return None
    get_renderer().status(
        "running",
        f"📸 Snapshots ready — {store.git_dir.relative_to(store.workspace)} "
        f"({store.stats.seconds:.1f}s, {len(store.nested)} nested repos)",
    )
    return store


def snapshot_resource():
    """`Tool.requires_handler="snapshot"` 이 보는 세션 자원 — 준비된 저장소, 아니면 None."""
    from pathlib import Path

    from agent_cli.snapshot import get_snapshot

    store = get_snapshot(Path.cwd())
    return store if store is not None and store.enabled else None


def browser_resource():
    """`Tool.requires_handler="browser"` 이 보는 세션 자원 — playwright 가 있고
    `AGENT_CLI_BROWSER_TOOL` 이 꺼져 있지 않으면 True(브라우저 자체는 첫 호출 때
    뜬다), 아니면 None → 도구가 프롬프트에서 빠진다."""
    from agent_cli.browser import browser_available

    return True if browser_available() else None


def build_monitor_registry(session_dir=None):
    """monitor 레지스트리 생성 + 프로세스 전역 등록 (run/web 공용).

    `build_agent_registry` 의 형제다. 도구는 모듈 전역(`monitor/runtime.py`)
    으로 닿는다 — 루프가 `LoopConfig.monitor_registry` 로도 닿던 두 번째
    경로는 주소 배달로 바뀌며 사라졌다(보고를 루프가 drain 하지 않는다).
    배달 배선은 `wire_monitor_delivery` 가 건다.
    """
    from agent_cli.monitor.registry import MonitorRegistry
    from agent_cli.monitor.runtime import set_monitor_registry

    registry = MonitorRegistry(session_dir=session_dir)
    set_monitor_registry(registry)
    return registry


def build_schedule_registry(session_dir=None):
    """schedule 레지스트리 생성 + 프로세스 전역 등록 (run/web 공용).

    만들기만 한다 — 발화는 입력 큐가 필요하므로, 호출자가 ``enqueue`` 를 꽂은
    뒤 ``start()`` 를 부른다(첫 정산이 꺼져 있던 동안 지난 발화를 질문으로
    남긴다). docs/schedule/DESIGN.md §6.
    """
    from agent_cli.schedule.registry import ScheduleRegistry
    from agent_cli.schedule.runtime import set_schedule_registry

    registry = ScheduleRegistry(session_dir=session_dir)
    set_schedule_registry(registry)
    return registry


def wire_agent_mail(registry, *, enqueue_wake, on_mail_notice=None, parent_ctx=None):
    """MailWaker + 회신 알림 훅 + restore/auto_spawn 조립 (run/web 공용).

    종전엔 ``monitors`` 인자를 받아 술어에 ``or monitors.has_pending()`` 을
    얹고 ``on_report`` 를 꽂았다. **둘 다 없앴다** — 모니터 보고가 이제
    주소 배달로 메일박스에 들어오므로, ``has_pending_replies()`` 가 이미
    참이 되고 ``on_reply`` 가 이미 깨운다. 얹을 항이 없는 것이 가장
    안전하다: 그 항을 빠뜨린 것이 v9.11.0 의 "발화해도 조용함" 이었다.

    Returns ``(waker, revived, auto)`` — 부활/auto-spawn 수는 호출자가 자기
    표면(콘솔/렌더러)으로 알린다. 감사자 핸들(`harness.auditor.build_auditor`)은
    호출자가 **이 앞에서** 만들고 노브를 적용한다(`knobs.apply_all`) — 상한
    노브가 restore/auto_spawn 의 게이트이기 때문이다 (v10.43.0)."""
    from agent_cli.subagent.agents_live import MailWaker

    waker = MailWaker(enqueue_wake, registry.has_pending_replies)

    def _on_agent_mail(reply: dict) -> None:
        # ``on_mail_notice(reply, busy)`` — CLI 만 건다(v10.51.0): main 이 바쁠
        # 때만 대기 줄을 찍는다. web 은 None — 미배달 메일이 입력창 위
        # 대기열에 사용자 요청과 같은 줄로 선다(`registry.on_pending_change`).
        if on_mail_notice is not None:
            on_mail_notice(reply, not waker.idle.is_set())
        waker.on_mail()

    registry.on_reply = _on_agent_mail
    # P3 (D7): resume 세션이면 이전 에이전트 자동 재생성 + 미배달 회신
    # 복원 (fresh 세션은 agents.json 이 없어 no-op).
    revived = registry.restore(parent_ctx=parent_ctx)
    auto = registry.auto_spawn(parent_ctx=parent_ctx)
    return waker, revived, auto


def main_run_ended(agent_registry, output: str = "") -> int:
    """main 의 런이 끝났다 — 남은 빚을 정리한다 (v9.22.0).

    독촉은 런 **안**에서 3회 있었다(dispatch 의 빚 목록 — 상주와 같은 기계).
    그러고도 남은 것: 회신 빚은 라벨 붙은 런 요약(``output``)을 그 에이전트
    inbox 로 폴백 배달하고, 답 안 한 질문은 사유와 함께 닫는다.

    상주 에이전트는 워커 루프의 디스패치 수렴점에서 같은 일을 한다. main 은
    펌프가 둘(run/web)이라 호출부가 둘인데, **정의는 하나**여야 한다 —
    한쪽만 고치는 것이 이 파일이 존재하는 이유의 사고 유형이다
    (모듈 docstring 의 teardown 4갈래 참조).

    독촉은 메일박스로 가고 ``MailWaker`` 가 유휴 main 도 깨운다. 건 수를
    돌려준다(0 = 빚 없음).
    """
    if agent_registry is None:
        return 0
    return agent_registry.end_run("main", output or "")


def teardown_session(
    session,
    ctx,
    *,
    agent_registry=None,
    mcp_manager=None,
    monitors=None,
    harness=None,
    jobs=None,
    warn_stuck: bool = False,
) -> None:
    """공용 종료 시퀀스 — 모든 run/web 종료 경로가 여기로 수렴한다.

    순서(종전 run 메인 경로와 동일): ①(옵션) 미답 질문 경고
    ② 상주 에이전트 전원 종료 ③ 스피너 정지 ④ MCP 해제(stdio 자식
    프로세스·errlog fd 정리) ⑤ 세션 저장. 저장-완료 메시지는 표면별로
    다르므로 호출자가 출력한다."""
    if warn_stuck and agent_registry is not None:
        stuck = agent_registry.open_human_question_keys()
        if stuck:
            from agent_cli.render import C, console

            console.print(
                f"[{C['accent']}]❓ Exiting with unanswered questions from "
                f"{', '.join(stuck)} — they reappear in the tray next session[/]"
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
    if harness is not None:
        harness.stop()  # 진행 중 감사를 세운다 (v10.42.0) — 팀원 종료와 같은 자리
    if jobs is not None:
        # 우리가 띄운 자식이다 — 고아로 남기지 않는다 (v10.47.0). 죽인 것은 적는다.
        killed = jobs.stop_all()
        if killed:
            from agent_cli.render import C, console

            names = ", ".join(f"#{j.id} pid {j.pid}" for j in killed)
            console.print(
                f"[{C['muted']}]🧵 Session ended — stopped {len(killed)} running "
                f"background job(s) ({names})[/]"
            )
    if agent_registry is not None:
        agent_registry.shutdown_all()
    # 🐍 python 워커 (v10.66.0) — 우리가 띄운 자식이다. 훅 러너처럼 전역을 푼다.
    from agent_cli.pyworker import get_py_registry, set_py_registry

    py = get_py_registry()
    if py is not None:
        py.stop_all()
        set_py_registry(None)
    from agent_cli.hooks.runtime import set_hook_runner

    set_hook_runner(None)  # 세션 훅 러너 해제 (v10.35.0)

    from agent_cli.render import render_spinner_stop

    render_spinner_stop()
    from agent_cli.browser import reset_browser

    reset_browser()  # 세션 브라우저 종료 (없으면 no-op)
    if mcp_manager:
        mcp_manager.disconnect_all()
    if session is not None:
        from agent_cli.context.session import finalize_session

        finalize_session(session, ctx)


# ── 루프 포트 빌더 — 조립 지점 다섯의 단일 정의 (DESIGN.md §4.4) ──────
#
# 종전엔 다섯 곳이 각자 부분집합을 손으로 나열했고, 포트 여덟 중 일곱이
# 어딘가에선 빠져 있었다 — 그게 의도인지 사고인지 코드 어디에도 없었다.
# 여기 모아 두면 §2.2 의 표가 **코드가 되고**, 나란히 비교된다.
#
# ``LoopPorts`` 에 기본값이 없으므로 새 포트를 더하면 **아래 다섯이 전부
# 즉시 안 만들어진다** — 호스트마다 "연결" 또는 "이래서 미연결" 중 하나를
# 쓸 수밖에 없다. 사유 문자열은 **문서**지 검증물이 아니다(진위는 아무도
# 못 잡는다). 그래서 확인된 사실만 적고, 확인 못 한 것은 "종전 배선
# 유지" 로 적어 둔다 — 지어낸 근거보다 낫다.

_WEB_ONLY = "main 루프 전용 — 입력 큐가 있는 호스트(run·web)에만 있다"
_NO_INBOX = "받은편지함이 없다 — main 은 웹 큐·메일박스, 일회성 루프는 받을 상대가 없다"
_NO_HOOK_RUNNER = (
    "세션 부트스트랩(run/web 의 build_hook_runner)을 거치지 않은 직접 호출 경로"
)


def _hook_runner_port():
    """네 포트 빌더가 같은 세션 러너를 받는다 (v10.35.0)."""
    from agent_cli.hooks.runtime import get_hook_runner

    return get_hook_runner()


def _hook_unwired() -> dict:
    """러너가 없을 때만 `unwired` 에 사유를 싣는다 — 있으면 배선된 포트다."""
    return {} if _hook_runner_port() is not None else {"hook_runner": _NO_HOOK_RUNNER}


_NO_REGISTRY_IN_SUBLOOP = (
    "서브루프에 레지스트리가 닿으면 안 된다 — 'teammate 안 teammate 금지'의 "
    "단일 가드가 LoopConfig.agent_registry 다"
)
_AS_BEFORE = "종전 배선 유지 — 이 호스트는 이 포트를 받은 적이 없다"
_RESIDENT_ROSTER = "상주 전용 — 동료 로스터는 상주 런의 꼬리에만 실린다 (v10.1.0)"


def wake_sources(owner: str) -> list[str]:
    """이 소유자를 **깨울 수 있는 것** — `wait` 도구의 판정 (v10.65.0, docs/wait/DESIGN.md).

    돌고 있는 백그라운드 작업·살아 있는 모니터는 소유자 것만, 에이전트 활동은
    **요청자별**(`AgentRegistry.pending_for` — 세션 전체 `has_active_work` 로 보면
    상주 에이전트 자신이 busy 라 늘 참이 된다), 예약은 세션 것. 사람이 읽을 한 줄씩."""
    out: list[str] = []
    from agent_cli.jobs import get_job_registry

    jobs = get_job_registry()
    if jobs is not None:
        for j in jobs.running(owner):
            out.append(f"job #{j.id} (`{j.command[:60]}`)")
    from agent_cli.monitor.runtime import get_monitor_registry

    mons = get_monitor_registry()
    if mons is not None:
        for m in mons.list_all():
            if m.owner == owner:
                out.append(f"monitor {m.id}")
    from agent_cli.subagent.agents_live import main_registry

    agents = main_registry()
    if agents is not None:
        out.extend(agents.pending_for(owner))
    from agent_cli.schedule.runtime import get_schedule_registry

    sched = get_schedule_registry()
    if sched is not None and sched.has_active_work():
        out.append("a schedule")
    return out


def session_has_live_work(
    *, pending_count: int, monitors=None, schedules=None, harness=None, jobs=None
) -> bool:
    """세션이 아직 할 일을 들고 있는가 — 큐·모니터·예약·하니스 작업 (v10.32.1).

    `run` 의 큐 펌프 정지 판정(`pump.QuietPolicy.should_stop`)과 `web` 의 idle
    self-reap 술어(`web_instance_is_active`)가 **같은 항을 같은 함수로** 본다.
    종전엔 모니터(v9.11.0)·예약(v10.12.0)이 붙을 때마다 두 곳에 같은 한 줄을
    넣어야 했고, 초판 모니터는 web 쪽 한 줄을 빠뜨려 뷰어 없는 인스턴스가
    살아 있는 모니터를 데리고 자기를 거뒀다.

    에이전트 항은 **일부러 넣지 않는다** — 둘의 뜻이 다르다. run 은 미배달
    회신을 배달하고 끝나야 하므로 `has_active_work()`(미배달 포함), web 은
    reap 돼도 resume 이 `agents.json` 에서 회신을 복원하므로 `any_activity()`
    (미배달 제외, 열린 사람 질문 포함). 한 함수에 넣으면 플래그가 생긴다.

    run 의 예약 항은 `QuietPolicy` 가 한 번 알리는 대기 안내와 함께 든다.

    ``harness`` (v10.42.0): 진행 중인 하니스 작업(감사)은 결과를 main 메일함에
    넣어야 끝나므로 같이 기다린다 — 감사 결과가 배달되기 전에 run 이 꺼지거나
    web 이 자기를 거두면 그 결과는 사라진다.
    """
    if pending_count:
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


def ports_for_main(
    *,
    agent_registry,
    mcp_manager,
    dequeue_user_message,
    route_message,
) -> LoopPorts:
    """main 루프의 턴 — run 과 web 이 같다 (v10.33.0, docs/pump/DESIGN.md).

    종전엔 ``ports_for_run``(주입 없음)과 ``ports_for_web``(주입 있음)이
    갈렸다. 세션 펌프가 하나가 되면서 run 도 런 도중 주입을 받는다 — 예약
    발화가 다음 턴 경계에 요청으로 합류하고, 합성 깨우기는 ``SessionPump.
    route`` 가 소비한다. ``route_message`` 는 펌프의 메서드라 세션 수명이다.
    """
    return LoopPorts(
        owner="main",
        questions=_main_questions(agent_registry),
        agent_registry=agent_registry,
        mcp_manager=mcp_manager,
        dequeue_user_message=dequeue_user_message,
        route_message=route_message,
        message_handler=None,
        hook_runner=_hook_runner_port(),
        absorb_inbox=None,
        peer_roster=None,
        unwired={
            "message_handler": "상주 에이전트 전용 — main 은 agent 도구로 보낸다",
            **_hook_unwired(),
            "absorb_inbox": _NO_INBOX,
            "peer_roster": _RESIDENT_ROSTER,
        },
    )


def ports_for_skill(*, agent_registry, owner: str) -> LoopPorts:
    """스킬 실행 루프.

    ``owner`` 는 **부모에게서 물려받는다** — 상주 에이전트 안에서 돈 스킬이
    건 모니터는 그 에이전트에게 보고해야 한다. 기본값을 두지 않는 이유는
    그 상속이 빠지면 조용히 main 으로 가기 때문이다.
    """
    return LoopPorts(
        owner=owner,
        agent_registry=agent_registry,
        questions=None,
        mcp_manager=None,
        message_handler=None,
        hook_runner=_hook_runner_port(),
        route_message=None,
        dequeue_user_message=None,
        absorb_inbox=None,
        peer_roster=None,
        unwired={
            "questions": _AS_BEFORE,
            "mcp_manager": _AS_BEFORE,
            "message_handler": _AS_BEFORE,
            **_hook_unwired(),
            "route_message": _WEB_ONLY,
            "dequeue_user_message": _WEB_ONLY,
            "absorb_inbox": _NO_INBOX,
            "peer_roster": _RESIDENT_ROSTER,
        },
    )


def ports_for_oneshot(*, owner: str) -> LoopPorts:
    """one-shot delegate — 포트 없음. ``owner`` 는 부모에게서 물려받는다."""
    return LoopPorts(
        owner=owner,
        questions=None,
        agent_registry=None,
        mcp_manager=None,
        message_handler=None,
        hook_runner=_hook_runner_port(),
        route_message=None,
        dequeue_user_message=None,
        absorb_inbox=None,
        peer_roster=None,
        unwired={
            "agent_registry": _NO_REGISTRY_IN_SUBLOOP,
            "questions": _AS_BEFORE,
            "mcp_manager": _AS_BEFORE,
            "message_handler": "one-shot 은 상주가 아니다 — 받을 상대가 없다",
            **_hook_unwired(),
            "route_message": _WEB_ONLY,
            "dequeue_user_message": _WEB_ONLY,
            "absorb_inbox": _NO_INBOX,
            "peer_roster": _RESIDENT_ROSTER,
        },
    )


def ports_for_resident(
    *, key: str, message_handler, questions, absorb_inbox, peer_roster
) -> LoopPorts:
    """상주 서브에이전트의 런. ``absorb_inbox`` (v9.25.0) 가 턴 경계마다
    받은편지함을 이 런으로 흡수하고, ``peer_roster`` (v10.1.0) 가 매 턴 꼬리에
    실을 동료 로스터를 돌려준다."""
    return LoopPorts(
        owner=f"agent:{key}",
        message_handler=message_handler,
        questions=questions,
        absorb_inbox=absorb_inbox,
        peer_roster=peer_roster,
        agent_registry=None,
        mcp_manager=None,
        hook_runner=_hook_runner_port(),
        route_message=None,
        dequeue_user_message=None,
        unwired={
            "agent_registry": _NO_REGISTRY_IN_SUBLOOP,
            "mcp_manager": _AS_BEFORE,
            **_hook_unwired(),
            "route_message": _WEB_ONLY,
            "dequeue_user_message": _WEB_ONLY,
        },
    )
