"""record → LLM 표현 렌더 (C5, v4.47.0).

재공급 NL(_to_natural_language/_convert_observation/_context_view),
예산 추정(_estimate_message_tokens — 재공급과 같은 view 를 세는 쌍둥이),
압축 요약 입력(_to_summary_text). 전부 무상태(인자만) — manager 의
캐시/정책과 결합 없음.
"""

from __future__ import annotations

import json

from agent_cli.context.token_estimator import estimate_tokens

# ── Defaults / constants ─────────────────────────────────
DEFAULT_TOKEN_BUDGET = 100_000


def _context_view(message: dict) -> dict:
    """An assistant turn as it should appear in RE-FED context: each op's
    ``action_input`` passed through its tool's
    ``render_action_input_for_context`` (default identity — so this is a no-op
    for every tool today; the seam is consulted by both the render path
    (:func:`_to_natural_language`) and the budget path
    (:func:`_estimate_message_tokens`) so the two always agree).

    Returns ``message`` unchanged (same object) when nothing is elided, else a
    shallow copy with rewritten ops — the source record (history.jsonl + cache)
    is never mutated.
    """
    if message.get("role") != "assistant":
        return message
    from agent_cli.tools import TOOLS  # lazy: registry → context.render cycle

    def _view(action: str, ai):
        if not action or not isinstance(ai, dict):
            return ai
        tool = TOOLS.get(action)
        return tool.render_action_input_for_context(ai) if tool else ai

    ops = message.get("ops")
    if isinstance(ops, list):
        new_ops = list(ops)
        changed = False
        for i, op in enumerate(ops):
            if not isinstance(op, dict):
                continue
            ai = op.get("action_input")
            view = _view(op.get("action"), ai)
            if view is not ai:
                new_ops[i] = {**op, "action_input": view}
                changed = True
        return {**message, "ops": new_ops} if changed else message

    ai = message.get("action_input")
    view = _view(message.get("action"), ai)
    return {**message, "action_input": view} if view is not ai else message


def _estimate_message_tokens(msg: dict) -> int:
    """Estimate tokens for a single message dict."""
    msg = _context_view(msg)  # count what is actually re-fed (elided body)
    total = 4  # role + formatting overhead
    for key in ("content", "thought", "action_input"):
        val = msg.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            total += estimate_tokens(val)
        elif isinstance(val, dict):
            total += estimate_tokens(json.dumps(val, ensure_ascii=False))
    action = msg.get("action", "")
    if action:
        total += estimate_tokens(action)
    # Multi-op (json_fc 기본 / xml_fc) assistant records carry their
    # action(s) + action_input + complete result inside ``ops`` — count them,
    # else every assistant turn is undercounted to just its ``thought`` (a
    # large write_file content arg / complete result would be invisible to the
    # budget estimator).
    ops = msg.get("ops")
    if isinstance(ops, list):
        for op in ops:
            if not isinstance(op, dict):
                continue
            op_action = op.get("action")
            if op_action:
                total += estimate_tokens(op_action)
            op_input = op.get("action_input")
            if isinstance(op_input, str):
                total += estimate_tokens(op_input)
            elif isinstance(op_input, dict):
                total += estimate_tokens(json.dumps(op_input, ensure_ascii=False))
    artifact = msg.get("artifact", "")
    if artifact:
        total += estimate_tokens(artifact)
    return total


def _sum_message_tokens(messages) -> int:
    """Estimated token total for a message list — the single expression for
    ``sum(_estimate_message_tokens(...))`` used across the cache (re)builds
    (resume restore, compaction evict, force_fit)."""
    return sum(_estimate_message_tokens(m) for m in messages)


def render_history_message(
    msg: dict, dialect, *, index: int, assistant_index: int | None
) -> list[dict]:
    """레코드 하나 → 요청 메시지 **목록** (v10.2.0). 방언이 관찰 렌더를 소유한다:
    `render_observation_from_history` 가 None 이면 종전 user 텍스트 한 건, 아니면
    그 목록(서버 파싱 방언은 op 마다 `tool` 메시지). assistant 는 `index` 를 받아
    호출 id 를 합성한다. 텍스트 방언에서는 바이트 동일한 한 건이다."""
    role = msg.get("role", "user")
    if role == "user" and msg.get("tool"):
        rendered = dialect.render_observation_from_history(
            msg, index=index, assistant_index=assistant_index
        )
        if rendered is not None:
            return list(rendered)
        return [_convert_observation(msg)]
    if role == "user":
        return [{"role": "user", "content": msg.get("content", "")}]
    try:
        return [dialect.render_assistant_from_history(_context_view(msg), index=index)]
    except TypeError:
        # 서드파티 플러그인이 옛 시그니처(record 만)를 구현한 경우
        return [dialect.render_assistant_from_history(_context_view(msg))]


def _to_natural_language(msg: dict, dialect) -> dict:
    """Convert a JSON history record to a natural-language message for the LLM.

    Input formats (from history.jsonl):
        User input:     {"role":"user", "content":"..."}
        Tool result:    {"role":"user", "tool":"...", "args":{...}, "content":"...", "artifact":"..."}
        Assistant act:  {"role":"assistant", "thought":"...", "action":"...", "action_input":{...}}
        Complete:       {"role":"assistant", "thought":"...", "action":"complete", "action_input":{"result":"..."}}

    Output format (for chat completion):
        {"role": "user"|"assistant", "content": "...natural language..."}

    Assistant records are handed off to ``dialect.render_assistant_
    from_history`` so each plugin owns the on-disk → message conversion
    for its own format. The user / tool branches live here because they
    are format-agnostic.
    """
    role = msg.get("role", "user")

    if role == "user":
        tool = msg.get("tool")
        if tool:
            return _convert_observation(msg)
        return {"role": "user", "content": msg.get("content", "")}

    # Re-feed the context view (bulky action_input bodies elided per-tool;
    # default identity → unchanged today). History/cache stay faithful.
    return dialect.render_assistant_from_history(_context_view(msg))


