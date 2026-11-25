"""사람에게 건 질문 — 등록·목록·답·정리 (6b ② 에서 `agents_live` 의 메서드를 그대로 옮겼다).

`AgentRegistry` 가 이 믹스인을 받는다 — 메서드의 ``self`` 는 레지스트리다(우편 `_post`·명단 `_agents`·락 `_cv`·
알림 `_notify_roster`·배달 `request` 를 레지스트리의 것으로 쓴다). 이 모듈은 레지스트리를 import 하지 않는다(타입만).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_cli import item_ids
from agent_cli import render as render_surface
from agent_cli.agent_icon import agent_display_name
from agent_cli.constants import is_private_owner
from agent_cli.post.request_book import RowError
from agent_cli.subagent.agent_mail import is_human_addr
from agent_cli.subagent.question_port import QuestionPort

if TYPE_CHECKING:
    from agent_cli.subagent.agents_live import AgentInstance

# ── 비동기 ask (docs/agent-ask/DESIGN.md) ──────────────
#
# 질문은 **아무것도 블록하지 않는다**: ``ask`` 가 여기에 등록하고 즉시
# 반환한다. v10.77.0 (B): 질문은 **사람에게만** 간다 — ❓ 트레이가 표면이고
# 사람이 답하면 asker 에게 새 배달로 온다. 에이전트·main 에게 묻는 길은
# `message`(요청 → 상대가 `reply` 를 빚진다) 하나다. 그래서 `answer` 도구도,
# 에이전트가 빚지는 질문(q-)도 없다. ``user*`` 는 하나의 주체로 본다 — CLI 는
# ``user``, 웹은 ``user:{nickname}`` 이고 뷰어가 여럿이면 닉이 다르다.


@dataclass
class Question:
    """열린 질문 하나 — 우편 장부의 질문 줄(q-)의 ``meta``. 주소는 언제나 사람
    (v10.77.0, B) — ❓ 트레이가 보여 주고 사람이 답한다.

    ``asked_in`` = asker 가 이 질문을 건 런(을 연 배달의 id) — 그 런의 결과에
    "사람에게 물은 것이 열려 있다" 알림을 붙이고(`_with_human_notice`), 같은 런
    안의 같은 질문을 접는다. 다른 런에서 걸어 둔(영영 열릴 수 있는) 질문이
    섞이면 안 되므로 런으로 좁힌다.
    """

    id: str
    asker: str  # "<agent key>" — 답이 돌아갈 곳
    target: str  # "user" — 사람(트레이)
    text: str
    asked_at: float = field(default_factory=time.time)
    asked_in: str = ""  # asker 가 이 질문을 건 런(을 연 배달의 id)
    name: str = ""  # 스킬 런이 물었으면 그 스킬 이름(6b) — `asker_label`

    @property
    def to_human(self) -> bool:
        return is_human_addr(self.target)


#: 사람이 질문을 답 없이 닫았을 때 묻는 쪽이 읽는 괄호 줄 (6b, 결정 E1)
QUESTION_CLOSED_TODO = (
    "The user closed it without an answer. Go on with what you have, and say "
    "what you assumed."
)


#: main 의 묻는 쪽 이름이자 우편 주소 (6b)
MAIN = "main"


def asker_address(asker: str) -> str:
    """묻는 쪽의 우편 주소 — main 은 ``main``, 스킬 런은 자기 주소(``skill:…`` 그대로), 에이전트는 ``agent:<key>``.
    답·닫힘 봉투가 이 상자로 간다."""
    if asker == MAIN or is_private_owner(asker):
        return asker
    return f"agent:{asker}"


def _new_question_id() -> str:
    return item_ids.next_id(item_ids.QUESTION)


class QuestionsMixin:
    """`AgentRegistry` 의 질문 메서드 묶음."""

    def open_human_questions(self) -> list[Question]:
        """열린 질문 — ❓ 트레이와 idle-reap 가드가 읽는다(주소는 전부 사람). 질문 줄의 ``meta`` 가 `Question` 이다."""
        return [r.meta for r in self._post.open_questions()]

    def open_human_question_keys(self) -> list[str]:
        """사람 답을 기다리는 열린 질문의 asker — 종료 시 경고 표시용."""
        return sorted({q.asker for q in self.open_human_questions()})

    def question_port(
        self, key: str | None = None, *, address: str = "", name: str = ""
    ) -> QuestionPort:
        """루프에 넘길 seam. ``key=None`` 이면 main 용, ``address``·``name`` 은 스킬 런의 주소와 스킬 이름(6b)."""
        return QuestionPort(self, key, address=address, name=name)

    def register_question(
        self, asker: str, text: str, *, name: str = ""
    ) -> tuple[str, str]:
        """사람에게 질문 등록 — ``(id, err)``. 아무것도 블록하지 않는다. ``asker`` 는 에이전트 키 또는 ``"main"``(6b).

        표면은 ❓ 트레이(열린 질문 목록 — `_notify_questions`)와 묻는 쪽의 줄이다(에이전트는 대화 창, main 은 터미널의
        질문 줄). 사람이 답하거나 닫으면 그 봉투가 묻는 쪽에게 간다(`_close_question`)."""
        text = (text or "").strip()
        if not text:
            return "", "empty question"
        tm = self._agents.get(asker)
        if tm is None and asker != MAIN and not is_private_owner(asker):
            return "", f'unknown asker "{asker}"'
        asked_in = tm.current_item if tm is not None else ""
        with self._cv:
            # 중복 접기 — 같은 묻는 쪽의 같은 글이 **열려 있는 동안**(6b — 누구에게나 한 규칙). ``_op_ask`` 는 루프
            # 탐지기 앞에서 반환하므로(dispatch.py) 반복을 여기서 접는다. 닫힌 뒤 다시 물으면 새 번호다.
            for q in self.open_human_questions():
                if q.asker == asker and q.text == text:
                    # 이 런도 물었다 — 런 결과의 알림(`_with_human_notice`)이 이 런에 그 질문을 싣게
                    q.asked_in = asked_in
                    return q.id, ""
            q = Question(
                id=_new_question_id(),
                asker=asker,
                target="user",
                text=text,
                asked_in=asked_in,
                name=name,
            )
            self._post.ask(q.id, asker_address(asker), q.target, text, meta=q)
            # 질문 줄은 질문을 여는 구간에서 적는다 — 답의 in 이 앞서지 않게 (v10.146.4)
            lines = self._question_line(q)
            self._lines_in_section(lines)
        self._lines_after_section(lines)
        if (
            asker not in self._agents
        ):  # 대화 창이 없는 묻는 쪽(main·스킬 런)은 터미널의 질문 줄
            render_surface.get_renderer().question_line(
                self.asker_label(q), q.id, q.text
            )
        self._notify_questions()
        return q.id, ""

    def _question_line(self, q: Question) -> list[tuple[AgentInstance, dict]]:
        """묻는 쪽 창의 질문 줄 — 사람이 먼저 보는 자리. 만들기만 한다. 묻는 쪽이 없으면 빈 목록."""
        tm = self._agents.get(q.asker)
        if tm is None:
            return []
        payload = {
            "key": tm.key,
            "direction": "question",
            "item": q.id,
            "author": tm.key,
            "text": q.text,
            "to": q.target,
            "ts": q.asked_at,
            "profile": tm.profile_name,
            "instance_name": tm.instance_name,
        }
        return [(tm, payload)]

    def questions_asked_in(self, asker: str, run_item: str) -> list[Question]:
        """``asker`` 가 ``run_item`` 런에서 건 열린 질문 (런 결과의 알림)."""
        return [
            q
            for q in self.open_human_questions()
            if q.asker == asker and q.asked_in == run_item
        ]

    def answer_question(self, qid: str, text: str, *, by: str) -> str:
        """사람이 ``qid`` 에 답한다(트레이·`/answer`) — 에러 또는 빈 문자열.

        ``user*`` 는 하나의 주체로 본다 — CLI 는 ``user``, 웹은 ``user:{nick}``
        이고 뷰어마다 닉이 달라, 문자열 동치로 검사하면 두 번째 뷰어의 트레이
        답이 거부된다.
        """
        text = (text or "").strip()
        if not text:
            return "empty answer"
        return self._close_question(
            qid,
            by,
            unknown=f'unknown or already-answered question "{qid}"',
            body=lambda q: f"Your question: {q.text}\nAnswer: {text}",
            kind="answer",
            todo="Continue the work it was blocking.",
            closes_as="user_answered",
        )

    def close_question(self, qid: str, *, by: str) -> str:
        """사람이 ``qid`` 를 답 없이 닫는다(트레이의 단추·`/close`, 6b) — 에러 또는 빈 문자열. 답과 같은 길이다: 묻는
        쪽에게 그 사실의 봉투가 가고(기다리던 `wait` 가 깬다), 질문 줄은 ``user_closed`` 로 닫힌다."""
        return self._close_question(
            qid,
            by,
            unknown=f'unknown or already-closed question "{qid}"',
            body=lambda q: f"Your question: {q.text}",
            kind="closed",
            todo=QUESTION_CLOSED_TODO,
            closes_as="user_closed",
        )

    def _close_question(
        self,
        qid: str,
        by: str,
        *,
        unknown: str,
        body,
        kind: str,
        todo: str,
        closes_as: str,
    ) -> str:
        """답·닫힘의 한 길 — 그 번호의 질문을 찾아, 사람만, 묻는 쪽에게 봉투를 배달하며 줄을 닫는다."""
        row = self._post.row(qid)
        q = row.meta if row is not None and row.kind == "q" else None
        if q is None:
            return unknown
        if not is_human_addr(by):
            return f'question "{qid}" is addressed to the person, not "{by}"'
        try:  # 배달이 곧 질문 줄 닫기(``settles`` — 한 락 구간)
            refused = self._deliver_to_asker(
                q, body(q), kind=kind, todo=todo, closes_as=closes_as
            )
        except RowError:
            return unknown  # 다른 사람이 먼저 답하거나 닫았다 — 줄이 이미 닫혔다
        if refused:  # 묻는 쪽이 멈췄거나 사라져 받지 못한다 — 그 질문들을 닫고 명단에 알린다 (v10.149.1)
            self.close_questions_of(q.asker)
            return f'the agent that asked "{qid}" has stopped — the question is closed'
        self._notify_questions()
        return ""

    def _notify_questions(self) -> None:
        """열린 질문 목록을 표면에 (6b) — 웹 트레이의 출처 하나(sticky), 터미널은 아무것도 안 한다. main·에이전트의
        질문이 같은 목록이다. 표시용 — 실행 경로를 막지 않는다."""
        try:
            render_surface.get_renderer().open_questions(
                [
                    {
                        "id": q.id,
                        "asker": q.asker,
                        "label": self.asker_label(q),
                        "text": q.text,
                        "ts": q.asked_at,
                    }
                    for q in self.open_human_questions()
                ]
            )
        except Exception:
            pass

    def asker_label(self, q: Question) -> str:
        """질문의 묻는 쪽 이름표 하나 — 터미널 질문 줄·`/questions`·❓ 트레이가 같은 글을 쓴다(6b). main 은 "main",
        스킬 런은 ``skill <스킬 이름>``, 에이전트는 질문 줄과 같은 ``이름 (키)``."""
        tm = self._agents.get(q.asker)
        if tm is not None:
            return f"{agent_display_name(tm.key, tm.profile_name, tm.instance_name)} ({tm.key})"
        if is_private_owner(q.asker):
            return f"skill {q.name}" if q.name else "skill"
        return q.asker

    def _deliver_to_asker(
        self, q: Question, body: str, *, kind: str, todo: str, closes_as: str
    ) -> str:
        """질문을 닫는 봉투(답·닫힘)를 묻는 쪽에게 — 줄 없는 ``n-`` 봉투(회신과 같다: 기다리던 것의 도착이지 새 항목이
        아니다), 질문 줄 닫기와 한 락 구간. main 은 메일로, 에이전트는 받은편지함으로 — 봉투 모양과 글은 같다.
        `request` 의 거절(묻는 쪽이 받지 못함) 또는 ""."""
        if is_private_owner(q.asker):  # 스킬 런의 상자 — private 메일과 같은 길
            self._push_private(
                q.asker,
                {"kind": kind, "re": q.id, "output": body, "todo": todo},
                settles=q.id,
                closes_as=closes_as,
            )
            return ""
        if q.asker == MAIN:
            self._push_reply(
                {"kind": kind, "re": q.id, "output": body, "todo": todo},
                settles=q.id,
                closes_as=closes_as,
            )
            return ""
        return self.request(
            q.asker,
            body,
            author=q.target,
            expects_reply=False,
            kind=kind,
            re=q.id,
            todo=todo,
            settles=q.id,
            closes_as=closes_as,
        )

    def close_questions_of(self, asker: str) -> None:
        """그가 건 질문을 닫고(받을 곳이 없다) 저장·명단 알림 — 영구 사망(✕·크래시, ``not revivable``)과 답이
        거절됐을 때(`answer_question`)만 부른다. 세션 종료(``shutdown_all``)에서 지우면 직후의 ``_save_state`` 가
        빈 목록을 저장해 resume 이 질문을 되살리지 못한다.
        """
        outgoing = self._post.forget_questions_of(asker_address(asker))
        if outgoing:
            self._notify_questions()

    def _with_human_notice(self, tm: AgentInstance, run_item: str, output: str) -> str:
        """이 런이 **사람에게** 물어 둔 미답 질문을 결과에 실어 알린다.

        사람에게는 강제를 못 건다 — 우리 루프가 아니다. 대신 결과에 실어
        "내가 답을 안 해서 끝났구나" 를 알린다. 이 문구는 **하네스가** 붙인다:
        dispatch 에서 붙이면 ``serialize_terminal_for_history`` 를 타고 모델
        자신이 쓴 최종답으로 ctx 에 남아, 다음 런에서 모델이 하네스 문구를
        모방한다.
        """
        human_open = [
            q for q in self.questions_asked_in(tm.key, run_item) if q.to_human
        ]
        if not human_open:
            return output
        lines = "\n".join(f'   [{q.id}] "{q.text}"' for q in human_open)
        return (
            f"{output}\n\n⏳ {len(human_open)} question(s) still unanswered — "
            f"answering lets this continue:\n{lines}"
        )
