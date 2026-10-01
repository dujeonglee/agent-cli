"""xml_fc — 스펙 구동 (Phase 5 S2). 모양·산문은 ``specs/xml_fc.py``, 동작은 ``engine``.

이 모듈은 이름을 지키는 얇은 껍데기다: 테스트·레지스트리가 ``XmlFcFormat`` 을
부르고, 등가성 비교는 ``tests/equivalence/expected/xml_fc.*`` (옛 모듈의 고정 출력)가 상대다.
"""

from __future__ import annotations

from agent_cli.wire_formats.engine import Dialect
from agent_cli.wire_formats.specs.xml_fc import XML_FC


class XmlFcFormat(Dialect):
    """태그-파라미터 function-call wire format (멀티-op) — ``XML_FC`` 스펙."""

    def __init__(self):
        super().__init__(XML_FC)
