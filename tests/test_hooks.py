"""Tests for hook system (PreToolUse, PostToolUse, PostToolUseFailure)."""

from __future__ import annotations

import json
from unittest.mock import patch


from agent_cli.hooks import (
    HookEntry,
    HookMatcher,
    HookResult,
    load_hooks,
    merge_hooks_configs,
    run_hooks,
)


class TestHookModels:
    def test_hook_entry_defaults(self):
        entry = HookEntry(command="echo ok")
        assert entry.command == "echo ok"
        assert entry.timeout == 30

    def test_hook_matcher_defaults(self):
        matcher = HookMatcher(matcher="", hooks=[HookEntry(command="echo ok")])
        assert matcher.matcher == ""
        assert len(matcher.hooks) == 1


class TestLoadHooks:
    def test_load_from_file(self, tmp_path, monkeypatch):
        hooks_file = tmp_path / ".agent-cli" / "hooks.json"
        hooks_file.parent.mkdir(parents=True)
        hooks_file.write_text(
            json.dumps(
                {
                    "PreToolUse": [
                        {
                            "matcher": "shell",
                            "hooks": [{"command": "echo pre", "timeout": 10}],
                        }
                    ]
                }
            )
        )

        import agent_cli.hooks.shell as hooks_shell

        monkeypatch.setattr(hooks_shell, "_HOOKS_PATHS", [hooks_file])

        result = load_hooks(use_cache=False)
        assert "PreToolUse" in result
        assert len(result["PreToolUse"]) == 1
        assert result["PreToolUse"][0].matcher == "shell"
        assert result["PreToolUse"][0].hooks[0].command == "echo pre"
        assert result["PreToolUse"][0].hooks[0].timeout == 10

    def test_load_no_file(self, tmp_path, monkeypatch):
        import agent_cli.hooks.shell as hooks_shell

        monkeypatch.setattr(
            hooks_shell, "_HOOKS_PATHS", [tmp_path / "nonexistent.json"]
        )

        result = load_hooks(use_cache=False)
        assert result == {}

    def test_no_hooks_configured(self):
        """No hooks → tools work normally."""
        result = run_hooks("PreToolUse", "shell", {"command": "ls"}, hooks_config={})
        assert result.allowed is True
        assert result.updated_input is None


class TestMatcherFiltering:
    def test_matcher_matches_tool(self):
        config = {
            "PreToolUse": [
                HookMatcher(
                    matcher="shell",
                    hooks=[HookEntry(command="echo matched")],
                )
            ]
        }
        with patch("agent_cli.hooks.shell._execute_hook_command") as mock_exec:
            mock_exec.return_value = HookResult(allowed=True)
            run_hooks("PreToolUse", "shell", {"command": "ls"}, hooks_config=config)
            assert mock_exec.called

    def test_matcher_skips_non_matching_tool(self):
        config = {
            "PreToolUse": [
                HookMatcher(
                    matcher="shell",
                    hooks=[HookEntry(command="echo matched")],
                )
            ]
        }
        with patch("agent_cli.hooks.shell._execute_hook_command") as mock_exec:
            mock_exec.return_value = HookResult(allowed=True)
            run_hooks("PreToolUse", "read_file", {"path": "a.py"}, hooks_config=config)
            assert not mock_exec.called

    def test_empty_matcher_matches_all(self):
        config = {
            "PreToolUse": [
                HookMatcher(
                    matcher="",
                    hooks=[HookEntry(command="echo all")],
                )
            ]
        }
        with patch("agent_cli.hooks.shell._execute_hook_command") as mock_exec:
            mock_exec.return_value = HookResult(allowed=True)
            run_hooks("PreToolUse", "read_file", {"path": "a.py"}, hooks_config=config)
            assert mock_exec.called


