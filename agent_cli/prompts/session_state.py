"""Session-state block — volatile state appended to the LAST message.

Why the tail (v8.46.0). Everything the model reads is one token sequence, and
providers cache a KV *prefix*: reuse ends at the first token that differs from
the previous call. So WHERE volatile text sits decides what it costs.

- In the system prompt (where ``Session Memory`` and the live agent roster used
  to live) a change invalidates the prefix from that section onward — which is
  the rest of the system prompt PLUS the entire conversation. One ``memory
  add`` mid-session forced a full re-prefill of tens of thousands of tokens.
  That pressure is why the roster deliberately carried membership only and no
  busy/idle state, and why ``_build_environment_section`` omits the date.
- Right after the USER'S REQUEST looks tempting but is the worst place in an
  agent loop: the request sits near the FRONT (one query, then N turns of
  assistant/observation grow after it), so a per-turn block there invalidates
  every turn that follows it.
- At the TAIL it is free. Turn N ends with ``… obs_{N-1} + STATE_N``; turn N+1
  is ``… obs_{N-1}, assistant_N, obs_N + STATE_{N+1}``. The prefix match ends
  at ``obs_{N-1}`` — exactly where it would have ended anyway, because
  ``assistant_N`` and ``obs_N`` are new tokens regardless. The only thing lost
  is the previous block's own ~50 cached tokens.

The tail is also where recency attention is strongest, which is what this
content wants: it is the state the model should be deciding against RIGHT NOW.

Delivery is by appending to the last message rather than adding a new one:
providers hand ``messages`` to the server verbatim (``providers/anthropic.py``
sends the list as-is), so an extra trailing ``role=user`` message would create
consecutive same-role turns whose handling differs per provider. Appending
keeps the message count unchanged. Same mechanism as ``_OBS_COMPLETE_NUDGE``:
applied at feed time in ``ContextManager.get_messages``, on a copy, and NEVER
persisted to history.jsonl (a stored block would re-feed stale numbers every
turn afterwards and pollute resume previews).
"""

from __future__ import annotations

#: 압축 안내가 뜨는 사용량 단계 (v9.26.4). 압축은 호출별 목표치(꼬리의 100%)를
#: 넘는 순간 돌고, 안내는 **각 단계를 처음 넘는 턴에 한 번씩**만 — 70·80·90%.
#: 종전(75% 이상 매 턴)은 같은 경보가 턴마다 쌓여 모델을 과하게 보수적으로 만들었다
#: (사용자 보고). 압축이 돌아 사용량이 내려가면 그 단계는 다시 무장된다
#: (:func:`compaction_notice_due`). 문구도 결핍이 아니라 사실을 말한다 — 오래된 턴은
#: 사라지는 게 아니라 구조화 요약으로 대체되고 작업은 그대로 이어진다. 압박을
#: 알리면 조기 ``complete`` 를 부른다는 ``_OBS_COMPLETE_NUDGE`` 의 실측은 그대로다.
COMPACTION_NOTICE_STEPS: tuple[float, ...] = (0.7, 0.8, 0.9)

#: 안내 문구 — 정보만, 명령은 하나(memory 는 선택지). "nearly full"·"lose"·"NOW"
#: 같은 결핍 어휘를 쓰지 않는다. 사용자가 정한 문구 그대로(2026-10-01).
COMPACTION_NOTICE = (
    "ℹ Context compaction is coming up: older turns will be replaced by a "
    "structured summary (task, state, decisions, failures, key facts) and work "
    "continues as usual. If a detail is worth more than its summary — an exact "
    "identifier, a failed approach — memory(mode=add) keeps it verbatim. Pace "
    "and scope stay the same."
)


