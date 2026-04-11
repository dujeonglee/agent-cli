# MCP Integration 설계

> Status: Draft
> Date: 2026-04-12

## 1. 개요

MCP (Model Context Protocol) 서버 연결을 통해 외부 도구와 리소스를 agent-cli에 통합.
LLM이 빌트인 도구와 동일하게 MCP 도구를 사용할 수 있게 함.

## 2. 설계 원칙

- **MCP 도구 = 일반 도구**: Available Tools에 동일하게 표시, 동일하게 호출
- **자동 연결**: 세션 시작 시 mcp.json 기반 자동 spawn/connect
- **네임스페이스**: `{server}.{tool}` 형식으로 충돌 방지
- **SDK 사용**: `mcp` Python 패키지 (직접 구현 X)

## 3. Configuration

### 3.1 파일 위치 및 우선순위

```
1. .agent-cli/mcp.json          (프로젝트 로컬) — 최우선
2. ~/.agent-cli/mcp.json        (유저 전역)
```

동일 서버 이름이 양쪽에 있으면 프로젝트가 이김.

### 3.2 파일 포맷

```json
{
  "mcpServers": {
    "code-search": {
      "command": "npx",
      "args": ["-y", "@anthropic/mcp-code-search"],
      "env": {
        "INDEX_DIR": "/tmp/index"
      }
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_TOKEN": "${GITHUB_TOKEN}"
      }
    },
    "remote-api": {
      "url": "http://localhost:8080",
      "transport": "sse"
    }
  }
}
```

서버 타입:
- **stdio**: `command` + `args` + `env` — 로컬 프로세스 spawn
- **SSE**: `url` + `transport: "sse"` — HTTP SSE 원격 연결

`env`의 `${VAR}` 문법: 환경 변수 참조.

## 4. Context Window 통합

### 4.1 도구 (Tools)

system prompt의 Available Tools에 빌트인 도구와 함께 표시:

```
## Available Tools
- read_file: 파일 읽기...
- shell: 셸 명령 실행...
- github.list_issues: List GitHub issues for a repository
- github.create_pr: Create a pull request
- code-search.search: Search code by semantic similarity
```

네이밍: `{server_name}.{tool_name}`

LLM 호출:
```json
{"thought": "이슈 목록을 확인하겠다", "action": "github.list_issues", "action_input": {"repo": "owner/repo"}}
```

### 4.2 리소스 (Resources)

`read_resource` 도구로 접근:
```json
{"action": "read_resource", "action_input": {"uri": "github://owner/repo/issues"}}
```

리소스 목록은 `/mcp resources <server>` 명령어로 조회.

## 5. 연결 흐름

### 5.1 세션 시작 시

```
세션 시작
  │
  ├─ mcp.json 로드
  │   ├─ .agent-cli/mcp.json (프로젝트)
  │   └─ ~/.agent-cli/mcp.json (유저, 프로젝트에 없는 서버만)
  │
  ├─ 각 서버 연결 (자동)
  │   ├─ stdio: subprocess spawn → initialize handshake
  │   └─ sse: HTTP 연결 → initialize handshake
  │
  ├─ 도구 목록 조회 (tools/list)
  │   └─ {server}.{tool} 형식으로 TOOLS에 등록
  │
  ├─ system prompt 빌드 (MCP 도구 포함)
  └─ 대화 시작
```

연결 실패 시: 경고 출력, 해당 서버 건너뜀. 나머지 정상 진행.

### 5.2 도구 실행 시

```
LLM: {"action": "github.list_issues", "action_input": {"repo": "owner/repo"}}
  │
  ├─ action에서 server_name, tool_name 분리
  │   "github.list_issues" → server="github", tool="list_issues"
  │
  ├─ McpClientManager에서 해당 서버 client 조회
  │
  ├─ tools/call JSON-RPC 요청
  │   {"method": "tools/call", "params": {"name": "list_issues", "arguments": {...}}}
  │
  ├─ 응답 → ToolResult로 변환
  │
  └─ observation으로 LLM에 전달 (일반 도구와 동일)
```

### 5.3 세션 종료 시

