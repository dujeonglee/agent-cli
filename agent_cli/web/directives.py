"""Directive 스코프 에디터 도메인 로직 — ✨ 생성 (5.4.0 전면 개편).

구 3축(성격/업무/지침) zone 외과수술·프리셋 라이브러리는 폐지됐다 —
에디터의 구조 = 파일의 구조(U-C 청중 스코프: 공통/``## @main``/``## @agents``)
하나뿐이고, 분해/조립은 :mod:`agent_cli.prompts.system_prompt` 의
``split_directive_scopes``/``join_directive_scopes`` 가 단일 출처다.

이 모듈이 소유하는 것은 ✨ 생성 하나: 사용자의 대략적 의도(brief)를
받아 **서브에이전트 루프로** directive 초안을 쓴다. 구 🪄 자동생성이
죽었던 원인(독립 산문 메타-콜의 CoT 누출 — omlx/Qwen 실측, JSON wire
경로는 안전)을 우회하는 형태: ``provider.call`` 직행 대신 run 엔진
(``tool_delegate``, 도구 0 = complete 만)으로 돌려 wire format 이 CoT 를
격리하고, complete 결과가 곧 초안이 된다. 진행은 일반 run 카드로 표면화.

FastAPI import 0 (전송은 server.py) — 테스트 가드 유지.
"""

from __future__ import annotations

VALID_AUDIENCES = ("common", "main", "agents")

# 청중별 프레이밍 — 생성 태스크에 삽입되는 요약. DIRECTIVE 스코프 의미론
# (U-C, docs/agent-unification/DESIGN.md §3.7)과 일치해야 한다.
_AUDIENCE_FRAMING = {
    "common": (
        "AUDIENCE: every LLM in the session — the main conversation loop AND "
        "all subagents. Write rules that hold everywhere (coding conventions, "
        "verification discipline, project constraints)."
    ),
    "main": (
        "AUDIENCE: the MAIN conversation LLM only (subagents never see this). "
        "Good fits: user-facing reporting style/voice/language, when to ask "
        "vs. proceed, how to summarize results for the user."
    ),
    "agents": (
        "AUDIENCE: subagents only (one-shot runs and persistent agents — the "
        "main LLM never sees this). Good fits: result format returned to the "
        "caller, scope discipline, citation/verification requirements."
    ),
}

_WRITER_INSTRUCTIONS = """You are a directive writer for agent-cli. A DIRECTIVE.md is a set of
persistent operating rules injected into the system prompt every turn —
it is read by an LLM, so every line must be an actionable instruction.

Rules for what you write:
- Output ONLY the directive body: short markdown bullets (optionally under
  `##` subheadings). No preamble, no explanation, no code fences.
- NEVER emit scope markers (`## @main`, `## @agents`) — the caller places
  your text into the right scope.
- Imperative, specific, testable ("답변은 한국어로", not "be helpful").
- Only rules that generalize beyond a single task; drop anything tied to
  one file/error/date.
- Keep it tight: prefer 3-8 bullets over prose. Merge overlapping rules.
- Write in the language the user's brief is written in.

When the task includes EXISTING directive content, produce the UPDATED
full body: keep rules that still apply, merge duplicates, integrate the
new intent — the result REPLACES the existing text.

Finish with a `complete` op whose result is exactly the directive body."""


def build_generation_task(audience: str, brief: str, current: str) -> str:
    """✨ 생성 서브에이전트에 넘길 task 텍스트."""
    parts = [
        _AUDIENCE_FRAMING[audience],
        f"USER INTENT (rough — turn this into directive rules):\n{brief.strip()}",
    ]
    current = (current or "").strip()
    if current:
        parts.append(
            "EXISTING directive content for this audience (revise/merge — "
            f"your output replaces it):\n{current}"
        )
    return "\n\n".join(parts)


def generate_directive_section(
    audience: str, brief: str, current: str, *, runtime: dict
) -> str:
    """brief → directive 초안 (활성 스코프용, 미저장 반환).

    run 엔진 1회: 도구 0(complete 만)·context none·짧은 턴 제한.
    ``runtime`` 은 web 부트스트랩이 채운 LLM 배선
    (provider/model/capabilities/provider_name/base_url/api_key/session).
    실패는 ValueError 로 — 전송 계층이 상태코드로 변환.
    """
    from agent_cli.subagent.oneshot import tool_delegate
    from agent_cli.subagent.report import extract_result_body

    if audience not in VALID_AUDIENCES:
        raise ValueError(f"unknown audience: {audience}")
    if not brief.strip():
        raise ValueError("brief 가 비어 있습니다")
    if not runtime or runtime.get("provider") is None:
        raise ValueError("LLM 이 배선되지 않았습니다")

    result = tool_delegate(
        {
            "tasks": [
                {
                    "task": build_generation_task(audience, brief, current),
                    "context": "none",
                    "tools": [],
                    "instructions": _WRITER_INSTRUCTIONS,
                }
            ]
        },
        parent_ctx=runtime.get("ctx"),
        provider=runtime["provider"],
        model=runtime.get("model", ""),
        capabilities=runtime.get("capabilities"),
        provider_name=runtime.get("provider_name", ""),
        base_url=runtime.get("base_url", ""),
        api_key=runtime.get("api_key", ""),
        max_turns=4,
        timeout=runtime.get("timeout", 120),
        session=runtime.get("session"),
    )
    if not result.success:
        raise ValueError(f"생성 실패: {result.error or '(no detail)'}")
    body = extract_result_body(result.output or "")
    if not body:
        raise ValueError("생성 결과가 비어 있습니다")
    return body
