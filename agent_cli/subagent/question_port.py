"""질문·회신 포트 — 루프가 질문 목록과 회신 빚에 닿는 seam (`QuestionPort`).

갚는 규칙(무엇에 대한 회신인지 ``id`` 로 고르고, 빚이 없거나 이미 갚았으면 사유를 돌려주는 것)이 여기 있다.
갚음 표시·배달·요청자의 기다림 닫기는 레지스트리의 `AgentRegistry.pay_debt` 한 번이다 — 이 모듈은 레지스트리를
import 하지 않는다(타입만).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, NamedTuple

from agent_cli.constants import has_words
from agent_cli.subagent.agent_mail import is_human_addr

if TYPE_CHECKING:
    from agent_cli.subagent.agents_live import AgentRegistry

#: `reply` 가 요청자에게 가지 못했다 — 그때 요청자가 명단에서 사라졌거나 죽었거나 멈추는 중이었다(v10.145.2). 빚은
#: 닫혔다. 같은 키가 나중에 되살아날 수 있으므로(`resume`·재시작) 과거형이다 — 그 요청자도 더는 이 회신을
#: 기다리지 않는다. 처음 갚을 때(`AgentRegistry.pay_debt`)와 그 id 에 다시 `reply` 할 때 같은 문장이다.
REPLY_REQUESTER_GONE = (
    'not delivered — agent "{key}", which sent request [{rid}], was already gone. '
    "[{rid}] is closed; there is nothing more to do for it."
)


@dataclass
class ReplyDebt:
    """회신 빚 하나의 공개 모양 — 우편 장부의 빚 줄(`AgentRegistry` 의 `_debt_view`)을 이 모양으로 보여 준다(v10.145.0 — 장부는
    `agent_cli.post`). `reply` 판정(`judge_reply`)이 읽는 모양이라 판정 곁에 둔다(v10.146.0).

    런이 받은 **요청**(``expects_reply=True`` 항목·main 에 배달된 `message`)
    마다 하나(v9.25.0 — 종전엔 상대별 하나라 같은 상대의 새 요청이 앞 것을
    덮었다; 런 도중 흡수된 요청이 여럿이면 각각 회신을 받아야 한다는 사용자
    결정). ``id`` 가 그 요청을 가리킨다 — 그 요청 배달의 id(`item_ids`,
    전역에서 유일). 갚으면(`reply(id)`, 그 대신인 폴백·정지 회신 — `message` 는
    갚지 않는다, v10.145.1) ``settled`` — 지우지 않는다: "이미 갚았다" 는 거부 사유가 사실이어야
    한다. 사람 주소(``user:*``)의 빚은 창이 곧 배달이라 갚을 것이 없지만
    **적어는 둔다** — 그 런의 `reply` 를 거부할 때 사유가 "요청이 없었다" 가
    아니라 "요청자가 창을 보는 사람" 이어야 하기 때문이다.
    """

    to: str  # "main" | "agent:<key>" | "user" | "user:<nick>"
    text: str  # 무엇에 대한 회신인지 (빚 목록의 설명)
    answers: list = field(default_factory=list)  # 요청 항목의 귀속 스냅샷
    settled: bool = False
    id: str = ""  # 요청 번호 — `reply(id=…)` 가 가리키는 값
    #: 닫힌 줄이면 닫힌 사유(장부의 ``Row.how`` — "reply"·"unsent"·…), 열린 줄이면 "". "unsent" 는 회신이
    #: 요청자에게 가지 못한 채 닫힌 것이다 — 다시 `reply` 할 때 "이미 회신했다" 가 아니라 그 사실을 말한다.
    closed_by: str = ""

    @property
    def human(self) -> bool:
        return is_human_addr(self.to)


def _not_delivered(d) -> str:
    """회신이 가지 못한 채 닫힌 빚(``closed_by == "unsent"``)에 다시 `reply` 했다 — 처음과 같은 사실."""
    return REPLY_REQUESTER_GONE.format(key=d.to.split(":", 1)[-1], rid=d.id)


# ── `reply` 판정 — main·에이전트 하나 (v10.146.0, 단일 구조 3단계) ─────────────────────────
#
# 판정(`judge_reply`)은 레지스트리의 사본(`ReplyFacts` — `AgentRegistry.reply_facts`, 락 한 번)만 읽고, 거절 글
# (`reply_refusal`)은 판정의 이름에서 짓는다. main 과 에이전트가 갈리는 것은 사실 하나 — 레지스트리가 **이 런을 연
# 주소**를 아는가(``opener``): 에이전트는 런을 연 항목의 author 를 안다, main 의 런은 펌프가 연다(None).

Verdict = Literal[
    "empty",
    "no_words",
    "none_owed_human",
    "none_owed_unsent",
    "none_owed_replied",
    "none_owed",
    "needs_id",
    "id_unsent",
    "id_replied",
    "id_not_owed",
    "pay",
]


class ReplyFacts(NamedTuple):
    """`reply` 판정의 재료 — 갚는 주체의 사본. ``opener_row`` 는 ``opener`` 에게 진 가장 최근 줄(갚았든 아니든),
    ``by_id`` 는 그 ``id`` 의 줄(이 주체의 것일 때만)."""

    owed: tuple[ReplyDebt, ...]  # 아직 갚지 않은 빚 — 열린 순서
    opener: str | None  # 이 런을 연 주소 — 레지스트리가 모르면 None
    opener_row: ReplyDebt | None
    by_id: ReplyDebt | None


def judge_reply(
    text: str, id: str, facts: ReplyFacts
) -> tuple[Verdict, ReplyDebt | None]:
    """`reply(text, id)` 의 판정 — ``(판정, 갚을 빚)``; 갚을 빚은 ``"pay"`` 일 때만. 읽기만 한다. ``text``·``id`` 는
    앞뒤 공백을 걷은 값이다.

    순서: 글 → ``id`` 가 이 런에서 이미 닫힌 내 줄(가지 못함·이미 갚음 — 그 줄로 말한다) → 빚이 없음(런을
    연 주소에게 진 줄로 사유를 고른다) → ``id`` 없음 → ``id`` 가 빚에 없음 → 갚음."""
    if not text:
        return "empty", None
    if not has_words(text):
        return "no_words", None
    prior = facts.by_id
    if prior is not None and prior.settled:
        # 이 런에서 이미 닫힌 내 줄 — 누구든(main·에이전트, 빚이 남았든 아니든) 그 줄로 말한다
        if prior.closed_by == "unsent":
            return "id_unsent", None
        return "id_replied", None
    if not facts.owed:
        d = facts.opener_row
        if d is not None and d.human:
            # 사람이 창에서 직접 시킨 런 — 창이 곧 배달이라 빚이 없다(D8)
            return "none_owed_human", None
        if d is not None and d.settled and d.closed_by == "unsent":
            return "none_owed_unsent", None
        if d is not None and d.settled:
            return "none_owed_replied", None
        return "none_owed", None
    if not id:
        return "needs_id", None
    d = next((x for x in facts.owed if x.id == id), None)
    if d is not None:
        return "pay", d
    return "id_not_owed", None


def _owed_listing(owed) -> str:
    return "; ".join(
        f'[{d.id}] {d.to.split(":", 1)[-1]}: "{d.text[:60]}"' for d in owed
    )


def reply_refusal(verdict: Verdict, text: str, id: str, facts: ReplyFacts) -> str:
    """거절 판정 → 모델이 읽는 글. 런을 연 주소를 모르면(main) "빚 없음" 은 그 주소를 단언하지 않는 글이다. 그 id 의
    줄로 고른 사유(가지 못함·이미 갚음)는 main·에이전트 같은 글이다."""
    opener_known = facts.opener is not None
    if verdict == "empty":
        return "empty reply — nothing sent"
    if verdict == "no_words":
        return f"reply has no words ({text[:20]!r}) — nothing sent; say it in words"
    if verdict == "none_owed_human":
        return (
            "nothing to send — your requester is a person watching this "
            "window, and they see your `complete` here. Just `complete`."
        )
    if verdict == "none_owed_unsent":
        return _not_delivered(facts.opener_row)
    if verdict == "none_owed_replied":
        a = facts.opener
        return (
            f"already replied to {a} in this run — a second reply is not "
            "delivered. If you must correct it, send the correction with "
            f'`message` (`to` "{a.split(":", 1)[-1]}", `text` your correction).'
        )
    if verdict == "none_owed":
        if not opener_known:
            return (
                "nothing to reply to — no agent is waiting on a reply from you. "
                "To give an agent new work, use `message`."
            )
        return (
            "nothing to reply to — this run was not started by a request "
            "that expects one (it was a delivered reply, a notice, or an "
            "answer). Do not acknowledge those. If you need something from "
            "someone, use `message` (`to` the recipient, `text` your message)."
        )
    listing = _owed_listing(facts.owed)
    if verdict == "needs_id":
        return (
            "reply needs `id` — which request is this the reply to? You owe: "
            f"{listing}. Send `reply` with that `id`."
        )
    if verdict == "id_unsent":
        return _not_delivered(facts.by_id)
    if verdict == "id_replied":
        prior = facts.by_id
        return (
            f"already replied to [{id}] ({prior.to}) in this run — a second "
            "reply is not delivered. If you must correct it, send the "
            f'correction with `message` (`to` "{prior.to.split(":", 1)[-1]}", '
            "`text` your correction)."
        )
    if verdict == "id_not_owed":
        return f'no unpaid request "{id}" — you owe: {listing}.'
    raise ValueError(f"not a refusal: {verdict}")


class QuestionPort:
    """루프가 질문·회신 빚에 닿는 seam (DESIGN.md §4).

    표면은 ``ask``·``reply``·``debts`` 다. main 도 같은 포트를 받는다
    (``key=None``) — 묻기·빚 갚기가 루프와 무관하게 하나이기 때문이다(6b).
    """

    def __init__(
        self,
        registry: AgentRegistry,
        key: str | None = None,
        *,
        address: str = "",
        name: str = "",
    ):
        self._reg = registry
        self.name = name  # 스킬 런의 스킬 이름(6b) — 질문의 묻는 쪽 이름표
        # 이 주체의 **주소**(질문의 target 과 비교) / **발신 라벨**(asker). 스킬 런(6b)은 자기 주소(``skill:…``)가
        # 셋 모두다 — 묻고, 답을 받고, 빚은 없다.
        self.key = key if not address else address
        self.me = address or ("main" if key is None else f"agent:{key}")
        self.asker = address or ("main" if key is None else key)

    def ask(self, text: str) -> tuple[str, str]:
        """사람에게 질문 등록 — ``(id, err)``, err 가 비면 성공 (v10.77.0, B).

        주소는 언제나 사람(❓ 트레이)이다. 에이전트·main 에게 무언가 필요하면
        `message` 다 — 그건 요청이고 상대가 `reply` 를 빚진다. 종전의 ``to``
        (원 요청자 기본·에이전트 주소)는 에이전트가 빚지는 질문(q-)과 `answer`
        도구를 만들었는데, 실제 기록에서 `answer` 11회가 전부 실패했다. main 도 같은 길이다(6b) — 열린 질문은
        트레이·`/questions` 의 목록에 서고, 사람이 답하거나 닫아서 닫힌다.
        """
        return self._reg.register_question(self.asker, text, name=self.name)

    def debts(self) -> list[dict]:
        """이 런이 아직 갚지 않은 회신 빚 (v9.22.0) — main·상주 **같은 코드**.

        ``{"kind": "reply", "to": addr, "id": rid, "text": …}`` — 이 런이 받은
        요청 중 안 갚은 것(우편 장부의 빚 줄). 상주는 시작·흡수 항목, main 은
        런에 배달된 `message`; 적는 쪽만 다르고 장부는 하나다. `complete`
        직전에 루프가 물어 독촉한다.
        """
        return [
            {"kind": "reply", "to": d.to, "id": d.id, "text": d.text}
            for d in self._reg.reply_debts(self.me)
        ]

    def open_questions(self) -> list[tuple[str, str]]:
        """이 주체가 사람에게 건 열린 질문 ``[(id, 글)]``, 연 순서 — 꼬리의 ``## Your open questions`` (6a)."""
        return [
            (q.id, q.text)
            for q in self._reg.open_human_questions()
            if q.asker == self.asker
        ]

    def awaited(self) -> list[dict]:
        """이 주체가 보내 놓고 아직 받지 못한 회신 (v10.141.0) — 장부의 "기다리는 회신".
        main·에이전트 같은 코드(`AgentRegistry.awaited_replies`)."""
        return self._reg.awaited_replies(self.me)

    def reply(self, text: str, *, id: str = "") -> str:
        """빚진 회신을 갚는다 (v9.21.0) — 에러 메시지 또는 빈 문자열. main·에이전트 **같은 판정**(`judge_reply`,
        v10.146.0) — 갈리는 것은 사실 하나(이 런을 연 주소를 레지스트리가 아는가)이고 거절 글(`reply_refusal`)이
        그것으로 고른다. 요청자가 사라져 배달하지 못했으면 빚은 닫히고 그 사실이 에러로 온다(`AgentRegistry.pay_debt`,
        v10.145.2).

        ``id`` (v9.25.0) 는 **필수** — 어느 요청에 대한 답인지 하네스가 알아야
        한다. 런 도중 요청이 흡수되면 빚이 여럿이고, 그때 "요청자" 는 하나가
        아니다. 없으면 항상 거부하고 번호 목록을 보여 준다(`complete` 의
        `answers` 와 같은 원칙, 사용자 결정). id 가 전역에서 유일하므로 그것만으로
        상대가 정해진다 — v10.77.0 에서 ``to`` 를 지웠다(C).
        돌려받을 것이 없으므로 `expects_reply=False` 로 간다.
        **빚진 게 없으면 거부한다**(사용자 결정): 회신·통지로 시작한
        런엔 답할 상대가 없고, "확인했습니다" 류의 ack 가 정확히 여기서
        새어 나온다. 거부 문구가 `message` 로 안내한다.
        """
        text = (text or "").strip()
        id = str(id or "").strip()
        facts = self._reg.reply_facts(self.me, id)
        verdict, debt = judge_reply(text, id, facts)
        if verdict == "pay":
            return self._reg.pay_debt(self.me, debt, text)
        return reply_refusal(verdict, text, id, facts)
