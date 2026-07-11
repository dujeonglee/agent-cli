"""DIRECTIVE.md 섹션/zone 조작 + 세션 학습 증류 — directive 도메인 로직.

C3: web 전송 계층(server.py)에서 분리. 섹션 헤딩·3축 zone(_AXIS_*)·학습
프롬프트/파서/기록 전부 순수 로직 — FastAPI 무의존(단독 테스트 가능).
"""

from __future__ import annotations

import json


_PERSONA_HEADING = "## 페르소나"
# Template BODIES per axis, matching what ``_zone_set`` expects: the persona zone
# gets its ``## 페르소나`` heading prepended by ``_zone_set`` (so the body is
# heading-less bullets); the task zone is placed verbatim (so it carries its own
# ``## 업무`` heading). learned has no template — it is filled by 📥 learn.
_AXIS_TEMPLATES = {
    "persona": (
        "- 말투·톤:\n"
        "- 적용 범위: 사용자 대면 답변(최종 결과·요약·질문)만 이 목소리로. 추론·도구 "
        "호출·파일 경로·명령어·코드·사실은 캐릭터와 무관하게 정확하게(왜곡·누락 금지)."
    ),
    "task": (
        "## 업무\n"
        "- 역할:\n"
        "- 작업 원칙:\n"
        "- 착수 전 확인:\n"
        "- 검증·품질 규율:\n"
        "- 메모리 활용: 중요한 실패·발견·결정은 즉시 memory 도구(mode=add, "
        "type=failure|discovery|decision)로 기록 — 컨텍스트 압축 후에도 잃지 않도록.\n"
        "- 주의사항:"
    ),
}


# Managed DIRECTIVE sections — code-owned blocks (persona voice, learned
# guidance) that generation SWAPS in/out deterministically while leaving the
# user's hand-written directive byte-identical. Each is a ``## <heading>`` run
# from its heading through just before the next top-level ``## `` (or EOF).
_LEARNED_HEADING = "## 학습된 지침"


def _strip_section(md: str, heading: str) -> str:
    """Remove the ``heading`` section (heading line through just before the next
    top-level ``## `` heading, or EOF) from ``md``; return the rest, stripped.
    A no-op (other than trimming) when the section is absent."""
    lines = md.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip().startswith(heading):
            i += 1  # skip the heading + its body up to the next `## `
            while i < n and not lines[i].startswith("## "):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out).strip()


def _replace_managed_section(
    md: str, heading: str, body: str, *, prepend: bool = False
) -> str:
    """Replace (or remove, when ``body`` is empty) the ``heading`` section in
    ``md``, leaving all other content byte-identical. ``body`` is the section
    body (heading added here). ``prepend`` places the block before the remaining
    content (persona voice leads); default appends it after (learned guidance
    trails the hand-written directive)."""
    rest = _strip_section(md, heading)
    body = (body or "").strip()
    if not body:
        return rest
    block = f"{heading}\n{body}"
    if not rest:
        return block
    return f"{block}\n\n{rest}" if prepend else f"{rest}\n\n{block}"


def _strip_code_fences(text: str) -> str:
    """Drop a leading ```lang / trailing ``` fence the model may wrap the
    directive in despite instructions, so the editor gets raw Markdown."""
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1 :]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


# ── Directives 📥 learn-from-session (POST /api/directives/learn) ──
# Distill REUSABLE lessons from the live conversation into the managed
# ``## 학습된 지침`` section — the safe alternative to whole-cloth task
# auto-generation (which hallucinated boilerplate). A dedicated one-off call
# whose ONLY job is extraction is reliable, unlike the loop model which
# empirically never self-records via the memory tool (docs/directive-learning
# DESIGN §1.1). The system — not the model — then writes both the memory store
# and the DIRECTIVE section (deterministic; §4).
_LEARN_SYSTEM = (
    "You extract REUSABLE operating lessons from a work session so the same kind "
    "of task goes better next time. The session may be any kind of work — "
    "coding, log/data analysis, research, ops, writing, and so on — so do not "
    "assume a domain. Read the conversation and keep ONLY transferable guidance "
    "— general working rules, gotchas, and decisions that would help on a "
    "DIFFERENT instance of a similar task. EXCLUDE session-specific facts (a "
    "particular error message, a specific file/line/record, a one-off value or "
    "name). If an existing '## 학습된 지침' list is given, MERGE with it: "
    "deduplicate and consolidate, regenerating the FULL set (accumulate but do "
    "not bloat). Write lessons in the session's language (Korean if the session "
    "is Korean). Output ONLY a JSON array — no prose, no code fences: "
    '[{"type": "failure|discovery|decision|note", "summary": "one actionable '
    'line", "detail": "optional context"}]. Output [] when there is nothing '
    "durable to learn."
)

# Cap the conversation fed to distillation (keep the most recent) so a long
# session can't blow the meta-call's context. Truncation is announced in the
# input, never silent (the no-silent-caps rule).
_LEARN_MAX_MESSAGES = 40


