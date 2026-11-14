"""루프에 주입되는 **능력 포트** 한 묶음 (docs/wiring/DESIGN.md §4).

## 왜 있나

`run_loop` 은 39개 파라미터를 받았고, 그중 8개가 "주입되는 협력자" 였다.
같은 사실이 세 곳(`run_loop` 시그니처 · `LoopConfig` 필드 ·
`handler_resources` dict)에 손으로 적혀 있었고, 조립 지점 다섯이 각자 **다른
부분집합**을 넘겼다 — 포트 여덟(v9.25.0 부터 아홉) 중 일곱이 어딘가에선 빠져 있었고 그게
의도인지 사고인지 코드 어디에도 안 적혀 있었다.

그 배선 누락이 릴리스 두 개를 깨뜨렸다(`monitor_registry` 미수용 → 첫 LLM
턴에서 `TypeError`). 조용한 형태는 더 나빴다 — waker 술어에 항 하나가 빠져
모니터가 발화해도 유휴 main 이 안 깼는데, 4000개 넘는 테스트가 초록이었다.

## 기구 — 기본값을 두지 않는다

필드에 **기본값이 없다**. 조립 지점이 포트 하나를 빠뜨리면 그 자리에서
`TypeError` 다. `abstractmethod` 와 같은 강제력을, 메서드 45개(포트 9 ×
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

from agent_cli.session_slots import get_hook_runner


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
    mcp_manager: Any
    hook_runner: Any
    route_message: Any
    dequeue_user_message: Any
    #: main 전용 (v10.120.0): ``(text, nickname) -> item`` — 입력 큐에 사람 요청 하나를
    #: 넣는다. 사람이 입력창에 친 메시지와 같은 길(다음 턴 경계 흡수 → ``u-N`` 장부)로
    #: 가야 하는 하니스 쪽 입력이 쓴다: 제자리 고리 확인(StaleGuard)의 Hint.
    enqueue_user_message: Any
    #: 상주 에이전트 전용 (v9.25.0): 턴 경계에서 받은편지함을 비워
    #: ``[{"record": …} | {"user_request": …}]`` 로 돌려주는 콜러블. 런 도중
    #: 도착한 요청이 다음 런까지 기다리지 않게 한다 — main 이 ``dequeue_user_
    #: message``/메일박스로 하는 일의 상주판. 장부·질문·영속은 레지스트리 쪽이
    #: 한다(서브루프는 레지스트리에 닿지 않는다).
    absorb_inbox: Any
    #: 상주 에이전트 전용 (v10.1.0): 매 턴 꼬리에 실을 **동료 로스터**를 돌려주는
    #: 콜러블(``() -> str``). 종전엔 로스터가 런 시작에 한 번 시스템 프롬프트에
    #: 박혀, 런 도중 spawn 된 동료를 그 런이 끝날 때까지 못 봤다(실측 cnr60g:
    #: 2번이 1분 반 동안 "3번이 없다" 고 main 에 키를 물었다). main 이 레지스트리로
    #: 꼬리 로스터를 만드는 것의 상주판 — 레지스트리 자체는 여전히 안 넘긴다.
    peer_roster: Any
    #: 우편 창구 (v10.133.27) — 이 주소에 묶인 `wake.Mailbox`. 다섯 종류 모두 값이 있다
    #: (레지스트리가 없는 세션이면 그 안이 None 일 뿐 — 미연결이 아니다). 권한인
    #: ``agent_registry`` 와 갈라 둔다: 깨우기 판정·체크인·제자리 기다림은 모든 루프의 일이다.
    #: 엔진은 우편 일을 이것으로만 한다 — 전역 에이전트 레지스트리를 읽지 않는다(v10.133.28,
    #: 가드 `tests/test_mailbox.py`).
    mailbox: Any

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

    def wakeable(self) -> bool:
        """이 루프를 **깨울 길이 있는가** (v10.65.0, `wait` 의 전제) — main 은 입력
        큐(``dequeue_user_message``), 상주는 받은편지함(``absorb_inbox``). 스킬·
        일회성 루프는 둘 다 없다: 결과가 호출자에게 돌아가는 인라인 런이라 끝나면
        그만이고, `wait` 로 끝낸 결과는 호출자에게 "기다리는 중" 으로 돌아가 버린다."""
        return self.dequeue_user_message is not None or self.absorb_inbox is not None

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


# ── 루프 포트 — 종류마다 `make_ports` (v10.87.0 LoopKind; 종전 `ports_for_*` 넷) ──
#
# 종전엔 다섯 곳이 각자 부분집합을 손으로 나열했고, 포트 여덟 중 일곱이
# 어딘가에선 빠져 있었다 — 그게 의도인지 사고인지 코드 어디에도 없었다.
# 지금은 종류 객체(`MainLoop`·`PrivateLoop`·`ResidentLoop`·`SkillLoop`)의
# `make_ports` 가 그 자리다; 사유 상수·훅 포트 헬퍼는 여기 있어 종류들이 쓴다.
#
# ``LoopPorts`` 에 기본값이 없으므로 새 포트를 더하면 **네 `make_ports` 가 전부
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
    """모든 종류의 `make_ports` 가 같은 세션 러너를 받는다 (v10.35.0)."""
    return get_hook_runner()


def _hook_unwired() -> dict:
    """러너가 없을 때만 `unwired` 에 사유를 싣는다 — 있으면 배선된 포트다."""
    return {} if _hook_runner_port() is not None else {"hook_runner": _NO_HOOK_RUNNER}


_NO_REGISTRY_IN_SUBLOOP = (
    "서브루프에 레지스트리가 닿으면 안 된다 — 'teammate 안 teammate 금지'의 "
    "단일 가드가 포트의 agent_registry 다"
)
_AS_BEFORE = "종전 배선 유지 — 이 호스트는 이 포트를 받은 적이 없다"
_RESIDENT_ROSTER = "상주 전용 — 동료 로스터는 상주 런의 꼬리에만 실린다 (v10.1.0)"


# ── 세션 자원 — `Tool.requires_handler` 가 보는 것 ──
#
# 엔진이 `handler_resources` 로 포트와 함께 넘긴다. 자원 모듈(`snapshot`·`browser`·`skills`)은
# 함수 안에서 import 한다 — 기동 비용.


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


def skills_resource():
    """`Tool.requires_handler="skills"` 이 보는 자원 — 모델이 부를 수 있는 스킬이 있으면
    True, 없으면(전부 ``disable-model-invocation``·읽기 실패) None → `run_skill` 이 빠진다.
    Skills 절(`build_skill_descriptions`)과 같은 집합을 본다."""
    try:
        from agent_cli.skills import model_skills

        return True if model_skills() else None
    except Exception:
        return None
