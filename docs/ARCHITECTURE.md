# Agent-CLI v2 아키텍처 문서

> **이 문서는 코드와 함께 유지보수되어야 합니다.**
> 코드 수정 시 관련 섹션을 반드시 업데이트하세요.
>
> 최종 업데이트: 2026-03-31
> 버전: 2.0.0-dev
> 총 소스: 7,144 LOC (43 Python 파일) + 8,905 LOC 테스트 (23 파일)
> 총 테스트: 548 유닛 + 56 통합 = 604개

---

## 1. 프로젝트 개요

Agent-CLI는 on-premise LLM을 위한 모듈형 에이전트 CLI입니다. ReAct(Reasoning + Acting) 패턴으로 LLM이 도구를 사용하여 단계적으로 작업을 수행합니다.

### 핵심 특징

- **멀티 프로바이더**: Anthropic, OpenAI 호환(vLLM, LM Studio, mlx-lm), Ollama
- **3단계 파싱 폴백**: json.loads → JSON repair → regex 추출
- **Constrained Decoding**: Ollama JSON Schema, OpenAI response_format, Anthropic tool calling
- **Hashline 편집**: CRC32 해시 기반 정밀 파일 편집 + 퍼지 매칭
- **컨텍스트 압축**: LLM 기반 구조화 요약 + 증분 업데이트
- **모델 적응형**: context window, thinking budget에 따른 자동 조정

### 외부 의존성

| 패키지 | 버전 | 용도 |
|--------|------|------|
| `typer` | >=0.9 | CLI 프레임워크 |
| `rich` | >=13.0 | 터미널 렌더링 (Panel, Table, Rule 등) |
| `requests` | >=2.28 | HTTP 클라이언트 (LLM API 호출) |
| `pyyaml` | >=6.0 | 스킬 frontmatter 파싱 |

표준 라이브러리: json, re, dataclasses, pathlib, subprocess, os, sys, zlib, textwrap, unicodedata

---

## 2. 디렉토리 구조

