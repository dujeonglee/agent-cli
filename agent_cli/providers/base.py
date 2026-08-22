"""LLM provider protocol and response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_cli.providers.capabilities import ModelCapabilities

# content 인라인 thinking 블록 격리 (5.10.0) — 구현은 thinking_tags 단일
# 소스로 이주 (multi-wire-format Phase 2 선행 리팩토링). openai.py 와
# 기존 테스트가 이 경로에서 import 하므로 re-export 로 유지.
from agent_cli.thinking_tags import (
    strip_think_blocks as strip_think_blocks,  # noqa: PLC0414
)


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int
    # Durations in nanoseconds.
    # OpenAI/Anthropic: client-measured via streaming.
    prompt_eval_ns: int = 0  # prefill / time-to-first-token
    eval_ns: int = 0  # decode / first-to-last token
    ttft_ns: int = 0  # client-measured TTFT (all providers, streaming only)
    # Anthropic prompt cache. Non-zero only when cache_control is set on
    # request blocks. ``input_tokens`` excludes both cache fields, so the
    # billable input total is input_tokens + cache_creation + cache_read.
    cache_creation_input_tokens: int = 0  # tokens written to cache (25% premium)
    cache_read_input_tokens: int = 0  # tokens served from cache (10% cost)

    @property
    def total_input_tokens(self) -> int:
        """The true prompt size / context occupancy: non-cached ``input_tokens``
        + cache writes + cache reads. ``input_tokens`` alone EXCLUDES both cache
        fields (Anthropic prompt cache), so use this wherever you mean "how full
        is the context" (budget reconcile, ctx% readout). For providers without a
        prompt cache (omlx etc.) the cache fields are 0, so this == input_tokens."""
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict] | None = None
    usage: TokenUsage | None = None
    # 정규화 어휘 (P0-1) — 프로바이더가 자기 원어를 이 어휘로 매핑해 반환할 계약:
    #   "stop"   = 정상 종료   (OpenAI stop / Anthropic end_turn·stop_sequence)
    #   "length" = 출력 절단   (OpenAI length / Anthropic max_tokens)
    #   합성: "interrupted"(사용자 중단) / "degenerate_runaway"(조기 종료)
    # 루프의 출력-절단 가드가 "length" 를 비교하므로, 원어를 그대로 흘리면
    # 가드가 그 프로바이더에서 무발화한다(실사고 — anthropic._STOP_REASON_MAP).
    stop_reason: str | None = None
    # Reasoning content surfaced via a separate API field (e.g. Anthropic
    # thinking blocks, OpenAI reasoning). Empty string when the provider
    # doesn't expose it or the model didn't produce any.
    thinking: str = ""


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol that all provider adapters must satisfy."""

    def call(
        self,
        messages: list[dict],
        system: str,
        model: str,
        capabilities: ModelCapabilities,
        **kwargs,
    ) -> LLMResponse: ...


@dataclass(frozen=True)
class ThinkingPolicy:
    """``request_overrides`` 의 **공용 해석 결과** (T1 잔여 — 리뷰 §4.2).

    종전엔 OpenAI/Anthropic 이 각자 오버라이드를 해석해 조합별 의미가
    갈렸다(예: OpenAI 는 enable_thinking=False 여도 reasoning_effort 잔존,
    eff="off"+enable=True 가 프로바이더별 on/off 상이). 해석은 여기 한 곳,
    프로바이더는 이 결과를 자기 wire 방언(reasoning_effort+
    chat_template_kwargs / thinking 블록)으로 번역만 한다.

    - ``enabled``: 사고 on/off. off 판정은 ``enable_thinking is False`` 또는
      ``reasoning_effort == "off"`` — "off" 가 명시 enable=True 보다 이긴다
      (Anthropic 의 종전 의미를 공용 규칙으로 채택).
    - ``effort``: enabled 일 때의 노력 수준 — 오버라이드가 low/medium/high 면
      그 값, 없으면 "medium" (양 프로바이더 공통 기본).
    - ``enable_override``: 원본 enable_thinking 오버라이드 (None=미설정).
      OpenAI 방언이 chat_template_kwargs 스위치를 **명시 오버라이드가 있을
      때만** 방출하기 위한 원본 보존 — 값 자체는 ``enabled`` 를 쓴다.
    """

    enabled: bool
    effort: str
    enable_override: bool | None


def resolve_thinking_policy(
    capabilities: ModelCapabilities, overrides: dict | None
) -> ThinkingPolicy | None:
    """thinking 오버라이드 해석 — 프로바이더 공용 정책 함수.

    ``supports_thinking=False`` 면 **None**: 기본값도 오버라이드도 일절
    주입하지 않는다는 v8.21.1 게이트 그대로 (호출측은 None 이면 사고 관련
    필드를 아무것도 만들지 않는다)."""
    if not capabilities.supports_thinking:
        return None
    ov = overrides or {}
    eff = ov.get("reasoning_effort")
    enable = ov.get("enable_thinking")
    enabled = not (enable is False or eff == "off")
    effort = eff if eff in ("low", "medium", "high") else "medium"
    return ThinkingPolicy(enabled=enabled, effort=effort, enable_override=enable)
