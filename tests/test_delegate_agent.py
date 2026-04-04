"""Tests for delegate agent parameter feature (AG-01 ~ AG-22)."""

from __future__ import annotations

from agent_cli.tools.delegate import (
    _load_agent,
    _run_single,
    _validate_agent_name,
)
from agent_cli.tools.result import ToolResult


# ── AG-01 ~ AG-05: Agent name validation ──────────────────


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
        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [agents_dir],
        )

        role, config, error = _load_agent("reviewer")

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
        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [agents_dir],
        )

        role, config, error = _load_agent("secure")

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
        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [agents_dir],
        )

        role, config, error = _load_agent("broken")

        assert error is None
        assert config == {}
        assert role == content.strip()

    def test_not_found(self, tmp_path, monkeypatch):
        """AG-09: Non-existent agent returns error."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [agents_dir],
        )

        role, config, error = _load_agent("nonexistent")

        assert role is None
        assert config == {}
        assert "not found" in error

    def test_invalid_name(self):
        """AG-10: Invalid agent name returns error."""
        role, config, error = _load_agent("../hack")

        assert role is None
        assert config == {}
        assert "Invalid agent name" in error

    def test_empty_body(self, tmp_path, monkeypatch):
        """AG-11: File with frontmatter but no body returns error."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "empty.md").write_text("---\nmodel: test\n---\n\n")
        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [agents_dir],
        )

        role, config, error = _load_agent("empty")

        assert role is None
        assert "no content" in error

    def test_project_overrides_global(self, tmp_path, monkeypatch):
        """AG-12: Project-local agent takes priority over user-global."""
        project_dir = tmp_path / "project" / ".agent-cli" / "agents"
        global_dir = tmp_path / "global" / ".agent-cli" / "agents"
        project_dir.mkdir(parents=True)
        global_dir.mkdir(parents=True)

        (project_dir / "reviewer.md").write_text("Project reviewer")
        (global_dir / "reviewer.md").write_text("Global reviewer")

        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [project_dir, global_dir],
        )

        role, config, error = _load_agent("reviewer")

        assert error is None
        assert role == "Project reviewer"

    def test_falls_back_to_global(self, tmp_path, monkeypatch):
        """AG-13: Falls back to global when not in project."""
        project_dir = tmp_path / "project" / ".agent-cli" / "agents"
        global_dir = tmp_path / "global" / ".agent-cli" / "agents"
        project_dir.mkdir(parents=True)
        global_dir.mkdir(parents=True)

        (global_dir / "reviewer.md").write_text("Global reviewer")

        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [project_dir, global_dir],
        )

        role, config, error = _load_agent("reviewer")

        assert error is None
        assert role == "Global reviewer"

    def test_unknown_frontmatter_fields_ignored(self, tmp_path, monkeypatch):
        """AG-14: Claude Code compat: unknown frontmatter fields are ignored."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "compat.md").write_text(
            "---\nallowed-tools:\n  - read_file\nunknown-field: value\nanother: 123\n---\n\nYou are an agent."
        )
        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [agents_dir],
        )

        role, config, error = _load_agent("compat")

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
        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [agents_dir],
        )

        captured_kwargs = {}

        def mock_run_loop(**kwargs):
            captured_kwargs.update(kwargs)
            return "done"

        monkeypatch.setattr("agent_cli.loop.run_loop", mock_run_loop)

        from agent_cli.providers.compat import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

        class FakeProvider:
            pass

        result = _run_single(
            task="Write tests",
            agent_name="tester",
            provider=FakeProvider(),
            model="test",
            capabilities=caps,
        )

        assert result.success
        assert captured_kwargs["agent_role"] == "You are a test engineer."

    def test_agent_not_found_returns_error(self, tmp_path, monkeypatch):
        """AG-16: Non-existent agent returns error ToolResult."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [agents_dir],
        )

        from agent_cli.providers.compat import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

        class FakeProvider:
            pass

        result = _run_single(
            task="Review code",
            agent_name="nonexistent",
            provider=FakeProvider(),
            model="test",
            capabilities=caps,
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
        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [agents_dir],
        )

        captured_kwargs = {}

        def mock_run_loop(**kwargs):
            captured_kwargs.update(kwargs)
            return "done"

        monkeypatch.setattr("agent_cli.loop.run_loop", mock_run_loop)

        from agent_cli.providers.compat import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

        class FakeProvider:
            pass

        result = _run_single(
            task="Read files",
            agent_name="reader",
            provider=FakeProvider(),
            model="test",
            capabilities=caps,
        )

        assert result.success
        assert captured_kwargs["active_tools"] == ["read_file"]

    def test_task_tools_overrides_agent_tools(self, tmp_path, monkeypatch):
        """AG-18: Task-level tools take priority over agent allowed-tools."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "reader.md").write_text(
            "---\nallowed-tools:\n  - read_file\n---\n\nYou are a reader."
        )
        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [agents_dir],
        )

        captured_kwargs = {}

        def mock_run_loop(**kwargs):
            captured_kwargs.update(kwargs)
            return "done"

        monkeypatch.setattr("agent_cli.loop.run_loop", mock_run_loop)

        from agent_cli.providers.compat import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

        class FakeProvider:
            pass

        result = _run_single(
            task="Run shell",
            agent_name="reader",
            allowed_tools=["shell"],
            provider=FakeProvider(),
            model="test",
            capabilities=caps,
        )

        assert result.success
        assert captured_kwargs["active_tools"] == ["shell"]

    def test_agent_model_override(self, tmp_path, monkeypatch):
        """AG-19: Agent model config overrides default model."""
        agents_dir = tmp_path / ".agent-cli" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "smart.md").write_text(
            "---\nmodel: special-model\n---\n\nYou are smart."
        )
        monkeypatch.setattr(
            "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
            [agents_dir],
        )

        captured_kwargs = {}

        def mock_run_loop(**kwargs):
            captured_kwargs.update(kwargs)
            return "done"

        monkeypatch.setattr("agent_cli.loop.run_loop", mock_run_loop)

        from agent_cli.providers.compat import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

        class FakeProvider:
            pass

        result = _run_single(
            task="Think hard",
            agent_name="smart",
            provider=FakeProvider(),
            model="default-model",
            capabilities=caps,
        )

        assert result.success
        assert captured_kwargs["model"] == "special-model"

    def test_without_agent_unchanged(self, tmp_path, monkeypatch):
        """AG-20: Empty agent_name preserves existing behavior."""
        captured_kwargs = {}

        def mock_run_loop(**kwargs):
            captured_kwargs.update(kwargs)
            return "done"

        monkeypatch.setattr("agent_cli.loop.run_loop", mock_run_loop)

        from agent_cli.providers.compat import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

        class FakeProvider:
            pass

        result = _run_single(
            task="Do something",
            agent_name="",
            provider=FakeProvider(),
            model="test",
            capabilities=caps,
        )

        assert result.success
        assert captured_kwargs["agent_role"] == ""


class TestToolDelegatePassesAgent:
    def test_passes_agent_name(self, monkeypatch):
        """AG-21: tool_delegate passes spec['agent'] to _run_single."""
        from agent_cli.tools.delegate import tool_delegate

        captured_kwargs = {}

        def mock_run_single(**kwargs):
            captured_kwargs.update(kwargs)
            return ToolResult(True, output="done")

        monkeypatch.setattr("agent_cli.tools.delegate._run_single", mock_run_single)

        from agent_cli.providers.compat import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

        class FakeProvider:
            pass

        tool_delegate(
            args={"tasks": [{"task": "Review", "agent": "reviewer"}]},
            provider=FakeProvider(),
            model="test",
            capabilities=caps,
        )

        assert captured_kwargs["agent_name"] == "reviewer"

    def test_parallel_with_different_agents(self, monkeypatch):
        """AG-22: Parallel tasks can use different agents."""
        from agent_cli.tools.delegate import _run_parallel

        captured_agents = []

        def mock_run_single(**kwargs):
            captured_agents.append(kwargs.get("agent_name", ""))
            return ToolResult(True, output="done")

        monkeypatch.setattr("agent_cli.tools.delegate._run_single", mock_run_single)

        from agent_cli.providers.compat import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

        class FakeProvider:
            pass

        _run_parallel(
            task_specs=[
                {"task": "Review code", "agent": "reviewer"},
                {"task": "Check security", "agent": "security"},
            ],
            provider=FakeProvider(),
            model="test",
            capabilities=caps,
        )

        assert sorted(captured_agents) == ["reviewer", "security"]