_SUMMARY_CONTENT_EXCERPT = 200  # chars of tool-result content kept per line


def _to_summary_text(msg: dict) -> str:
    """Render one history record as a single natural-language line for the
    summarisation transcript.

    Unlike ``_to_natural_language`` (which round-trips assistant turns back
    to the wire shape — ReAct JSON — for resume/recovery self-reinforcement),
    this is for the *summariser's* input: assistant turns become prose and
    tool args are summarised (``write_file`` → path only, no file body), so
    the model sees a transcript to summarise rather than a ReAct conversation
    to continue. Keeping the wire shape here made a small model emit another
    ``write_file`` action instead of a summary.
    """
    role = msg.get("role", "user")

    if role == "user":
        tool = msg.get("tool")
        if not tool:
            return f"User: {msg.get('content', '')}"
        header = f"[{tool}]"
        content = str(msg.get("content", "") or "").strip()
        if content:
            excerpt = content[:_SUMMARY_CONTENT_EXCERPT]
            if len(content) > _SUMMARY_CONTENT_EXCERPT:
                excerpt += "…"
            header += f" → {excerpt}"
        artifact = msg.get("artifact", "")
        if artifact:
            header += f" → {artifact}"
        return header

    # assistant
    ops = msg.get("ops")
    if not ops and "action" not in msg and "thought" not in msg:
        return f"Assistant: {msg.get('content', '')}"
    thought = (msg.get("thought") or "").strip()
    # Normalize single-op ({action, action_input}) and multi-op ({ops:[...]})
    # records to one op list. Multi-op formats (json_fc, xml_fc) store `ops`,
    # so reading only the top-level `action` here lost EVERY tool label for
    # them — the summariser saw thought-only prose with no record of which
    # tools ran.
    op_list = ops if isinstance(ops, list) else ([msg] if msg.get("action") else [])
    # Delegate the per-action label to the tool itself (sibling of
    # touched_paths): each tool reads its OWN prefixed/array action_input
    # shape. Lazy import avoids the module-load cycle (registry →
    # context-tool → context.render).
    from agent_cli.tools.registry import TOOLS

    action_lines: list[str] = []
    for op in op_list:
        if not isinstance(op, dict):
            continue
        action = op.get("action") or ""
        if not action:
            continue
        tool = TOOLS.get(action)
        # Stored ops are FLAT (the model's emission, e.g. read_file `{path}`);
        # summary_arg reads the tool's CANONICAL shape (`read_file_reads[]`).
        # Normalize flat → canonical (idempotent on already-canonical input).
        ai = op.get("action_input") or {}
        arg_summary = tool.summary_arg(tool.wrap_single_op(ai)) if tool else ""
        action_lines.append(f"  → action: {action}({arg_summary})")
    head = f"Assistant: {thought}" if thought else "Assistant:"
    return head + "\n" + "\n".join(action_lines) if action_lines else head


def _convert_observation(msg: dict) -> dict:
    """Convert a tool result message to natural language."""
    tool = msg.get("tool", "")
    content = msg.get("content", "")
    artifact = msg.get("artifact", "")

    # Tool-result records carry no args (history.jsonl stores only
    # {role, tool, success, content}), so there is nothing to label here.
    parts = [f"[{tool}]"]
    if content:
        parts.append(content)
    if artifact:
        parts.append(f"→ {artifact}")

    return {"role": "user", "content": "\n".join(parts)}


def message_label(msg: dict) -> str:
    """사람이 보는 역할 라벨 — ``tool`` 메시지는 어느 호출의 결과인지 ``tool_call_id``
    를 붙인다(native_fc, v10.2.1). 인스펙터 섹션 이름이 쓴다."""
    role = str(msg.get("role", "?"))
    call_id = msg.get("tool_call_id")
    return f"{role} {call_id}" if role == "tool" and call_id else role


def message_display_text(msg: dict) -> str:
    """사람이 보는 메시지 본문 — ``content`` 에 더해, native_fc 가 구조 필드로 보내는
    ``tool_calls`` 를 한 줄씩 적는다(v10.2.1). 인스펙터·verbose ``context`` 레코드가
    같은 함수를 써서, 모델이 받은 것이 ``content`` 밖에 있어도 빈 카드가 되지 않는다.
    문법·토큰 추정과는 무관한 표시 전용이다."""
    content = msg.get("content")
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    else:
        text = json.dumps(content, ensure_ascii=False, default=str)
    calls = msg.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return text
    lines = []
    for call in calls:
        if not isinstance(call, dict):
            lines.append(str(call))
            continue
        fn = call.get("function") or {}
        name = fn.get("name", "?") if isinstance(fn, dict) else "?"
        args = fn.get("arguments", "") if isinstance(fn, dict) else ""
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False, default=str)
        call_id = call.get("id", "")
        line = f"⚡ {name} {args}".rstrip()
        lines.append(f"{line}  ({call_id})" if call_id else line)
    block = "\n".join(lines)
    return f"{text.rstrip()}\n\n{block}" if text.strip() else block
