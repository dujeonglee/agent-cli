# Web Server, Rendering, Wire Format, Skills, MCP 분석 보고서

## 1. 개요

이 문서는 agent-cli의 웹 서버, 렌더링 시스템, 와이어 포맷 시스템, 스킬 시스템, MCP 통합 모듈의 설계와 상호작용을 분석한 보고서입니다.

---

## 2. 렌더링 시스템 (`agent_cli/render/`)

### 2.1 아키텍처

렌더링 시스템은 **Renderer 추상 베이스 클래스**를 중심으로 플러그인 가능한 구조를 가집니다.

```
Renderer (ABC)                    ← agent_cli/render/base.py
  ├── MinimalRenderer             ← agent_cli/render/minimal.py (CLI 기본)
  └── WebRenderer                 ← agent_cli/render/web.py (웹 UI)
```

### 2.2 Renderer 추상 베이스 (`base.py`)

**핵심 책임:**
- **출력**: LLM의 thought, action, observation, final answer 등을 렌더링
- **입력**: `prompt_user()`, `confirm()`을 통해 사용자 입력을 받음
- **중첩 렌더링**: `push_depth()`/`pop_depth()`로 skill/delegate의 중첩 컨텍스트 지원
- **스레드 로컬 캡처**: `start_capture()`/`stop_capture()`로 병렬 delegate의 출력을 버퍼링

**추상 메서드 (구현 필수):**
| 메서드 | 역할 |
|--------|------|
| `header()` | 세션/스킬 시작 배너 |
| `turn_sep()` | 턴 구분선 |
| `thought()` | LLM 추론/생각 |
| `action()` | 도구 호출 |
| `observation()` | 도구 결과 |
| `final()` | 최종 답변 |
| `error()` | 오류 메시지 |
| `raw()` | 원시 LLM 응답 (verbose) |
| `status()` | 상태 업데이트 |
| `model_detected()` | 모델 정보 감지 |
| `model_loaded()` | 모델 로드 |
| `context_dump()` | 컨텍스트 덤프 |
| `spinner_start/stop()` | 스피너 애니메이션 |
| `dispatch_progress()` | 디스패치 진행도 |
| `prompt_user()` | 사용자 텍스트 입력 |
| `confirm()` | 다중 선택 확인 |

**선택적 메서드 (기본 no-op):**
- `thinking()`: provider별 추론 콘텐츠 (예: Ollama Qwen3)
- `stream_chunk()`/`stream_end()`: 스트리밍 렌더링
- `group_start()`/`group_end()`: 중첩 블록 (skill/delegate)
- `begin_delegate_task()`/`end_delegate_task()`: 병렬 delegate 가시성

### 2.3 MinimalRenderer (`minimal.py`)

**특징:**
- Rich 라이브러리를 사용한 CLI 렌더링
- 아이콘 기반 출력 (💭 thought, ⚡ action, ✅ final, ✗ error)
- `rich.Live`를 사용한 스피너 애니메이션
- 동적 폭 계산으로 터미널 리사이즈 안전
- 동양어(Ambiguous width) 문자 처리
- `group_start()`/`group_end()`에서 `┌─`/`└─` 블록 표시
- 스트리밍 시 실시간 텍스트 페인팅 (터미널 폭 추적)

### 2.4 WebRenderer (`web.py`)

**아키텍처:**
```
AgentLoop (worker thread)              FastAPI / uvicorn (main thread, async)
────────────────────────              ───────────────────────────────────
renderer.thought(...)                  
  → _emit("assistant_turn", ...) ─→ event_buffer (persistent)
                                      ↓
                                    conn.queue (per active SSE)
                                      ↓
                                     SSE endpoint pulls and yields

renderer.prompt_user(...)
  → _emit("input_required") ────→ (SSE pushes form to client)
  → input_queue.get() (blocks worker thread)
                                          ↑
                                    POST /api/input puts here
```

