"""Ollama API provider adapter with constrained decoding support."""
from __future__ import annotations

import sys

import requests

from agent_cli.constants import LLM_API_TIMEOUT

from agent_cli.providers.base import LLMResponse, TokenUsage
from agent_cli.providers.compat import ModelCapabilities

# JSON Schema for ReAct format used with Ollama's constrained decoding.
# "thought" is free-form string; action/action_input are schema-enforced.
REACT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string"},
        "action_input": {},
        "final_answer": {"type": "string"},
    },
    "required": ["thought"],
}


class OllamaProvider:
    """Adapter for Ollama's /api/chat endpoint."""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")

    def call(
        self,
        messages: list[dict],
        system: str,
        model: str,
        capabilities: ModelCapabilities,
        **kwargs,
    ) -> LLMResponse:
        url = f"{self.base_url}/api/chat"
        msgs = [{"role": "system", "content": system}] + messages

        body: dict = {
            "model": model,
            "stream": False,
            "messages": msgs,
        }

        # format control: skip_json_format=True disables all JSON forcing
        # (needed for plan generation where free-form text is expected)
        skip_json = kwargs.get("skip_json_format", False)
        if not skip_json:
            if capabilities.supports_structured_output:
                body["format"] = REACT_JSON_SCHEMA
            else:
                body["format"] = "json"

        # Thinking budget: allocate enough output tokens for thinking + response
        if capabilities.supports_thinking and capabilities.thinking_budget > 0:
            body.setdefault("options", {})
            body["options"]["num_predict"] = (
                capabilities.thinking_budget + capabilities.max_output_tokens
            )

        try:
            r = requests.post(url, json=body, timeout=LLM_API_TIMEOUT)
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            # Ollama < 0.5.0 does not support JSON Schema in format param.
            # Fall back to basic "json" mode.
            if (
                capabilities.supports_structured_output
                and e.response is not None
                and e.response.status_code == 400
            ):
                print(
                    "[warn] Ollama rejected JSON Schema format. "
                    "Falling back to basic JSON mode. "
                    "Upgrade Ollama to 0.5.0+ for constrained decoding.",
                    file=sys.stderr,
                )
                body["format"] = "json"
                r = requests.post(url, json=body, timeout=LLM_API_TIMEOUT)
                r.raise_for_status()
            else:
                raise

        data = r.json()
        content = data.get("message", {}).get("content", "")

        usage = None
        if "eval_count" in data or "prompt_eval_count" in data:
            usage = TokenUsage(
                input_tokens=data.get("prompt_eval_count", 0),
                output_tokens=data.get("eval_count", 0),
            )

        return LLMResponse(
            content=content,
            tool_calls=None,
            usage=usage,
            stop_reason=data.get("done_reason"),
        )
