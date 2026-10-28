"""배달 id 할당기 (v10.77.0) — id 가 곧 키다.

- 번호 공간 하나를 모든 접두(u/n/r/q)가 함께 쓴다 — 단조 증가, 겹침 없음.
- 할당기는 저장하지 않는다. 다시 열면 남은 id 를 :func:`observe` 해 번호를 잇는다
  (history 의 ``request_id``, agents.json 의 미배달 메일·질문·받은편지함).
- 메일 한 통의 id 는 종류대로 접두를 받는다(질문 ``q-``·회신 기대 ``r-``·그 밖 ``n-``).
"""

from __future__ import annotations

import threading

import pytest

from agent_cli import item_ids
from agent_cli.context.manager import ContextManager
from agent_cli.loop.core import mail_item
from agent_cli.subagent.agents_live import AgentRegistry


class TestAllocator:
    def test_prefixes_share_one_number_space(self):
        got = [
            item_ids.next_id(item_ids.USER),
            item_ids.next_id(item_ids.REPLY),
            item_ids.next_id(item_ids.MAIL),
            item_ids.next_id(item_ids.QUESTION),
        ]
        assert got == ["u-1", "r-2", "n-3", "q-4"]

    def test_unknown_prefix_is_refused(self):
        with pytest.raises(ValueError):
            item_ids.next_id("x")

    def test_concurrent_allocation_never_repeats(self):
        out: list[str] = []
        lock = threading.Lock()

        def take():
            for _ in range(50):
                i = item_ids.next_id(item_ids.MAIL)
                with lock:
                    out.append(i)

        ts = [threading.Thread(target=take) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert len(set(out)) == len(out) == 400

    def test_observe_continues_past_the_highest_seen(self):
        item_ids.observe("u-3", "r-17", "q-7a3f", None, "", "n-2")
        assert item_ids.next_id(item_ids.USER) == "u-18"
        item_ids.observe("u-5")  # 더 작은 번호는 되돌리지 않는다
        assert item_ids.next_id(item_ids.USER) == "u-19"

    @pytest.mark.parametrize(
        ("raw", "num"), [("r-17", 17), ("q-7a3f", 0), ("", 0), (None, 0), ("17", 0)]
    )
    def test_number_of(self, raw, num):
        assert item_ids.number_of(raw) == num


class TestResumeContinuesNumbers:
    def test_history_request_ids_are_observed_on_resume(self, tmp_path):
        """지난 생의 user 레코드가 단 ``request_id`` 와 새 id 가 겹치면, 새 요청이
        그 옛 레코드를 압축 핀으로 붙잡는다."""
        ctx = ContextManager(tmp_path / "s", max_context_tokens=10_000)
        ctx.add({"role": "user", "content": "a", "request_id": "u-9"})
        ctx.add({"role": "assistant", "content": "b"})
        item_ids._last = 0  # 새 프로세스
        ContextManager(tmp_path / "s", max_context_tokens=10_000, resume=True)
        assert item_ids.next_id(item_ids.USER) == "u-10"

    def test_pending_mail_and_questions_are_observed_on_restore(self, tmp_path):
        reg = AgentRegistry(tmp_path)
        reg._push_reply({"kind": "job", "output": "done"})  # n-1
        reg._push_reply({"kind": "job", "output": "done 2"})  # n-2
        item_ids._last = 0
        fresh = AgentRegistry(tmp_path)
        fresh.restore()
        assert [m["item_id"] for m in fresh._pending] == ["n-1", "n-2"]
        assert item_ids.next_id(item_ids.MAIL) == "n-3"


class TestMailIdsByKind:
    def test_prefix_follows_the_kind(self, tmp_path):
        reg = AgentRegistry(tmp_path)
        reg._push_reply({"kind": "job", "output": "x"})
        reg._push_reply(
            {
                "kind": "peer_message",
                "key": "agt-a",
                "output": "y",
                "expects_reply": True,
            }
        )
        reg._push_reply({"kind": "question", "id": "q-40", "output": "z"})
        assert [m["item_id"] for m in reg._pending] == ["n-1", "r-2", "q-40"]

    def test_mail_item_stamps_an_unstamped_delivery_once(self):
        reply = {"kind": "job", "output": "x"}
        assert mail_item(reply) == ("n-1", "mail")
        assert mail_item(reply) == ("n-1", "mail")  # 같은 메일은 같은 id
