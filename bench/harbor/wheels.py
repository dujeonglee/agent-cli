"""dist/ 의 최신 agent-cli wheel 고르기 — **버전 숫자**로 비교한다.

종전 ``sorted(glob)[-1]`` 은 사전순이라 ``agent_cli-9.24.0`` 이 ``agent_cli-10.27.0`` 을
이겼고, 벤치 컨테이너에 옛 wheel 이 조용히 설치됐다(세션 메타의 ``response_format``
키가 단서 — 10.x 는 쓰지 않는 키). harbor 없이 import 되는 stdlib 전용 모듈이라
``test_wheels.py`` 가 호스트 파이썬으로 돈다.
"""

from __future__ import annotations

import re
from pathlib import Path

_WHEEL_RE = re.compile(r"^agent_cli-(\d+(?:\.\d+)*)([^-]*)-")


def wheel_version(name: str) -> tuple[int, ...] | None:
    """``agent_cli-10.27.0-py3-none-any.whl`` → ``(10, 27, 0, 0)``; 패턴 밖이면 None.
    마지막 원소는 정식판 0 / ``rc1`` 같은 접미사 -1 — 같은 숫자면 정식판이 뒤다."""
    m = _WHEEL_RE.match(name)
    if not m:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    return nums + ((-1,) if m.group(2) else (0,))


def newest_wheel(dist_dir: Path) -> Path:
    wheels = [
        (v, p)
        for p in dist_dir.glob("agent_cli-*.whl")
        if (v := wheel_version(p.name)) is not None
    ]
    if not wheels:
        raise FileNotFoundError(
            f"no agent-cli wheel under {dist_dir} — run "
            "`python3 -m build --wheel` first, or pass --ak wheel=<path>"
        )
    return max(wheels, key=lambda t: t[0])[1]
