"""Tests for hook system (PreToolUse, PostToolUse, PostToolUseFailure)."""

from __future__ import annotations

import json
from unittest.mock import patch


from agent_cli.hooks import (
    HookEntry,
    HookMatcher,
    HookResult,
    load_hooks,
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
