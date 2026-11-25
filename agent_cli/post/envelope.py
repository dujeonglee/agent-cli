"""봉투 — 나르는 것의 모양 하나 (시안 §3).

머리는 우편·장부가 읽는다: 누가 누구에게(``src``·``dst``), 무엇이(``kind``), 무엇에 대한 회신인가(``re``), 닫는
법(``closes_by`` — 만드는 쪽이 정한다), 깨우는가·저장하는가, hop·귀속(``answers``). ``text`` 는 본문 — 장부
줄과 읽기 전용 조회가 그대로 옮기며 해석하지 않는다. ``payload`` 는 글을 짓는 쪽만 읽는다(이미지·런 메타·
작업 정보 등). 우편·장부는 payload 를 열지 않는다.

``closes_by`` 는 아래 만드는 함수에서만 정한다 — 종류마다 이름 있는 함수 하나.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

Kind = Literal["request", "notice", "reply", "system"]
ClosesBy = Literal["reply", "complete"]

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class Envelope:
    """봉투 하나 — 불변. ``answers`` 는 튜플, ``payload`` 는 읽기 전용 사본으로 굳는다."""

    id: str
    kind: Kind
    src: str
    dst: str
    text: str
    re: str = ""
    mark: str = ""
    closes_by: ClosesBy | None = None
    wakes: bool = True
    hop: int = 0
    answers: tuple[str, ...] = ()
    ts: float = 0.0
    payload: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "answers", tuple(self.answers))
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def expects_reply(self) -> bool:
        """받는 쪽이 이것으로 `reply` 를 빚지는가."""
        return self.closes_by == "reply"


def request(
    id: str,
    src: str,
    dst: str,
    text: str,
    *,
    hop: int = 0,
    answers=(),
    ts: float = 0.0,
    payload: Mapping[str, Any] | None = None,
) -> Envelope:
    """회신을 기대하는 요청 (r-) — 받는 쪽이 `reply` 를 빚진다."""
    return Envelope(
        id,
        "request",
        src,
        dst,
        text,
        closes_by="reply",
        hop=hop,
        answers=answers,
        ts=ts,
        payload=payload or _EMPTY,
    )


def human(
    id: str,
    src: str,
    dst: str,
    text: str,
    *,
    re: str = "",
    answers=(),
    ts: float = 0.0,
    payload: Mapping[str, Any] | None = None,
) -> Envelope:
    """사람의 말 (u-) — 에이전트 채널 입력·트레이의 답(``re`` = 그 질문). `complete` 의 ``answers`` 로 닫힌다
    (창이 곧 배달이라 회신은 없다)."""
    return Envelope(
        id,
        "request",
        src,
        dst,
        text,
        re=re,
        closes_by="complete",
        answers=answers,
        ts=ts,
        payload=payload or _EMPTY,
    )


def words(
    id: str,
    src: str,
    dst: str,
    text: str,
    *,
    ts: float = 0.0,
    payload: Mapping[str, Any] | None = None,
) -> Envelope:
    """사람의 말 — 줄 없이 상자에 선다(main 앞: 꺼내기 전에는 취소·명령으로 빠질 수 있고, 줄은 받는 쪽이 집을 때
    연다). 재시작하면 대기 중이던 말은 사라진다(봉투는 저장하지 않는다 — 5c-0)."""
    return Envelope(id, "request", src, dst, text, ts=ts, payload=payload or _EMPTY)


def notice(
    id: str,
    src: str,
    dst: str,
    text: str,
    *,
    hop: int = 0,
    ts: float = 0.0,
    payload: Mapping[str, Any] | None = None,
) -> Envelope:
    """통지 — 빚 없음, 읽으면 끝."""
    return Envelope(
        id, "notice", src, dst, text, hop=hop, ts=ts, payload=payload or _EMPTY
    )


def reply(
    id: str,
    src: str,
    dst: str,
    text: str,
    *,
    re: str,
    mark: str = "",
    hop: int = 0,
    wakes: bool = True,
    answers=(),
    ts: float = 0.0,
    payload: Mapping[str, Any] | None = None,
) -> Envelope:
    """회신 — 요청 ``re`` 를 갚는다(명시·폴백·정지·removed 모두). 줄은 없다. ``wakes`` 가 거짓이면 받는 쪽을
    깨우지 않는다("Stop everything")."""
    return Envelope(
        id,
        "reply",
        src,
        dst,
        text,
        re=re,
        mark=mark,
        hop=hop,
        wakes=wakes,
        answers=answers,
        ts=ts,
        payload=payload or _EMPTY,
    )


def system(
    id: str,
    src: str,
    dst: str,
    text: str,
    *,
    ts: float = 0.0,
    payload: Mapping[str, Any] | None = None,
) -> Envelope:
    """하니스 통지 — 작업·모니터·감사·사망·체크인. 줄이 없다(읽으면 끝)."""
    return Envelope(id, "system", src, dst, text, ts=ts, payload=payload or _EMPTY)


def is_human_words(env: Envelope) -> bool:
    """사람의 말인가 — 머리의 종류·닫는 법으로만(주소를 읽지 않는다): `reply` 로 닫지 않는 요청(`human`·`words`)."""
    return env.kind == "request" and env.closes_by != "reply"
