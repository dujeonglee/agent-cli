"""`MainInput`(창구의 main 상자) 가 `InputQueue` 와 같은 것을 보이는가 — 같은 연산 열을 둘에 주고 관찰값을 맞춘다.

연산 열은 짧은 것 전부(순열)다. 항목 id 는 할당기 하나를 둘이 번갈아 쓰므로 첫 등장 순서로 바꿔 비교한다.
`InputQueue` 의 ``system`` 키는 새 항목에 없다(합성 입력이 없다) — 빼고 비교한다.
"""

from __future__ import annotations

import itertools

import pytest

from agent_cli.input_queue import InputQueue
from agent_cli.main_input import CLOSED, MainInput


class _Old:
    def __init__(self):
        self.q = InputQueue()

    def enqueue(self, *a, **k):
        return self.q.enqueue(*a, **k)

    def take_next(self):
        return self.q.dequeue_nowait()

    def take_first_if(self, pred):
        return self.q.take_first_human_if(pred)

    def put_back_first(self, item):
        self.q.put_back_first(item)

    def cancel(self, conn, mid):
        return self.q.cancel(conn, mid)

    def snapshot(self):
        return self.q.snapshot()

    def pending_count(self):
        return self.q.pending_count()

    def wait_for_arrival(self, t):
        return self.q.wait_for_arrival(t)

    def open(self):
        got = self.q.dequeue_blocking(timeout=0)
        return CLOSED if got is InputQueue.SHUTDOWN else got

    def close(self):
        self.q.shutdown()

    def check(self):
        pass


class _New:
    def __init__(self):
        self.m = MainInput()

    def enqueue(self, *a, **k):
        return self.m.enqueue(*a, **k)

    def take_next(self):
        return self.m.take_next()

    def take_first_if(self, pred):
        return self.m.take_first_if(pred)

    def put_back_first(self, item):
        self.m.put_back_first(item)

    def cancel(self, conn, mid):
        return self.m.cancel(conn, mid)

    def snapshot(self):
        return self.m.snapshot()

    def pending_count(self):
        return self.m.pending_count()

    def wait_for_arrival(self, t):
        return self.m.wait_for_arrival(t)

    def open(self):
        return self.m.wait_to_open(0)

    def close(self):
        self.m.close()

    def check(self):
        self.m.post.check_consistency()


def _plain(v):
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items() if k != "system"}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def _is_command(text: str) -> bool:
    return text[:1] in ("/", "@")


OPS = (
    "say_dj",
    "say_cmd",
    "say_img",
    "take",
    "cancel_mine",
    "cancel_other",
    "first_cmd",
    "put_back",
    "open",
    "close",
)


def _run(impl, ops) -> list:
    out: list = []
    first_id = None
    held = None
    for op in ops:
        if op == "say_dj":
            got = impl.enqueue("c1", "hello", nickname="dj")
        elif op == "say_cmd":
            got = impl.enqueue(None, "/noop", nickname=None)
        elif op == "say_img":
            got = impl.enqueue("c2", "look", nickname="ann", images=[{"path": "a.png"}])
        elif op == "take":
            got = impl.take_next()
        elif op == "cancel_mine":
            got = impl.cancel("c1", first_id or "")
        elif op == "cancel_other":
            got = impl.cancel("c9", first_id or "")
        elif op == "first_cmd":
            got = held = impl.take_first_if(_is_command)
        elif op == "put_back":
            got = None
            if held is not None:
                impl.put_back_first(held)
                held = None
        elif op == "open":
            got = impl.open()
            got = "CLOSED" if got is CLOSED else got
        else:
            impl.close()
            got = None
        if op.startswith("say") and first_id is None:
            first_id = got["id"]
        impl.check()
        out.append(
            (
                op,
                _plain(got),
                _plain(impl.snapshot()),
                impl.pending_count(),
                impl.wait_for_arrival(0),
            )
        )
    return out


def _ids_by_first_seen(rows) -> list:
    """id 를 첫 등장 순서 번호로 — 두 구현이 할당기를 번갈아 써도 같은 판."""
    seen: dict = {}

    def norm(v):
        if isinstance(v, dict):
            return {
                k: (seen.setdefault(x, len(seen)) if k == "id" else norm(x))
                for k, x in v.items()
            }
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        return v

    return norm(rows)


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_every_sequence_looks_the_same(n):
    for ops in itertools.product(OPS, repeat=n):
        old = _ids_by_first_seen(_run(_Old(), ops))
        new = _ids_by_first_seen(_run(_New(), ops))
        assert new == old, ops
