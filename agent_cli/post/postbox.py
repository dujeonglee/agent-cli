"""상자 — 주소 하나 앞의 봉투 (시안 §3).

``queued`` 는 도착 순서로 줄 선 봉투, ``taken`` 은 런이 꺼내 들고 있는 봉투(런이 끝나면 빈다). 락도 조건변수도
없다 — `PostOffice` 의 조건변수 아래서만 불린다. ``close`` 는 표지만 세운다: 남은 봉투를 읽을지는 호출자가 정한다.
"""

from __future__ import annotations

from agent_cli.post.envelope import Envelope, is_human_words


class BoxError(ValueError):
    """위치 전이표에 없는 칸 — 같은 id 의 봉투가 이미 있다 등."""


class Postbox:
    def __init__(self) -> None:
        self._queued: list[Envelope] = []
        self._taken: list[Envelope] = []
        self.closed = False

    # ── 넣기·꺼내기 ─────────────────────────────────

    def put(self, env: Envelope) -> None:
        """줄 끝에 넣는다 — 닫힌 상자도 받는다(남은 메일은 저장돼 다음에 읽힌다)."""
        if self.holds(env.id):
            raise BoxError(f"{env.id}: already in this box")
        self._queued.append(env)

    def take_next(self) -> tuple[Envelope, ...]:
        """런 하나를 열 봉투 — 첫 **깨우는** 봉투, 그것이 사람의 말이면 바로 뒤이은 사람의 말까지 한 묶음.
        깨우지 않는 봉투는 줄에 남는다(그 런의 첫 흡수가 읽는다). 없으면 빈 튜플."""
        first = next((i for i, e in enumerate(self._queued) if e.wakes), None)
        if first is None:
            return ()
        picked = [first]
        if is_human_words(self._queued[first]):
            for i in range(first + 1, len(self._queued)):
                if not is_human_words(self._queued[i]) or not self._queued[i].wakes:
                    break
                picked.append(i)
        return self._move(picked)

    def take_all(self) -> tuple[Envelope, ...]:
        """줄 선 것 전부 — 깨우지 않는 것도, 도착 순서로(턴 경계의 흡수)."""
        return self._move(range(len(self._queued)))

    def take_all_human_first(self) -> tuple[Envelope, ...]:
        """줄 선 것 전부 — 사람의 말 먼저, 나머지는 도착 순서(main 의 주입: 사람 먼저, 메일 뒤)."""
        order = [i for i, e in enumerate(self._queued) if is_human_words(e)]
        order += [i for i, e in enumerate(self._queued) if not is_human_words(e)]
        return self._move(order)

    def take_away(self) -> tuple[Envelope, ...]:
        """줄 선 것 전부를 꺼낸다 — 런이 들지 않는다(taken 에 두지 않음)."""
        out, self._queued = tuple(self._queued), []
        return out

    def take_reply(self, rid: str) -> Envelope | None:
        """요청 ``rid`` 에 대한 회신 하나를 상자에서 꺼낸다(런이 들지 않는다 — `@agent` 명령의 답)."""
        for i, e in enumerate(self._queued):
            if e.re == rid:
                return self._queued.pop(i)
        return None

    def drop_queued(self) -> tuple[Envelope, ...]:
        """줄 선 것을 모두 버리고 돌려준다(✕ — 호출자가 줄을 닫는다)."""
        out, self._queued = tuple(self._queued), []
        return out

    def finish_run(self) -> tuple[Envelope, ...]:
        """런이 끝났다 — 들고 있던 봉투를 비우고 돌려준다."""
        out, self._taken = tuple(self._taken), []
        return out

    def discard(self, env_id: str) -> Envelope | None:
        """그 id 의 봉투를 어디서든(queued·taken) 뺀다 — 줄이 회신 없이 닫힐 때(dropped)."""
        for lst in (self._queued, self._taken):
            for i, e in enumerate(lst):
                if e.id == env_id:
                    return lst.pop(i)
        return None

    def close(self) -> None:
        self.closed = True

    # ── 묻기 ────────────────────────────────────────

    def queued(self) -> tuple[Envelope, ...]:
        return tuple(self._queued)

    def taken(self) -> tuple[Envelope, ...]:
        return tuple(self._taken)

    def holds(self, env_id: str) -> bool:
        return any(e.id == env_id for e in self._queued) or any(
            e.id == env_id for e in self._taken
        )

    def arrived(self) -> int:
        """이미 도착해 다음 경계에 배달될 봉투 수 — 깨우지 않는 것도 센다(`wait` 의 "도착분" 판정)."""
        return len(self._queued)

    def backlog(self) -> int:
        """앞에 쌓인 할 일 — 깨우는 봉투만(REQUEST_STACKED). 깨우지 않는 회신은 할 일을 열지 않는다."""
        return sum(1 for e in self._queued if e.wakes)

    def has_waking(self) -> bool:
        return any(e.wakes for e in self._queued)

    # ── 안쪽 ────────────────────────────────────────

    def _move(self, indexes) -> tuple[Envelope, ...]:
        idx = list(indexes)
        moved = tuple(self._queued[i] for i in idx)
        for i in sorted(idx, reverse=True):
            del self._queued[i]
        self._taken.extend(moved)
        return moved
