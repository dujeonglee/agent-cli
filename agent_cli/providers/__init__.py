"""Provider adapters and capabilities."""

from agent_cli.providers.base import LLMProvider, LLMResponse, TokenUsage
from agent_cli.providers.compat import ModelCapabilities, get_capabilities

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "TokenUsage",
    "ModelCapabilities",
    "get_capabilities",
    "create_provider",
]


def create_provider(provider: str, base_url: str, api_key: str) -> LLMProvider:
    """Create a provider adapter instance by name."""
    if provider == "anthropic":
        from agent_cli.providers.anthropic import AnthropicProvider

        return AnthropicProvider(base_url, api_key)
    elif provider == "openai":
        from agent_cli.providers.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(base_url, api_key)
    elif provider == "ollama":
        from agent_cli.providers.ollama import OllamaProvider

        return OllamaProvider(base_url, api_key)
    else:
        raise ValueError(
            f"Unknown provider: {provider}. Available: anthropic, openai, ollama"
        )