def _section_body(md: str, heading: str) -> str:
    """Return just the body of the ``heading`` section (its lines up to the next
    top-level ``## `` or EOF), or ``""`` if absent — the inverse of
    ``_strip_section``, used to feed the existing learned list back for merge."""
    lines = md.splitlines()
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip().startswith(heading):
            i += 1
            body: list[str] = []
            while i < n and not lines[i].startswith("## "):
                body.append(lines[i])
                i += 1
            return "\n".join(body).strip()
        i += 1
    return ""


def _render_learning_input(messages: list, existing_learned: str) -> str:
    """Build the distillation user prompt: the conversation (capped to the most
    recent ``_LEARN_MAX_MESSAGES``, with any elision announced) plus any existing
    learned section to merge against."""
    total = len(messages)
    recent = messages[-_LEARN_MAX_MESSAGES:]
    parts: list[str] = []
    if total > len(recent):
        parts.append(
            f"(앞부분 {total - len(recent)}개 메시지 생략, 최근 {len(recent)}개만 표시)"
        )
    for m in recent:
        role = m.get("role", "?")
        content = m.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        parts.append(f"[{role}] {content}")
    convo = "\n".join(parts)
    if existing_learned:
        return (
            f"기존 '## 학습된 지침' (중복 제거·통합 대상):\n{existing_learned}\n\n"
            f"=== 세션 대화 ===\n{convo}"
        )
    return f"=== 세션 대화 ===\n{convo}"


def _parse_lessons(raw: str) -> list[dict]:
    """Parse the distillation output into validated ``{type, summary, detail}``
    lessons. Tolerant: strips fences, takes the first JSON array, coerces an
    unknown type to ``note``, drops empty-summary entries. Returns ``[]`` on
    unrecoverable output so a bad meta-call degrades to "nothing learned" rather
    than 500ing (distillation quality is tuned on real 27B in a later phase)."""
    from agent_cli import memory as _mem

    text = _strip_code_fences(raw or "")
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type", "note")).strip().lower()
        if typ not in _mem.VALID_TYPES:
            typ = "note"
        summary = str(item.get("summary", "")).strip()
        if not summary:
            continue
        out.append(
            {
                "type": typ,
                "summary": summary,
                "detail": str(item.get("detail", "")).strip(),
            }
        )
    return out


def _render_learned_block(lessons: list[dict]) -> str:
    """Render the ``## 학습된 지침`` body — one concise bullet per lesson summary
    (full detail lives in the memory store, per DESIGN §8.2)."""
    return "\n".join(f"- {les['summary']}" for les in lessons)


def _record_lessons(session_dir, lessons: list[dict]) -> int:
    """Persist lessons to the session memory store (deterministic — the system
    writes, not the model). Skips a lesson already stored (same type+summary) so
    repeated 📥 presses don't spam the store. Returns the count newly recorded."""
    from agent_cli import memory as _mem

    if not session_dir:
        return 0
    seen = {(e["type"], e["summary"]) for e in _mem.load(session_dir)}
    n = 0
    for les in lessons:
        key = (les["type"], les["summary"])
        if key in seen:
            continue
        _mem.add(
            session_dir,
            type=les["type"],
            summary=les["summary"],
            detail=les["detail"] or None,
        )
        seen.add(key)
        n += 1
    return n


# ── Directive axis zones ──────────────────────────────────────────────
# A directive has three axes the editor edits independently: persona (the
# ``## 페르소나`` section), learned guidance (``## 학습된 지침`` section), and
# task — everything else (the free-form body). Each 🪄/📥 generator and each
# per-axis preset save/load targets exactly ONE zone, leaving the other two
# byte-identical. All zone parsing lives here (Python) so the frontend never
# re-implements section splitting.
_AXIS_HEADINGS = {"persona": _PERSONA_HEADING, "learned": _LEARNED_HEADING}


def _task_zone(content: str) -> str:
    """The task zone = the directive minus the persona and learned sections
    (the free-form body the user writes / the 업무 🪄 generates)."""
    return _strip_section(_strip_section(content, _PERSONA_HEADING), _LEARNED_HEADING)


def _zone_get(content: str, axis: str) -> str:
    """Current body of one axis's zone within ``content`` (heading excluded for
    the section axes; the whole remainder for task)."""
    if axis == "task":
        return _task_zone(content)
    return _section_body(content, _AXIS_HEADINGS[axis])


def _zone_set(content: str, axis: str, body: str) -> str:
    """Replace one axis's zone in ``content`` with ``body`` (empty removes it),
    leaving the other two zones byte-identical. Persona leads (prepend), learned
    trails (append), task is the middle remainder."""
    if axis == "persona":
        return _replace_managed_section(content, _PERSONA_HEADING, body, prepend=True)
    if axis == "learned":
        return _replace_managed_section(content, _LEARNED_HEADING, body)
    # task: rebuild as persona(prepend) + new task body + learned(append), so
    # regenerating/loading the task never disturbs the two managed sections.
    persona = _section_body(content, _PERSONA_HEADING)
    learned = _section_body(content, _LEARNED_HEADING)
    out = _replace_managed_section(
        (body or "").strip(), _PERSONA_HEADING, persona, prepend=True
    )
    return _replace_managed_section(out, _LEARNED_HEADING, learned)


# Per-file cap for workspace uploads (POST /api/workspace/upload). A guard
# against an accidental huge upload filling the on-prem disk — generous enough
# for source trees / small assets, not for blobs.
