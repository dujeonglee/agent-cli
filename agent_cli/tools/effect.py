"""도구 부수효과 인텐트 — 다중 사용자 병렬 턴(A3 계층 락)의 어휘.

포크(Coagora)가 먼저 구현·실측 검증한 "병렬 추론 + 직렬 부수효과" 계약을
본류로 역병합하는 M1 단계 산출물이다 (docs/research/11-upstream-merge-plan.md
§2 M1). 여기서 정의하는 것은 **분류 어휘뿐** — 실제 직렬화(큐·FIFO·펌프)는
M4 의 효과 락이 이 인텐트를 읽어 수행한다.

왜 도구가 자기 인텐트를 아는가: 기존 :meth:`Tool.touched_paths` 와 같은
소유권 원칙이다 — 도구만이 자기 ``action_input`` 의 shape 를 안다. 분류
지식을 중앙 테이블에 두면 도구 추가 시 두 곳을 고쳐야 하고, 실제로 그
방식이 깨진 전례가 ``touched_paths`` 도입 이유였다.

호환성 행렬(포크 ``sandboxLock.ts:16-22`` 주석 = 검증된 규칙):

    FILE_WRITE/READ(경로 P) ↔ FILE_WRITE/READ(경로 Q≠P) : 병렬
    FILE_WRITE/READ(P)      ↔ FILE_WRITE/READ(P)        : 직렬
    그 외 전부(SHELL/PACKAGE/FILE_DELETE/UNKNOWN_WORKSPACE_EFFECT): 배타
    NON_WORKSPACE_OR_COMPOSITE                            : 이 게이트 대상 아님

``FILE_DELETE`` 가 배타인 이유(포크 ``sandboxLock.ts:20-22``): 삭제는
디렉토리째 지울 수 있어 ``rm -r src/`` 와 ``write src/x.py`` 가 경로 키가
달라 병렬 진입하면 ENOENT 레이스가 **새로** 생긴다. 삭제는 드물어 배타
비용이 거의 없으므로 새 레이스 클래스를 만들지 않는 쪽을 택했다.

경로 정규화는 **여기서 하지 않는다**. 포크도 정규화(``normalizeLockPath``)를
락 모듈이 소유하며, 락 키 동일성 판정은 M4 의 책임이다. ``touched_paths``
가 원본 경로를 그대로 돌려주는 것과 같은 규율 — 인텐트는 도구가 받은
경로를 그대로 싣는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EffectKind(str, Enum):
    """부수효과 종류. ``str`` 혼합이라 JSON 직렬화·로그가 값 그대로 나간다.

    ``FILE_DELETE`` / ``PACKAGE`` 는 계약 어휘로 정의하되 **현재 내장 도구
    중 이 둘을 내는 것은 없다**: 본류에는 파일 삭제 전용 도구가 없고(삭제는
    ``shell`` 의 ``rm`` — 이미 ``SHELL``=배타), 패키지 설치도 ``shell`` 을
    지나간다. 포크의 행렬과 어휘를 일치시켜 M4 락이 두 코드베이스에서 같은
    규칙을 갖도록 남겨둔 것이며, 추측으로 명령 문자열을 파싱해 ``PACKAGE``
    를 만들어내지는 않는다(그 휴리스틱이야말로 오분류의 근원).
    """

    FILE_READ = "FILE_READ"
    FILE_WRITE = "FILE_WRITE"
    FILE_DELETE = "FILE_DELETE"
    SHELL = "SHELL"
    PACKAGE = "PACKAGE"
    # A leaf tool whose workspace effects were not classified.  This is the
    # fail-closed default for newly registered/plugin tools: it must take the
    # workspace-exclusive gate until the tool declares a narrower intent.
    UNKNOWN_WORKSPACE_EFFECT = "UNKNOWN_WORKSPACE_EFFECT"
    # A tool that is explicitly known not to have a leaf workspace effect, or
    # a composite whose children acquire their own gates.  Keeping this
    # separate from the fail-closed default avoids both silent races and the
    # parent/child deadlock that one overloaded UNKNOWN value used to create.
    NON_WORKSPACE_OR_COMPOSITE = "NON_WORKSPACE_OR_COMPOSITE"

    # Backward-compatible spelling for third-party callers. It aliases the
    # safe fail-closed meaning, never the old unlocked behavior.
    UNKNOWN = "UNKNOWN_WORKSPACE_EFFECT"  # noqa: PIE796

@dataclass(frozen=True)
class EffectIntent:
    """한 도구 호출이 일으키는 부수효과의 선언.

    ``path`` 는 ``FILE_*`` 종류에서만 의미가 있고, 나머지에서는 빈 문자열이다
    (``SHELL``/``PACKAGE`` 는 파이프·변수전개·서브셸 때문에 **어떤 파일을
    만질지 알 수 없다** — 포크 ``sandboxLock.ts:11-13``).
    """

    kind: EffectKind
    path: str = ""

    @property
    def is_exclusive(self) -> bool:
        """샌드박스(=워크스페이스) 전체 배타가 필요한가.

        위 호환성 행렬을 그대로 옮긴 술어이며, 포크의 ``lockScopeFor``
        (``turn.ts:109-117``)와 같은 판정을 한다 — 경로 기반 병렬은
        ``FILE_READ``/``FILE_WRITE`` 가 **비어있지 않은** 경로를 가질 때만
        허용되고, 빈 경로는 락 키로 신뢰할 수 없으므로 배타로 떨어진다.
        """
        if self.kind in (EffectKind.FILE_READ, EffectKind.FILE_WRITE):
            return not self.path.strip()
        return self.kind is not EffectKind.NON_WORKSPACE_OR_COMPOSITE
