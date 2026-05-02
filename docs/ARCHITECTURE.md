# Agent-CLI v2 아키텍처 문서

> **이 문서는 코드와 함께 유지보수되어야 합니다.**
> 코드 수정 시 관련 섹션을 반드시 업데이트하세요.
>
> 최종 업데이트: 2026-04-25
> 버전: 2.0.0-dev
> 총 소스: 10,974 LOC (56 Python 파일) + 15,180 LOC 테스트 (37 파일)
> 총 테스트: 985 유닛 + 22 통합 (36 ollama_integration deselected)

---

## 1. 프로젝트 개요

Agent-CLI는 on-premise LLM을 위한 모듈형 에이전트 CLI입니다. ReAct(Reasoning + Acting) 패턴으로 LLM이 도구를 사용하여 단계적으로 작업을 수행합니다.

### 핵심 특징

- **멀티 프로바이더**: Anthropic, OpenAI 호환(vLLM, LM Studio, mlx-lm), Ollama
- **3단계 파싱 폴백**: json.loads → JSON repair → regex 추출
- **Basic JSON Mode**: Ollama `format="json"`, OpenAI `response_format={"type":"json_object"}`, Anthropic tool calling (strict JSON Schema는 확장성 위해 미사용)
- **Hashline 편집**: CRC32 해시 기반 정밀 파일 편집 + 퍼지 매칭
- **컨텍스트 관리**: FIFO 메시지 큐 + history.jsonl 영속화 (LLM 압축 제거)
- **모델 적응형**: context window, thinking budget에 따른 자동 조정

### 외부 의존성

| 패키지 | 버전 | 용도 |
|--------|------|------|
| `typer` | >=0.9 | CLI 프레임워크 |
| `rich` | >=13.0 | 터미널 렌더링 (Panel, Table, Rule 등) |
| `requests` | >=2.28 | HTTP 클라이언트 (LLM API 호출) |
| `pyyaml` | >=6.0 | 스킬 frontmatter 파싱 |

표준 라이브러리: json, re, dataclasses, pathlib, os, sys, zlib, textwrap, unicodedata, copy, tempfile, threading

---

## 2. 디렉토리 구조

```
agent_cli/
├── __init__.py              (3)    패키지 버전 (__version__ = "2.0.0-dev")
├── __main__.py              (5)    python -m agent_cli 진입점
├── main.py                  (1200) CLI 명령어: run, chat, setup, sessions, @agent 디스패치, --style, resume preview
├── resource_loader.py       (144)  ResourceLoader — 파일 검색/우선순위 (스킬/에이전트/지시사항)
├── config.py                (217)  config.json 3레이어 로딩 + models.json 레지스트리
├── setup.py                 (281)  SetupWizard (Rich TUI, 첫 실행 설정 마법사 — 기존 config 노출 + 프로브 진행 표시)
├── constants.py             (~45)  공유 상수 (timeout, observation 템플릿, retry hint 정적 메시지, system-injected user prefix). 외부 모듈 의존 없음 — 저층 레이어
├── recovery/                       Robust Harness Recovery Layer (docs/robust-harness/DESIGN.md)
│   ├── __init__.py                 primitive·detector·observability 재export
│   ├── builders.py          (~115) Intervention 합성 factory — `format_no_json_retry` (A1a), `format_no_action_retry` (A1c), `format_action_loop_intervention` (B1). primitives 조합으로 Intervention 생성, 빈 prior_content 시 정적 RETRY_HINT_* fallback
│   ├── detectors.py         (~180) 감지기 모음. stateful: `ActionLoopDetector` (B1, turn 간 (action, args) 추적). stateless: `detect_unknown_tool` (A4), `detect_schema_mismatch` (A5, `validate_tool_input` wrap), `detect_nested_envelope` (A6, complete 결과의 이중 래핑 감지 — 관찰 전용)
│   ├── intervention.py      (~30)  `Intervention` dataclass — primitive 합성 결과 (message + 적용된 primitive 이름)
│   ├── observability.py     (~115) `TurnRecorder` — 세션별 `turns.jsonl` 추가-only writer; `TurnRecord` 스키마(seq, model, parse_stage, failure_signal, primitives_applied). FAILURE_* 라벨 7종 (NO_JSON / NO_OUTPUT / NO_ACTION / UNKNOWN_TOOL / SCHEMA_MISMATCH / NESTED_ENVELOPE / ACTION_LOOP)
│   └── primitives.py        (~120) 순수 회복 primitive (`echo_prior_output`, `constrain_format_json`, `constrain_action_required`, `probe_progress`, `restate_task`) — provider/모델/채널 이름 모름
├── default_models.json             패키지 기본 모델 정의 (6개 모델)
├── hooks/                          Hook 시스템 (Python + Shell 라이프사이클 훅)
│   ├── __init__.py          (24)   shell hook API re-export (하위 호환)
│   ├── shell.py             (236)  Shell hook (PreToolUse/PostToolUse/PostToolUseFailure)
│   ├── events.py            (53)   11개 이벤트 상수 + EVENT_TO_FUNC 매핑
│   ├── context.py           (145)  HookContext (messages 조작, system prompt 주입, MCP 메모리, 도구 제어)
│   ├── loader.py            (88)   Python hook 파일 스캔/로드 (.agent-cli/hooks/*.py)
│   └── runner.py            (95)   HookRunner (이벤트 발화, Python→Shell 순서 실행)
├── input_history.py         (174)  readline/gnureadline 설정 + 채팅 히스토리 영속화 (CJK 지원, paste/IME 디코드 오류 방어)
├── verbose.py               (27)   공용 verbose 플래그 + debug_log (providers가 loop을 역참조하지 않도록 추출)
├── loop.py                  (1305) AgentLoop 클래스 + ReAct 루프 (text parsing, token-budget FIFO, hook, streaming, nested depth rendering, failure-grounding retry)
├── render/                         플러그인 가능 렌더링 시스템
│   ├── __init__.py          (211)  렌더러 디스패치 + load_renderer_by_name + render crash 방어 + observation success 전달
│   ├── base.py              (189)  Renderer ABC (depth, capture, group, thread_status, 19개 메서드 + thinking, observation success 인자)
│   └── minimal.py           (524)  MinimalRenderer — 유일한 번들 렌더러 (nested depth, markdown, ASCII-art talking-face streaming progress with token counter + 시간 기반 프레임 throttle + 폭 통일 패딩 + 좁은 터미널 안전망 + resize-recovery, ASCII-art thinking spinner, `FrameClock` 공유 (delegate 병렬 패널이 동일 cadence로 reuse), write_file/edit_file unified-diff 렌더링, ToolResult.success 직접 전달로 정확한 ✓/✗ 표시, capture, group blocks, CJK+Ambiguous width, verbose에서 provider thinking 블록 표시). 커스텀은 `render/{name}.py`에 Renderer 서브클래스를 두면 `--style {name}`으로 로드됨
│
├── providers/                      LLM 프로바이더 어댑터
│   ├── __init__.py          (33)   create_provider() 팩토리
│   ├── base.py              (50)   LLMProvider 프로토콜, LLMResponse(+thinking), TokenUsage(+cache_creation/cache_read tokens)
│   ├── compat.py            (419)  ModelCapabilities + 프로브 감지 (thinking + format) + 진행 콜백 + 자동 저장
│   ├── http.py              (147)  post_with_retry (Timeout/ConnectionError 재시도, pre-stream only, 고정 1초 백오프)
│   ├── anthropic.py         (216)  Anthropic Messages API (tool_use + thinking blocks + streaming + TTFT + prompt cache via cache_control)
│   ├── openai_compat.py     (194)  OpenAI 호환 API (function calling + reasoning_content + streaming + TTFT)
│   └── ollama.py            (176)  Ollama API (basic JSON mode + message.thinking + streaming + TTFT)
│
├── parsing/                        응답 파싱
│   ├── __init__.py          (3)    re-export: parse_react, ReActResult
│   ├── react_parser.py      (253)  3단계 폴백 ReAct 파서 + thinking 분리 + action_input 정규화 (가상 도구 alias + sibling key bundling)
│   ├── json_repair.py       (183)  깨진 JSON 복구 (6단계 파이프라인)
│
├── tools/                          도구 시스템
│   ├── __init__.py          (81)   TOOLS dict (실제+가상) + VIRTUAL_TOOLS + _execute_tool() (internal primitive)
│   ├── result.py            (15)   ToolResult 데이터클래스 (success, output, error, artifact)
│   ├── registry.py          (481)  스키마 정의, 검증 (3-tuple 리턴), inline 가이드
│   ├── _diff.py             (113)  write_file/edit_file 공용 unified-diff 포매터 (Rich markup, OLD/NEW line-number gutter, 100줄 cap)
│   ├── read_file.py         (264)  파일 읽기 + hashline 포맷팅 + 부분 읽기/검색/stat 모드 + 대용량 가드 → ToolResult
│   ├── write_file.py        (37)   파일 생성/덮어쓰기 + 변경사항 colored diff → ToolResult
│   ├── edit_file.py         (274)  파일 편집 (hashline + 퍼지 매칭 + 중복 ref/range overlap 거부 + edits 필터링 + colored diff) → ToolResult
│   ├── shell.py             (167)  셸 명령 실행 + 위험 명령 (rm/rmdir/mv) y/n/a 확인 (decision + 선택적 코멘트, env로 비활성 가능) → ToolResult
│   ├── shell_artifact.py    (249)  Shell stdout 대용량 가드: 한도 초과 시 `<session>/shell/`에 저장하고 head/tail 미리보기로 치환, LRU 회전
│   ├── fetch.py             (230)  웹 페이지 fetch → 마크다운 변환 → ToolResult
│   ├── delegate.py          (700)  in-process 서브에이전트 (fork/none, 병렬 + Live 상태 패널은 render.minimal `FrameClock` reuse, subdir, agent_stack, stop_event)
│   ├── context.py           (574)  read_context 도구 (list / search: scope+sessions 필터 / fetch: loc+range)
│
├── context/                        컨텍스트 관리
│   ├── __init__.py          (14)   re-export
│   ├── token_estimator.py   (23)   토큰 추정 (chars/4)
│   ├── overflow.py          (45)   프로바이더별 오버플로 감지
│   ├── manager.py           (298)  ContextManager (토큰 budget FIFO + history.jsonl + 자연어 변환)
│   └── session.py           (184)  세션 메타데이터 (session.jsonl) + resume용 user↔assistant 페어 추출 (recent_exchanges)
│
├── prompts/                        프롬프트 템플릿
│   ├── __init__.py          (1)
│   └── system_prompt.py     (446)  Attention 최적화 시스템 프롬프트 빌더 (Primacy/Middle/Recency, Role 상속, Context Recovery Guide, FORMAT_RULES 6개 — 단일 액션 강제 + 효율성 가이드 + ask vs complete 구분 가이드). Recency 순서: Environment → Recovery → Directives → Execution Context (passive→active, persistent→immediate; Execution Context만 동적이라 끝에 배치 → 앞 3개 KV cache 안정)
│
├── skills/                         프롬프트 스킬 시스템
│   ├── __init__.py          (7)    re-export
│   ├── models.py            (21)   Skill 데이터 모델 (model/context/hooks/invocation)
│   ├── loader.py            (95)   스킬 파일 검색/파싱 (ResourceLoader 기반, 캐싱)
│   ├── executor.py          (209)  인자 치환 + 도구 교집합 + Role 상속 + skill subdir + stop_event
│   └── builtin/                    패키지 내장 스킬
│       ├── create-skill.md         스킬 생성 메타 스킬
│       ├── create-agent.md         에이전트 생성 메타 스킬
│       ├── plan.md                 구현 계획 생성 (plan/ 디렉토리에 저장)
│       └── create-team/            에이전트 팀 구성 메타 스킬
│           ├── SKILL.md            6단계 워크플로 (분석→설계→에이전트→스킬→오케스트레이터→검증)
│           └── references/         단계별 가이드 (design-patterns, agent-writing, skill-writing)
│
├── agents/                         에이전트 정의 패키지
│   ├── __init__.py          (1)
│   └── builtin/                    패키지 내장 에이전트
│       └── explorer.md             읽기 전용 코드베이스 탐색 에이전트
│
├── mcp/                            MCP (Model Context Protocol) 통합
│   ├── __init__.py          (1)
│   ├── config.py            (108)  mcp.json 로드/병합 (프로젝트 > 유저)
│   ├── client.py            (258)  McpClientManager (stdio/SSE 연결, 도구 호출, stderr 격리)
│   └── adapter.py           (95)   MCP 도구 → ToolResult 래핑, TOOLS dict 등록

pyproject.toml                      패키지 설정
agent-cli.py                        하위 호환 래퍼 (4줄)
```

