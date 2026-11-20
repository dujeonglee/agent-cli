"""`MainInput` — main 앞 사람의 말. 항목 모양 ``{id, conn_id, nickname, text, images}`` 는 웹 대기열 표시·취소
소유권·런의 귀속이 읽는 계약이라 필드 고정."""

from __future__ import annotations

import threading

from agent_cli.main_input import CLOSED, MainInput
from agent_cli.post import envelope as post_envelope


class TestMainInput:
    def test_fifo_and_item_shape(self):
        q = MainInput()
        q.enqueue("c1", "first", nickname="bob", images=[{"path": "a.png"}])
        q.enqueue(None, "second")
        a = q.wait_to_open(1)
        b = q.wait_to_open(1)
        assert a == {
            "id": a["id"],
            "conn_id": "c1",
            "nickname": "bob",
            "text": "first",
            "images": [{"path": "a.png"}],
        }
        assert b["text"] == "second" and b["conn_id"] == "" and b["nickname"] is None
        assert a["id"].startswith("u-") and a["id"] != b["id"]

    def test_timeout_returns_none(self):
        assert MainInput().wait_to_open(0.05) is None

    def test_close_wins_over_queued_words(self):
        q = MainInput()
        q.enqueue(None, "queued-before-close")
        q.close()
        assert q.wait_to_open(1) is CLOSED  # 줄 선 말로 런을 열지 않는다
        assert q.pending_count() == 1
        q.close()
        assert q.wait_to_open(1) is CLOSED  # 멱등

    def test_close_wakes_a_blocked_consumer(self):
        q = MainInput()
        got = []
        started = threading.Event()

        def consumer():
            started.set()
            got.append(q.wait_to_open(None))

        t = threading.Thread(target=consumer)
        t.start()
        assert started.wait(2)
        q.close()
        t.join(timeout=2)
        assert got == [CLOSED]

    def test_open_and_snapshot(self):
        q = MainInput()
        assert q.wait_to_open(0) is None
        q.enqueue(None, "x")
        assert [i["text"] for i in q.snapshot()] == ["x"]
        assert q.pending_count() == 1
        assert q.wait_to_open(0)["text"] == "x"

    def test_cancel_owner_only(self):
        q = MainInput()
        item = q.enqueue("owner", "mine")
        assert q.cancel("intruder", item["id"]) is False
        assert q.cancel("owner", item["id"]) is True
        assert q.pending_count() == 0

    def test_on_change_fires_outside_the_lock(self):
        events = []
        q = MainInput(on_change=lambda: events.append(q.pending_count()))
        q.enqueue(None, "a")  # 락 밖 호출이라 pending_count 재진입 가능해야 한다
        q.wait_to_open(0)
        assert events == [1, 0]

    def test_words_stand_without_a_row(self):
        q = MainInput()
        item = q.enqueue(None, "x")
        (queued,) = q.post.words("main")
        assert queued.id == item["id"]
        assert queued.closes_by is None  # 줄 없이 선다 — 취소·명령으로 빠질 수 있다

    def test_mail_in_the_same_box_is_not_words(self):
        q = MainInput()
        q.post.deliver(post_envelope.notice("n-1", "job", "main", "done"))
        q.enqueue(None, "hi")
        assert [i["text"] for i in q.snapshot()] == ["hi"] and q.pending_count() == 1
        assert q.wait_to_open(0)["text"] == "hi" and q.pending_count() == 0
