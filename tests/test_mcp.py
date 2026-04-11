"""Tests for MCP integration (config, client, adapter)."""

from __future__ import annotations

import json


from agent_cli.mcp.config import (
    McpServerConfig,
    _resolve_env_vars,
    load_mcp_config,
)


# ── Config Tests ──────────────────────────────────────


class TestMcpServerConfig:
    def test_stdio_config(self):
        cfg = McpServerConfig(
            name="test",
            command="npx",
            args=["-y", "server"],
            transport="stdio",
        )
        assert cfg.is_stdio
        assert not cfg.is_sse

    def test_sse_config(self):
        cfg = McpServerConfig(
            name="test",
            url="http://localhost:8080",
            transport="sse",
        )
        assert cfg.is_sse
        assert not cfg.is_stdio

    def test_invalid_config(self):
        cfg = McpServerConfig(name="test")
        assert not cfg.is_stdio
        assert not cfg.is_sse


class TestResolveEnvVars:
    def test_resolves_existing_var(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret123")
        assert _resolve_env_vars("Bearer ${MY_TOKEN}") == "Bearer secret123"

    def test_missing_var_becomes_empty(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        assert _resolve_env_vars("${NONEXISTENT_VAR}") == ""

    def test_no_vars_unchanged(self):
        assert _resolve_env_vars("plain text") == "plain text"

    def test_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert _resolve_env_vars("${A}-${B}") == "1-2"


class TestLoadMcpConfig:
    def test_empty_when_no_files(self, tmp_path):
        result = load_mcp_config(search_paths=[tmp_path / "nonexistent.json"])
        assert result == {}

    def test_loads_single_file(self, tmp_path):
        config_file = tmp_path / "mcp.json"
        config_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "github": {
                            "command": "npx",
                            "args": ["-y", "@mcp/github"],
                        }
                    }
                }
            )
        )
        result = load_mcp_config(search_paths=[config_file])
        assert "github" in result
        assert result["github"].command == "npx"
        assert result["github"].args == ["-y", "@mcp/github"]
        assert result["github"].is_stdio

    def test_sse_server(self, tmp_path):
        config_file = tmp_path / "mcp.json"
        config_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": "http://localhost:8080",
                            "transport": "sse",
                        }
                    }
                }
            )
        )
        result = load_mcp_config(search_paths=[config_file])
        assert result["remote"].is_sse
        assert result["remote"].url == "http://localhost:8080"

    def test_project_overrides_user(self, tmp_path):
        user_file = tmp_path / "user_mcp.json"
        user_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "github": {"command": "old-cmd", "args": ["--old"]},
                        "only-user": {"command": "user-cmd"},
                    }
                }
            )
        )
        project_file = tmp_path / "project_mcp.json"
        project_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "github": {"command": "new-cmd", "args": ["--new"]},
                    }
                }
            )
        )
        # user first (lower priority), then project (higher priority)
        result = load_mcp_config(search_paths=[user_file, project_file])
        assert result["github"].command == "new-cmd"
        assert result["github"].args == ["--new"]
        assert "only-user" in result  # user-only server preserved

    def test_env_var_resolution(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "abc123")
        config_file = tmp_path / "mcp.json"
        config_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "github": {
                            "command": "npx",
                            "args": [],
                            "env": {"GITHUB_TOKEN": "${GH_TOKEN}"},
                        }
                    }
                }
            )
        )
        result = load_mcp_config(search_paths=[config_file])
        assert result["github"].env["GITHUB_TOKEN"] == "abc123"

    def test_invalid_json_skipped(self, tmp_path):
        config_file = tmp_path / "mcp.json"
        config_file.write_text("NOT VALID JSON")
        result = load_mcp_config(search_paths=[config_file])
        assert result == {}

    def test_url_auto_detects_sse(self, tmp_path):
        config_file = tmp_path / "mcp.json"
        config_file.write_text(
            json.dumps({"mcpServers": {"api": {"url": "http://host:9090"}}})
        )
        result = load_mcp_config(search_paths=[config_file])
        assert result["api"].transport == "sse"
        assert result["api"].is_sse


# ── Adapter Tests ─────────────────────────────────────


class TestMcpAdapter:
    def test_wrap_mcp_tool_success(self):
        from unittest.mock import MagicMock

        from agent_cli.mcp.adapter import wrap_mcp_tool

        manager = MagicMock()
        mock_result = MagicMock()
        mock_result.content = [MagicMock(text="search result")]
        manager.call_tool.return_value = mock_result

        fn = wrap_mcp_tool(manager, "github", "search")
        result = fn({"query": "test"})

        assert result.success
        assert "search result" in result.output
        manager.call_tool.assert_called_once_with("github", "search", {"query": "test"})

    def test_wrap_mcp_tool_failure(self):
        from unittest.mock import MagicMock

        from agent_cli.mcp.adapter import wrap_mcp_tool

        manager = MagicMock()
        manager.call_tool.side_effect = ConnectionError("server down")

        fn = wrap_mcp_tool(manager, "github", "search")
        result = fn({"query": "test"})

        assert not result.success
        assert "server down" in result.error

    def test_register_mcp_tools(self):
        from unittest.mock import MagicMock

        from agent_cli.mcp.adapter import register_mcp_tools
        from agent_cli.mcp.client import McpToolInfo

        manager = MagicMock()
        manager.list_tools.return_value = [
            McpToolInfo(
                server="github",
                name="list_issues",
                description="List issues",
                input_schema={},
            ),
            McpToolInfo(
                server="github",
                name="create_pr",
                description="Create PR",
                input_schema={},
            ),
        ]

        tools = register_mcp_tools(manager)
        assert "github.list_issues" in tools
        assert "github.create_pr" in tools
        assert callable(tools["github.list_issues"])

    def test_build_mcp_tool_descriptions(self):
        from unittest.mock import MagicMock

        from agent_cli.mcp.adapter import build_mcp_tool_descriptions
        from agent_cli.mcp.client import McpToolInfo

        manager = MagicMock()
        manager.list_tools.return_value = [
            McpToolInfo(
                server="github",
                name="list_issues",
                description="List GitHub issues",
                input_schema={
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name"}
                    }
                },
            ),
        ]

        desc = build_mcp_tool_descriptions(manager)
        assert "github.list_issues" in desc
        assert "List GitHub issues" in desc
        assert "repo" in desc

    def test_build_descriptions_empty(self):
        from unittest.mock import MagicMock

        from agent_cli.mcp.adapter import build_mcp_tool_descriptions

        manager = MagicMock()
        manager.list_tools.return_value = []

        assert build_mcp_tool_descriptions(manager) == ""
