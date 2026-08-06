"""M2 동시성 계측 — ``turns.jsonl`` 에 턴/락/압축/거부 이벤트를 추가 기록.

병합 계획(docs/research/11 §2 M2)의 "측정 훅"이다. P1(HOL TTFT)·P2/P3(락
대기)·P4(사용자별 공정성)·N1(동시 압축)·N3(귀속 정확도) 실험이 전부 이
파일 하나를 읽어 수치를 뽑는다. 포크(Coagora)는 락 계측(BENCH_TRACE)을
설계만 하고 구현하지 않았으므로(EXPERIMENTS.md H0.3), 이 모듈이 계약의 첫
완전 계측 구현이다.

**기록 위치는 기존 ``{session_dir}/turns.jsonl`` 을 공유한다** — 복구 관측
(``recovery/observability.TurnRecorder``)이 소유한 파일이며, 행 구별은
``event`` 키다: TurnRecord 행은 ``event`` 키가 없고, 압축 행은
``"compaction"``, 이 모듈은 ``"turn"``/``"lock"``/``"compact"``/``"reject"``
를 쓴다. 한 파일 = 한 타임라인이라 분석 스크립트가 조인 없이 인과를 본다.
스키마 버저닝 없음 — 독자는 모르는 필드를 무시한다(observability 와 동일
규약). append 는 ``fsio.append_line`` 이 직렬화한다.

**프로세스 전역 + 기본 off** 인 이유: 발화 지점이 서로 다른 수명의 객체에
흩어져 있다 — 서버 스레드(enqueue/reject), 디스패처(dispatch), run_loop
내부(first_token), 도구 스레드(lock), ctx(compact). 인스턴스를 전부에
배선하면 그 배선 자체가 직렬 경로를 건드린다. ``effect_lock`` 의 전역 스코프와
같은 정당화다: 한 프로세스 = 한 세션. off 일 때 ``emit`` 은 None 체크 한 번의
no-op 이라 기존 경로의 동작이 보존된다(opt-in 계약).

시계 규약: ``mono_ms`` (``time.monotonic()*1000``) 로 구간을 계산한다 —
같은 프로세스의 스레드끼리 단조성이 보장되므로 TTFT = first_token.mono_ms −
enqueue.mono_ms 가 스레드 경계를 넘어 유효하다. ``timestamp`` (ISO UTC)는
사람이 다른 로그와 대조하는 용도다.

프라이버시: observability 와 같은 원칙 — 프롬프트/응답 본문은 기록하지
않는다. 구조 메타데이터(턴 id, 큐 id, 닉네임, 경로, 시각)만 쓴다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from agent_cli.fsio import append_line

_path: Path | None = None
_active_turns_provider: Callable[[], int] | None = None


def enable(session_dir: Path | str) -> None:
    """계측을 켠다 — ``{session_dir}/turns.jsonl`` 에 기록 시작.

    CLI ``--turn-metrics`` 가 부른다. 멱등."""
    global _path
    _path = Path(session_dir) / "turns.jsonl"


def disable() -> None:
    """계측을 끈다 + 프로바이더 해제 — 테스트 격리용."""
    global _path, _active_turns_provider
    _path = None
    _active_turns_provider = None


def is_enabled() -> bool:
    return _path is not None


def set_active_turns_provider(fn: Callable[[], int] | None) -> None:
    """ "지금 활성 턴 몇 개인가" 콜러블 등록 (병렬 모드의 TurnRegistry가 건다).

    압축 이벤트가 "압축 중 동시 턴 수"를 실어 나르는 데 쓴다(N1). 직렬
    모드에서는 아무도 걸지 않아 해당 필드가 생략된다."""
    global _active_turns_provider
    _active_turns_provider = fn


def active_turns() -> int | None:
    """등록된 프로바이더의 현재 값. 없으면(직렬) None."""
    fn = _active_turns_provider
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None  # 계측 실패가 본 작업을 막지 않는다


def emit(event: str, **fields) -> None:
    """이벤트 한 줄 append. off 면 no-op.

    값이 None 인 필드는 생략한다 — "모름"과 "0"을 파일에서 구별하기 위해서다
    (직렬 모드의 active_turns 등). 기록 실패는 삼킨다: 계측은 관측이지
    참여가 아니며, 디스크 오류로 턴을 죽이면 관측 자체가 실험을 오염시킨다."""
    path = _path
    if path is None:
        return
    rec: dict = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mono_ms": time.monotonic() * 1000.0,
    }
    for k, v in fields.items():
        if v is not None:
            rec[k] = v
    try:
        append_line(path, json.dumps(rec, ensure_ascii=False))
    except OSError:
        pass
