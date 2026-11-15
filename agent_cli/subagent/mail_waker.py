"""main 깨우기 — 메일이 왔는데 main 이 한가하면 입력 큐에 깨우기 항목을 넣는다 (`MailWaker`).

레지스트리와 웹을 모르는 순수 로직이다: 큐에 넣는 콜러블과 "깨울 메일이 있는가" 판정을 받는다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class MailWaker:
    """P4 (D3): main idle 자동 재기동 조율자 — 순수 로직 (web 무의존).

    회신/질문 도착 시 main worker 가 큐 대기(idle) 중이면 합성 wake
    아이템을 입력 큐에 넣어 run 을 깨운다. 배달 자체는 그 run 의 첫 턴
    경계가 수행. ``armed`` 가 중복 wake 를 막고(여러 mail → run 하나),
    wake 아이템을 꺼냈을 때 잔여가 없으면(이미 다른 run 이 배달) skip.
    ``on_run_end`` 는 "run 마지막 턴 경계 이후 도착한 mail" 레이스 봉합.
    """

    WAKE_TEXT = (
        "New agent mail has arrived. It is delivered as observation(s) at "
        "the start of this turn — review it and continue accordingly."
    )

    def __init__(self, enqueue: Callable, has_pending: Callable[[], bool]):
        self._enqueue = enqueue  # (conn_id, text) — web 서버의 입력 큐
        self._has_pending = has_pending
        self._armed = False
        # P0-9c: _armed 는 worker(on_mail)·펌프(mark_idle/handle_dequeued) 두
        # 스레드가 공유 — check-then-set 을 락으로 원자화해 "중복 무장 무해"
        # 가드가 스스로 레이스이던 것을 봉합(비무장 오판 시 wake 유실 → park).
        self._armed_lock = threading.Lock()
        self.idle = threading.Event()  # worker 가 dequeue 대기 중인 구간

    def _arm(self) -> None:
        with self._armed_lock:
            if self._armed:
                return
            self._armed = True
        self._enqueue(None, self.WAKE_TEXT)

    def on_mail(self) -> None:
        """registry.on_reply 에서 — idle 일 때만 wake."""
        if self.idle.is_set():
            self._arm()

    def mark_idle(self) -> None:
        """펌프가 큐 블록 직전에 호출 — idle 를 set 하고, **이미 미배달 회신이
        있으면 즉시 재무장**한다. ``on_run_end()`` 반환 후 ``idle.set()`` 전
        창에서 도착한 회신은 ``on_mail()`` 이 idle 미set 을 보고 wake 를
        드롭하는데(web 은 timeout 없는 무한 블록이라 그대로 영구 park),
        이 재확인이 그 lost-wakeup 을 봉합한다.

        정합성: idle 를 **먼저** set 한 뒤 pending 을 확인하므로, 동시 도착
        회신은 (a) 여기 ``_has_pending`` 에 잡히거나 (b) idle set 을 본
        ``on_mail`` 이 무장한다 — 최소 한쪽은 반드시 무장(append 와
        has_pending 은 registry cv 로 직렬화, idle 는 Event 로 순서 보장).
        중복 무장은 ``_armed`` 가드로 무해(handle_dequeued 가 skip 처리)."""
        self.idle.set()
        if self._has_pending():
            self._arm()

    def on_run_end(self) -> None:
        """run 종료 직후 — 마지막 턴 경계 이후 도착분 잔여 확인."""
        if self._has_pending():
            self._arm()

    def handle_dequeued(self, text: str) -> str | None:
        """큐에서 꺼낸 메시지 판정: wake 아니면 None, wake 면
        "run"(배달할 잔여 있음) 또는 "skip"(이미 배달됨)."""
        if text != self.WAKE_TEXT:
            return None
        with self._armed_lock:
            self._armed = False
        return "run" if self._has_pending() else "skip"
