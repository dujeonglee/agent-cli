"""루프 종류 (`LoopKind`) — 공용 엔진 앞뒤의 가변부를 한 객체로 (v10.87.0, docs/loop-kinds/DESIGN.md).

엔진(`AgentLoop` 와 협력자들)은 하나인데 들어가는 길과 나오는 길이 다섯이었다 — main ·
상주 · one-shot · 스킬 · 하니스 작업. 각자가 주소·포트·ctx·스코프·결과 포장·정리를 자기
모듈에서 손으로 조립했고(열 곳이 중복), 엔진은 종류를 플래그 여덟 개로 추측했다. 여기
기본 틀은 그 가변부에 이름을 붙인다: **종류가 결정하고 엔진이 돈다.**

- ① ``owner`` ② ``depth`` ③ ``agent_stack``·``skill_stack`` ④ ``binding`` ⑤ ``tools``
  ⑥ ``hooks_config`` ⑦ ``ports`` ⑧ ``stop_event`` — 생성자에서 확정되는 정체.
- ⑬ ``make_ctx`` ⑭ ``open_scope``/``close_scope`` ⑰ ``finish`` ⑱ ``release`` — 런의 단계.
  ``run()`` 이 그 순서를 한 번 적는다(템플릿).
- ⑨–⑫·⑮·⑯(프롬프트 윤곽·꼬리 로스터·렌더·시작·흡수·wait 정책)은 PR 2 에서 엔진이 종류에게
  묻는다 — PR 1 의 엔진은 종전 플래그를 그대로 읽는다.

다섯 구현은 각자의 영역에 있다: `runtime.MainLoop` · `subagent.resident_loop.ResidentLoop` ·
`subagent.oneshot.OneShotLoop` · `skills.executor.SkillLoop` · `harness.jobs.HarnessLoop`.
스레드와 수명(펌프·워커·작업 스레드·병렬 one-shot)은 종류 밖 — 호스트의 것이다. 주소는
문자열 그대로라 레지스트리의 라우팅(`deliver`·`fire_checkin`·`arrived_for`)은 바뀌지 않는다.

엔진 호출은 ``engine(query, ctx, **kwargs) -> (ToolResult, secs)`` 한 모양 — 테스트·하니스가
가짜 러너를 꽂는 자리(`AgentRegistry(runner=)`·`HarnessJobs(runner=)`)와 같다. 기본은
``agent_cli.loop.run_loop`` 를 **호출 시점에** 읽으므로 ``patch("agent_cli.loop.run_loop")`` 가
다섯 종류 전부에 닿는다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_cli.loop.ports import LoopPorts


class LoopKindError(Exception):
    """종류가 런을 열 수 없다 — 모델(또는 호스트)에게 보낼 사유 한 줄이 메시지다."""


@dataclass(frozen=True)
class Caller:
    """중첩 루프(one-shot·스킬)가 **호출자**에게서 읽는 것.

    루프 안에서 부르면 ``from_loop(cfg, state, ctx)`` (돌고 있는 루프의 ``LoopConfig`` 가 곧
    호출자의 정체), 루프 밖(``@agent``·``/skill`` 명령)에서 부르면 ``main_session(...)`` —
    두 길이 같은 모양이라 중첩 종류는 호출자가 어디인지 모른다. 기본값이 없다: 하나라도
    빠지면 생성 시 ``TypeError`` — 주소 상속이 조용히 ``"main"`` 으로 새던 사고(docs/wiring)
    의 재발 방지.
    """

    owner: str
    depth: int
    max_depth: int
    max_turns: int
    binding: Any
    #: 호출자의 (엔진 필터 **뒤**) 도구 목록 — ``None`` 은 전부. 스킬은 이 범위로 좁혀진다(D3).
    tools: list[str] | None
    hooks_config: dict | None
    hook_runner: Any
    mcp_manager: Any
    agent_registry: Any
    session: Any
    compaction_enabled: bool
    graceful_interrupt: bool
    verbose: bool
    agent_role: str
    skill_stack: list[str]
    agent_stack: list[str]
    ctx: Any
    stop_event: Any

    @classmethod
    def from_loop(cls, cfg, state, ctx) -> Caller:
        """돌고 있는 루프가 호출자다 (`ToolBridge` 의 `agent`·`TurnDispatcher` 의 `run_skill`)."""
        return cls(
            owner=cfg.owner,
            depth=cfg.depth,
            max_depth=cfg.max_depth,
            max_turns=cfg.max_turns,
            binding=cfg.binding,
            tools=list(cfg.tools_list),
            hooks_config=cfg.hooks_config,
            hook_runner=cfg.hook_runner,
            mcp_manager=cfg.mcp_manager,
            agent_registry=cfg.agent_registry,
            session=cfg.session,
            compaction_enabled=cfg.compaction_enabled,
            graceful_interrupt=cfg.graceful_interrupt,
            verbose=cfg.verbose,
            agent_role=cfg.agent_role,
            skill_stack=list(cfg.skill_stack),
            agent_stack=list(cfg.agent_stack),
            ctx=ctx,
            stop_event=state.stop_event,
        )

    @classmethod
    def main_session(
        cls,
        *,
        binding,
        ctx,
        session,
        max_turns: int = 0,
        max_depth: int = 2,
        hooks_config: dict | None = None,
        stop_event=None,
        graceful_interrupt: bool = False,
        verbose: bool = False,
        agent_registry=None,
        hook_runner=None,
        mcp_manager=None,
    ) -> Caller:
        """루프 **밖**의 디스패치(`@agent`·`/skill`) — 감싸는 루프가 없으니 main 세션이 호출자다."""
        return cls(
            owner="main",
            depth=0,
            max_depth=max_depth,
            max_turns=max_turns,
            binding=binding,
            tools=None,
            hooks_config=hooks_config,
            hook_runner=hook_runner,
            mcp_manager=mcp_manager,
            agent_registry=agent_registry,
            session=session,
            compaction_enabled=True,
            graceful_interrupt=graceful_interrupt,
            verbose=verbose,
            agent_role="",
            skill_stack=[],
            agent_stack=[],
            ctx=ctx,
            stop_event=stop_event,
        )


@dataclass
class Start:
    """런을 여는 것 — 시작 메시지와 그 귀속. 종류가 ``start(...)`` 로 만들거나(스킬은 템플릿
    치환, 상주는 받은편지함 묶음) 호스트가 넘긴다(main 의 `RunRequest`)."""

    query: str
    images: list[dict] = field(default_factory=list)
    author: str | None = None
    #: 시작 메시지가 사람의 요청인가 — 회계(장부)와 `complete` 의 answers 강제가 이걸 본다.
    author_is_user: bool = False
    request_id: str = ""
    user_requests: list = field(default_factory=list)
    #: main 만 — 런마다 새 Event(펌프). 다른 종류는 자기 ``stop_event`` 를 쓴다.
    stop_event: Any = None


class LoopKind:
    """공용 틀. 서브클래스는 정체(①–⑧)를 ``__init__`` 에서 세우고 단계(⑬⑭⑰⑱)를 덮어쓴다."""

    # ── A. 정체 — 서브클래스 생성자가 세운다 (owner 는 기본값 없음: 빠지면 AttributeError) ──
    owner: str
    depth: int = 0
    max_depth: int = 2
    max_turns: int = 0
    binding: Any = None
    tools: list[str] | None = None
    hooks_config: dict | None = None
    ports: LoopPorts
    stop_event: Any = None
    agent_stack: list[str] = ()  # type: ignore[assignment]
    skill_stack: list[str] = ()  # type: ignore[assignment]
    agent_name: str = ""
    skill_name: str = ""
    skill_args: str = ""
    agent_role: str = ""
    peer_agents_section: str = ""
    session: Any = None
    compaction_enabled: bool = True
    graceful_interrupt: bool = False
    verbose: bool = False
    record_turns: bool = True
    #: ``(query, ctx, **kwargs) -> (ToolResult, secs)`` — ``None`` 이면 `run_loop`.
    engine: Callable | None = None
    ctx: Any = None

    # ── 템플릿 ──
    def start(self, *args, **kwargs) -> Start:
        """종류가 스스로 시작을 만든다(스킬·one-shot·하니스·상주). main 은 호스트가 넘긴다."""
        raise NotImplementedError(f"{type(self).__name__} needs an explicit Start")

    def run(self, start: Start | None = None):
        """스코프 → ctx → 엔진 → 정리 → 결과 포장 → 스코프 닫기. 반환 모양은 종류의 것
        (`finish` 가 정한다): one-shot·스킬은 `ToolResult`, 상주는 ``(ToolResult, secs)``,
        하니스는 ``(ok, output)``, main 은 엔진 결과 그대로."""
        if start is None:
            start = self.start()
        handle = self.open_scope(start)
        t0 = time.monotonic()
        finished = None
        try:
            try:
                self.ctx = self.make_ctx(start)
            except LoopKindError as e:
                finished = self.rejected(str(e))
                return finished
            result = None
            try:
                result, _secs = self._engine()(
                    start.query, self.ctx, **self.loop_kwargs(start)
                )
            finally:
                released = self.release()
            finished = self.finish(result, time.monotonic() - t0, released)
            return finished
        except Exception as exc:
            finished = self.crashed(exc, time.monotonic() - t0)
            return finished
        finally:
            self.close_scope(handle, finished, time.monotonic() - t0)

    def loop_kwargs(self, start: Start) -> dict:
        """엔진(`run_loop`)에 넘기는 전부 — 종류 하나가 한 벌을 적는다. 종전엔
        `run_loop`·`AgentLoop.__init__`·`run_subagent_message` 가 같은 30여 인자를 세 번 나열했다."""
        return {
            "query_images": list(start.images),
            "query_author": start.author,
            "query_author_is_user": start.author_is_user,
            "query_request_id": start.request_id,
            "user_requests": list(start.user_requests),
            "stop_event": (
                start.stop_event if start.stop_event is not None else self.stop_event
            ),
            "graceful_interrupt": self.graceful_interrupt,
            "binding": self.binding,
            "max_turns": self.max_turns,
            "verbose": self.verbose,
            "depth": self.depth,
            "max_depth": self.max_depth,
            "active_tools": self.tools,
            "session": self.session,
            "hooks_config": self.hooks_config,
            "skill_name": self.skill_name,
            "skill_args": self.skill_args,
            "skill_stack": list(self.skill_stack),
            "agent_stack": list(self.agent_stack),
            "agent_name": self.agent_name,
            "agent_role": self.agent_role,
            "record_turns": self.record_turns,
            "compaction_enabled": self.compaction_enabled,
            "peer_agents_section": self.peer_agents_section,
            "ports": self.ports,
        }

    def _engine(self) -> Callable:
        return self.engine if self.engine is not None else self._run_loop

    @staticmethod
    def _run_loop(query: str, ctx, **kwargs):
        """기본 엔진 — ``agent_cli.loop.run_loop`` 를 호출 시점에 읽는다(패치 가능)."""
        from agent_cli import loop as loop_pkg

        t0 = time.monotonic()
        return loop_pkg.run_loop(query=query, ctx=ctx, **kwargs), time.monotonic() - t0

    # ── 가변부 — 서브클래스가 덮어쓴다 ──
    def make_ctx(self, start: Start):
        """⑬ 대화 컨텍스트. 기본: 이미 가진 것(main 의 세션 ctx·상주의 에이전트 ctx).
        열 수 없으면 `LoopKindError` — ``rejected`` 가 받는다."""
        return self.ctx

    def open_scope(self, start: Start):
        """⑭ 렌더 스코프를 연다 — 돌려준 핸들이 ``close_scope`` 로 온다. 기본: 없음."""
        return

    def close_scope(self, handle, finished, secs: float) -> None:
        """⑭ 닫는다. ``finished`` 는 `finish`/`rejected`/`crashed` 의 반환(없으면 None)."""

    def finish(self, result, secs: float, released: str):
        """⑰ 엔진 결과를 호스트가 받을 모양으로. 기본: 그대로."""
        return result

    def release(self) -> str:
        """⑱ 이 런이 소유한 것을 거둔다 — 거둔 것을 알리는 한 줄(없으면 "")."""
        return ""

    def rejected(self, reason: str):
        """ctx 를 열 수 없었다. 기본: 호출자에게 던진다."""
        raise LoopKindError(reason)

    def crashed(self, exc: BaseException, secs: float):
        """엔진·정리·포장 중 예외. 기본: 다시 던진다."""
        raise exc


class PrivateLoop(LoopKind):
    """자기 주소(``run:``·``harness:``)를 가진 루프 (v10.84.0, docs/wait/DESIGN.md §7) —
    레지스트리의 private 메일함을 턴 경계에 흡수하므로 깨울 수 있고(`wait` 광고), `wait` 는
    런을 끝내는 대신 메일 도착까지 막는다. 끝나면 잡·모니터·메일함·체크인을 전부 거둔다."""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.ports = self.make_ports(owner)

    @staticmethod
    def make_ports(owner: str) -> LoopPorts:
        from agent_cli.loop.ports import LoopPorts
        from agent_cli.runtime import (
            _AS_BEFORE,
            _NO_REGISTRY_IN_SUBLOOP,
            _RESIDENT_ROSTER,
            _WEB_ONLY,
            _hook_runner_port,
            _hook_unwired,
            _private_inbox,
        )

        return LoopPorts(
            owner=owner,
            questions=None,
            agent_registry=None,
            mcp_manager=None,
            message_handler=None,
            hook_runner=_hook_runner_port(),
            route_message=None,
            dequeue_user_message=None,
            absorb_inbox=_private_inbox(owner),
            peer_roster=None,
            unwired={
                "agent_registry": _NO_REGISTRY_IN_SUBLOOP,
                "questions": _AS_BEFORE,
                "mcp_manager": _AS_BEFORE,
                "message_handler": "one-shot 은 상주가 아니다 — 받을 상대가 없다",
                **_hook_unwired(),
                "route_message": _WEB_ONLY,
                "dequeue_user_message": _WEB_ONLY,
                "peer_roster": _RESIDENT_ROSTER,
            },
        )

    def release(self) -> str:
        from agent_cli.runtime import release_private_owner

        return release_private_owner(self.owner)
