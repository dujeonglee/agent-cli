"""on-disk record shape 의 계약 — 단일 리더/분류기 (C5, v4.47.0).

assistant 레코드의 두 저장 shape(멀티-op ``ops`` / 단수 legacy)과
관찰/query 레코드를 "어떻게 읽고 분류하나"의 유일한 소유자.
manager(enrich)·delegate report(활동로그)·review(tool-calls)·
tools/read_context 가 소비 — 예전 tools/context.py 의 private 침범이
계약 모듈의 공개 소비로 정당화된다.
"""

from __future__ import annotations

import json


# ── Defaults / constants ─────────────────────────────────
DEFAULT_TOKEN_BUDGET = 100_000

_OBSERVATION_PREFIX = "Observation: "
_OP_SUMMARY_CAP = 200


def _op_summary(action: str, action_input) -> str:
    """Flatten one op to a compact ``action {args}`` search string (capped)."""
    try:
        args = json.dumps(action_input, ensure_ascii=False)
    except (TypeError, ValueError):
        args = str(action_input)
    s = f"{action} {args}".strip()
    return s[:_OP_SUMMARY_CAP]


def iter_record_ops(record: dict) -> list[tuple[str, object]]:
    """``(action, action_input)`` pairs from ONE assistant history record.

    The single reader for both on-disk assistant shapes — the multi-op
    ``{"ops": [{"action", "action_input"}, ...]}`` record (md_array / react
    ``serialize_assistant_for_history`` / ``serialize_terminal_for_history``)
    and the base singular ``{"action", "action_input"}`` record (legacy
    sessions + non-multi-op formats). Bare-content records (prose drift, no
    action) and non-assistant roles yield ``[]``. Ops without an action name
    (actionless infer stubs) are skipped.

    Public: consumed by delegate's activity-log extractors and the loop's
    review tool-calls builder, so the record-shape knowledge stays in this
    module (next to :func:`_classify_record`) instead of each consumer
    re-guessing the shape — that re-guessing is exactly how the delegate
    extractors silently broke when the shape moved from JSON-in-``content``
    to structured fields (423608e).
    """
    if record.get("role") != "assistant":
        return []
    ops = record.get("ops")
    if isinstance(ops, list):
        return [
            (o["action"], o.get("action_input") or {})
            for o in ops
            if isinstance(o, dict) and o.get("action")
        ]
    action = record.get("action")
    if action:
        return [(action, record.get("action_input") or {})]
    return []


def _classify_record(message: dict) -> tuple[str, list[str], str]:
    """Derive ``(kind, tools, text)`` retrieval fields from a record's shape.

    ``kind``  — query | observation | action | final | raw | system | <role>
    ``tools`` — tool names involved (list; empty for query/final/raw)
    ``text``  — flat searchable surface (prefix-stripped query/observation,
                thought+op summaries for actions, the result for a final)

    Pure function of the record shape — no prefix-convention guessing leaks
    into read_context; this is the single place that encodes it.
    """
    role = message.get("role")
    content = str(message.get("content") or "")

    if role == "system":
        return "system", [], content

    if role == "user":
        if "tool" in message:  # tool observation
            text = (
                content[len(_OBSERVATION_PREFIX) :]
                if content.startswith(_OBSERVATION_PREFIX)
                else content
            )
            return "observation", [str(message.get("tool") or "")], text
        # human query — strip the "[author]: " label for the search surface
        author = message.get("author")
        text = content
        if author and content.startswith(f"[{author}]: "):
            text = content[len(f"[{author}]: ") :]
        return "query", [], text

    if role == "assistant":
        ops = message.get("ops")
        if isinstance(ops, list) and ops:
            actions = [
                o.get("action") for o in ops if isinstance(o, dict) and o.get("action")
            ]
            is_final = "complete" in actions
            thought = str(message.get("thought") or "")
            parts: list[str] = [thought] if thought else []
            for o in ops:
                if not isinstance(o, dict):
                    continue
                action = o.get("action") or ""
                ai = o.get("action_input")
                if action == "complete":
                    result = ai.get("result") if isinstance(ai, dict) else ai
                    parts.append(str(result or ""))
                else:
                    parts.append(_op_summary(action, ai))
            text = " | ".join(p for p in parts if p)
            return ("final" if is_final else "action"), actions, text
        # raw assistant content (e.g. NO_JSON fallback stored verbatim)
        return "raw", [], content

    return str(role or "?"), [], content