괄호 안 숫자는 LOC(Lines of Code)입니다.

---

## 3. 모듈 의존성 그래프

### 3.1 전체 의존성 플로우

```
┌─────────────┐
│  main.py    │ ← __main__.py, agent-cli.py
│ (CLI 진입)  │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  loop.py    │
│ (에이전트   │
│  루프)      │
└──────┬──────┘
       ├────────┬────────┬────────┬────────┐
       ▼        ▼        ▼        ▼        ▼
┌──────────┐┌────────┐┌───────┐┌────────┐┌────────┐
│providers/││parsing/││tools/ ││context/││prompts/│
│          ││        ││       ││        ││        │
│anthropic ││react_  ││regis- ││manager ││system_ │
│openai_   ││parser  ││try    ││overflow││prompt  │
│compat    ││json_   ││read_  ││token_  ││        │
│ollama    ││repair  ││write_ ││estima- ││        │
│compat    ││        ││edit_  ││tor     ││        │
│base      ││        ││shell  ││session ││        │
│          ││        ││fetch  ││        ││        │
│          ││        ││dele-  ││        ││        │
│          ││        ││gate   ││        ││        │
└──────────┘└────────┘└───────┘└────────┘└────────┘
       │                  │         │
       ▼                  ▼         ▼
┌──────────┐       ┌──────────┐┌──────────┐
│config.py │       │render.py ││models.   │
│          │       │          ││json      │
└──────────┘       └──────────┘└──────────┘
```

### 3.2 모듈별 import 관계

**순환 의존 없음.** 단방향 흐름: config → compat → base → adapters → loop → main

```
config.py           → (외부만: json, pathlib)
constants.py        → (외부만: 없음, 순수 상수)
verbose.py          → (외부만: sys, time) — providers/http, loop가 공유
providers/compat.py → config
providers/base.py   → providers/compat
providers/http.py   → verbose, render (lazy)
providers/*.py      → providers/base, providers/compat, providers/http
parsing/json_repair → (외부만: json, re)
parsing/react_parser→ parsing/json_repair
tools/result.py     → (외부만: dataclasses, 순수 데이터 타입)
tools/read_file.py  → tools/result, (외부만: re, zlib, pathlib)
tools/edit_file.py  → tools/read_file, tools/result
tools/shell.py      → tools/result
tools/write_file.py → tools/result
tools/context.py    → tools/result, context/session
tools/delegate.py   → tools/result, context/manager, resource_loader, loop (lazy import)
tools/registry.py   → (외부만: json, dataclasses)
context/token_est.  → (외부만: 없음)
context/overflow.py → context/token_estimator, providers/compat
context/manager.py  → (외부만: json, collections, pathlib)
prompts/system_pr.  → providers/compat, tools/registry
loop.py             → constants, context/manager, context/overflow, parsing/react_parser,
                      prompts/system_prompt, providers/base, providers/compat,
                      render, tools, tools/delegate, tools/registry, verbose
skills/loader.py    → skills/models, resource_loader
resource_loader.py  → yaml (optional)
skills/executor.py  → loop, skills/models, providers/base, providers/compat
main.py             → config, context/manager, loop, providers, render, skills
```

---

## 4. 핵심 데이터 구조

### 4.1 LLM 응답 (`providers/base.py`)

```python
@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int

@dataclass
class LLMResponse:
    content: str                          # 텍스트 응답
    tool_calls: list[dict] | None = None  # 네이티브 tool calling 결과
    usage: TokenUsage | None = None
    stop_reason: str | None = None
    thinking: str = ""                    # provider-side reasoning 채널

# tool_calls 항목 형식:
# {"id": "tu_1", "name": "read_file", "input": {"path": "a.py"}}
```

`thinking`은 모델이 별도 reasoning 채널로 노출한 텍스트를 운반합니다. 채널 매핑:
- **Ollama**: `message.thinking` 필드 (Qwen3 / Qwen3.5 / Qwen3.6 family)
- **Anthropic**: `content[].type == "thinking"` 블록 + 스트리밍 `thinking_delta`
- **OpenAI 호환**: `choice.message.reasoning_content` (vLLM 컨벤션)
- 위 채널이 없으면 `""` (plain OpenAI Chat Completions 등 — graceful)
- `<think>...</think>` 태그가 content 안에 있는 경우는 별도 — `parse_react`가 `ReActResult.thinking`으로 분리 추출

**소비처 (v1):** verbose 모드의 `render_thinking` 디버그 출력 *전용*. recovery 레이어(`format_no_*_retry`, `recovery/primitives.py`)는 thinking을 *읽지 않음* — primitive contract가 channel-agnostic이어야 누더기를 막기 때문 (`docs/robust-harness/DESIGN.md` §2.2).

### 4.2 모델 능력치 (`providers/compat.py`)

```python
@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int               # 컨텍스트 윈도우 크기 (토큰)
    max_output_tokens: int            # 최대 출력 토큰
    supports_structured_output: bool  # basic JSON mode 가능 (Ollama format="json" / OpenAI response_format)
    supports_thinking: bool           # thinking/reasoning 지원
    thinking_budget: int              # thinking 토큰 예산 (0=비활성)
    supports_strict_schema: bool      # (dormant) strict JSON Schema 표식 — 현재 어떤 provider도 이 플래그로 동작 분기 안 함. 향후 opt-in strict schema 재도입 시 사용 예정.
    thinking_format: str = ""         # thinking 블록 태그 ("think", "reasoning", "")
```

`thinking_format` 값:
- `"think"` — `<think>...</think>` 형식 (Qwen3, DeepSeek-R1)
- `"reasoning"` — `<reasoning>...</reasoning>` 형식
- `""` — thinking 블록 미사용 (Anthropic API 레벨 처리, GPT 등)

능력치 조회 우선순위:
1. `models.json` 정적 설정 (최우선)
2. 런타임 API 감지 (Ollama `/api/show` + thinking probe + format probe)
3. 보수적 기본값 (4096 context, 모든 기능 비활성)

**런타임 감지 세부 (Ollama):**
1. `/api/show` — 메타데이터 (context_length 등)
2. thinking probe — `"What is 2+2?"`를 평문으로 보내 `message.thinking` 필드 혹은 `<think>` 태그 여부 검사 → `supports_thinking`, `thinking_format` 결정
3. **format probe** — `format="json"`을 붙여 똑같이 단순 요청을 보낸 뒤 HTTP 200 + `error` 없는 응답이 오는지 검사. mlx 엔진으로 패키징된 일부 모델(예: bf16 safetensors) 이 `format` 파라미터에서 깨지기 때문에 사전에 걸러냄. 실패 시 `supports_structured_output=False`로 저장하고 stderr에 `[warn]` 한 줄 기록 → 이후 실 요청이 `format` 파라미터 자체를 생략.

첫 감지 시 probe 2번(thinking, format)이 `/api/chat`에 가기 때문에 cold-load 비용이 1회 발생 (~10초). 감지 결과는 `~/.agent-cli/models.json`에 저장되어 이후엔 재실행 없음.

### 4.3 ReAct 파서 결과 (`parsing/react_parser.py`)

```python
@dataclass
class ReActResult:
    thought: str | None = None
    action: str | None = None     # "complete" = 작업 완료
    action_input: dict | str | None = None
    raw: str = ""                # 원본 LLM 텍스트 (thinking 제거 후)
    parse_stage: int = 0         # 0=실패, 1=json.loads, 2=json_repair, 3=regex
    thinking: str | None = None  # 추출된 thinking 블록 내용
    truncated: bool = False      # JSON 복구가 닫지 못한 브래킷/문자열을 보충했을 때 True
```

### 4.4 도구 스키마 (`tools/registry.py`)

```python
@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict  # JSON Schema 형태

# 등록된 도구: read_file, write_file, edit_file, shell, read_context,
#               complete, ask, run_skill, ready_for_review
# 가상 도구 (loop에서 인터셉트):
# VIRTUAL_TOOLS = frozenset({"complete", "ask", "run_skill", "ready_for_review"})
# _ALWAYS_INCLUDE = ("complete", "ready_for_review") — allowed_tools와 무관하게 항상 API tool 목록에 포함
# delegate는 별도 DELEGATE_TOOL_SCHEMA로 관리
```

---

## 5. 핵심 플로우

### 5.1 ReAct 에이전트 루프 (`loop.py` — `AgentLoop` 클래스)

#### 컨텍스트 윈도우 레이아웃

`ctx.get_messages()` 반환: history.jsonl의 마지막 N개를 자연어 변환

```
[system]   Role (main/delegate/skill별 상이)
           Task Guidelines + Format Rules (thought에 목적+이유 필수)
           Available Tools / Skills / Agents
           DIRECTIVE.md / Environment
           Context Recovery Guide ("read_file({session_dir}/history.jsonl)")

[messages] user: "hooks.py 분석해줘"
           assistant: hooks.py를 분석하기 위해 파일을 읽겠다. → read_file(hooks.py)
           user: [read_file] hooks.py\n(전문)
           assistant: 분석이 완료되었다. hooks.py는 3개의 hook 타입을 지원...
```

- Scratchpad/Summary inject 없음. messages만 (토큰 budget 기반 FIFO, 자동 계산)
- 저장: history.jsonl (JSON Lines, 구조화)
- 표현: 자연어 변환 (thought → "목적. → action(인자)")

#### ctx.add 저장 포맷

| 메시지 타입 | history.jsonl 저장 형태 |
|------------|----------------------|
| 사용자 입력 | `{"role":"user", "content":"..."}` |
| LLM action | `{"role":"assistant", "thought":"...", "action":"...", "action_input":{...}}` |
| 도구 결과 | `{"role":"user", "content":"Observation: ..."}` |
| complete | `{"role":"assistant", "thought":"...", "action":"complete", "action_input":{"result":"..."}}` |

#### 루프 플로우

