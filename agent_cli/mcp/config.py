"""MCP server configuration loader.

v9.0.0: ``.agent-cli/mcp.json`` (프로젝트) **한 곳** — docs/config-scopes.
종전엔 ``~/.agent-cli/mcp.json`` 과 이름별로 병합했다(프로젝트 승). MCP 서버는
로컬 프로세스를 띄우는 프로젝트 성격의 설정이라 프로젝트에만 둔다. 종전의
유저 파일은 무시된다.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agent_cli.paths import project_dir

# 리스트로 두는 건 load_mcp_config 의 순회 구조·테스트 seam 을 유지하기 위함.
_MCP_CONFIG_PATHS = [project_dir() / "mcp.json"]

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")


@dataclass
class McpServerConfig:
    """Configuration for a single MCP server."""

    name: str
    # stdio transport
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # SSE transport
    url: str = ""
    transport: str = "stdio"  # "stdio" or "sse"

    @property
    def is_stdio(self) -> bool:
        return self.transport == "stdio" and bool(self.command)

    @property
    def is_sse(self) -> bool:
        return self.transport == "sse" and bool(self.url)


def _resolve_env_vars(value: str) -> str:
    """Replace ${VAR} with environment variable values."""

    def _replace(m: re.Match) -> str:
        return os.environ.get(m.group(1), "")

    return _ENV_VAR_RE.sub(_replace, value)


def _parse_server_config(name: str, data: dict) -> McpServerConfig:
    """Parse a single server config dict into McpServerConfig."""
    # Resolve env vars in env dict
    raw_env = data.get("env", {})
    resolved_env = {k: _resolve_env_vars(v) for k, v in raw_env.items()}

    # Detect transport type
    if "url" in data:
        transport = data.get("transport", "sse")
    else:
        transport = "stdio"

    return McpServerConfig(
        name=name,
        command=data.get("command", ""),
        args=data.get("args", []),
        env=resolved_env,
        url=data.get("url", ""),
        transport=transport,
    )


def load_mcp_config(
    search_paths: list[Path] | None = None,
) -> dict[str, McpServerConfig]:
    """Load MCP server configs (v9.0.0: 프로젝트 단일 — 병합 없음).

    Returns dict of {server_name: McpServerConfig}.
    """
    paths = search_paths if search_paths is not None else _MCP_CONFIG_PATHS
    merged: dict[str, McpServerConfig] = {}

    for config_path in paths:
        if not config_path.is_file():
            continue
        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[warn] Failed to load {config_path}: {e}", file=sys.stderr)
            continue

        servers = data.get("mcpServers", {})
        for name, server_data in servers.items():
            if not isinstance(server_data, dict):
                continue
            merged[name] = _parse_server_config(name, server_data)

    return merged
