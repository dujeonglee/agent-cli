"""Model capabilities: what each model supports."""

from __future__ import annotations

from dataclasses import dataclass

import re

import requests

from agent_cli.config import get_model_entry, save_model_entry


@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int
    max_output_tokens: int
    supports_structured_output: bool
    supports_tool_calling: bool
    supports_thinking: bool
    thinking_budget: int
    supports_strict_schema: bool
    thinking_format: str = ""  # "think", "reasoning", "" (none)


# Conservative defaults for unregistered models
DEFAULT_CAPABILITIES = ModelCapabilities(
    context_window=4096,
    max_output_tokens=2048,
    supports_structured_output=False,
    supports_tool_calling=False,
    supports_thinking=False,
    thinking_budget=0,
    supports_strict_schema=False,
    thinking_format="",
)


# Tracks whether the last get_capabilities() call triggered runtime detection
_last_was_runtime_detected: bool = False


def was_runtime_detected() -> bool:
    """Return True if the last get_capabilities() call used runtime detection."""
    return _last_was_runtime_detected


def get_capabilities(
    model: str,
    provider: str | None = None,
    base_url: str | None = None,
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
        detected = _detect_runtime_capabilities(provider, base_url, model)
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
        "supports_tool_calling": caps.supports_tool_calling,
        "supports_thinking": caps.supports_thinking,
        "thinking_budget": caps.thinking_budget,
        "supports_strict_schema": caps.supports_strict_schema,
        "thinking_format": caps.thinking_format,
        "_auto_detected": True,
    }
    save_model_entry(model, entry)


def _build_from_entry(entry: dict) -> ModelCapabilities:
    return ModelCapabilities(
        context_window=entry.get("context_window", 4096),
        max_output_tokens=entry.get("max_output_tokens", 2048),
        supports_structured_output=entry.get("supports_structured_output", False),
        supports_tool_calling=entry.get("supports_tool_calling", False),
        supports_thinking=entry.get("supports_thinking", False),
        thinking_budget=entry.get("thinking_budget", 0),
        supports_strict_schema=entry.get("supports_strict_schema", False),
        thinking_format=entry.get("thinking_format", ""),
    )


def _detect_runtime_capabilities(
    provider: str, base_url: str, model: str
) -> ModelCapabilities | None:
    """Detect model capabilities at runtime via provider API."""
    if provider == "ollama":
        return _detect_ollama_capabilities(base_url, model)
    elif provider == "openai":
        return _detect_openai_compat_capabilities(base_url, model)
    return None


def _detect_ollama_capabilities(base_url: str, model: str) -> ModelCapabilities | None:
    """Query Ollama /api/show for model info, then probe for thinking support."""
    try:
        # Step 1: Get model metadata
        url = f"{base_url.rstrip('/')}/api/show"
        r = requests.post(url, json={"model": model}, timeout=10)
        r.raise_for_status()
        data = r.json()

        # Extract context length (architecture-agnostic)
        model_info = data.get("model_info", {})
        context_length = 4096
        for key, value in model_info.items():
            if key.endswith(".context_length") or key == "context_length":
                if isinstance(value, int) and value > 0:
                    context_length = value
                    break

        max_output = min(context_length // 4, 4096)

        # Step 2: Probe for thinking support by sending a simple prompt
        supports_thinking, thinking_format = _probe_thinking_support(base_url, model)
        thinking_budget = 4096 if supports_thinking else 0

        return ModelCapabilities(
            context_window=context_length,
            max_output_tokens=max_output,
            supports_structured_output=True,
            supports_tool_calling=False,
            supports_thinking=supports_thinking,
            thinking_budget=thinking_budget,
            supports_strict_schema=False,
            thinking_format=thinking_format,
        )
    except Exception:
        return None


# Known thinking block tags to detect in probe response
_THINKING_TAGS = ["think", "thinking", "reasoning", "reflection"]
_THINKING_TAG_PATTERN = re.compile(
    r"<(" + "|".join(_THINKING_TAGS) + r")>",
    re.I,
)


def _probe_thinking_support(base_url: str, model: str) -> tuple[bool, str]:
    """Send a simple prompt and check if the model produces thinking blocks.

    Returns (supports_thinking, thinking_format).
    """
    try:
        url = f"{base_url.rstrip('/')}/api/chat"
        r = requests.post(
            url,
            json={
                "model": model,
                "stream": False,
                "messages": [
                    {"role": "user", "content": "Say hello."},
                ],
            },
            timeout=30,
        )
        r.raise_for_status()
        content = r.json().get("message", {}).get("content", "")

        # Check for thinking tags in response
        match = _THINKING_TAG_PATTERN.search(content)
        if match:
            return True, match.group(1).lower()

        return False, ""
    except Exception:
        return False, ""


def _detect_openai_compat_capabilities(
    base_url: str, model: str
) -> ModelCapabilities | None:
    """Detect capabilities for OpenAI-compatible servers (vLLM, LM Studio, mlx-lm).

    Step 1: GET /v1/models for context window (max_model_len — vLLM, etc.)
    Step 2: Probe with simple prompt for thinking support
    """
    try:
        base = base_url.rstrip("/")

        # Step 1: Try to get context window from /v1/models
        context_window = _detect_openai_context_window(base, model)

        # Step 2: Probe for thinking support
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
            headers={"Content-Type": "application/json"},
            timeout=30,
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

        max_output = min(context_window // 4, 4096)

        return ModelCapabilities(
            context_window=context_window,
            max_output_tokens=max_output,
            supports_structured_output=False,
            supports_tool_calling=False,
            supports_thinking=supports_thinking,
            thinking_budget=4096 if supports_thinking else 0,
            supports_strict_schema=False,
            thinking_format=thinking_format,
        )
    except Exception:
        return None


def _detect_openai_context_window(base_url: str, model: str) -> int:
    """Try to get context window from /v1/models endpoint.

    vLLM returns max_model_len. Other servers may not.
    Returns detected value or 4096 default.
    """
    try:
        r = requests.get(f"{base_url}/models", timeout=10)
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

    return 4096  # conservative default