class TestPreToolUse:
    def test_allow_on_exit_0(self):
        """exit 0 → tool proceeds."""
        result = run_hooks(
            "PreToolUse",
            "shell",
            {"command": "ls"},
            hooks_config={
                "PreToolUse": [
                    HookMatcher(
                        matcher="shell",
                        hooks=[HookEntry(command="exit 0")],
                    )
                ]
            },
        )
        assert result.allowed is True

    def test_block_on_exit_2(self):
        """exit 2 → tool blocked."""
        result = run_hooks(
            "PreToolUse",
            "shell",
            {"command": "rm -rf /"},
            hooks_config={
                "PreToolUse": [
                    HookMatcher(
                        matcher="shell",
                        hooks=[
                            HookEntry(
                                command='echo "Dangerous command blocked" >&2; exit 2'
                            )
                        ],
                    )
                ]
            },
        )
        assert result.allowed is False
        assert "Dangerous" in (result.stderr or "")

    def test_updated_input(self):
        """stdout JSON with updatedInput → tool input modified."""
        json_output = json.dumps({"updatedInput": {"command": "ls --safe"}})
        result = run_hooks(
            "PreToolUse",
            "shell",
            {"command": "ls"},
            hooks_config={
                "PreToolUse": [
                    HookMatcher(
                        matcher="shell",
                        hooks=[HookEntry(command=f"echo '{json_output}'")],
                    )
                ]
            },
        )
        assert result.allowed is True
        assert result.updated_input == {"command": "ls --safe"}


class TestPostToolUse:
    def test_fires_after_success(self):
        """PostToolUse hook executes after successful tool."""
        with patch("agent_cli.hooks.shell._execute_hook_command") as mock_exec:
            mock_exec.return_value = HookResult(allowed=True)
            run_hooks(
                "PostToolUse",
                "edit_file",
                {"path": "a.py"},
                hooks_config={
                    "PostToolUse": [
                        HookMatcher(
                            matcher="edit_file",
                            hooks=[HookEntry(command="echo formatted")],
                        )
                    ]
                },
                tool_result="STATUS: success\nRESULT: ok",
            )
            assert mock_exec.called
            # Verify tool_result was passed in the stdin data
            call_args = mock_exec.call_args
            stdin_data = call_args[0][1]  # second positional arg
            assert "tool_result" in stdin_data


class TestPostToolUseFailure:
    def test_fires_after_failure(self):
        """PostToolUseFailure hook executes after failed tool."""
        with patch("agent_cli.hooks.shell._execute_hook_command") as mock_exec:
            mock_exec.return_value = HookResult(allowed=True)
            run_hooks(
                "PostToolUseFailure",
                "shell",
                {"command": "bad"},
                hooks_config={
                    "PostToolUseFailure": [
                        HookMatcher(
                            matcher="",
                            hooks=[HookEntry(command="echo logged")],
                        )
                    ]
                },
                tool_result="STATUS: error\nERROR: failed",
            )
            assert mock_exec.called


class TestHookTimeout:
    def test_timeout_does_not_block(self):
        """Slow hook → timeout, tool proceeds."""
        result = run_hooks(
            "PreToolUse",
            "shell",
            {"command": "ls"},
            hooks_config={
                "PreToolUse": [
                    HookMatcher(
                        matcher="",
                        hooks=[HookEntry(command="sleep 60", timeout=1)],
                    )
                ]
            },
        )
        # Timeout should not block the tool
        assert result.allowed is True


class TestSkillFrontmatterHooks:
    def test_parse_hooks_from_frontmatter(self, tmp_path):
        from agent_cli.skills.loader import _parse_skill_file

        skill_file = tmp_path / "safe-deploy.md"
        skill_file.write_text(
            "---\n"
            "name: safe-deploy\n"
            "description: Deploy safely\n"
            "hooks:\n"
            "  PreToolUse:\n"
            "    - matcher: shell\n"
            "      hooks:\n"
            "        - command: echo check\n"
            "          timeout: 10\n"
            "---\n\n"
            "Deploy $ARGUMENTS\n"
        )
        skill = _parse_skill_file(skill_file)
        assert skill is not None
        assert skill.hooks is not None
        assert "PreToolUse" in skill.hooks
        assert skill.hooks["PreToolUse"][0].matcher == "shell"
        assert skill.hooks["PreToolUse"][0].hooks[0].command == "echo check"


