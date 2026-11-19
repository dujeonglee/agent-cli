"""main 앞 사람의 입력 — 창구(`agent_cli.post`)의 ``"main"`` 상자에 봉투로 넣고, 보이고, 주인이 취소하고, 도착을
기다리고, 런을 열 것을 꺼낸다 (`MainInput`).

웹 채팅·`run` 의 argv 질의·제자리 대기 중 친 터미널 줄·예약 발화·StaleGuard 의 Hint 가 모두 여기로 들어온다.
메일과 **같은 상자·같은 조건변수**라, 펌프는 "사람의 말 또는 깨우는 메일" 을 한 자리에서 기다린다
(`PostOffice.wait_to_open`). 사람의 말은 줄(장부) 없이 선다 — 꺼내기 전에는 취소되거나 명령으로 빠질 수 있다.
저장하지 않는다.

화면·펌프에 내주는 항목 모양은 ``{id, conn_id, nickname, text, images}`` 하나다 — 대기열 표시·취소 소유권·
런의 귀속이 이 필드를 읽는다. ``on_change`` 는 상자의 사람 말이 바뀔 때마다 **락 밖에서** 부른다(웹이 대기열을
다시 그린다 — 렌더러 락과 겹치지 않게).
"""

from __future__ import annotations

from collections.abc import Callable

from agent_cli import item_ids
from agent_cli.post.envelope import Envelope, is_human_words, words
from agent_cli.post.office import CLOSED, OPEN_MAIL, PostOffice

__all__ = ["CLOSED", "MAIN", "OPEN_MAIL", "MainInput", "intake_item"]

#: 사람의 입력을 받는 주소
MAIN = "main"


class MainInput:
    def __init__(
        self,
        post: PostOffice | None = None,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """``post`` 가 없으면 새로 만든다 — 세션의 에이전트 레지스트리가 이 창구를 받아 써야 메일과 사람의 말이 한
        상자에 선다."""
        self.post = post if post is not None else PostOffice()
        self.on_change = on_change

    def _changed(self) -> None:
        cb = self.on_change
        if cb is not None:
            cb()

    # ── 넣기 ─────────────────────────────────────────

    def enqueue(
        self,
        conn_id: str | None,
        text: str,
        *,
        nickname=None,
        images: list[dict] | None = None,
    ) -> dict:
        """사람의 말 하나를 상자 끝에 — 그 항목을 돌려준다. id 는 장부 형식(``u-N``, 세션 할당기 하나)이고 그 문자열이
        꼬리·`answers`·기록의 `request_id`·웹 취소까지 같다."""
        env = words(
            item_ids.next_id(item_ids.USER),
            f"user:{nickname}" if nickname else "user",
            MAIN,
            text,
            payload={
                "conn_id": conn_id or "",
                "nickname": nickname,
                "images": list(images or []),
            },
        )
        self.post.deliver(env)
        self._changed()
        return self.item_of(env)

    # ── 꺼내기 ───────────────────────────────────────

    def wait_to_open(self, timeout: float | None):
        """펌프: 다음 런을 열 것 — 사람의 말 항목(꺼냄), `OPEN_MAIL`(깨우는 메일이 있다), `CLOSED`, 또는 None
        (``timeout``). 기다리는 동안 main 은 한가함(`idle`)이다."""
        got = self.post.wait_to_open(MAIN, timeout=timeout)
        if isinstance(got, Envelope):
            self._changed()
            return self.item_of(got)
        return got

    def take_first_if(self, pred: Callable[[str], bool]) -> dict | None:
        """맨 앞의 사람의 말이 ``pred(text)`` 면 꺼내 돌려준다, 아니면 None — main 이 제자리에서 기다리는 동안 친
        명령(``/stop`` 등)을 그 자리에서 라우팅하려고."""
        first = next(iter(self.post.words(MAIN)), None)
        if first is None or not pred(first.text):
            return None
        env = self.post.withdraw(MAIN, first.id)
        if env is None:
            return None  # 그 사이 취소됐다
        self._changed()
        return self.item_of(env)

    def cancel(self, conn_id: str | None, msg_id: str) -> bool:
        """대기 중인 말 하나를 뺀다 — 주인(``conn_id`` 일치)만. 이미 꺼낸 것은 못 뺀다. 뺐는가를 돌려준다."""
        mine = any(
            e.id == msg_id and e.payload.get("conn_id") == (conn_id or "")
            for e in self.post.words(MAIN)
        )
        if not mine or self.post.withdraw(MAIN, msg_id) is None:
            return False
        self._changed()
        return True

    # ── 기다리기·묻기 ────────────────────────────────

    def wait_for_arrival(self, timeout: float) -> bool:
        """사람의 말이 줄 설 때까지만 기다린다 — 꺼내지 않는다. ``timeout`` 안에 왔거나 이미 있으면 True, 닫혔으면
        False. 메일은 세지 않는다(메일은 메일함이 깨운다)."""
        return self.post.wait_words(MAIN, timeout)

    def snapshot(self) -> list[dict]:
        """화면에 보일 대기열 — 줄 선 사람의 말, 선 순서."""
        return [self.item_of(e) for e in self.post.words(MAIN)]

    def pending_count(self) -> int:
        return len(self.post.words(MAIN))

    def idle(self) -> bool:
        """main 이 런을 열 것을 기다리는 중인가 — 펌프가 `wait_to_open` 안에 있다."""
        return self.post.idle(MAIN)

    def close(self) -> None:
        """기다리는 펌프를 깨워 끝나게 한다 — 줄 선 사람의 말은 먼저 꺼내진다. 멱등."""
        self.post.close(MAIN)

    @staticmethod
    def item_of(env: Envelope) -> dict:
        """사람의 말 봉투 → 화면·펌프의 항목 ``{id, conn_id, nickname, text, images}``."""
        p = env.payload
        return {
            "id": env.id,
            "conn_id": p.get("conn_id") or "",
            "nickname": p.get("nickname"),
            "text": env.text,
            "images": list(p.get("images") or []),
        }


def intake_item(env: Envelope) -> dict:
    """main 상자의 봉투 하나 → 턴 경계 흡수 항목: 사람의 말은 ``{"message": 항목}``(`MainInput.item_of`), 메일은
    ``{"mail": 회신}``(봉투가 싣고 온 옛 dict)."""
    if is_human_words(env):
        return {"message": MainInput.item_of(env)}
    return {"mail": dict(env.payload)}
