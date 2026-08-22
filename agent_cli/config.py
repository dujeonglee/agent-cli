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


# Search order: project local > user global > package defaults
# (경로쌍은 scoped_paths 단일 소스 — v8.40.0, 리뷰 §4.5)
from agent_cli.paths import scoped_paths

_SEARCH_PATHS = [
    *scoped_paths("models.json"),
    Path(__file__).parent / "default_models.json",
]

# Auto-save target: always user global
_GLOBAL_MODELS_PATH = Path.home() / ".agent-cli" / "models.json"

_cached_registry: dict[str, Any] | None = None

# Hardcoded fallbacks
_PROVIDER_FALLBACKS = {
    "anthropic": ("https://api.anthropic.com/v1", "claude-sonnet-4-20250514"),
    "openai": ("https://api.openai.com/v1", "gpt-4o"),
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

    fb_url, fb_model = _PROVIDER_FALLBACKS.get(
        provider, ("http://127.0.0.1:8000/v1", "")
    )

    return ProviderDefaults(
        base_url=entry.get("base_url", fb_url),
        default_model=entry.get("default_model", fb_model),
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

# Config search order: workspace (highest) > user > env (lowest)
_CONFIG_PATHS = scoped_paths("config.json")

# Environment variable → config key mapping
_ENV_MAP = {
    "AGENT_CLI_PROVIDER": "provider",
    "AGENT_CLI_BASE_URL": "base_url",
    "AGENT_CLI_API_KEY": "api_key",
    "AGENT_CLI_MODEL": "default_model",
}

_cached_config: dict[str, str] | None = None


def load_config(use_cache: bool = True) -> dict[str, str]:
    """Load config by merging: env vars → user config → workspace config.

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
