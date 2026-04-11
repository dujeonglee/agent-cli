# OpenHarness 벤치마크 분석

> Source: https://github.com/HKUDS/OpenHarness
> Date: 2026-04-07
> 목적: agent-cli에 접목할 수 있는 패턴과 기능 분석

## 1. 아키텍처 비교

| 차원 | OpenHarness | Agent-CLI | 차이 |
|------|-------------|----------|------|
| 구조 | 10개 서브시스템 (engine, tools, skills, coordinator, memory, plugins, permissions, channels, mcp, ui) | 8개 모듈 (tools, skills, context, providers, parsing, render, agents, hooks) | OH가 더 세분화 |
| 도구 | 클래스 기반 (BaseTool + Pydantic) | 함수 기반 (TOOLS dict) | 타입 안전성 차이 |
| 멀티에이전트 | Coordinator 패턴 + TeamRegistry | ad-hoc delegate + threading | 구조화 수준 차이 |
| 통신 | 10+ 채널 (Slack, Discord, Telegram 등) | 터미널 전용 | OH가 멀티플랫폼 |
| MCP | 완전 구현 (stdio/HTTP/WebSocket) | 없음 | 큰 격차 |
| 샌드박스 | 다층 퍼미션 (경로 규칙, 명령 패턴, 모드) | 없음 | 큰 격차 |
| 스트리밍 | 이벤트 기반 (6개 이벤트 타입) | render 모듈 기반 출력 | 아키텍처 차이 |

## 2. Tools 시스템

### OpenHarness 방식
```python
class BashTool(BaseTool):
    name = "bash"
    description = "Execute shell commands"
    input_model = BashInput  # Pydantic model

    async def execute(self, input: BashInput, context: ToolExecutionContext) -> ToolResult:
        ...
```
- `BaseTool` 추상 클래스 상속
- `input_model`로 Pydantic 자동 스키마 생성
- `ToolRegistry`에 동적 등록
- `ToolExecutionContext`로 실행 메타데이터 전달
- MCP 도구를 `McpToolAdapter`로 래핑

### Agent-CLI 현재 방식
```python
TOOLS = {
    "shell": tool_shell,        # 단순 함수
    "read_file": tool_read_file,
}

def tool_shell(args: dict) -> ToolResult:
    ...
```
- 함수 기반, dict 등록
- 스키마는 `registry.py`에 별도 정의
- 수동 입력 검증

### 접목 가능성
- **Pydantic input model**: 스키마 자동 생성 + 검증 통합. 현재 수동 JSON schema 정의를 대체
- **ToolExecutionContext**: 도구 실행 시 세션 정보, 퍼미션 등 전달. 현재 kwargs로 전달하는 것을 구조화
- **MCP 도구 어댑터**: 외부 MCP 서버의 도구를 동적으로 agent-cli에 통합

## 3. Skills 시스템

### OpenHarness 확장 메타데이터
```yaml
---
name: code-review
description: Review code for quality issues
tools: [read_file, shell]
model: claude-sonnet-4-6              # 모델 오버라이드
effort: high                          # 실행 수준
memory: session                       # 메모리 범위
isolation: process                    # 프로세스 격리
mcpServers: [code-search]             # MCP 의존성
---
```

### Agent-CLI 현재 메타데이터
```yaml
---
name: optimize
description: Analyze source code for optimization
allowed-tools: [read_file, shell, write_file]
max-turns: 0
argument-hint: "<path>"
---
```

### 접목 가능성
- **model override**: 스킬별 최적 모델 지정 (이미 Skill 모델에 model 필드 존재, 활용도 확대)
- **isolation mode**: 스킬 실행 시 프로세스 격리 (현재 같은 프로세스 내 실행)
- **MCP 의존성 선언**: 스킬이 필요한 MCP 서버를 명시, 자동 연결

## 4. Swarm / Multi-Agent

### OpenHarness Coordinator 패턴
```
Coordinator (이해 + 종합)
    ├→ Worker A (제한된 도구: bash, files)
    ├→ Worker B (제한된 도구: web, search)
    └→ Worker C (제한된 도구: code analysis)
         └→ 결과를 Coordinator에게 TaskNotification으로 전달
```
- `AgentDefinition` Pydantic 모델로 에이전트 정의
- `TeamRegistry`로 팀 상태 관리 (에이전트 추가/메시지 전송)
- 7개 빌트인 에이전트 (general, Explore, Plan, worker, verification 등)
- 3가지 실행 모드: local_agent, remote_agent, in_process_teammate

