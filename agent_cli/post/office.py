"""창구 — 상자들과 장부를 조건변수 하나 아래 함께 바꾼다 (시안 §2·§5·§6).

모든 변경은 이름 있는 메서드 하나다: 그 안에서 배달과 줄 열기·닫기가 한 락 구간으로 일어나고(밖에서는 순서가
보이지 않는다) 기다리는 이를 깨운다. 저장·렌더·콜백은 하지 않는다 — 락 밖에서 호출자가 한다. 조건변수는 주어지면 그것을(입력 큐와 공유), 아니면 기본
`threading.Condition()`(재진입 락)을 쓴다.

hop 상한·요청자 부재·사람 주소 같은 판정은 호출자의 몫이다 — 여기서는 회신 없이 닫으라는 지시(빈 ``replies``)로
받는다.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from agent_cli.post.envelope import Envelope
from agent_cli.post.postbox import BoxError, Postbox
from agent_cli.post.request_book import (
    CLOSES,
    IN_RUN,
    ROW_KIND,
    RequestBook,
    Row,
    RowError,
)

Outcome = Literal["done", "stopped", "wait", "session_end"]


class ConsistencyError(AssertionError):
    """불변식 위반 — 어긴 것의 목록을 든다."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class PostOffice:
    def __init__(self, cv: threading.Condition | None = None) -> None:
        self.cv = cv if cv is not None else threading.Condition()
        self._boxes: dict[str, Postbox] = {}
        self._book = RequestBook()

    # ── 배달 ─────────────────────────────────────────

    def deliver(self, env: Envelope) -> None:
        """``env.dst`` 의 상자에 넣는다 — 닫는 법이 있는 봉투면 그 줄을 queued 로 연다."""
        with self.cv:
            self._ensure_new([env])
            self._deliver(env)
            self.cv.notify_all()

    # ── 런 ───────────────────────────────────────────

    def start_run(self, addr: str) -> tuple[Envelope, ...]:
        """런 하나를 열 봉투를 꺼낸다(사람의 말은 묶음) — 그 줄은 picked. 없으면 빈 튜플. 런 id 는 닫을 때
        (`pay`·`complete`·`end_run`) 받는다 — 집을 때는 필요 없다."""
        with self.cv:
            envs = self._box(addr).take_next()
            self._picked(envs)
            return envs

    def absorb(self, addr: str, *, human_first: bool = False) -> tuple[Envelope, ...]:
        """턴 경계 — 줄 선 것 전부를 지금 런으로(``human_first`` 면 사람의 말 먼저). 그 줄은 picked."""
        with self.cv:
            box = self._box(addr)
            envs = box.take_all_human_first() if human_first else box.take_all()
            self._picked(envs)
            return envs

    def open_human(
        self,
        rid: str,
        src: str,
        dst: str,
        text: str,
        *,
        answers: Iterable[str] = (),
    ) -> Row:
        """입력 큐를 지난 사람의 말(main 의 u-) — 봉투 없이 곧바로 picked 줄. 저장 대상이 아니다(main 앞 사람
        입력은 큐에만 있다 — 시안 §6)."""
        with self.cv:
            row = self._book.open_human(rid, src, dst, text, answers=tuple(answers))
            self.cv.notify_all()
            return row

    def owe(
        self,
        rid: str,
        debtor: str,
        creditor: str,
        text: str,
        *,
        hop: int = 0,
        answers: Iterable[str] = (),
    ) -> Row:
        """봉투 없이 곧바로 집힌 r 줄 — 빚이 상자를 지나지 않고 생겼다. 같은 id 의 줄이 이미 있으면 `RowError`
        (이미 집힌 봉투의 빚인지는 호출자가 `row` 로 먼저 본다). 장부의 r 줄은 저장 대상이 아니다."""
        with self.cv:
            row = self._book.owe(
                rid, debtor, creditor, text, hop=hop, answers=tuple(answers)
            )
            self.cv.notify_all()
            return row

    def take(self, addr: str) -> tuple[Envelope, ...]:
        """줄 선 것 전부를 꺼내고 잊는다 — 런이 들지 않는다(private 주소의 상자: 그 루프가 턴 경계마다 읽는다).
        줄이 있는 봉투가 섞여 있으면 아무것도 바꾸지 않고 `RowError` — 줄을 닫을 길이 없어지기 때문이다."""
        with self.cv:
            box = self._box(addr)
            with_rows = [e.id for e in box.queued() if e.closes_by is not None]
            if with_rows:
                raise RowError(f"take({addr}) would orphan rows: {with_rows}")
            envs = box.take_away()
            self.cv.notify_all()
            return envs

    def pay(self, debtor: str, rid: str, reply_env: Envelope, *, run_id: str) -> None:
        """``debtor`` 가 요청 ``rid`` 를 ``reply_env`` 로 갚는다 — 줄을 닫고 회신을 배달한다(한 락 구간)."""
        with self.cv:
            row = self._book.get(rid)
            if row is None or row.kind != "r" or row.dst != debtor:
                raise RowError(f"{rid}: {debtor} owes no such reply")
            if reply_env.re != rid or reply_env.dst != row.src:
                raise RowError(f"{reply_env.id}: does not answer {rid} to {row.src}")
            self._ensure_new([reply_env])
            self._book.close(rid, "reply", in_run=run_id)
            self._deliver(reply_env)
            self.cv.notify_all()

    def pay_unsent(self, debtor: str, rid: str, *, run_id: str) -> None:
        """``debtor`` 가 요청 ``rid`` 를 갚았는데 회신이 가지 못했다(요청자가 사라졌다) — 줄을 갚음으로 닫아
        그 런 끝까지 남긴다("이미 회신했다" 거부 사유가 사실이어야 한다). 배달할 것은 없다."""
        with self.cv:
            row = self._book.get(rid)
            if row is None or row.kind != "r" or row.dst != debtor:
                raise RowError(f"{rid}: {debtor} owes no such reply")
            self._book.close(rid, "unsent", in_run=run_id)
            self.cv.notify_all()

    def pay_by_message(
        self, debtor: str, to: str, env: Envelope, *, run_id: str
    ) -> Row | None:
        """``debtor`` 가 ``to`` 에게 `message` 를 보낸다 — ``to`` 에게 진 가장 오래된 빚이 있으면 갚음으로 닫고,
        봉투(요청·통지)를 배달한다(한 락 구간). 닫은 줄을 돌려준다."""
        with self.cv:
            self._ensure_new([env])
            row = self._book.owes_oldest(debtor, to)
            if row is not None:
                row = self._book.close(row.rid, "by_message", in_run=run_id)
            self._deliver(env)
            self.cv.notify_all()
            return row

    def complete(
        self, addr: str, ids: Iterable[str], *, run_id: str
    ) -> tuple[str, ...]:
        """``addr`` 의 런이 `complete` 의 ``answers`` 로 닫은 u·n 줄 — 닫은 id(없는 id 는 건너뛴다). 줄은 저장
        대상이 아니다(장부는 q- 만 저장 — 시안 §7)."""
        with self.cv:
            open_ids = {r.rid for r in self._book.open_requests(addr)}
            done = tuple(i for i in ids if i in open_ids)
            for rid in done:
                self._book.close(rid, "completed", in_run=run_id)
            self.cv.notify_all()
            return done

    def end_run(
        self,
        addr: str,
        *,
        run_id: str,
        outcome: Outcome,
        replies: Mapping[str, Envelope] | None = None,
    ) -> None:
        """``addr`` 의 런이 끝났다. 규칙 하나:

        - ``done``·``stopped``: 집힌 r 줄마다 ``replies`` 에 그 rid 가 있으면 그 회신을 배달하고 닫는다(``done`` →
          fallback, ``stopped`` → stopped), 없으면 dropped. 집힌 u·n 줄은 run_ended. 들고 있던 봉투를 비우고,
          남겨 둔 닫힌 줄(명시 회신)을 버린다.
        - ``wait``: 줄은 그대로(잠든 빚), 들고 있던 봉투만 비운다.
        - ``session_end`` [단계 5 에서 쓰임 — 지금 제품은 부르지 않는다]: 아무것도 건드리지 않는다 — 들고 있던 봉투와
          줄은 재시작이 되살린다.

        쓰이지 않을 회신은 받지 않는다: ``replies`` 의 rid 가 이번에 닫는 r 줄이 아니거나, ``wait``·``session_end``
        인데 ``replies`` 가 있으면 아무것도 바꾸지 않고 `RowError`."""
        replies = replies or {}
        with self.cv:
            box = self._box(addr)
            closing = (
                {r.rid for r in self._book.owed_by(addr)}
                if outcome in ("done", "stopped")
                else set()
            )
            self._ensure_used(replies, closing)
            if outcome in ("done", "stopped"):
                self._ensure_new(replies.values())
                how = "fallback" if outcome == "done" else "stopped"
                for row in self._book.owed_by(addr):
                    env = replies.get(row.rid)
                    if env is None:
                        self._book.close(row.rid, "dropped")
                        continue
                    self._book.close(row.rid, how, in_run=run_id)
                    self._deliver(env)
                for row in self._book.open_requests(addr):
                    self._book.close(row.rid, "run_ended", in_run=run_id)
                self._book.purge_closed(addr)
            if outcome != "session_end":
                box.finish_run()
            self.cv.notify_all()

    # ── 정지·버림 ────────────────────────────────────

    def remove_queued(
        self, addr: str, replies: Mapping[str, Envelope] | None = None
    ) -> None:
        """✕ — 줄 선 봉투를 모두 뺀다. r 줄은 ``replies`` 의 회신을 배달하고 removed(회신이 없으면 dropped),
        그 밖에 줄이 있는 봉투(사람의 말 등)는 dropped, 줄 없는 봉투는 그냥 지운다. ``replies`` 의 rid 가 줄 선
        r 줄이 아니면 아무것도 바꾸지 않고 `RowError`."""
        replies = replies or {}
        with self.cv:
            queued_r = {
                e.id
                for e in self._box(addr).queued()
                if (row := self._book.get(e.id)) is not None and row.kind == "r"
            }
            self._ensure_used(replies, queued_r)
            self._ensure_new(replies.values())
            gone = self._box(addr).drop_queued()
            for env in gone:
                if env.closes_by is None:
                    continue
                reply_env = replies.get(env.id)
                row = self._book.get(env.id)
                if row is not None and row.kind == "r" and reply_env is not None:
                    self._book.close(env.id, "removed")
                    self._deliver(reply_env)
                else:
                    self._book.close(env.id, "dropped")
            self.cv.notify_all()

    def take_reply(self, addr: str, rid: str) -> Envelope | None:
        """``addr`` 앞의 요청 ``rid`` 에 대한 회신 하나를 꺼낸다(런이 들지 않는다)."""
        with self.cv:
            env = self._box(addr).take_reply(rid)
            self.cv.notify_all()
            return env

    # ── 질문 ─────────────────────────────────────────

    def ask(
        self, qid: str, asker: str, target: str, text: str, *, meta: Any = None
    ) -> Row:
        """사람에게 묻는다 — q 줄을 연다. 질문은 저장된다(시안 §7). ``meta`` 는 부르는
        쪽의 것(물은 때·런)이다 — 해석하지 않고 `open_questions` 의 줄에 그대로 돌려준다."""
        with self.cv:
            row = self._book.open_question(qid, asker, target, text, meta=meta)
            self.cv.notify_all()
            return row

    def answer(self, qid: str, answer_env: Envelope) -> None:
        """사람의 답 — 질문 줄을 닫고 답 봉투를 묻는 쪽에게 배달한다(한 락 구간)."""
        with self.cv:
            row = self._book.get(qid)
            if row is None or row.kind != "q":
                raise RowError(f"{qid}: no open question")
            if answer_env.dst != row.src:
                raise RowError(f"{answer_env.id}: the answer goes to {row.src}")
            self._ensure_new([answer_env])
            self._book.close(qid, "user_answered")
            self._deliver(answer_env)
            self.cv.notify_all()

    def forget_questions_of(self, asker: str) -> tuple[str, ...]:
        """묻는 쪽이 사라졌다 — 그 질문 줄을 undeliverable 로 닫는다. 닫은 id 를 돌려준다."""
        with self.cv:
            gone = tuple(r.rid for r in self._book.open_questions() if r.src == asker)
            for qid in gone:
                self._book.close(qid, "undeliverable")
            self.cv.notify_all()
            return gone

    # ── 묻기 (락 안 사본) ────────────────────────────

    def queued(self, addr: str) -> tuple[Envelope, ...]:
        with self.cv:
            return self._box_or_empty(addr).queued()

    def taken(self, addr: str) -> tuple[Envelope, ...]:
        with self.cv:
            return self._box_or_empty(addr).taken()

    def arrived(self, addr: str) -> int:
        with self.cv:
            return self._box_or_empty(addr).arrived()

    def backlog(self, addr: str) -> int:
        with self.cv:
            return self._box_or_empty(addr).backlog()

    def has_waking(self, addr: str) -> bool:
        with self.cv:
            return self._box_or_empty(addr).has_waking()

    def owed_by(self, debtor: str) -> tuple[Row, ...]:
        with self.cv:
            return self._book.owed_by(debtor)

    def awaited_by(self, requester: str) -> tuple[Row, ...]:
        with self.cv:
            return self._book.awaited_by(requester)

    def owes_oldest(self, debtor: str, to: str) -> Row | None:
        with self.cv:
            return self._book.owes_oldest(debtor, to)

    def open_requests(self, addr: str) -> tuple[Row, ...]:
        with self.cv:
            return self._book.open_requests(addr)

    def open_questions(self) -> tuple[Row, ...]:
        with self.cv:
            return self._book.open_questions()

    def row(self, rid: str) -> Row | None:
        with self.cv:
            return self._book.get(rid)

    def rows(self) -> tuple[Row, ...]:
        with self.cv:
            return self._book.rows()

    def addresses(self) -> tuple[str, ...]:
        with self.cv:
            return tuple(self._boxes)

    # ── 불변식 ───────────────────────────────────────

    def check_consistency(self) -> None:
        """불변식 I1~I7 — 어기면 `ConsistencyError`. 테스트가 부른다(제품 실행에서는 부르지 않는다)."""
        with self.cv:
            problems: list[str] = []
            where: dict[str, list[tuple[str, str, Envelope]]] = {}
            for addr, box in self._boxes.items():
                for place, envs in (("queued", box.queued()), ("taken", box.taken())):
                    for env in envs:
                        where.setdefault(env.id, []).append((addr, place, env))
            rows = {r.rid: r for r in self._book.rows()}
            for eid, spots in where.items():
                if len(spots) > 1:
                    problems.append(f"I4 {eid} is in {len(spots)} places")
                addr, place, env = spots[0]
                if env.dst != addr:
                    problems.append(
                        f"I4 {eid} sits in {addr}'s box but goes to {env.dst}"
                    )
                if env.closes_by is None:
                    continue
                row = rows.get(eid)
                if row is None:
                    problems.append(f"I1 {eid} ({place}) has no row")
                    continue
                if place == "taken" and row.state == "queued":
                    problems.append(f"I6 {eid} is taken but its row is still queued")
                if row.state == "closed" and (
                    place != "taken" or row.how not in IN_RUN
                ):
                    problems.append(
                        f"I1 {eid} ({place}) row closed by {row.how} — only a row closed inside "
                        "the run that holds it may outlive its close"
                    )
                if place == "queued" and row.state != "queued":
                    problems.append(f"I2 {eid} is queued but its row is {row.state}")
                if (row.src, row.dst) != (env.src, env.dst):
                    problems.append(f"I7 {eid} row and envelope disagree on src/dst")
            for rid, row in rows.items():
                spots = where.get(rid, [])
                if row.kind == "q" and spots:
                    problems.append(f"I3 question {rid} has an envelope")
                if row.state == "queued" and not any(
                    p == "queued" and a == row.dst for a, p, _ in spots
                ):
                    problems.append(
                        f"I2 {rid} is queued but no envelope waits in {row.dst}"
                    )
                if row.state == "closed":
                    if row.how not in CLOSES.get((row.kind, "picked"), frozenset()) | (
                        CLOSES.get((row.kind, "queued"), frozenset())
                    ):
                        problems.append(f"I5 {rid} closed by {row.how}")
                    if row.how in IN_RUN and not row.closed_in_run:
                        problems.append(f"I5 {rid} closed by {row.how} outside a run")
            if problems:
                raise ConsistencyError(problems)

    # ── 안쪽 (cv 를 쥔 채로) ─────────────────────────

    def _box(self, addr: str) -> Postbox:
        box = self._boxes.get(addr)
        if box is None:
            box = self._boxes[addr] = Postbox()
        return box

    def _box_or_empty(self, addr: str) -> Postbox:
        return self._boxes.get(addr) or Postbox()

    @staticmethod
    def _ensure_used(replies: Mapping[str, Envelope], closing: set[str]) -> None:
        """회신마다 이번에 닫는 줄이 있어야 한다 — 쓰이지 않을 회신을 말없이 버리지 않는다."""
        unused = sorted(set(replies) - closing)
        if unused:
            raise RowError(f"replies for rows this call does not close: {unused}")

    def _ensure_new(self, envs: Iterable[Envelope]) -> None:
        """배달할 봉투들을 **바꾸기 전에** 검사한다 — 한 메서드는 전부 하거나 하나도 안 한다."""
        seen: set[str] = set()
        for env in envs:
            if env.id in seen or any(b.holds(env.id) for b in self._boxes.values()):
                raise BoxError(f"{env.id}: already in a box")
            seen.add(env.id)
            if env.closes_by is not None:
                if (env.kind, env.closes_by) not in ROW_KIND:
                    raise RowError(f"no row for a {env.kind}/{env.closes_by} envelope")
                if self._book.get(env.id) is not None:
                    raise RowError(f"{env.id}: a row with this id already exists")

    def _deliver(self, env: Envelope) -> None:
        """검사를 마친 봉투를 넣는다(`_ensure_new` 뒤) — 닫는 법이 있으면 줄을 연다."""
        if env.closes_by is not None:
            self._book.open(env)
        self._box(env.dst).put(env)

    def _picked(self, envs: tuple[Envelope, ...]) -> None:
        """꺼낸 봉투들의 줄을 picked 로 — 기다리는 이를 깨운다."""
        for env in envs:
            if env.closes_by is not None:
                self._book.pick(env.id)
        self.cv.notify_all()