```
agent_cli/
├── __init__.py              (3)    패키지 버전 (__version__ = "2.0.0-dev")
├── __main__.py              (5)    python -m agent_cli 진입점
├── main.py                  (732)  CLI 명령어: run, chat, setup, sessions + 공유 헬퍼
├── config.py                (215)  config.json 3레이어 로딩 + models.json 레지스트리
├── setup.py                 (229)  SetupWizard (Rich TUI, 첫 실행 설정 마법사)
├── constants.py             (20)   공유 상수 (타임아웃, 임계값, 메시지 템플릿)
├── default_models.json             패키지 기본 모델 정의 (6개 모델)
├── hooks.py                 (215)  Hook 시스템 (PreToolUse/PostToolUse/PostToolUseFailure)
├── input_history.py         (67)   readline 설정 + 채팅 히스토리 영속화
├── loop.py                  (1531) AgentLoop 클래스 + ReAct 루프 + scratchpad/hook/run_skill
├── render.py                (247)  Rich 터미널 렌더링 + 모델 정보 + compact observation
│
├── providers/                      LLM 프로바이더 어댑터
│   ├── __init__.py          (33)   create_provider() 팩토리
│   ├── base.py              (36)   LLMProvider 프로토콜, LLMResponse, TokenUsage
│   ├── compat.py            (306)  ModelCapabilities + 프로브 감지 + 자동 저장
│   ├── anthropic.py         (91)   Anthropic Messages API (tool_use + thinking)
│   ├── openai_compat.py     (101)  OpenAI 호환 API (function calling + reasoning)
│   └── ollama.py            (104)  Ollama API (constrained decoding + thinking)
│
├── parsing/                        응답 파싱
│   ├── __init__.py          (3)    re-export: parse_react, ReActResult
│   ├── react_parser.py      (156)  3단계 폴백 ReAct 파서 + thinking 분리
│   ├── json_repair.py       (175)  깨진 JSON 복구 (6단계 파이프라인)
│
├── tools/                          도구 시스템
│   ├── __init__.py          (67)   TOOLS dict (실제+가상) + VIRTUAL_TOOLS + execute_tool() → ToolResult
│   ├── result.py            (14)   ToolResult 데이터클래스 (모든 도구의 표준 반환 타입)
│   ├── registry.py          (387)  스키마 정의, 검증, API 형식 변환
│   ├── run_skill.py         (66)   run_skill 도구 (LLM 자동 스킬 호출) → ToolResult
│   ├── read_artifact.py     (141)  read_artifact 도구 (artifact 읽기/목록/검색) → ToolResult
│   ├── read_file.py         (102)  파일 읽기 + hashline 포맷팅 + 부분 읽기 → ToolResult
│   ├── write_file.py        (21)   파일 생성 → ToolResult
│   ├── edit_file.py         (164)  파일 편집 (hashline + 퍼지 매칭 + edits 필터링) → ToolResult
│   ├── shell.py             (40)   셸 명령 실행 → ToolResult
│   ├── delegate.py          (85)   서브에이전트 위임 → ToolResult
│   ├── context.py           (63)   read_context 도구 (세션 이력 조회) → ToolResult
│   (truncation.py 삭제됨 — tool output은 잘림 없이 그대로 LLM에 전달)
│
├── context/                        컨텍스트 관리
│   ├── __init__.py          (34)   re-export
│   ├── token_estimator.py   (23)   토큰 추정 (chars/4)
│   ├── overflow.py          (45)   프로바이더별 오버플로 감지
│   ├── manager.py           (337)  ContextManager (세션별 scratchpad, 스킬 컨텍스트, compaction 힌트)
│   ├── scratchpad.py        (413)  Scratchpad + Artifact + ContextBudget + 세션/스킬 격리
│   └── session.py           (214)  프로젝트 로컬 세션 영속화 (sessions/{id}/ 구조)
│
├── prompts/                        프롬프트 템플릿
│   ├── __init__.py          (1)
│   ├── system_prompt.py     (182)  조건부 시스템 프롬프트 빌더 + 스킬/artifact 안내
│   └── compression_prompt.py (36)  요약/증분 업데이트 프롬프트
│
├── skills/                         프롬프트 스킬 시스템
│   ├── __init__.py          (7)    re-export
│   ├── models.py            (21)   Skill 데이터 모델 (model/context/hooks/invocation)
│   ├── loader.py            (136)  스킬 파일 검색/파싱 (플랫+디렉토리, PyYAML, hooks, 캐싱)
│   └── executor.py          (127)  인자/변수/!`cmd` 치환 + model/context/skill_name 오버라이드

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
│compat    ││json_   ││read_  ││token_  ││compres-│
│ollama    ││repair  ││write_ ││estima- ││sion_   │
│compat    ││        ││edit_  ││tor     ││prompt  │
│base      ││        ││shell  ││        ││        │
│          ││        ││dele-  ││        ││        │
│          ││        ││gate   ││        ││        │
│          ││        ││trun-  ││        ││        │
│          ││        ││cation ││        ││        │
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
providers/compat.py → config
providers/base.py   → providers/compat
providers/*.py      → providers/base, providers/compat
parsing/json_repair → (외부만: json, re)
parsing/react_parser→ parsing/json_repair
tools/result.py     → (외부만: dataclasses, 순수 데이터 타입)
tools/read_file.py  → tools/result, (외부만: re, zlib, pathlib)
tools/edit_file.py  → tools/read_file, tools/result
tools/shell.py      → tools/result
tools/write_file.py → tools/result
tools/context.py    → tools/result, context/session
tools/delegate.py   → tools/result
tools/read_artifact → tools/result
tools/run_skill.py  → tools/result
tools/registry.py   → (외부만: json, dataclasses)
context/token_est.  → (외부만: 없음)
context/overflow.py → context/token_estimator, providers/compat
context/manager.py  → context/overflow, context/token_estimator,
                      prompts/compression_prompt, providers/base, providers/compat
prompts/system_pr.  → providers/compat, tools/registry
loop.py             → constants, context/manager, context/overflow, parsing/react_parser,
                      prompts/system_prompt, providers/base, providers/compat,
                      render, tools, tools/delegate, tools/registry
skills/loader.py    → skills/models
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

# tool_calls 항목 형식:
# {"id": "tu_1", "name": "read_file", "input": {"path": "a.py"}}
```

### 4.2 모델 능력치 (`providers/compat.py`)

```python
@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int               # 컨텍스트 윈도우 크기 (토큰)
    max_output_tokens: int            # 최대 출력 토큰
    supports_structured_output: bool  # constrained decoding (Ollama format, OpenAI json_schema)
    supports_tool_calling: bool       # 네이티브 function/tool calling API
    supports_thinking: bool           # thinking/reasoning 지원
    thinking_budget: int              # thinking 토큰 예산 (0=비활성)
    supports_strict_schema: bool      # strict JSON Schema 모드
    thinking_format: str = ""         # thinking 블록 태그 ("think", "reasoning", "")
```