**핵심 메커니즘:**
- **SSE 이벤트 스트리밍**: `_emit()` → `event_buffer`(영구) + `conn.queue`(실시간)
- **단일 활성 클라이언트**: `register_connection()`에서 takeover 처리
- **영구 버퍼**: 재연결 시 스냅샷 리플레이
- **사용자 입력**: `input_queue`를 통해 worker thread ↔ FastAPI 간 동기화
- **delegate 가시성**: `begin_delegate_task()`/`end_delegate_task()`로 thread_id → task_id 매핑

**SSE 이벤트 타입:**
| 이벤트 | 역할 |
|--------|------|
| `assistant_turn` | LLM 응답 (thought + action/final) |
| `observation` | 도구 결과 |
| `error` | 오류 |
| `status` | 상태 업데이트 |
| `spinner` | 로딩 표시 |
| `stream_chunk`/`stream_end` | 스트리밍 |
| `group_start`/`group_end` | 중첩 블록 |
| `input_required` | 사용자 입력 요청 |
| `takeover` | 연결 인수 |
| `prune` | 컨텍스트 정리 |
| `delegate_task_start`/`end` | 병렬 delegate |

---

## 3. 웹 서버 (`agent_cli/web/server.py`)

### 3.1 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `GET` | `/` | 정적 UI (index.html) |
| `GET` | `/api/health` | 라이브니스 프로브 (무인증) |
| `GET` | `/api/stream` | SSE 이벤트 스트림 (token 인증) |
| `POST` | `/api/input` | 사용자 입력 제출 (token 인증) |
| `POST` | `/api/abort` | 현재 prompt_user/confirm 중단 |

### 3.2 인증
- `token` 쿼리 파라미터로 인증
- `secrets.token_urlsafe(32)`로 자동 생성 또는 `--token`으로 수동 지정
- `secrets.compare_digest()`로 상수시간 비교 (타이밍 사이드채널 방지)

### 3.3 아키텍처

```
WebServer
  ├── renderer: WebRenderer
  ├── _chat_queue: SimpleQueue (worker loop용)
  ├── token: str
  ├── push_chat() / pop_chat() / shutdown()  ← worker loop 인터페이스
  └── stream_events()  ← SSE async generator
```

**Worker Loop 패턴:**
1. `push_chat(message)` → `_chat_queue.put()`
2. Worker thread: `pop_chat()` → `AgentLoop.run()` → 반복
3. `shutdown()` → `SHUTDOWN` sentinel 푸시 → worker 종료

### 3.4 슬래시 명령어
- `/help`: 웹 명령어 목록
- `/sh <cmd>`: 직접 셸 실행 (LLM 우회)
- `@agents`/`@<agent>`/`/skills`/`/<skill>`: `agent_cli.main.try_dispatch_agent_or_skill()`으로 라우팅

### 3.5 FIFO 동기화
- 매 턴 후 renderer의 영구 이벤트 수와 ContextManager 캐시 크기 비교
- 불일치 시 `prune` 이벤트 브로드캐스트 → 프론트엔드에서 동일한 접두사 제거

---

## 4. 와이어 포맷 시스템 (`agent_cli/wire_formats/`)

### 4.1 아키텍처

와이어 포맷은 **LLM 응답의 온더와이어(on-the-wire) 형태**를 정의합니다. 시스템 프롬프트 규칙, 파서, 복구 메시지, 프리필, provider 특이사항을 하나의 모듈에 번들링합니다.

```
WireFormat (ABC)                      ← agent_cli/wire_formats/base.py
  ├── ReActFormat                     ← agent_cli/wire_formats/react.py (기본)
  └── PrefixMdFormat                  ← agent_cli/wire_formats/prefix_md.py
```

**레지스트리 패턴:**
- 각 플러그인은 모듈 하단에서 `register(PluginInstance())` 호출
- `get(name)`으로 이름 기반 조회
- `list_names()`로 사용 가능한 포맷 목록
- 빌트인 플러그인은 `_register_builtin_plugins()`에서 자동 등록

### 4.2 WireFormat ABC (`base.py`)

