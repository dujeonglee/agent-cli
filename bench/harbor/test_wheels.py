"""bench/harbor/wheels.py — 최신 wheel 은 사전순이 아니라 버전 숫자로 고른다."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import wheels


def test_ten_beats_nine(tmp_path):
    # 사전순이면 9.24.0 > 10.27.0 — 실제로 벤치 컨테이너에 v9.24.0 이 설치됐다.
    for n in ("agent_cli-9.24.0", "agent_cli-10.27.0", "agent_cli-9.23.1"):
        (tmp_path / f"{n}-py3-none-any.whl").write_bytes(b"")
    assert wheels.newest_wheel(tmp_path).name == "agent_cli-10.27.0-py3-none-any.whl"


def test_prerelease_sorts_before_final():
    assert wheels.wheel_version(
        "agent_cli-10.27.0rc1-py3-none-any.whl"
    ) < wheels.wheel_version("agent_cli-10.27.0-py3-none-any.whl")
    assert wheels.wheel_version("other-1.0-py3-none-any.whl") is None


def test_no_wheel_is_loud(tmp_path):
    with pytest.raises(FileNotFoundError, match="build --wheel"):
        wheels.newest_wheel(tmp_path)