### Agent-CLI 현재 방식
```
Main Loop
    ├→ delegate(agent=explorer, task="분석해줘")  # 동일 프로세스 스레드
    └→ delegate(tasks=[...])                       # 병렬 스레드
```
- ad-hoc 위임 (TOOLS dict의 delegate 도구)
- 에이전트는 .md 파일로 정의 (role_prompt + allowed-tools)
- 스레드 기반 병렬 실행
- agent_stack으로 재귀 방지

### 접목 가능성
- **빌트인 에이전트 라이브러리**: Explore, Plan, Verify 등 범용 에이전트 제공 (현재 explorer만 존재)
- **TeamRegistry**: 에이전트 팀 상태 영속화, 세션 간 팀 구성 유지
- **Worker 도구 제한**: Coordinator가 Worker에게 제한된 도구만 제공 (보안 강화)
- **Remote agent 실행**: 원격 프로세스/머신에서 에이전트 실행

## 5. Coordinator / Orchestration

### OpenHarness
- Coordinator가 작업을 이해하고 Worker에게 구체적 구현 지시
- Worker는 제한된 도구만 사용
- `TaskNotification` 시스템으로 결과 전달 (task_id, status, summary, content, usage)
- XML envelope로 안정적 통신
- 병렬 실행 강조 ("launch independent workers concurrently")

### Agent-CLI 접목 방향
- **TaskNotification 패턴**: delegate 결과를 구조화된 notification으로 전달 (현재 ToolResult.output 텍스트)
- **Coordinator 모드**: 특정 스킬/에이전트를 coordinator로 지정, 하위 worker 관리
- **Worker 도구 제한**: delegate 시 parent가 worker에게 허용 도구를 명시적으로 제한

## 6. Channels / Communication

### OpenHarness
- `BaseChannel` 추상화 + 10개 구현 (Telegram, Discord, Slack, Feishu, DingTalk, Email, QQ, Matrix, WhatsApp, Mochat)
- `ChannelManager`로 채널 오케스트레이션
- `MessageBus`로 이벤트 라우팅
- `InboundMessage`/`OutboundMessage` 타입
- 채널별 인증 (토큰, 앱 ID, SMTP 등)

### Agent-CLI 현재
- 터미널 전용 (stdin/stdout)
- render 모듈로 출력 포맷팅

### 접목 가능성
- **Channel 추상화**: 터미널 외 Slack/Discord 통합 가능성 (장기 목표)
- **MessageBus 패턴**: 내부 이벤트 시스템 (hook 확장)
- **InboundMessage/OutboundMessage**: 구조화된 입출력 (현재 dict 기반)

## 7. MCP (Model Context Protocol)

### OpenHarness 구현
```python
class McpClientManager:
    """Manages MCP client connections."""
    transport_types: stdio, HTTP, WebSocket
    
    async def connect(self, config: McpServerConfig) -> McpClient
    async def list_tools(self, server: str) -> list[McpToolInfo]
    async def call_tool(self, server: str, name: str, args: dict) -> Any
```
- `.mcp.json` 또는 manifest에서 서버 설정 로드
- `ListMcpResourcesTool`, `ReadMcpResourceTool`로 리소스 접근
- `McpToolAdapter`로 MCP 도구를 ToolRegistry에 동적 래핑
- Lazy 초기화 (`__getattr__` 패턴)

### Agent-CLI 현재
- MCP 미지원

### 접목 가능성 (높은 우선순위)
- **MCP 클라이언트**: `.mcp.json`에서 서버 설정 로드 → stdio/HTTP 연결
- **동적 도구 래핑**: MCP 서버의 도구를 agent-cli TOOLS에 자동 등록
- **리소스 접근**: MCP 리소스를 read_file처럼 접근 가능
- **구현 난이도**: 중간 — MCP Python SDK 사용하면 비교적 간단

## 8. Sandbox / Isolation

### OpenHarness 다층 퍼미션
```python
class PermissionChecker:
    """Multi-layer permission system."""
    
    def check(self, tool: str, input: dict) -> PermissionDecision:
        # 1. Explicit denials (최우선)
        # 2. Explicit allowances
        # 3. Path rules (glob 패턴)
        # 4. Command patterns (deny-list)
        # 5. Mode-specific logic (FULL_AUTO / PLAN / DEFAULT)
```
- 3가지 모드: FULL_AUTO, PLAN (확인 필요), DEFAULT
- 경로 기반 규칙 (glob 패턴)
- 명령 deny-list (`rm -rf /` 등 차단)
- 도구별 `is_read_only()` 선언
- `PermissionDecision`에 이유 포함

