# Agent-CLI 워크스페이스 분석 보고서

> **작성일**: 2025-06-27  
> **버전**: agent-cli v2.0.0-dev  
> **분석 방식**: 8개 에이전트 병렬 분석 -> 취합 -> 최종 리뷰

---

## 목차

1. [프로젝트 개요](#1-프로젝트-개요)
2. [디렉토리 구조](#2-디렉토리-구조)
3. [핵심 아키텍처](#3-핵심-아키텍처)
4. [모듈별 상세 분석](#4-모듈별-상세-분석)
5. [데이터 흐름](#5-데이터-흐름)
6. [테스트 아키텍처](#6-테스트-아키텍처)
7. [설계 원칙](#7-설계-원칙)
8. [의존성 그래프](#8-의존성-그래프)
9. [참고 문서](#9-참고-문서)

---

## 1. 프로젝트 개요

**Agent-CLI**는 on-premise LLM을 위한 ReAct(Reasoning + Acting) 패턴 기반 에이전트 CLI입니다.

| 항목 | 내용 |
|------|------|
| **버전** | 2.0.0-dev |
| **라이선스** | MIT |
| **Python** | 3.10+ |
| **총 소스 코드** | ~22,800 LOC (89 Python 파일) |
| **총 테스트 코드** | ~27,200 LOC (70 파일, 1,846 테스트) |
| **주요 의존성** | typer, rich, requests, pyyaml, tree-sitter (+ 7개 언어 grammar) |
| **선택적 의존성** | pytest(dev), fastapi/uvicorn(web), gnureadline(macos) |

### 지원 프로바이더

| 프로바이더 | API | 특징 |
|-----------|-----|------|
| **Anthropic** | Messages API | tool_use + thinking + streaming + prompt cache |
| **OpenAI Compat** | OpenAI API | function calling + reasoning_content + streaming (vLLM, LM Studio, mlx-lm 호환) |
| **Ollama** | Ollama API | basic JSON mode + message.thinking + streaming |

### 핵심 설계 결정

- **네이티브 tool calling 미사용** -- 모든 프로바이더가 동일한 ReAct 텍스트 파싱 경로를 거쳐 provider 편차 제거
- **Basic JSON mode만** -- strict JSON Schema는 mlx 엔진 호환성 문제로 기본 비활성
- **프라이버시 계약** -- observability는 구조 메타데이터만, prompt/응답 본문 기록 없음

---

## 2. 디렉토리 구조

```
agent-cli/
+-- agent-cli.py                  # 하위 호환 wrapper (5줄)
+-- pyproject.toml                # 프로젝트 설정 (setuptools)
+-- README.md                     # 사용법 문서
+-- CLAUDE.md                     # Claude용 개발 가이드
|
+-- agent_cli/                    # 메인 패키지
|   +-- __init__.py               # 버전 정보
|   +-- __main__.py               # python -m agent_cli 진입점
|   +-- main.py                   # CLI 명령어 (run/chat/web/setup/sessions)
|   +-- loop.py                   # AgentLoop -- ReAct 패턴 핵심
|   +-- config.py                 # 3레이어 설정 병합
|   +-- constants.py              # 상수 정의
|   +-- setup.py                  # 설정 마법사
|   +-- verbose.py                # 로깅 유틸리티
|   +-- input_history.py          # 입력 히스토리 관리
|   +-- resource_loader.py        # 패키지 리소스 로딩
|   |
|   +-- tools/                    # 도구 시스템
|   |   +-- registry.py           # ToolSchema + 스키마 검증
|   |   +-- read_file.py          # 파일 읽기 (hashline)
|   |   +-- write_file.py         # 파일 생성/덮어쓰기
|   |   +-- edit_file.py          # hashline 기반 정밀 편집
|   |   +-- shell.py              # 셸 명령 실행
|   |   +-- fetch.py              # 웹 페이지 fetch
|   |   +-- delegate.py           # 서브에이전트 위임
|   |   +-- code_index.py         # 코드 인덱서 wrapper
|   |   +-- context.py            # 세션 이력 조회
|   |   +-- result.py             # ToolResult 타입
|   |   +-- action_summary.py     # 도구 인자 요약
|   |   +-- _diff.py              # colored diff
|   |
|   +-- code_index/               # tree-sitter 코드 인덱서
|   |   +-- builder.py            # Pass-1/Pass-2 빌드 파이프라인
|   |   +-- store.py              # IndexStore (SQLite 리더)
|   |   +-- schema.py             # Symbol/Ref dataclass
|   |   +-- callgraph.py          # 함수 간 호출 그래프
|   |   +-- slice.py              # LLM-context 마크다운 렌더러
|   |   +-- preproc.py            # C/C++ 전처리 파이프라인
|   |   +-- languages/            # 9개 언어 walker
|   |       +-- python.py, javascript.py, typescript.py
|   |       +-- c.py, cpp.py, go.py, rust.py, java.py, markdown.py
|   |
|   +-- context/                  # 컨텍스트 관리
|   |   +-- manager.py            # ContextManager (핵심)
|   |   +-- session.py            # 세션 메타데이터
|   |   +-- overflow.py           # 오버플로우 감지
|   |   +-- token_estimator.py    # 토큰 추정 (chars/4)
|   |   +-- _file_extract.py      # 파일 경로 추출
|   |
|   +-- providers/                # LLM 프로바이더
|   |   +-- base.py               # LLMProvider Protocol
|   |   +-- compat.py             # ModelCapabilities + 런타임 감지
|   |   +-- http.py               # post_with_retry
|   |   +-- anthropic.py          # Anthropic Messages API
|   |   +-- openai_compat.py      # OpenAI 호환 API
|   |   +-- ollama.py             # Ollama API
|   |
|   +-- wire_formats/             # LLM 응답 형식 플러그인
|   |   +-- base.py               # WireFormat ABC
|   |   +-- react.py              # JSON {thought, action, action_input}
|   |   +-- prefix_md.py          # Markdown heading 섹션
|   |   +-- _format_rules_builder.py
|   |
|   +-- recovery/                 # 에러 복구 시스템
|   |   +-- detectors.py, primitives.py, intervention.py
|   |   +-- common_recovery.py, wf_recovery.py, observability.py
|   |
|   +-- hooks/                    # 라이프사이클 훅
|   |   +-- events.py, context.py, loader.py, runner.py, shell.py
|   |
|   +-- render/                   # 렌더링 시스템
|   |   +-- base.py, minimal.py, web.py
|   |
|   +-- web/                      # 웹 UI
|   |   +-- server.py             # FastAPI + SSE 서버
|   |
|   +-- skills/                   # 스킬 시스템
|   |   +-- models.py, loader.py, executor.py
|   |
|   +-- mcp/                      # MCP 통합
|   |   +-- config.py, client.py, adapter.py
|   |
|   +-- agents/                   # 에이전트 정의
|   +-- prompts/                  # 시스템 프롬프트
|
+-- tests/                        # 테스트 스위트 (70 파일, 1,846 테스트)
|   +-- conftest.py, test_*.py    # 유닛 테스트 (50개)
|   +-- code_index/               # 코드 인덱서 테스트 (20개)
|
+-- docs/                         # 설계 문서
|   +-- ARCHITECTURE.md, code-index/, context-compaction/
|   +-- context-redesign/, robust-harness/, hook-redesign/
|   +-- mcp-integration/, web-fixes-3/
|
+-- .agent-cli/                   # 런타임 설정 (agents/skills/sessions)
+-- .claude/                      # Claude Code 설정
```

---

## 3. 핵심 아키텍처

### 3.1 ReAct 패턴

```
+-----------------------------------------------------------------+
|                    AgentLoop.run()                               |
|                                                                 |
|  +----------+    +-----------+    +------------------+          |
|  |  _call    |-->|  Parse    |-->|  Recovery        |          |
|  |  _llm()   |    |  (3-stage)|    |  Detection       |          |
|  +----------+    +-----------+    +--------+---------+          |
|       ^                          +--------|              |
|       |                          |        v              |
|       |                 +--------+------+                |
|       |                 |  _dispatch    |                |
|       |                 |  _tool()      |                |
|       |                 +--------+------+                |
|       |                          |                       |
|       |                 +--------+------+                |
|       |                 |  ctx.add()    |                |
|       |                 |  (history)    |                |
|       +-----------------+---------------+                |
|                                                                 |
|  while _should_continue():                                     |
|    _begin_turn() -> _call_llm() -> parse -> recovery           |
|    -> dispatch -> ctx.add() -> render                          |
+-----------------------------------------------------------------
```

### 3.2 핵심 클래스

| 클래스 | 파일 | 역할 |
|--------|------|------|
| `AgentLoop` | `loop.py` (~1,735 LOC) | ReAct 패턴의 핵심 -- LLM 호출, 파싱, 도구 디스패치, 복구 |
| `ContextManager` | `context/manager.py` (~659 LOC) | 대화 컨텍스트 관리, 토큰 예산, 압축, 영속성 |
| `IndexStore` | `code_index/store.py` | SQLite 코드 인덱스 리더 |
| `Renderer` (ABC) | `render/base.py` (~320 LOC) | 출력 추상화 (19개 출력 메서드 + 2개 입력 메서드) |
| `WireFormat` (ABC) | `wire_formats/base.py` (~414 LOC) | LLM 응답 형식 플러그인 |
| `HookRunner` | `hooks/runner.py` | 라이프사이클 훅 실행 |
| `McpClientManager` | `mcp/client.py` | MCP 서버 연결 관리 |

---

## 4. 모듈별 상세 분석

### 4.1 진입점 & CLI

**파일**: `agent-cli.py`, `agent_cli/__main__.py`, `agent_cli/main.py` (~1,701 LOC)

| 진입점 | 설명 |
|--------|------|
| `agent-cli.py` | 하위 호환 wrapper -- `agent_cli.main:app` 호출 (5줄) |
| `python -m agent_cli` | `__main__.py` -> `main:app` |
| `agent-cli` (pip install 후) | `pyproject.toml`의 `[project.scripts]` 등록 |

**CLI 명령어**:

| 명령어 | 설명 |
|--------|------|
| `agent-cli run "task"` | 단발 실행 (세션 자동 저장) |
| `agent-cli chat` | 대화형 REPL (graceful interrupt, /help, /clear, /sh 등) |
| `agent-cli web` | LAN 웹 UI (FastAPI + SSE, optional dep) |
| `agent-cli setup` | 설정 마법사 |
| `agent-cli sessions` | 이전 세션 목록 |

**Dispatcher** (`try_dispatch_agent_or_skill`):
- `@<agent>` 접두사 -> 에이전트 디스패치
- `/<skill>` 접두사 -> 스킬 디스패치
- `/sh <command>` -> 직접 셸 명령 (LLM 우회)

### 4.2 에이전트 루프 (ReAct)

**파일**: `agent_cli/loop.py` (~1,735 LOC)

`AgentLoop` 클래스가 ReAct 패턴의 핵심입니다:

```
AgentLoop.run()
  +-- _setup() -> 시스템 프롬프트 빌드 + 컨텍스트 초기화
  +-- while _should_continue():
  |   +-- _begin_turn() -> 턴 렌더링
  |   +-- _call_llm() -> LLM 호출 (오버플로 시 compaction/FIFO 재시도)
  |   +-- _handle_text_path() -> 3단계 파싱 + 도구 디스패치
  +-- graceful interrupt 처리
```

**3단계 파싱 폴백**:
1. `json.loads` -- 표준 JSON 파싱
2. `json_repair` -- 6단계 JSON 복구 (깨진 JSON 자동 수리)
3. regex 추출 -- JSON이 아닌 경우 regex로 thought/action/action_input 추출

**Recovery Layer**:
- NO_JSON, NO_ACTION, NO_THOUGHT, UNKNOWN_TOOL, SCHEMA_MISMATCH, NESTED_ENVELOPE, ACTION_LOOP 감지
- B1 Action Loop: 같은 (action, args)가 연속 2회 이상이면 단계적 개입 (probe_progress -> restate_task -> hard-fail)
- Per-turn observability: `turns.jsonl`에 구조 메타데이터만 기록 (프라이버시 계약)

### 4.3 도구 시스템

**파일**: `agent_cli/tools/` (총 13개 파일)

**실제 도구**:

| 도구 | 파일 | 설명 |
|------|------|------|
| `read_file` | `read_file.py` (278 LOC) | 파일 읽기 + hashline 포맷팅 + stat/search/부분읽기 모드 + 대용량 가드 |
| `write_file` | `write_file.py` | 파일 생성/덮어쓰기 + colored diff |
| `edit_file` | `edit_file.py` | hashline 기반 정밀 편집 + 퍼지 매칭 + multi-edit 안전장치 |
| `shell` | `shell.py` | 셸 명령 실행 + 위험 명령 확인 (rm/rmdir/mv) |
| `fetch` | `fetch.py` | 웹 페이지 fetch -> 마크다운 변환 |
| `read_context` | `context.py` | 세션 이력 조회 (list/search/fetch) |
| `code_index` | `code_index.py` | tree-sitter SQLite 코드 인덱서 wrapper (10 mode dispatch) |

**가상 도구 (loop 인터셉트)**:

| 도구 | 설명 |
|------|------|
| `complete` | 작업 완료 신호 -- 루프 종료 |
| `ask` | 사용자에게 질문 (대화형 전용) |
| `run_skill` | 스킬 실행 -- skill subdir 생성 |
| `ready_for_review` | 자기 검증 -- 원본 query 반환 |
| `delegate` | in-process 서브에이전트 위임 (병렬 지원) |

**Hashline 시스템**: CRC32 기반 2-char 해시 태그로 정밀 파일 편집. 해시 불일치 시 퍼지 매칭으로 비용 제로 보정.

**레지스트리 패턴** (`registry.py`):
- `ToolSchema` dataclass -- 모든 도구의 JSON Schema 정의
- `validate_tool_input()` -- 스키마 기반 입력 검증 + 자동 타입 변환
- `TOOLS` dict (이름 -> 함수 매핑) + `_execute_tool()` 디스패치 함수

### 4.4 코드 인덱서

**파일**: `agent_cli/code_index/` (~5,000 LOC, tree-sitter 기반 SQLite 코드 인덱싱 시스템)

**핵심 기능**:
- 소스 파일의 심볼(함수, 타입, 변수, 상수, 섹션)과 참조(호출, 이름, 타입)를 SQLite에 인덱싱
- 함수 간 호출 그래프(call graph) 구축
- LLM 컨텍스트용 마크다운 "slice" 렌더링
- 9개 언어 지원 (C, C++, Python, Go, Rust, Java, JavaScript, TypeScript, Markdown)

**빌드 파이프라인** (`builder.py`):
1. 파일 분류 -- `os.walk`로 루트 디렉토리 순회, sha1 비교 -> `reused` vs `changed` 분류
2. Pass-1 (심볼 정의 추출) -- 변경된 파일만, tree-sitter로 파싱 후 `walk_definitions()` 호출
3. Pass-2 (참조 추출) -- 변경된 파일 + 영향받는 파일, `walk_refs()` 호출
4. SQLite 작성 -- `executemany`로 bulk insert

**SQLite 스키마** (SCHEMA_VERSION=2):
- `meta` 테이블: 스키마 버전, 루트 경로, 빌드 시간, preproc fingerprint
- `files` 테이블: path, size, lines, sha1, has_error, n_symbols, identifiers(JSON), language
- `symbols` 테이블: id, name, qualified_name, kind, file, line, col, end_line, is_definition, language, kind_raw, modifiers, parent, signature, return_type, enum_values, params
- `refs` 테이블: id, name, kind, file, line, col, language

**C/C++ 전처리** (`preproc.py`): 14단계 regex rewrite 체인 + `unifdef -b` 실행. 라인 번호 보존.

**Lazy grammar import**: 각 언어별 tree-sitter Parser 인스턴스 캐싱. Python-only 프로젝트는 Rust/C++ grammar wheel을 로드하지 않음.

### 4.5 컨텍스트 관리

**파일**: `agent_cli/context/` (총 1,008 LOC)

| 모듈 | 역할 |
|------|------|
| `manager.py` (~659 LOC) | `ContextManager` -- 토큰 budget 압축 + FIFO fallback + history.jsonl 영속화 |
| `token_estimator.py` | 토큰 추정 (chars/4) |
| `overflow.py` | 프로바이더별 오버플로 감지 (15개 regex 패턴) |
| `session.py` | 세션 메타데이터 + resume용 user/assistant 페어 추출 |
| `_file_extract.py` | compaction 시 evict 묶음에서 touched file paths 추출 |

**2-Tier Compaction**:
1. 캐시가 budget의 90%를 넘으면 LLM 요약 compaction 시도 (system anchor 보존 -> oldest 절반 evict -> 단일 호출로 recursive 요약 -> `[system][summary][file_list][retained]` 재구성)
2. 요약 실패 또는 재구성 후 미충족 시 belt-and-braces FIFO drop
3. `--no-compaction` / `AGENT_CLI_COMPACTION=off`로 완전히 비활성화 가능

**영속성**:
- `history.jsonl`: append-only JSON Lines 파일
- `compaction.json`: 압축 상태의 원자적 저장 (temp + rename)
- `dynamic_start_index`: resume 시 이미 요약된 영역 중복 방지

### 4.6 LLM 프로바이더

**파일**: `agent_cli/providers/`

| 모듈 | 역할 |
|------|------|
| `base.py` | `LLMProvider` Protocol, `LLMResponse`, `TokenUsage` |
| `compat.py` (~419 LOC) | `ModelCapabilities` + 런타임 프로브 감지 (thinking + format) + 자동 저장 |
| `http.py` | `post_with_retry` -- Timeout/ConnectionError 재시도 (고정 1초 백오프) |
| `anthropic.py` | Anthropic Messages API (tool_use + thinking + streaming + prompt cache) |
| `openai_compat.py` | OpenAI 호환 API (function calling + reasoning_content + streaming) |
| `ollama.py` | Ollama API (basic JSON mode + message.thinking + streaming) |

**중요 설계 결정**: 네이티브 tool calling을 **사용하지 않음**. 모든 프로바이더가 동일하게 ReAct 텍스트 파싱을 거쳐 provider 편차를 제거합니다.

**프로바이더 팩토리** (`__init__.py`):
```python
def create_provider(provider, base_url, api_key):
    if provider == "anthropic": return AnthropicProvider(...)
    elif provider == "openai": return OpenAICompatProvider(...)
    elif provider == "ollama": return OllamaProvider(...)
```

### 4.7 와이어 포맷

**파일**: `agent_cli/wire_formats/`

LLM 응답 형식을 추상화하는 플러그인 시스템:

| 플러그인 | 형식 |
|----------|------|
| `react.py` (~655 LOC) | 기본 -- JSON `{thought, action, action_input}` + 3단계 파서 |
| `prefix_md.py` (~436 LOC) | 실험 -- `## Thought / ## Action / ## Input` 마크다운 섹션 |

**설계**: `WireFormat` ABC가 lifecycle default를 제공. 새 plugin은 format-specific abstract method만 구현하면 main code 변경 없이 작동. 폐기는 파일 삭제 + 등록 줄 제거로 끝.

### 4.8 복구 시스템 (Recovery)

**파일**: `agent_cli/recovery/`

**4-Layer 구조**:
1. Provider Layer -- LLMResponse 반환
2. Parse Layer -- 3-stage fallback (json.loads -> json_repair -> regex)
3. Detection Layer (`detectors.py`) -- FailureSignal 감지
4. Recovery Layer (`primitives.py` + intervention builders) -- Intervention 적용

**실패 카탈로그**:

| ID | 이름 | 감지 |
|---|---|---|
| A1a | JSON 부재 (prose만) | parser stage 0 + 내용 있음 |
| A1b | 응답 비어있음 | parser stage 0 + 빈 문자열 |
| A3 | action 필드 누락 | parser 성공 but action is None |
| A4 | 알 수 없는 tool name | `detect_unknown_tool()` |
| A5 | action_input 스키마 불일치 | `detect_schema_mismatch()` |
| A6 | Nested envelope (이중 래핑) | `detect_nested_envelope()` |
| A7 | thought 필드 누락 | `detect_thought_missing()` |
| B1 | Action loop | `ActionLoopDetector` (stateful) |

**Recovery Primitives** (`primitives.py`):
- `echo_prior_output()` -- 모델의 이전 출력을 거울처럼 인용 (최대 400자 head-truncate)
- `probe_progress()` -- B1 level 1: "이 호출 N번째, 다른 행동 취해"
- `restate_task()` -- B1 level 2: 원본 task 재고정 + 진단 질문

**Observability** (`observability.py`): `TurnRecord` -- 매 턴의 구조적 메타데이터만 기록 (LLM 생성 텍스트나 사용자 프롬프트는 포함 안 함)

### 4.9 훅 시스템 (Hooks)

**파일**: `agent_cli/hooks/`

**11개 라이프사이클 이벤트**:

| 카테고리 | 이벤트 |
|---|---|
| 세션 | `OnSessionStart`, `OnSessionEnd` |
| 턴 | `PreLLMCall`, `PostLLMCall`, `OnTurnEnd` |
| 도구 | `PreToolUse`, `PostToolUse` |
| Delegate | `OnDelegateStart`, `OnDelegateEnd` |
| Skill | `OnSkillStart`, `OnSkillEnd` |

**HookContext**:
- 읽기 전용 상태: event, session_dir, turn, messages, tool_name, tool_input, tool_result 등
- Context 조작: `inject_message()`, `inject_system_section()`, `remove_system_section()`
- PreToolUse 제어: `block(reason)`, `modify_input(new_input)`
- MCP Memory 래핑: `store_memory()`, `search_memory()`, `read_memory()`

**Python Hook** (`loader.py`): `.agent-cli/hooks/*.py` 파일 스캔, `EVENTS` 리스트로 이벤트 매핑
**Shell Hook** (`shell.py`): `hooks.json` 기반, exit code 0=allow, 2=block

### 4.10 렌더링 & 웹 UI

**파일**: `agent_cli/render/`, `agent_cli/web/`

**렌더링 시스템** (`render/`):

| 모듈 | 역할 |
|------|------|
| `base.py` (~320 LOC) | `Renderer` ABC -- 출력 메서드 19개 + 입력 메서드 2개 |
| `minimal.py` (~600 LOC) | CLI 렌더러 -- nested depth, markdown, ASCII-art streaming, CJK+Ambiguous width |
| `web.py` (~680 LOC) | 웹 렌더러 -- SSE 이벤트 버퍼 + snapshot replay + parallel delegate 가시성 |

**웹 서버** (`web/server.py`):
- FastAPI + uvicorn + sse-starlette
- 단일 active client (takeover 모델)
- Vanilla JS 프론트엔드 (의존성 0) -- 자체 markdown 파서 + XSS 안전
- worker thread <-> async main thread 간 큐 기반 동기화

### 4.11 스킬 시스템

**파일**: `agent_cli/skills/`

Claude Code 호환 프롬프트 스킬:
- YAML frontmatter + Markdown 본문
- `$ARGUMENTS`, `$0`, `$1` 인자 치환
- 검색 경로: 프로젝트 로컬 -> 사용자 전역 -> 패키지 내장
- 내장 스킬: `create-skill`, `create-agent`, `plan`, `create-team`
- 템플릿 변수 대체: `${SKILL_DIR}`, ``!`cmd```
- 도구 교차, 독립 컨텍스트 포크 지원

### 4.12 MCP 통합

**파일**: `agent_cli/mcp/`

Model Context Protocol 지원:
- `config.py` -- mcp.json 로드/병합 (프로젝트 > 유저)
- `client.py` -- `McpClientManager` (stdio/SSE 연결, 도구 호출)
- `adapter.py` -- MCP 도구 -> ToolResult 래핑, TOOLS dict 등록
- `{server}.{tool}` 네임스페이스로 agent-cli 도구 레지스트리에 통합

### 4.13 에이전트 시스템

**파일**: `agent_cli/agents/`

- Markdown 파일 기반 에이전트 정의 (YAML frontmatter + 역할 본문)
- 검색 경로: `.agent-cli/agents/` -> `~/.agent-cli/agents/` -> `agent_cli/agents/builtin/`
- `allowed-tools`, `model` 오버라이드 지원

### 4.14 설정 시스템

**파일**: `agent_cli/config.py`

3레이어 병합 (낮은 -> 높은 우선순위):
1. 환경변수 (`AGENT_CLI_*`)
2. `~/.agent-cli/config.json` (사용자 전역)
3. `.agent-cli/config.json` (워크스페이스)
4. CLI 파라미터 (임시 오버라이드)

**models.json**: 3단계 검색 (프로젝트 로컬 -> 사용자 전역 -> 패키지 기본값) + 런타임 자동 감지

---

## 5. 데이터 흐름

```
사용자 입력
  |
  v
main.py (CLI 명령어)
  |
  +-- _setup_provider() -> config.json + models.json + 런타임 감지
  +-- _setup_mcp() -> MCP 서버 연결 + 도구 등록
  +-- ContextManager 생성 -> 토큰 budget 계산
  |
  v
loop.py (AgentLoop.run())
  |
  +-- build_system_prompt() -> Role + Guidelines + Format Rules + Tools + Environment
  +-- while 루프:
  |   +-- _call_llm() -> providers/{name}.py -> LLMResponse
  |   +-- wire_format.parse() -> ParsedAction (3단계 폴백)
  |   +-- recovery 감지 -> Intervention (필요 시 retry)
  |   +-- _dispatch_tool_with_hooks() -> tools/ -> ToolResult
  |   +-- ctx.add() -> history.jsonl + in-memory cache
  |
  v
ToolResult -> render -> 사용자에게 출력
```

---

## 6. 테스트 아키텍처

**총 70 테스트 파일, 1,846 테스트**

### 테스트 분류

| 분류 | 파일 수 | 테스트 수 | 설명 |
|------|---------|----------|------|
| **코어 기능** | 50 | ~1,700 | 유닛 테스트 |
| **코드 인덱서** | 20 | ~100 | 언어별/기능별 테스트 |
| **통합 테스트** | 2 | ~46 | LLM 연동 테스트 |
| **전체** | **70** | **~1,846** | |

### 코어 테스트 (`tests/test_*.py`)

| 테스트 파일 | 범위 |
|-------------|------|
| `test_loop.py` | AgentLoop 핵심 동작 |
| `test_providers.py` | 프로바이더 팩토리, 호출 |
| `test_providers_retry.py` | HTTP 리트라이 |
| `test_config.py` | 설정 병합 |
| `test_context_manager.py` | 컨텍스트 관리 |
| `test_context_compaction.py` | 컨텍스트 압축 |
| `test_registry.py` | 도구 레지스트리 |
| `test_fuzzy_edit.py` | 퍼지 편집 |
| `test_react_parser.py` | ReAct 파서 |
| `test_json_repair.py` | JSON 복구 |
| `test_action_loop_detector.py` | 액션 루프 감지 |
| `test_recovery_primitives.py` | 복구 원시 함수 |
| `test_intervention.py` | 인터벤션 빌더 |
| `test_retry_builders.py` | 리트라이 빌더 |
| `test_observability.py` | 관찰성 |
| `test_overflow.py` | 오버플로우 감지 |
| `test_token_estimator.py` | 토큰 추정 |
| `test_session.py` | 세션 관리 |
| `test_hooks.py`, `test_hooks_python.py` | 훅 시스템 |
| `test_render.py`, `test_renderer_system.py` | 렌더링 |
| `test_web_renderer.py`, `test_web_server.py` | 웹 UI |
| `test_wire_formats_*.py` | 와이어 포맷 (base/react/prefix_md) |
| `test_skills.py`, `test_skill_executor.py` | 스킬 시스템 |
| `test_builtin_skills.py`, `test_builtin_agents.py` | 내장 스킬/에이전트 |
| `test_delegate_agent.py`, `test_delegate_output.py` | 위임 |
| `test_dispatch_agent_or_skill.py` | 디스패처 |
| `test_mcp.py` | MCP 통합 |
| `test_system_prompt.py` | 시스템 프롬프트 |
| `test_compat.py` | ModelCapabilities |
| `test_streaming.py` | 스트리밍 |
| `test_fetch.py` | 웹 fetch |
| `test_tools_coverage.py` | 도구 커버리지 |
| `test_tools_diff.py` | diff 도구 |
| `test_input_history.py` | 입력 히스토리 |
| `test_multiline_input.py` | 멀티라인 입력 |
| `test_setup.py` | 설정 마법사 |
| `test_resource_loader.py` | 리소스 로더 |
| `test_app_markdown.py` | Markdown 앱 |
| `test_import_cycles.py` | 순환 의존성 검사 |
| `test_action_summary.py` | 액션 요약 |

### 코드 인덱서 테스트 (`tests/code_index/test_*.py`)

| 테스트 파일 | 범위 |
|-------------|------|
| `test_builder.py` | 빌드 파이프라인 |
| `test_store.py` | IndexStore 쿼리 |
| `test_callgraph.py` | 호출 그래프 |
| `test_slice.py` | Slice 렌더링 |
| `test_preproc.py` | C/C++ 전처리 |
| `test_path_normalize.py` | 경로 정규화 |
| `test_post_hook.py` | 빌드 후 훅 |
| `test_property_invariants.py` | 속성 불변식 |
| `test_property_walkers.py` | 워커 속성 |
| `test_tool_dispatch.py` | 도구 디스패치 |
| `test_tool_hashline.py` | Hashline 도구 |
| `test_tool_on_demand.py` | On-demand 파싱 |
| `test_python.py` | Python 워커 |
| `test_c.py` | C 워커 |
| `test_cpp.py` | C++ 워커 |
| `test_go.py` | Go 워커 |
| `test_rust.py` | Rust 워커 |
| `test_java.py` | Java 워커 |
| `test_js_ts.py` | JavaScript/TypeScript 워커 |
| `test_markdown.py` | Markdown 워커 |

### 통합 테스트

| 테스트 파일 | 범위 |
|-------------|------|
| `test_integration.py` | 실제 LLM 연동 테스트 (ollama_integration marker) |
| `test_integration_builtin.py` | 내장 에이전트/스킬 연동 |

**통합 테스트 모델**: `qwen3-coder:30b`, `glm-4.7-flash:q8_0`, `qwen3.5:35b`

**테스트 설정** (`pyproject.toml`):
- `pytest` + `pytest-asyncio` (strict mode)
- `httpx` (HTTP 테스트)
- `hypothesis` (속성 기반 테스트)
- `ollama_integration` marker로 실제 LLM 테스트 분리

---

## 7. 설계 원칙

1. **순환 의존 없음** -- 단방향 흐름: config -> compat -> base -> adapters -> loop -> main
2. **Wire format plugin화** -- LLM 응답 형식을 main code와 완전히 분리
3. **Recovery primitive 계약** -- provider/모델/채널 이름을 절대 참조하지 않음
4. **2-tier 컨텍스트 관리** -- LLM 요약 compaction -> FIFO belt-and-braces fallback
5. **네이티브 tool calling 미사용** -- 모든 프로바이더가 동일한 ReAct 텍스트 파싱 경로
6. **Basic JSON mode만** -- strict JSON Schema는 mlx 엔진 호환성 문제로 기본 비활성
7. **프라이버시 계약** -- observability는 구조 메타데이터만, prompt/응답 본문 기록 없음
8. **Hashline 기반 정밀 편집** -- CRC32 해시 + 퍼지 매칭으로 LLM 재호출 없이 보정
9. **실패는 1급 시민** -- 예외가 아니라 정상 경로의 분기 (Recovery Layer)
10. **Failure grounding** -- 모델의 출력을 거울처럼 보여주면 자기 보고 자기 고침

---

## 8. 의존성 그래프

```
main.py
  +-- config.py (설정)
  +-- loop.py (에이전트 루프)
  |   +-- providers/ (LLM 호출)
  |   |   +-- base.py (Protocol)
  |   |   +-- compat.py (ModelCapabilities)
  |   |   +-- http.py (HTTP 리트라이)
  |   |   +-- anthropic.py, openai_compat.py, ollama.py
  |   +-- wire_formats/ (응답 파싱)
  |   |   +-- base.py (ABC)
  |   |   +-- react.py, prefix_md.py
  |   +-- recovery/ (에러 복구)
  |   |   +-- detectors.py, primitives.py, intervention.py
  |   |   +-- common_recovery.py, wf_recovery.py, observability.py
  |   +-- tools/ (도구)
  |   |   +-- registry.py, result.py
  |   |   +-- read_file.py, write_file.py, edit_file.py, shell.py
  |   |   +-- fetch.py, delegate.py, code_index.py, context.py
  |   +-- context/ (컨텍스트 관리)
  |   |   +-- manager.py, session.py, overflow.py
  |   |   +-- token_estimator.py, _file_extract.py
  |   +-- hooks/ (라이프사이클 훅)
  |   |   +-- events.py, context.py, loader.py, runner.py, shell.py
  |   +-- render/ (출력)
  |   |   +-- base.py, minimal.py, web.py
  |   +-- prompts/ (시스템 프롬프트)
  |   +-- skills/ (스킬)
  |   +-- mcp/ (MCP 통합)
  +-- web/ (웹 UI)
  +-- code_index/ (코드 인덱서)
  |   +-- builder.py, store.py, schema.py
  |   +-- callgraph.py, slice.py, preproc.py
  |   +-- languages/ (9개 언어 walker)
```

---

## 9. 참고 문서

| 문서 | 설명 |
|------|------|
| `docs/ARCHITECTURE.md` | 전체 아키텍처 문서 (최종 업데이트: 2026-05-25) |
| `docs/code-index/DESIGN.md` | 코드 인덱서 설계 (572줄) |
| `docs/context-compaction/DESIGN.md` | 컨텍스트 압축 설계 |
| `docs/context-compaction/REQUIREMENTS.md` | 컨텍스트 압축 요구사항 |
| `docs/context-compaction/TEST_PLAN.md` | 컨텍스트 압축 테스트 계획 |
| `docs/context-redesign/DESIGN.md` | 컨텍스트 리디자인 설계 |
| `docs/context-redesign/IMPLEMENTATION_PLAN.md` | 컨텍스트 리디자인 구현 계획 |
| `docs/context-redesign/REMAINING_DEBT.md` | 컨텍스트 리디자인 잔여 기술 부채 |
| `docs/robust-harness/DESIGN.md` | Recovery Layer 설계 |
| `docs/robust-harness/REMAINING_DEBT.md` | Recovery Layer 잔여 기술 부채 |
| `docs/hook-redesign/DESIGN.md` | 훅 시스템 리디자인 설계 |
| `docs/mcp-integration/DESIGN.md` | MCP 통합 설계 |
| `docs/web-fixes-3/DESIGN.md` | 웹 UI 수정 설계 |
| `docs/web-fixes-3/REQUIREMENTS.md` | 웹 UI 수정 요구사항 |
| `docs/web-fixes-3/TEST_PLAN.md` | 웹 UI 수정 테스트 계획 |
| `README.md` | 설치, 사용법, 환경변수, 모델 권장 사양 |
| `CLAUDE.md` | Claude용 개발 가이드 |

---

## 부록: 분석 방법론

본 보고서는 다음 과정을 통해 생성되었습니다:

1. **디렉토리 구조 파악**: `find` 명령어로 전체 파일 구조 분석
2. **8개 에이전트 병렬 분석**:
   - Task 1: 진입점 & CLI (main.py, loop.py, config.py, setup.py 등)
   - Task 2: 도구 시스템 (tools/ 디렉토리)
   - Task 3: 코드 인덱서 (code_index/ 디렉토리)
   - Task 4: 컨텍스트 관리 (context/ 디렉토리)
   - Task 5: LLM 프로바이더 & 시스템 프롬프트 (providers/, prompts/)
   - Task 6: 복구 시스템 & 훅 시스템 (recovery/, hooks/)
   - Task 7: 렌더링 & 웹 UI & 와이어 포맷 & 스킬 & MCP (render/, web/, wire_formats/, skills/, mcp/)
   - Task 8: 테스트 아키텍처 (tests/)
3. **취합**: 모든 분석 결과를 통합
4. **최종 리뷰**: 완전성, 정확성, 구조, 가독성 검토
