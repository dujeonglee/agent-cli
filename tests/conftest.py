"""Shared test fixtures and configuration.

Integration test models can be changed via:
  1. Environment variable: INTEGRATION_MODELS="model1,model2"
  2. Editing DEFAULT_MODELS list below
"""

from __future__ import annotations

import os

import pytest
import requests

# ── Integration Test Model Configuration ──────────────────
# Change these to use different models for E2E testing.
# Or set INTEGRATION_MODELS env var (comma-separated).
DEFAULT_MODELS = [
    "qwen3-coder:30b",  # Thinking + coding specialized
    "glm-4.7-flash:q8_0",  # Non-thinking general purpose
    "qwen3.5:35b",  # Latest generation general purpose
]

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")


def _get_integration_models() -> list[str]:
    """Get model list from env var or defaults."""
    env = os.environ.get("INTEGRATION_MODELS", "")
    if env:
        return [m.strip() for m in env.split(",") if m.strip()]
    return DEFAULT_MODELS


def _is_ollama_available() -> bool:
    """Check if Ollama is reachable."""
    try:
        r = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def _is_model_available(model: str) -> bool:
    """Check if a specific model is loaded in Ollama."""
    try:
        r = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        if r.status_code != 200:
            return False
        models = [m["name"] for m in r.json().get("models", [])]
        return model in models
    except Exception:
        return False


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def ollama_available():
    """Skip all integration tests if Ollama is not running."""
    if not _is_ollama_available():
        pytest.skip("Ollama not available at " + OLLAMA_BASE_URL)
    return True


def get_available_integration_models() -> list[str]:
    """Return only models that are actually available in Ollama."""
    if not _is_ollama_available():
        return []
    requested = _get_integration_models()
    return [m for m in requested if _is_model_available(m)]


# Generate parametrize IDs for available models
_available_models = get_available_integration_models()


@pytest.fixture(
    params=_available_models if _available_models else ["no-models-available"],
    scope="session",
)
def integration_model(request, ollama_available):
    """Parametrized fixture: runs test once per available model."""
    if request.param == "no-models-available":
        pytest.skip("No integration test models available in Ollama")
    return request.param


@pytest.fixture(scope="session")
def ollama_provider():
    """Real OllamaProvider instance."""
    from agent_cli.providers.ollama import OllamaProvider

    return OllamaProvider(OLLAMA_BASE_URL)


@pytest.fixture
def model_capabilities(integration_model):
    """Get capabilities for integration model (runtime detection)."""
    from agent_cli.providers.compat import get_capabilities

    return get_capabilities(
        integration_model, provider="ollama", base_url=OLLAMA_BASE_URL
    )
