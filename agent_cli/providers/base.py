"""LLM provider protocol and response types."""

from __future__ import annotations

import re

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_cli.providers.capabilities import ModelCapabilities


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
    stop_reason: str | None = None
    # Reasoning content surfaced via a separate API field (e.g. Anthropic
    # thinking blocks, OpenAI reasoning). Empty string when the provider
    # doesn't expose it or the model didn't produce any.
    thinking: str = ""


_THINK_TAG_NAMES = ("think", "thinking", "reasoning", "reflection")
_THINK_BLOCK_RE = re.compile(
    r"<(" + "|".join(_THINK_TAG_NAMES) + r")\b[^>]*>(.*?)</\1\s*>",
    re.S | re.I,
)
_THINK_OPEN_RE = re.compile(
    r"<(" + "|".join(_THINK_TAG_NAMES) + r")\b[^>]*>", re.I
)


def strip_think_blocks(text: str) -> tuple[str, str]:
    """content 에 인라인으로 섞인 ``<think>`` 류 추론 블록 격리 (5.10.0).

    일부 모델(MiMo 등)이 reasoning 을 별도 API 필드가 아니라 content 안의
    태그로 흘린다 — 길면 wire 파싱을 깨고 컨텍스트를 태운다. 닫힌 블록은
    전부, **안 닫힌 열림 태그는 EOF 까지**(max_tokens 를 추론 중 소진한
    경우) 제거하고, 제거분은 버리지 않고 두 번째 반환값으로 돌려준다 —
    호출자(provider)가 ``LLMResponse.thinking`` 에 실어 verbose 에서
    보이게 한다. 태그 vocab 은 capabilities 탐지와 동일 4종.
    """
    if "<" not in text:
        return text, ""
    thinks: list[str] = []

    def _grab(m: re.Match) -> str:
        thinks.append(m.group(2).strip())
        return ""

    cleaned = _THINK_BLOCK_RE.sub(_grab, text)
    # 안 닫힌 열림 태그 — 그 지점부터 전부 추론으로 간주.
    m = _THINK_OPEN_RE.search(cleaned)
    if m:
        thinks.append(cleaned[m.end() :].strip())
        cleaned = cleaned[: m.start()]
    return cleaned.strip(), "\n\n".join(x for x in thinks if x)


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