`thinking_format` 값:
- `"think"` — `<think>...</think>` 형식 (Qwen3, DeepSeek-R1)
- `"reasoning"` — `<reasoning>...</reasoning>` 형식
- `""` — thinking 블록 미사용 (Anthropic API 레벨 처리, GPT 등)

능력치 조회 우선순위:
1. `models.json` 정적 설정 (최우선)
2. 런타임 API 감지 (Ollama `/api/show`)
3. 보수적 기본값 (4096 context, 모든 기능 비활성)

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
```

### 4.4 도구 스키마 (`tools/registry.py`)

```python
@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict  # JSON Schema 형태

# 등록된 도구: read_file, write_file, edit_file, shell, read_context,
#               complete, ask, run_skill, read_artifact, ready_for_review
# 가상 도구 (loop에서 인터셉트):
# VIRTUAL_TOOLS = frozenset({"complete", "ask", "run_skill", "read_artifact", "ready_for_review"})
# _ALWAYS_INCLUDE = ("complete", "ready_for_review") — allowed_tools와 무관하게 항상 API tool 목록에 포함
# delegate는 별도 DELEGATE_TOOL_SCHEMA로 관리
```

---

## 5. 핵심 플로우

### 5.1 ReAct 에이전트 루프 (`loop.py` — `AgentLoop` 클래스)

#### 컨텍스트 윈도우 레이아웃

`ctx.get_messages()` 반환 순서 (매 LLM 호출 시):

```
[scratchpad]  [Scratchpad — persistent task context]     ← 스킬 내부에서는 스킵
              goal, progress, decisions

[summary]     [Previous conversation summary]             ← compaction 후만
              요약 + "[artifact 경로를 read_artifact로 복구 가능]"

[messages]    user: "hooks.py 분석해줘"                    ← 실제 대화
              assistant: {"action": "read_file", ...}
              user: "Observation: STATUS: success..."
              assistant: "분석 완료"                        ← complete 결과
              user: "Used skill: optimize(./) — ..."       ← 스킬 호출 기록
              assistant: "Analysis complete..."             ← 스킬 결과
```

#### ctx.add 책임 분리

main.py는 ctx.add를 직접 호출하지 않음. 각 컴포넌트가 자기 영역 책임:

| 컴포넌트 | ctx.add 내용 |
|----------|-------------|
| AgentLoop._setup | `("user", query)` |
| _append_*_observation | `("assistant", llm_text)` + `("user", observation)` |
| AgentLoop complete/echo | `("assistant", final_answer)` |
| _dispatch_skill | `("user", "Used skill: ...")` + `("assistant", result)` |
| AgentLoop._maybe_checkpoint | `("user", checkpoint_msg)` |

#### 루프 플로우

```
AgentLoop.run()
    │
    ├─ _install_signal_handler()   ← Ctrl+C를 flag로 변환
    ├─ _setup()
    │   ├─ 시스템 프롬프트 빌드 (capabilities, tools, skill_stack, session_id)
    │   ├─ scratchpad 초기화 (최초 1회)
    │   └─ ctx.add("user", query) → ctx.get_messages()
    │
    ├─ while _should_continue():
    │    │
    │    ├─ ★ CHECK: _interrupted? → _on_interrupt() → return None
    │    │     ctx.add("user", "⚡ User interrupted...")
    │    │     scratchpad progress: "⚡ Interrupted at iteration N"
    │    │
    │    ├─ _begin_iteration() → turn 카운터, 체크포인트
    │    │
    │    ├─ _call_llm() → LLMResponse (overflow 시 압축 후 재시도)
    │    │                 ← Ctrl+C 와도 flag만 설정, 호출은 완료
    │    │
    │    └─ _handle_native_path() 또는 _handle_text_path()
    │         │
    │         ├─ [ready_for_review] → 원본 query + scratchpad progress를 observation으로 반환
    │         │                      → LLM이 검증 후 complete 또는 추가 작업
    │         │
    │         ├─ [complete] → ctx.add("assistant", answer)
    │         │               → artifact 저장 + scratchpad progress
    │         │               → return answer
    │         │
    │         ├─ [run_skill] → 내부 AgentLoop (ctx 공유, scratchpad 스킵)
    │         │                → 결과를 observation으로 주입
    │         │
    │         ├─ [도구] → execute (잘림 없이 전체 출력 전달)
    │         │           → ctx.add(assistant + observation)
    │         │           → artifact 저장 + scratchpad progress
    │         │
    │         └─ [compaction] → 요약 + artifact 복구 힌트
    │
    └─ _restore_signal_handler()   ← 원래 핸들러 복원
