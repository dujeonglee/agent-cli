"""열린 항목 id — 형식 하나 ``<접두>-<토큰>`` (docs/ledger/DESIGN.md §11.4, v10.56.0).

장부(`loop/ledger.py`)의 모든 항목이 이 형식이고, id 는 **생기는 곳에서 바로**
이 형식으로 만든다 — 어디서도 "1 ↔ u-1" 을 번역하지 않는다. 접두가 닫는 법을
말한다:

==========  ====================  =================================
접두        출처                  닫는 법
==========  ====================  =================================
``u-``      사람 요청(예약 포함)  ``complete`` 의 ``answers``
``n-``      main 앞 메일          ``complete`` 의 ``answers``
``r-``      회신 빚               ``reply(id, text)``
``q-``      질문 답 빚            ``answer(id, text)``
==========  ====================  =================================

하이픈은 질문 id(``q-7a3f``, ``agents.json`` 에 영속돼 resume 으로 돌아온다)와
같은 형식이라서다 — 질문을 고치지 않고 나머지를 맞춘다.
"""

from __future__ import annotations

USER = "u"
MAIL = "n"
REPLY = "r"
QUESTION = "q"

#: 접두 → 그 항목을 닫는 도구
CLOSER = {USER: "complete", MAIL: "complete", REPLY: "reply", QUESTION: "answer"}


def make(prefix: str, token) -> str:
    return f"{prefix}-{token}"


def prefix_of(item_id) -> str:
    """``"r-12"`` → ``"r"``. 형식이 아니면 ``""``."""
    head, sep, _ = str(item_id or "").partition("-")
    return head if sep and head in CLOSER else ""


def closer_of(item_id) -> str:
    """그 id 의 항목을 닫는 도구 이름, 모르면 ``""``."""
    return CLOSER.get(prefix_of(item_id), "")