### Agent-CLI 현재
- 퍼미션 시스템 없음 (모든 허용된 도구 신뢰)
- Hook 시스템 (PreToolUse/PostToolUse)으로 제한적 제어 가능

### 접목 가능성
- **PLAN 모드**: 파일 수정/삭제 시 사용자 확인 요청 (Hook 확장으로 구현 가능)
- **경로 규칙**: `.agent-cli/permissions.json`에 허용/차단 경로 정의
- **명령 deny-list**: 위험한 shell 명령 차단 (`rm -rf`, `sudo` 등)
- **구현 난이도**: 낮음 — 기존 Hook 시스템 위에 구축 가능

## 9. Context Management 비교

### OpenHarness
- `ConversationMessage` 타입 + content blocks (Text/ToolUse/ToolResult)
- `CostTracker`로 턴별 비용 추적
- `QueryContext`로 토큰 제약 전달
- 턴 제한 (per-query)

### Agent-CLI 현재
- FIFO 캐시 (deque N=100) + history.jsonl
- JSON → 자연어 변환 (`thought: ...\naction: ...`)
- 대략적 토큰 추정 (chars/4)
- Context Recovery Guide (system prompt)

### 비교
- OH가 메시지 구조화 수준이 높음 (content blocks)
- agent-cli가 영속화 측면에서 우수 (history.jsonl + session subdir)
- OH가 비용 추적 구현 (agent-cli 미구현)

## 10. 우선순위별 접목 로드맵

### Tier 1: 높은 영향, 중간 난이도
| 항목 | 설명 | 예상 효과 |
|------|------|----------|
| **MCP 클라이언트** | stdio/HTTP MCP 서버 연결 + 동적 도구 래핑 | 외부 도구 생태계 통합 |
| **퍼미션 시스템** | 경로 규칙 + 명령 deny-list + PLAN 모드 | 보안 강화, 사고 방지 |
| **빌트인 에이전트** | Plan, Verify, CodeReview 등 범용 에이전트 | 즉시 사용 가능한 팀 |

### Tier 2: 중간 영향, 낮은 난이도
| 항목 | 설명 | 예상 효과 |
|------|------|----------|
| **스킬 메타 확장** | model override, isolation mode, MCP 의존성 | 스킬 유연성 |
| **Coordinator 패턴** | 전담 coordinator + worker 도구 제한 | 복잡한 작업 분해 |
| **비용 추적** | 턴별 토큰 사용량 + 비용 계산 | 운영 가시성 |

### Tier 3: 장기 목표
| 항목 | 설명 | 예상 효과 |
|------|------|----------|
| **Channel 시스템** | Slack/Discord 통합 | 멀티플랫폼 |
| **클래스 기반 도구** | BaseTool + Pydantic 전환 | 코드 품질 |
| **Remote agent** | 원격 프로세스 실행 | 수평 확장 |
| **Plugin 시스템** | 동적 확장 로딩 | 생태계 구축 |

## 11. 핵심 인사이트

### Agent-CLI가 이미 잘하는 것
- **history.jsonl 영속화**: OH보다 명확한 세션 파일 구조
- **delegate subdir**: 실행 과정 + 결과를 디렉토리 단위로 관리
- **자연어 변환**: JSON → LLM-friendly 변환이 독창적
- **FIFO 단순성**: 복잡한 압축 없이 깔끔한 context 관리
- **stop_event 전파**: 중첩 실행 중단이 잘 동작

### OpenHarness에서 배울 것
- **구조화 수준**: 도구/에이전트/스킬 모두 Pydantic 모델 기반
- **퍼미션**: 다층 보안이 프로덕션에 필수
- **MCP**: 외부 도구 생태계 통합이 경쟁력
- **Coordinator**: 복잡한 작업을 체계적으로 분해하는 패턴

### 접목하지 않을 것
- **10+ 채널 통합**: on-premise CLI 도구에 불필요한 복잡성
- **React/Ink TUI**: 현재 render 모듈로 충분
- **AsyncIO 전환**: threading 기반이 on-premise LLM 환경에서 더 단순

## 12. Built-in 도구/에이전트/스킬 상세 비교

### Tools 비교

