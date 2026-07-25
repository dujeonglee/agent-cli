"""Shared test fixtures and configuration.

Integration fixtures target a live OpenAI-compatible omlx server. Override
the connection with environment variables:
  - OMLX_BASE_URL       (default http://127.0.0.1:8000/v1)
  - OMLX_API_KEY        (default empty — local servers usually need none)
  - INTEGRATION_MODELS  comma-separated model ids (default Qwen3.6-27B-MLX-8bit)

Tests marked ``omlx_integration`` skip automatically when the server is
unreachable, so a plain ``pytest tests/`` stays green. Run them explicitly
against a live server with ``pytest tests/ -m omlx_integration``.
"""

from __future__ import annotations

import os

# 실브라우저 스위트(tests/browser)는 옵트인 아니면 **수집조차** 안 한다.
# per-item skip 만으로는 pytest-asyncio 가 수집 단계에서 이벤트 루프를
# 남겨 이후 async e2e(trust_local/stream)를 깨뜨린다 — collect_ignore 로
# 원천 차단. 브라우저 CI 는 AGENT_CLI_BROWSER_TESTS=1 로 별도 잡 실행.
if os.environ.get("AGENT_CLI_BROWSER_TESTS") != "1":
    collect_ignore = ["browser"]

import pytest
import requests


@pytest.fixture(autouse=True)
def _disable_workspace_confine(monkeypatch):
    """Disable workspace confinement across the unit suite.

    Confinement is default-ON in production, but tests write / edit under
    ``tmp_path`` (outside the repo cwd = outside the workspace) and run without
    a TTY, so the gate would refuse them. Off by default here; the dedicated
    ``test_confine.py`` re-enables it explicitly and drives the prompt flow.
    """
    monkeypatch.setenv("AGENT_CLI_WORKSPACE_CONFINE", "0")


# ── omlx integration connection ───────────────────────────
OMLX_BASE_URL = os.environ.get("OMLX_BASE_URL", "http://127.0.0.1:8000/v1")
OMLX_API_KEY = os.environ.get("OMLX_API_KEY", "")

# Default model for integration runs. Override with INTEGRATION_MODELS.
DEFAULT_INTEGRATION_MODELS = ["Qwen3.6-27B-MLX-8bit"]


def _omlx_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OMLX_API_KEY}"} if OMLX_API_KEY else {}


def _list_omlx_models() -> list[str]:
    """Return model ids from the omlx ``/v1/models`` endpoint, or [] if the
    server is unreachable. Connection refused returns fast, so probing this
    at collection time does not slow a server-down ``pytest tests/`` run."""
    try:
        r = requests.get(
            f"{OMLX_BASE_URL.rstrip('/')}/models",
            headers=_omlx_headers(),
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]
    except Exception:
        return []
    return []


def _is_omlx_available() -> bool:
    return bool(_list_omlx_models())


def _requested_integration_models() -> list[str]:
    env = os.environ.get("INTEGRATION_MODELS", "")
    if env:
        return [m.strip() for m in env.split(",") if m.strip()]
    return DEFAULT_INTEGRATION_MODELS


def get_available_integration_models() -> list[str]:
    """Requested models that are actually served right now."""
    available = _list_omlx_models()
    if not available:
        return []
    return [m for m in _requested_integration_models() if m in available]


# Probed once at collection time → parametrize ids for available models.
_available_models = get_available_integration_models()


# ── Auto-reset loaders after each test ────────────────────


@pytest.fixture(autouse=True)
def _reset_loaders_after_test():
    """Reset skill and agent loaders to default paths after each test."""
    yield
    try:
        from agent_cli.skills.loader import _reset_loader

        _reset_loader()
    except Exception:
        pass
    # main registry 프로세스 슬롯 격리 (v7.17.0) — 테스트가 등록한 슬롯이
    # 다음 테스트의 execute_skill 자동 상속으로 새지 않게 항상 비운다.
    try:
        from agent_cli.subagent.agents_live import set_main_registry

        set_main_registry(None)
    except Exception:
        pass


# ── omlx integration fixtures ─────────────────────────────


@pytest.fixture(scope="session")
def omlx_available():
    """Skip the integration suite if the omlx server is not reachable."""
    if not _is_omlx_available():
        pytest.skip(f"omlx not available at {OMLX_BASE_URL}")
    return True


@pytest.fixture(
    params=_available_models if _available_models else ["no-models-available"],
    scope="session",
)
def integration_model(request, omlx_available):
    """Parametrized fixture: runs each test once per available model."""
    if request.param == "no-models-available":
        pytest.skip("No integration models available on the omlx server")
    return request.param


@pytest.fixture(scope="session")
def omlx_provider():
    """Real OpenAIProvider pointed at the omlx server."""
    from agent_cli.providers.openai import OpenAIProvider

    return OpenAIProvider(OMLX_BASE_URL, OMLX_API_KEY)


@pytest.fixture
def model_capabilities(integration_model):
    """Runtime-detected capabilities for the integration model."""
    from agent_cli.providers.capabilities import get_capabilities

    return get_capabilities(
        integration_model, provider="openai", base_url=OMLX_BASE_URL
    )


@pytest.fixture(autouse=True)
def _restore_profile_loader():
    """profiles._profile_loader 는 테스트가 직접 교체하는 모듈 전역 —
    테스트 간 누수를 원천 차단(스냅샷/복원). C2 의 _agent_loader 규율을
    5.0.0 프로파일 병합 로더로 승계."""
    import agent_cli.subagent.profiles as _profiles_mod

    orig = _profiles_mod._profile_loader
    yield
    _profiles_mod._profile_loader = orig
