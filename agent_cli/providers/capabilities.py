"""Model capabilities: what each model supports."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import requests

from agent_cli.config import get_model_entry, save_model_entry
from agent_cli.constants import DETECTION_PROBE_TIMEOUT
from agent_cli.thinking_tags import THINK_TAG_NAMES as _THINKING_TAGS

# Optional progress callback — set by the caller (main.py) before
# runtime capability detection so the user sees what each probe step
# is doing. Cold-load + two probes can take 20-30s for a large model
# the first time; without progress the CLI appears frozen. Default
# None keeps capabilities.py callable in isolation (tests, scripts).
_progress_cb: Callable[[str], None] | None = None


def set_progress_callback(cb: Callable[[str], None] | None) -> None:
    """Register (or clear) a progress callback. Called once by main.py
    around the get_capabilities() invocation that might hit runtime
    detection. Pass None to disable."""
    global _progress_cb
    _progress_cb = cb


def _emit_progress(msg: str) -> None:
    """Emit a single progress message through the registered callback,
    if any. Safe no-op when no callback is set."""
    cb = _progress_cb
    if cb is not None:
        try:
            cb(msg)
        except Exception:
            # Never let a progress UI error derail capability detection.
            pass


@dataclass(frozen=True)
class ModelCapabilities:
    # v7.0.0: supports_structured_output / supports_strict_schema 필드 제거 —
    # 유일 소비자(react 의 json_mode)가 react 와 함께 제거돼 소비자 0.
    # 구 models.json 엔트리의 해당 키는 로더가 무시 (무해).
    context_window: int
    max_output_tokens: int
    supports_thinking: bool


# Fallback for a model we could neither look up (models.json) nor probe
# (runtime detection returned None). The window is OPTIMISTIC on purpose:
# the old 4096 made ``oversized_cap`` (= window/10) 409 tokens, so virtually
# every read_file / shell observation was dropped as over-cap — the agent was
# unusable on any model that failed detection, and 4096 also contradicted
# ``MIN_CONTEXT_WINDOW`` (detection hard-rejects anything under 16K). 256K
# errs the other way: on a model whose real window is smaller the first call
# is rejected by the server and ``ContextManager.force_fit`` (flow 2) shrinks
# and retries — a few wasted round trips instead of a crippled session.
# ``max_output_tokens`` stays conservative (a too-large request is rejected
# outright by many servers, with no reactive recovery path).
DEFAULT_CONTEXT_WINDOW = 262_144  # 256K
DEFAULT_CAPABILITIES = ModelCapabilities(
    context_window=DEFAULT_CONTEXT_WINDOW,
    max_output_tokens=2048,
    supports_thinking=False,
)

# Auto-detected output token budget = context_window // this. A model
# with a 256K window gets 64K output; 16K → 4K. (Registry/models.json
# entries keep their stored value — this only governs detection.)
_OUTPUT_TOKEN_DIVISOR = 4

# Models with a context window below this are rejected at detection
# time: even output (= window//4) would be under 4K, too small to run
# the agent loop usefully. Surfaced as UnsupportedModelError so the CLI
# can fail fast with a clear message instead of silently degrading.
MIN_CONTEXT_WINDOW = 16384


class UnsupportedModelError(RuntimeError):
    """Raised during capability detection when a model's context window
    is too small (< MIN_CONTEXT_WINDOW) to run the agent. Propagates out
    of get_capabilities; the CLI catches it and exits with a message."""


# Context-window detection fallback when neither /v1/models metadata nor
# the overflow probe yields a number. 128K is a realistic floor for
# modern local models — far less wasteful than the old 4096 default,
# while staying under-set (safe: flow 2 overflow recovery corrects at
# runtime if the real window is smaller). See docs/ARCHITECTURE.md.
_DEFAULT_CONTEXT_FALLBACK = 131072  # 128K

# Filler size for the overflow probe (in repetitions of "word "). At
# ~0.75 tokens/word this is ≈1.5M tokens — over the limit of essentially
# every local model, so the server rejects it right after tokenisation
# (no eval/generation, no server occupancy). Models whose real window
# exceeds ~1.5M simply return 200 and the caller falls back.
_CONTEXT_PROBE_WORDS = 2_000_000


# Tracks whether the last get_capabilities() call triggered runtime detection
_last_was_runtime_detected: bool = False


def was_runtime_detected() -> bool:
    """Return True if the last get_capabilities() call used runtime detection."""
    return _last_was_runtime_detected


def get_capabilities(
    model: str,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str = "",
) -> ModelCapabilities:
    """Look up capabilities with priority: models.json > runtime detection > defaults."""
    global _last_was_runtime_detected
    _last_was_runtime_detected = False

    # Priority 1: Static registry (models.json)
    entry = get_model_entry(model)
    if entry is not None:
        return _build_from_entry(entry)

    # Priority 2: Runtime detection
    if provider and base_url:
        detected = _detect_runtime_capabilities(provider, base_url, model, api_key)
        if detected is not None:
            _auto_save_detected(model, detected)
            _last_was_runtime_detected = True
            return detected

    # Priority 3: Conservative defaults
    return DEFAULT_CAPABILITIES


def caps_to_entry(caps: ModelCapabilities, *, auto_detected: bool = False) -> dict:
    """Serialize a ``ModelCapabilities`` to a models.json entry dict — the
    inverse of :func:`_build_from_entry`. Single source so the field list can't
    drift between the runtime-probe save path and the setup-wizard save path
    (``main._prompt_model_capabilities``). ``auto_detected`` tags entries
    written by the runtime probe."""
    entry = {
        "context_window": caps.context_window,
        "max_output_tokens": caps.max_output_tokens,
        "supports_thinking": caps.supports_thinking,
    }
    if auto_detected:
        entry["_auto_detected"] = True
    return entry


def _auto_save_detected(model: str, caps: ModelCapabilities) -> None:
    """Save runtime-detected capabilities to ~/.agent-cli/models.json (new models only)."""
    save_model_entry(model, caps_to_entry(caps, auto_detected=True))


def _build_from_entry(entry: dict) -> ModelCapabilities:
    # Legacy field `supports_tool_calling` — silently ignored if present in
    # older models.json entries; the loop uses ReAct text parsing, not the
    # native tool-calling API on any provider.
    return ModelCapabilities(
        context_window=entry.get("context_window", DEFAULT_CONTEXT_WINDOW),
        max_output_tokens=entry.get("max_output_tokens", 2048),
        supports_thinking=entry.get("supports_thinking", False),
    )


# Compiled patterns for efficiency. 태그 vocab 은 thinking_tags 단일 소스
# (모듈 상단 import — strip 경로와 탐지 경로가 같은 4종을 봐야 한다).
_THINKING_TAG_PATTERN = re.compile(
    r"<(" + "|".join(_THINKING_TAGS) + r")>",
    re.IGNORECASE,
)


def _detect_runtime_capabilities(
    provider: str, base_url: str, model: str, api_key: str = ""
) -> ModelCapabilities | None:
    """Detect model capabilities at runtime via provider API.

    All providers share the probe orchestration (`_detect_capabilities`);
    transport 는 **프로바이더 클래스의 ``capability_transport`` 훅** 소유
    (v8.41.0 self-register — 종전 이 함수의 provider-이름 분기 흡수).
    미등록 프로바이더/훅 부재는 None → 기본값 폴백 (종전 계약)."""
    from agent_cli.providers import get_provider_class

    cls = get_provider_class(provider)
    transport_factory = getattr(cls, "capability_transport", None)
    if transport_factory is None:
        return None
    return _detect_capabilities(model, transport_factory(base_url, model, api_key))


# Prompt whose natural answer is prose, not JSON. If the server does not
# enforce ``response_format`` it answers with markdown, which fails
# ``json.loads`` — so a passing probe means the constraint was genuinely
# honored, not that the model happened to emit JSON on its own.


def _detect_thinking(content: str) -> bool:
    """``<think>``-style tag detection on a probe response's content."""
    return bool(_THINKING_TAG_PATTERN.search(content or ""))


