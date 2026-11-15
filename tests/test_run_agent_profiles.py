"""run 엔진의 profile 파라미터 계약 — 구 delegate agent 파라미터 (AG-01 ~ AG-22)."""

from __future__ import annotations

from agent_cli.subagent.profiles import _PROFILE_NAME_PATTERN, load_profile
from agent_cli.tools.result import ToolResult
from tests.binding_support import make_binding
from tests.loop_kinds import make_caller, run_task


def _validate_agent_name(name: str) -> bool:
    # 5.0.0: 검증은 load_profile 내부의 이름 패턴으로 병합 — 기존 AG-01~05
    # 계약을 패턴에 대해 유지한다.
    return bool(_PROFILE_NAME_PATTERN.match(name))


# ── AG-01 ~ AG-05: Agent name validation ──────────────────


def _set_agent_paths(paths):
    """C2: prod 의 테스트 전용 mutator(_reset_agent_loader) 삭제 대체 —
    agents 모듈의 로더 전역을 직접 교체한다."""
    import agent_cli.subagent.profiles as _profiles_mod
    from agent_cli.resource_loader import ResourceLoader

    _profiles_mod._profile_loader = ResourceLoader(list(paths))


class TestValidateAgentName:
    def test_valid_names(self):
        """AG-01: Valid names with alphanumeric, hyphens, underscores."""
        assert _validate_agent_name("reviewer") is True
        assert _validate_agent_name("security-reviewer") is True
        assert _validate_agent_name("my_agent_01") is True

    def test_path_traversal(self):
        """AG-02: Path traversal attempts rejected."""
        assert _validate_agent_name("../etc/passwd") is False
        assert _validate_agent_name("../../secret") is False

    def test_slash(self):
        """AG-03: Slashes rejected."""
        assert _validate_agent_name("foo/bar") is False
        assert _validate_agent_name("/absolute") is False

    def test_special_chars(self):
        """AG-04: Special characters rejected."""
        assert _validate_agent_name("agent name") is False
        assert _validate_agent_name("agent;rm") is False
        assert _validate_agent_name("agent.md") is False

    def test_empty(self):
        """AG-05: Empty string rejected."""
        assert _validate_agent_name("") is False


# ── AG-06 ~ AG-14: Agent file loading ─────────────────────


