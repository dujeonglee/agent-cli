"""Thinking-tag 스트리핑 단일 소스 (multi-wire-format Phase 2 선행 리팩토링).

일부 모델(MiMo·Qwen 계열)이 CoT 를 별도 API 필드가 아니라 content 안의
태그로 흘린다 — **모델-런타임 quirk 이지 wire-shape 의 속성이 아니다**.
그 vocab 과 strip 구현이 4곳(providers/base ①②, react ①②, json_fc ③
정규식 2개, capabilities vocab)에 중복돼 드리프트 위험이었던 것을 여기로
통합한다. 층은 그대로 둔다 — 경로마다 지키는 것이 다르기 때문:

  - providers/openai (①②): content 정규화 — 컴팩션 요약·✨ directive 등
    **비-파서 소비자** 보호 (5.10.0 의 존재 이유).
  - WireFormat.strip_thinking (①②, 파서 stage 0): provider 를 안 거치는
    경로(anthropic/http 의 content-태그 leak, bench 의 provider 우회,
    직접 파서 호출)의 유일 방어.
  - providers/capabilities: thinking 지원 탐지 프로브의 태그 vocab.
  - json_fc (③): 고아/트레일링 태그 — 발생 위치가 포맷의 수리
    파이프라인 내부라 **적용 지점은 포맷 소유**, 정규식만 여기서 공급.

leak shape 용어: ① 완전 블록 ``<think>…</think>`` / ② 미닫힘 opener
(max_tokens 를 추론 중 소진 — EOF 까지 추론으로 간주) / ③ 고아 태그
(opener 를 reasoning 채널이 소비해 closer 만 content 에 남는 경우 등).
①② 는 blind sub 라 문자열 값 안의 완전 블록도 먹는다 — openai 경로가
5.10.0 부터 이미 그랬으므로 (경로 간 일관성) 수용; ③ 은 앵커드 정규식이라
문자열 값 안의 고아 태그는 보존된다 (json_fc 계약 테스트로 고정).
"""

from __future__ import annotations

import re

# 태그 vocab — capabilities 탐지·strip 양쪽이 공유하는 4종 (case-insensitive).
THINK_TAG_NAMES: tuple[str, ...] = ("think", "thinking", "reasoning", "reflection")

_NAMES = "|".join(THINK_TAG_NAMES)

# ① 완전 블록 (속성 허용: ``<think budget=…>``).
THINK_BLOCK_RE = re.compile(r"<(" + _NAMES + r")\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
# ② 미닫힘 opener.
THINK_OPEN_RE = re.compile(r"<(" + _NAMES + r")\b[^>]*>", re.I)
# ③ 고아 태그 낱개 (여닫이 무관) — thought 산문 청소용 (json_fc sanitize).
ORPHAN_THINK_TAG_RE = re.compile(r"</?\s*(?:" + _NAMES + r")\s*>", re.IGNORECASE)
# ③ 트레일링 고아 태그 무리 — 본문 끝에만 앵커 (문자열 값 안은 보존).
TRAILING_THINK_TAG_RE = re.compile(
    r"(?:\s*</?\s*(?:" + _NAMES + r")\s*>)+\s*$", re.IGNORECASE
)


# ② 의 미닫힘-truncation 은 **라인 선두 opener 만** — 산문 속 인라인 언급
# ("Discussing the <thinking> channel")이 뒤따르는 action 블록 전체를
# EOF 까지 삼키던 data-loss 의 수리 (v7.11.4). 실측 미닫힘 opener 는
# 항상 라인 선두였다.
THINK_OPEN_LINE_RE = re.compile(r"(?m)^[ \t]*<(" + _NAMES + r")\b[^>]*>", re.IGNORECASE)


def strip_think_blocks(
    text: str, *, stop: "re.Pattern | None" = None
) -> tuple[str, str]:
    """content 에 인라인으로 섞인 thinking 블록(①②) 격리.

    닫힌 블록은 전부, 안 닫힌 열림 태그(라인 선두만 — 산문 속 언급은
    opener 가 아니다)는 ``stop`` 매치 직전(없으면 EOF)까지 제거하고,
    제거분은 버리지 않고 두 번째 반환값으로 돌려준다 — provider 는
    ``LLMResponse.thinking`` 에, 파서는 ``ParsedTurn.thinking`` 에 실어
    verbose 에서 보이게 한다 (양쪽 다 **비재공급** 채널).

    ``stop`` (v7.11.4): 포맷의 첫-구조 마커 — 미닫힘 opener 뒤에 tool
    call 이 따라오면 EOF-삼킴 대신 구조 직전에서 멈춰 턴의 ops 를
    보존한다 (opener 를 진짜로 안 닫는 모델 + 이어지는 액션 실측).
    """
    if "<" not in text:
        return text, ""
    thinks: list[str] = []

    def _grab(m: re.Match) -> str:
        thinks.append(m.group(2).strip())
        return ""

    cleaned = THINK_BLOCK_RE.sub(_grab, text)
    # 안 닫힌 열림 태그(라인 선두) — stop/EOF 까지 추론으로 간주.
    m = THINK_OPEN_LINE_RE.search(cleaned)
    if m:
        end = len(cleaned)
        if stop is not None:
            sm = stop.search(cleaned, m.end())
            if sm is not None:
                end = sm.start()
        thinks.append(cleaned[m.end() : end].strip())
        cleaned = cleaned[: m.start()] + cleaned[end:]
    return cleaned.strip(), "\n\n".join(x for x in thinks if x)