```

**Graceful Interrupt (`graceful_interrupt=True`, chat 전용):**
- 1st Ctrl+C: `_interrupted` flag 설정 → 현재 스텝 완료 후 다음 iteration 시작 시 탈출
- 2nd Ctrl+C: `KeyboardInterrupt` 즉시 발생 (기본 핸들러 복원 후)
- 인터럽트 시 ctx와 scratchpad에 기록되어 다음 사용자 입력에서 LLM이 맥락을 이어감

**run 모드 Ctrl+C:** signal handler 미설치, `KeyboardInterrupt` 즉시 발생 → `try/except`로 세션 저장 후 종료

#### 플래그 분리: `headless` vs `suppress_output`

| 플래그 | 위치 | 역할 |
|--------|------|------|
| `--headless` | main.py (CLI) | 세션 미생성 + tmpdir ctx + stdout 출력 (서브에이전트 전용) |
| `suppress_output` | AgentLoop/run_loop | Rich 렌더링 억제 + ask 도구 제거 |

- `run` 일반: `suppress_output=headless` (headless면 억제)
- `chat` 일반: `suppress_output=False` (항상 렌더링)
- 스킬 실행: `suppress_output=True` (항상 억제, 부모가 progress 표시)

### 5.2 프로바이더별 도구 호출 방식

```
                    ┌─ supports_tool_calling=True ─┐
                    │                               │
              ┌─────┴──────┐                ┌──────┴──────┐
              │ Anthropic  │                │ OpenAI      │
              │ tool_use   │                │ tool_calls  │
              │ 블록       │                │ function    │
              └────────────┘                └─────────────┘
                    파싱 불필요                  파싱 불필요
                    (구조화된 블록)              (구조화된 응답)

              ┌─ supports_structured_output=True ─┐
              │                                    │
        ┌─────┴──────┐                             │
        │ Ollama     │                             │
        │ format:    │                             │
        │ JSON Schema│                             │
        └────────────┘                             │
              파싱 필요                              │
              (구조화된 JSON)                        │

              ┌─ 둘 다 False ──────────────────────┘
              │
        ┌─────┴──────┐
        │ 텍스트     │
        │ 3단계 폴백  │
        │ 파서       │
        └────────────┘
              파싱 필요
              (비구조화 텍스트)
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

### 5.4 컨텍스트 압축 (`context/manager.py`)

```
메시지 추가 (add)
    │
    ▼
total_chars > max_context_chars?
    │
    ├─ No → 그대로 유지
    │
    ▼ Yes
_compress() 호출
    │
    ├─ _summary 없음 (첫 압축)
    │   └─ SUMMARIZATION_PROMPT로 전체 요약 생성
    │      (Goal, Progress, Key Decisions, Current State, Files Touched)
    │
    └─ _summary 있음 (후속 압축)
        └─ INCREMENTAL_UPDATE_PROMPT로 기존 요약에 새 정보만 추가

도구 결과는 2,000자로 절단 후 요약에 포함
압축 후 "[artifact 경로를 read_artifact로 복구 가능]" 힌트 자동 추가
```

### 5.6 Scratchpad & Artifact 시스템

#### 설계 배경

On-premise LLM(128K context)에서 긴 태스크 수행 시 세 가지 문제가 발생:
1. 초반 맥락 망각 — 턴이 많아지면 목표/결정을 잊고 반복
2. 도구 결과 손실 — compaction 시 이전에 읽은 파일 디테일 소실
3. 컨텍스트 오염 — 과거 도구 결과 자동 주입 시 LLM 혼란

해결 전략: **Scratchpad(인덱스) + Artifact(데이터) + Lazy Loading**

#### Scratchpad — 항상 보이는 나침반

