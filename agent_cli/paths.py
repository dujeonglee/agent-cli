"""`.agent-cli` 프로젝트/사용자 경로쌍의 단일 소스 (리뷰 §4.5 P1, v8.40.0).

종전엔 config/models/mcp/hooks(파일·디렉토리)/skills/agents/DIRECTIVE 의
7개 모듈이 같은 경로쌍을 각자 손으로 나열했고, 선언 순서까지 서로 달랐다
(mcp.json 만 [사용자, 프로젝트] 역순 선언). 이제 선언은 여기 한 곳 —
**항상 [프로젝트, 사용자] 순 = 우선순위 순(프로젝트 승)** 이 계약이고,
소비 모듈은 자기 병합 입도(필드별 병합 / 이름별 가림 / 둘 다 사용)만
자기 쪽에 남긴다. 역순 순회가 필요한 소비자는 ``reversed(...)`` 로 명시.

주의: 각 모듈이 이 함수를 **모듈-레벨 상수 조립에** 쓰므로 ``Path.cwd()``
는 종전과 동일하게 import 시점에 고정된다 (테스트의 상수 monkeypatch
seam 도 그대로 유지).
"""

from __future__ import annotations

import os
from pathlib import Path

_DIR_NAME = ".agent-cli"
_SESSIONS_ENV = "AGENT_CLI_SESSIONS_DIR"


def scoped_paths(*parts: str) -> list[Path]:
    """``[cwd/.agent-cli/<parts>, home/.agent-cli/<parts>]`` — 프로젝트
    우선 경로쌍. 인자 없이 부르면 베이스 디렉토리 쌍(DIRECTIVE 용)."""
    return [
        Path.cwd() / _DIR_NAME / Path(*parts) if parts else Path.cwd() / _DIR_NAME,
        Path.home() / _DIR_NAME / Path(*parts) if parts else Path.home() / _DIR_NAME,
    ]


def sessions_dir() -> Path:
    """세션 루트 (v8.50.0) — 종전 3개 모듈(context/session·tools/context·
    main web 인스턴스 파일)이 각자 `.agent-cli/sessions` 를 손으로 조립하던
    것의 단일 소스. ``AGENT_CLI_SESSIONS_DIR`` 가 설정되면 그 경로
    (``~`` 확장) — 작업 트리에 세션을 남기지 않을 곳(헤드리스/CI 자동화,
    읽기 전용·공유 체크아웃, 벤치 컨테이너)용. 미설정 시 종전과 동일한
    cwd 상대 ``.agent-cli/sessions`` (상대경로 유지 — 소비자가 기록·표시하는
    경로 형태가 바뀌지 않게). 소비 모듈은 이 값을 모듈-레벨 상수
    ``_SESSIONS_DIR`` 로 import 시점에 고정한다(테스트 monkeypatch seam)."""
    raw = os.environ.get(_SESSIONS_ENV, "")
    return Path(raw).expanduser() if raw else Path(_DIR_NAME) / "sessions"