class TestLoadAgent:
    def test_body_only(self, tmp_path, monkeypatch):
        """AG-06: File without frontmatter uses entire content as role prompt."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "reviewer.md").write_text(
            "You are a code reviewer.\nCheck for bugs."
        )

        _set_agent_paths([agents_dir])

        role, config, error = load_profile("reviewer")

        assert error is None
        assert role == "You are a code reviewer.\nCheck for bugs."
        assert config == {}

    def test_with_frontmatter(self, tmp_path, monkeypatch):
        """AG-07: File with frontmatter parses config and body separately."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "secure.md").write_text(
            "---\nallowed-tools:\n  - read_file\n  - shell\nmodel: test-model\n---\n\nYou are a security reviewer."
        )

        _set_agent_paths([agents_dir])

        role, config, error = load_profile("secure")

        assert error is None
        assert role == "You are a security reviewer."
        assert config["allowed-tools"] == ["read_file", "shell"]
        assert config["model"] == "test-model"

    def test_frontmatter_parse_error(self, tmp_path, monkeypatch):
        """AG-08: Invalid YAML frontmatter falls back to full text as body."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        content = "---\n: invalid: yaml: [broken\n---\n\nYou are a reviewer."
        (agents_dir / "broken.md").write_text(content)

        _set_agent_paths([agents_dir])

        role, config, error = load_profile("broken")

        assert error is None
        assert config == {}
        assert "reviewer" in role

    def test_not_found(self, tmp_path, monkeypatch):
        """AG-09: Non-existent agent returns error."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)

        _set_agent_paths([agents_dir])

        role, config, error = load_profile("nonexistent")

        assert role is None
        assert config == {}
        assert "not found" in error

    def test_invalid_name(self):
        """AG-10: Invalid agent name returns error."""
        role, config, error = load_profile("../hack")

        assert role is None
        assert config == {}
        assert "Invalid profile name" in error

    def test_empty_body(self, tmp_path, monkeypatch):
        """AG-11: File with frontmatter but no body returns error."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "empty.md").write_text("---\nmodel: test\n---\n\n")

        _set_agent_paths([agents_dir])

        role, _config, error = load_profile("empty")

        assert role is None
        assert error is not None  # empty body → not found

    def test_project_overrides_global(self, tmp_path, monkeypatch):
        """AG-12: Project-local agent takes priority over user-global."""
        project_dir = tmp_path / "project" / ".agent-cli" / "agents"
        global_dir = tmp_path / "global" / ".agent-cli" / "agents"
        project_dir.mkdir(parents=True)
        global_dir.mkdir(parents=True)

        (project_dir / "reviewer.md").write_text("Project reviewer")
        (global_dir / "reviewer.md").write_text("Global reviewer")

        _set_agent_paths([project_dir, global_dir])

        role, _config, error = load_profile("reviewer")

        assert error is None
        assert role == "Project reviewer"

    def test_falls_back_to_global(self, tmp_path, monkeypatch):
        """AG-13: Falls back to global when not in project."""
        project_dir = tmp_path / "project" / ".agent-cli" / "agents"
        global_dir = tmp_path / "global" / ".agent-cli" / "agents"
        project_dir.mkdir(parents=True)
        global_dir.mkdir(parents=True)

        (global_dir / "reviewer.md").write_text("Global reviewer")

        _set_agent_paths([project_dir, global_dir])

        role, _config, error = load_profile("reviewer")

        assert error is None
        assert role == "Global reviewer"

    def test_unknown_frontmatter_fields_ignored(self, tmp_path, monkeypatch):
        """AG-14: Claude Code compat: unknown frontmatter fields are ignored."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "compat.md").write_text(
            "---\nallowed-tools:\n  - read_file\nunknown-field: value\nanother: 123\n---\n\nYou are an agent."
        )

        _set_agent_paths([agents_dir])

        role, config, error = load_profile("compat")

        assert error is None
        assert role == "You are an agent."
        assert config["allowed-tools"] == ["read_file"]
        # Unknown fields are in config dict but not causing errors
        assert config["unknown-field"] == "value"


# ── AG-15 ~ AG-22: Delegate execution with agent ──────────


