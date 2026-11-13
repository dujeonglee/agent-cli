"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_cli.loop.ledger import Ledger

if TYPE_CHECKING:
    from agent_cli.loop.kind import LoopKind

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
    """AgentLoop 이 런 시작 때 **계산한 것**의 자리 (v10.133.0) — 종류(`LoopKind`)가 든 값은
    여기 복사하지 않는다. 읽는 쪽은 종류에서 직접 읽는다: 설정은 ``cfg.kind.depth``, 포트는
    ``cfg.kind.ports.hook_runner`` 처럼 **종전과 같은 출처**에서.

    남은 셋은 런 시작 때의 환경(깊이·스냅샷·브라우저·스킬·모델의 비전 능력)으로 한 번 계산한
    결과다. 종류는 세션 수명(main)이라 거기 둘 수 없고, 읽을 때마다 다시 계산하면 한 턴 안에서
    프롬프트·함수 스키마(`llm.py`)와 "없는 도구" 판정(`dispatch.py`)이 다른 목록을 볼 수 있다.
    frozen 이므로 협력자/스레드 간 공유 안전(컨테이너의 내용 불변은 관례 — ``__init__`` 확정 후
    아무도 mutate 하지 않는다).

    C1(Option 3, PR-1)의 협력 객체(SystemPromptSvc/ToolBridge/LLMCaller/TurnDispatcher)가 이
    객체를 읽기 전용으로 주입받는다.
    """

    #: 이 루프의 종류 (`loop.kind.LoopKind`, v10.88.0) — 정체·설정·포트를 든다. 엔진이 묻는
    #: 것(`wait` 정책, 턴 경계 흡수, 프롬프트 윤곽, 꼬리 로스터, 렌더 여부)도 여기에.
    kind: LoopKind
    #: 게이트(깊이·자원·짝 탑재·지연)를 거친 도구 목록 — 프롬프트·스키마·디스패치가 같은 것을 본다.
    tools_list: list
    #: 들어온 스택 + 자기 이름 (재귀 방지).
    skill_stack: list
    agent_stack: list

    # ── 바인딩을 읽는 속성 — 저장하지 않는다 ──
    # 세션의 모델 바인딩(v10.43.0, providers/binding)은 종류의 것이다. 값을 복사해 두지 않으므로
    # `binding.switch()` 가 바꾼 모델은 다음 LLM 호출부터 실린다.
    @property
    def model(self) -> str:
        b = self.kind.binding
        return b.model if b is not None else ""

    @property
    def capabilities(self):
        b = self.kind.binding
        return b.capabilities if b is not None else None

    @property
    def dialect(self):
        b = self.kind.binding
        return b.dialect if b is not None else None

    @property
    def provider(self):
        b = self.kind.binding
        return b.provider if b is not None else None

    @property
    def provider_name(self) -> str:
        b = self.kind.binding
        return b.provider_name if b is not None else "openai"


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
    #: 이 런의 열린 항목 장부 — 사용자 요청·되묻기·독촉·빚 카운터 (P1, v10.54.0,
    #: `loop/ledger.py`). 종전엔 필드 넷(`run_requests`·`answers_prompted`·
    #: `requests_nagged`·`debt_nags`)이 여기 흩어져 있었다.
    ledger: Ledger = field(default_factory=Ledger)
    interrupted: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)
    #: 런을 멈춘 사람의 결정에 붙는 사실 한 줄 (v10.120.0) — 디스패처(StaleGuard 의 Stop)가
    #: 쓰고 엔진의 `_on_interrupt` 가 결과와 화면에 싣는다. 보통 Stop 은 빈 값.
    stop_note: str = ""
