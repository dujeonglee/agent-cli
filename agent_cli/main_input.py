"""main 앞 사람의 입력 — 창구(`agent_cli.post`)의 ``"main"`` 상자에 봉투로 넣고, 보이고, 주인이 취소하고, 도착을
기다리고, 런을 열 것을 꺼낸다 (`MainInput`).

웹 채팅·`run` 의 argv 질의·제자리 대기 중 친 터미널 줄·예약 발화·StaleGuard 의 Hint 가 모두 여기로 들어온다.
메일과 **같은 상자·같은 조건변수**라, 펌프는 "사람의 말 또는 깨우는 메일" 을 한 자리에서 기다린다
(`PostOffice.next_run`). 사람의 말은 줄(장부) 없이 선다 — 꺼내기 전에는 취소되거나 명령으로 빠질 수 있다.
저장하지 않는다.

화면·펌프에 내주는 항목 모양은 ``{id, conn_id, nickname, text, images, command}`` 하나다 — 대기열 표시·취소
소유권·런의 귀속이 이 필드를 읽는다. ``command`` 는 들어올 때 한 번 판정한 "명령인가"(5b-3) — 명령은 하니스가 main 의
안전한 지점(펌프의 차례·턴 경계·제자리 대기)에서 선 순서대로 실행하고, 모델의 우편이 아니다. ``on_change`` 는 상자의 사람 말이 바뀔 때마다 **락 밖에서** 부른다(웹이 대기열을
다시 그린다 — 렌더러 락과 겹치지 않게).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from agent_cli import item_ids
from agent_cli.constants import EMPTY_WORDS, INPUT_IS_COMMAND, is_blank_words
from agent_cli.post.envelope import Envelope, is_human_words, words
from agent_cli.post.office import CLOSED, PostOffice

__all__ = ["CLOSED", "MAIN", "EmptyWords", "MainInput", "human_addr", "intake_item"]

#: 사람의 입력을 받는 주소
MAIN = "main"


def human_addr(nickname: str | None) -> str:
    """사람의 주소 — 별명이 있으면 ``user:<별명>``, 없으면(``run`` 의 argv 질의·터미널 줄) ``user``. main 앞 사람의
    말·에이전트 채널 입력·main 입력창의 ``@agt-…`` 요청이 같은 규칙을 쓴다 (v10.149.4)."""
    return f"user:{nickname}" if nickname else "user"


class EmptyWords(ValueError):
    """글도 이미지도 없는 말 — 사람의 말이 아니라 넣지 않았다(`MainInput.enqueue`)."""


class MainInput:
    def __init__(
        self,
        post: PostOffice | None = None,
        *,
        on_change: Callable[[], None] | None = None,
        is_command: Callable[[str], bool] | None = None,
    ) -> None:
        """``post`` 가 없으면 새로 만든다 — 세션의 에이전트 레지스트리가 이 창구를 받아 써야 메일과 사람의 말이 한
        상자에 선다. ``is_command`` 는 표면의 판정(`command_table.run_input_is_command`·`web_input_is_command`) —
        들어오는 말마다 한 번 묻는다. None 이면 이 상자에는 명령이 없다."""
        self.post = post if post is not None else PostOffice()
        self.on_change = on_change
        self._is_command = is_command
        #: 지금 런의 정지 플래그 — 펌프가 런을 열 때 건다(`MainActor.open`), 끝나면 None. 닫기(`close`)가 세운다
        self._stop: threading.Event | None = None

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
        src: str | None = None,
    ) -> dict:
        """사람의 말 하나를 상자 끝에 — 그 항목을 돌려준다. id 는 장부 형식(``u-N``, 세션 할당기 하나)이고 그 문자열이
        꼬리·`answers`·기록의 `request_id`·웹 취소까지 같다. ``src`` 는 봉투의 보낸 이 — 없으면 그 사람(`human_addr`),
        예약 발화는 ``schedule:<id>``(`constants.schedule_addr`). 명령인가는 여기서 한 번 판정해 봉투에 싣는다.

        글도 이미지도 없는 말(공백뿐인 글 포함)은 말이 아니다 — 넣지 않고 `EmptyWords`. main 앞에 서는 사람의 말은
        모두 이 문을 지난다."""
        if is_blank_words(text, images):
            raise EmptyWords(EMPTY_WORDS)
        command = self._is_command is not None and bool(self._is_command(text))
        env = words(
            item_ids.next_id(item_ids.USER),
            src or human_addr(nickname),
            MAIN,
            text,
            payload={
                "conn_id": conn_id or "",
                "nickname": nickname,
                "images": list(images or []),
                INPUT_IS_COMMAND: command,
            },
        )
        self.post.deliver(env)
        self._changed()
        return self.item_of(env)

    # ── 꺼내기 ───────────────────────────────────────

    def wait_to_open(self, timeout: float | None):
        """펌프: 다음 런을 연 봉투 — 줄 선 첫 깨우는 것 하나(도착 순서)를 꺼내 턴 경계 흡수 항목의 모양으로
        (`intake_item`: 사람의 말은 ``{"message": 항목}``, 메일은 ``{"mail": …}``), `CLOSED`, 또는 None(``timeout``).
        기다리는 동안 main 은 한가함(`idle`)이다."""
        got = self.post.next_run(MAIN, timeout=timeout)
        if got is None or got is CLOSED:
            return got
        self._changed()
        return intake_item(got)

    def take_first_command(self) -> dict | None:
        """맨 앞의 사람의 말이 명령이면(들어올 때의 판정) 꺼내 돌려준다, 아니면 None — main 이 제자리에서 기다리는
        동안 줄 선 명령(``/stop`` 등)을 그 자리에서 라우팅하려고."""
        first = next(iter(self.post.words(MAIN)), None)
        if first is None or not first.payload.get(INPUT_IS_COMMAND):
            return None
        env = self.post.withdraw(MAIN, first.id)
        if env is None:
            return None  # 그 사이 취소됐다
        self._changed()
        return self.item_of(env)

    def take_command(self) -> dict | None:
        """줄 선 사람의 말 가운데 처음 만나는 명령(들어올 때의 판정)을 꺼내 돌려준다, 없으면 None — 스킬 런이 제자리에서
        기다리는 동안(6b) 그 앞의 보통의 말은 main 의 것이라 그대로 두고, 명령(`/answer`·`/close` 등)만 그 자리에서
        라우팅하려고. main 의 제자리 대기는 `take_first_command`(맨 앞만 — 보통의 말이 main 을 깨운다)."""
        first = next(
            (e for e in self.post.words(MAIN) if e.payload.get(INPUT_IS_COMMAND)), None
        )
        if first is None:
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

    def closed(self) -> bool:
        """main 상자가 닫혔는가 — 세션이 끝나는 중이다(웹 서버가 내려갈 때 `WebServer.shutdown` 이 닫는다)."""
        return self.post.is_closed(MAIN)

    def close(self) -> None:
        """세션이 끝난다 — main 상자를 닫고 **지금 작업 구간의 정지 플래그도 세운다**(5e1). 기다리는 펌프는 깨어 끝나고(줄
        선 사람의 말은 런을 열지 않는다 — 닫힘이 이긴다), 일하던 main 은 다음 정지 판정(턴 경계·스트림·도구 직전·제자리
        대기)에서 멈춰 세션 종료로 끝난다(`AgentLoop._on_interrupt` — 끝 정리 없음). 멱등."""
        with self.post.cv:
            self.post.close(MAIN)
            stop = self._stop
        if stop is not None:
            stop.set()

    def bind_stop(self, event: threading.Event | None) -> None:
        """지금 런의 정지 플래그를 건다(None = 해제) — 닫기가 세울 것. 이미 닫혔으면 곧바로 세운다: 닫힌 뒤 열린
        런(닫힘과 런 시작이 엇갈림)도 첫 정지 판정에서 끝난다."""
        with self.post.cv:
            self._stop = event
            closed = self.post.is_closed(MAIN)
        if event is not None and closed:
            event.set()

    @staticmethod
    def item_of(env: Envelope) -> dict:
        """사람의 말 봉투 → 화면·펌프의 항목 ``{id, conn_id, nickname, text, images, command}``."""
        p = env.payload
        return {
            "id": env.id,
            "conn_id": p.get("conn_id") or "",
            "nickname": p.get("nickname"),
            "text": env.text,
            "images": list(p.get("images") or []),
            "command": bool(p.get(INPUT_IS_COMMAND)),
        }


def intake_item(env: Envelope) -> dict:
    """main 상자의 봉투 하나 → 턴 경계 흡수 항목: 사람의 말은 ``{"message": 항목}``(`MainInput.item_of`), 메일은
    ``{"mail": 회신}``(봉투가 싣고 온 옛 dict)."""
    if is_human_words(env):
        return {"message": MainInput.item_of(env)}
    return {"mail": dict(env.payload)}
