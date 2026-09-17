"""`.agent-cli` 경로의 단일 소스 (agent_cli/paths.py) — **설정의 집은 하나**
(v9.0.0, docs/config-scopes).

종전(v8.40.0)엔 ``scoped_paths()`` 가 [프로젝트, 사용자] 쌍을 돌려주고 7개
모듈이 각자 병합했다. v9.0.0 부터 각 설정은 정확히 한 스코프에 살고 쌍을
원하는 소비자가 없어 ``scoped_paths`` 는 은퇴했다. 여기서는 **각 소비
모듈의 상수가 단일 스코프**임을 고정한다 — 두 번째 경로가 슬쩍 되살아나면
"어느 파일이 이겼나"라는 종전의 혼란이 돌아온다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.paths import project_dir, sessions_dir, user_dir

_A = ".agent-cli"


class TestScopeDirs:
    def test_project_dir_is_cwd(self):
        assert project_dir() == Path.cwd() / _A

    def test_user_dir_is_home(self):
        assert user_dir() == Path.home() / _A

    def test_scoped_paths_is_retired(self):
        """쌍을 돌려주는 API 가 남아 있으면 누군가 다시 쓴다."""
        from agent_cli import paths

        assert not hasattr(paths, "scoped_paths")


class TestSingleScopePins:
    """소비 모듈 10곳 — 각각 **정확히 하나**의 스코프.

    유저: config.json · models.json (머신 사실)
    프로젝트: mcp · skills · agents · hooks · hooks.json · DIRECTIVE
    sessions_dir(): chat_history
    """

    # ── 유저 스코프 ──
    def test_config_is_user_only(self):
        from agent_cli import config

        assert config._CONFIG_PATHS == [Path.home() / _A / "config.json"]

    def test_models_is_user_only_plus_builtin(self):
        from agent_cli import config

        assert config._SEARCH_PATHS == [
            Path.home() / _A / "models.json",
            Path(config.__file__).parent / "default_models.json",
        ]
        # 자동 저장 대상 == 탐색 경로 (한 파일)
        assert config._GLOBAL_MODELS_PATH == config._SEARCH_PATHS[0]

    # ── 프로젝트 스코프 ──
    def test_mcp_is_project_only(self):
        import agent_cli.mcp.config as mcp_config

        assert mcp_config._MCP_CONFIG_PATHS == [Path.cwd() / _A / "mcp.json"]

    def test_hooks_json_is_project_only(self):
        import agent_cli.hooks.shell as hooks_shell

        assert hooks_shell._HOOKS_PATHS == [Path.cwd() / _A / "hooks.json"]

    def test_hook_dirs_is_project_only(self):
        import agent_cli.hooks.loader as hooks_loader

        assert hooks_loader._hook_dirs() == [Path.cwd() / _A / "hooks"]

    def test_skills_is_project_plus_builtin(self):
        import agent_cli.skills.loader as skills_loader

        assert skills_loader._SEARCH_PATHS == [
            Path.cwd() / _A / "skills",
            Path(skills_loader.__file__).parent / "builtin",
        ]

    def test_agents_is_project_plus_builtin(self):
        from agent_cli.subagent import profiles

        assert profiles._PROFILE_SEARCH_PATHS == [
            Path.cwd() / _A / "agents",
            Path(profiles.__file__).parent.parent / "agents" / "builtin",
        ]

    def test_directive_is_project_only(self):
        import agent_cli.prompts.system_prompt as sysprompt

        assert sysprompt._DIRECTIVE_PATHS == [Path.cwd() / _A]
        assert sysprompt.project_directive_file() == Path.cwd() / _A / "DIRECTIVE.md"

    # ── sessions_dir 스코프 ──
    def test_chat_history_lives_in_sessions_dir(self):
        """작업 트리(.agent-cli/)가 아니라 sessions_dir() — 타이핑한 내용이
        트리에 남지 않고, AGENT_CLI_SESSIONS_DIR 로 컨테이너에서 뺄 수 있다."""
        import agent_cli.input_history as ih

        assert ih._HISTORY_FILE == Path(_A) / "sessions" / "chat_history"

    @pytest.mark.parametrize("name", ["home", "Path.home"])
    def test_no_consumer_reaches_for_home_except_user_scope(self, name):
        """유저 스코프 모듈(config) 외엔 홈 디렉토리를 직접 조립하지 않는다 —
        전역 경로가 은근슬쩍 돌아오는 걸 막는 소스 가드."""
        import re

        allowed = {"agent_cli/paths.py", "agent_cli/config.py"}
        offenders = []
        for f in Path("agent_cli").rglob("*.py"):
            rel = str(f)
            if rel in allowed:
                continue
            src = f.read_text(encoding="utf-8")
            if re.search(r"Path\.home\(\)\s*/\s*[\"']\.agent-cli", src):
                offenders.append(rel)
        assert offenders == [], offenders


class TestSessionsDir:
    """세션 루트 단일 소스 (v8.50.0): 기본은 종전과 동일한 cwd 상대
    `.agent-cli/sessions`, `AGENT_CLI_SESSIONS_DIR` 로 통째 이전."""

    def test_default_is_relative_dot_agent_cli_sessions(self, monkeypatch):
        monkeypatch.delenv("AGENT_CLI_SESSIONS_DIR", raising=False)
        assert sessions_dir() == Path(_A) / "sessions"
        assert not sessions_dir().is_absolute()  # 소비자 기록 경로 형태 보존

    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CLI_SESSIONS_DIR", str(tmp_path / "s"))
        assert sessions_dir() == tmp_path / "s"

    def test_env_override_expands_home(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_SESSIONS_DIR", "~/x/sessions")
        assert sessions_dir() == Path.home() / "x" / "sessions"

    def test_empty_env_means_default(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_SESSIONS_DIR", "")
        assert sessions_dir() == Path(_A) / "sessions"

    def test_consumers_pin_the_same_root(self):
        """종전 3곳의 손-조립 리터럴과 등가 — session/tools.context 상수가
        같은 함수에서 파생 (main 의 web 인스턴스 파일은 get_session_dir 경유)."""
        import agent_cli.context.session as session_mod
        import agent_cli.tools.context as ctx_mod

        assert session_mod._SESSIONS_DIR == Path(_A) / "sessions"
        assert ctx_mod._SESSIONS_DIR == Path(_A) / "sessions"

    def test_env_redirects_session_writes(self, monkeypatch, tmp_path):
        """env 로 옮긴 루트에 실제 세션 파일이 떨어지고 작업 트리엔 남지 않음
        (모듈 상수는 import 고정이라 여기선 상수를 함수값으로 재바인딩)."""
        import agent_cli.context.session as session_mod

        monkeypatch.setenv("AGENT_CLI_SESSIONS_DIR", str(tmp_path / "elsewhere"))
        monkeypatch.setattr(session_mod, "_SESSIONS_DIR", sessions_dir())
        meta = session_mod.create_session(str(tmp_path))
        session_mod.save_meta(meta)
        assert (tmp_path / "elsewhere" / meta.session_id / "session.jsonl").is_file()
        assert (
            session_mod.get_session_dir(meta)
            == tmp_path / "elsewhere" / meta.session_id
        )


class TestUserScopeFilesAreIgnoredAtRuntime:
    """상수 핀(TestSingleScopePins)만으론 부족하다 — 상수가 맞아도 로더가
    홈을 따로 뒤지면 전역이 되살아난다. 여기서는 **실제로 홈에 파일을 두고
    무시되는지**를 각 로더로 확인한다. 실장 검증(서브프로세스)은
    tests/test_config_scopes_e2e.py."""

    def _home_and_project(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        proj = tmp_path / "proj"
        (home / ".agent-cli").mkdir(parents=True)
        (proj / ".agent-cli").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.chdir(proj)
        return home, proj

    def test_user_mcp_json_ignored(self, tmp_path, monkeypatch):
        import json

        from agent_cli.mcp.config import load_mcp_config

        home, proj = self._home_and_project(tmp_path, monkeypatch)
        (home / ".agent-cli" / "mcp.json").write_text(
            json.dumps({"mcpServers": {"ghost": {"command": "x"}}})
        )
        (proj / ".agent-cli" / "mcp.json").write_text(
            json.dumps({"mcpServers": {"real": {"command": "y"}}})
        )
        # 상수는 import 시점 cwd 고정이라 명시 경로로 프로젝트만 넘긴다 —
        # 제품 배선이 그렇게 한다(test_mcp_is_project_only).
        got = load_mcp_config(search_paths=[proj / ".agent-cli" / "mcp.json"])
        assert set(got) == {"real"}

    def test_user_directive_ignored(self, tmp_path, monkeypatch):
        from agent_cli.prompts import system_prompt as sp

        home, proj = self._home_and_project(tmp_path, monkeypatch)
        (home / ".agent-cli" / "DIRECTIVE.md").write_text("GHOST RULE")
        (proj / ".agent-cli" / "DIRECTIVE.md").write_text("REAL RULE")
        monkeypatch.setattr(sp, "_DIRECTIVE_PATHS", [proj / ".agent-cli"])
        out = sp._load_directives()
        assert "REAL RULE" in out and "GHOST RULE" not in out

    def test_user_hooks_json_ignored(self, tmp_path, monkeypatch):
        import json

        import agent_cli.hooks.shell as hs

        home, proj = self._home_and_project(tmp_path, monkeypatch)
        (home / ".agent-cli" / "hooks.json").write_text(
            json.dumps(
                {"PreToolUse": [{"matcher": "", "hooks": [{"command": "exit 2"}]}]}
            )
        )
        monkeypatch.setattr(hs, "_HOOKS_PATHS", [proj / ".agent-cli" / "hooks.json"])
        cfg = hs.load_hooks(use_cache=False)
        assert cfg == {}  # 홈의 차단 훅은 로드되지 않았다

    def test_project_config_json_ignored(self, tmp_path, monkeypatch):
        import json

        from agent_cli import config as cfgmod

        home, proj = self._home_and_project(tmp_path, monkeypatch)
        (proj / ".agent-cli" / "config.json").write_text(
            json.dumps({"default_model": "PROJECT-MODEL"})
        )
        (home / ".agent-cli" / "config.json").write_text(
            json.dumps({"default_model": "USER-MODEL"})
        )
        monkeypatch.setattr(
            cfgmod, "_CONFIG_PATHS", [home / ".agent-cli" / "config.json"]
        )
        for k in ("AGENT_CLI_MODEL", "AGENT_CLI_PROVIDER"):
            monkeypatch.delenv(k, raising=False)
        assert cfgmod.load_config(use_cache=False)["default_model"] == "USER-MODEL"


class TestChatHistoryFollowsSessionsDir:
    """chat_history 는 sessions_dir() 를 따른다 — 작업 트리가 아니라
    AGENT_CLI_SESSIONS_DIR 로 옮길 수 있는 곳 (docs/config-scopes §4)."""

    def test_default_under_dot_agent_cli_sessions(self, monkeypatch):
        monkeypatch.delenv("AGENT_CLI_SESSIONS_DIR", raising=False)
        assert sessions_dir() / "chat_history" == Path(_A) / "sessions" / "chat_history"

    def test_env_moves_history_out_of_tree(self, monkeypatch, tmp_path):
        """컨테이너·CI 에서 트리를 안 더럽히는 게 이 위치 선택의 이유."""
        monkeypatch.setenv("AGENT_CLI_SESSIONS_DIR", str(tmp_path / "out"))
        assert sessions_dir() / "chat_history" == tmp_path / "out" / "chat_history"
        assert not str(sessions_dir()).startswith(_A)

    def test_history_module_pins_sessions_dir(self):
        """input_history 가 자기 경로를 따로 조립하면 이 계약이 깨진다."""
        import agent_cli.input_history as ih

        assert ih._HISTORY_FILE.parent == sessions_dir()
        assert ih._HISTORY_FILE.name == "chat_history"
