"""Model capabilities: what each model supports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import re

import requests

from agent_cli.constants import DETECTION_PROBE_TIMEOUT

from agent_cli.config import get_model_entry, save_model_entry


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
    context_window: int
    max_output_tokens: int
    supports_structured_output: bool
    supports_thinking: bool
    thinking_budget: int
    supports_strict_schema: bool
    thinking_format: str = ""  # "think", "reasoning", "" (none)


# Conservative defaults for unregistered models
DEFAULT_CAPABILITIES = ModelCapabilities(
    context_window=4096,
    max_output_tokens=2048,
    supports_structured_output=False,
    supports_thinking=False,
    thinking_budget=0,
    supports_strict_schema=False,
    thinking_format="",
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


def _auto_save_detected(model: str, caps: ModelCapabilities) -> None:
    """Save runtime-detected capabilities to ~/.agent-cli/models.json (new models only)."""
    entry = {
        "context_window": caps.context_window,
        "max_output_tokens": caps.max_output_tokens,
        "supports_structured_output": caps.supports_structured_output,
        "supports_thinking": caps.supports_thinking,
        "thinking_budget": caps.thinking_budget,
        "supports_strict_schema": caps.supports_strict_schema,
        "thinking_format": caps.thinking_format,
        "_auto_detected": True,
    }
    save_model_entry(model, entry)


def _build_from_entry(entry: dict) -> ModelCapabilities:
    # Legacy field `supports_tool_calling` — silently ignored if present in
    # older models.json entries; the loop uses ReAct text parsing, not the
    # native tool-calling API on any provider.
    return ModelCapabilities(
        context_window=entry.get("context_window", 4096),
        max_output_tokens=entry.get("max_output_tokens", 2048),
        supports_structured_output=entry.get("supports_structured_output", False),
        supports_thinking=entry.get("supports_thinking", False),
        thinking_budget=entry.get("thinking_budget", 0),
        supports_strict_schema=entry.get("supports_strict_schema", False),
        thinking_format=entry.get("thinking_format", ""),
    )


# Compiled patterns for efficiency
_THINKING_TAGS = ["think", "thinking", "reasoning", "reflection"]
_THINKING_TAG_PATTERN = re.compile(
    r"<(" + "|".join(_THINKING_TAGS) + r")>",
    re.I,
)


def _detect_runtime_capabilities(
    provider: str, base_url: str, model: str, api_key: str = ""
) -> ModelCapabilities | None:
    """Detect model capabilities at runtime via provider API."""
    if provider == "openai":
        return _detect_openai_capabilities(base_url, model, api_key)
    return None


def _detect_openai_capabilities(
    base_url: str, model: str, api_key: str = ""
) -> ModelCapabilities | None:
    """Detect capabilities for OpenAI-compatible servers (vLLM, LM Studio, mlx-lm).

    Step 1: GET /v1/models for context window (max_model_len — vLLM, etc.)
    Step 2: Probe with simple prompt for thinking support
    """
    try:
        base = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        _emit_progress(f"First run for {model} — detecting capabilities")

        # Step 1: Try to get context window from /v1/models
        _emit_progress(f"Querying context window via /v1/models ({model})")
        context_window = _detect_openai_context_window(base, model, api_key)

        # Step 2: Probe for thinking support
        _emit_progress(
            f"Probing thinking support ({model}) — may take ~10s on cold load"
        )
        url = f"{base}/chat/completions"
        r = requests.post(
            url,
            json={
                "model": model,
                "messages": [
                    {"role": "user", "content": "Say hello."},
                ],
                "max_tokens": 512,
            },
            headers=headers,
            timeout=DETECTION_PROBE_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        supports_thinking = False
        thinking_format = ""
        match = _THINKING_TAG_PATTERN.search(content)
        if match:
            supports_thinking = True
            thinking_format = match.group(1).lower()

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
            supports_structured_output=False,
            supports_thinking=supports_thinking,
            thinking_budget=4096 if supports_thinking else 0,
            supports_strict_schema=False,
            thinking_format=thinking_format,
        )
    except UnsupportedModelError:
        # Hard reject — propagate to the CLI instead of degrading to defaults.
        raise
    except Exception as e:
        import sys

        print(
            f"[warn] OpenAI-compat detection failed for {model}: {e}",
            file=sys.stderr,
        )
        return None


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