```
세션 종료 / Ctrl+C
  │
  └─ 모든 MCP 서버 graceful shutdown
      ├─ stdio: 프로세스 종료
      └─ sse: 연결 close
```

## 6. CLI 명령어

```
/mcp                          서버 상태 보기 (연결/미연결)
/mcp connect <server>         서버 수동 연결
/mcp disconnect <server>      서버 연결 해제
/mcp tools <server>           서버의 도구 목록
/mcp resources <server>       서버의 리소스 목록
```

## 7. 아키텍처

### 7.1 새 파일

```
agent_cli/
├── mcp/
│   ├── __init__.py
│   ├── config.py             ← mcp.json 로드/병합
│   ├── client.py             ← McpClientManager (연결/해제/도구 호출)
│   └── adapter.py            ← MCP 도구를 TOOLS dict에 래핑
```

### 7.2 모듈 책임

**config.py** — mcp.json 파일 탐색, 로드, 병합 (프로젝트 > 유저)
```python
def load_mcp_config() -> dict[str, McpServerConfig]:
    """Load and merge MCP server configs."""
```

**client.py** — MCP 서버 연결/해제/호출 관리
```python
class McpClientManager:
    async def connect_all(configs: dict[str, McpServerConfig])
    async def disconnect_all()
    async def call_tool(server: str, tool: str, args: dict) -> Any
    async def list_tools(server: str) -> list[McpToolInfo]
    async def list_resources(server: str) -> list[McpResourceInfo]
```

**adapter.py** — MCP 도구를 agent-cli ToolResult 인터페이스로 래핑
```python
def wrap_mcp_tool(manager: McpClientManager, server: str, tool: McpToolInfo) -> callable:
    """Wrap MCP tool as agent-cli tool function returning ToolResult."""

def register_mcp_tools(manager: McpClientManager) -> dict[str, callable]:
    """Register all connected MCP tools into TOOLS-compatible dict."""
```

### 7.3 연동 지점

- **main.py**: 세션 시작 시 `McpClientManager.connect_all()` 호출
- **loop.py**: `_execute_single_tool`에서 MCP 도구 실행 (TOOLS dict에 등록되니 자동)
- **system_prompt.py**: `get_tool_descriptions`에서 MCP 도구도 포함 (TOOLS에 있으니 자동)
- **main.py**: `/mcp` 명령어 처리

## 8. MCP SDK 사용

### 8.1 의존성

```
mcp>=1.0.0
```

pyproject.toml에 추가.

### 8.2 주요 SDK API

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

# stdio 연결
async with stdio_client(StdioServerParameters(
    command="npx",
    args=["-y", "@anthropic/mcp-code-search"],
    env={"INDEX_DIR": "/tmp"}
)) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("search", {"query": "auth"})

# SSE 연결
async with sse_client("http://localhost:8080") as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        ...
```

### 8.3 Async 고려

MCP SDK는 async. agent-cli는 sync (threading).
해결: `asyncio.run()` 래퍼로 sync 인터페이스 제공.

```python
def call_tool_sync(server: str, tool: str, args: dict) -> ToolResult:
    return asyncio.run(_call_tool_async(server, tool, args))
```

## 9. 구현 계획

### Phase 1: 기반
- [ ] mcp.json 로드/병합 (config.py)
- [ ] McpClientManager 구현 (client.py)
- [ ] stdio transport 연결/해제
- [ ] tools/list, tools/call 기본 동작
- [ ] 유닛 테스트

### Phase 2: 통합
- [ ] MCP 도구를 TOOLS dict에 동적 등록 (adapter.py)
- [ ] system prompt에 MCP 도구 표시
- [ ] _execute_single_tool에서 MCP 도구 실행
- [ ] 세션 시작 시 자동 연결
- [ ] 세션 종료 시 graceful shutdown
- [ ] 유닛 테스트

### Phase 3: CLI + 리소스
- [ ] /mcp 명령어 (status, connect, disconnect, tools, resources)
- [ ] read_resource 도구 구현
- [ ] SSE transport 지원
- [ ] 환경 변수 치환 (${VAR})
- [ ] integration 테스트

### Phase 4: 정리
- [ ] README 업데이트
- [ ] ARCHITECTURE.md 업데이트
- [ ] ruff + 전체 테스트 통과
