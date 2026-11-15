"""에이전트 메일의 모양 — 봉투 한 모양(`envelope`)과 main 이 받는 관찰 레코드(`build_reply_record`)·입력창 위
대기열 한 줄(`mail_queue_item`).

레지스트리(`agents_live`)가 메일을 나르고, 여기는 그 메일이 모델에게 어떤 글로 보이는지만 정한다 — 상태를
갖지 않는 잎이다. `build_reply_record` 의 ``registry`` 는 배달 시점의 잔여 상태 한 줄을 읽으려고 받는다
(``get`` 만 본다).
"""

from __future__ import annotations

from agent_cli.subagent.agents_state import state_is_active
from agent_cli.token_estimator import estimate_tokens
from agent_cli.wake import harness_record

#: 배달된 peer 회신의 꼬리표 (v10.1.0 문구). 종전 "`complete` alone reports to no
#: one" 은 받은 쪽이 작별 인사를 `message` 로 되보내게 만들었고(빚 생성 → 핑퐁),
#: 실측(cnr60g) 게임 종료 뒤 작별 인사만 다섯 왕복이 돌았다. 되받아칠 것이
#: 없고 ack 도 원치 않는다는 사실을 말한다.


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


def is_human_addr(addr: str) -> bool:
    """``user`` / ``user:<nick>`` — 사람 주소인가 (§0: 하나의 주체)."""
    return addr == "user" or addr.startswith("user:")


def format_agent_label(key: str, profile: str = "", name: str = "") -> str:
    """표시 라벨 — 다중 인스턴스 구분: "agt-x (code-writer · ui)"."""
    parts = [p for p in (profile, name) if p]
    return f"{key} ({' · '.join(parts)})" if parts else key


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
    ``teammates/<key>/replies/reply-<id>.md`` 에 영속했다(배달과 무관하게
    항상 저장 — P3 resume 미배달 보존의 토대).

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
        # 상주 에이전트가 main 에게 먼저 보낸 메시지 (v5.11) — 회신 대기
        # 아님. main 은 필요하면 `message` 로 답한다.
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
        body = f"(the run ended with an error)\n{body}"
    # 런의 회신(라벨 붙은 런 요약 폴백 포함) — 그 런을 연 요청이 ``run``
    content = envelope("reply", mid, label, body, re=str(reply.get("run") or ""))
    tm = registry.get(key) if registry is not None else None
    if tm is not None and getattr(tm, "state", "dead") != "dead":
        queued = tm.inbox.qsize()
        if queued > 0 or state_is_active(tm.state):
            tail = (
                f"(agent status: {tm.state} · {queued} queued — remaining "
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