**추상 메서드:**
| 메서드 | 역할 |
|--------|------|
| `name` | 포맷 식별자 |
| `parse_response(raw, tools)` | LLM 응답 파싱 → `ParsedAction` |
| `format_rules_anchor()` | 포맷 설명 문장 |
| `format_rules_field_specific()` | 필드별 규칙 (Rules 1-2) |
| `render_full_example()` | 완전 예시 렌더링 |
| `static_retry_hint_parse_fail()` | 파싱 실패 재시도 힌트 |
| `static_retry_hint_no_action()` | 액션 없음 재시도 힌트 |
| `system_user_prefixes()` | 시스템 주입 메시지 접두사 |

**선택적 오버라이드:**
| 메서드 | 역할 |
|--------|------|
| `format_rules()` | `## Response Format` 섹션 구성 (기본: `_format_rules_builder` 사용) |
| `normalize_assistant_for_messages()` | 메시지 버퍼용 응답 정규화 |
| `serialize_assistant_for_history()` | 히스토리 직렬화 |
| `render_assistant_from_history()` | 히스토리에서 렌더링 |
| `render_action_input()` | 액션 입력 렌더링 |
| `provider_call_kwargs()` | provider 호출 인자 (예: `format="json"`) |
| `prefill()` | assistant-turn 프리필 |

### 4.3 ParsedAction (`base.py`)

포맷 독립적 파싱 결과:
```python
@dataclass
class ParsedAction:
    thought: str | None          # 추론 텍스트
    action: str | None           # 도구 이름
    action_input: str | None     # 도구 인자 (JSON)
    tool_name: str | None        # 파싱된 도구 이름
    tool_input: dict | None      # 파싱된 도구 인자
    truncated: bool = False      # 응답 잘림 여부
```

### 4.4 ReActFormat (`react.py`)

**와이어 형태:**
```json
{"thought": "...", "action": "tool_name", "action_input": {...}}
```

**특징:**
- JSON 기반 포맷
- `provider_call_kwargs()`에서 `format="json"` 반환 (Ollama JSON 모드 활성화)
- `prefill()`에서 `{"thought": "` 프리필 제공
- `render_action_input()`에서 JSON pretty-print
- ReAct 전용 복구: `NO_THOUGHT` 케이스 처리

### 4.5 PrefixMdFormat (`prefix_md.py`)

**와이어 형태:**
```markdown
## Thought
<free reasoning, multi-line OK>

## Action
<tool_name on its own line>

## Input
{"<arg>": "<value>", ...}
```

**특징:**
- 마크다운 ATX H2 헤딩 기반
- 작은 모델이 XML/JSON보다 마크다운 헤딩을 더 잘 따름
- `provider_call_kwargs()`에서 Ollama JSON 모드 비활성화 요청 (PREFIX-MD는 `## `로 시작)
- `## Thought`/`## Action`/`## Input` 헤딩을 엄격히 매칭

### 4.6 Format Rules Builder (`_format_rules_builder.py`)

**목적:** 모든 와이어 포맷 플러그인의 `## Response Format` 섹션을 일관되게 구성

**공유 요소:**
- `COMPLETION_INTRO`: `ready_for_review` → `complete` 완료 패턴
- `SHARED_RULES_TAIL`: Rules 3-6 (오류 수정, 단일 액션, 효율성, 언어)
- `SCHEMA_EXAMPLE_INPUT`, `READY_FOR_REVIEW_EXAMPLE_INPUT`, `COMPLETE_EXAMPLE_INPUT`: 공유 예시 입력

**플러그인 계약:**
- `format_rules_anchor()`: 포맷 설명
- `render_full_example()`: 예시 렌더링 (동일 입력, 다른 출력)
- `format_rules_field_specific()`: Rules 1-2

**측정 비교 가능성:** 모든 플러그인이 동일한 `(thought, action, action_input)` 삼중주를 받지만 렌더링만 다름 → 프롬프트 비교 실험에서 동일한 의도 보장

---

## 5. 스킬 시스템 (`agent_cli/skills/`)

### 5.1 아키텍처

