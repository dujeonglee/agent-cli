"""루프에 주입되는 **능력 포트** 한 묶음 (docs/wiring/DESIGN.md §4).

## 왜 있나

`run_loop` 은 39개 파라미터를 받았고, 그중 8개가 "주입되는 협력자" 였다.
같은 사실이 세 곳(`run_loop` 시그니처 · `LoopConfig` 필드 ·
`handler_resources` dict)에 손으로 적혀 있었고, 조립 지점 다섯이 각자 **다른
부분집합**을 넘겼다 — 포트 여덟 중 일곱이 어딘가에선 빠져 있었고 그게
의도인지 사고인지 코드 어디에도 안 적혀 있었다.

그 배선 누락이 릴리스 두 개를 깨뜨렸다(`monitor_registry` 미수용 → 첫 LLM
턴에서 `TypeError`). 조용한 형태는 더 나빴다 — waker 술어에 항 하나가 빠져
모니터가 발화해도 유휴 main 이 안 깼는데, 4000개 넘는 테스트가 초록이었다.

## 기구 — 기본값을 두지 않는다

필드에 **기본값이 없다**. 조립 지점이 포트 하나를 빠뜨리면 그 자리에서
`TypeError` 다. `abstractmethod` 와 같은 강제력을, 메서드 40개(포트 8 ×
조립 5) 없이, 언어가 준다. 새 포트를 여기 추가하면 **다섯 빌더가 전부 즉시
안 만들어진다** — 각 호스트에 대해 "연결" 또는 "이래서 미연결" 중 하나를
쓰도록 강제된다.

`frozen` — 조립 시점에 값이 고정된다(지연 평가가 아니므로 "언제 불리느냐" 가
의미를 갖지 않는다). 협력자·스레드 간 공유 안전.

## 여기 없는 것

- `ctx` — 다섯 호스트가 **전부** 넘기는 기반이고, 협력자 넷에 위치 인자로
  꿰여 있다. "의도적으로 없음" 이 성립하지 않으므로 포트가 아니다.
- `stop_event` — 가변 `LoopState` 필드이고 setter 브리지가 있으며 루프가
  스스로 기본값을 만든다. frozen 배선과 성격이 다르다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any


@dataclass(frozen=True, kw_only=True)
class LoopPorts:
    """조립 지점이 루프에 건네는 능력 포트 전부."""

    #: 이 루프의 주소 — ``"main"`` | ``"agent:<key>"``. 지금은 아무도 읽지
    #: 않는다(모니터 소유자 라우팅이 C2 에서 소비한다). 값을 **먼저** 세워
    #: 두는 이유: C2 가 이걸 220곳에 새로 꿰지 않아도 되게 하려는 것이다.
    owner: str
    questions: Any
    message_handler: Any
    agent_registry: Any
    monitor_registry: Any
    mcp_manager: Any
    hook_runner: Any
    route_message: Any
    dequeue_user_message: Any

    #: 미연결 포트의 **사유** — 이름 → 왜. 값이 `None` 인 포트는 여기
    #: 적혀 있어야 한다(테스트가 강제). 의도적 미연결과 빠뜨린 것이 둘 다
    #: `None` 이던 것이 이 설계가 생긴 이유의 절반이다.
    #:
    #: 사유는 **문서**지 검증물이 아니다 — 문자열의 진위는 아무도 못
    #: 잡는다(설계 2판이 `mcp_manager` 에 거짓 사유를 적었고 통과했다).
    #: 강제되는 것은 "사유가 있다" 뿐이다.
    #:
    #: **빌더의 의도**를 적는 자리지 런타임 존재 여부가 아니다. 예컨대
    #: `mcp_manager` 는 MCP 서버가 설정되지 않은 세션에서 `None` 이지만
    #: 그건 미연결이 아니라 "연결됐고 저쪽에 아무도 없음" 이다 — 사유를
    #: 적지 않는 것이 맞다.
    unwired: Mapping[str, str] = field(default_factory=dict)

    #: ``Tool.requires_handler`` 가 보지 **않는** 필드 — 자원이 아니다.
    _NON_RESOURCE = ("owner", "unwired")

    def handler_resources(self) -> dict[str, Any]:
        """``Tool.requires_handler`` 가 보는 뷰 — 손으로 안 적는다.

        종전엔 `loop/core.py` 안에 3키짜리 dict 리터럴이었다. 파생으로
        바꾸면 새 포트가 도구 마운트 규칙에 **자동으로** 편입된다.
        `ctx` 는 포트가 아니므로 호출부가 얹는다.
        """
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name not in self._NON_RESOURCE
        }