class TestRunSingleWithAgent:
    def test_with_agent_injects_role(self, tmp_path, monkeypatch):
        """AG-15: Agent name triggers role prompt injection into run_loop."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "tester.md").write_text("You are a test engineer.")

        _set_agent_paths([agents_dir])

        captured_kwargs = {}

        def mock_run_loop(kind, start, ctx):
            captured_kwargs["kind"] = kind
            from agent_cli.tools.result import ToolResult

            return ToolResult(True, output="done")

        monkeypatch.setattr("agent_cli.loop.run.run_loop", mock_run_loop)

        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
        )

        class FakeProvider:
            pass

        result = run_task(
            {"task": "Write tests", "agent": "tester"},
            make_caller(
                binding=make_binding(
                    provider=FakeProvider(), model="test", capabilities=caps
                )
            ),
        )

        assert result.success
        assert captured_kwargs["kind"].agent_role == "You are a test engineer."

    def test_compaction_disabled_propagates_to_run_loop(self, monkeypatch):
        """Parent's compaction_enabled=False threads into the delegate's
        run_loop (the internal flag flowing to subagents)."""
        captured_kwargs = {}

        def mock_run_loop(kind, start, ctx):
            captured_kwargs["kind"] = kind
            from agent_cli.tools.result import ToolResult

            return ToolResult(True, output="done")

        monkeypatch.setattr("agent_cli.loop.run.run_loop", mock_run_loop)

        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
        )

        class FakeProvider:
            pass

        run_task(
            {"task": "Do work"},
            make_caller(
                binding=make_binding(
                    provider=FakeProvider(), model="test", capabilities=caps
                ),
                compaction_enabled=False,
            ),
        )
        assert captured_kwargs["kind"].compaction_enabled is False

    def test_compaction_defaults_enabled(self, monkeypatch):
        """Backward-compat: omitting compaction_enabled keeps compaction on."""
        captured_kwargs = {}

        def mock_run_loop(kind, start, ctx):
            captured_kwargs["kind"] = kind
            from agent_cli.tools.result import ToolResult

            return ToolResult(True, output="done")

        monkeypatch.setattr("agent_cli.loop.run.run_loop", mock_run_loop)

        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
        )

        class FakeProvider:
            pass

        run_task(
            {"task": "Do work"},
            make_caller(
                binding=make_binding(
                    provider=FakeProvider(), model="test", capabilities=caps
                )
            ),
        )
        assert captured_kwargs["kind"].compaction_enabled is True

    def test_agent_not_found_returns_error(self, tmp_path, monkeypatch):
        """AG-16: Non-existent agent returns error ToolResult."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)

        _set_agent_paths([agents_dir])

        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
        )

        class FakeProvider:
            pass

        result = run_task(
            {"task": "Review code", "agent": "nonexistent"},
            make_caller(
                binding=make_binding(
                    provider=FakeProvider(), model="test", capabilities=caps
                )
            ),
        )

        assert not result.success
        assert "not found" in result.error

    def test_agent_tools_override(self, tmp_path, monkeypatch):
        """AG-17: Agent allowed-tools used when task tools not specified."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "reader.md").write_text(
            "---\nallowed-tools:\n  - read_file\n---\n\nYou are a reader."
        )

        _set_agent_paths([agents_dir])

        captured_kwargs = {}

        def mock_run_loop(kind, start, ctx):
            captured_kwargs["kind"] = kind
            from agent_cli.tools.result import ToolResult

            return ToolResult(True, output="done")

        monkeypatch.setattr("agent_cli.loop.run.run_loop", mock_run_loop)

        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
        )

        class FakeProvider:
            pass

        result = run_task(
            {"task": "Read files", "agent": "reader"},
            make_caller(
                binding=make_binding(
                    provider=FakeProvider(), model="test", capabilities=caps
                )
            ),
        )

        assert result.success
        assert captured_kwargs["kind"].tools == ["read_file"]

    def test_task_tools_outside_the_profile_are_rejected(self, tmp_path, monkeypatch):
        """AG-18 (v10.14.0): the profile's allowed-tools is an upper bound —
        task-level tools used to replace it, giving a read-only profile
        `shell`. With nothing in common the run is refused before any loop."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "reader.md").write_text(
            "---\nallowed-tools:\n  - read_file\n---\n\nYou are a reader."
        )

        _set_agent_paths([agents_dir])

        captured_kwargs = {}

        def mock_run_loop(kind, start, ctx):
            captured_kwargs["kind"] = kind
            from agent_cli.tools.result import ToolResult

            return ToolResult(True, output="done")

        monkeypatch.setattr("agent_cli.loop.run.run_loop", mock_run_loop)

        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
        )

        class FakeProvider:
            pass

        result = run_task(
            {"task": "Run shell", "agent": "reader", "tools": ["shell"]},
            make_caller(
                binding=make_binding(
                    provider=FakeProvider(), model="test", capabilities=caps
                )
            ),
        )

        assert not result.success
        assert "Delegation rejected" in result.error
        assert "shell" in result.error and "read_file" in result.error
        assert captured_kwargs == {}

    def test_agent_model_override(self, tmp_path, monkeypatch):
        """AG-19: Agent model config overrides default model."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "smart.md").write_text(
            "---\nmodel: special-model\n---\n\nYou are smart."
        )

        _set_agent_paths([agents_dir])

        captured_kwargs = {}

        def mock_run_loop(kind, start, ctx):
            captured_kwargs["kind"] = kind
            from agent_cli.tools.result import ToolResult

            return ToolResult(True, output="done")

        monkeypatch.setattr("agent_cli.loop.run.run_loop", mock_run_loop)

        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
        )

        class FakeProvider:
            pass

        result = run_task(
            {"task": "Think hard", "agent": "smart"},
            make_caller(
                binding=make_binding(
                    provider=FakeProvider(), model="default-model", capabilities=caps
                )
            ),
        )

        assert result.success
        assert captured_kwargs["kind"].binding.model == "special-model"

    def test_without_agent_unchanged(self, tmp_path, monkeypatch):
        """AG-20: Empty agent_name preserves existing behavior."""
        captured_kwargs = {}

        def mock_run_loop(kind, start, ctx):
            captured_kwargs["kind"] = kind
            from agent_cli.tools.result import ToolResult

            return ToolResult(True, output="done")

        monkeypatch.setattr("agent_cli.loop.run.run_loop", mock_run_loop)

        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
        )

        class FakeProvider:
            pass

        result = run_task(
            {"task": "Do something", "agent": ""},
            make_caller(
                binding=make_binding(
                    provider=FakeProvider(), model="test", capabilities=caps
                )
            ),
        )

        assert result.success
        assert captured_kwargs["kind"].agent_role == ""


class TestRunAgentsPassAgent:
    """호스트(`run_agent.launch_run`)가 과제의 ``agent`` 를 그 run 에이전트의 종류에 넘긴다 — 엔진은
    가짜(`agent_cli.loop.run.run_loop`), 프로파일은 내장(실제 로드)."""

    @staticmethod
    def _caller():
        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        )

        class FakeProvider:
            pass

        return make_caller(
            binding=make_binding(
                provider=FakeProvider(), model="test", capabilities=caps
            )
        )

    def test_passes_agent_name(self, monkeypatch):
        """AG-21: the spec's ``agent`` reaches the run agent's kind."""
        from tests.loop_kinds import run_tasks

        captured = []

        def fake_engine(kind, start, ctx):
            captured.append(kind.agent_name)
            return ToolResult(True, output="done")

        monkeypatch.setattr("agent_cli.loop.run.run_loop", fake_engine)
        run_tasks(
            {"tasks": [{"task": "Review", "agent": "code-reviewer"}]}, self._caller()
        )
        assert captured == ["code-reviewer"]

    def test_parallel_with_different_agents(self, monkeypatch):
        """AG-22: Parallel tasks can use different agents."""
        from tests.loop_kinds import run_tasks

        captured = []
        lock = __import__("threading").Lock()

        def fake_engine(kind, start, ctx):
            with lock:
                captured.append(kind.agent_name)
            return ToolResult(True, output="done")

        monkeypatch.setattr("agent_cli.loop.run.run_loop", fake_engine)
        run_tasks(
            {
                "tasks": [
                    {"task": "Review code", "agent": "code-reviewer"},
                    {"task": "Write code", "agent": "code-writer"},
                ]
            },
            self._caller(),
        )
        assert sorted(captured) == ["code-reviewer", "code-writer"]

    def test_parallel_runs_get_unique_keys_across_calls(self, monkeypatch):
        """AG-23: run agent keys (and so their channel blocks ``<key>#<item>``) stay
        unique across consecutive parallel calls — a recycled id would make the
        web frontend reuse a stale block and never draw the new one."""
        from tests.loop_kinds import run_tasks

        keys = []

        class CapturingRenderer:
            def current_task_id(self):
                return ""

            def begin_prompt_scope(self, *a, **k):
                pass

            def end_prompt_scope(self, *a, **k):
                pass

            def note_scope_ctx(self, ctx):
                pass

            def agent_roster(self, roster):
                pass

            def begin_agent_work(self, *, key, **kw):
                keys.append(key)

            def end_agent_work(self, **kw):
                pass

            def agent_message(
                self, **kw
            ):  # 과제 요청·회신 줄 (v10.141.0 — run 도 요청을 받는다)
                pass

        monkeypatch.setattr(
            "agent_cli.render.get_renderer", lambda: CapturingRenderer()
        )
        monkeypatch.setattr(
            "agent_cli.loop.run.run_loop",
            lambda kind, start, ctx: ToolResult(True, output="done"),
        )
        for _ in range(5):
            run_tasks({"tasks": [{"task": "A"}, {"task": "B"}]}, self._caller())
        assert len(keys) == 10
        assert len(set(keys)) == 10