```
AgentLoop.run()
    │
    ├─ _install_signal_handler()   ← Ctrl+C를 flag로 변환
    ├─ _setup()
    │   ├─ 시스템 프롬프트 빌드 (capabilities, tools, session_dir, agent_role)
    │   └─ ctx.add(user query) → ctx.get_messages() (자연어 변환)
    │
    ├─ while _should_continue():
    │    │
    │    ├─ ★ CHECK: _interrupted? → _on_interrupt() → return None
    │    │
    │    ├─ _begin_iteration() → turn separator 렌더링
    │    │
    │    ├─ _call_llm() → LLMResponse (overflow 시 FIFO refresh 후 재시도)
    │    │
    │    └─ _handle_text_path()  ← text parsing only (native tool calling 제거)
    │         │
    │         ├─ [ready_for_review] → 원본 query를 observation으로 반환
    │         │
    │         ├─ [complete] → ctx.add(structured dict) → return answer
    │         │
    │         ├─ [run_skill] → 내부 AgentLoop (별도 skill subdir)
    │         │
    │         └─ [도구] → execute → ctx.add(assistant + observation)
    │
    └─ _restore_signal_handler()
```

**Graceful Interrupt (`graceful_interrupt=True`, chat 전용):**
- 1st Ctrl+C: `_interrupted` flag 설정 → 현재 스텝 완료 후 다음 turn 시작 시 탈출
- 2nd Ctrl+C: `KeyboardInterrupt` 즉시 발생 (기본 핸들러 복원 후)
- 인터럽트 시 ctx에 기록되어 history.jsonl에 영속화

**run 모드 Ctrl+C:** signal handler 미설치, `KeyboardInterrupt` 즉시 발생 → `try/except`로 세션 저장 후 종료

#### 중첩 렌더링: `push_depth` / `pop_depth` + 그룹 블록

스킬/delegate 실행 시 출력을 시각적으로 감싸기 위해 `group_start`/`group_end`와
depth 기반 prefix(`│ `)를 사용. 병렬 delegate는 worker별 capture 후 Live 패널로
실시간 상태 표시, 완료 후 block replay.

| 시점 | 호출 | 출력 |
|------|------|------|
| 스킬/delegate 시작 | `render_group_start(label, icon)` | `┌─ 🪄 skill:plan` |
| 내부 턴 | `push_depth` 상태에서 `_p()` | `│ 💭 thought...` |
| 스킬/delegate 종료 | `render_group_end(label, success, dur)` | `└─ ✓ skill:plan (5.2s)` |

`--headless` 플래그(main.py)는 세션 미생성 + tmpdir ctx 용도로만 유지.

### 5.2 프로바이더별 도구 호출 방식

모든 프로바이더가 **ReAct 텍스트 파싱**만 씁니다. 네이티브 tool calling API (Anthropic `tool_use`, OpenAI `function calling`)는 **사용하지 않습니다** — 프로바이더 편차 제거와 구현 단순성을 위한 선택. 따라서 `supports_tool_calling` 같은 플래그는 존재하지 않고, 모든 분기는 JSON 출력 여부 (`supports_structured_output`) 하나로 수렴합니다.

```
              ┌─ supports_structured_output=True ─┐
              │                                    │
        ┌─────┴──────┐                     ┌──────┴──────┐
        │ Ollama     │                     │ OpenAI      │
        │ format:    │                     │ response_   │
        │ "json"     │                     │ format      │
        │ (basic)    │                     │ json_object │
        └────────────┘                     └─────────────┘
              파싱 필요                             파싱 필요
              (JSON 출력)                          (JSON 출력)

              ┌─ False ─────────────────────────────┐
              │                                      │
        ┌─────┴──────┐                              │
        │ 텍스트 자유  │                              │
        │ 형식        │                              │
        └────────────┘                              │
              파싱 필요                               │
              (비구조화 텍스트)                         │

  모든 경우: 3단계 폴백 파서 (json.loads → json_repair → regex)가 도구 호출 추출
```

### 5.3 3단계 파싱 폴백 (`parsing/react_parser.py`)

```
LLM 텍스트 응답
    │
    ▼
유니코드 서로게이트 제거 (_sanitize_surrogates)
    │
    ▼
Thinking 블록 분리 (_strip_thinking_blocks)
    │  ├─ <think>...</think> 제거 → thinking 필드에 보존
    │  ├─ <thinking>...</thinking> 제거
    │  ├─ <reasoning>...</reasoning> 제거
    │  └─ <reflection>...</reflection> 제거
    │
    ▼
Stage 1: 마크다운 펜스 제거 → json.loads()
    ├─ 성공 → ReActResult (parse_stage=1)
    │
    ▼ 실패
Stage 2: json_repair() — 6단계 복구 파이프라인
    │  ├─ JSON 블록 추출 (brace depth tracking)
    │  ├─ 작은따옴표 → 큰따옴표
    │  ├─ 따옴표 없는 키 수정
    │  ├─ trailing comma 제거
    │  ├─ 닫히지 않은 문자열 닫기
    │  └─ 누락된 괄호 추가
    ├─ 성공 → ReActResult (parse_stage=2)
    │
    ▼ 실패
Stage 3: regex 필드 추출
    │  ├─ "thought": "..." 추출
    │  ├─ "action": "..." 추출
    │  └─ "action_input": {...} 추출
    ├─ 성공 → ReActResult (parse_stage=3)
    │
    ▼ 실패
ReActResult (parse_stage=0, 모든 필드 None)
```

#### 형제 키 정규화 (action_input hoist, 2-레이어)

일부 모델(qwen3 계열 등)은 action 인자를 `action_input` 안에 **중첩하지 않고 top-level 형제 키로** 뱉는 드리프트를 보입니다:

```json
// 드리프트 A: 가상 툴 payload가 top-level
{"thought": "done", "action": "complete", "result": "final answer"}

// 드리프트 B: 실제 툴 인자가 top-level (pcie_scsc 세션에서 관찰)
{"thought": "find files", "action": "shell", "command": "ls"}

// 둘 다의 기대 형태
{"thought": "...", "action": "...", "action_input": {...}}
```

JSON 자체는 valid하고 action 이름도 올바른데, loop이 `action_input.X`를 찾기 때문에 조용히 실패 (가상 툴은 "Completed without result", 실제 툴은 "Missing required field" → repeated-call guard). strict JSON Schema로도 막히지 않음 — 과거 schema가 `thought`만 required로 두고 `additionalProperties` 제한이 없었기 때문.

`_normalize_action_input()`이 파싱 직후 두 레이어로 정규화합니다 (`parsing/react_parser.py`):

**Layer 1 — 가상 툴 별칭 매핑.** `complete` / `ready_for_review` / `ask`에 대해 정해진 후보 키를 canonical target 키로 매핑:

| action | target key | top-level fallback 순위 |
|---|---|---|
| `complete` | `action_input.result` | `result` > `answer` > `response` > `final` > `output` |
| `ready_for_review` | `action_input.summary` | `summary` |
| `ask` | `action_input.questions` | `questions` > `question` (`_extract_questions`가 str→list 처리) |

알려진 가상 툴인데 후보가 하나도 없으면 **fall-through 안 함** — `action_input=None` 유지해서 downstream이 "no payload" 경로로 처리 (복귀 가능).

**Layer 2 — 실제 툴 / 미지의 action의 형제 키 번들링.** 가상 툴이 아니고 `action_input`이 없으면, 예약되지 않은 top-level 키 전부를 `action_input`으로 모아줌:

```json
// 입력
{"thought":"...", "action":"shell", "command":"ls", "timeout":10}
// 정규화 후
{"thought":"...", "action":"shell", "action_input":{"command":"ls","timeout":10}}
```

MCP 제공 툴처럼 `action` 이름이 레지스트리에 없어도 같은 룰 적용.

**예약어 블랙리스트 (`_REACT_RESERVED`).** 다음 키들은 형제로 나타나도 `action_input`에 담기지 않습니다:

- `thought` / `action` / `action_input` — ReAct 프로토콜 필드
- `observation` — 시스템 프롬프트가 금지하지만 드리프트 시 혼입 가능
- `reasoning` / `reflection` — thinking 태그 변종 (태그로 나타나면 `_strip_thinking_blocks`가 잡지만 top-level 키 형태로도 등장 가능)
- `role` / `_meta` — 저장/세션 계층 메타 필드

**우선순위 규칙.** `action_input`이 이미 있고 truthy면 Layer 1, 2 모두 skip — 모델이 명시적으로 nested를 선택했다고 보고 형제 키는 무시. `action_input`이 `None` 또는 `{}`면 레이어 로직 발동.

이 정규화는 strict JSON Schema 도입 없이 작동하며, flat form을 정식 canonical로 승격하는 미래 변경(`plan/schema-flatten.md` 참조)의 파서 기반이 됩니다.

#### Failure Grounding Retry (`recovery/primitives.py` + `constants.py` + `loop.py`)

> 설계 문서: `docs/robust-harness/DESIGN.md` (4-layer 디자인, primitive 도구함, playbook)

3단계 파싱이 모두 실패하거나(JSON 깨짐 — `parse_stage=0`) JSON은 파싱됐는데 `action`이 없으면 (`parse_stage>0` & `action=None`), `loop.py`가 user role 메시지를 한 개 주입하고 같은 turn을 재시도합니다 (`turn -= 1`로 카운트 제외). 메시지는 `recovery/primitives.py`의 순수 함수들을 합성한 결과:

```
Your response was not valid JSON.

Your prior output:               ← echo_prior_output: head 400자 (구조 마커 보존)
---
{LLM이 방금 토출한 content}
---

Honor that. Output ONLY a JSON object: {...}.   ← constrain_format_json
```

**v1 design — content-only echo.** thinking 채널 echo는 격리 측정값 없이 runtime 의존성만 유발하므로 v1에서 제외. Step 2 observability (TurnRecord JSONL) 데이터로 필요성이 검증되면 별도 primitive로 추가. (자세한 결정 배경은 `docs/robust-harness/DESIGN.md` §2.2.)

`prior_content`가 비면 정적 fallback (`RETRY_HINT_NO_JSON` / `RETRY_HINT_NO_ACTION`) — graceful path.

**A1a (NO_JSON) vs A1b (NO_OUTPUT) 라벨 분리.** parse stage 0 실패는 두 가지 운영 모드가 섞여 있음 — (a) 모델이 *내용은 있는데* JSON 형식에서 드리프트 (YAML 키, prose, code fence 등), (b) 모델이 *아무것도* 안 뱉음 (whitespace-only). `loop.py`의 `_handle_text_path`가 `llm_text.strip()` 검사로 둘을 분리해 `failure_signal` 을 `FAILURE_NO_JSON` 또는 `FAILURE_NO_OUTPUT` 으로 기록. 회복 경로는 동일(둘 다 `format_no_json_retry`) — A1b는 echo 대상이 없어 자연스럽게 정적 fallback path로 떨어지고 `primitives_applied=[]` 가 됨. 라벨 분리의 목적은 *관찰성*이며, 두 모드가 회복률 분포에서 어떻게 갈리는지 데이터를 모은 뒤 별도 primitive 도입 여부를 결정 (DESIGN.md §1, A1a/A1b).

**근거 (failure grounding):** 추상적 *"your response was invalid"*는 모델이 무엇을 위반했는지 모르게 함 — 같은 출력을 반복할 가능성 높음. retry에 자기 출력을 인용해 보여주면 모델이 자기 드리프트(YAML-style 키, 함수-호출 신택스, bare prose 등)를 직접 보고 self-diagnose 가능. 구조 마커가 보통 출력 시작 부분이라 head-truncate.

**Primitive 계약 (누더기 방지):** primitive는 provider/모델/채널 이름을 절대 참조하지 않음. 새 실패 모드는 *primitive 합성과 매핑 한 줄*로 처리 — `if "ollama"`, `response.thinking` 같은 분기를 primitive 시그니처에 두면 invariant 위반.

