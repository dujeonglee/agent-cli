"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

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

    #: 이 루프의 주소 — ``"main"`` | ``"agent:<key>"`` (``LoopPorts.owner``).
    #: 모니터 소유자 라우팅이 `RunContext` 를 거쳐 이걸 읽는다.
    owner: str = "main"
    model: str = ""
    provider_name: str = "openai"
    base_url: str = ""
    api_key: str = ""
    depth: int = 0
    max_depth: int = 2
    max_turns: int = 0
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
    # v5.11: 에이전트↔에이전트 메시징 훅 — 상주 서브루프에서만 주입.
    # 있으면 ``message`` 도구가 이 callable(to, text)->confirmation 으로
    # 라우팅되고, __init__ 이 message 도구를 tools_list 에 강제 탑재한다.
    message_handler: object = None
    # 비동기 ask/answer 의 단일 seam (docs/agent-ask/DESIGN.md §4) —
    # ``QuestionPort``. 레지스트리가 아니라 포트라 위의 "teammate 안
    # teammate 금지" 가드는 그대로다. main 도 받는다(답할 수단이 필요).
    questions: object = None
    # v5.11: 상주 에이전트에 주입되는 미리 만든 ``## Live Agents`` 로스터
    # 문자열(자기 제외) — registry 자체는 안 넘기고(상주 모드 차단 유지)
    # 프롬프트 가시성만 준다.
    peer_agents_section: str = ""


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
    #: 이 런이 응답해야 할 **사용자 요청들** — ``{"id", "author", "text"}``.
    #: 런 스타터 1건 + 턴 경계 drain 으로 들어온 N건. 웹 큐가 이미 id 를
    #: 발급하는데(`enqueue` → `{id, …}`, `cancel_pending` 이 쓴다) 루프까지
    #: 오면서 버려지고 있었다. 그래서 drain-all 이 요청 셋을 한 턴에 합쳐도
    #: **무엇이 답해졌는지**는 아무도 몰랐다 — `run_authors` 는 *누가* 물었는지
    #: 만 안다. CLI 는 요청이 하나뿐이라 비어 있다.
    run_requests: list = field(default_factory=list)
    #: 이 런에서 `answers` 를 이미 한 번 되물었나. 되묻기는 **한 번**이다 —
    #: 끝내 안 실으면 받아주고 "미신고" 로 적는다. 무한히 되물으면 고집 센
    #: 모델이 런을 태운다.
    answers_prompted: bool = False
    #: 이미 독촉한 요청 id — 한 요청당 **한 번**이다. `complete` 이 미답을
    #: 남기면 최종답은 내보내되 루프를 닫지 않고 남은 요청을 가리킨다
    #: (붙잡기가 아니다 — 결과는 이미 나갔다). 상한이 없으면 고집 센 모델과
    #: 물려 런을 태운다.
    requests_nagged: set = field(default_factory=set)
    #: 상주 에이전트: 요청자에게 빚진 `reply`/`message` 없이 `complete` 하려
    #: 해서 독촉한 횟수 (v9.21.0). 상한은 `dispatch.MAX_REPLY_NAGS`(3) —
    #: 무제한이면 아무것도 런을 못 멈춘다(개입은 max_turns 미계수, B1 은
    #: 도구 경로에만 있어 반복 complete 을 안 본다). 상한 뒤엔 레지스트리가
    #: 런 요약을 라벨 붙여 폴백 배달한다 — 요청자가 침묵을 받진 않는다.
    debt_nags: int = 0
    #: 이 런이 사용자 요청을 받았는가 (v9.22.1) — 시작 메시지든 턴 경계
    #: 주입이든 `is_user` 인 것이 하나라도. 빚이 남은 `complete` 의 갈림이
    #: **이것**이다(수신 유형 규칙): 사용자 요청이 있는 런은 결과가 사용자에게
    #: 가므로 수락 후 독촉, 없는 런(🤝 웨이크·에이전트 항목)은 거부. 종전엔
    #: main/상주(`port.nonblocking`)로 갈랐는데 그건 이 값의 근사였다 — main
    #: 의 웨이크 런과 상주의 사람 창 런에서 틀린다.
    user_run: bool = False
    interrupted: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)