class TestMergeHooksConfigs:
    """merge_hooks_configs layers skill-local matchers on top of the
    caller's hooks so a skill can tighten tool controls for its own
    execution window without losing parent hooks."""

    def test_merge_none_and_none_returns_none(self):
        assert merge_hooks_configs(None, None) is None

    def test_merge_empty_dicts_returns_none(self):
        assert merge_hooks_configs({}, {}) is None

    def test_merge_passthrough_single(self):
        cfg = {"PreToolUse": [HookMatcher(matcher="shell", hooks=[HookEntry("x")])]}
        merged = merge_hooks_configs(None, cfg)
        assert merged is not None
        assert len(merged["PreToolUse"]) == 1
        assert merged["PreToolUse"][0].hooks[0].command == "x"

    def test_merge_concatenates_same_event(self):
        parent = {
            "PreToolUse": [HookMatcher(matcher="shell", hooks=[HookEntry("parent")])]
        }
        skill = {
            "PreToolUse": [HookMatcher(matcher="shell", hooks=[HookEntry("skill")])]
        }
        merged = merge_hooks_configs(parent, skill)
        assert merged is not None
        matchers = merged["PreToolUse"]
        assert len(matchers) == 2
        # Parent first, skill second — preserves "parent fires, then skill"
        # ordering so a skill can react to a parent hook's decision.
        assert matchers[0].hooks[0].command == "parent"
        assert matchers[1].hooks[0].command == "skill"

    def test_merge_preserves_distinct_events(self):
        parent = {"PreToolUse": [HookMatcher(matcher="", hooks=[HookEntry("pre")])]}
        skill = {"PostToolUse": [HookMatcher(matcher="", hooks=[HookEntry("post")])]}
        merged = merge_hooks_configs(parent, skill)
        assert merged is not None
        assert set(merged) == {"PreToolUse", "PostToolUse"}


class TestSkillHooksWiring:
    """Regression guard: Skill.hooks must actually reach run_loop via the
    executor. The field used to be populated by the loader and then
    silently dropped on the floor."""

    def test_execute_skill_forwards_merged_hooks_to_run_loop(self):
        """execute_skill must pass merge_hooks_configs(parent, skill.hooks)
        as hooks_config into run_loop — otherwise the frontmatter hooks
        field is write-only dead data."""
        from unittest.mock import MagicMock

        from agent_cli.providers.compat import ModelCapabilities
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.models import Skill
        from agent_cli.tools.result import ToolResult

        skill_hooks = {
            "PreToolUse": [
                HookMatcher(matcher="shell", hooks=[HookEntry("echo skill")])
            ]
        }
        parent_hooks = {
            "PreToolUse": [HookMatcher(matcher="", hooks=[HookEntry("echo parent")])]
        }

        skill = Skill(
            name="t",
            description="d",
            prompt_template="go",
            allowed_tools=["shell"],
            hooks=skill_hooks,
        )

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
            thinking_format="",
        )

        captured: dict = {}

        def fake_run_loop(**kwargs):
            captured.update(kwargs)
            return ToolResult(True, output="done")

        with patch("agent_cli.skills.executor.run_loop", side_effect=fake_run_loop):
            execute_skill(
                skill=skill,
                arguments="",
                provider=MagicMock(),
                capabilities=caps,
                model="m",
                parent_hooks_config=parent_hooks,
            )

        forwarded = captured.get("hooks_config")
        assert forwarded is not None, "hooks_config must be forwarded"
        assert "PreToolUse" in forwarded
        cmds = [m.hooks[0].command for m in forwarded["PreToolUse"]]
        assert cmds == ["echo parent", "echo skill"]

    def test_execute_skill_no_hooks_yields_none(self):
        """When neither parent nor skill has hooks, hooks_config stays
        None so the downstream `if hooks_config:` branches skip cleanly."""
        from unittest.mock import MagicMock

        from agent_cli.providers.compat import ModelCapabilities
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.models import Skill
        from agent_cli.tools.result import ToolResult

        skill = Skill(
            name="t",
            description="d",
            prompt_template="go",
            allowed_tools=["shell"],
        )
        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_tool_calling=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
            thinking_format="",
        )

        captured: dict = {}

        def fake_run_loop(**kwargs):
            captured.update(kwargs)
            return ToolResult(True, output="done")

        with patch("agent_cli.skills.executor.run_loop", side_effect=fake_run_loop):
            execute_skill(
                skill=skill,
                arguments="",
                provider=MagicMock(),
                capabilities=caps,
                model="m",
            )

        assert captured.get("hooks_config") is None