```
[Scratchpad — persistent task context]
---
status: in_progress
---
## Progress
- [턴0] User: hooks.py 분석해줘
- [턴1] read_file: hooks.py (215줄) → artifacts/turn_0001.md
- [턴2] User: 리팩토링해줘
- [턴3] edit_file: hooks.py → artifacts/turn_0003.md
- [턴4] User: /optimize ./
- [턴4] Used skill: optimize(./)
## Decisions
- [턴5] TX aggregation 별도 모듈 분리
```

- 매 LLM 호출 시 `get_messages()` 맨 앞에 주입 (compaction에서 살아남음)
- **스킬 내부 루프에서는 주입하지 않음** — 외부 태스크 정보가 스킬 LLM을 혼란시키는 것 방지
- `run`과 `chat` 모두 동일하게 동작 — `run`도 세션 기반 scratchpad 사용
- `--headless`(서브에이전트)는 `tempfile.mkdtemp()` 기반 tmpdir에 저장 — 프로세스 종료 시 자동 정리

#### Artifact — 디스크에 보존, 필요할 때만 로드

```
.agent-cli/sessions/{session_id}/artifacts/
  turn_0003.md                     # 일반 도구 결과
  turn_0005_optimize/              # 스킬 내부 결과 (서브디렉토리)
    turn_0006.md
    turn_0007.md
```

- 매 이터레이션 도구 결과를 YAML frontmatter + 원본으로 저장
- 컨텍스트에 자동 주입하지 않음 (Lazy Loading)
- LLM이 `read_artifact` 도구로 선택적 로드
- 시스템 프롬프트에 사용법 안내, compaction 후 복구 힌트 제공

#### ContextBudget — 모델 크기별 토큰 배분

```python
ContextBudget.for_model(context_window)
# 8K:  scratchpad 10%, artifact 15%, conversation 52%
# 32K: scratchpad 6%,  artifact 25%, conversation 51%
# 128K+: scratchpad 3%, artifact 35%, conversation 50%
```

#### 스킬 내부 격리

```
외부 루프: get_messages() → [scratchpad + 대화]    ← scratchpad 주입
  └─ run_skill(optimize)
       └─ 내부 루프: get_messages() → [대화만]      ← scratchpad 스킵
                     set_skill_context() → 서브디렉토리 라우팅
                     end_turn() → artifacts/turn_N_optimize/ 에 저장
```

---

## 6. 도구 시스템

### 6.1 등록된 도구

**실제 도구** — 파일/셸/네트워크 작업 수행:

| 도구 | 설명 | 필수 입력 | 출력 |
|------|------|----------|------|
| `read_file` | 파일 읽기 (hashline 포맷) | `path` | `LINE#HASH:content` 형식 |
| `write_file` | 파일 생성/덮어쓰기 | `path`, `content` | 저장 확인 메시지 |
| `edit_file` | hashline 기반 파일 편집 | `path`, `edits[]` | 편집 확인 메시지 |
| `shell` | 셸 명령 실행 | `command` | stdout + stderr + exit code |
| `delegate` | 서브에이전트 위임 (`--headless`) | `task` | 서브에이전트 실행 결과 |
| `read_context` | 이전 세션 이력 조회 | `mode` | 세션 목록 또는 상세 이력 |

**가상 도구** (`VIRTUAL_TOOLS`) — loop.py에서 인터셉트, 도구 설명에서 제외:

| 도구 | 설명 | 필수 입력 | 비고 |
|------|------|----------|------|
| `complete` | 작업 완료 신호 | `result` | 루프 종료 |
| `ask` | 사용자에게 질문 | `questions` | 대화형 전용 (suppress_output 시 제거) |
| `run_skill` | 스킬 실행 | `name` | loop 레벨 인터셉트, ctx 전달 |
| `read_artifact` | artifact 읽기/목록/검색 | `path` 또는 `mode` | hashline 없이 반환 |

### 6.2 run_skill 결과 포맷

`run_skill` 실행 결과에는 스킬 식별 헤더와 내부 스킬 호출 이력이 포함:

```
STATUS: success
RESULT:
SKILL: summarize(./)
The agent-cli directory contains a ReAct pattern-based agent CLI...

[Internal skill calls during this execution:]
- run_skill(optimize): Task completed: Analysis complete. OptimizationToDo.md updated.
```

- `SKILL: name(arguments)` — 실행된 스킬과 인자
- `[Internal skill calls]` — A→B 체이닝 시 내부에서 호출된 스킬 이력
- 외부 LLM이 이를 보고 중복 실행 방지

### 6.2 Hashline 시스템 (`tools/read_file.py`)

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

