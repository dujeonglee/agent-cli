"""Shared test fixtures and configuration."""

from __future__ import annotations

import pytest

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
    try:
        from agent_cli.tools.delegate import _reset_agent_loader

        _reset_agent_loader()
    except Exception:
        pass
