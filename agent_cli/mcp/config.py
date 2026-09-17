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

# Streamable HTTP 의 표기 흔들림 수용 — 규격 문서·다른 클라이언트가
# "streamable-http" / "streamable_http" / "http" 를 섞어 쓴다 (v9.3.0).
_STREAMABLE_ALIASES = frozenset({"streamable-http", "streamable_http", "http"})
STREAMABLE_HTTP = "streamable-http"  # 우리가 파일에 쓰는 캐노니컬 이름


@dataclass
class McpServerConfig:
    """Configuration for a single MCP server."""

    name: str
    # stdio transport
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # HTTP transports (sse | streamable-http)
    url: str = ""
    transport: str = "stdio"  # "stdio" | "sse" | "streamable-http"

    @property
    def is_stdio(self) -> bool:
        return self.transport == "stdio" and bool(self.command)

    @property
    def is_sse(self) -> bool:
        """구 HTTP+SSE 전송 (MCP 초기 규격)."""
        return self.transport == "sse" and bool(self.url)

    @property
    def is_streamable_http(self) -> bool:
        """Streamable HTTP (MCP 2025-03-26 — 현재 권장 원격 전송, v9.3.0).

        단일 엔드포인트에 POST 하며 ``Accept: application/json,
        text/event-stream`` 을 **둘 다** 요구한다. 구 sse_client 로 붙으면
        서버가 -32600 "Not Acceptable" 로 거절한다."""
        return self.transport in _STREAMABLE_ALIASES and bool(self.url)

    @property
    def is_remote(self) -> bool:
        return self.is_sse or self.is_streamable_http


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

    # Detect transport type. url 이 있으면 HTTP 계열 — 어느 세대인지는
    # ``transport`` 가 정한다. **생략 시 기본은 종전대로 "sse"**: 바꾸면
    # 기존 손편집 설정이 조용히 다르게 동작한다. 마법사는 항상 명시해
    # 저장하므로 새로 만든 설정은 영향이 없고, 손편집으로 세대를 잘못 고른
    # 경우는 client.humanize_error 가 "전송 방식이 다릅니다"로 안내한다.
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