```
Skill (dataclass)                    ← agent_cli/skills/models.py
  ↑
load_skills()                        ← agent_cli/skills/loader.py
  ↑
execute_skill()                      ← agent_cli/skills/executor.py
```

### 5.2 Skill 데이터 모델 (`models.py`)

```python
@dataclass
class Skill:
    name: str                          # 스킬 이름
    description: str                   # 설명
    prompt_template: str               # 프롬프트 템플릿 (마크다운 본문)
    allowed_tools: list[str] | None    # 허용 도구 (None = 전체)
    max_turns: int                     # 최대 턴 (0 = 기본값)
    argument_hint: str                 # 인자 힌트
    model: str | None                  # 전용 모델 (None = 호출자 모델)
    context: str | None                # "fork" = 독립 컨텍스트
    hooks: dict | None                 # 훅 매처
    disable_model_invocation: bool     # LLM 자동 호출 금지
    user_invocable: bool               # /skills 메뉴 표시 여부
    source_path: str                   # 소스 파일 경로
```

### 5.3 스킬 로더 (`loader.py`)

**검색 경로 (우선순서):**
1. `.agent-cli/skills/*.md` (프로젝트 로컬, 평면)
2. `.agent-cli/skills/<name>/SKILL.md` (프로젝트 로컬, 디렉토리)
3. `~/.agent-cli/skills/*.md` (사용자 글로벌, 평면)
4. `~/.agent-cli/skills/<name>/SKILL.md` (사용자 글로벌, 디렉토리)
5. `agent_cli/skills/builtin/*` (패키지 빌트인)

**특징:**
- `ResourceLoader`를 사용한 파일 발견 + frontmatter 파싱
- 매 호출마다 디스크 재스캔 (세션 중 스킬 추가 시 재시작 불필요)
- YAML frontmatter 필수 (없으면 스킵)
- 프로젝트 로컬이 사용자 글로벌을 오버라이드

### 5.4 스킬 실행기 (`executor.py`)

**템플릿 변수 대체:**
| 변수 | 설명 |
|------|------|
| `$ARGUMENTS` | 전체 인자 문자열 |
| `$ARGUMENTS[N]` | N번째 인자 (0-indexed) |
| `$0`, `$1`, ... | 인자 단축어 |
| `${SKILL_DIR}` | 스킬 디렉토리 경로 |
| `${CLAUDE_SKILL_DIR}` | Claude Code 호환 별칭 |
| `${SESSION_ID}` | 세션 ID |
| `` !`command` `` | 셸 명령어 동적 주입 |

**실행 흐름:**
1. 도구 교차: `skill.allowed_tools ∩ parent_tools`
2. 템플릿 변수 대체
3. `context="fork"` 시 독립 `ContextManager` 생성
4. `run_loop()` 호출 (skill의 `max_turns`, `model` 적용)
5. 훅 병합: `merge_hooks_configs(parent_hooks_config, skill.hooks)`
6. 결과 `result.md` 저장

---

## 6. MCP 통합 (`agent_cli/mcp/`)

### 6.1 아키텍처

```
McpServerConfig (dataclass)          ← agent_cli/mcp/config.py
  ↑
load_mcp_config()                     ← agent_cli/mcp/config.py
  ↑
McpClientManager                      ← agent_cli/mcp/client.py
  ↑
wrap_mcp_tool() / register_mcp_tools() ← agent_cli/mcp/adapter.py
```

### 6.2 MCP 설정 (`config.py`)

**검색 경로:**
1. `~/.agent-cli/mcp.json` (사용자 글로벌)
2. `.agent-cli/mcp.json` (프로젝트 로컬, 우선순위 높음)

**McpServerConfig:**
```python
@dataclass
class McpServerConfig:
    name: str
    command: str          # stdio 전송
    args: list[str]       # stdio 전송
    env: dict[str, str]   # 환경 변수
    url: str              # SSE 전송
    transport: str        # "stdio" 또는 "sse"
```

**특징:**
- `${VAR}` 환경 변수 대체 지원
- 프로젝트 설정이 사용자 설정을 오버라이드
- `mcpServers` 키에서 서버 구성 읽음

