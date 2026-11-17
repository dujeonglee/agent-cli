"""우편함·장부의 독립 참조 모델 — 시안 v4.1 §3·§4 표와 승인된 결정(D1~D5, 수정 일곱)만 보고 따로 쓴 것.

`agent_cli.post` 를 import 하지 않는다(가드 테스트). 봉투는 평범한 dict 로 받는다 — 키: id·kind·src·dst·re·
closes_by·wakes·persists. 락도 없다. 관찰값은 구현의 공개 질문과 같은 모양으로 낸다: 주소별 queued·taken id 순서,
줄 (rid, state, how), arrived·backlog·has_waking.

틀린 연산(구현이 거절해야 할 것)은 `Refused` 로 알린다 — 모델의 상태는 그대로 둔다.
"""

from __future__ import annotations


class Refused(Exception):
    pass


#: 줄 종류 — 봉투의 (kind, closes_by) 로만
KIND = {
    ("request", "reply"): "r",
    ("request", "complete"): "u",
    ("system", "complete"): "n",
}

#: 런 안에서 닫는 방법 — 그 빚진 쪽의 런이 끝날 때까지 닫힌 채 남는다
IN_RUN = {
    "reply",
    "unsent",
    "completed",
    "fallback",
    "stopped",
    "run_ended",
}


def _human(e: dict) -> bool:
    return e["kind"] == "request" and e["closes_by"] == "complete"


def _may_close(kind: str, state: str, how: str) -> bool:
    if state == "queued":
        return how in (
            {"removed", "dropped", "died"} if kind == "r" else {"dropped", "died"}
        )
    if state != "picked":
        return False
    if kind == "r":
        return how in {
            "reply",
            "unsent",
            "fallback",
            "stopped",
            "dropped",
            "died",
        }
    if kind in ("u", "n"):
        return how in {"completed", "run_ended", "dropped"}
    return how in {"user_answered", "undeliverable"}  # q


