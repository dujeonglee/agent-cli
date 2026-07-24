"""세션 입력 큐 — web 과 CLI 가 공유하는 메시지 펌프의 심장 (teammate P5).

WebServer 안에 있던 pending deque + Condition 골격을 추출한 것 (사용자
지시: "CLI 전용 큐를 새로 만들지 말고 web 골격을 공용 계층으로").
web 은 여기에 SSE 브로드캐스트·닉네임 해석을 얹고(:class:`WebServer`
의 wrapper 메서드), CLI ``run`` 은 이 큐를 직접 돌려 **큐 펌프**가 된다
— 초기 질의도, teammate 회신의 wake 아이템(MailWaker)도 같은 큐로
들어오므로 main 이 idle 이어도 회신이 잠들지 않는다 (D3 의 CLI 완성).

의도적으로 web 무의존: 아이템 shape ``{id, conn_id, nickname, text}`` 는
web 의 기존 계약 그대로 유지한다 (queue 표시·cancel 소유권이 이 필드를
읽음).
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable


class InputQueue:
    """FIFO 입력 큐 + blocking 소비 + shutdown sentinel.

    ``on_change`` (옵션): enqueue/dequeue/cancel 로 내용이 바뀔 때마다
    호출 — web 이 라이브 큐 표시 브로드캐스트를 얹는 자리 (락 밖에서
    호출된다: renderer 락과의 중첩 회피, 기존 ``_broadcast_queue`` 규율).
    """

    SHUTDOWN = object()  # identity 비교 — 큐를 소진한 뒤에만 반환된다

    def __init__(self, on_change: Callable[[], None] | None = None):
        self._items: deque = deque()
        self._cv = threading.Condition()
        self._shutdown = False
        self._seq = 0
        self.on_change = on_change

    def _changed(self) -> None:
        cb = self.on_change
        if cb is not None:
            cb()

    def enqueue(self, conn_id: str | None, text: str, *, nickname=None) -> dict:
        """메시지 추가 — ``{id, conn_id, nickname, text}`` 반환."""
        with self._cv:
            self._seq += 1
            item = {
                "id": str(self._seq),
                "conn_id": conn_id or "",
                "nickname": nickname,
                "text": text,
            }
            self._items.append(item)
            self._cv.notify()
        self._changed()
        return item

    def dequeue_blocking(self, timeout: float | None = None):
        """블록 소비: 아이템 dict / :data:`SHUTDOWN` / ``None`` (timeout).

        shutdown 전에 쌓인 메시지는 먼저 소진된다 (FIFO — 기존 web 계약).
        ``timeout`` 은 CLI 큐 펌프의 정지 판정용 (web 은 무한 대기).
        """
        with self._cv:
            while not self._items and not self._shutdown:
                if not self._cv.wait(timeout):
                    return None  # timeout — 호출자가 quiescence 판정
            if not self._items:
                return self.SHUTDOWN
            item = self._items.popleft()
        self._changed()
        return item

    def dequeue_nowait(self) -> dict | None:
        """턴 경계 (running loop): 있으면 하나, 없으면 None (비블록)."""
        with self._cv:
            if not self._items:
                return None
            item = self._items.popleft()
        self._changed()
        return item

    def cancel(self, conn_id: str | None, msg_id: str) -> bool:
        """대기 중 메시지 제거 — 소유자(conn_id 일치)만. 제거 여부 반환."""
        removed = False
        with self._cv:
            for it in list(self._items):
                if it["id"] == msg_id and it["conn_id"] == (conn_id or ""):
                    self._items.remove(it)
                    removed = True
                    break
        if removed:
            self._changed()
        return removed

    def snapshot(self) -> list[dict]:
        with self._cv:
            return [dict(it) for it in self._items]

    def pending_count(self) -> int:
        with self._cv:
            return len(self._items)

    def shutdown(self) -> None:
        """blocking 소비자를 깨워 SHUTDOWN 을 받게 한다. 멱등."""
        with self._cv:
            self._shutdown = True
            self._cv.notify_all()
