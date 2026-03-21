"""Configuration loading: models.json registry + provider defaults.

Search paths (project local takes priority):
  1. .agent-cli/models.json  (project local, read-only)
  2. ~/.agent-cli/models.json (user global, auto-save target)
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ProviderDefaults:
    base_url: str
    default_model: str


# Search order: project local (priority), then user global
_SEARCH_PATHS = [
    Path.cwd() / ".agent-cli" / "models.json",
    Path.home() / ".agent-cli" / "models.json",
]

# Auto-save target: always user global
_GLOBAL_MODELS_PATH = Path.home() / ".agent-cli" / "models.json"

_cached_registry: dict[str, Any] | None = None

# Hardcoded fallbacks
_PROVIDER_FALLBACKS = {
    "anthropic": ("https://api.anthropic.com/v1", "claude-sonnet-4-20250514"),
    "openai": ("https://api.openai.com/v1", "gpt-4o"),
    "ollama": ("http://localhost:11434", "qwen3:32b"),
}


def _find_models_json() -> Path | None:
    for p in _SEARCH_PATHS:
        if p.is_file():
            return p
    return None


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
            except Exception:
                pass

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

    fb_url, fb_model = _PROVIDER_FALLBACKS.get(provider, ("http://localhost:11434", ""))

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

    # Don't overwrite existing entries
    if model in existing.get("models", {}):
        return False

    # Add new model
    existing.setdefault("models", {})[model] = entry

    # Save
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
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
