"""사람에게 건 질문 — 등록·목록·답·정리 (6b ② 에서 `agents_live` 의 메서드를 그대로 옮겼다).

`AgentRegistry` 가 이 믹스인을 받는다 — 메서드의 ``self`` 는 레지스트리다(우편 `_post`·명단 `_agents`·락 `_cv`·
알림 `_notify_roster`·배달 `request` 를 레지스트리의 것으로 쓴다). 이 모듈은 레지스트리를 import 하지 않는다(타입만).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_cli import item_ids
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

    @property
    def to_human(self) -> bool:
        return is_human_addr(self.target)


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

    def question_port(self, key: str | None = None) -> QuestionPort:
        """루프에 넘길 seam. ``key=None`` 이면 main 용."""
        return QuestionPort(self, key)

    def register_question(self, asker: str, text: str) -> tuple[str, str]:
        """사람에게 질문 등록 — ``(id, err)``. 아무것도 블록하지 않는다.

        표면은 ❓ 트레이(로스터의 ``open_questions``)와 asker 의 대화 창이다 —
        배달할 inbox 가 없다. 사람이 답하면 :meth:`answer_question` 이 asker 에게
        새 배달로 보낸다."""
        text = (text or "").strip()
        if not text:
            return "", "empty question"
        tm = self._agents.get(asker)
        if tm is None:
            return "", f'unknown asker "{asker}"'
        asked_in = tm.current_item
        with self._cv:
            # 중복 접기 — **같은 런 안에서만**. ``_op_ask`` 는 루프 탐지기
            # 앞에서 반환하므로(dispatch.py) 한 런이 같은 질문을 반복해도
            # 구조적으로 안 잡힌다. 런 경계를 넘으면 접지 않는다.
            for q in self.open_human_questions():
                if q.asker == asker and q.text == text and q.asked_in == asked_in:
                    return q.id, ""
            q = Question(
                id=_new_question_id(),
                asker=asker,
                target="user",
                text=text,
                asked_in=asked_in,
            )
            self._post.ask(q.id, f"agent:{asker}", q.target, text, meta=q)
            # 질문 줄은 질문을 여는 구간에서 적는다 — 답의 in 이 앞서지 않게 (v10.146.4)
            lines = self._question_line(q)
            self._lines_in_section(lines)
        self._lines_after_section(lines)
        # 트레이는 로스터의 ``open_questions`` 를 읽는다 — 알리지 않으면
        # 질문이 다음 로스터 브로드캐스트까지 화면에 안 뜬다.
        self._notify_roster()
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
        """사람이 트레이에서 ``qid`` 에 답한다 — 에러 또는 빈 문자열.

        ``user*`` 는 하나의 주체로 본다 — CLI 는 ``user``, 웹은 ``user:{nick}``
        이고 뷰어마다 닉이 달라, 문자열 동치로 검사하면 두 번째 뷰어의 트레이
        답이 거부된다.
        """
        text = (text or "").strip()
        if not text:
            return "empty answer"
        unknown = f'unknown or already-answered question "{qid}"'
        row = self._post.row(qid)
        q = row.meta if row is not None and row.kind == "q" else None
        if q is None:
            return unknown
        if not is_human_addr(by):
            return f'question "{qid}" is addressed to the person, not "{by}"'
        try:  # 답의 배달이 곧 질문 줄 닫기(``settles`` — 한 락 구간)
            refused = self._deliver_answer(q, text)
        except RowError:
            return unknown  # 다른 답변자가 먼저 답했다 — 줄이 이미 닫혔다
        if refused:  # 묻는 쪽이 멈췄거나 사라져 받지 못한다 — 그 질문들을 닫고 명단에 알린다 (v10.149.1)
            self._purge_questions_for(q.asker)
            return f'the agent that asked "{qid}" has stopped — the question is closed'
        return ""

    def _deliver_answer(self, q: Question, text: str) -> str:
        """사람의 답을 asker 에게 — 새 배달(사람 발신이라 ``u-``)로. `request` 의 거절(묻는 쪽이 받지 못함) 또는 "".

        사람이 보낸 것이므로 그 런은 사람 런이다: 결과는 창이 곧 배달이고, 닫는
        법은 사람 요청과 같다(`complete` 의 `answers`). 봉투가 무엇에 대한
        답인지(``re``) 적는다."""
        return self.request(
            q.asker,
            f"Your question: {q.text}\nAnswer: {text}",
            author=q.target,
            expects_reply=True,
            kind="answer",
            re=q.id,
            todo="Continue the work it was blocking.",
            settles=q.id,
        )

    def _purge_questions_for(self, key: str) -> None:
        """그가 건 질문을 닫고(받을 곳이 없다) 저장·명단 알림 — 영구 사망(✕·크래시, ``not revivable``)과 답이
        거절됐을 때(`answer_question`)만 부른다. 세션 종료(``shutdown_all``)에서 지우면 직후의 ``_save_state`` 가
        빈 목록을 저장해 resume 이 질문을 되살리지 못한다.
        """
        outgoing = self._post.forget_questions_of(f"agent:{key}")
        if outgoing:
            self._notify_roster()

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
