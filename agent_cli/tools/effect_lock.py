"""부수효과 계층 락 — "병렬 추론 + 직렬 부수효과" 계약의 후반부 (M4/A3).

포크(Coagora) ``backend/src/agent/sandboxLock.ts`` 의 이식이다. A1 이 **추론**을
병렬화했다면, 여기서는 그 병렬 턴들이 일으키는 **부수효과**를 충돌 단위로
직렬화해 "동시 파일 쓰기 진입 0"을 만든다.

포크의 설계 이력이 곧 이 모듈의 근거다 (``sandboxLock.ts:7-13``): v1 은
샌드박스 단위 **단일 mutex** 였는데, 서로 **다른 파일**을 만지는 턴까지 줄을
세웠다. 실측(E2-B)에서 두 동시 턴이 모두 파일을 수정하면 병렬 이점이 1.07× 로
붕괴하는 것이 확인돼, 직렬 경계를 "워크스페이스"에서 "**충돌 단위**"로 좁혔다.

핵심 제약: SHELL/PACKAGE 는 **어떤 파일을 만질지 알 수 없다**(파이프·변수전개·
서브셸). 그래서 경로 단위로 좁힐 수 없고 워크스페이스 전체 배타여야 한다.
결과적으로 단순 mutex 가 아니라 아래 행렬을 만족하는 **계층 락**이 된다:

    FILE_WRITE/READ(경로 P) ↔ FILE_WRITE/READ(경로 Q≠P) : 병렬  ← 이득의 원천
    FILE_WRITE/READ(P)      ↔ FILE_WRITE/READ(P)        : 직렬
    그 외 모든 조합(SHELL/PACKAGE/FILE_DELETE)          : 배타

공정성: 워크스페이스별 **엄격 FIFO(추월 금지)**. 큐 머리가 막히면 뒤도 대기한다
— 파일 작업이 계속 들어와도 SHELL 이 굶지 않는다. 동시성을 조금 포기하고
공정성을 택한 것이며, 이 선택 역시 포크에서 검증됐다(``sandboxLock.ts:24-25``).

**UNKNOWN 은 락을 잡지 않는다** — M1 에서 남긴 숙제의 해소다. M1 은 "모호하면
UNKNOWN=배타"로 안전측 분류를 했지만, 실제 락을 붙여 보니 UNKNOWN 이 성격이
다른 셋을 뭉뚱그리고 있었다:
  ① **복합 도구**(``agent``/``run_skill``) — 중첩 루프를 띄우고 그 안에서 잎
     도구들이 **각자** 락을 잡는다. 부모가 배타 락을 쥔 채 자식이 같은 락을
     요구하면 **교착**이다(자식은 다른 스레드라 재진입도 안 통한다).
  ② **사람 대기 도구**(``ask``) — 배타 락을 쥔 채 사람 답을 기다리면 그동안
     다른 모든 턴의 부수효과가 멈춘다.
  ③ **워크스페이스 밖 상태 도구**(``memory``/``read_context``/``code_index``/
     ``fetch``) — 세션 파일이나 인덱스 DB 를 만지지 이 락이 정렬할 대상인
     워크스페이스 경로를 만지지 않는다. 각자 자기 가드가 있다(fsio append 락,
     ``code_index._BUILD_LOCK``).
셋 다 "이 락으로 정렬할 워크스페이스 효과가 없다"는 점에서 같으므로, UNKNOWN 의
운용 의미를 **"정렬 대상 아님"** 으로 확정한다. :attr:`EffectIntent.is_exclusive`
는 여전히 "잠근다면 배타여야 하는가"를 답하며, **잠글지 여부**는 여기가 정한다.

프로세스 전역인 이유: 락은 한 워크스페이스에 대한 것이고, 서브에이전트는
프로세스가 아니라 스레드다. 루프별 설정으로 두면 어떤 루프는 잠그고 어떤 루프는
안 잠그는 상태가 되어 보호가 통째로 무의미해진다(포크도 ``config.isolation``
전역 설정이다).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from agent_cli import turn_metrics
from agent_cli.tools.effect import EffectIntent, EffectKind

#: 락 스코프 — CLI ``--lock-scope`` 가 고른다.
#:   ``off``      : 잠그지 않는다. **P3 ablation 의 대조군**이자 기본값
#:                  (기존 직렬 경로 바이트 수준 보존).
#:   ``workspace``: 모든 효과를 워크스페이스 전체 배타로 — 포크 v1 의 단순
#:                  mutex 재현. ablation 의 "너무 넓은 락" 팔.
#:   ``conflict`` : 위 호환성 행렬 적용. 실사용 권장값.
SCOPES = ("off", "workspace", "conflict")
DEFAULT_SCOPE = "off"

_scope = DEFAULT_SCOPE


def set_scope(scope: str) -> None:
    """전역 락 스코프 설정. 알 수 없는 값은 ``ValueError`` — 조용한 폴백 금지
    (오타 하나로 "락이 걸린 줄" 알고 측정하면 실험이 무효가 된다)."""
    global _scope
    if scope not in SCOPES:
        raise ValueError(f"unknown lock scope {scope!r} (expected: {'|'.join(SCOPES)})")
    _scope = scope


def get_scope() -> str:
    return _scope


def normalize_lock_path(rel_path: str) -> str:
    """경로를 락 키로 정규화한다 — 같은 파일이 다른 키가 되지 않도록.

    ``normpath`` 로 ``./``·중복 구분자·``..`` 를 정리한다. 포크는 win32
    소문자화도 하지만 본 프로젝트는 Linux/WSL 을 정식 측정 기준으로 삼기로 해
    넣지 않았다. 경로 탈출 검사는 하지 않는다(그건 ``_confine`` 책임) — 여기서는
    키 동일성만 본다. 상대/절대 혼용은 ``abspath`` 로 흡수한다: 같은 파일을 한
    턴은 상대경로로, 다른 턴은 절대경로로 부르면 키가 갈려 보호가 새기 때문이다.
    """
    return os.path.abspath(os.path.normpath(rel_path))


@dataclass
class _Waiter:
    exclusive: bool
    path: str
    #: 입장 게이트. 펌프가 set 하면 대기 중인 작업이 시작된다.
    gate: threading.Event = field(default_factory=threading.Event)


@dataclass
class _LockState:
    #: 현재 실행 중인 작업들(경로가 다르면 병렬 가능하므로 리스트).
    running: list[_Waiter] = field(default_factory=list)
    #: 대기열 — 엄격 FIFO.
    queue: list[_Waiter] = field(default_factory=list)


_states: dict[str, _LockState] = {}
_states_lock = threading.Lock()


def _compatible(w: _Waiter, running: list[_Waiter]) -> bool:
    """``w`` 가 현재 실행 중인 작업들과 동시에 돌 수 있는가 (행렬 그대로)."""
    if w.exclusive:
        return not running
    # 경로 모드: 실행 중인 것이 모두 경로 모드이고 경로가 서로 달라야 한다.
    return all((not r.exclusive) and r.path != w.path for r in running)


def _pump_locked(key: str) -> list[_Waiter]:
    """큐 머리에서 호환되는 동안 입장시킨다 (``_states_lock`` 보유 중 호출).

    **머리를 추월시키지 않는 것이 기아 방지의 핵심**이다 — 배타 대기자가 머리에
    있으면 뒤의 경로 작업도 함께 기다린다. 실제 ``gate.set()`` 은 락 밖에서
    하도록 입장자 목록만 돌려준다(콜백을 락 아래서 부르지 않는 규율).
    """
    st = _states.get(key)
    if st is None:
        return []
    admitted: list[_Waiter] = []
    while st.queue:
        head = st.queue[0]
        if not _compatible(head, st.running):
            break
        st.queue.pop(0)
        st.running.append(head)
        admitted.append(head)
    if not st.running and not st.queue:
        del _states[key]  # 메모리 누수 방지 — 포크 sandboxLock.ts:86 과 동형
    return admitted


@contextmanager
def hold(intent: EffectIntent, *, key: str | None = None) -> Iterator[bool]:
    """``intent`` 가 선언한 효과를 충돌 단위로 직렬화한 채 블록을 실행한다.

    ``yield`` 값은 "실제로 잠갔는가" — 테스트·계측용이며 보통 무시한다.
    스코프가 ``off`` 이거나 정렬 대상이 아닌 효과(UNKNOWN)면 잠그지 않고 즉시
    진행한다. 블록이 예외를 던져도 ``finally`` 에서 반드시 해제하고 다음
    대기자를 진행시킨다(직렬성 보존 — 포크 ``sandboxLock.ts:114-126``).
    """
    scope = _scope
    if scope == "off" or intent.kind is EffectKind.UNKNOWN:
        yield False
        return

    if scope == "workspace":
        waiter = _Waiter(exclusive=True, path="")
    else:
        exclusive = intent.is_exclusive
        waiter = _Waiter(
            exclusive=exclusive,
            path="" if exclusive else normalize_lock_path(intent.path),
        )

    lock_key = key or _default_key()
    # M2 계측: 대기 시작→획득→해제. thread 이름이 곧 턴 귀속이다 —
    # TurnRegistry 가 워커 스레드를 ``agent-turn-t{n}`` 으로 명명하므로
    # 별도 배선 없이 락 이벤트가 턴에 귀속된다(직렬 모드는 MainThread 류).
    # 포크가 설계만 하고 미구현한 BENCH_TRACE(EXPERIMENTS.md H0.3)의 실현.
    _thread = threading.current_thread().name
    turn_metrics.emit(
        "lock",
        phase="enqueue",
        kind=intent.kind.value,
        path=waiter.path or None,
        exclusive=waiter.exclusive,
        thread=_thread,
    )
    _t0 = time.monotonic()
    with _states_lock:
        st = _states.setdefault(lock_key, _LockState())
        st.queue.append(waiter)
        admitted = _pump_locked(lock_key)
    for w in admitted:
        w.gate.set()

    waiter.gate.wait()
    _t1 = time.monotonic()
    turn_metrics.emit(
        "lock",
        phase="acquire",
        kind=intent.kind.value,
        path=waiter.path or None,
        exclusive=waiter.exclusive,
        thread=_thread,
        wait_ms=(_t1 - _t0) * 1000.0,
    )
    try:
        yield True
    finally:
        with _states_lock:
            cur = _states.get(lock_key)
            if cur is not None and waiter in cur.running:
                cur.running.remove(waiter)
            admitted = _pump_locked(lock_key)
        for w in admitted:
            w.gate.set()
        turn_metrics.emit(
            "lock",
            phase="release",
            kind=intent.kind.value,
            path=waiter.path or None,
            exclusive=waiter.exclusive,
            thread=_thread,
            held_ms=(time.monotonic() - _t1) * 1000.0,
        )


def _default_key() -> str:
    """기본 락 키 = 워크스페이스 루트. 한 프로세스 = 한 워크스페이스지만, 키를
    두어 서브에이전트가 다른 루트에서 도는 미래 구성에도 의미가 유지된다."""
    from agent_cli.tools._confine import workspace_root

    return str(workspace_root())


# ── 테스트/진단 ──────────────────────────────────────────


def active_keys() -> int:
    """대기/실행 체인이 걸려 있는 키 수 (0 이면 모두 정리됨)."""
    with _states_lock:
        return len(_states)


def running_count(key: str | None = None) -> int:
    """특정 키에서 현재 실행 중인 작업 수 (병렬도 관측)."""
    with _states_lock:
        st = _states.get(key or _default_key())
        return len(st.running) if st else 0


def reset() -> None:
    """전역 상태 초기화 — 테스트 격리용."""
    global _scope
    with _states_lock:
        _states.clear()
    _scope = DEFAULT_SCOPE
