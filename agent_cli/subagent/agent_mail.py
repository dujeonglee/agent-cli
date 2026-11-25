"""에이전트 메일의 모양 — 봉투 한 모양(`envelope`)과 main 이 받는 관찰 레코드(`build_reply_record`)·입력창 위
대기열 한 줄(`mail_queue_item`).

레지스트리(`agents_live`)가 메일을 나르고, 여기는 그 메일이 모델에게 어떤 글로 보이는지만 정한다 — 상태를
갖지 않는 잎이다. `build_reply_record` 의 ``registry`` 는 배달 시점의 잔여 상태 한 줄을 읽으려고 받는다
(``get`` 만 본다).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from agent_cli.post import envelope as post_envelope
from agent_cli.post.envelope import Envelope
from agent_cli.subagent.agent_stop import one_line
from agent_cli.subagent.agents_state import WAITING, state_is_active
from agent_cli.token_estimator import estimate_tokens
from agent_cli.wake import harness_record


def envelope(
    kind: str,
    item_id: str,
    who: str,
    body: str,
    *,
    re: str = "",
    todo: str = "",
    mark: str = "",
) -> str:
    """들어오는 배달의 봉투 — 모양 하나 (v10.77.0, D).

    ``── <kind> [<id>] from <who>[ re [<id>]] ──`` 다음 줄부터 본문, 할 일이
    있을 때만 끝에 괄호 한 줄. 종전엔 배달마다 머리줄이 제각각이었다(`agent X
    message`·`agent X reply (success)`·`[question q- from …]`·`[answer to your
    question: …]`). 회신·답은 무엇에 대한 것인지(``re``) 적는다 — 보낸 쪽과
    받은 쪽이 같은 id 를 본다. ``who`` 가 비면 ``from`` 을 뺀다(죽음 통지). ``mark`` 는 머리줄 끝의
    표지 — 사람이 멈춘 에이전트의 회신(``stopped by user``, `agent_stop`)."""
    src = f" from {who}" if who else ""
    ref = f" re [{re}]" if re else ""
    tag = f" · {mark}" if mark else ""
    out = f"── {kind} [{item_id}]{src}{ref}{tag} ──\n{body}"
    return f"{out}\n({todo})" if todo else out


def owe_line(who: str, rid: str) -> str:
    """요청 봉투의 괄호 줄 — 누구에게 무엇으로 갚는가."""
    return f'You owe {who} a `reply` with `id` "{rid}".'


#: 빚진 `message` 없이 런이 끝났을 때 요청자에게 가는 폴백 요약의 머리말
#: (v9.21.0). 라벨이 있어야 받는 쪽이 "명시 회신" 과 구분한다.
_NO_REPLY_LABEL = "(no explicit reply — this is the agent's run summary)"


#: 질문을 닫는 봉투의 종류 (6b) — 사람의 답, 사람이 답 없이 닫음. 둘 다 줄 없는 봉투(회신과 같다)
QUESTION_CLOSERS = ("answer", "closed")


def is_human_addr(addr: str) -> bool:
    """``user`` / ``user:<nick>`` — 사람 주소인가 (§0: 하나의 주체)."""
    return addr == "user" or addr.startswith("user:")


#: 실패로 끝난 런의 회신(폴백 런 요약) 머리 한 줄. 원인은 엔진의 사유(턴 상한·provider 오류 — `ToolResult.error`)
#: 다 — 실패한 런은 산출물이 비어 사유가 이 줄에만 남는다(v10.143.0; 종전엔 버려져 요청자가 "왜" 를 몰랐다).
RUN_ERROR_LINE = "(the run ended with an error)"
RUN_ERROR_CAUSE_LINE = "(the run ended with an error: {error})"
#: 원인 한 줄의 길이 — provider 오류는 응답 본문을 통째로 실을 수 있다.
RUN_ERROR_CHARS = 300


def run_error_line(error: str) -> str:
    """실패한 런의 머리 한 줄 — 원인이 있으면 한 줄로 접어 함께, 없으면(예외 — 사유가 산출물에 있다) 접두만."""
    cause = one_line(error, RUN_ERROR_CHARS)
    return RUN_ERROR_CAUSE_LINE.format(error=cause) if cause else RUN_ERROR_LINE


def format_agent_label(key: str, profile: str = "", name: str = "") -> str:
    """표시 라벨 — 다중 인스턴스 구분: "agt-x (code-writer · ui)"."""
    parts = [p for p in (profile, name) if p]
    return f"{key} ({' · '.join(parts)})" if parts else key


@dataclass(frozen=True)
class MailView:
    """주소 앞에 줄 선(또는 런이 꺼내 간) 배달 하나의 읽기 전용 사본 — 레지스트리의 공개 조회
    (`AgentRegistry.queued_for`·`taken_by`)가 돌려준다. 담는 것은 봉투 머리의 어휘(우편·장부가 읽는 것)와 본문뿐
    이고, main 메일과 에이전트 받은편지함 항목의 저장 모양 차이를 여기서 지운다.

    - ``src``: 보낸 주소 — ``"main"``·``"agent:<key>"``·``"user:<nick>"``, 하니스 통지는 그 종류(``"job"`` 등).
    - ``wakes``: 받는 쪽을 깨우는가 — "Stop everything" 의 정지 회신만 거짓.
    - ``expects_reply``: 받는 쪽이 이것으로 `reply` 를 빚지는가 — 사람의 말·통지·회신은 거짓."""

    id: str
    src: str
    text: str
    re: str = ""
    mark: str = ""
    wakes: bool = True
    expects_reply: bool = False


def view_of_main_mail(mail: dict) -> MailView:
    """main 메일함의 메일 한 통 → :class:`MailView`. 읽기만 한다."""
    key = str(mail.get("key") or "")
    return MailView(
        id=str(mail.get("item_id") or ""),
        src=f"agent:{key}" if key else str(mail.get("kind") or ""),
        text=str(mail.get("output") or mail.get("text") or ""),
        re=str(mail.get("re") or ""),
        mark=str(mail.get("mark") or ""),
        wakes=bool(mail.get("wakes_main", True)),
        expects_reply=bool(mail.get("expects_reply")),
    )


def view_of_inbox_item(item: dict) -> MailView:
    """에이전트 받은편지함 항목 하나 → :class:`MailView`. 읽기만 한다. ``expects_reply`` 는 런 시작·흡수가 빚을 적는
    규칙과 같다 — 회신을 기대하는 항목이고 보낸 이가 사람이 아니다(사람의 빚은 창이 곧 배달이라 갚을 것이 없다)."""
    author = str(item.get("author") or "main")
    return MailView(
        id=str(item.get("id") or ""),
        src=author,
        text=str(item.get("text") or ""),
        re=str(item.get("re") or ""),
        mark=str(item.get("mark") or ""),
        wakes=bool(item.get("opens_run", True)),
        expects_reply=bool(item.get("expects_reply", True))
        and not is_human_addr(author),
    )


# ── 옛 저장 모양 → 봉투 (단일 구조 2단계 — 받은편지함·메일함이 우편(`agent_cli.post`)으로 옮겨 간다) ──────
#
# 봉투의 ``payload`` 는 옛 항목·메일 dict 의 사본이다 — 글 짓기(`view_of_*`·`build_reply_record`)가 그것을 읽는다.
# 우편·장부는 payload 를 열지 않는다.
#
# 규칙은 지금 동작에서 정했다:
# - 에이전트 항목: 사람 author(답 ``kind: "answer"`` 포함) → `human` — 런 시작과 흡수가 사람 주소의 빚을 적는다
#   (6b: 사람의 답·닫힘은 줄 없는 봉투 — `QuestionsMixin._deliver_to_asker`). 봉투 재료 없는 회신 기대 요청 → `request` — `_enqueue` 가
#   보낸 쪽 기록을, 시작·흡수가 빚을 적는다. ``kind`` reply → `reply`, notice 또는 회신 기대 없음 → `notice`, 그 밖의
#   종류(job·monitor·checkin) → `system`(줄 없음 — 하니스 통지는 에이전트에서 항목이 아니다).
#   ``opens_run`` 거짓 → 깨우지 않음.
# - main·private 메일: peer_message 의 ``expects_reply`` → `request` — main 이 빚을 적는다(`AgentLoop._inject_mail`);
#   ``notice`` → `notice`; ``re`` 가 있으면(명시 회신·정지 회신) 또는 kind reply(폴백) → `reply`; 그 밖
#   (job·monitor·harness·died·checkin) → `system`, 줄 없음 — main 앞 메일의 n- 항목은 루프 장부(`Ledger`)가 들고
#   있다. ``wakes_main`` 거짓 → 깨우지 않음.


def envelope_of_item(item: dict, dst: str) -> Envelope:
    """에이전트 받은편지함 항목 → 봉투. 읽기만 한다."""
    author = str(item.get("author") or "main")
    kind = str(item.get("kind") or "")
    common = {
        "id": str(item.get("id") or ""),
        "src": author,
        "dst": dst,
        "text": str(item.get("text") or ""),
        "ts": float(item.get("ts") or 0.0),
        "payload": dict(item),
    }
    wakes = item.get("opens_run", True) is not False
    if kind in QUESTION_CLOSERS:
        # 사람의 답·닫힘 (6b) — 회신과 같은 줄 없는 봉투: 질문 줄은 그 배달이 닫는다
        env = post_envelope.reply(**common, re=str(item.get("re") or ""), wakes=wakes)
    elif is_human_addr(author) and not kind:
        env = post_envelope.human(
            **common, re=str(item.get("re") or ""), answers=item.get("answers") or ()
        )
    elif not kind and item.get("expects_reply", True):
        env = post_envelope.request(
            **common,
            answers=item.get("answers") or (),
        )
    elif kind == "reply":
        env = post_envelope.reply(
            **common,
            re=str(item.get("re") or ""),
            mark=str(item.get("mark") or ""),
            wakes=wakes,
        )
    elif kind in ("", "notice"):
        env = post_envelope.notice(**common)
    else:
        env = post_envelope.system(**common)
    return replace(env, wakes=wakes and env.wakes)


def is_human_request(item: dict) -> bool:
    """받은편지함 항목이 사람의 말(``u-`` — `complete` 의 ``answers`` 로 닫는 요청)인가 — 봉투 머리 하나로 판정한다
    (`post.envelope.is_human_words`). 사람이 보낸 답은 사람의 말이 아니다(6b)."""
    return post_envelope.is_human_words(envelope_of_item(item, ""))


def envelope_of_main_mail(mail: dict, dst: str = "main") -> Envelope:
    """main(또는 private) 메일함의 메일 → 봉투. 읽기만 한다 — ``item_id`` 가 찍힌 뒤에 부른다."""
    key = str(mail.get("key") or "")
    kind = str(mail.get("kind") or "")
    common = {
        "id": str(mail.get("item_id") or ""),
        "src": f"agent:{key}" if key else kind,
        "dst": dst,
        "text": str(mail.get("output") or mail.get("text") or ""),
        "payload": dict(mail),
    }
    wakes = bool(mail.get("wakes_main", True))
    if kind in QUESTION_CLOSERS:
        # 사람의 답·닫힘 (6b) — 에이전트의 받은편지함과 같은 줄 없는 봉투
        env = post_envelope.reply(**common, re=str(mail.get("re") or ""))
    elif kind == "peer_message" and mail.get("expects_reply"):
        env = post_envelope.request(**common, answers=mail.get("answers") or ())
    elif kind == "peer_message" and mail.get("notice"):
        env = post_envelope.notice(**common)
    elif kind == "reply" or (kind == "peer_message" and mail.get("re")):
        env = post_envelope.reply(
            **common,
            re=str(mail.get("re") or ""),
            mark=str(mail.get("mark") or ""),
            answers=mail.get("answers") or (),
        )
    else:
        env = post_envelope.system(**common)
    return replace(env, wakes=wakes and env.wakes)


def mail_queue_item(reply: dict) -> dict:
    """미배달 메일 1건 → 입력창 위 대기열의 한 줄 (v10.51.0).

    사용자 요청이 main 이 바쁠 때 서는 그 대기열에 같은 모양으로 선다 —
    ``{id, conn_id, nickname, text}`` 에 ``mail: True``. 글은 **곧 들어갈
    관찰의 머리줄**이라 대기열 줄과 타임라인의 관찰 줄이 같은 말을 한다.
    ``conn_id`` 가 비어 아무도 취소할 수 없다(메일은 취소 대상이 아니다)."""
    rec = build_reply_record(reply)
    lines = [ln.strip() for ln in str(rec.get("content") or "").splitlines()]
    lines = [ln for ln in lines if ln]
    tool = rec.get("tool") or "agent"
    head = lines[0].strip("─ ").strip() if lines else ""
    if head == tool and len(lines) > 1:
        head = lines[1]  # 하니스 작업: 머리줄이 `── auditor ──` 뿐이다
    return {
        "id": str(reply.get("item_id") or ""),
        "conn_id": "",
        "nickname": tool,
        "text": head,
        "mail": True,
    }


def build_reply_record(reply: dict, *, cap: int = 0, registry=None) -> dict:
    """mailbox 아이템 1건 → main ctx 에 넣을 관찰 레코드.

    에이전트에게서 온 것은 봉투 하나(:func:`envelope`, v10.77.0 D) — 요청·통지·
    회신·죽음. 머리의 id 는 그 메일의 id(`item_id`)이고, 요청이면 main 이 그
    id 로 빚진다. 작업·모니터·하니스 보고는 도구가 만든 보고라 봉투 밖이다.

    ``cap``(loop 의 ``_oversized_cap``, 0=무제한) 초과 회신은 전문 대신
    디스크 포인터 + head 발췌로 치환 — 전문은 worker 가 이미
    ``agents/<key>/replies/reply-<id>.md`` 에 영속했다(배달과 무관하게
    항상 저장).

    ``registry`` (v7.11.0, 배달 시점에만 전달): 회신 레코드 말미에 그
    에이전트의 **배달-시점** 잔여 상태 한 줄을 동봉 — main 이 "얼마나
    밀렸는지" 보고 다음 요청/대기를 판단한다. 잔여가 있으면 남은 회신도
    자동 배달됨을 같이 안내(status 폴링 넛지와 정합 — 이 줄이 폴링을
    유발하면 안 됨). died/peer 레코드에는 붙이지 않는다.
    """
    key = reply.get("key", "")
    label = format_agent_label(key, reply.get("profile", ""), reply.get("name", ""))
    mid = str(reply.get("item_id") or "")

    if reply.get("kind") == "harness":
        # 하니스 작업의 결과 (v10.42.0, harness/jobs) — 팀원 회신이 아니다:
        # 돌려줄 주소도, 빚도 없다. 감사 보고가 여기로 온다. `tool=<job>` 은
        # monitor 와 같은 이유로 등록이 필요 없다(아래 주석).
        job = reply.get("job") or "harness"
        body = reply.get("output") or "(empty result)"
        return {
            "role": "user",
            "tool": job,
            "success": bool(reply.get("success")),
            "content": (
                f"── {job} ──\n{body}\n"
                "(A harness job's result — nothing to send back. Act on findings "
                "if there are any; otherwise there is nothing to do.)"
            ),
            "source": "harness",
        }

    harness = harness_record(reply)
    if harness is not None:
        # 잡·모니터·체크인 — 도구가 만든 보고라 봉투 밖. 상주의 흡수·one-shot 의 메일함과
        # **같은 함수**(v10.89.0). 모니터의 `tool="monitor"` 는 빈 문자열이 아니어야 한다:
        # `is_format_intervention` 이 `tool == ""` 를 형식 개입으로 읽는다.
        return harness

    if reply.get("kind") in QUESTION_CLOSERS:
        # 사람의 답·닫힘 (6b) — 에이전트가 받은편지함에서 읽는 것과 같은 봉투·같은 글
        return {
            "role": "user",
            "tool": "ask",
            "success": True,
            "content": envelope(
                str(reply.get("kind")),
                mid,
                "the user",
                str(reply.get("output") or ""),
                re=str(reply.get("re") or ""),
                todo=str(reply.get("todo") or ""),
            ),
        }

    if reply.get("kind") == "died":
        # worker 사망 통지 (Q4): kill/세션종료가 아닌 비정상 종료 — main 이
        # status 를 조회하기 전에 능동적으로 알린다.
        reason = reply.get("output") or "worker terminated unexpectedly"
        content = envelope(
            "died",
            mid,
            "",
            f"{label}: {reason}",
            todo=(
                "Its queued requests were lost, but its context is kept — bring "
                f'it back with `agent` `mode` "resume" and `key` "{key}" to '
                "continue with full memory, or spawn a new one. If it died from "
                "a crash, the same cause may recur."
            ),
        )
        return {
            "role": "user",
            "tool": "agent",
            "success": False,
            "content": content,
            "source": "agent_died",
        }

    if reply.get("kind") == "peer_message":
        # 에이전트가 main 에게 보낸 메시지 — 통지·요청·회신 셋으로 갈린다(아래).
        msg = reply.get("output") or "(empty message)"
        if reply.get("notice"):
            # v10.1.0: 통지 — 빚도 회신도 아니다. 할 일이 없으니 괄호 줄도 없다.
            content = envelope("notice", mid, label, msg)
        elif reply.get("expects_reply"):
            # v9.22.0: `message` — 회신을 빚진다(빚 id = 이 메일의 id).
            rid = str(reply.get("debt_id") or mid)
            content = envelope("request", rid, label, msg, todo=owe_line("it", rid))
        else:
            # `reply` — main 이 보낸 요청의 답. 되받아칠 것 없음.
            content = envelope(
                "reply",
                mid,
                label,
                msg,
                re=str(reply.get("re") or ""),
                mark=str(reply.get("mark") or ""),
            )
        return {
            "role": "user",
            "tool": "agent",
            "success": True,
            "content": content,
            "source": "agent_message",
        }

    body = reply.get("output") or "(empty reply)"

    tokens = estimate_tokens(body)
    if cap and tokens > cap:
        path = reply.get("reply_path", "")
        head = body[: cap * 2]  # ~cap/2 tokens 어치만 발췌 (chars≈tokens*4)
        body = (
            f"(reply is ~{tokens} tokens — over the {cap}-token cap; "
            f'full text saved to "{path}". Read a specific range or search '
            f"it, or send a narrower request.)\n"
            f"--- head excerpt ---\n{head}"
        )

    if not reply.get("success"):
        body = f"{run_error_line(str(reply.get('error') or ''))}\n{body}"
    # 런 끝 폴백 — 표지는 이 회신이 갚는 요청(``re``)이다. 그 런을 연 배달(``run``)이 아니다: 체크인·하위 회신이 연
    # 런이나 런 도중 흡수한 둘째 요청의 폴백이면 둘이 다르고, main 은 ``run`` 을 본 적이 없다(v10.145.1). 이 분기로
    # 오는 메일은 `_fall_back` 의 것뿐이고 언제나 ``re`` 를 단다.
    content = envelope("reply", mid, label, body, re=str(reply.get("re") or ""))
    tm = registry.get(key) if registry is not None else None
    if tm is not None and getattr(tm, "state", "dead") != "dead":
        queued = registry.backlog_of(key)
        if queued > 0 or state_is_active(tm.state):
            # 기다리는 중이면(우편의 기다림 표시) 작업 상태 대신 그 낱말로 — 꼬리의 `## Live Agents` 와 같다
            now = WAITING if registry.is_waiting(f"agent:{key}") else tm.state
            tail = (
                f"(agent status: {now} · {queued} queued — remaining "
                "replies arrive automatically; no need to poll.)"
            )
        else:
            tail = "(agent status: idle — ready for new requests.)"
        content = f"{content}\n{tail}"
    return {
        "role": "user",
        "tool": "agent",
        "success": bool(reply.get("success")),
        "content": content,
        # additive 마킹 — tool="" (형식-개입 레거시) 오인 금지 계약과 짝.
        "source": "agent_reply",
    }
