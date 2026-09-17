"""`.agent-cli` 경로의 단일 소스 — **설정의 집은 하나** (v9.0.0).

종전(v8.40.0~)엔 ``scoped_paths()`` 가 [프로젝트, 사용자] 쌍을 돌려주고
7개 모듈이 각자 병합했다(config 는 필드별, mcp 는 이름별, hooks 는 누적,
DIRECTIVE 는 연결). "둘 다 읽고 프로젝트 우선"은 말하기는 쉬웠지만 결과가
헷갈렸다 — 어느 파일이 이겼는지 매번 따져야 했고, ``Path.cwd()`` 기준이라
하위 디렉토리에서 실행하면 조용히 달라졌다.

v9.0.0 부터 각 설정은 **정확히 한 곳**에 산다 (docs/config-scopes/DESIGN.md):

    유저 (~/.agent-cli)     config.json · models.json        ← 머신 사실
    프로젝트 (.agent-cli)   mcp · skills · agents · hooks · DIRECTIVE
    sessions_dir()          sessions · chat_history          ← 트리 밖으로 뺄 수 있는 것

쌍을 원하는 소비자가 없어졌으므로 ``scoped_paths()`` 는 은퇴했다. 소비
모듈은 ``project_dir() / "mcp.json"`` 처럼 조립하고, 종전대로 **모듈-레벨
상수**로 import 시점에 고정한다(``Path.cwd()`` 고정 + 테스트 monkeypatch seam).
"""

from __future__ import annotations

import os
from pathlib import Path

_DIR_NAME = ".agent-cli"
_SESSIONS_ENV = "AGENT_CLI_SESSIONS_DIR"


def project_dir() -> Path:
    """``cwd/.agent-cli`` — 프로젝트 설정 (mcp.json · skills/ · agents/ ·
    hooks/ · hooks.json · DIRECTIVE.md). 저장소가 필요로 하는 것."""
    return Path.cwd() / _DIR_NAME


def user_dir() -> Path:
    """``~/.agent-cli`` — 유저(머신) 설정 (config.json · models.json).
    이 컴퓨터의 환경: provider·API 키·모델 capability 캐시."""
    return Path.home() / _DIR_NAME


def sessions_dir() -> Path:
    """세션 루트 (v8.50.0) — 종전 3개 모듈(context/session·tools/context·
    main web 인스턴스 파일)이 각자 `.agent-cli/sessions` 를 손으로 조립하던
    것의 단일 소스. ``AGENT_CLI_SESSIONS_DIR`` 가 설정되면 그 경로
    (``~`` 확장) — 작업 트리에 세션을 남기지 않을 곳(헤드리스/CI 자동화,
    읽기 전용·공유 체크아웃, 벤치 컨테이너)용. 미설정 시 종전과 동일한
    cwd 상대 ``.agent-cli/sessions`` (상대경로 유지 — 소비자가 기록·표시하는
    경로 형태가 바뀌지 않게). 소비 모듈은 이 값을 모듈-레벨 상수
    ``_SESSIONS_DIR`` 로 import 시점에 고정한다(테스트 monkeypatch seam).

    v9.0.0: ``chat_history`` 도 여기 산다 — 사용자가 타이핑한 것이라 경로·
    토큰이 섞일 수 있어 작업 트리(``.agent-cli/``)에 두지 않고, 이 env 로
    컨테이너·CI 에서 트리 밖으로 뺄 수 있게 한다."""
    raw = os.environ.get(_SESSIONS_ENV, "")
    return Path(raw).expanduser() if raw else Path(_DIR_NAME) / "sessions"
