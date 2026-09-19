"""Configuration loading: config.json + models.json registry + provider defaults.

Config loading priority (highest wins):
  1. .agent-cli/config.json          (workspace)
  2. ~/.agent-cli/config.json        (user)
  3. Environment variables            (global)

Models.json search paths (project local takes priority):
  1. .agent-cli/models.json           (project local, read-only)
  2. ~/.agent-cli/models.json         (user global, auto-save target)
  3. agent_cli/default_models.json    (package defaults, read-only)
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_cli.fsio import atomic_write_json


@dataclass
class ProviderDefaults:
    base_url: str
    default_model: str


# v9.0.0 — models.json 은 **유저 스코프만** (docs/config-scopes). capability
# 탐지(콜드 로드 유발)의 캐시라 머신 단위가 맞고, 자동 저장도 원래 여기였다.
# 종전의 프로젝트 models.json 은 무시된다.
from agent_cli.paths import user_dir

_GLOBAL_MODELS_PATH = user_dir() / "models.json"
_SEARCH_PATHS = [
    _GLOBAL_MODELS_PATH,
    Path(__file__).parent / "default_models.json",
]

_cached_registry: dict[str, Any] | None = None

# 프로바이더별 기본 **주소**만. 모델 이름은 여기 두지 않는다 (v9.5.0):
# `gpt-4o` / `claude-sonnet-4-…` 같은 추측은 로컬 서버(omlx·vLLM·LM Studio)에서
# **절대 맞을 수 없고**, "모델을 안 골랐다"를 "없는 모델 404"로 바꿔 원인을
# 가렸다. 아무것도 해석되지 않으면 추측하지 말고 그렇게 말해야 한다
# (`model_check.NoModelSelected`). 주소는 사정이 다르다 — 프로바이더의 공개
# 엔드포인트는 실제로 그 주소가 맞다.
_PROVIDER_FALLBACK_URLS = {
    "anthropic": "https://api.anthropic.com/v1",
    "openai": "https://api.openai.com/v1",
}


def _load_registry() -> dict[str, Any]:
    global _cached_registry
    if _cached_registry is not None:
        return _cached_registry

    # Merge: load global first, then overlay project-local on top
    merged: dict[str, Any] = {"models": {}, "provider_defaults": {}}

    for p in reversed(_SEARCH_PATHS):  # global first, then local overrides
        if p.is_file():
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                # Merge models
                merged["models"].update(data.get("models", {}))
                # Merge provider_defaults
                merged["provider_defaults"].update(data.get("provider_defaults", {}))
            except Exception as e:
                print(f"[warn] Failed to load {p}: {e}", file=sys.stderr)

    _cached_registry = merged
    return _cached_registry


def get_model_entry(model: str) -> dict[str, Any] | None:
    """Return the raw model entry dict from models.json, or None."""
    registry = _load_registry()
    return registry.get("models", {}).get(model)


def get_provider_defaults(provider: str) -> ProviderDefaults:
    """Return base_url and default_model for a provider."""
    registry = _load_registry()
    entry = registry.get("provider_defaults", {}).get(provider, {})

    fb_url = _PROVIDER_FALLBACK_URLS.get(provider, "http://127.0.0.1:8000/v1")

    return ProviderDefaults(
        base_url=entry.get("base_url", fb_url),
        # 빈 문자열 = "고른 적 없음". 추측하지 않는다.
        default_model=entry.get("default_model", ""),
    )


def save_model_entry(model: str, entry: dict) -> bool:
    """Save a runtime-detected model to ~/.agent-cli/models.json.

    Only adds new models — never overwrites existing entries.
    Returns True if saved, False if already exists or error.
    """
    target = _GLOBAL_MODELS_PATH

    # Load existing global file
    existing: dict[str, Any] = {"models": {}, "provider_defaults": {}}
    if target.is_file():
        try:
            with open(target, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception as e:
            print(f"[warn] Cannot read {target}: {e}", file=sys.stderr)
            return False

    # Don't overwrite manually registered entries;
    # allow refresh for auto-detected ones (model or server-config updates)
    existing_entry = existing.get("models", {}).get(model)
    if existing_entry is not None and not existing_entry.get("_auto_detected"):
        return False
    if existing_entry is not None:
        # refresh 는 프로브 산출 필드만 갱신 — 사용자가 auto-detected 엔트리에
        # 손으로 추가한 키(예: wire_format 바인딩)는 보존 (multi-wire-format
        # §7-A1). caps_to_entry 는 capabilities 필드만 내므로 단순 병합으로 충분.
        entry = {**existing_entry, **entry}

    # Add new model
    existing.setdefault("models", {})[model] = entry

    # Save
    try:
        atomic_write_json(target, existing, indent=2)  # 상태 파일 — fsio
    except Exception as e:
        print(f"[warn] Cannot save to {target}: {e}", file=sys.stderr)
        return False

    # Invalidate cache so next load picks up the new entry
    reload_registry()
    return True


def reload_registry() -> None:
    """Force reload models.json (for testing)."""
    global _cached_registry
    _cached_registry = None


# ── Config.json (provider/model/url settings) ──────────────────

# v9.0.0 — config.json 은 **유저 스코프만** (docs/config-scopes): provider·
# API 키는 머신 사실이다. 우선순위는 유저 파일 > env. 프로젝트별 모델 고정은
# `--model`/env 로 (종전의 프로젝트 config.json 은 무시된다).
_CONFIG_PATHS = [user_dir() / "config.json"]

# Environment variable → config key mapping
_ENV_MAP = {
    "AGENT_CLI_PROVIDER": "provider",
    "AGENT_CLI_BASE_URL": "base_url",
    "AGENT_CLI_API_KEY": "api_key",
    "AGENT_CLI_MODEL": "default_model",
}

_cached_config: dict[str, str] | None = None


def load_config(use_cache: bool = True) -> dict[str, str]:
    """Load config by merging: env vars → user config (v9.0.0: 유저 단일).

    Higher priority layers override lower ones per-field.
    Returns a dict with keys: provider, base_url, api_key, default_model.
    """
    global _cached_config
    if use_cache and _cached_config is not None:
        return _cached_config

    # Layer 1: environment variables (lowest priority)
    merged: dict[str, str] = {}
    for env_key, config_key in _ENV_MAP.items():
        val = os.environ.get(env_key, "")
        if val:
            merged[config_key] = val

    # Layer 2+: config files (reversed so highest priority is applied last)
    for config_path in reversed(_CONFIG_PATHS):
        if config_path.is_file():
            try:
                data = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k, v in data.items():
                        if v:  # skip empty values
                            merged[k] = str(v)
            except Exception:
                pass

    _cached_config = merged
    return merged


def save_config(config: dict, path: Path) -> None:
    """Save config dict to a JSON file.

    형제 ``save_model_entry`` 와 동형으로 저장 후 캐시를 무효화한다 — 같은
    프로세스의 다음 ``load_config()`` 가 방금 쓴 값을 보게 (종전엔 스테일
    캐시 잔존 — 리뷰 §4.5)."""
    atomic_write_json(path, config, indent=2)  # 상태 파일 — fsio
    reload_config()


def save_config_value(key: str, value: Any) -> bool:
    """유저 ``config.json`` 의 키 하나만 갱신한다 (없으면 만든다).

    모델 검증이 대화형으로 고른 이름을 그 자리에서 저장하는 용도 — 사용자가
    같은 것을 두 번 고치게 하지 않는다. 다른 키는 읽어서 그대로 되쓰므로
    수동 편집분이 날아가지 않는다. 실패는 예외가 아니라 False (저장 못 해도
    이번 실행은 계속 굴러야 한다)."""
    path = _CONFIG_PATHS[0]
    try:
        current: dict[str, Any] = {}
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                current = loaded
        current[key] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        save_config(current, path)
        return True
    except (OSError, json.JSONDecodeError):
        return False


def has_config() -> bool:
    """Check if any config file exists or env vars are set."""
    for config_path in _CONFIG_PATHS:
        if config_path.is_file():
            return True
    for env_key in _ENV_MAP:
        if os.environ.get(env_key, ""):
            return True
    return False


def reload_config() -> None:
    """Force reload config (for testing)."""
    global _cached_config
    _cached_config = None
