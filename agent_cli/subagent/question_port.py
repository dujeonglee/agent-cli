"""질문·회신 포트 — 루프가 질문 목록과 회신 빚에 닿는 seam (`QuestionPort`).

갚는 규칙(무엇에 대한 회신인지 ``id`` 로 고르고, 빚이 없거나 이미 갚았으면 사유를 돌려주는 것)이 여기 있다.
갚음 표시·배달·요청자의 기다림 닫기는 레지스트리의 `AgentRegistry.pay_debt` 한 번이다 — 이 모듈은 레지스트리를
import 하지 않는다(타입만).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NamedTuple

from agent_cli.constants import has_words

if TYPE_CHECKING:
    from agent_cli.subagent.agents_live import AgentRegistry, ReplyDebt

#: `reply` 가 요청자에게 가지 못했다 — 그때 요청자가 명단에서 사라졌거나 죽었거나 멈추는 중이었다(v10.145.2). 빚은
#: 닫혔다. 같은 키가 나중에 되살아날 수 있으므로(`resume`·재시작) 과거형이다 — 그 요청자도 더는 이 회신을
#: 기다리지 않는다. 처음 갚을 때(`AgentRegistry.pay_debt`)와 그 id 에 다시 `reply` 할 때 같은 문장이다.
REPLY_REQUESTER_GONE = (
    'not delivered — agent "{key}", which sent request [{rid}], was already gone. '
    "[{rid}] is closed; there is nothing more to do for it."
)


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
    "unknown_self",
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

    known: bool  # 주체가 명단에 있는가 (main 은 언제나)
    owed: tuple[ReplyDebt, ...]  # 아직 갚지 않은 빚 — 열린 순서
    opener: str | None  # 이 런을 연 주소 — 레지스트리가 모르면 None
    opener_row: ReplyDebt | None
    by_id: ReplyDebt | None


def judge_reply(
    text: str, id: str, facts: ReplyFacts
) -> tuple[Verdict, ReplyDebt | None]:
    """`reply(text, id)` 의 판정 — ``(판정, 갚을 빚)``; 갚을 빚은 ``"pay"`` 일 때만. 읽기만 한다. ``text``·``id`` 는
    앞뒤 공백을 걷은 값이다.

    순서: 글 → 주체 → 빚이 없음(런을 연 주소에게 진 줄로 사유를 고른다) → ``id`` 없음 → ``id`` 가 빚에 없음(그 id
    의 줄로 사유를 고른다) → 갚음."""
    if not text:
        return "empty", None
    if not has_words(text):
        return "no_words", None
    if not facts.known:
        return "unknown_self", None
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
    prior = facts.by_id
    if prior is not None and prior.settled and prior.closed_by == "unsent":
        return "id_unsent", None
    if prior is not None and prior.settled:
        return "id_replied", None
    return "id_not_owed", None


def _owed_listing(owed) -> str:
    return "; ".join(
        f'[{d.id}] {d.to.split(":", 1)[-1]}: "{d.text[:60]}"' for d in owed
    )


def reply_refusal(verdict: Verdict, text: str, id: str, facts: ReplyFacts) -> str:
    """거절 판정 → 모델이 읽는 글. 런을 연 주소를 모르면(main) "빚 없음" 은 그 주소를 단언하지 않는 글이고, 그 id 의
    줄로 고른 사유(가지 못함·이미 갚음)는 "그런 빚 없음" 으로 말한다 — 지금 글 그대로(3단계 기준 R-1, 사용자 결정 전)."""
    opener_known = facts.opener is not None
    if verdict in ("id_unsent", "id_replied") and not opener_known:
        verdict = "id_not_owed"
    if verdict == "empty":
        return "empty reply — nothing sent"
    if verdict == "no_words":
        return f"reply has no words ({text[:20]!r}) — nothing sent; say it in words"
    if verdict == "unknown_self":
        return "unknown agent"
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
    """루프가 질문 목록에 닿는 **유일한 seam** (DESIGN.md §4).

    콜러블 셋을 ``LoopConfig``·``AgentLoop.__init__``·``run_loop``·
    ``ResidentLoop`` 에 꿰면 15군데가 된다 —
    객체 하나로 묶는다. **레지스트리 자체는 절대 넘기지 않는다**:
    포트의 ``agent_registry``(``kind.ports``)가 "teammate 안 teammate 금지"의 단일
    가드이므로 서브루프에 닿으면 안 된다.

    표면은 ``ask``·``reply``·``debts`` 다. main 도 같은 포트를 받는다
    (``key=None``) — 빚을 갚는 `reply` 가 루프와 무관하게 하나이기 때문이다.
    단 ``ask`` 는 거부한다(아래).
    """

    def __init__(self, registry: AgentRegistry, key: str | None = None):
        self._reg = registry
        self.key = key
        # 이 주체의 **주소**(질문의 target 과 비교) / **발신 라벨**(asker).
        self.me = "main" if key is None else f"agent:{key}"
        self.asker = "main" if key is None else key

    @property
    def nonblocking(self) -> bool:
        """이 주체의 ``ask`` 가 막지 않는가 — 상주 에이전트만 True.

        main 의 ``ask`` 는 사람에게 묻는 기존 블로킹 경로 그대로다(§8-④).
        디스패치와 시스템 프롬프트가 같은 이 값을 본다.
        """
        return self.key is not None

    def ask(self, text: str) -> tuple[str, str]:
        """사람에게 질문 등록 — ``(id, err)``, err 가 비면 성공 (v10.77.0, B).

        주소는 언제나 사람(❓ 트레이)이다. 에이전트·main 에게 무언가 필요하면
        `message` 다 — 그건 요청이고 상대가 `reply` 를 빚진다. 종전의 ``to``
        (원 요청자 기본·에이전트 주소)는 에이전트가 빚지는 질문(q-)과 `answer`
        도구를 만들었는데, 실제 기록에서 `answer` 11회가 전부 실패했다.

        **main 은 거부한다.** main 의 ``ask`` 는 사람에게 묻는 기존 블로킹
        경로(`renderer.prompt_user`) 그대로다. 여기로 오면 로스터에도 창에도 안
        뜨는(둘 다 asker 를 에이전트 키로 찾는다) 질문이 생겨 ``any_activity()``
        를 영구 True 로 만든다.
        """
        if self.key is None:
            return "", (
                "main asks the operator through the blocking `ask` prompt, "
                "not through the question list"
            )
        return self._reg.register_question(self.asker, text)

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

    def awaited(self) -> list[dict]:
        """이 주체가 보내 놓고 아직 받지 못한 회신 (v10.141.0) — 장부의 "기다리는 회신".
        main·에이전트 같은 코드(`AgentRegistry.awaited_replies`)."""
        return self._reg.awaited_replies(self.me)

    @staticmethod
    def _owed_listing(owed) -> str:
        return "; ".join(
            f'[{d.id}] {d.to.split(":", 1)[-1]}: "{d.text[:60]}"' for d in owed
        )

    def _main_reply(self, text: str, *, id: str) -> str:
        """main 이 에이전트에게 빚진 회신을 갚는다 (v10.56.0, 장부 P3 §11.3).

        종전엔 main 의 `agent` 의 옛 요청 모드가 그 에이전트 앞 **가장 오래된**
        빚을 갚은 것으로 쳤다 — id 가 없어 어떤 빚이 닫혔는지 추적되지 않았고,
        "일을 시킨다" 와 "답한다" 가 한 도구에 섞였다. 이제 갚는 도구는 루프와
        무관하게 `reply(id)` 하나다. 배달은 그 에이전트의 inbox 로, 되받을 것
        없이(`expects_reply=False` — 핑퐁 방지)."""
        owed = self._reg.reply_debts(self.me)
        if not owed:
            return (
                "nothing to reply to — no agent is waiting on a reply from you. "
                "To give an agent new work, use `message`."
            )
        listing = self._owed_listing(owed)
        id = str(id or "").strip()
        if not id:
            return (
                "reply needs `id` — which request is this the reply to? You owe: "
                f"{listing}. Send `reply` with that `id`."
            )
        d = next((x for x in owed if x.id == id), None)
        if d is None:
            return f'no unpaid request "{id}" — you owe: {listing}.'
        return self._reg.pay_debt(self.me, d, text)

    def reply(self, text: str, *, id: str = "") -> str:
        """빚진 회신을 갚는다 (v9.21.0) — 에러 메시지 또는 빈 문자열. 요청자가 사라져 배달하지 못했으면 빚은
        닫히고 그 사실이 에러로 온다(`AgentRegistry.pay_debt`, v10.145.2 — main 의 길과 같은 문장).

        ``id`` (v9.25.0) 는 **필수** — 어느 요청에 대한 답인지 하네스가 알아야
        한다. 런 도중 요청이 흡수되면 빚이 여럿이고, 그때 "요청자" 는 하나가
        아니다. 없으면 항상 거부하고 번호 목록을 보여 준다(`complete` 의
        `answers` 와 같은 원칙, 사용자 결정). id 가 전역에서 유일하므로 그것만으로
        상대가 정해진다 — v10.77.0 에서 ``to`` 를 지웠다(C).
        돌려받을 것이 없으므로 `expects_reply=False` 로 간다.
        **빚진 게 없으면 거부한다**(사용자 결정): 회신·질문·독촉으로 시작한
        런엔 답할 상대가 없고, "확인했습니다" 류의 ack 가 정확히 여기서
        새어 나온다. 거부 문구가 `message` 로 안내한다.
        """
        text = (text or "").strip()
        if not text:
            return "empty reply — nothing sent"
        if not has_words(text):
            return f"reply has no words ({text[:20]!r}) — nothing sent; say it in words"
        if self.key is None:
            return self._main_reply(text, id=id)
        tm = self._reg.get(self.key)
        if tm is None:
            return "unknown agent"
        owed = self._reg.reply_debts(self.me)
        if not owed:
            a = tm.current_author
            d = self._reg.reply_debt(self.me, a)
            if d is not None and d.human:
                # 사람이 창에서 직접 시킨 런 — 창이 곧 배달이라 빚이 없다(D8).
                # 종전 문구("요청으로 시작하지 않은 런 … message 를 써라")는
                # 거짓이었고, 그대로 따르면 message(to="user:…") 가 에이전트가
                # 아니라 실패한다.
                return (
                    "nothing to send — your requester is a person watching this "
                    "window, and they see your `complete` here. Just `complete`."
                )
            if d is not None and d.settled and d.closed_by == "unsent":
                return _not_delivered(d)
            if d is not None and d.settled:
                # 이미 갚았다 — 실측(a209hq): player 가 "칙령" 을 보낸 뒤 "칙명"
                # 으로 정정하려다 "요청으로 시작되지 않은 런" 이라는 거짓
                # 사유를 받았다. 사유는 정직해야 다음 행동이 맞아진다.
                return (
                    f"already replied to {a} in this run — a second reply is not "
                    "delivered. If you must correct it, send the correction with "
                    f'`message` (`to` "{a.split(":", 1)[-1]}", `text` your correction).'
                )
            return (
                "nothing to reply to — this run was not started by a request "
                "that expects one (it was a delivered reply, a notice, or an "
                "answer). Do not acknowledge those. If you need something from "
                "someone, use `message` (`to` the recipient, `text` your message)."
            )
        listing = self._owed_listing(owed)
        id = str(id or "").strip()
        if not id:
            return (
                "reply needs `id` — which request is this the reply to? You owe: "
                f"{listing}. Send `reply` with that `id`."
            )
        d = next((x for x in owed if x.id == id), None)
        if d is None:
            prior = self._reg.debt_by_id(self.me, id)
            if prior is not None and prior.settled and prior.closed_by == "unsent":
                return _not_delivered(prior)
            if prior is not None and prior.settled:
                return (
                    f"already replied to [{id}] ({prior.to}) in this run — a second "
                    "reply is not delivered. If you must correct it, send the "
                    f'correction with `message` (`to` "{prior.to.split(":", 1)[-1]}", '
                    "`text` your correction)."
                )
            return f'no unpaid request "{id}" — you owe: {listing}.'
        return self._reg.pay_debt(self.me, d, text)
