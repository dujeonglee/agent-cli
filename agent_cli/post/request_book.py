"""요청 장부 — 기다리는 상대가 있는 의무의 줄 (시안 §1·§3).

줄 하나 = 누군가 기다린다: u-(사람이 기다림), r-(에이전트·main 이 회신을 기다림), n-(main 앞 하니스 통지 —
main 의 답을 받을 사람이 기다림), q-(에이전트가 사람의 답을 기다림). 회신·통지·체크인은 줄이 없다.
줄의 종류는 봉투 머리(``kind``·``closes_by``)에서만 정한다 — 주소·id 문자열을 해석하지 않는다.

상태: ``queued``(봉투가 받는 쪽 상자에 줄 섰다) → ``picked``(빚으로 적혔다 — 런이 집었다) → 닫힘. 닫힌 줄은
쌓지 않는다: 런 안에서 닫힌 것(:data:`IN_RUN` — 명시 회신·갚음·complete)만 그 빚진 쪽의 런이 끝날 때까지 남고
(:meth:`RequestBook.purge_closed` — "이미 갚았다" 거부 문구가 읽는 구간), 런 밖에서 닫힌 것(removed·dropped·질문의 답 등)은 바로 지운다. `wait` 로 끝난 런은 비우지 않는다 —
잠든 빚과 함께 다음 런으로 간다. 락이 없다 — `PostOffice` 의 조건변수 아래서만 바꾼다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from agent_cli.post.envelope import Envelope

RowKind = Literal["u", "n", "r", "q"]
State = Literal["queued", "picked", "closed"]
How = Literal[
    "reply",
    "unsent",
    "fallback",
    "by_message",
    "stopped",
    "removed",
    "dropped",
    "completed",
    "run_ended",
    "user_answered",
    "undeliverable",
    "died",
]

#: 봉투 머리 → 줄 종류. 표에 없는 조합은 줄을 열지 않는다(`RowError`).
ROW_KIND: dict[tuple[str, str], RowKind] = {
    ("request", "reply"): "r",
    ("request", "complete"): "u",
    ("system", "complete"): "n",
}

#: (줄 종류, 상태) → 닫을 수 있는 방법. 없는 칸은 `RowError`.
CLOSES: dict[tuple[RowKind, State], frozenset[str]] = {
    ("r", "queued"): frozenset({"removed", "dropped", "died"}),
    ("u", "queued"): frozenset({"dropped", "died"}),
    ("n", "queued"): frozenset({"dropped", "died"}),
    ("r", "picked"): frozenset(
        {"reply", "unsent", "fallback", "by_message", "stopped", "dropped", "died"}
    ),
    ("u", "picked"): frozenset({"completed", "run_ended", "dropped"}),
    ("n", "picked"): frozenset({"completed", "run_ended", "dropped"}),
    ("q", "picked"): frozenset({"user_answered", "undeliverable"}),
}

#: 런 안에서 닫는 방법 — 닫힌 줄의 ``closed_in_run`` 이 비면 안 된다(불변식 I5)
IN_RUN: frozenset[str] = frozenset(
    {"reply", "unsent", "by_message", "completed", "fallback", "stopped", "run_ended"}
)

#: 닫힌 뒤에도 그 빚진 쪽의 런 끝까지 남기는 방법 — 런 안에서 닫는 것 전부. "이미 갚았다" 거부 사유가
#: 읽고, 그 런이 아직 든 봉투의 줄이 된다(불변식 I1)
RETAINED: frozenset[str] = IN_RUN


class RowError(ValueError):
    """전이표에 없는 칸 — 상태·종류·방법이 맞지 않는다."""


@dataclass(frozen=True)
class Row:
    """줄 하나 — 불변. 바뀔 때마다 새 값으로 갈아 끼운다."""

    rid: str
    src: str  # 기다리는 쪽(요청자·묻는 에이전트)
    dst: str  # 빚진 쪽(받는 쪽)
    kind: RowKind
    text: str
    answers: tuple[str, ...] = ()
    hop: int = 0
    state: State = "queued"
    how: How | None = None
    closed_in_run: str = ""
    #: 부르는 쪽의 것 — 장부는 읽지도 비교하지도 않고 그대로 돌려준다(질문의 물은 때·런 등, D3)
    meta: Any = field(default=None, compare=False)


class RequestBook:
    """줄 모음 — 연 순서를 지킨다. 바꾸는 것은 이름 있는 메서드, 묻는 것은 이름 있는 질문뿐."""

    def __init__(self) -> None:
        self._rows: dict[str, Row] = {}

    # ── 바꾸기 ───────────────────────────────────────

    def open(self, env: Envelope) -> Row:
        """봉투가 상자에 들었다 — 그 줄을 queued 로 연다."""
        kind = ROW_KIND.get((env.kind, env.closes_by or ""))
        if kind is None:
            raise RowError(f"no row for a {env.kind}/{env.closes_by} envelope")
        return self._add(
            Row(env.id, env.src, env.dst, kind, env.text, env.answers, env.hop)
        )

    def open_human(self, rid: str, src: str, dst: str, text: str, *, answers=()) -> Row:
        """봉투 없이 곧바로 집힌 사람의 말(u-) — main 의 입력은 입력 큐를 지나 상자에 들지 않는다."""
        return self._add(Row(rid, src, dst, "u", text, tuple(answers), state="picked"))

    def owe(
        self,
        rid: str,
        debtor: str,
        creditor: str,
        text: str,
        *,
        hop: int = 0,
        answers=(),
    ) -> Row:
        """봉투 없이 곧바로 집힌 r 줄 — ``debtor`` 가 ``creditor`` 에게 `reply` 를 빚진다(봉투가 상자를 지나지
        않은 빚: main 이 관찰로 읽은 요청, 테스트가 세운 빚). 같은 id 의 줄이 있으면 `RowError`."""
        return self._add(
            Row(rid, creditor, debtor, "r", text, tuple(answers), hop, state="picked")
        )

    def open_question(
        self, qid: str, asker: str, target: str, text: str, *, meta: Any = None
    ) -> Row:
        """에이전트가 사람에게 묻는다(q-) — 봉투 없이 picked. 사람의 답이 닫는다. ``meta`` 는 해석하지 않는다."""
        return self._add(Row(qid, asker, target, "q", text, state="picked", meta=meta))

    def pick(self, rid: str) -> Row:
        """런이 집었다 — queued → picked."""
        row = self._need(rid)
        if row.state != "queued":
            raise RowError(f"{rid}: cannot pick a {row.state} row")
        return self._put(replace(row, state="picked"))

    def close(self, rid: str, how: How, *, in_run: str = "") -> Row:
        """닫는다 — 표(:data:`CLOSES`)의 칸만. 런 안에서 닫힌 것(:data:`RETAINED`)만 남기고 나머지는 지운다."""
        row = self._need(rid)
        allowed = CLOSES.get((row.kind, row.state), frozenset())
        if how not in allowed:
            raise RowError(f"{rid}: a {row.state} {row.kind} row cannot close by {how}")
        if how in IN_RUN and not in_run:
            raise RowError(f"{rid}: {how} closes inside a run — in_run is required")
        closed = replace(row, state="closed", how=how, closed_in_run=in_run)
        if how in RETAINED:
            self._put(closed)
        else:
            del self._rows[rid]
        return closed

    def purge_closed(self, debtor: str) -> tuple[str, ...]:
        """``debtor`` 의 런이 끝났다 — 그 런 안에서 닫혀 남겨 둔 줄을 버린다. 버린 id."""
        gone = tuple(
            r.rid
            for r in self._rows.values()
            if r.state == "closed" and r.dst == debtor
        )
        for rid in gone:
            del self._rows[rid]
        return gone

    # ── 묻기 ────────────────────────────────────────

    def get(self, rid: str) -> Row | None:
        return self._rows.get(rid)

    def owed_by(self, debtor: str) -> tuple[Row, ...]:
        """``debtor`` 가 `reply` 를 빚진 집힌 r 줄 — 연 순서(장부의 Owed Replies·런 끝 폴백 대상)."""
        return tuple(
            r
            for r in self._rows.values()
            if r.kind == "r" and r.state == "picked" and r.dst == debtor
        )

    def awaited_by(self, requester: str) -> tuple[Row, ...]:
        """``requester`` 가 보내 놓고 회신을 기다리는 r 줄(닫히지 않은 것) — 연 순서. 줄 선 것도 든다:
        빚으로 적히기 전에도 기다림은 있다(`wait` 의 근거)."""
        return tuple(
            r
            for r in self._rows.values()
            if r.kind == "r" and r.state != "closed" and r.src == requester
        )

    def owes_oldest(self, debtor: str, to: str) -> Row | None:
        """``debtor`` 가 ``to`` 에게 빚진 가장 오래된 집힌 r 줄 — 상대만 고르는 갚기(`message`)."""
        return next(
            (r for r in self.owed_by(debtor) if r.src == to),
            None,
        )

    def open_requests(self, addr: str) -> tuple[Row, ...]:
        """``addr`` 의 런이 `complete` 의 ``answers`` 로 닫을 집힌 u·n 줄 — 연 순서(Open Requests)."""
        return tuple(
            r
            for r in self._rows.values()
            if r.kind in ("u", "n") and r.state == "picked" and r.dst == addr
        )

    def open_questions(self) -> tuple[Row, ...]:
        """사람의 답을 기다리는 q 줄 — 연 순서."""
        return tuple(r for r in self._rows.values() if r.kind == "q")

    def rows(self) -> tuple[Row, ...]:
        """줄 전부(남겨 둔 닫힌 것 포함) — 검사·비교용 사본."""
        return tuple(self._rows.values())

    # ── 안쪽 ────────────────────────────────────────

    def _need(self, rid: str) -> Row:
        row = self._rows.get(rid)
        if row is None:
            raise RowError(f"{rid}: no such row")
        if row.state == "closed":
            raise RowError(f"{rid}: already closed by {row.how}")
        return row

    def _add(self, row: Row) -> Row:
        if row.rid in self._rows:
            raise RowError(f"{row.rid}: a row with this id already exists")
        return self._put(row)

    def _put(self, row: Row) -> Row:
        self._rows[row.rid] = row
        return row
