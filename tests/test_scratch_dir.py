"""모델의 임시 디렉토리 `.agent-cli/tmp` 는 세션과 함께 생긴다 (v10.90.0).

GLM 벤치 27 트라이얼 중 5 에서 모델이 프롬프트의 "없으면 만들라" 를 잊은 채
그 아래에 쓰다 `Directory nonexistent` 로 한 턴씩 잃었다(`savefig` 실패 두 건
포함). 프롬프트가 가리키는 곳은 하니스가 미리 만든다 — run·web 의 단일 조립
경로 `main._build_context` 에서.
"""

from __future__ import annotations

from unittest.mock import patch

from agent_cli.context.session import create_session
from agent_cli.main import _build_context
from agent_cli.paths import scratch_dir


def test_build_context_creates_the_scratch_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "agent_cli.context.session._SESSIONS_DIR", tmp_path / "sessions"
    )
    assert not (tmp_path / scratch_dir()).exists()
    with patch("agent_cli.main.ContextManager") as cm:
        _build_context(create_session(), binding=object())
    assert cm.called
    assert (tmp_path / ".agent-cli" / "tmp").is_dir()


def test_build_context_tolerates_an_existing_scratch_dir(tmp_path, monkeypatch):
    """두 번째 세션(resume·재시작)에서도 같은 자리 — 있으면 그대로 둔다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "agent_cli.context.session._SESSIONS_DIR", tmp_path / "sessions"
    )
    (tmp_path / ".agent-cli" / "tmp").mkdir(parents=True)
    (tmp_path / ".agent-cli" / "tmp" / "keep.txt").write_text("x")
    with patch("agent_cli.main.ContextManager"):
        _build_context(create_session(), binding=object())
    assert (tmp_path / ".agent-cli" / "tmp" / "keep.txt").read_text() == "x"