def _detect_capabilities(model: str, transport) -> ModelCapabilities | None:
    """Provider-agnostic capability-probe orchestration.

    The PROVIDER-SPECIFIC ``transport`` supplies three calls — the only part
    that differs between OpenAI and Anthropic (endpoint / headers / request +
    response shape):
      - ``context_window()`` — 3-tier (metadata → overflow probe → fallback).
      - ``thinking_probe()`` → bool. **공통 계약: 2단계** — ① 기본 프로브(모델이
        기본으로 사고를 뱉으면 감지) → ② 미검출 시 방언별 "사고 켜기" 스위치로
        재프로브(OpenAI: chat_template_kwargs.enable_thinking / Anthropic:
        thinking 블록). 재프로브 실패는 관용(→ 미지원) — 기본 OFF 사고 모델
        (Qwen 등)을 transport 무관하게 잡는다.
    Everything else (reject-too-small, ``max_output = ctx//4``, assembly,
    error→None degradation) is shared. ``UnsupportedModelError`` (window below
    the minimum) propagates; any other probe failure returns None so detection
    degrades to defaults rather than crashing."""
    try:
        _emit_progress(f"First run for {model} — detecting capabilities")
        _emit_progress(f"Querying context window ({model})")
        context_window = transport.context_window()

        # Thinking probe runs before the reject (matches the historical order).
        # 2단계(기본 → 사고-켜기 재프로브, transport 공통 계약)라 기본 OFF 사고
        # 모델(Qwen 등)도 잡는다 — 미검출이면 재프로브만큼 더 걸릴 수 있다.
        _emit_progress(
            f"Probing thinking support ({model}) — may take ~10s on cold load"
        )
        supports_thinking = transport.thinking_probe()

        if context_window < MIN_CONTEXT_WINDOW:
            raise UnsupportedModelError(
                f"{model}: context window {context_window:,} is below the "
                f"{MIN_CONTEXT_WINDOW:,} minimum — too small to run the agent."
            )
        max_output = context_window // _OUTPUT_TOKEN_DIVISOR

        _emit_progress(f"Detection complete for {model}")
        return ModelCapabilities(
            context_window=context_window,
            max_output_tokens=max_output,
            supports_thinking=supports_thinking,
        )
    except UnsupportedModelError:
        # Hard reject — propagate to the CLI instead of degrading to defaults.
        raise
    except Exception as e:
        import sys

        print(f"[warn] capability detection failed for {model}: {e}", file=sys.stderr)
        return None


