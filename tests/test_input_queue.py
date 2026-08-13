"""공용 InputQueue 계약 (teammate P5) — web WebServer 와 CLI run 펌프가
공유하는 골격. spectator-facing snapshot shape
{id, conn_id, nickname, text}는 web의 큐 표시·cancel 소유권 계약이라 고정하고,
worker item만 P1 capability metadata를 추가로 운반한다."""

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

    def test_capability_metadata_reaches_worker_but_not_queue_snapshot(self):
        q = InputQueue()
        q.enqueue(
            "c1",
            "write it",
            write_paths=["mine.txt"],
            expected_contents={"mine.txt": "exact"},
        )
        visible = q.snapshot()[0]
        assert "write_paths" not in visible and "expected_contents" not in visible
        item = q.dequeue_nowait()
        assert item["write_paths"] == ["mine.txt"]
        assert item["expected_contents"] == {"mine.txt": "exact"}

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