**Prefix 호환성:** retry 메시지 시작은 항상 정적 템플릿과 같은 문장 (`"Your response was not valid JSON."` / `"Your JSON was parsed but has no action."`)으로 시작하므로 `SYSTEM_USER_PREFIXES` 매칭이 그대로 유지됨 → resume 시 자연어 변환에서 noise로 표시되지 않음.

#### Per-Turn Observability (`recovery/observability.py`)

`format_no_*_retry`는 단순 문자열이 아니라 `Intervention` (message + primitives 이름) 을 반환합니다. `_handle_text_path`는 try/finally로 매 턴 한 번씩 `TurnRecorder.record()`를 호출 — 성공/실패/예외 모든 경로에서 정확히 한 줄이 기록됩니다.

**스키마 (`TurnRecord`, `{session_dir}/turns.jsonl` 한 줄당 한 row):**
- `seq` — 세션 내 모노토닉 (0, 1, 2, ...)
- `model` — 어떤 모델이 응답했는지 (분석 시 그룹 키)
- `timestamp` — ISO 8601 UTC
- `parse_stage` — 0(실패), 1(json.loads), 2(json_repair), 3(regex)
- `failure_signal` — `"NO_JSON"` / `"NO_OUTPUT"` / `"NO_ACTION"` / `"UNKNOWN_TOOL"` / `"SCHEMA_MISMATCH"` / `"NESTED_ENVELOPE"` / `"ACTION_LOOP"` / `null`
- `primitives_applied` — 합성된 primitive 이름 list (실패 retry 시에만 채워짐)

**프라이버시 계약:** 사용자 prompt나 LLM 응답 본문은 절대 기록되지 않음 — 구조 메타만. 회복률은 *저장하지 않고* 분석 시 walk-forward로 계산 (실패 row 다음 row의 failure_signal을 봐서 회복 여부 판단). retrospective 쓰기 회피.

**활성화 조건:**
- `ctx is not None` (headless·subagent에선 비활성)
- `record_turns=True` (CLI: `--record-turns/--no-record-turns`, 기본 켜짐)

**활용:** Step 3·4의 playbook 튜닝 데이터 누적이 주 목적. 분석은 별도 스크립트 (`jq`로도 충분). 자세한 설계는 `docs/robust-harness/DESIGN.md` §3.3.

#### B1 — Action Loop 감지 + 회복 (`recovery/detectors.py` + B1 playbook)

같은 `(action, args)` 호출이 연속 2회 이상이면 모델이 막힌 상태로 보고 단계적 개입을 발동합니다. 기존 hard-fail (`_detect_repeated_calls`)을 대체.

**Detector (`ActionLoopDetector`):** turn 간 stateful — `_last_signature`, `_consecutive_count`, `_fire_count` 보유. `observe(action, args, prev_was_error=False)`가 호출될 때마다 escalation level 반환:
- 0 — 임계값 미만 또는 error retry로 카운터 리셋
- 1 — 첫 발동 (probe_progress)
- 2 — 두 번째 발동 (restate_task)
- 3+ — 회복 소진, hard-fail

`prev_was_error=True`면 카운터 리셋 — 정당한 재시도 false-positive 방지. 다른 action이 끼면 카운터 리셋. Args canonicalization은 `json.dumps(sort_keys=True)` (dict 키 순서 무시).

**Playbook (`format_action_loop_intervention`):**
- Level 1: `probe_progress` — 가벼운 nudge ("이미 가진 응답을 다시 봐, complete 또는 다른 action 선택")
- Level 2: `restate_task` — 원본 task 재고정 + 진단 질문 ("task가 이 호출을 왜 필요로 하나? 못 얻고 있는 정보가 뭔가?")
- Level 3+: `None` 반환 — caller가 hard-fail (어떤 primitive를 시도했는지 에러 메시지에 포함)

**Temperature-down 컬럼 의도적 누락:** DESIGN.md §2.3에 명시된 escalation 컬럼 중 "+temp↓"은 v1에서 제외. provider별 temperature 처리가 다양해 primitive 계약(provider-agnostic) 위반 위험. Step 4에서 데이터 보고 재검토.

**감지 시점:** dispatch *전*. tool은 실행되지 않고, 모델은 다음 turn에 새 prompt(`probe_progress` 또는 `restate_task`)와 함께 같은 결정을 다시 내림. 중복 비용 0.

#### A4 / A5 — Pre-dispatch Detection (`recovery/detectors.py`)

LLM이 emit한 action·input이 도구 레지스트리·스키마와 안 맞을 때:

- **A4 (Unknown tool)** — `detect_unknown_tool(action, tools_list)` → `action not in tools_list`
- **A5 (Schema mismatch)** — `detect_schema_mismatch(action, action_input)` → `validate_tool_input` wrap, `(mismatched, error_message, normalized_input)` 반환

**감지 위치:** `_dispatch_text_path` 안, B1 detector → render_action 직후, `_dispatch_tool_with_hooks` 호출 직전. 모든 *pre-dispatch* 검사가 한 자리에 모임 (DESIGN.md §3.1 detection layer).

**처리:** 라벨링 + observation 주입 + dispatch 우회.
```
A4: outcome["failure_signal"] = FAILURE_UNKNOWN_TOOL
    Observation: "Unknown tool 'X'. Available: ..."
A5: outcome["failure_signal"] = FAILURE_SCHEMA_MISMATCH
    Observation: "Missing required field(s) for 'X': ... Expected: {...} Fix action_input and retry."
```

**v1은 라벨링만 — 별도 primitive 없음.** 이유: 현재 메시지(레지스트리·스키마 정보 포함)가 이미 grounding 역할 수행 중. 별도 primitive(`probe_tool_name`, `echo_diff` 등)가 *측정 효과*를 내는지는 TurnRecord 통계 보고 결정 (Step 4b).

**B1 detector와의 순서:** B1이 먼저 (A4·A5 무관 *반복 자체*가 더 큰 신호). 같은 unknown tool을 2번 emit하면 B1이 잡아 `probe_progress`를 줌 — A4 메시지가 무한 반복되지 않음.

