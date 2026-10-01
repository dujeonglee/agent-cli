"""json_fc — 스펙 구동 (Phase 5 S3). 모양·산문은 ``specs/json_fc.py``, 동작은 ``engine``.

이름을 지키는 얇은 껍데기: 테스트·레지스트리가 ``JsonFcFormat`` 을 부르고, 등가성
비교는 ``_legacy/json_fc.py`` 가 상대다. md_array 헤더 관용은 여기서 끝났다(결정 1).
"""

from __future__ import annotations

from agent_cli.wire_formats.engine import Dialect
from agent_cli.wire_formats.specs.json_fc import JSON_FC


class JsonFcFormat(Dialect):
    """산문 thought + flat action-array (multi-op, complete 종결) — ``JSON_FC`` 스펙."""

    def __init__(self):
        super().__init__(JSON_FC)