### 6.3 MCP 클라이언트 (`client.py`)

**McpClientManager:**
- 각 서버별 독립 세션 관리
- stdio/SSE 전송 지원
- `mcp` Python SDK 사용
- 동기 래퍼: `_run
- async → sync 변환

**연결 흐름:**
1. `connect_all(configs)` → 각 서버별 `_connect_one()`
2. `_connect_stdio()` / `_connect_sse()` → `mcp.ClientSession` 초기화
3. `session.initialize()` → `session.list_tools()` → 도구 목록 캐싱
4. `disconnect_all()` → 세션/transport 정리

**도구/리소스 연
작:**
- `list_tools(server?)`: 도구 목록 조회
- `call_tool(server, tool_name, args)`: 도구 호출 (동기)
- `list_resources(server)`: 리소스 목록 조회
- `read_resource(server, uri)`: 리소스 읽기

### 6.4 MCP 어댑터 (`adapter.py`)

**목적:** MCP 도구를 agent-cli의 `ToolResult` 함수로 감싸 TOOLS 딕셔너리에 등록

**핵심 함수:**
| 함수 | 역할 |
|------|------|
| `wrap_mcp_tool()` | MCP 도구 호출을 `ToolResult`로 감쌈 |
| `register_mcp_tools()` | 모든 연결된 MCP 도구를 `{server}.{tool}` 이름으로 등록 |
| `build_mcp_tool_descriptions()` | 시스템 프롬프트용 도구 설명 텍스트 생성 |

**도구 네임스페이스:** `{server_name}.{tool_name}` (예: `filesystem.read_file`)

**에러 처리:** MCP 호출 실패 시 `ToolResult(False, error="MCP {server}.{tool} failed: {e}")`

---

## 7. 시스템 간 상호작용

### 7.1 렌더링 ↔ 웹 서버

```
WebServer.create_app()
  ├── WebRenderer 생성
  ├── GET /api/stream → WebServer.stream_events() → WebRenderer.register_connection()
  ├── POST /api/input → WebRenderer.push_user_input() / push_user_message()
  └── POST /api/abort → WebRenderer.push_abort()
```

**데이터 흐름:**
1. AgentLoop (worker thread)에서 `renderer.thought()` 호출
2. WebRenderer._emit() → event_buffer + conn.queue
3. SSE endpoint에서 conn.queue polling → 클라이언트에 전송
4. 클라이언트에서 입력 → POST /api/input → WebRenderer.input_queue
5. WebRenderer.prompt_user()에서 input_queue.get() → worker thread 차단 해제

### 7.2 와이어 포맷 ↔ 렌더링

와이어 포맷과 렌더링은 **직접적으로 연결되지 않습니다**. 와이어 포맷은 LLM 응답 파싱을 담당하고, 렌더링은 파싱 결과를 표시합니다.

**간접 연결:**
- `ParsedAction` → `render_step("action", ...)` → `renderer.action()`
- `ParsedAction.thought` → `render_step("thought", ...)` → `renderer.thought()`
- `ToolResult` → `render_step("observation", ...)` → `renderer.observation()`

### 7.3 스킬 ↔ 렌더링

스킬 실행 중 렌더링은 **중첩 깊이**로 표시됩니다:

```
execute_skill()
  → render_group_start(skill.name)
  → renderer.push_depth()
  → run_loop()  ← 모든 렌더링이 depth+1로 표시
  → renderer.pop_depth()
  → render_group_end(skill.name)
```

### 7.4 MCP ↔ 도구 시스템

MCP 도구는 agent-cli의 도구 레지스트리에 통합됩니다:

```
load_mcp_config() → McpClientManager.connect_all()
  → register_mcp_tools() → {"server.tool": function}
  → TOOLS dict에 병합
  → 시스템 프롬프트에 도구 설명 추가
  → AgentLoop에서 일반 도구와 동일하게 호출
