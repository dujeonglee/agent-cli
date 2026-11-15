"""세션 하위 디렉터리 이름 — 스킬 런의 ctx 집 ``{prefix}_{name}_{hash}_{ts}{ms}`` (`generate_subdir_name`).

종전엔 일회성 run 의 결과 포장(``STATUS: …``·활동 로그·``result.md``)이 여기 살았다 — v10.141.0
에서 결과가 회신이 되며 지웠다(docs/agent-engine/DESIGN.md §4). run 에이전트는 다른 에이전트와
같이 ``agents/<key>/`` 에 산다.
"""

from __future__ import annotations

import os
import time


def generate_subdir_name(prefix: str, name: str) -> str:
    """Unique session-subdir name: ``{prefix}_{name}_{hash}_{ts}{ms}``.

    The name is minted at the SCOPE-opening call site (so the resume sidecar's
    ``ctx_dir`` matches the directory actually written) and passed down."""
    hash_part = os.urandom(3).hex()  # 6-char hex
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    ms = f"{int(time.time() * 1000) % 1000:03d}"
    return f"{prefix}_{name}_{hash_part}_{ts}{ms}"