**`_dispatch_tool_with_hooks` 내부 검증 제거됨:** Step 4a 전엔 `_execute_single_tool` 안에서도 `validate_tool_input` 호출 + `tool_name in tools_list` 체크가 있었음. 이젠 recovery 레이어가 단일 진실 원천 — 중복 제거. 2026-05-03: `tools/__init__.py:execute_tool`의 boundary 방어도 제거 + `_execute_tool`로 internal rename (REMAINING_DEBT.md #2/#3 청산).

**남은 부채:** 이 작업 중 *알면서 남긴* 부채는 `docs/robust-harness/REMAINING_DEBT.md`에 명시 기록.

#### A6 — Nested Envelope Detection (관찰 전용)

`complete` action의 결과 페이로드가 다시 `{"result": "..."}` JSON 객체로 래핑되어 들어오는 경우 — qwen3.5/3.6 계열에서 산발적으로 관찰됨. 사용자에게 `✅ {"result": "..."}` 같은 문자열이 그대로 표시되는 UX 회귀로 이어짐.

- **감지** — `detect_nested_envelope(result_value)` → `str` 인지 확인 → `lstrip().startswith('{"result"')` → `json.loads` 성공 → 결과 dict의 top-level `result` 키 존재. 한 단계라도 실패하면 false (오탐 방지).
- **위치** — `loop.py`의 `complete` 분기, `answer` 결정 *직후*. 라벨링만 하고 출력은 그대로 둠.
- **라벨** — `outcome["failure_signal"] = FAILURE_NESTED_ENVELOPE` (TurnRecord에 기록).
- **자동 unwrap 안 함 (의도적)** — 빈도·재현 모델 분포 측정 후 4b에서 결정. v1에서 unwrap을 하면 (a) 모델이 의도적으로 그렇게 답한 경우 데이터를 잃고 (b) anti-patchwork 원칙(측정 후 결정) 위반.

### 5.4 컨텍스트 관리 (`context/manager.py`)

> 상세 설계: `docs/context-redesign/DESIGN.md`

#### 토큰 Budget 기반 FIFO

```
메시지 추가 (add)
    │
    ├─ 메모리 캐시 (list)에 append + 토큰 추정치 누적
    │   └─ budget 초과 시 가장 오래된 메시지 단위로 evict (메시지 중간 잘림 없음)
    │
    └─ history.jsonl에 JSON 한 줄 append (write-only)

Budget 계산:
    budget = context_window - max_output_tokens - 4000 (system prompt 예약)
    예: 262K context → ~254K token budget

LLM 호출 시:
    캐시에서 budget 내 메시지를 자연어 변환 → messages 배열 구성

세션 재개 시:
    history.jsonl 뒤에서부터 budget 내 메시지 파싱 → 캐시 초기화
```

- **LLM 기반 압축 없음.** 토큰 budget FIFO (모델 context_window에서 자동 계산)
- **Scratchpad 없음.** history.jsonl이 대화 기록이자 artifact 인덱스
- **Context inject 없음.** LLM이 필요할 때 read_file로 pull
- System prompt에 Context Recovery Guide 포함
- 스킬/delegate는 부모 budget 상속

#### 저장과 표현의 분리

- **저장**: history.jsonl (JSON Lines) — 구조화된 메시지
- **표현**: 자연어 변환 — LLM에 전달되는 user/assistant 메시지

```
저장: {"role":"assistant","thought":"auth.py를 읽겠다","action":"read_file","action_input":{"path":"src/auth.py"}}
표현: auth.py를 읽어 구조를 파악해야 한다. → read_file(src/auth.py)
```

#### 세션 파일 구조

```
.agent-cli/sessions/{session_id}/
├── history.jsonl                              ← main 대화 기록
├── main_plan_e8d4_20260405T143112890.md       ← main artifact (flat)
│
├── delegate_coder_f1a9_20260405T143230456/    ← delegate subdir
│   ├── history.jsonl                          ← delegate 내부 대화
│   └── result.md                              ← delegate 최종 결과
│
└── skill_summarize_d4e1_20260405T143200100/   ← skill subdir
    ├── history.jsonl                          ← skill 내부 대화
    └── result.md                              ← skill 최종 결과
```

- main: root에 flat artifact
- delegate/skill: subdir에 history.jsonl + result.md (재귀 중첩 가능)
- fork 모드: parent history.jsonl 복사 → delegate가 이어서 append

---

## 6. 도구 시스템

### 6.1 등록된 도구

**실제 도구** — 파일/셸/네트워크 작업 수행:

| 도구 | 설명 | 필수 입력 | 출력 |
|------|------|----------|------|
| `read_file` | 파일 읽기 (hashline 포맷). 모드: `stat` (메타데이터 + 앞 20줄), `search` (정규식 grep), `line_start/line_end` (부분 범위 — 범위가 파일 전체를 덮으면 whole-file read), 또는 mode 없이 full read. Mode 없는 bare full read는 파일이 threshold(`AGENT_CLI_READ_FILE_LIMIT` env, 기본 300줄) 초과 시 거부되고 stat-형태 응답으로 대안 제시 — 거부 메시지가 전체 파일이 필요한 경우를 위해 `line_start=1, line_end=<total>` 구체 예시까지 박아줌. 전용 escape-hatch 파라미터는 없음 (line_start/line_end로 일원화). | `path` | `LINE#HASH:content` 형식 또는 `[refused-full-read]` |
| `write_file` | 파일 생성/덮어쓰기 | `path`, `content` | 저장 확인 메시지 |
| `edit_file` | hashline 기반 파일 편집 | `path`, `edits[]` | 편집 확인 메시지 |
| `shell` | 셸 명령 실행 | `command` | stdout + stderr + exit code |
| `delegate` | in-process 서브에이전트 위임 | `tasks[]` (각 항목: task, context?, tools?, agent?) | 구조화된 결과 (output + activity log + duration) + delegate subdir 경로, 복수 시 병렬 |
| `read_context` | 세션 이력 조회 | `mode`, `keyword`, `scope?`, `sessions?`, `loc?`, `range?` | **list**: 전체 세션 목록. **search**: 기본 현재 세션, `sessions="all"` 또는 ID로 확장; `scope`로 필드 필터 (reasoning/tool/observation/query); 결과 턴 블록 + preview 200자 cap + 50건 truncation + fetch hint footer. **fetch**: `loc='{session}/{path}:{line}'` (search 결과 그대로) 로 전체 턴 회상; `loc` 단일/배열 (max 10), `range` 0-5 (앞뒤 N턴). multi-line 보존, action_input compact JSON, all-or-nothing 시멘틱. |
| `fetch` | 웹 페이지 fetch → 마크다운 변환 | `url` | 재귀 링크 추출, 에러 힌트 |

**가상 도구** (`VIRTUAL_TOOLS`) — loop.py에서 인터셉트, 도구 설명에서 제외:

| 도구 | 설명 | 필수 입력 | 비고 |
|------|------|----------|------|
| `complete` | 작업 완료 신호 | `result` | 루프 종료 |
| `ask` | 사용자에게 질문 | `questions` | 대화형 전용 (ctx 없으면 제거) |
| `run_skill` | 스킬 실행 | `name` | loop 레벨 인터셉트, skill subdir 생성 |
| `ready_for_review` | 작업 검증 요청 | `summary` | 원본 query 반환하여 self-check |

### 6.2 delegate agent 로딩

`delegate` 도구의 `agent` 파라미터로 사전 정의된 에이전트 역할을 로드할 수 있습니다:

```
검색 경로 (우선순위 순):
  1. .agent-cli/agents/{name}.md  (프로젝트 로컬)
  2. ~/.agent-cli/agents/{name}.md (유저 전역)

에이전트 파일 형식:
  ---
  allowed-tools: [read_file, shell]   # 선택: 허용 도구 제한
  model: claude-sonnet-4-6            # 선택: 모델 오버라이드
  ---
  에이전트 역할/원칙 본문 (시스템 프롬프트의 Agent Role 섹션에 주입)
```

**핵심 함수** (`tools/delegate.py`):
- `_validate_agent_name(name)` — 이름 검증 (`[a-zA-Z0-9_-]`만 허용)
- `_load_agent(name)` — 파일 탐색 + YAML frontmatter 파싱 → `(role_prompt, config, error)`
- `_extract_activity_log(messages)` — 컨텍스트 메시지에서 per-turn 액션 요약 추출
- `_summarize_action(action, action_input)` — 단일 액션을 한 줄 요약으로 포맷
- `_extract_last_actions(messages, n)` — 마지막 N개 액션 + 에러 observation 추출
- `_persist_delegate_result(formatted, delegate_dir)` — result.md를 delegate subdir에 저장
- `_format_delegate_output(result)` — DelegateResult를 구조화된 observation 문자열로 포맷
- `_AGENT_SEARCH_PATHS` — 검색 경로 리스트
- `_FRONTMATTER_PATTERN` — `---` frontmatter 정규식

**DelegateResult 필드**: `output`, `duration_secs`, `activity_log`, `last_actions`, `iterations`

**산출물 구조**: delegate 실행 결과는 다음 섹션을 포함:
1. 서브에이전트 출력 (output 또는 "(subagent returned no result)")
2. `[Subagent activity]` — per-turn 액션 로그 (최대 20개)
3. `[Last actions before failure]` — 실패 시 마지막 5개 액션 + 에러 힌트
4. `[Duration: Ns]` + `[Subagent used N turns]` — 실행 메타데이터
5. `→ delegate_{name}_{hash}_{ts}/` — delegate subdir 경로 (history.jsonl + result.md)

**적용 우선순위**: task에 명시된 `tools`/`model`이 agent 파일 설정보다 우선합니다.

**병렬 delegate Live 패널** (`_run_parallel`):
- 각 worker thread는 `render_start_capture()`로 출력을 버퍼에 수집
- 메인 thread는 Rich `Live`로 per-task 진행 상황 실시간 표시:
  - `_THINK_FRAMES` 페이스 (단일 작업 thinking 스피너와 동일, `_FRAME_INTERVAL`로 throttle) + task 라벨 (전체)
  - 현재 thought (별도 라인, `renderer.get_thread_status(tid)`로 조회)
  - 완료 시 ✓/✗ + duration
- 모든 worker join 후 Live 종료 → 각 task를 `┌─ 🦀 [N] ... └─` 그룹 블록으로 replay
- 중첩 병렬 (delegate 안의 delegate)은 outer Live가 이미 떠있으면 스킵

### 6.3 run_skill 결과 포맷

`run_skill` 실행 결과에는 스킬 식별 헤더가 포함:

```
STATUS: success
RESULT:
SKILL: summarize(./)
The agent-cli directory contains a ReAct pattern-based agent CLI...
```

- `SKILL: name(arguments)` — 실행된 스킬과 인자
- 스킬은 자체 subdir에 history.jsonl + result.md 저장
- 도구 교집합: skill allowed-tools ∩ parent allowed-tools (빈 교집합 시 거부)
- Role 상속: parent의 Role을 이어받음

### 6.4 Hashline 시스템 (`tools/read_file.py`)

```
원본 파일:             hashline 출력:
def hello():    →    1#VR:def hello():
    return "hi"      2#KT:    return "hi"
                     3#ZZ:

해시 알고리즘: CRC32(line_content, seed) & 0xFF → 2-char 태그
시드: 내용 있는 줄 → 0, 빈 줄 → line_number
알파벳: ZPMQVRWSNKTXJBYH (16자 기반 256 조합)
```

편집 연산:
```json
{"op": "replace", "pos": "2#KT", "lines": ["    return 'hello'"]}
{"op": "replace", "pos": "1#VR", "end": "3#ZZ", "lines": ["def greet():", "    pass"]}
{"op": "append",  "pos": "1#VR", "lines": ["    # 주석"]}
{"op": "prepend", "pos": "1#VR", "lines": ["# 헤더"]}
{"op": "append",  "lines": ["# EOF"]}  // pos 없으면 파일 끝
```

퍼지 매칭 (`edit_file.py`): 해시 불일치 시 공백/따옴표/대시 정규화 후 재매칭. LLM 재호출 없이 비용 제로 보정.

**Multi-edit 안전장치.** `edits[]` 한 호출에 여러 편집이 들어올 때 두 가지 모호성을 사전에 거부:

- **중복 ref 거부 (Layer 1)** — 두 편집이 같은 `pos` 또는 같은 `end` 태그를 참조하면 reject. 같은 줄을 두 번 다른 방식으로 바꾸려는 의도가 정의되지 않음.
- **범위 겹침 거부 (Layer 2)** — 각 edit을 (start_line, end_line) 구간으로 환산해 pairwise 검사. 범위가 겹치면 어느 쪽이 먼저 적용돼야 할지 모호하므로 reject. 같은 위치에 append/prepend 같이 의도적으로 동일 좌표를 쓰는 케이스는 별도로 허용.

거부 시 단일 fail 메시지로 전체 호출이 atomically 실패 — 일부만 적용된 후 hash mismatch로 멈추는 케이스 차단.

### 6.5 Tool Output 전달 방식

Tool output은 **잘림(truncation) 없이 전체를 그대로** LLM에 전달합니다. 이전에는 context window의 3% 비율로 잘랐으나(`tools/truncation.py`, 삭제됨), LLM이 불완전한 정보로 판단하는 성능 열화가 확인되어 제거. context가 넘치면 `context/manager.py`의 토큰-budget FIFO가 오래된 메시지부터 통째로 떨어냄 (요약 압축 아님).

### 6.5.0 `read_file` Full-Read Guard

큰 파일을 mode 없이 읽어 컨텍스트 예산을 순식간에 소진하는 패턴을
억제하는 tool-level guard. 동작:

- `read_file(path)` + 파일이 threshold(`AGENT_CLI_READ_FILE_LIMIT` env
  var, 기본 300줄) 초과 → `[refused-full-read]` 응답 (stat-형태의
  메타데이터 + 앞 20줄 + 대안 예시).
- `stat=true` / `search=` / `line_start/line_end` 모드는 guard 무시.
- 실제 **전체가 필요하면** 거부 메시지에 명시된 형태 그대로
  `read_file(path, line_start=1, line_end=<total>)` 호출. `<total>`
  값은 거부 응답에 이미 박혀있어서 LLM이 그대로 복사하면 됨.
  별도 boolean escape-hatch 파라미터 (예: `full=true`)는 **없음** —
  line_start/line_end 단일 경로로 통일.
- Threshold ≤ 0 → guard 비활성 (CI/배치에서 유용).

**왜 별도 escape hatch 없이 line-range로 통일했나**:
`full=true` 같은 단어 하나짜리 플래그는 LLM이 반사적으로 고르기
쉬운 "최소 저항 경로". 반면 `line_start=1, line_end=370` 은 LLM이
파일 크기를 **명시적으로 인지해서 숫자로 적어야** 하는 행위. 후자가
더 강한 "의식적 선택" 신호. 게다가 API 표면이 작아지고, 부분/전체
read의 의미론이 "범위 지정"이라는 한 개념으로 일원화됨. LLM이
`line_start=1, line_end=<very large>` 같은 "사실상 full read" 패턴을
쓰는 것은 막지 않음 — 그것도 의식적 선택으로 간주.

**설계 경계** — guard는 **bare full read의 습관적 사용**만 차단한다.
`line_start/line_end` 로 범위 지정, `search=` 로 패턴 지정,
`stat=true` 로 메타 조회는 모두 **LLM의 의식적 선택**으로 간주하여
threshold를 적용하지 않는다. 사용자가 "1-to-1200" 범위 같은 bypass
패턴을 관찰해도 이는 정상 동작 — guard의 목적은 "반사적 blunder
방지"이지 "1회 전송량 상한"이 아니다. 상한이 필요하다면 context
manager(대화 압축)가 downstream에서 처리한다.

### 6.5.0b Shell Output Artifact Guard (`tools/shell_artifact.py`)

`read_file` guard와 같은 철학을 shell 출력에 적용한 tool-level
guard. `find /`, `grep -r`, `pytest -v` 같이 수천 줄을 뿜는 명령이
context를 통째로 집어삼키는 것을 방지.

**핵심 차이** vs read_file guard:

| 측면 | read_file | shell |
|---|---|---|
| 명령 재실행 가능성 | ✅ (같은 파일 다시 읽으면 됨) | ❌ (side effect·비결정적·비싼 실행) |
| 전체 요청 경로 | `line_start=1, line_end=<total>` | artifact 생성 후 read_file이 맡음 |
| Output 유실 | 없음 (파일은 그대로) | 없음 (artifact에 전체 저장) |

**단일 mechanism 체인**: read_file 쪽에 별도 escape-hatch 파라미터가
사라지면서 shell→read_file 체인도 단순화됨. Shell preview는 `search=`
와 `line_start/line_end` 두 옵션만 공개. LLM이 artifact 전체가 필요
하다 판단하면 `read_file(path)` bare 시도 → artifact가 크면 read_file
full-read guard가 refuse → 그 refusal이 `line_start=1, line_end=<total>`
구체 예시 제공 → LLM이 복사해서 재호출. 전 체인에서 **동일한
line-range 기법**으로 "전체 요청"을 표현. 개별 도구마다 서로 다른
escape hatch를 배울 필요 없음.

**동작 흐름** (loop-level 후처리 — tool_shell 시그니처 불변):

```
AgentLoop._dispatch_tool_with_hooks:
  1. _execute_tool("shell", ...) → ToolResult(output=stdout)
  2. session_dir 있음 + exceeds_limit(output) → 후처리 진입
       a. save_artifact(session_dir, cmd, output) → session_dir/shell/<ts>-<hash>.log
       b. build_preview(cmd, output, path, succeeded=...) → head+tail+경로+대안
       c. result = ToolResult(True, output=preview)
  3. read_file 호출이 성공했고 path가 session_dir/shell/ 내부 → touch (LRU read-awareness)
```

**3단 방어** (Context·디스크·LRU):

1. **Per-artifact 상한** (`AGENT_CLI_SHELL_ARTIFACT_MAX_SIZE`, 기본 5 MB):
   단일 pathological output도 5 MB까지만 기록 + 끝에 truncation 주석.
2. **Per-session LRU** (`AGENT_CLI_SHELL_ARTIFACT_KEEP`, 기본 20):
   `session_dir/shell/` 내 파일 > 20이면 mtime-oldest부터 삭제. 매 write 시 1회.
3. **Read-aware**: read_file이 성공적으로 artifact를 읽으면 loop가 `Path.touch()` 호출 → mtime 갱신 → 활발히 참조되는 artifact는 eviction 안 당함.

**Threshold 2축 OR** (`AGENT_CLI_SHELL_OUTPUT_LIMIT_LINES` 기본 500,
`AGENT_CLI_SHELL_OUTPUT_LIMIT_BYTES` 기본 20 KB): 둘 중 하나라도 초과면
guard 발동. 각 env var 0 이하 → 해당 축 비활성.

**Preview 전략**: head 20줄 + tail 20줄 (실패 명령은 tail 30줄 — 에러
로그는 끝에 몰리는 경향). 중간 생략은 `[... N lines omitted ...]`로
표시. 끝에 `read_file(path, search=...)` 와 `read_file(path, line_start=N, line_end=M)` 2대안 제시 (전체 artifact가 필요한 경우는 read_file refusal이 line-range 형태로 가이드).

**Read-aware LRU 판정** (`is_session_shell_artifact`):
`Path.resolve().is_relative_to(session_dir/shell)` 로 엄밀 검사.
path 문자열에 `/shell/` 있다고 무작정 touch하지 않음 — 사용자 프로젝트의
`project/shell/build.log` 읽기 같은 false positive 방지. 다른 세션의
artifact 경로도 우리 LRU에 영향 주지 않도록 session 스코프 확인.

**Config 비활성 조합**:
- `AGENT_CLI_SHELL_OUTPUT_LIMIT_LINES=0` + `AGENT_CLI_SHELL_OUTPUT_LIMIT_BYTES=0`
  → guard 완전 off (기존 동작 그대로).
- `AGENT_CLI_SHELL_ARTIFACT_KEEP=0` → LRU off (artifact 무한 누적).

### 6.5.1 Fulfillment Review (`ready_for_review`)

LLM이 작업 완료 전 자기 검증을 수행하는 가상 도구입니다.

1. LLM이 `ready_for_review(summary="...")` 호출
2. Loop이 intercept → **원본 query + summary**를 observation으로 반환
3. LLM이 요청 vs 실행 내역을 대조 → 빠뜨린 게 있으면 계속, 다 했으면 `complete` 호출

`_ALWAYS_INCLUDE`에 등록되어 skill의 `allowed_tools`와 무관하게 항상 API tool 목록에 포함됩니다.

### 6.6 스키마 검증 (`tools/registry.py`)

검증 순서:
1. 도구 존재 확인
2. action_input이 string이면 → dict 자동 변환 시도
3. 필수 필드 존재 확인
4. 타입 검증 + 자동 변환:
   - `"30"` (string) → `30` (integer)
   - `{}` (dict) → `[{}]` (array)
   - `42` (int) → `"42"` (string)

---

## 7. 프로바이더 시스템

### 7.1 LLMProvider 프로토콜 (`providers/base.py`)

```python
class LLMProvider(Protocol):
    def call(
        self,
        messages: list[dict],
        system: str,
        model: str,
        capabilities: ModelCapabilities,
        **kwargs,          # tools, skip_json_format 등
    ) -> LLMResponse: ...
```

### 7.2 프로바이더별 구현

| 프로바이더 | 엔드포인트 | 인증 | 구조화 출력 | Thinking |
|-----------|-----------|------|-----------|---------|
| **Anthropic** | `/messages` | x-api-key | - | budget_tokens |
| **OpenAI Compat** | `/chat/completions` | Bearer token | `response_format={"type":"json_object"}` (basic JSON) | reasoning_effort |
| **Ollama** | `/api/chat` | 없음 | `format="json"` (basic JSON) | num_predict |

네이티브 tool calling (Anthropic `tool_use`, OpenAI `function calling`)은 **사용하지 않습니다**. 모든 프로바이더가 동일하게 ReAct 텍스트 파싱을 거치므로 provider-specific 코드 경로가 줄고, 프로바이더 편차가 거의 없어집니다.

**구조화 출력 정책**: 세 프로바이더 모두 **basic JSON mode**만 사용하고, **strict JSON Schema는 쓰지 않습니다**. 이는 확장성을 위한 선택이며 다음과 같은 배경이 있습니다:

- 이전 구현은 Ollama에서 `format=<REACT_JSON_SCHEMA>`(strict)를 보냈지만, Ollama의 mlx 엔진으로 패키징된 일부 모델(예: safetensors 포맷)에서 HTTP 200 + 스트림 중간 `{"error": "mlx runner failed"}`로 조용히 깨졌음.
- Basic JSON mode(`format="json"` / `response_format={"type":"json_object"}`)는 "유효한 JSON을 내라"는 신호만 주고 스키마는 강제하지 않음. 거의 모든 백엔드가 지원.
- ReAct JSON 구조 강제는 대신 시스템 프롬프트의 `FORMAT_RULES`와 3단계 파서(json.loads → json_repair → regex)가 담당. 32B+ 모델에서 신뢰성 충분.
- 7-14B 모델은 schema 없을 때 포맷 drift가 늘지만, 이 사이즈는 README에서 이미 비권장 구간.

향후 특정 백엔드가 strict schema를 반드시 필요로 하면, 현재 기본값을 건드리지 말고 **opt-in 플래그**로 다시 도입할 것. mlx 패키지 모델에서 재발 여지가 있으므로 기본 활성화는 금지.

### 7.3 프로바이더 팩토리 (`providers/__init__.py`)

```python
create_provider("anthropic", base_url, api_key)  → AnthropicProvider
create_provider("openai", base_url, api_key)     → OpenAICompatProvider
create_provider("ollama", base_url, api_key)      → OllamaProvider
```

OpenAICompatProvider 하나로 OpenAI, vLLM, LM Studio, mlx-lm을 `--base-url`만 바꿔서 커버.

### 7.4 Thinking Budget 적용

| 프로바이더 | 파라미터 | 동작 | thinking_format |
|-----------|---------|------|----------------|
| Ollama | `options.num_predict = budget + max_output` | thinking + 출력 토큰 합산 | `"think"` (Qwen3, DeepSeek-R1) |
| Anthropic | `thinking.budget_tokens = budget`, `max_tokens += budget` | Anthropic이 max_tokens에서 thinking 차감 | `""` (API 레벨 처리) |
| OpenAI | `reasoning_effort = low/medium/high` | budget ≤1024→low, ≤8192→medium, >8192→high | `""` (API 레벨 처리) |

Thinking 블록 처리 플로우:
1. Ollama thinking 모델 → `<think>...</think>` 블록을 텍스트에 출력
2. `parse_react()`가 `_strip_thinking_blocks()`로 블록 분리
3. 분리된 thinking 내용은 `ReActResult.thinking`에 보존
4. 나머지 텍스트(JSON)만 파싱 → Stage 1 직접 성공률 향상

### 7.5 재시도 헬퍼 (`providers/http.py`)

세 프로바이더 모두 동일한 재시도 래퍼 `post_with_retry(requests.post, url, **kwargs)`를 거쳐 HTTP를 발송합니다. 목적은 on-prem LLM 서버(Ollama / vLLM)에서 간헐적으로 발생하는 일시적 네트워크 오류 — 서버 재시작 직후의 `ConnectionError`, 첫 호출 시 모델 로딩이 늦어서 발생하는 `Timeout` — 을 사용자 레벨로 노출하지 않고 복구하는 것입니다.

**범위: pre-stream only.** `requests.post()` 호출 자체에서 발생한 예외만 재시도합니다. 스트리밍이 시작된 이후(즉 `requests.post(stream=True)`가 Response를 돌려준 뒤) 청크를 읽다가 발생한 오류는 재시도 대상 아님 — 이미 소비된 청크가 중복되면 LLM 출력이 깨지기 때문.

**재시도 대상 예외:**
- `requests.Timeout` (ConnectTimeout, ReadTimeout 포함)
- `requests.ConnectionError`
- HTTP 4xx/5xx는 재시도 **안 함**. `raise_for_status()`는 `post_with_retry` 반환 *뒤에* 호출되어 서버의 거절 응답을 그대로 caller로 전달.

**백오프:** 고정 1초 (지수 아님). on-prem 단일 사용자 전제라 rate-limit / thundering-herd 대책이 필요 없고, `ConnectionError` 직후 서버 부팅 마무리에만 약간의 헤드룸을 주면 충분. `Timeout`은 이미 긴 대기였으므로 추가 대기 효과는 작지만 해롭지도 않음.

**설정:**
- `AGENT_CLI_LLM_RETRY_ATTEMPTS` (기본 3, 최초 포함 총 시도 횟수; 0/음수는 1로 clamp)
- `AGENT_CLI_LLM_RETRY_DELAY` (기본 1.0초)

**가시성:** 재시도 시 `render_status("running", ...)` 한 줄로 사용자에게 표시(예: `LLM request failed (Timeout) — retrying (2/3)`). spinner는 계속 돌아감. 모두 실패하면 `render_status("error", ...)` 후 마지막 예외를 그대로 raise. verbose 모드에서는 `agent_cli.verbose.debug_log`로 stderr에도 한 줄 남김.

**테스트 호환:** `post_with_retry`는 `post_fn`을 인자로 받고, 각 프로바이더는 자기 네임스페이스의 `requests.post`를 명시적으로 넘깁니다. 덕분에 기존 테스트가 `agent_cli.providers.{name}.requests.post`를 패치하는 패턴이 그대로 동작.

### 7.6 공용 debug 유틸 (`verbose.py`)

`agent_cli/verbose.py`가 verbose 플래그와 `debug_log()`의 단일 소유자입니다. 과거에는 `loop.py` 모듈 안에 `_debug_verbose` / `_debug_log`로 있었으나, `providers/http.py`가 재시도 로그를 찍어야 하면서 provider 레이어가 loop를 역참조하지 않도록 추출했습니다. `loop.py`는 하위 호환을 위해 해당 심볼을 그대로 재-export합니다.

---

## 8. 설정 시스템

### 8.0 config.json (프로바이더/모델 설정)

```json
{
  "provider": "ollama",
  "base_url": "http://localhost:11434",
  "api_key": "",
  "default_model": "qwen3:32b"
}
```

**3레이어 병합** (`load_config()`):
```
env vars (AGENT_CLI_*)  →  최저 우선순위
~/.agent-cli/config.json →  사용자 전역
.agent-cli/config.json   →  워크스페이스 (최고)
+ CLI 파라미터             →  임시 오버라이드
```

필드 단위 병합: 상위 레이어가 해당 필드를 가지면 덮어씀, 없으면 하위에서 상속.

**SetupWizard** (`setup.py`): 설정 파일이 없으면 자동 실행.
`agent-cli setup`으로 수동 재설정 가능.

**DIRECTIVE.md** — 프로젝트 지시사항 (`prompts/system_prompt.py`):
```
.agent-cli/DIRECTIVE.md   →  프로젝트별 규칙 (우선 로드)
~/.agent-cli/DIRECTIVE.md →  사용자 전역 규칙
```
- 둘 다 존재하면 모두 로드 (content hash 중복 제거)
- content hash 중복 제거, truncation 없음 (ResourceLoader 기반)
- 매 세션 시작 시 system prompt 동적 영역에 주입

### 8.1 models.json 구조

```json
{
  "models": {
    "<model_id>": {
      "provider": "anthropic | openai | ollama",
      "context_window": 32768,
      "max_output_tokens": 4096,
      "supports_structured_output": true,
      "supports_thinking": true,
      "thinking_budget": 4096,
      "supports_strict_schema": false
    }
  },
  "provider_defaults": {
    "ollama": {"base_url": "http://localhost:11434", "default_model": "qwen3:32b"},
    "openai": {"base_url": "https://api.openai.com/v1", "default_model": "gpt-4o"},
    "anthropic": {"base_url": "https://api.anthropic.com/v1", "default_model": "claude-sonnet-4-20250514"}
  }
}
```

### 8.2 파일 위치 및 정책

| 우선순위 | 위치 | 역할 | 자동 저장 |
|---------|------|------|----------|
| 1 | `.agent-cli/models.json` | 프로젝트 로컬 오버라이드 | 안 함 (읽기만) |
| 2 | `~/.agent-cli/models.json` | 사용자 전역 설정 | 새 모델 자동 저장 |
| 3 | `agent_cli/default_models.json` | 패키지 기본값 | 안 함 (읽기만) |

### 8.3 설정 로딩 우선순위 (`config.py`)

3개 파일을 병합하되, 높은 우선순위가 낮은 우선순위를 오버라이드:
1. `agent_cli/default_models.json` (패키지) — 먼저 로딩
2. `~/.agent-cli/models.json` (전역) — 동일 키 덮어쓰기
3. `.agent-cli/models.json` (프로젝트 로컬) — 동일 키 덮어쓰기 (최종)
4. 하드코딩 폴백 (모든 파일 없어도 동작)

### 8.4 능력치 조회 우선순위 (`providers/compat.py`)

1. `models.json` 정적 설정 (병합된 결과)
2. 런타임 감지 → **`~/.agent-cli/models.json`에 자동 저장**
   - Ollama: `/api/show` (메타데이터) + `/api/chat` (thinking 프로브 + format 프로브)
   - OpenAI 호환: `/chat/completions` (thinking 프로브)
3. `DEFAULT_CAPABILITIES` (context_window=4096, 모든 기능 비활성)

프로브는 진행 콜백을 받아 첫 실행 시 어느 단계가 돌고 있는지 사용자에게 표시 (`set_progress_callback`). 한 번 감지된 결과는 `_auto_detected: true` 마커와 함께 저장되어 재실행 시 프로브 생략.

### 8.6 Thinking 감지 방식

하드코딩 패턴 매칭이 아닌 **프로브 기반 감지**:
1. 모델에 "What is 2+2?" 프롬프트 전송
2. 두 가지 위치에서 thinking 확인:
   - `message.thinking` 필드 (Ollama API — Qwen3, Qwen3.5, GLM 등)
   - `<think>`, `<thinking>`, `<reasoning>`, `<reflection>` 태그 in content (DeepSeek-R1 등)
3. 감지되면 → `supports_thinking=True`, `thinking_format=감지방식`
4. 결과를 `~/.agent-cli/models.json`에 저장 (`_auto_detected: true`) → 다음 실행 시 프로브 불필요
5. 모델 업데이트 시 자동 감지 항목은 재감지로 갱신됨 (수동 등록 항목은 보호)

새 모델이 추가되어도 코드 수정 없이 자동 감지됩니다.

OpenAI 호환 서버(vLLM 등)에서는 `/v1/models` API로 context window도 감지합니다 (`max_model_len` 필드).

#### Format 프로브 (Ollama 전용)

일부 모델/백엔드 조합 (예: Ollama의 mlx tensor 포맷)은 `format="json"` 파라미터를 받으면 런타임 에러를 냅니다. 프로브가 형식 강제 호출을 한 번 시도해 성공하면 `supports_structured_output=True`, 실패하면 False — 결과는 모델별로 캐시되어 이후 호출에서 자동 적용. 사용자가 직접 모델 호환성을 추측할 필요가 없습니다.

### 8.5 모델 정보 출력

| 상황 | 출력 |
|------|------|
| 새 모델 감지 + 저장 | Rich Panel (상세 — context, thinking, tool calling 등) |
| 기존 모델 로딩 | 한 줄 요약 (`● Model: name (ctx=N, thinking=✓)`) |
| `--headless` 모드 | 세션 미생성 (모델 정보는 정상 출력) |

---

## 9. 시스템 프롬프트 아키텍처 (`prompts/system_prompt.py`)

LLM attention 패턴에 최적화된 섹션 순서 — Primacy(앞), Middle(중간), Recency(끝):

```
build_system_prompt(capabilities, active_tools, include_delegate, skill_stack, session_id, agent_role)
    │
    │  ── Primacy: 정체성 + 핵심 원칙 (강한 attention) ──
    │
    ├─ ROLE_PROMPT (항상 포함 — 에이전트 역할 정의)
    │
    ├─ CONTEXT_DISCIPLINE (항상 포함 — 컨텍스트 창이 핵심 리소스임을 교육)
    │   └─ "읽을 것만 읽어라 / thought 간결 / 불필요한 덤프 금지"
    │
    ├─ TASK_GUIDELINES (항상 포함 — 코드 작업 원칙 7개)
    │   └─ 코드 읽기 선행, 범위 제한, 보안, 정직한 보고 등
    │
    ├─ FORMAT_RULES (항상 포함 — JSON ReAct 포맷 + 규칙 10개)
    │   └─ ready_for_review → complete 워크플로, 재귀 금지, 단일 액션 강제,
    │      효율적 액션 선택 (batch 필드 활용 / shell 파이프라이닝 / 좁은 read 모드 우선)
    │
    │  ── Middle: 레퍼런스 (필요시 참조) ──
    │
    ├─ Available Tools (active_tools + _ALWAYS_INCLUDE)
    │   └─ 정적 도구 먼저 (KV cache 안정), 조건부 도구 뒤에
    │   └─ 가이드가 해당 도구에 inline (별도 섹션 없음):
    │       - edit_file ← Hashline Guide
    │       - delegate ← Delegation Guide
    │
    ├─ Available Skills (skill_stack에 없는 스킬만, run_skill 사용 안내)
    │
    ├─ Available Agents (depth < max_depth + agent_stack 재귀 방지)
    │   └─ .agent-cli/agents/ + ~/.agent-cli/agents/ + builtin/ 스캔
    │
    │  ── Recency: passive reference → active rules → immediate constraint ──
    │
    ├─ Environment (항상 포함 — CWD, 플랫폼)
    │   └─ 날짜는 의도적으로 제외 — KV prefix cache 안정성 (자정 rollover 방지)
    │
    ├─ Context Recovery Guide (session_dir가 있을 때만)
    │   └─ "이전 대화 내용이 필요하면 read_file({session_dir}/history.jsonl)"
    │
    ├─ Directives (DIRECTIVE.md가 존재할 때만)
    │   └─ .agent-cli/DIRECTIVE.md (프로젝트) + ~/.agent-cli/DIRECTIVE.md (유저 전역)
    │
    └─ Execution Context (skill_stack/agent_stack이 있을 때만 — Recency 마지막)
        ├─ "Call stack: main → agent:reviewer → skill:plan"
        ├─ "Do not delegate to or invoke: reviewer, plan (already in call stack)"
        └─ 세션 내 변동 가능한 유일한 Recency 섹션 → 끝에 두어 앞 3개를 안정적
           KV prefix로 보존
    
    Role 선택 (Primacy 영역):
    - main: 기본 ROLE_PROMPT
    - delegate: Agent Role이 기본 Role을 대체
    - skill: parent의 Role 상속
```

---

## 10. 테스트 아키텍처

### 10.1 테스트 분류

| 분류 | 파일 수 | 테스트 수 | 실행 방법 |
|------|---------|----------|----------|
| 유닛 테스트 | 36 | 985 | `pytest tests/ -m "not ollama_integration"` |
| 통합 테스트 | 1 | 22 | `pytest tests/test_integration.py` |
| **전체** | **37** | **1007** | `pytest tests/` |

### 10.2 통합 테스트 모델 구성 (`tests/conftest.py`)

```python
DEFAULT_MODELS = [
    "qwen3-coder:30b",       # Thinking + 코딩 특화
    "glm-4.7-flash:q8_0",    # Non-thinking 범용
    "qwen3.5:35b",            # 최신 세대 범용
]
```

모델 변경:
```bash
# 환경 변수로 변경
INTEGRATION_MODELS="model1,model2" pytest tests/test_integration.py

# conftest.py의 DEFAULT_MODELS 리스트 수정
```

### 10.3 테스트 실행

```bash
# 유닛 테스트만 (빠름, CI용)
pytest tests/ -m "not ollama_integration" -v

# 통합 테스트만 (Ollama 필요)
pytest tests/test_integration.py -v

# 전체
pytest tests/ -v

# 특정 모듈
pytest tests/test_react_parser.py -v
```

---

## 11. CLI 명령어 레퍼런스

### 11.1 `run` — 단발 실행

```bash
agent-cli run "task description" [options]
  -p, --provider    ollama | openai | anthropic    (기본: ollama)
  -m, --model       모델 ID                       (기본: 프로바이더 기본값)
  --base-url        API 엔드포인트
  --api-key         API 키 (환경 변수 자동 감지)
  -n, --max-turns    최대 턴 (0=무제한)
  --max-depth       서브에이전트 중첩 깊이 (기본: 2)
  --delegate-timeout 서브에이전트 타임아웃 초 (기본: 300)
  -v, --verbose     원시 LLM 응답 표시

  /sh <cmd>         LLM 없이 셸 명령 직접 실행

  # 내부 플래그 (서브에이전트용)
  --headless        세션 없음 + 출력 억제 + tmpdir 컨텍스트 (휘발)
  --depth N         현재 중첩 깊이
```

`run`도 `chat`과 동일하게 세션/컨텍스트(FIFO + history.jsonl)를 관리합니다. 완료 후 세션 ID가 출력되며 `chat --resume <id>`로 이어서 작업할 수 있습니다. `--headless`는 서브에이전트(delegate) 전용으로, tmpdir 기반 휘발성 컨텍스트를 사용하고 세션을 저장하지 않습니다.

### 11.2 `chat` — 대화형 모드

```bash
agent-cli chat [options]
  (run 옵션 포함)

  # 대화 중 명령어:
  /help, /?          명령어 목록
  /quit, /exit       세션 종료
  /clear             컨텍스트 초기화
  /sh <cmd>          셸 명령 실행
  /skills            사용 가능한 스킬 목록
  /<skill> <args>    스킬 실행
  /ctx_window        컨텍스트 윈도우 덤프 (디버그)
```

---

## 12. 확장 가이드

### 12.1 새 프로바이더 추가

1. `providers/` 디렉토리에 새 파일 생성 (예: `google.py`)
2. `LLMProvider` 프로토콜을 만족하는 클래스 구현:
   ```python
   class GoogleProvider:
       def __init__(self, base_url: str, api_key: str): ...
       def call(self, messages, system, model, capabilities, **kwargs) -> LLMResponse: ...
   ```
3. `providers/__init__.py`의 `create_provider()`에 분기 추가
4. `config.py`의 `_PROVIDER_FALLBACKS`에 기본값 추가
5. `models.json`에 모델 등록
6. `tests/test_providers.py`에 테스트 추가

### 12.2 새 도구 추가

1. `tools/` 디렉토리에 새 파일 생성 (예: `search.py`)
2. `tool_search(args: dict) -> str` 함수 구현
3. `tools/registry.py`의 `TOOL_SCHEMAS`에 스키마 추가
4. `tools/__init__.py`의 `TOOLS` dict에 등록
   - 가상 도구(loop 인터셉트)면 `VIRTUAL_TOOLS`에도 추가
   - 항상 포함되어야 하면 `_ALWAYS_INCLUDE`에도 추가
5. `tests/test_registry.py`에 검증 테스트 추가

### 12.3 새 모델 등록

`models.json`에 항목 추가:
```json
"new-model:14b": {
  "provider": "ollama",
  "context_window": 16384,
  "max_output_tokens": 4096,
  "supports_structured_output": true,
  "supports_thinking": false,
  "thinking_budget": 0,
  "supports_strict_schema": false
}
```

미등록 모델은 런타임 감지(Ollama) 또는 보수적 기본값으로 동작합니다.

---

## 13. 스킬 시스템 (`skills/`)

### 13.1 개요

프롬프트 스킬은 특정 작업에 최적화된 재사용 가능한 프롬프트 템플릿입니다. Claude Code의 스킬 파일 포맷과 호환되도록 설계되었습니다.

### 13.2 스킬 파일 포맷 (Claude Code 호환)

```markdown
---
name: review-code
description: Review code for bugs and security
allowed-tools: [read_file]
max-turns: 5
argument-hint: "<file_path>"
---

You are a code reviewer. Read $ARGUMENTS and analyze for bugs.
```

| Frontmatter 필드 | 타입 | 설명 |
|-----------------|------|------|
| `name` | string | 슬래시 명령어 이름 |
| `description` | string | 스킬 설명 |
| `allowed-tools` | list[str] | 허용 도구 (미지정 시 전체) |
| `max-turns` | int | 최대 턴 (미지정 시 기본값) |
| `argument-hint` | string | 인자 힌트 |

### 13.3 인자 치환

| 패턴 | 설명 |
|------|------|
| `$ARGUMENTS` | 전체 인자 문자열 |
| `$0`, `$1`, ... | N번째 인자 (0-indexed) |

### 13.4 스킬 검색 경로

1. `.agent-cli/skills/*.md` (프로젝트 로컬, 최우선)
2. `~/.agent-cli/skills/*.md` (사용자 전역)
3. `agent_cli/skills/builtin/*.md` (패키지 내장, 최하위)

동일 name의 스킬이 여러 위치에 있으면 상위 우선순위가 오버라이드합니다.

패키지 내장 스킬:
- `create-skill` — 새 스킬 파일 대화형 생성
- `create-agent` — 새 에이전트 정의 파일 대화형 생성
- `plan` — 기능 요청을 작업 분해 + 의존성 + 범위 추정으로 구조화 (plan/ 저장)

### 13.5 실행 플로우

```
사용자 입력: /review-code src/auth.py
    │
    ▼
load_skills() — 호출 시점마다 디스크 재스캔, 파일 파싱
    │  └─ 캐시 없음. /create-skill로 방금 만든 스킬도 재시작 없이 즉시 인식
    ▼
스킬 매칭: "review-code" → Skill 객체
    │
    ▼
substitute_arguments() — $ARGUMENTS → "src/auth.py" 치환
    │
    ▼
run_loop(query=치환된_프롬프트, allowed_tools=["read_file"], max_turns=5)
    │  └─ loop.py의 기존 인프라 그대로 활용
    ▼
결과 반환
```

### 13.6 스킬 스택 (재귀 방지)

스킬이 `run_skill`로 다른 스킬을 호출할 수 있지만, 재귀는 방지:

```
A→B: 허용 (summarize → optimize)
A→A: 차단 (summarize → summarize)
A→B→A: 차단 (summarize → optimize → summarize)
```

방어 메커니즘 3단계:
1. **skill_stack** — `run_loop`이 `skill_stack: list[str]`를 추적. `_handle_run_skill`이 스택에 같은 이름이 있으면 에러 반환.
2. **시스템 프롬프트** — `build_skill_descriptions(exclude_names=skill_stack)`로 현재 실행 중인 스킬을 Available Skills에서 숨김. LLM이 재귀 시도 자체를 하지 않도록 유도.
3. **프롬프트 규칙** — Rule 7: "NEVER invoke yourself recursively via shell"

### 13.7 커스텀 스킬 작성

`.agent-cli/skills/my-skill.md` 파일을 생성하면 자동으로 `/my-skill` 명령어가 등록됩니다.

### 13.8 기본 내장 스킬

| 스킬 | 도구 | 설명 |
|------|------|------|
| `/review-code <file>` | read_file, shell | 코드 리뷰 (버그, 보안, 성능) |
| `/summarize <path>` | read_file, shell | 파일/디렉토리 요약 |
| `/test <file>` | read_file, write_file, shell | 유닛 테스트 생성 |
| `/optimize <path>` | read_file, shell, write_file | 코드 최적화 분석 → OptimizationToDo.md |

---

## 14. Hook 시스템 (`hooks/`)

### 14.1 개요

Python hook + shell hook 두 가지 방식의 라이프사이클 훅을 지원한다.
- **Python hook**: `.agent-cli/hooks/*.py` — context window 조작, MCP 메모리 접근 가능
- **Shell hook**: `.agent-cli/hooks.json` — 외부 명령 실행 (기존 방식, 하위 호환)
- **Skill-local shell hook**: SKILL.md frontmatter의 `hooks:` 섹션 — 해당 스킬이 실행되는 동안만 적용되는 로컬 matcher. 호출자의 hooks_config와 `merge_hooks_configs(parent, skill.hooks)`로 합쳐져서 부모 훅과 함께 발동.
- **Agent-local shell hook**: 에이전트 정의 파일(`.agent-cli/agents/*.md`) frontmatter의 `hooks:` 섹션 — 해당 에이전트로 delegate 되는 동안만 적용되는 로컬 matcher. skill과 동일한 merge 계약: `merge_hooks_configs(parent, agent.hooks)`로 부모 훅 뒤에 덧붙여 fire.
- **Delegate 전파**: `tool_delegate`가 `hooks_config`를 subagent `run_loop`에 그대로 전달. 즉 전역/프로젝트/스킬 훅은 모두 상속되고, 에이전트 자신의 overlay까지 그 위에 얹힘.

### 14.2 라이프사이클 이벤트 (11개)

| 이벤트 | 시점 | 함수명 |
|--------|------|--------|
| OnSessionStart | 세션 시작 후 | `on_session_start(ctx)` |
| PreLLMCall | LLM 호출 직전 (매 턴) | `pre_llm_call(ctx)` |
| PostLLMCall | LLM 응답 수신 후 | `post_llm_call(ctx)` |
| PreToolUse | 도구 실행 직전 | `pre_tool_use(ctx)` |
| PostToolUse | 도구 실행 직후 | `post_tool_use(ctx)` |
| OnTurnEnd | 턴 종료 후 | `on_turn_end(ctx)` |
| OnDelegateStart | delegate 실행 직전 | `on_delegate_start(ctx)` |
| OnDelegateEnd | delegate 완료 후 | `on_delegate_end(ctx)` |
| OnSkillStart | skill 실행 직전 | `on_skill_start(ctx)` |
| OnSkillEnd | skill 완료 후 | `on_skill_end(ctx)` |
| OnSessionEnd | 세션 종료 시 | `on_session_end(ctx)` |

### 14.3 Python Hook 파일 규약

```python
# .agent-cli/hooks/00_memory.py
EVENTS = ["OnSessionStart", "OnTurnEnd"]

def on_session_start(ctx):
    memories = ctx.search_memory("project context")
    if memories:
        ctx.inject_system_section("Memory", format_memories(memories))

def on_turn_end(ctx):
    ctx.store_memory([{"name": "...", "entityType": "decision", "observations": [...]}])
```

- 파일명 숫자 prefix 순서 실행 (`00_` → `10_` → `20_`)
- 프로젝트 hooks → 유저 hooks 순서
- `EVENTS` 리스트로 구독할 이벤트 선언
- 에러 발생 시 해당 hook 건너뜀 (에이전트 루프 중단 없음)

### 14.4 HookContext

hook 함수가 받는 컨텍스트 객체:
- **읽기**: `event`, `messages`, `session_dir`, `turn`, `tool_name`, `tool_input`, `tool_result`, `llm_response`
- **context 조작**: `inject_message()`, `inject_system_section()`, `remove_system_section()`
- **도구 제어** (PreToolUse): `block(reason)`, `modify_input(new_input)`
- **MCP 메모리**: `store_memory()`, `search_memory()`, `read_memory()`

### 14.5 실행 순서

```
이벤트 발생 → HookContext 생성 → Python hooks (파일명 순) → Shell hooks (hooks.json)
```

### 14.6 loop.py 통합

```
AgentLoop.run()
  ├─ _setup() → OnSessionStart
  ├─ _execute_turn()
  │   ├─ PreLLMCall → system_sections 적용
  │   ├─ _call_llm()
  │   ├─ PostLLMCall
  │   ├─ self._dispatch_tool_with_hooks()
  │   │   ├─ PreToolUse (Python) → PreToolUse (Shell)
  │   │   ├─ OnDelegateStart / OnSkillStart
  │   │   ├─ 도구 실행
  │   │   ├─ OnDelegateEnd / OnSkillEnd
  │   │   └─ PostToolUse (Python) → PostToolUse (Shell)
  │   └─ OnTurnEnd
  └─ OnSessionEnd (finally)
```

---

## 15. 설계 원칙

1. **모델은 commodity, harness가 성패를 결정한다** — 파싱 폴백, 도구 출력 압축, 퍼지 편집 등 harness 레벨 최적화가 핵심
2. **프로바이더별 최선의 방식 자동 선택** — 네이티브 tool calling > basic JSON mode > 텍스트 파싱 (strict JSON Schema는 확장성 이슈로 미사용)
3. **소형 모델 우선 설계** — 보수적 기본값, 적응형 출력 압축, 스키마 자동 변환
4. **비용 제로 보정 우선** — LLM 재호출 없이 harness에서 보정 (퍼지 매칭, 타입 변환)
5. **점진적 기능 저하** — 기능 미지원 시 에러 대신 다음 폴백으로 graceful degradation
6. **순환 의존 없는 단방향 모듈 구조** — config → compat → base → adapters → loop → main
