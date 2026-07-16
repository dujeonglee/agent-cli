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
    ``{"ops": [{"action", "action_input"}, ...]}`` record (json_fc / xml_fc
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


def is_format_intervention(record: dict) -> bool:
    """형식-복구 개입 관찰인가 — fold(v4.51.0) 계약 술어.

    1차 식별: additive ``recovery == "format"`` 마킹 (NO_THOUGHT/NO_JSON/
    NO_ACTION/A4/A5 개입이 기록 시 찍음 — B1[행동 루프] 개입과 실제 도구
    실행 실패는 마킹 없음 = fold 비대상).
    레거시 백스톱: 마킹 도입 전 세션의 파싱 개입은 ``tool == ""`` 규약
    (web replay 가 같은 규약 소비)으로 식별 — 단 A4/A5 는 구 세션에서
    도구명이 찍혀 있어 식별 불가(안전하게 fold 안 함).
    """
    if record.get("role") != "user" or "tool" not in record:
        return False
    if record.get("recovery") == "format":
        return True
    return record.get("tool") == "" and not record.get("success", True)


def fold_resolved_intervention_indices(records: list) -> list[int]:
    """fold 대상 인덱스(내림차순) — "개입 관찰 뒤에 파싱-성공 assistant
    레코드(ops 보유)가 존재"하면 그 개입과 **직전 실패 assistant** 를 접는다.

    순수 함수: live(성공 직후 호출 — 이때는 아직 성공 레코드가 캐시에 없어
    꼬리 개입도 접도록 caller 가 ``assume_tail_resolved`` 의미로 사용)와
    resume(레코드만 보고 재판정) 이 같은 규칙을 공유하기 위한 기반.
    여기서는 레코드-기반 판정만: 뒤에 ops-보유 assistant 가 있는 개입.
    """
    out: list[int] = []
    for i, rec in enumerate(records):
        if not is_format_intervention(rec):
            continue
        resolved = any(
            r.get("role") == "assistant" and iter_record_ops(r)
            for r in records[i + 1 :]
        )
        if not resolved:
            continue
        out.append(i)
        if i > 0 and records[i - 1].get("role") == "assistant":
            out.append(i - 1)  # 직전 실패 emission 도 함께
    return sorted(set(out), reverse=True)
