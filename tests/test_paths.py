"""`.agent-cli` 경로쌍 단일화 (agent_cli/paths.py, v8.40.0 — 리뷰 §4.5).

등가성 계약: 7개 소비 모듈의 경로 상수가 종전 손-나열 값과 **순서 포함
동일** (HEAD 표현식 재구성 대조 — 릴리스 시 하네스로도 검증). 유일한
의도 변경은 hooks.json 병합 규칙(first-found → 둘 다 발화, 사용자 결정)
이며 그 계약은 test_hooks.py::TestLoadHooksMergesBothScopes 가 고정.
"""

from __future__ import annotations

from pathlib import Path

from agent_cli.paths import scoped_paths, sessions_dir

_A = ".agent-cli"


class TestScopedPaths:
    def test_pair_structure(self):
        """[프로젝트, 사용자] 순 = 우선순위 순 (프로젝트 승) — 단일 계약."""
        assert scoped_paths("x.json") == [
            Path.cwd() / _A / "x.json",
            Path.home() / _A / "x.json",
        ]

    def test_multi_part(self):
        assert scoped_paths("a", "b.md") == [
            Path.cwd() / _A / "a" / "b.md",
            Path.home() / _A / "a" / "b.md",
        ]

    def test_no_args_gives_base_dirs(self):
        """인자 없음 = 베이스 디렉토리 쌍 (DIRECTIVE.md 소비자용)."""
        assert scoped_paths() == [Path.cwd() / _A, Path.home() / _A]


class TestSiteEquivalence:
    """7개 소비 모듈의 상수 == 종전(v8.39.0 HEAD) 손-나열 값 (순서 포함).

    mcp 는 소비자가 정순 순회 later-wins 라 종전의 [사용자, 프로젝트]
    역순이 그대로 보존돼야 한다 — reversed(scoped_paths(...)) 파생 핀.
    """

    def test_config_models_search_paths(self):
        from agent_cli import config

        assert config._SEARCH_PATHS == [
            Path.cwd() / _A / "models.json",
            Path.home() / _A / "models.json",
            Path(config.__file__).parent / "default_models.json",
        ]

    def test_config_paths(self):
        from agent_cli import config

        assert config._CONFIG_PATHS == [
            Path.cwd() / _A / "config.json",
            Path.home() / _A / "config.json",
        ]

    def test_mcp_paths_keep_reversed_consumption_order(self):
        import agent_cli.mcp.config as mcp_config

        assert mcp_config._MCP_CONFIG_PATHS == [
            Path.home() / _A / "mcp.json",  # 정순 순회 + later-wins 소비
            Path.cwd() / _A / "mcp.json",
        ]

    def test_hooks_json_paths(self):
        import agent_cli.hooks.shell as hooks_shell

        assert hooks_shell._HOOKS_PATHS == [
            Path.cwd() / _A / "hooks.json",
            Path.home() / _A / "hooks.json",
        ]

    def test_hook_dirs(self):
        import agent_cli.hooks.loader as hooks_loader

        assert hooks_loader._hook_dirs() == [
            Path.cwd() / _A / "hooks",
            Path.home() / _A / "hooks",
        ]

    def test_skills_search_paths(self):
        import agent_cli.skills.loader as skills_loader

        assert skills_loader._SEARCH_PATHS == [
            Path.cwd() / _A / "skills",
            Path.home() / _A / "skills",
            Path(skills_loader.__file__).parent / "builtin",
        ]

    def test_profile_search_paths(self):
        from agent_cli.subagent import profiles

        assert profiles._PROFILE_SEARCH_PATHS == [
            Path.cwd() / _A / "agents",
            Path.home() / _A / "agents",
            Path(profiles.__file__).parent.parent / "agents" / "builtin",
        ]

    def test_directive_paths(self):
        import agent_cli.prompts.system_prompt as sysprompt

        assert sysprompt._DIRECTIVE_PATHS == [Path.cwd() / _A, Path.home() / _A]


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
