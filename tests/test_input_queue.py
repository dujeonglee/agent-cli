"""공용 InputQueue 계약 (teammate P5) — web WebServer 와 CLI run 펌프가
공유하는 골격. 아이템 shape {id, conn_id, nickname, text} 는 web 의 큐
표시·cancel 소유권 계약이라 필드 고정."""

from __future__ import annotations

import threading

from agent_cli.input_queue import InputQueue


class TestInputQueue:
    def test_fifo_and_item_shape(self):
        q = InputQueue()
        q.enqueue("c1", "first", nickname="bob")
        q.enqueue(None, "second")
        a = q.dequeue_blocking(timeout=1)
        b = q.dequeue_blocking(timeout=1)
        assert a["text"] == "first" and a["conn_id"] == "c1" and a["nickname"] == "bob"
        assert b["text"] == "second" and b["conn_id"] == "" and b["nickname"] is None
        assert a["id"] != b["id"]

    def test_timeout_returns_none(self):
        q = InputQueue()
        assert q.dequeue_blocking(timeout=0.05) is None

    def test_shutdown_drains_fifo_first(self):
        q = InputQueue()
        q.enqueue(None, "queued-before-shutdown")
        q.shutdown()
        item = q.dequeue_blocking(timeout=1)
        assert item["text"] == "queued-before-shutdown"
        assert q.dequeue_blocking(timeout=1) is InputQueue.SHUTDOWN
        assert q.dequeue_blocking(timeout=1) is InputQueue.SHUTDOWN  # 멱등

    def test_shutdown_wakes_blocked_consumer(self):
        q = InputQueue()
        got = []
        t = threading.Thread(target=lambda: got.append(q.dequeue_blocking()))
        t.start()
        q.shutdown()
        t.join(timeout=2)
        assert got == [InputQueue.SHUTDOWN]

    def test_nowait_and_snapshot(self):
        q = InputQueue()
        assert q.dequeue_nowait() is None
        q.enqueue(None, "x")
        assert [i["text"] for i in q.snapshot()] == ["x"]
        assert q.pending_count() == 1
        assert q.dequeue_nowait()["text"] == "x"

    def test_cancel_owner_only(self):
        q = InputQueue()
        item = q.enqueue("owner", "mine")
        assert q.cancel("intruder", item["id"]) is False
        assert q.cancel("owner", item["id"]) is True
        assert q.pending_count() == 0

    def test_on_change_fires_outside_lock(self):
        events = []
        q = InputQueue(on_change=lambda: events.append(q.pending_count()))
        q.enqueue(None, "a")  # 락 밖 호출이라 pending_count 재진입 가능해야 함
        q.dequeue_nowait()
        assert events == [1, 0]

    def test_web_server_shares_sentinel(self):
        # WebServer.SHUTDOWN is InputQueue.SHUTDOWN — worker 의 identity
        # 비교 계약이 공용화 후에도 유지된다.
        from agent_cli.web.server import WebServer

        assert WebServer.SHUTDOWN is InputQueue.SHUTDOWN


class TestSyntheticInput:
    """합성 입력(에이전트 메일 깨우기)은 **화면 대기열에 뜨지 않는다** (v9.7.0).

    깨우기가 큐를 타는 건 워커의 `dequeue_blocking` 을 푸는 길이 그것뿐이라서
    이지, 사용자가 보낸 게 아니다. 종전엔 닉네임 ``"?"`` 의 대기 메시지로
    입력창 위에 떴다 즉시 사라져, 아무도 보내지 않은 메시지가 대기열에 보였다
    (사용자 지적). 프런트의 "주입됨/취소됨" 영수증 추론도 헛돌았다."""

    def test_hidden_from_snapshot_but_still_delivered(self):
        q = InputQueue()
        q.enqueue("c1", "사람이 보낸 것", nickname="두정")
        q.enqueue(None, "wake", nickname="", system=True)

        # 화면에는 사람 것만.
        assert [i["text"] for i in q.snapshot()] == ["사람이 보낸 것"]
        # 대기 카운트에는 남는다 — idle 리퍼의 "할 일 있나" 신호이고,
        # 깨우기는 실제로 할 일이다. 여기서 빼면 리퍼가 세션을 거둬간다.
        assert q.pending_count() == 2
        # 워커에게는 그대로 전달된다.
        assert q.dequeue_nowait()["text"] == "사람이 보낸 것"
        assert q.dequeue_nowait()["text"] == "wake"

    def test_plain_enqueue_is_not_system(self):
        q = InputQueue()
        q.enqueue("c1", "보통 메시지")
        assert q.snapshot()[0]["system"] is False

    def test_web_server_system_entry_point_marks_it(self):
        """`server.enqueue_system` 이 MailWaker 의 호출 규약(conn_id, text)을
        받으면서 플래그를 붙인다 — 배선이 되돌아가면 여기서 잡힌다."""
        import inspect

        import agent_cli.main as main_mod
        from agent_cli.web.server import WebServer

        src = inspect.getsource(WebServer.enqueue_system)
        assert "system=True" in src
        # main 의 web 배선이 이 진입점을 쓰는지
        assert "enqueue_wake=server.enqueue_system" in inspect.getsource(main_mod)
