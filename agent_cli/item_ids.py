"""열린 항목 id — 형식 하나 ``<접두>-<토큰>`` (docs/ledger/DESIGN.md §11.4, v10.56.0).

장부(`loop/ledger.py`)의 모든 항목이 이 형식이고, id 는 **생기는 곳에서 바로**
이 형식으로 만든다 — 어디서도 "1 ↔ u-1" 을 번역하지 않는다. 접두가 닫는 법을
말한다:

==========  ====================  =================================
접두        출처                  닫는 법
==========  ====================  =================================
``u-``      사람 요청(예약 포함)  ``complete`` 의 ``answers``
``n-``      그 밖의 배달(main 앞  ``complete`` 의 ``answers`` (main) — 회신·
            메일·알림·회신 도착)  통지는 id 만 있고 항목이 아니다(v10.141.0)
``r-``      회신을 기다리는 요청  ``reply`` 의 ``id``
``q-``      사람에게 건 질문      (모델이 닫지 않는다 — 사람이 트레이에서 답한다)
==========  ====================  =================================

v10.77.0: 번호는 :func:`next_id` 하나가 매긴다 — 번호 공간을 모든 접두가 함께
쓰고, **모든 배달이 id 를 받는다**(장부 항목이 아닌 알림·도착한 회신도). id 가
곧 키다: 받은편지함·런 이름·회신 파일·화면이 순번 대신 이것을 쓴다.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

USER = "u"
MAIL = "n"
REPLY = "r"
QUESTION = "q"

#: 접두 전부 (v10.77.0)
PREFIXES = frozenset({USER, MAIL, REPLY, QUESTION})

#: 접두 → 그 항목을 닫는 도구. ``q-`` 는 없다 — 질문은 사람에게만 가고(v10.77.0)
#: 모델이 닫을 항목이 아니다.
CLOSER = {USER: "complete", MAIL: "complete", REPLY: "reply"}


_ALLOC_LOCK = threading.Lock()
_last = 0


def next_id(prefix: str) -> str:
    """새 항목·배달 id — 번호 공간 하나를 모든 접두가 함께 쓴다 (v10.77.0).

    id 가 곧 키다: 번호만으로도 프로세스 안에서 유일하고 단조 증가라 순서도
    말한다. 세션을 다시 열면 :func:`observe` 가 저장된 id 를 넘겨 번호를
    잇는다(할당기를 따로 저장하지 않는다)."""
    if prefix not in PREFIXES:
        raise ValueError(f"unknown item prefix {prefix!r}")
    global _last
    with _ALLOC_LOCK:
        _last += 1
        return f"{prefix}-{_last}"


def number_of(item_id) -> int:
    """``"r-17"`` → ``17``. 번호 형식이 아니면(예전 세션의 ``q-7a3f``) 0."""
    _, sep, tail = str(item_id or "").partition("-")
    return int(tail) if sep and tail.isdigit() else 0


def observe(*item_ids) -> None:
    """이미 있는 id 를 본다 — 다음 번호가 그보다 크게 (resume·복원)."""
    global _last
    top = max((number_of(i) for i in item_ids), default=0)
    with _ALLOC_LOCK:
        _last = max(_last, top)


#: 글 속의 id — 접두 하나·하이픈·숫자(앞이 낱말 글자가 아닐 때만: ``user-1`` 의 ``r-1`` 은 아니다)
_ID_IN_TEXT = re.compile(r"\b[unrq]-(\d+)\b")

#: 재시작 때 번호를 잇는 기록 글 — 모델 기록과 🤝 대화창
RECORD_FILES = ("history.jsonl", "conversation.jsonl")


def observe_records(folder: Path) -> None:
    """``folder`` 아래의 기록 글(`RECORD_FILES`)에 적힌 모든 id 를 본다 — 재시작한 세션의 새 번호가 지난 생의 어떤
    id 와도 겹치지 않게 (5c-0). 할당기를 저장하지 않으므로 기록이 곧 근거다: 모델이 본 요청·질문·회신 id 는 전부
    history 나 🤝 창의 글에 있다. 사람이 쓴 글의 ``r-2026`` 같은 것도 번호를 올린다 — 건너뛸 뿐 겹치지 않는다.
    읽지 못한 파일은 건너뛴다."""
    top = 0
    for name in RECORD_FILES:
        for path in Path(folder).rglob(name):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            top = max([top, *(int(m) for m in _ID_IN_TEXT.findall(text))])
    observe(f"u-{top}")


def prefix_of(item_id) -> str:
    """``"r-12"`` → ``"r"``. 형식이 아니면 ``""``."""
    head, sep, _ = str(item_id or "").partition("-")
    return head if sep and head in PREFIXES else ""


def closer_of(item_id) -> str:
    """그 id 의 항목을 닫는 도구 이름, 모르면 ``""``."""
    return CLOSER.get(prefix_of(item_id), "")
