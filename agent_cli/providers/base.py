"""LLM provider protocol and response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_cli.constants import DEFAULT_STREAM_IDLE_TIMEOUT_S, STREAM_MAX_ATTEMPTS
from agent_cli.providers.capabilities import ModelCapabilities

# content 인라인 thinking 블록 격리 (5.10.0) — 구현은 thinking_tags 단일
# 소스로 이주 (dialects Phase 2 선행 리팩토링). openai.py 와
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
    #         / "runaway"(v10.10.0 — 내용 무관 폭주 감지, 사유는 stop_detail)
    # 루프의 출력-절단 가드가 "length" 를 비교하므로, 원어를 그대로 흘리면
    # 가드가 그 프로바이더에서 무발화한다(실사고 — anthropic._STOP_REASON_MAP).
    stop_reason: str | None = None
    #: "runaway" 의 감지 규칙 이름 (providers/runaway.REASON_TEXT 의 키); 그 외 "".
    stop_detail: str = ""
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
class CallSettings:
    """루프 → provider → 스트림으로 **여행하는** 세션-런타임 노브의 단일
    컨테이너 (v8.55.0). ctx 가 세션 상태로 들고, llm.py 가 매 콜 스냅샷을
    만들어 ``provider.call(settings=...)`` 로 전달한다. 새 노브 = 여기 필드
    추가(+기본값) + 소비 지점 한 곳 — provider 시그니처는 불변.

    담는 기준: provider 경계를 실제로 건너는 것만. ctx-로컬 노브
    (compaction_ratio 등)는 제외 — 담으면 진실 원천이 둘이 된다."""

    #: 세션 사고 스위치 (v10.72.0: on/off 둘뿐, 기본 끔 — ``ctx.thinking`` 스냅샷).
    #: 프로바이더는 ``capabilities.supports_thinking`` 일 때만 자기 방언의 스위치로
    #: 싣는다(OpenAI ``chat_template_kwargs.enable_thinking`` · Anthropic ``thinking``
    #: 블록). 정도(effort·budget)는 보내지 않는다 — 모델마다 키·어휘·접는 방향이
    #: 달라 하니스가 번역하면 조용히 틀린다(GLM 템플릿은 ``medium`` 을 ``Max`` 로
    #: 읽었다). 정도는 서버·모델 기본값의 몫.
    thinking: bool = False
    #: 스트림 무진전(no-token) 한도(초). 0 = 감지 끔.
    stream_idle_timeout_s: int = DEFAULT_STREAM_IDLE_TIMEOUT_S
    #: 무진전 시 **총** 시도 횟수(첫 전송 포함). 한도와 한 쌍 —
    #: 최대 대기 = stream_idle_timeout_s × stream_max_attempts (v8.60.0).
    stream_max_attempts: int = STREAM_MAX_ATTEMPTS
    #: 요청-시 클램프된 max_tokens. None = capabilities 등록값 사용 —
    #: capabilities 는 순수 모델 서술로 남는다 (v8.53.0 의 replace 대체).
    max_output_tokens: int | None = None
    #: 이 턴의 디코딩 문법 (EBNF, v9.24.0) — 서버가 지원하고 켜져 있을 때만
    #: 루프가 채운다(``DialectBase.grammar``). None = 제약 없음. OpenAI 방언은
    #: ``guided_grammar`` 로 싣고, Anthropic 은 받을 곳이 없어 무시한다.
    grammar: str | None = None
    #: v10.2.0 — 서버 네이티브 함수 호출(native_fc): OpenAI 함수 스키마 목록.
    #: None = 보내지 않음. Anthropic 경로는 받지 않는다(부트에서 fail-fast).
    tools: list[dict] | None = None
    #: v10.69.0 — 세션 샘플링 노브 ``{temperature?, top_p?}`` (ctx.sampling 스냅샷).
    #: None/빈 dict = 세션이 정한 값 없음 → 모델 엔트리(capabilities) → 보내지 않음.
    #: 해석은 :func:`resolve_sampling` 한 곳.
    sampling: dict | None = None


#: 샘플링 노브 이름 — 요청 본문 키와 같다 (OpenAI·Anthropic 둘 다 이 이름).
SAMPLING_KEYS: tuple[str, ...] = ("temperature", "top_p")


def resolve_sampling(capabilities: ModelCapabilities, sampling: dict | None) -> dict:
    """요청에 실을 샘플링 필드 — **세션 노브 > models.json 엔트리 > 없음** (v10.69.0,
    docs/knobs/DESIGN.md §7). 정해진 키만 돌려주므로 어느 층도 안 정한 키는 요청에
    없고 서버 기본이 적용된다(종전과 같은 본문). 프로바이더 둘이 같은 이름으로
    싣는다 — 번역은 없다."""
    ov = sampling or {}
    out: dict = {}
    for name in SAMPLING_KEYS:
        value = ov.get(name)
        if value is None:
            value = getattr(capabilities, name, None)
        if value is not None:
            out[name] = float(value)
    return out


def sampling_source(
    capabilities: ModelCapabilities, sampling: dict | None, name: str
) -> str:
    """``name`` 의 실효값이 어느 층에서 왔나 — ``"session"`` · ``"models.json"`` ·
    ``"server"``(아무 층도 안 정해 필드를 보내지 않는다). 🎲 칩의 설명 줄용."""
    if (sampling or {}).get(name) is not None:
        return "session"
    if getattr(capabilities, name, None) is not None:
        return "models.json"
    return "server"


def thinking_switch(capabilities: ModelCapabilities, thinking: bool) -> bool | None:
    """이 요청에 실을 사고 스위치 — ``None`` 이면 **아무것도 싣지 않는다**
    (``supports_thinking=False``: 사고 관련 필드를 모르는 서버에 보내 400 나는 것을
    막는 v8.21.1 게이트). 지원 모델이면 세션 값 그대로 — 끔도 명시한다(Qwen 계열
    템플릿은 스위치가 없으면 사고를 켠다, v9.24.8)."""
    if not capabilities.supports_thinking:
        return None
    return bool(thinking)