class Model:
    def __init__(self) -> None:
        self.queued: dict[str, list[dict]] = {}
        self.taken: dict[str, list[dict]] = {}
        #: rid → {"kind", "src", "dst", "state", "how"}, 연 순서
        self.rows: dict[str, dict] = {}

    # ── 안쪽 ──
    def _q(self, a):
        return self.queued.setdefault(a, [])

    def _t(self, a):
        return self.taken.setdefault(a, [])

    def _anywhere(self, eid):
        return any(
            e["id"] == eid
            for lst in (*self.queued.values(), *self.taken.values())
            for e in lst
        )

    def _close(self, rid, how, in_run=""):
        r = self.rows.get(rid)
        if (
            r is None
            or r["state"] == "closed"
            or not _may_close(r["kind"], r["state"], how)
        ):
            raise Refused(f"close {rid} {how}")
        if how in IN_RUN and not in_run:
            raise Refused(f"{how} needs a run")
        if how in IN_RUN:
            r.update(state="closed", how=how)
        else:
            del self.rows[rid]

    def _put(self, e):
        if self._anywhere(e["id"]):
            raise Refused(f"dup {e['id']}")
        if e["closes_by"] is not None:
            kind = KIND.get((e["kind"], e["closes_by"]))
            if kind is None or e["id"] in self.rows:
                raise Refused(f"row {e['id']}")
            self.rows[e["id"]] = {
                "kind": kind,
                "src": e["src"],
                "dst": e["dst"],
                "state": "queued",
                "how": None,
            }
        self._q(e["dst"]).append(e)

    def _pick_all(self, envs):
        for e in envs:
            if e["closes_by"] is not None:
                self.rows[e["id"]]["state"] = "picked"

    def _owed(self, debtor):
        return [
            rid
            for rid, r in self.rows.items()
            if r["kind"] == "r" and r["state"] == "picked" and r["dst"] == debtor
        ]

    # ── 연산 (구현의 PostOffice 메서드와 같은 이름·뜻) ──
    def deliver(self, e):
        self._put(e)

    def start_run(self, addr):
        q = self._q(addr)
        first = next((i for i, e in enumerate(q) if e["wakes"]), None)
        if first is None:
            return []
        idx = [first]
        if _human(q[first]):
            j = first + 1
            while j < len(q) and _human(q[j]) and q[j]["wakes"]:
                idx.append(j)
                j += 1
        took = [q[i] for i in idx]
        self.queued[addr] = [e for i, e in enumerate(q) if i not in idx]
        self._t(addr).extend(took)
        self._pick_all(took)
        return took

    def absorb(self, addr, human_first=False):
        q = self._q(addr)
        took = (
            ([e for e in q if _human(e)] + [e for e in q if not _human(e)])
            if human_first
            else list(q)
        )
        self.queued[addr] = []
        self._t(addr).extend(took)
        self._pick_all(took)
        return took

    def pay(self, debtor, rid, reply, run_id):
        r = self.rows.get(rid)
        if (
            r is None
            or r["kind"] != "r"
            or r["dst"] != debtor
            or reply["re"] != rid
            or reply["dst"] != r["src"]
            or self._anywhere(reply["id"])
        ):
            raise Refused("pay")
        self._close(rid, "reply", run_id)
        self._put(reply)

    def complete(self, addr, ids, run_id):
        for rid in ids:
            r = self.rows.get(rid)
            if (
                r
                and r["kind"] in ("u", "n")
                and r["state"] == "picked"
                and r["dst"] == addr
            ):
                self._close(rid, "completed", run_id)

    def end_run(self, addr, run_id, outcome, replies):
        closing = set(self._owed(addr)) if outcome in ("done", "stopped") else set()
        if set(replies) - closing:
            raise Refused("unused replies")
        if outcome in ("done", "stopped"):
            for e in replies.values():
                if self._anywhere(e["id"]):
                    raise Refused("dup reply")
            how = "fallback" if outcome == "done" else "stopped"
            for rid in self._owed(addr):
                if rid in replies:
                    self._close(rid, how, run_id)
                    self._put(replies[rid])
                else:
                    self._close(rid, "dropped")
            for rid, r in list(self.rows.items()):
                if (
                    r["kind"] in ("u", "n")
                    and r["state"] == "picked"
                    and r["dst"] == addr
                ):
                    self._close(rid, "run_ended", run_id)
            for rid, r in list(self.rows.items()):
                if r["state"] == "closed" and r["dst"] == addr:
                    del self.rows[rid]
        if outcome != "session_end":
            self.taken[addr] = []

    def remove_queued(self, addr, replies):
        queued_r = {
            e["id"]
            for e in self._q(addr)
            if e["id"] in self.rows and self.rows[e["id"]]["kind"] == "r"
        }
        if set(replies) - queued_r:
            raise Refused("unused replies")
        for e in replies.values():
            if self._anywhere(e["id"]):
                raise Refused("dup reply")
        gone = list(self._q(addr))
        self.queued[addr] = []
        for e in gone:
            if e["closes_by"] is None:
                continue
            if self.rows[e["id"]]["kind"] == "r" and e["id"] in replies:
                self._close(e["id"], "removed")
                self._put(replies[e["id"]])
            else:
                self._close(e["id"], "dropped")

    def owe(self, rid, debtor, creditor):
        if rid in self.rows:
            raise Refused("dup row")
        self.rows[rid] = {
            "kind": "r",
            "src": creditor,
            "dst": debtor,
            "state": "picked",
            "how": None,
        }

    def take(self, addr):
        q = self._q(addr)
        if any(e["closes_by"] is not None for e in q):
            raise Refused("take would orphan rows")
        self.queued[addr] = []
        return q

    def take_reply(self, addr, rid):
        q = self._q(addr)
        for i, e in enumerate(q):
            if e["re"] == rid:
                return q.pop(i)
        return None

    def ask(self, qid, asker, target):
        if qid in self.rows:
            raise Refused("dup question")
        self.rows[qid] = {
            "kind": "q",
            "src": asker,
            "dst": target,
            "state": "picked",
            "how": None,
        }

    def answer(self, qid, env):
        r = self.rows.get(qid)
        if (
            r is None
            or r["kind"] != "q"
            or env["dst"] != r["src"]
            or self._anywhere(env["id"])
        ):
            raise Refused("answer")
        self._close(qid, "user_answered")
        self._put(env)

    def forget_questions_of(self, asker):
        for rid, r in list(self.rows.items()):
            if r["kind"] == "q" and r["src"] == asker:
                self._close(rid, "undeliverable")

    # ── 관찰 ──
    def observe(self, addrs) -> dict:
        return {
            "boxes": {
                a: (
                    [e["id"] for e in self.queued.get(a, [])],
                    [e["id"] for e in self.taken.get(a, [])],
                    len(self.queued.get(a, [])),
                    sum(1 for e in self.queued.get(a, []) if e["wakes"]),
                    any(e["wakes"] for e in self.queued.get(a, [])),
                )
                for a in addrs
            },
            "rows": [(rid, r["state"], r["how"]) for rid, r in self.rows.items()],
        }