| 카테고리 | OpenHarness | Agent-CLI | 비고 |
|---------|------------|----------|------|
| **파일 I/O** | Bash, Read, Write, Edit, Glob, Grep (6) | shell, read_file, write_file, edit_file (4) | Glob/Grep 별도 도구 없음 |
| **검색** | WebFetch, WebSearch, ToolSearch, LSP (4) | fetch (1) | WebSearch, LSP 없음 |
| **에이전트** | Agent, SendMessage, Skill (3) | delegate, run_skill (2) | SendMessage 없음 |
| **팀 관리** | TeamCreate, TeamDelete (2) | 없음 | |
| **태스크 관리** | TaskCreate/Get/List/Update/Stop (5) | 없음 | 작업 추적 도구 부재 |
| **MCP** | MCPTool, ListMcpResources, ReadMcpResource (3) | 없음 | MCP 미지원 |
| **스케줄링** | CronCreate/List/Delete, RemoteTrigger (4) | 없음 | |
| **모드 전환** | EnterPlanMode, ExitPlanMode, Worktree (3) | 없음 | PlanMode 미지원 |
| **유틸리티** | Config, Brief, Sleep, AskUser (4) | ask, complete, ready_for_review, read_context (4) | 다른 종류 |
| **노트북** | NotebookEdit (1) | 없음 | |
| **합계** | **~35개** | **~12개** | |

#### 눈에 띄는 격차

| 도구 | 효과 | 접목 난이도 |
|------|------|-----------|
| **Glob/Grep** | shell로 대체 가능하나 별도 도구면 LLM이 더 정확하게 사용 | 낮음 |
| **WebSearch** | 인터넷 검색 능력 추가 | 중간 (API 키 필요) |
| **TaskCreate/Update/List** | LLM이 작업을 자기 관리 (TODO 리스트) | 낮음 |
| **PlanMode** | 파일 수정 전 사용자 확인 | 낮음 (Hook 확장) |
| **LSP** | 코드 정의/참조 탐색 | 높음 |
| **SendMessage** | 에이전트 간 비동기 통신 | 중간 |

### Skills 비교

| OpenHarness (7개) | Agent-CLI (4개) | 비고 |
|-------------------|----------------|------|
| commit | - | git 커밋 자동화 |
| debug | - | 디버깅 워크플로 |
| diagnose | - | 문제 진단 |
| plan | plan ✅ | 구현 계획 생성 |
| review | - | 코드 리뷰 |
| simplify | - | 코드 단순화 |
| test | - | 테스트 생성 |
| - | create-skill | 스킬 생성 메타 스킬 |
| - | create-agent | 에이전트 생성 메타 스킬 |
| - | create-team | 팀 구성 메타 스킬 |

#### 격차 분석
- OH의 실용 스킬: **commit, debug, review, test** — 일상 개발 작업에 바로 사용
- Agent-CLI의 메타 스킬: **create-skill, create-agent, create-team** — 시스템 확장에 집중
- **추가 우선순위**: commit > review > test > debug > diagnose > simplify

### Agents 비교

| OpenHarness (~7개) | Agent-CLI (1개) | 비고 |
|-------------------|----------------|------|
| general-purpose | - | 범용 에이전트 |
| Explore | explorer ✅ | 읽기 전용 탐색 |
| Plan | - | 구현 계획 수립 |
| worker | - | 제한된 도구로 작업 실행 |
| verification | - | 작업 결과 검증 |
| statusline-setup | - | 설정 관련 |
| claude-code-guide | - | 가이드/도움말 |

#### 격차 분석
- Agent-CLI는 **explorer 하나**만 존재
- **추가 우선순위**: Plan > verification > worker (Coordinator 패턴과 함께)

### 종합: Agent-CLI가 먼저 추가해야 할 것

**즉시 추가 가능 (낮은 난이도):**
1. ~~Glob/Grep 도구~~ (shell로 대체 가능, 우선순위 낮음)
2. **TaskCreate/Update/List 도구** — LLM 자기 관리 작업 추적
3. **commit 스킬** — git add + commit 자동화
4. **review 스킬** — 코드 리뷰 워크플로
5. **test 스킬** — 테스트 생성 워크플로

**중기 추가 (중간 난이도):**
6. **Plan 에이전트** — 빌트인 계획 수립 에이전트
7. **verification 에이전트** — 작업 검증 에이전트
8. **PlanMode** — 파일 수정 전 확인 모드
9. **WebSearch 도구** — 인터넷 검색