class _OpenAITransport:
    """Capability-probe transport for OpenAI-compatible servers
    (omlx / vLLM / LM Studio / OpenAI). Delegates context-window + structured
    probes to the existing OpenAI-specific helpers."""

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def context_window(self) -> int:
        return _detect_openai_context_window(self.base, self.model, self.api_key)

    def simple_chat(
        self, user_text: str, max_tokens: int, enable_thinking: bool = False
    ) -> str:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": user_text}],
            "max_tokens": max_tokens,
        }
        # 기본 OFF 모델(Qwen 등)을 재프로브할 때 사고 스위치를 켠다.
        if enable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": True}
        r = requests.post(
            f"{self.base}/chat/completions",
            json=body,
            headers=self._headers(),
            timeout=DETECTION_PROBE_TIMEOUT,
        )
        r.raise_for_status()
        msg = r.json().get("choices", [{}])[0].get("message", {}) or {}
        content = msg.get("content") or ""
        # vLLM reasoning 파서는 사고를 별도 `reasoning_content` 필드로 뽑는다
        # (content 엔 `<think>` 가 없음). 이를 `<think>` 로 정규화해 태그 검출과
        # 하나로 합친다 — 필드/태그 어느 쪽이든 감지.
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        return f"<think>{reasoning}</think>{content}" if reasoning else content

    def thinking_probe(self) -> bool:
        """2단계 사고 감지: ① 기본 프로브(모델이 기본으로 사고를 뱉으면 감지).
        미검출이면 ② `enable_thinking=true` 재프로브 — Qwen 처럼 사고가 기본 OFF
        이고 chat_template 스위치로 켜지는 모델을 잡는다. 재프로브가 실패(서버가
        chat_template_kwargs 거부 등)해도 검출 전체를 깨지 않도록 관용(→ 미지원).
        1차 프로브 오류는 상위로 전파(검출 실패 = 기본값 폴백, 기존 계약)."""
        if _detect_thinking(self.simple_chat("Say hello.", 512)):
            return True
        try:
            return _detect_thinking(
                self.simple_chat("Say hello.", 512, enable_thinking=True)
            )
        except Exception:
            return False


def _detect_openai_capabilities(
    base_url: str, model: str, api_key: str = ""
) -> ModelCapabilities | None:
    """Detect capabilities for OpenAI-compatible servers — back-compat entry
    that routes through the shared orchestrator with an OpenAI transport."""
    return _detect_capabilities(model, _OpenAITransport(base_url, model, api_key))


