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


#: ⑯ `wait` 정책 — 런을 끝내고 깨우기를 기다린다 / 제자리에서 막는다 / 거부한다.
WAIT_END_RUN = "end_run"
WAIT_BLOCK = "block"
WAIT_REFUSE = "refuse"


@dataclass(frozen=True)
class PromptProfile:
    """⑨ 프롬프트 윤곽 — 시스템 프롬프트·꼬리·거절 문구가 종류에 따라 달라지는 입력 전부.
    글은 바뀌지 않는다: 같은 입력이 같은 절을 만든다(`build_system_prompt_sections`)."""

    #: `## Role` 본문 — 빈 문자열이면 기본 ROLE_PROMPT
    role: str
    #: 상주의 `## Live Agents` 안내 + Reply Discipline (빈 문자열이면 없음)
    peer_section: str
    #: `agent` 도구를 전부 광고할 레지스트리 — None 이면 run-only 축소 스키마
    agent_registry: Any
    #: `ask` 가 사람 답을 기다리며 막는가 (상주는 질문 포트로 비차단)
    ask_blocking: bool
    #: 종결문이 "호출자에게 보고" 어투인가 (서브루프)
    reports_to_caller: bool
    #: 상주 모드 거절 문구가 `message` 동료 채널을 가리키는가
    peer_channel: bool


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

    @staticmethod
    def make_ports(owner: str, **wired) -> LoopPorts:
        """맨 종류의 포트 — 준 것만 연결, 나머지는 미연결 (`LoopConfig` 를 종류 없이 만든 쪽).
        `LoopPorts` 를 만드는 자리는 종류의 `make_ports` 뿐이다(테스트가 AST 로 고정)."""
        from agent_cli.loop.ports import LoopPorts

        base = {
            "questions": None,
            "message_handler": None,
            "agent_registry": None,
            "mcp_manager": None,
            "hook_runner": None,
            "route_message": None,
            "dequeue_user_message": None,
            "absorb_inbox": None,
            "peer_roster": None,
        }
        base.update(wired)
        return LoopPorts(owner=owner, **base)

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
            "ports": self.ports,
            "kind": self,
        }

    # ── B·C 엔진이 묻는 것 (PR 2, v10.88.0) — 기본 구현은 정체(포트·깊이·이름)에서 유도한다.
    # 다섯 종류는 자기 정책을 **명시**해 덮어쓴다; 기본은 `run_loop` 를 직접 부르는 쪽(테스트·
    # 임베더)의 맨 종류(`AgentLoop` 가 만든다)를 위한 것이다.
    def wait_policy(self) -> str:
        """⑯ `wait` 가 런을 끝내나(main·상주), 제자리에서 막나(private), 거부하나(인라인)."""
        from agent_cli.constants import is_private_owner

        if not self.ports.wakeable():
            return WAIT_REFUSE
        return WAIT_BLOCK if is_private_owner(self.owner) else WAIT_END_RUN

    def intake(self) -> list[dict]:
        """⑮ 턴 경계에 흡수할 항목 — 종류가 **어디서 받을지** 정하고 엔진이 넣는다.
        모양: ``{"message": 큐 항목}`` · ``{"mail": 레지스트리 회신}`` · ``{"record": 레코드}`` ·
        ``{"user_request": {id, author, text}}``."""
        items: list[dict] = []
        dequeue = self.ports.dequeue_user_message
        if dequeue is not None:
            while True:
                item = dequeue()
                if not item:
                    break
                items.append({"message": item})
        registry = self.ports.agent_registry
        if registry is not None:
            items.extend({"mail": r} for r in registry.drain_replies())
        absorb = self.ports.absorb_inbox
        if absorb is not None:
            items.extend(absorb() or [])
        return items

    def prompt_profile(self) -> PromptProfile:
        """⑨ 프롬프트 윤곽."""
        q = self.ports.questions
        return PromptProfile(
            role=self.agent_role,
            peer_section=self.peer_agents_section,
            agent_registry=self.ports.agent_registry,
            ask_blocking=not (q is not None and getattr(q, "nonblocking", False)),
            reports_to_caller=self.depth > 0,
            peer_channel=self.ports.message_handler is not None,
        )

    def tail_roster(self, tools: list[str]) -> str:
        """⑩ 꼬리 `## Live Agents` 본문 — ``tools`` 는 엔진 필터 뒤의 도구 목록(`agent` 가
        없는 루프엔 로스터를 광고하지 않는다)."""
        registry = self.ports.agent_registry
        if registry is not None and "agent" in tools:
            from agent_cli.prompts.system_prompt import build_live_agents_section

            return build_live_agents_section(registry, include_state=True)
        roster = self.ports.peer_roster
        if roster is not None:
            try:
                return roster() or ""
            except Exception:
                return ""
        return ""

    def renders(self) -> bool:
        """⑪ 턴·관찰을 렌더러에 그리나 — 스킬만 안 그린다(카드만)."""
        return not self.skill_name

    @property
    def tracks_run_authors(self) -> bool:
        """사용자 귀속(`run_authors`)을 렌더러·레지스트리에 비추나 — 입력 큐가 있는 main 만."""
        return self.ports.dequeue_user_message is not None

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
        from agent_cli.runtime import release_owner
        from agent_cli.subagent.agents_live import main_registry

        return release_owner(self.owner, registry=main_registry()).note

    # ── 엔진이 묻는 것 — private 종류의 명시 정책 ──
    def wait_policy(self) -> str:
        return WAIT_BLOCK  # 런이 곧 함수 호출 — 메일함에 통지가 올 때까지 여기서 막는다

    def intake(self) -> list[dict]:
        return list(self.ports.absorb_inbox() or [])  # 자기 메일함 → `mail_record`

    def prompt_profile(self) -> PromptProfile:
        return PromptProfile(
            role=self.agent_role,
            peer_section="",
            agent_registry=None,  # run-only 축소 스키마
            ask_blocking=True,
            reports_to_caller=True,
            peer_channel=False,
        )

    def tail_roster(self, tools: list[str]) -> str:
        return ""

    def renders(self) -> bool:
        return True

    @property
    def tracks_run_authors(self) -> bool:
        return False