def compaction_notice_due(
    used_tokens: int, budget_tokens: int, armed: set[float]
) -> tuple[bool, set[float]]:
    """``(안내를 띄울지, 다음 턴의 armed)`` — 처음 넘은 단계가 있으면 True.

    ``armed`` 는 아직 안 띄운 단계. 사용량이 어느 단계 아래로 내려가면(압축 뒤)
    그 단계는 다시 무장된다. 예산이 없으면 조용하다."""
    if budget_tokens <= 0:
        return False, set(COMPACTION_NOTICE_STEPS)
    pct = used_tokens / budget_tokens
    rearmed = {t for t in COMPACTION_NOTICE_STEPS if pct < t}
    crossed = {t for t in armed if pct >= t}
    return bool(crossed), (armed - crossed) | rearmed


#: Marker line that opens the block. Public so callers (and tests) can
#: locate the boundary between the conversation and the appended state.
SESSION_STATE_HEADER = (
    "── session state (context only — not part of the conversation) ──"
)

#: Task Guidelines 세그먼트의 헤더 (v8.52.1). 가이드라인을 SESSION_STATE_HEADER
#: **아래**에 넣었더니 그 헤더의 자기-면책 문구("not part of the conversation")
#: 가 규칙까지 무시해도 되는 메타데이터로 만들었다 — Harbor tb21 실측:
#: 태스크 본문의 "hard rules" 는 1/1 준수, 면책 헤더 아래 같은 문장은 0/1
#: (백업 없이 DB 를 열어 WAL 4번째 소실). 규칙은 상태가 아니므로 자기
#: 헤더("always in effect")를 갖고 상태 블록 앞에 선다.
RULES_HEADER = "── standing rules (always in effect) ──"

#: 꼬리 전체의 첫 줄 (v9.25.2). 꼬리는 마지막 사용자 메시지 **본문에 이어**
#: 붙으므로, 모델이 받는 메시지는 "이건 왜 필요한거야?\n\n── standing rules ──
#: ## Task Guidelines …" 이다 — "이건" 의 가장 가까운 후보가 가이드라인이고,
#: 모델은 이 블록을 사용자가 쓰지도 보지도 않는다는 것을 알 길이 없다(회사
#: 실측: 그 질문에 Task Guidelines 를 설명했다). 규칙이 아니라 **사실**을
#: 준다: 사용자가 볼 수 없는 글은 사용자의 "이건" 이 될 수 없다. "무시해도
#: 된다" 로 읽힐 말은 넣지 않는다 — 규칙 헤더의 "always in effect" 는 그대로
#: (tb21: 면책 문구 아래의 규칙은 지켜지지 않았다).
TAIL_BOUNDARY = (
    "── end of message · everything below is added by the harness on every "
    "turn — the user neither writes nor sees it ──"
)


def _context_line(used: int, budget: int, turn: int, max_turns: int) -> str:
    """``budget`` is the live compaction target, so the percentage is "how
    far to compaction". It is spelled out (v9.25.1) because a reader —
    human or model — took 90% for an alarm and wondered why nothing ran:
    compaction fires only once the cache passes the target."""
    parts = []
    if budget > 0:
        pct = min(100, round(used * 100 / budget))
        note = "compaction at 100%" if pct < 100 else "compaction due"
        parts.append(f"context: ~{used:,} / {budget:,} tokens ({pct}% — {note})")
    elif used:
        parts.append(f"context: ~{used:,} tokens")
    if turn:
        if max_turns:
            left = max(0, max_turns - turn)
            parts.append(f"turn {turn}/{max_turns} ({left} left after this one)")
        else:
            parts.append(f"turn {turn}")
    return " · ".join(parts)


def final_turn_notice(*, reports_to_caller: bool) -> str:
    """The last-allowed-turn instruction (v9.24.1).

    At the turn cap the loop simply stops (``_on_max_turns``) — no wrap-up
    turn. Measured (Harbor, v5 large-scale): a sub-agent spent 60 turns and
    12 minutes, was still planning its next experiment on turn 60, and its
    caller received only a list of commands — every finding, including "I
    overwrote /app/mac.vim during a probe", was lost. So on the final turn
    the model is told plainly that the run ends after it and what to put in
    ``complete``. It asks for a status report, never for writing unfinished
    work into the task's deliverables."""
    who = (
        "The agent that called you receives ONLY what you put in complete — "
        "anything you do not write there is lost."
        if reports_to_caller
        else "The user receives what you put in complete."
    )
    return (
        "⚠ This is your LAST turn — the run ends after it, whatever you emit. "
        "Call complete now instead of starting new work. In the result, report "
        "what you found: what you verified (and how), what you believe but did "
        "not verify, what is still open, and every file you created, changed or "
        f"damaged. {who}"
    )