def _anthropic_text(data: dict) -> str:
    """Text of an Anthropic ``/messages`` response
    (``{"content": [{"type": "text", "text": …}]}``). 첫 **text 타입** 블록을
    찾는다 — thinking 활성 응답은 blocks[0] 이 thinking 블록이라 위치 고정
    인덱싱이 빈 문자열을 돌려주던 것을 수리."""
    blocks = data.get("content") if isinstance(data, dict) else None
    if isinstance(blocks, list):
        for b in blocks:
            if isinstance(b, dict) and b.get("type", "text") == "text":
                return b.get("text", "") or ""
    return ""


_ANTHROPIC_STRUCTURED_PROMPT = (
    'Respond with ONLY a JSON object of the form {"colors": [...]} listing three '
    "primary colors. No prose, no markdown fences."
)


class _AnthropicTransport:
    """Capability-probe transport for the Anthropic Messages API (real Anthropic
    or an Anthropic-compatible server such as omlx). ``/messages`` + ``x-api-key``
    / ``anthropic-version`` headers; response is ``content[].text``. Anthropic
    has no ``response_format`` enforcement, so the structured probe is
    prompt-only and strict-schema is always False."""

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if self.api_key:
            h["x-api-key"] = self.api_key
        return h

    def context_window(self) -> int:
        return _detect_anthropic_context_window(self.base, self.model, self.api_key)

    def simple_chat(
        self, user_text: str, max_tokens: int, enable_thinking: bool = False
    ) -> str:
        body: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_text}],
        }
        # 기본 OFF 모델을 재프로브할 때 Anthropic 방언의 사고 스위치(thinking
        # 블록)를 켠다 — OpenAI transport 의 chat_template_kwargs 와 동형 계약.
        # budget 은 API 하한 1024, max_tokens 는 budget 만큼 증액(Anthropic 이
        # 사고를 max_tokens 에서 차감하므로).
        if enable_thinking:
            body["thinking"] = {"type": "enabled", "budget_tokens": 1024}
            body["max_tokens"] = max_tokens + 1024
        r = requests.post(
            f"{self.base}/messages",
            json=body,
            headers=self._headers(),
            timeout=DETECTION_PROBE_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        content = _anthropic_text(data)
        # thinking 블록을 `<think>` 로 정규화 — OpenAI transport 의
        # reasoning_content 정규화와 동형(필드/태그 어느 쪽이든 감지).
        blocks = data.get("content") if isinstance(data, dict) else None
        thinking = ""
        if isinstance(blocks, list):
            thinking = "".join(
                b.get("thinking", "") or ""
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "thinking"
            )
        return f"<think>{thinking}</think>{content}" if thinking else content

    def thinking_probe(self) -> bool:
        """2단계 사고 감지 (transport 공통 계약 — OpenAI 와 동형): ① 기본
        프로브(`<think>` 태그·thinking 블록). 미검출이면 ② thinking 블록을 켠
        재프로브 — Anthropic-호환 로컬 서버(omlx 등)가 서빙하는 기본 OFF 사고
        모델(Qwen 류)을 잡는다. 재프로브 실패(비사고 모델에 thinking 블록을
        보내 400 등)는 관용(→ 미지원); 실 Anthropic 모델은 레지스트리 기반이라
        이 프로브 경로 자체를 타지 않는다. 1차 프로브 오류는 상위로 전파
        (검출 실패 = 기본값 폴백, 기존 계약)."""
        if _detect_thinking(self.simple_chat("Say hello.", 512)):
            return True
        try:
            return _detect_thinking(
                self.simple_chat("Say hello.", 512, enable_thinking=True)
            )
        except Exception:
            return False


def _detect_anthropic_context_window(
    base_url: str, model: str, api_key: str = ""
) -> int:
    """Anthropic context window: ``/models`` metadata → ``/messages`` overflow
    probe → ``_DEFAULT_CONTEXT_FALLBACK``. Mirrors the OpenAI tiers via the
    Anthropic API. Real Anthropic ``/v1/models`` lacks the field (→ fallback /
    registry), but omlx-style servers DO expose ``max_model_len`` and the
    overflow probe reveals the limit otherwise."""
    base = base_url.rstrip("/")
    headers = {"anthropic-version": "2023-06-01"}
    if api_key:
        headers["x-api-key"] = api_key
    # Tier 1: /models metadata (omlx exposes it; real Anthropic doesn't).
    try:
        r = requests.get(f"{base}/models", headers=headers, timeout=10)
        r.raise_for_status()
        for m in r.json().get("data", []):
            if m.get("id") == model:
                for key in ("max_model_len", "context_length"):
                    ctx = m.get(key)
                    if isinstance(ctx, int) and ctx > 0:
                        return ctx
                break
    except Exception:
        pass
    # Tier 2: overflow probe via /messages — parse the limit from the 400 body.
    from agent_cli.context.overflow import is_context_overflow, parse_overflow_amounts

    _emit_progress(f"Probing context window via overflow ({model})")
    try:
        r = requests.post(
            f"{base}/messages",
            json={
                "model": model,
                "max_tokens": 16,
                "messages": [
                    {"role": "user", "content": "word " * _CONTEXT_PROBE_WORDS}
                ],
            },
            headers={**headers, "Content-Type": "application/json"},
            timeout=DETECTION_PROBE_TIMEOUT,
        )
        if r.status_code != 200 and is_context_overflow(r.text):
            _actual, limit = parse_overflow_amounts(r.text)
            if limit and limit > 0:
                return limit
    except Exception:
        pass
    return _DEFAULT_CONTEXT_FALLBACK


def _detect_openai_context_window(base_url: str, model: str, api_key: str = "") -> int:
    """Determine the model's context window for an OpenAI-compatible server.

    Three tiers, in order:
      1. ``/v1/models`` metadata — ``max_model_len`` (vLLM) or
         ``context_length``. Cheapest and exact when present.
      2. Overflow probe — servers that don't expose the window in
         metadata (notably omlx/mlx-lm) still reveal it by rejecting an
         over-limit prompt with a 400 that names the limit. See
         ``_probe_context_window_via_overflow``.
      3. ``_DEFAULT_CONTEXT_FALLBACK`` (128K) when neither yields a
         number — conservative/under-set so it never triggers a 400 on
         its own; flow-2 runtime recovery corrects a too-large estimate.
    """
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        r = requests.get(f"{base_url}/models", headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()

        for m in data.get("data", []):
            if m.get("id") == model:
                # vLLM: max_model_len
                ctx = m.get("max_model_len")
                if isinstance(ctx, int) and ctx > 0:
                    return ctx
                # Some servers: context_length
                ctx = m.get("context_length")
                if isinstance(ctx, int) and ctx > 0:
                    return ctx
                break
    except Exception:
        pass

    # Metadata didn't expose it (e.g. omlx) — probe via overflow.
    _emit_progress(f"Probing context window via overflow ({model})")
    probed = _probe_context_window_via_overflow(base_url, model, api_key)
    if probed:
        return probed

    return _DEFAULT_CONTEXT_FALLBACK


def _probe_context_window_via_overflow(
    base_url: str, model: str, api_key: str = ""
) -> int | None:
    """Read the real context window from an intentional overflow rejection.

    Servers that don't advertise their window in ``/v1/models`` metadata
    (notably omlx/mlx-lm) reject an over-limit prompt with a 400 whose
    body names the limit, e.g. ``"...exceeds max context window of
    262144 tokens"``. We send a deliberately huge prompt and parse that
    number out via the same ``parse_overflow_amounts`` the runtime
    recovery layer uses.

    Cost: an over-limit prompt is rejected right after *tokenisation* —
    no eval, no generation — so the server is not occupied the way an
    under-limit prompt would be (verified live against omlx, 2026-05-30).
    This is why we never binary-search toward the boundary: an
    under-limit probe would force a full prompt-eval and block the
    server for the duration.

    Returns the parsed limit, or ``None`` when the server accepted the
    prompt (window exceeds the probe size), didn't return an overflow
    400, or returned a 400 without a parseable number. The caller falls
    back to a conservative default in those cases.
    """
    from agent_cli.context.overflow import (
        is_context_overflow,
        parse_overflow_amounts,
    )

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "word " * _CONTEXT_PROBE_WORDS}],
        "max_tokens": 16,
    }
    try:
        r = requests.post(
            url, json=body, headers=headers, timeout=DETECTION_PROBE_TIMEOUT
        )
    except Exception:
        return None

    if r.status_code == 200:
        # Prompt fit — the window is larger than our probe; can't learn
        # the exact value this way.
        return None

    try:
        text = r.text
    except Exception:
        return None

    if not is_context_overflow(text):
        return None
    _actual, limit = parse_overflow_amounts(text)
    return limit if (limit and limit > 0) else None
