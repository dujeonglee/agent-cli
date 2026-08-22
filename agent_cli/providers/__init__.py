"""Provider adapters and capabilities.

프로바이더 self-register (v8.41.0, 리뷰 §4.2): 종전엔 프로바이더 추가 시
``create_provider`` 분기 + ``capabilities._detect_runtime_capabilities``
분기 등 여러 곳을 동기 수정해야 했다. 이제 각 프로바이더 모듈이 import
시점에 :func:`register_provider` 로 스스로 등록하고 (wire_formats 의
register 와 동형 패턴), capability 프로브 transport 도 프로바이더 클래스가
``capability_transport`` 훅으로 소유한다 — **프로바이더 추가 = 모듈 1개
+ 내장 목록(_BUILTIN_MODULES) 1줄**.
"""

from __future__ import annotations

import importlib

from agent_cli.providers.base import LLMProvider, LLMResponse, TokenUsage
from agent_cli.providers.capabilities import (
    ModelCapabilities,
    UnsupportedModelError,
    get_capabilities,
)

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "ModelCapabilities",
    "TokenUsage",
    "UnsupportedModelError",
    "create_provider",
    "get_capabilities",
    "get_provider_class",
    "register_provider",
]

# name → provider class. 내장은 _BUILTIN_MODULES 의 지연 import 가 채우고,
# 외부 프로바이더는 register_provider 직접 호출로 참여한다.
_PROVIDERS: dict[str, type] = {}

# 내장 프로바이더 모듈 — import 부수효과로 self-register 된다. 패키지
# import 시점이 아니라 첫 조회 시점에 로드 (requests 등 무거운 의존을
# 레지스트리 조회 전까지 지연 — 종전 create_provider 의 지연 import 유지).
_BUILTIN_MODULES = (
    "agent_cli.providers.openai",
    "agent_cli.providers.anthropic",
)


def register_provider(name: str, provider_cls: type) -> None:
    """프로바이더 등록 — 각 어댑터 모듈이 import 시점에 자기 등록.

    같은 클래스 재등록은 no-op (모듈 재-import 관용); 다른 클래스로의
    이름 충돌은 프로그래밍 오류라 시끄럽게 거부한다."""
    existing = _PROVIDERS.get(name)
    if existing is not None and existing is not provider_cls:
        raise ValueError(
            f"Provider '{name}' is already registered to a different class. "
            "Each adapter module should register exactly once."
        )
    _PROVIDERS[name] = provider_cls


def _ensure_builtins_loaded() -> None:
    for mod in _BUILTIN_MODULES:
        importlib.import_module(mod)


def get_provider_class(name: str) -> type | None:
    """등록된 프로바이더 클래스 — 미등록이면 None (호출자 폴백 판단)."""
    _ensure_builtins_loaded()
    return _PROVIDERS.get(name)


def create_provider(provider: str, base_url: str, api_key: str) -> LLMProvider:
    """Create a provider adapter instance by name."""
    cls = get_provider_class(provider)
    if cls is None:
        available = ", ".join(sorted(_PROVIDERS))
        raise ValueError(f"Unknown provider: {provider}. Available: {available}")
    return cls(base_url, api_key)
