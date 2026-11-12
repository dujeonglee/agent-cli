"""``requests`` 예외를 읽는 공용 조각 (v10.126.0) — 예외 사슬 순회와 TLS 사유 한 줄.

``tools/fetch.py``(도구 실패 문구)와 ``providers/http.py``(부트·LLM 실패 줄의 연결 분류)가
같은 사슬을 본다. 두 벌을 두면 한쪽만 urllib3 버전 차이를 따라가게 된다. 이 모듈은
표준 라이브러리 밖으로는 아무것도 import 하지 않는다 — 도구 층과 공급자 층 어느 쪽이
가져와도 의존 방향이 꼬이지 않는 자리."""

from __future__ import annotations

import re
import ssl
from collections.abc import Iterator


def exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """예외와 그 원인들 — ``__cause__``·``__context__``·urllib3 의 ``reason``·
    ``args`` 안의 예외(requests 는 ``MaxRetryError`` 를 args 로 감싼다)."""
    seen: set[int] = set()
    todo: list[BaseException] = [exc]
    while todo:
        e = todo.pop(0)
        if id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        nxt = [e.__cause__, e.__context__, getattr(e, "reason", None), *e.args]
        todo.extend(x for x in nxt if isinstance(x, BaseException))


def one_line(text: object) -> str:
    return " ".join(str(text).split())


def tls_detail(exc: BaseException) -> str:
    """TLS 실패의 이유 — 가장 안쪽 ``ssl.SSLError`` 의 말에서 ``[SSL: …]`` 머리와
    ``(_ssl.c:N)`` 꼬리를 뗀다. requests 의 겉 문구(``HTTPSConnectionPool(…):
    Max retries exceeded …``)는 이유를 300자 뒤에 묻는다."""
    inner = next((e for e in exception_chain(exc) if isinstance(e, ssl.SSLError)), None)
    if inner is not None:
        verify = getattr(inner, "verify_message", None)
        msg = f"certificate verify failed: {verify}" if verify else str(inner)
        msg = re.sub(r"^\[SSL: [A-Z_]+\]\s*", "", one_line(msg))
        msg = re.sub(r"\s*\(_ssl\.c:\d+\)$", "", msg)
        if msg:
            return msg
    return one_line(exc) or type(exc).__name__
