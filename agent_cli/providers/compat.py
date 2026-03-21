"""Model capabilities: what each model supports."""
from __future__ import annotations

from dataclasses import dataclass

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
    return None


def _detect_ollama_capabilities(
    base_url: str, model: str
) -> ModelCapabilities | None:
    """Query Ollama /api/show for model info."""
    try:
        url = f"{base_url.rstrip('/')}/api/show"
        r = requests.post(url, json={"model": model}, timeout=10)
        r.raise_for_status()
        data = r.json()

        # Extract model parameters
        model_info = data.get("model_info", {})
        # Context length: search for any key ending with ".context_length"
        # Different architectures use different prefixes:
        #   llama.context_length, qwen3next.context_length, gemma.context_length, etc.
        context_length = 4096  # default
        for key, value in model_info.items():
            if key.endswith(".context_length") or key == "context_length":
                if isinstance(value, int) and value > 0:
                    context_length = value
                    break

        # Detect parameter count from details
        details = data.get("details", {})
        param_size = details.get("parameter_size", "")

        # Estimate max_output_tokens (conservative: 25% of context)
        max_output = min(context_length // 4, 4096)

        # Heuristic: Ollama models generally support JSON format
        supports_structured = True

        # Detect thinking support and format from model family
        family = details.get("family", "").lower()
        model_lower = model.lower()
        supports_thinking = any(
            t in model_lower for t in ("qwen3", "deepseek-r1", "thinking")
        )
        thinking_budget = 4096 if supports_thinking else 0
        thinking_format = "think" if supports_thinking else ""

        return ModelCapabilities(
            context_window=context_length,
            max_output_tokens=max_output,
            supports_structured_output=supports_structured,
            supports_tool_calling=False,
            supports_thinking=supports_thinking,
            thinking_budget=thinking_budget,
            supports_strict_schema=False,
            thinking_format=thinking_format,
        )
    except Exception:
        return None
