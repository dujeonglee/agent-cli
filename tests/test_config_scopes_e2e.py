"""설정 스코프 정리 실장 검증 (v9.0.0, docs/config-scopes).

인프로세스 테스트(test_paths.py)는 모듈 상수를 몽키패치한다 — 상수가 import
시점 ``Path.cwd()``/``Path.home()`` 으로 고정되기 때문이다. 그래서 **실제
배선**(어느 모듈이 어느 경로를 import 시점에 잡는가)은 새 프로세스에서만
검증된다. 여기서는 가짜 HOME 과 프로젝트 디렉토리를 만들고 서브프로세스가
각 로더로 무엇을 보는지 JSON 으로 보고하게 한다.

유령(ghost) = 유저 전역에만 있는 것 → 보이면 안 됨.
실물(real)  = 프로젝트에 있는 것       → 보여야 함.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_PROBE = r"""
import json, os
from pathlib import Path
out = {}

from agent_cli.paths import project_dir, user_dir, sessions_dir
out["project_dir"] = str(project_dir())
out["user_dir"] = str(user_dir())

from agent_cli.mcp.config import load_mcp_config
out["mcp"] = sorted(load_mcp_config())

from agent_cli.prompts.system_prompt import _load_directives
out["directive"] = _load_directives()

from agent_cli.hooks.shell import load_hooks
out["hooks_events"] = sorted(load_hooks(use_cache=False))

from agent_cli.hooks.loader import _hook_dirs
out["hook_dirs"] = [str(p) for p in _hook_dirs()]

from agent_cli.skills.loader import _loader as skills_loader
out["skills"] = sorted(n for n in skills_loader.list_names() if n in ("ghost", "real"))

from agent_cli.subagent.profiles import _profile_loader
out["agents"] = sorted(n for n in _profile_loader.list_names() if n in ("ghost", "real"))

from agent_cli.config import load_config, _CONFIG_PATHS, _SEARCH_PATHS
out["config_paths"] = [str(p) for p in _CONFIG_PATHS]
out["models_paths"] = [str(p) for p in _SEARCH_PATHS]
out["default_model"] = load_config(use_cache=False).get("default_model", "")

import agent_cli.input_history as ih
out["chat_history"] = str(ih._HISTORY_FILE)

print(json.dumps(out))
"""


def _md(name: str) -> str:
    return f"---\nname: {name}\ndescription: {name}\n---\n{name} body\n"


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    h = home / ".agent-cli"
    p = proj / ".agent-cli"
    for d in (h / "skills", h / "agents", h / "hooks", p / "skills", p / "agents"):
        d.mkdir(parents=True)

    # ── 유저 전역: 전부 유령이어야 하는 것 (config/models 제외) ──
    (h / "mcp.json").write_text(json.dumps({"mcpServers": {"ghost": {"command": "x"}}}))
    (h / "DIRECTIVE.md").write_text("GHOST DIRECTIVE")
    (h / "hooks.json").write_text(
        json.dumps({"PreToolUse": [{"matcher": "", "hooks": [{"command": "exit 2"}]}]})
    )
    (h / "skills" / "ghost.md").write_text(_md("ghost"))
    (h / "agents" / "ghost.md").write_text(_md("ghost"))
    # 유저 config 는 **읽혀야** 하는 것
    (h / "config.json").write_text(json.dumps({"default_model": "USER-MODEL"}))

    # ── 프로젝트: 실물 ──
    (p / "mcp.json").write_text(json.dumps({"mcpServers": {"real": {"command": "y"}}}))
    (p / "DIRECTIVE.md").write_text("REAL DIRECTIVE")
    (p / "skills" / "real.md").write_text(_md("real"))
    (p / "agents" / "real.md").write_text(_md("real"))
    # 프로젝트 config 는 **무시되어야** 하는 것
    (p / "config.json").write_text(json.dumps({"default_model": "PROJECT-MODEL"}))
    return home, proj


def _probe(home: Path, cwd: Path, extra_env: dict | None = None) -> dict:
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    for k in ("AGENT_CLI_MODEL", "AGENT_CLI_PROVIDER", "AGENT_CLI_SESSIONS_DIR"):
        env.pop(k, None)
    env.update(extra_env or {})
    r = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestFreshProcessWiring:
    def test_scope_dirs_resolve_from_real_home_and_cwd(self, tmp_path):
        home, proj = _seed(tmp_path)
        got = _probe(home, proj)
        assert got["project_dir"] == str(proj / ".agent-cli")
        assert got["user_dir"] == str(home / ".agent-cli")

    def test_project_scope_items_ignore_user_global(self, tmp_path):
        """mcp · DIRECTIVE · hooks · skills · agents — 유령은 하나도 안 보인다."""
        home, proj = _seed(tmp_path)
        got = _probe(home, proj)
        assert got["mcp"] == ["real"]
        assert "REAL DIRECTIVE" in got["directive"]
        assert "GHOST DIRECTIVE" not in got["directive"]
        assert got["hooks_events"] == []  # 홈의 차단 훅 미로드
        assert got["hook_dirs"] == [str(proj / ".agent-cli" / "hooks")]
        assert got["skills"] == ["real"]
        assert got["agents"] == ["real"]

    def test_user_scope_items_ignore_project(self, tmp_path):
        """config · models — 프로젝트 파일이 있어도 유저 것만."""
        home, proj = _seed(tmp_path)
        got = _probe(home, proj)
        assert got["config_paths"] == [str(home / ".agent-cli" / "config.json")]
        assert got["default_model"] == "USER-MODEL"  # PROJECT-MODEL 무시
        assert got["models_paths"][0] == str(home / ".agent-cli" / "models.json")

    def test_chat_history_default_and_env(self, tmp_path):
        home, proj = _seed(tmp_path)
        got = _probe(home, proj)
        assert got["chat_history"] == str(
            Path(".agent-cli") / "sessions" / "chat_history"
        )
        moved = _probe(home, proj, {"AGENT_CLI_SESSIONS_DIR": str(tmp_path / "out")})
        assert moved["chat_history"] == str(tmp_path / "out" / "chat_history")

    def test_running_from_subdirectory_sees_nothing(self, tmp_path):
        """알고 받아들인 손실(docs/config-scopes §3): 프로젝트는 git 루트가
        아니라 cwd 기준이다. 하위 디렉토리에서 실행하면 프로젝트 설정이
        빈껍데기다 — 이게 바뀌면(루트 탐색 추가 등) 여기서 드러난다."""
        home, proj = _seed(tmp_path)
        sub = proj / "src" / "deep"
        sub.mkdir(parents=True)
        got = _probe(home, sub)
        assert got["mcp"] == []
        assert got["directive"] == ""
        assert got["skills"] == []
        # 유저 스코프는 cwd 와 무관하게 그대로
        assert got["default_model"] == "USER-MODEL"

    def test_cli_still_boots(self, tmp_path):
        """import-time 배선을 건드렸으니 진짜 진입점이 뜨는지."""
        home, proj = _seed(tmp_path)
        env = {**os.environ, "HOME": str(home)}
        r = subprocess.run(
            [sys.executable, "-m", "agent_cli", "--version"],
            cwd=proj,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert r.returncode == 0, r.stderr
        assert "agent-cli" in r.stdout