### 6.3 Tool Output 전달 방식

Tool output은 **잘림(truncation) 없이 전체를 그대로** LLM에 전달합니다.
context가 넘치면 `context/manager.py`의 대화 압축이 오래된 메시지를 요약하여 처리합니다.

이전에는 tool output을 context window의 3% 비율로 잘랐으나 (`tools/truncation.py`),
이로 인해 LLM이 불완전한 정보로 판단하는 성능 열화가 확인되어 제거되었습니다.

### 6.3.1 Fulfillment Review (`ready_for_review`)

LLM이 작업 완료 전 자기 검증을 수행하는 가상 도구입니다.

1. LLM이 `ready_for_review(summary="...")` 호출
2. Loop이 intercept → **원본 query + scratchpad progress**를 observation으로 반환
3. LLM이 요청 vs 실행 내역을 대조 → 빠뜨린 게 있으면 계속, 다 했으면 `complete` 호출

`_ALWAYS_INCLUDE`에 등록되어 skill의 `allowed_tools`와 무관하게 항상 API tool 목록에 포함됩니다.

### 6.4 스키마 검증 (`tools/registry.py`)

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

| 프로바이더 | 엔드포인트 | 인증 | 구조화 출력 | 네이티브 Tool Calling | Thinking |
|-----------|-----------|------|-----------|---------------------|---------|
| **Anthropic** | `/messages` | x-api-key | - | tool_use 블록 | budget_tokens |
| **OpenAI Compat** | `/chat/completions` | Bearer token | response_format | function calling | reasoning_effort |
| **Ollama** | `/api/chat` | 없음 | format (JSON Schema) | - | num_predict |

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

### 8.1 models.json 구조

```json
{
  "models": {
    "<model_id>": {
      "provider": "anthropic | openai | ollama",
      "context_window": 32768,
      "max_output_tokens": 4096,
      "supports_structured_output": true,
      "supports_tool_calling": false,
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
   - Ollama: `/api/show` (메타데이터) + `/api/chat` (thinking 프로브)
   - OpenAI 호환: `/chat/completions` (thinking 프로브)
3. `DEFAULT_CAPABILITIES` (context_window=4096, 모든 기능 비활성)

### 8.6 Thinking 감지 방식

하드코딩 패턴 매칭이 아닌 **프로브 기반 감지**:
1. 모델에 "What is 2+2?" 프롬프트 전송
2. 두 가지 위치에서 thinking 확인:
   - `message.thinking` 필드 (Ollama API — Qwen3, Qwen3.5, GLM 등)
   - `<think>`, `<thinking>`, `<reasoning>`, `<reflection>` 태그 in content (DeepSeek-R1 등)
3. 감지되면 → `supports_thinking=True`, `thinking_format=감지방식`
4. 결과를 `~/.agent-cli/models.json`에 저장 → 다음 실행 시 프로브 불필요

새 모델이 추가되어도 코드 수정 없이 자동 감지됩니다.

OpenAI 호환 서버(vLLM 등)에서는 `/v1/models` API로 context window도 감지합니다 (`max_model_len` 필드).

### 8.5 모델 정보 출력

| 상황 | 출력 |
|------|------|
| 새 모델 감지 + 저장 | Rich Panel (상세 — context, thinking, tool calling 등) |
| 기존 모델 로딩 | 한 줄 요약 (`● Model: name (ctx=N, thinking=✓)`) |
| `--headless` 모드 (suppress_output) | 미출력 |

---

## 9. 시스템 프롬프트 아키텍처 (`prompts/system_prompt.py`)

조건부 조립 방식 — 활성 도구, 모델 능력치, 플래그에 따라 섹션을 선택적으로 포함:

```
build_system_prompt(capabilities, active_tools, include_delegate, skill_stack, session_id)
    │
    ├─ BASE_ROLE_PROMPT (항상 포함)
    │   └─ JSON ReAct 응답 포맷 정의
    │   └─ ready_for_review → complete 워크플로 안내
    │
    ├─ Session (session_id가 있을 때만 — 현재 세션 ID 노출)
    │
    ├─ Available Tools (active_tools + _ALWAYS_INCLUDE)
    │   └─ 도구별 이름 + 설명 + Input JSON
    │   └─ complete, ready_for_review는 allowed_tools와 무관하게 항상 포함
    │
    ├─ HASHLINE_GUIDE (edit_file in active_tools일 때만)
    │
    ├─ DELEGATE_GUIDE (include_delegate=True일 때만)
    │
    ├─ ARTIFACT_GUIDE (항상 포함 — read_artifact 사용 안내)
    │
    ├─ RULES (항상 포함 — Rule 7: 재귀 금지, Rule 8: complete 전 ready_for_review 필수)
    │
    ├─ SMALL_MODEL_HINTS (context_window ≤ 8192)
    │
    ├─ THINKING_MODEL_HINTS (thinking + small context일 때만)
    │
    └─ Available Skills (skill_stack에 없는 스킬만 표시, run_skill 사용 안내)
