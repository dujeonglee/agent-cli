"""상자 — 주소 하나 앞의 봉투 (시안 §3).

``queued`` 는 도착 순서로 줄 선 봉투, ``taken`` 은 런이 꺼내 들고 있는 봉투(런이 끝나면 빈다). 락도 조건변수도
없다 — `PostOffice` 의 조건변수 아래서만 불린다.
"""

from __future__ import annotations

from agent_cli.post.envelope import Envelope, is_human_words


class BoxError(ValueError):
    """위치 전이표에 없는 칸 — 같은 id 의 봉투가 이미 있다 등."""


class Postbox:
    def __init__(self) -> None:
        self._queued: list[Envelope] = []
        self._taken: list[Envelope] = []

    # ── 넣기·꺼내기 ─────────────────────────────────

    def put(self, env: Envelope) -> None:
        """줄 끝에 넣는다."""
        if self.holds(env.id):
            raise BoxError(f"{env.id}: already in this box")
        self._queued.append(env)

    def take_next(self) -> Envelope | None:
        """런 하나를 열 봉투 — 첫 **깨우는** 봉투 하나. 나머지는(깨우지 않는 것도) 줄에 남는다 — 그 런의 첫 흡수가
        도착 순서로 읽는다. 없으면 None. 줄 없는 사람의 말(main 앞의 말)은 상자에서 빼기만 한다(런이 들지 않는다) —
        런 끝에 닫을 줄이 없고, 명령이면 런도 열리지 않는다."""
        first = next((i for i, e in enumerate(self._queued) if e.wakes), None)
        if first is None:
            return None
        env = self._queued[first]
        if is_human_words(env) and env.closes_by is None:
            return self._queued.pop(first)
        return self._move([first])[0]

    def take_all(self) -> tuple[Envelope, ...]:
        """줄 선 것 전부 — 깨우지 않는 것도, 도착 순서로(턴 경계의 흡수)."""
        return self._move(range(len(self._queued)))

    def remove(self, env_id: str) -> Envelope | None:
        """줄 선 봉투 하나를 id 로 뺀다. 없으면 None."""
        for i, e in enumerate(self._queued):
            if e.id == env_id:
                return self._queued.pop(i)
        return None

    def take_reply(self, rid: str) -> Envelope | None:
        """요청 ``rid`` 에 대한 회신 하나를 상자에서 꺼낸다(런이 들지 않는다 — `@agent` 명령의 답)."""
        for i, e in enumerate(self._queued):
            if e.re == rid:
                return self._queued.pop(i)
        return None

    def remove_all(self) -> tuple[Envelope, ...]:
        """줄 선 것을 모두 상자에서 빼 돌려준다 — 런이 들지 않는다(taken 에 두지 않음)."""
        out, self._queued = tuple(self._queued), []
        return out

    def finish_run(self) -> tuple[Envelope, ...]:
        """런이 끝났다 — 들고 있던 봉투를 비우고 돌려준다."""
        out, self._taken = tuple(self._taken), []
        return out

    # ── 묻기 ────────────────────────────────────────

    def queued(self) -> tuple[Envelope, ...]:
        return tuple(self._queued)

    def taken(self) -> tuple[Envelope, ...]:
        return tuple(self._taken)

    def holds(self, env_id: str) -> bool:
        return any(e.id == env_id for e in self._queued) or any(
            e.id == env_id for e in self._taken
        )

    def arrivals(self, *, waking_only: bool, words: bool) -> tuple[Envelope, ...]:
        """줄 선 봉투 — 도착 순서. ``waking_only`` 면 깨우는 것만, ``words`` 가 거짓이면 사람의 말은 뺀다."""
        return tuple(
            e
            for e in self._queued
            if (e.wakes or not waking_only) and (words or not is_human_words(e))
        )

    def count(self, *, waking_only: bool, words: bool) -> int:
        """`arrivals` 의 수."""
        return len(self.arrivals(waking_only=waking_only, words=words))

    def words(self) -> tuple[Envelope, ...]:
        """줄 선 사람의 말 — 도착 순서."""
        return tuple(e for e in self._queued if is_human_words(e))

    def mail(self) -> tuple[Envelope, ...]:
        """줄 선 것 가운데 사람의 말이 아닌 것 — 도착 순서."""
        return tuple(e for e in self._queued if not is_human_words(e))

    # ── 안쪽 ────────────────────────────────────────

    def _move(self, indexes) -> tuple[Envelope, ...]:
        idx = list(indexes)
        moved = tuple(self._queued[i] for i in idx)
        for i in sorted(idx, reverse=True):
            del self._queued[i]
        self._taken.extend(moved)
        return moved
