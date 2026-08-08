"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from agent_cli.constants import (
    AGENT_DEFAULT_TIMEOUT,
)

# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.
# Loop-control sentinels: distinct from None (failure) and str (answer).
# 모듈 상수 (C1 PR-3) — AgentLoop._execute_turn 과 TurnDispatcher 가 공유.
_CONTINUE = object()  # keep looping
_NOT_HANDLED = object()  # dispatch 헬퍼: 이 분기가 처리 안 함 → 폴스루
_RETRY = object()  # overflow retry


@dataclass(frozen=True)
class LoopConfig:
    """AgentLoop 의 불변 배선 — ``__init__`` 에서 1회 조립되는 세션-수명 설정.

    C1(Option 3, PR-1): god-object 의 ~40개 ``self.*`` 중 실측상 "생성 후
    아무도 재할당하지 않는" 설정군을 한 객체로 격리. PR-2/PR-3 에서 승격되는
    협력 객체(SystemPromptSvc/ToolBridge/LLMCaller/TurnDispatcher)들은 이
    객체를 읽기 전용으로 주입받는다 — 각자가 ``self.model`` 류를 직접 헤집는
    무경계 공유를 구조적으로 차단하는 것이 목적. frozen 이므로 협력자/스레드
    간 공유 안전. (컨테이너 필드의 내용 불변은 관례로 지킨다 — ``tools_list``
    등은 ``__init__`` 확정 후 아무도 mutate 하지 않음을 전제.)
    """

    model: str = ""
    provider_name: str = "openai"
    base_url: str = ""
    api_key: str = ""
    depth: int = 0
    max_depth: int = 2
    max_turns: int = 0
    agent_timeout: int = AGENT_DEFAULT_TIMEOUT
    tools_list: list = field(default_factory=list)
    skill_name: str = ""
    skill_args: str = ""
    skill_stack: list = field(default_factory=list)
    agent_stack: list = field(default_factory=list)
    capabilities: object = None
    wire_format: object = None
    mcp_manager: object = None
    hook_runner: object = None
    hooks_config: dict | None = None
    session: object = None
    agent_role: str = ""
    graceful_interrupt: bool = False
    compaction_enabled: bool = True
    verbose: bool = False
    # teammate P1: 상주 에이전트 레지스트리 — main 부트스트랩만 주입.
    # None(서브에이전트/headless)이면 AgentLoop.__init__ 이 teammate 도구를
    # tools_list 에서 제거한다 (teammate 안 teammate 금지의 단일 가드).
    agent_registry: object = None
    # teammate P2: ask 라우팅 훅 — teammate 서브루프에서만 주입.
    # 있으면 ask 도구가 사용자 프롬프트 대신 이 callable(question)->answer
    # 로 간다 (worker 가 질문을 main mailbox 에 올리고 답변을 블록 대기).
    ask_handler: object = None
    # v5.11: 에이전트↔에이전트 메시징 훅 — 상주 서브루프에서만 주입.
    # 있으면 ``message`` 도구가 이 callable(to, text)->confirmation 으로
    # 라우팅되고, __init__ 이 message 도구를 tools_list 에 강제 탑재한다.
    message_handler: object = None
    # v5.11: 상주 에이전트에 주입되는 미리 만든 ``## Live Agents`` 로스터
    # 문자열(자기 제외) — registry 자체는 안 넘기고(상주 모드 차단 유지)
    # 프롬프트 가시성만 준다.
    peer_agents_section: str = ""
    # A1(v7.29.0): 이 루프가 처리 중인 **사용자 턴의 id** (``t1``, ``t2``...).
    # 직렬 모드에서는 항상 "" — 턴이 하나뿐이라 구분할 대상이 없다.
    # 병렬 모드에서만 TurnRegistry 가 발급한 값이 들어오며, 두 곳이 읽는다:
    #   ① 상주 에이전트 request 에 실려 회신이 **요청한 턴으로** 돌아오게 한다
    #      (없으면 먼저 턴 경계에 도달한 아무 턴이 남의 회신을 가져간다).
    #   ② 회신 회수 시 자기 몫만 걸러낸다 (``_deliver_agent_mail``).
    origin_turn: str = ""
    # v7.30: 턴 스코핑 — 공유 트랜스크립트에서 이 턴이 **어느 요청을 수행
    # 중인지**를 시스템 프롬프트에 못 박는다. 병렬 계약에서만 의미가 있고
    # (``origin_turn`` 이 빈 직렬 모드에서는 동시 요청이 애초에 없다),
    # 기본 on 이다 — 효과가 측정됐다: 라이브 80 턴 중 56 이 남의 파일을
    # 쓰던 것이 스코핑으로 80 중 0 이 됐고, 자기 과제 완수는 오히려 올랐다
    # (bench n3c). 절제 팔은 ``--no-turn-scoping`` 으로 명시적으로 끈다.
    # 목적은 구조적 귀속(이미 정확하다)이 아니라 **의미론적** 혼선, 즉
    # 모델이 남의 동시 질문에 답해 버리는 현상의 완화다.
    turn_scoping: bool = True


@dataclass
class LoopState:
    """AgentLoop 의 per-run 가변 공유 상태 — 실측상 여러 클러스터가 함께
    읽고 쓰는 필드는 정확히 이 6종(+query 정체성 2종)뿐이다.

    C1(Option 3, PR-1): 협력 객체들이 이 단일 인스턴스를 참조 공유한다 —
    "무엇이 진짜 공유 상태인가"를 타입으로 못박아, 이후 추가되는 상태가
    아무 데나 ``self.X`` 로 스며드는 것을 막는다. 여기 없는 가변 필드는
    한 클러스터의 전유물이며 그 소유 객체(PR-2/3)로 이동한다.
    """

    query: str = ""
    query_author: str | None = None
    messages: list = field(default_factory=list)
    turn: int = 0
    task_log: list = field(default_factory=list)
    interrupted: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)