```

---

## 10. 테스트 아키텍처

### 10.1 테스트 분류

| 분류 | 파일 수 | 테스트 수 | 실행 방법 |
|------|---------|----------|----------|
| 유닛 테스트 | 23 | 548 | `pytest tests/ -m "not ollama_integration"` |
| 통합 테스트 | 1 | 56 | `pytest tests/test_integration.py` |
| **전체** | **23** | **604** | `pytest tests/` |

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
  -n, --max-iter    최대 이터레이션 (0=무제한)
  --max-depth       서브에이전트 중첩 깊이 (기본: 2)
  --delegate-timeout 서브에이전트 타임아웃 초 (기본: 300)
  -v, --verbose     원시 LLM 응답 표시

  /sh <cmd>         LLM 없이 셸 명령 직접 실행

  # 내부 플래그 (서브에이전트용)
  --headless        세션 없음 + 출력 억제 + tmpdir 컨텍스트 (휘발)
  --depth N         현재 중첩 깊이
```

`run`도 `chat`과 동일하게 세션/컨텍스트/scratchpad를 관리합니다. 완료 후 세션 ID가 출력되며 `chat --resume <id>`로 이어서 작업할 수 있습니다. `--headless`는 서브에이전트(delegate) 전용으로, tmpdir 기반 휘발성 컨텍스트를 사용하고 세션을 저장하지 않습니다.

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
  "supports_tool_calling": false,
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
max-iter: 5
argument-hint: "<file_path>"
---

You are a code reviewer. Read $ARGUMENTS and analyze for bugs.
```

| Frontmatter 필드 | 타입 | 설명 |
|-----------------|------|------|
| `name` | string | 슬래시 명령어 이름 |
| `description` | string | 스킬 설명 |
| `allowed-tools` | list[str] | 허용 도구 (미지정 시 전체) |
| `max-iter` | int | 최대 이터레이션 (미지정 시 기본값) |
| `argument-hint` | string | 인자 힌트 |

### 13.3 인자 치환

| 패턴 | 설명 |
|------|------|
| `$ARGUMENTS` | 전체 인자 문자열 |
| `$0`, `$1`, ... | N번째 인자 (0-indexed) |

### 13.4 스킬 검색 경로

1. `.agent-cli/skills/*.md` (프로젝트 로컬, 우선)
2. `~/.agent-cli/skills/*.md` (사용자 전역)

동일 name의 스킬이 양쪽에 있으면 프로젝트 로컬이 우선합니다.

### 13.5 실행 플로우

```
사용자 입력: /review-code src/auth.py
    │
    ▼
load_skills() — 디스크에서 스킬 파일 검색/파싱
    │
    ▼
스킬 매칭: "review-code" → Skill 객체
    │
    ▼
substitute_arguments() — $ARGUMENTS → "src/auth.py" 치환
    │
    ▼
run_loop(query=치환된_프롬프트, allowed_tools=["read_file"], max_iter=5)
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

## 14. 설계 원칙

1. **모델은 commodity, harness가 성패를 결정한다** — 파싱 폴백, 도구 출력 압축, 퍼지 편집 등 harness 레벨 최적화가 핵심
2. **프로바이더별 최선의 방식 자동 선택** — 네이티브 tool calling > constrained decoding > 텍스트 파싱
3. **소형 모델 우선 설계** — 보수적 기본값, 적응형 출력 압축, 스키마 자동 변환
4. **비용 제로 보정 우선** — LLM 재호출 없이 harness에서 보정 (퍼지 매칭, 타입 변환)
5. **점진적 기능 저하** — 기능 미지원 시 에러 대신 다음 폴백으로 graceful degradation
6. **순환 의존 없는 단방향 모듈 구조** — config → compat → base → adapters → loop → main