def build_session_state(
    *,
    used_tokens: int = 0,
    budget_tokens: int = 0,
    turn: int = 0,
    max_turns: int = 0,
    agents: str = "",
    memory: str = "",
    requests: str = "",
    debts: str = "",
    guidelines: str = "",
    reports_to_caller: bool = False,
    compaction_notice: bool = False,
) -> str:
    """Render the block, or ``""`` when there is nothing worth saying.

    ``compaction_notice`` (v9.26.4): 이번 턴에 압축 안내 한 줄을 붙일지 — 호출자가
    :func:`compaction_notice_due` 로 단계(70·80·90%)를 처음 넘는 턴에만 True 를 준다.

    A non-empty block always opens with ``TAIL_BOUNDARY`` (v9.25.2) so the
    model can tell where the user's text ends and the harness's begins.

    ``reports_to_caller`` (v9.24.1) selects the final-turn wording: a
    sub-agent or skill loop (``depth > 0``) hands its ``complete`` result to
    the agent that called it; the main loop hands it to the user.

    ``guidelines`` is ``TASK_GUIDELINES`` verbatim (v8.52.0 — the WHOLE
    section moved here from the system prompt's primacy zone: bench-measured,
    the 35B-class model ignored these principles there but follows them at the
    tail; static text, so the only cost is re-prefilling ~0.4K tokens that the
    un-cacheable tail region would re-process anyway).
    ``requests`` (v9.16.0) is the outstanding-user-request list, rendered when
    a drain merged two or more requests into this run. It rides here rather
    than as a one-shot injection at drain time: a message injected once gets
    pushed far back as the turn grows, and the model reads it nowhere near the
    moment it calls ``complete`` (live session cgyx7z — it saw the list and
    omitted ``answers`` anyway). The tail is re-read every turn and is not
    persisted to history.

    ``debts`` (v9.24.5) is the run's unpaid message/ask debts
    (``constants.owed_replies_block``) — right after ``requests``: both are
    "who this run still has to answer".

    ``agents`` / ``memory`` are the already-rendered sections
    (``build_live_agents_section(include_state=True)`` / ``memory.render_index``)
    — passed in rather than fetched here so this stays a pure function and the
    model keeps seeing the SAME headings it saw when these lived in the system
    prompt (nothing to re-learn from the move).
    """
    # ``requests`` 를 먼저 — 미답 요청은 "지금 무엇을 결정해야 하나" 에
    # 가장 가깝다. 꼬리 안에서도 앞이 눈에 띈다.
    blocks = [
        b
        for b in (requests.strip(), debts.strip(), agents.strip(), memory.strip())
        if b
    ]
    ctx_line = _context_line(used_tokens, budget_tokens, turn, max_turns)
    rules = guidelines.strip()
    if not ctx_line and not blocks and not rules:
        return ""

    lines: list[str] = []
    if rules:
        lines.append(RULES_HEADER)
        lines.append(rules)
    if ctx_line or blocks:
        if lines:
            lines.append("")
        lines.append(SESSION_STATE_HEADER)
        if ctx_line:
            lines.append(ctx_line)
        for b in blocks:
            lines.append("")
            lines.append(b)

    if max_turns and turn >= max_turns:
        lines.append("")
        lines.append(final_turn_notice(reports_to_caller=reports_to_caller))

    if compaction_notice:
        lines.append("")
        lines.append(COMPACTION_NOTICE)
    return "\n".join([TAIL_BOUNDARY, "", *lines])
