"""`agent_cli_harbor.AgentCli` 의 플래그 표 — 어댑터가 제품에 없는 조건을 몰래 걸지 않는다.

harbor 는 벤치 전용 의존성이라(uvx 격리) 없으면 건너뛴다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("harbor.agents.installed.base")

sys.path.insert(0, str(Path(__file__).parent))

import agent_cli_harbor


def _agent(tmp_path: Path, **kwargs):
    wheel = tmp_path / "agent_cli-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    return agent_cli_harbor.AgentCli(
        logs_dir=tmp_path,
        model_name="openai/m",
        wheel=str(wheel),
        models_json="",
        host_config=str(tmp_path / "no-config.json"),
        **kwargs,
    )


def test_no_turn_cap_unless_asked(tmp_path):
    """기본으로는 ``--max-turns`` 가 나가지 않는다 — agent-cli 의 기본(상한 없음)으로 돈다."""
    assert "--max-turns" not in _agent(tmp_path).build_cli_flags()


def test_turn_cap_is_passed_when_given(tmp_path):
    assert "--max-turns 60" in _agent(tmp_path, max_turns=60).build_cli_flags()
