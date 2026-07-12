"""DIRECTIVE.md 섹션/zone 조작 + 세션 학습 증류 — directive 도메인 로직.

C3: web 전송 계층(server.py)에서 분리. 섹션 헤딩·3축 zone(_AXIS_*)·학습
프롬프트/파서/기록 전부 순수 로직 — FastAPI 무의존(단독 테스트 가능).
"""

from __future__ import annotations


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
    if prepend:
        return f"{block}\n\n{rest}"
    return _append_before_scope_markers(rest, block)


def _append_before_scope_markers(rest: str, block: str) -> str:
    """Append ``block`` at the end of the COMMON zone — before the first
    ``## @main``/``## @agents`` scope marker if one exists, else at EOF.

    U-C(5.1.0) 상호작용: learned 지침을 파일 끝에 그대로 붙이면 마지막
    스코프 블록 안으로 빨려 들어가 서브(or main) 전용이 돼 버린다 — 세션
    교훈은 항상 common 이어야 하므로 마커 앞에 삽입한다."""
    from agent_cli.prompts.system_prompt import DIRECTIVE_SCOPE_MARKER

    lines = rest.splitlines()
    for i, ln in enumerate(lines):
        if DIRECTIVE_SCOPE_MARKER.fullmatch(ln.strip()):
            head = "\n".join(lines[:i]).rstrip()
            tail = "\n".join(lines[i:])
            if head:
                return f"{head}\n\n{block}\n\n{tail}"
            return f"{block}\n\n{tail}"
    return f"{rest}\n\n{block}"


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
