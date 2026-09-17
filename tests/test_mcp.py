"""Tests for MCP integration (config, client, adapter)."""

from __future__ import annotations

import json

from agent_cli.mcp.config import (
    McpServerConfig,
    _resolve_env_vars,
    load_mcp_config,
)

# ── Config Tests ──────────────────────────────────────


class TestDisconnectClosesErrlog:
    """stdio 전송의 errlog(/dev/null) fd 는 disconnect 에서 닫힌다 — 종전엔
    저장조차 안 해 서버당 fd 1개가 프로세스 수명 내내 누수(리뷰 §4.5 수리)."""

    def _manager_with_fake_client(self, errlog):
        import asyncio
        from unittest.mock import MagicMock

        from agent_cli.mcp.client import McpClientManager

        mgr = McpClientManager()

        async def _noop(*a, **k):
            return None

        session = MagicMock()
        session.__aexit__ = _noop
        transport = MagicMock()
        transport.__aexit__ = _noop
        client = {"session": session, "transport_cm": transport}
        if errlog is not None:
            client["errlog"] = errlog
        mgr._clients["srv"] = client
        mgr._loop = asyncio.new_event_loop()
        return mgr

    def test_errlog_closed_on_disconnect(self, tmp_path):
        errlog = open(tmp_path / "null", "w")  # noqa: SIM115 — close 검증 대상
        mgr = self._manager_with_fake_client(errlog)
        try:
            mgr.disconnect("srv")
            assert errlog.closed
            assert "srv" not in mgr._clients
        finally:
            if not errlog.closed:
                errlog.close()
            mgr._loop.close()

    def test_errlog_closed_even_when_aexit_raises(self, tmp_path):
        errlog = open(tmp_path / "null", "w")  # noqa: SIM115 — close 검증 대상
        mgr = self._manager_with_fake_client(errlog)

        async def _boom(*a, **k):
            raise RuntimeError("teardown failed")

        mgr._clients["srv"]["session"].__aexit__ = _boom
        try:
            mgr.disconnect("srv")  # 예외는 삼켜지고 fd 는 finally 에서 닫힌다
            assert errlog.closed
        finally:
            if not errlog.closed:
                errlog.close()
            mgr._loop.close()

    def test_sse_client_without_errlog_is_noop(self):
        mgr = self._manager_with_fake_client(None)
        try:
            mgr.disconnect("srv")  # errlog 키 없음(SSE) — 조용히 통과
            assert "srv" not in mgr._clients
        finally:
            mgr._loop.close()


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

    def test_later_file_wins_when_given_several(self, tmp_path):
        """로더의 later-wins 병합 **기계**. v9.0.0 부터 제품은 경로를 하나만
        넘긴다(프로젝트 mcp.json — docs/config-scopes); 유저 전역 mcp.json 은
        제거됐다. 이 테스트는 search_paths 인자로 병합 기계만 검사한다."""
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
    def test_mcp_tool_run_success(self):
        from unittest.mock import MagicMock

        from agent_cli.mcp.adapter import McpTool

        manager = MagicMock()
        mock_result = MagicMock()
        mock_result.content = [MagicMock(text="search result")]
        manager.call_tool.return_value = mock_result

        tool = McpTool(manager, "github", "search", "Search", {})
        result = tool.run({"query": "test"})

        assert result.success
        assert "search result" in result.output
        manager.call_tool.assert_called_once_with("github", "search", {"query": "test"})

    def test_mcp_tool_run_failure(self):
        from unittest.mock import MagicMock

        from agent_cli.mcp.adapter import McpTool

        manager = MagicMock()
        manager.call_tool.side_effect = ConnectionError("server down")

        tool = McpTool(manager, "github", "search", "Search", {})
        result = tool.run({"query": "test"})

        assert not result.success
        assert "server down" in result.error

    def test_mcp_tool_is_prefixless(self):
        """MCP keys are bare (like virtual tools): strip_prefix is a no-op
        and claims stays False so MCP never hijacks infer_action."""
        from unittest.mock import MagicMock

        from agent_cli.mcp.adapter import McpTool

        tool = McpTool(MagicMock(), "github", "search", "Search", {})
        # bare key passes through unchanged
        assert tool.strip_prefix({"query": "x"}) == {"query": "x"}
        # bare-key payload is not claimed
        assert tool.claims({"query": "x"}) is False

    def test_mcp_tool_wrap_single_op_is_identity(self):
        """Regression: under a multi-op format the loop calls
        ``wrap_single_op`` on every tool op. MCP is prefix-less, so the base
        default (add_prefix) would namespace its bare keys
        (``{query}`` → ``{github.search_query}``) and the prefixed input would
        then fail validate (SCHEMA_MISMATCH) — MCP tools unusable under the
        default json_fc/react formats. McpTool overrides to identity."""
        from unittest.mock import MagicMock

        from agent_cli.mcp.adapter import McpTool

        tool = McpTool(
            MagicMock(),
            "github",
            "search",
            "Search",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        flat = {"query": "x"}
        assert tool.wrap_single_op(flat) == flat
        # end-to-end: the wrapped op still validates against the MCP schema
        from agent_cli.tools.registry import TOOLS, validate_tool_input

        TOOLS["github.search"] = tool
        try:
            ok, err, _ = validate_tool_input("github.search", tool.wrap_single_op(flat))
            assert ok is True, err
        finally:
            del TOOLS["github.search"]

    def test_register_mcp_tools(self):
        from unittest.mock import MagicMock

        from agent_cli.mcp.adapter import McpTool, register_mcp_tools
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
        # Tool instances, not bare callables — registry contract (.run/.parameters)
        assert isinstance(tools["github.list_issues"], McpTool)
        assert hasattr(tools["github.list_issues"], "run")
        assert hasattr(tools["github.list_issues"], "parameters")

    def test_mcp_dispatch_through_registry(self):
        """Regression for the Tool-ABC migration gap (423608e): MCP tools
        merged into TOOLS must flow through the SAME validate + dispatch
        path as native tools without crashing. Previously they were bare
        functions and ``validate_tool_input``/``_execute_tool`` raised
        AttributeError ('function' has no attribute 'parameters'/'run')."""
        from unittest.mock import MagicMock

        from agent_cli.mcp.adapter import register_mcp_tools
        from agent_cli.mcp.client import McpToolInfo
        from agent_cli.tools.registry import (
            TOOLS,
            _execute_tool,
            validate_tool_input,
        )

        manager = MagicMock()
        res = MagicMock()
        res.content = [MagicMock(text="ok")]
        manager.call_tool.return_value = res
        manager.list_tools.return_value = [
            McpToolInfo(
                server="gh",
                name="search",
                description="Search",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
        ]

        registered = register_mcp_tools(manager)
        TOOLS.update(registered)  # exactly what main.py does
        try:
            # validation path (recovery A5 detector wraps this)
            ok, err, _conv = validate_tool_input("gh.search", {"query": "x"})
            assert ok, err
            # missing required field is reported, not crashed
            bad_ok, bad_err, _ = validate_tool_input("gh.search", {})
            assert not bad_ok
            assert "query" in bad_err
            # dispatch path (loop._invoke_regular → _execute_tool)
            result = _execute_tool("gh.search", {"query": "x"})
            assert result.success
            assert "ok" in result.output
        finally:
            del TOOLS["gh.search"]

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


class TestMcpSdkIsADeclaredDependency:
    """v8.62.0: ``mcp`` SDK 가 **필수** 의존성이다.

    종전엔 pyproject 에 선언이 없고 client.py 가 함수 안에서 import 했다.
    SDK 가 없는 환경에선 ``connect_all`` 이 예외를 삼켜 stderr 경고 한 줄만
    남기고 **MCP 도구가 조용히 사라졌다** — 에이전트는 그대로 돌아서 왜
    도구가 없는지 드러나지 않는다. 그 조용한 실패가 이 테스트의 대상이다."""

    def test_sdk_importable(self):
        """설치 환경에 SDK 가 있다. pyproject 에서 의존성을 빼면 CI 가
        SDK 없이 설치하므로 여기서 잡힌다."""
        import mcp  # noqa: F401
        from mcp import ClientSession, StdioServerParameters  # noqa: F401
        from mcp.client.sse import sse_client  # noqa: F401
        from mcp.client.stdio import stdio_client  # noqa: F401

    def test_declared_in_pyproject(self):
        """선언 자체를 고정 — 설치 환경에 우연히 있는 것과 구분한다."""
        import re
        from pathlib import Path

        deps = Path("pyproject.toml").read_text(encoding="utf-8")
        block = deps.split("[project.optional-dependencies]")[0]
        assert re.search(r'^\s*"mcp[><=]', block, re.MULTILINE), (
            "mcp 는 core dependencies 에 있어야 한다 (optional extra 아님)"
        )

    def test_stdio_client_accepts_errlog(self):
        """하한 1.6 의 근거. ``errlog`` 는 1.6.0 에 들어왔고 1.4.0 엔 없다 —
        없는 SDK 로는 ``_connect_stdio`` 가 TypeError 로 깨진다. SDK 를
        올리다 이 인자가 사라지면 여기서 먼저 잡힌다."""
        import inspect

        from mcp.client.stdio import stdio_client

        assert "errlog" in inspect.signature(stdio_client).parameters

    def test_client_keeps_sdk_import_lazy(self):
        """필수 의존성이 됐어도 import 는 **함수 안에 남긴다** — ``import mcp``
        가 실측 ~235ms 라, 최상위로 올리면 MCP 를 안 쓰는 모든 CLI 실행에
        그 비용이 붙는다. 무심코 '정리'되는 걸 막는 가드."""
        from pathlib import Path

        src = Path("agent_cli/mcp/client.py").read_text(encoding="utf-8")
        head = src.split("class ", 1)[0]
        assert "from mcp import" not in head, "SDK import 는 함수 안에 두어야 한다"
        assert "from mcp import" in src  # 함수 안에는 있다


class TestLeafError:
    """v9.1.0: mcp SDK 는 전송 오류를 anyio TaskGroup 의 ExceptionGroup 으로
    감싼다 — 그대로 str() 하면 "unhandled errors in a TaskGroup (1 sub-exception)"
    이라 진짜 원인이 묻힌다. 마법사 실장 검증에서 잡혔고, 부팅 시 [warn] 줄도
    같은 문제였다.

    내장 ``ExceptionGroup`` 은 3.11+ 라 여기선 ``.exceptions`` 속성만 가진 가짜를
    쓴다 — 3.10 에선 anyio 가 ``exceptiongroup`` 백포트를 쓰므로 그 덕타이핑이
    실제 계약이다."""

    @staticmethod
    def _group(msg, subs):
        class _Group(Exception):
            def __init__(self, m, ex):
                super().__init__(m)
                self.exceptions = tuple(ex)

        return _Group(msg, subs)

    def test_plain_exception_passes_through(self):
        from agent_cli.mcp.client import _leaf_error

        assert _leaf_error(ConnectionRefusedError("refused")) == "refused"

    def test_group_is_unwrapped_to_leaves(self):
        from agent_cli.mcp.client import _leaf_error

        eg = self._group(
            "unhandled errors in a TaskGroup", [OSError("Connection refused")]
        )
        assert _leaf_error(eg) == "Connection refused"

    def test_nested_groups_and_dedup(self):
        from agent_cli.mcp.client import _leaf_error

        inner = self._group("g", [OSError("a"), OSError("a")])
        outer = self._group("g", [inner, ValueError("b")])
        assert _leaf_error(outer) == "a; b"

    def test_empty_message_falls_back_to_type(self):
        from agent_cli.mcp.client import _leaf_error

        assert _leaf_error(RuntimeError("")) == "RuntimeError"

    def test_connect_all_reports_leaf(self):
        """실제 경로: connect_all 의 status 문자열과 [warn] 줄에 잎이 실린다."""
        from unittest.mock import MagicMock, patch

        from agent_cli.mcp.client import McpClientManager
        from agent_cli.mcp.config import McpServerConfig

        m = McpClientManager()
        eg = self._group(
            "unhandled errors in a TaskGroup", [OSError("Connection refused")]
        )
        # _connect_one 도 패치 — 안 그러면 인자로 만들어진 코루틴이 await 없이
        # 버려져 "never awaited" 경고가 난다.
        with (
            patch.object(m, "_connect_one", new=MagicMock(return_value=None)),
            patch.object(m, "_run_sync", side_effect=eg),
        ):
            r = m.connect_all(
                {"s": McpServerConfig(name="s", url="http://h", transport="sse")}
            )
        assert r["s"] == "error: Connection refused"
