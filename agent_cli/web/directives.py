"""Directive 스코프 에디터 도메인 로직 — ✨ 생성 (5.7.0 산문 직접 호출).

구 3축(성격/업무/지침) zone 외과수술·프리셋 라이브러리는 폐지됐다 —
에디터의 구조 = 파일의 구조(U-C 청중 스코프: 공통/``## @main``/``## @agents``)
하나뿐이고, 분해/조립은 :mod:`agent_cli.prompts.system_prompt` 의
``split_directive_scopes``/``join_directive_scopes`` 가 단일 출처다.

✨ 생성의 경로 변천 (재론 방지용 기록):
- 산문 메타-콜(구 🪄) → **구세대 Qwen3 의 CoT 누출 실측**으로 폐기.
- 5.4.0: in-process run 엔진 (wire format 이 CoT 격리) — 메인 타임라인에
  카드 표면화.
- 5.6.0: 별도 ``agent-cli run`` 서브프로세스 (완전 격리·동시 생성).
- **5.7.0: 산문 직접 호출 복귀** — Qwen3.6 세대(27B·35B-A3B) 재실측
  16/16 무누출 확인 후 사용자 결정. 래퍼/서브프로세스/역추출 전부 제거,
  ``provider.call`` 1회 + 구조적 새니타이저(<think> 블록·코드 펜스
  스트립 — 미래 모델 교체로 누출이 재발해도 조용히 오염되지 않게).

동시 생성은 전송 계층의 executor 오프로드 + 백엔드 병렬로 성립 (POST
마다 독립 호출 — 메인 워커/렌더러/세션 무접촉은 그대로).
FastAPI import 0 (전송은 server.py) — 테스트 가드 유지.
"""

from __future__ import annotations

import re

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

# 산문 메타-콜 시스템 프롬프트 — 실측 프로브(16/16 클린)와 동일 골격.
_WRITER_SYSTEM = (
    "You turn a rough intent into a DIRECTIVE.md section — persistent "
    "operating rules injected into an LLM system prompt.\n"
    "Output ONLY the directive body as short markdown bullets (optionally "
    "under `##` subheadings). No preamble, no explanation, no code fences, "
    "no reasoning text. NEVER emit scope markers (`## @main`, `## @agents`). "
    "Imperative, specific, testable. Only rules that generalize beyond a "
    "single task. Prefer 3-8 bullets. Write in the language of the user's "
    "intent.\n"
    "When EXISTING directive content is given, produce the UPDATED full "
    "body: keep rules that still apply, merge duplicates, integrate the new "
    "intent — your output replaces it."
)

_THINK_BLOCK = re.compile(r"<think[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)


def build_generation_task(audience: str, brief: str, current: str) -> str:
    """✨ 생성 메타-콜의 user 메시지."""
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


def sanitize_generated(text: str) -> str:
    """구조적 새니타이저 — 내용 필터가 아니라 **포장 제거만**.

    <think> 블록(구세대류 CoT 누출 재발 대비)과 감싼 코드 펜스를 벗긴다.
    문구 기반 필터는 하지 않는다 — 정당한 규칙 본문("사용자가 '예'를…")
    을 오탐한 실측이 있어 위험 (프로브 검출기의 교훈).
    """
    t = _THINK_BLOCK.sub("", text).strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1 :]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def generate_directive_section(
    audience: str, brief: str, current: str, *, runtime: dict
) -> str:
    """brief → directive 초안 (활성 스코프용, 미저장 반환).

    ``provider.call`` 1회 (블로킹 — 전송 계층이 executor 오프로드).
    ``runtime`` = {provider(객체), model, capabilities}. 입력 오류는
    ValueError(→400), 호출/결과 실패는 RuntimeError(→502).
    """
    if audience not in VALID_AUDIENCES:
        raise ValueError(f"unknown audience: {audience}")
    if not brief.strip():
        raise ValueError("brief 가 비어 있습니다")
    if not runtime or runtime.get("provider") is None:
        raise ValueError("LLM 이 배선되지 않았습니다")

    task = build_generation_task(audience, brief, current)
    try:
        resp = runtime["provider"].call(
            messages=[{"role": "user", "content": task}],
            system=_WRITER_SYSTEM,
            model=runtime.get("model", ""),
            capabilities=runtime.get("capabilities"),
        )
    except Exception as e:  # provider/network — 전송 계층이 502 로
        raise RuntimeError(f"생성 실패: {e}") from e
    body = sanitize_generated(resp.content or "")
    if not body:
        raise RuntimeError("생성 결과가 비어 있습니다")
    return body