```

### 7.5 종합 데이터 흐름

```
┌─────────────────────────────────────────────────────────────┐
│                     System Prompt Builder                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ WireFormat   │  │ Skills       │  │ MCP Tools        │  │
│  │ .format_rules│  │ .prompt_     │  │ .tool_descriptions│  │
│  │              │  │ template     │  │                  │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘  │
│         └─────────────────┼───────────────────┘              │
│                    system_prompt                             │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                      AgentLoop                               │
│                                                              │
│  LLM Call ──→ raw_response ──→ WireFormat.parse_response()  │
│                                    │                         │
│                                    ▼                         │
│                              ParsedAction                    │
│                                    │                         │
│                    ┌───────────────┼───────────────┐         │
│                    ▼               ▼               ▼         │
│              render.thought()  Tool Call     render.final()  │
│                           │                         │        │
│                           ▼                         │        │
│                    ToolResult ──────────────────────┘        │
│                           │                                  │
│                           ▼                                  │
│                    render.observation()                      │
│                                                              │
│  Renderer: MinimalRenderer (CLI) or WebRenderer (Web)       │
└─────────────────────────────────────────────────────────────┘
```

---

## 8. 설계 원칙 요약

### 8.1 플러그인 아키텍처
- **Renderer**: ABC 기반, `set_renderer()`로 교체
- **WireFormat**: 레지스트리 기반, `register()`로 등록
- **Skills**: 파일 기반 발견, YAML frontmatter + 마크다운 본문
- **MCP**: JSON 설정 기반, 서버별 독립 세션

### 8.2 관심사 분리
- **와이어 포맷**: LLM 응답의 형태 정의 (파싱 + 프롬프트 규칙)
- **렌더링**: 사용자 인터페이스 출력 (CLI/Web)
- **웹 서버**: HTTP/SSE 전송 계층
- **스킬**: 재사용 가능한 프롬프트 템플릿
- **MCP**: 외부 도구 통합

### 8.3 확장성
- 새 렌더러: `Renderer` 상속 + `agent_cli/render/<name>.py` 추가
- 새 와이어 포맷: `WireFormat` 상속 + `register()` 호출
- 새 스킬: `.agent-cli/skills/<name>/SKILL.md` 추가
- 새 MCP 서버: `.agent-cli/mcp.json`에 추가

---

## 9. 분석된 파일 목록

| 파일 | 라인 수 | 역할 |
|------|---------|------|
| `agent_cli/render/base.py` | 319 | Renderer ABC, ConfirmOption |
| `agent_cli/render/minimal.py` | 603 | CLI 렌더러 |
| `agent_cli/render/web.py` | 710 | 웹 SSE 렌더러 |
| `agent_cli/render/__init__.py` | 269 | 렌더러 팩토리/전역 상태 |
| `agent_cli/web/server.py` | 489 | FastAPI 웹 서버 |
| `agent_cli/wire_formats/base.py` | 414 | WireFormat ABC, ParsedAction |
| `agent_cli/wire_formats/react.py` | 656 | ReAct JSON 포맷 |
| `agent_cli/wire_formats/prefix_md.py` | 437 | PREFIX-MD 마크다운 포맷 |
| `agent_cli/wire_formats/_format_rules_builder.py` | 105 | 공유 포맷 규칙 빌더 |
| `agent_cli/wire_formats/__init__.py` | 134 | 와이어 포맷 레지스트리 |
| `agent_cli/skills/models.py` | 22 | Skill 데이터 모델 |
| `agent_cli/skills/loader.py` | 96 | 스킬 파일 로더 |
| `agent_cli/skills/executor.py` | 211 | 스킬 실행기 |
| `agent_cli/skills/__init__.py` | 8 | 스킬 패키지 리엑스포트 |
| `agent_cli/mcp/config.py` | 109 | MCP 서버 설정 로더 |
| `agent_cli/mcp/client.py` | 259 | MCP 클라이언트 매니저 |
| `agent_cli/mcp/adapter.py` | 97 | MCP 도구 어댑터 |
| `agent_cli/mcp/__init__.py` | 2 | MCP 패키지 |

---

*분석일: 2025-05-25*
*분석 범위: web, render, wire_formats, skills, MCP 모듈*
