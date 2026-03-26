# Agent-CLI

![License: MIT](https://img.shields.io/badge/License-MIT-brightgreen.svg)
![Python](https://img.shields.io/badge/python-3.10+-blue.svg)

ReAct 패턴 기반 에이전트 CLI. 멀티 프로바이더(Anthropic, OpenAI, Ollama) 지원, on-premise LLM 최적화.

## 설치

```bash
# 개발 모드
git clone https://github.com/your-repo/agent-cli.git
cd agent-cli
pip install -e ".[dev]"

# 또는 직접 실행
pip install typer rich requests
python agent-cli.py --help
```

설치 후 `agent-cli` 명령어가 PATH에 등록됩니다.

```bash
# pip install 없이 실행하는 방법
python3 agent-cli.py run "task"
python3 -m agent_cli run "task"
```

## 빠른 시작

```bash
# Ollama (기본 프로바이더, 로컬)
agent-cli run "List files in the current directory"

# 특정 모델 지정
agent-cli run "Read README.md" -m qwen3-coder:30b

# OpenAI
agent-cli run "Analyze this project" -p openai

# Anthropic
agent-cli run "Read README.md and summarize" -p anthropic

# 직접 셸 명령 (LLM 없이)
agent-cli run "/sh ls -la"

# 스킬 실행
agent-cli run "/review-code src/auth.py"
agent-cli run "/summarize README.md"

# 도움말
agent-cli --help
agent-cli run --help
agent-cli chat --help
agent-cli plan --help
agent-cli sessions --help
```

## 명령어

### `run` — 단발 실행

```bash
agent-cli run "task description" [options]
```

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `-p, --provider` | `ollama` / `openai` / `anthropic` | `ollama` |
| `-m, --model` | 모델 ID | 프로바이더 기본값 |
| `--base-url` | API 엔드포인트 | 프로바이더 기본값 |
| `--api-key` | API 키 (환경 변수 자동 감지) | |
| `-n, --max-iter` | 최대 이터레이션 (0=무제한) | `0` |
| `--max-depth` | 서브에이전트 중첩 깊이 | `2` |
| `--delegate-timeout` | 서브에이전트 타임아웃 (초) | `300` |
| `-v, --verbose` | 원시 LLM 응답 + 컨텍스트 덤프 표시 | |
| `-q, --quiet` | 최소 출력 (결과만) | |

### `plan` — 계획 기반 실행

복잡한 작업을 단계별 계획으로 분해하여 실행합니다.

```bash
# 계획 생성 → 검토 → 실행
agent-cli plan "Refactor auth module to use JWT"

# 계획만 보기 (실행 안 함)
agent-cli plan "Migrate database schema" --plan-only

# 검토 없이 바로 실행
agent-cli plan "Add unit tests for utils.py" --auto-approve

# 계획 저장 + 나중에 재개
agent-cli plan "Big refactoring task" --save-plan my-plan.json
agent-cli plan "Big refactoring task" --resume my-plan.json

# 계획 생성은 작은 모델, 실행은 큰 모델
agent-cli plan "Complex task" --plan-model qwen3:8b --model qwen3:32b
```

| 추가 옵션 | 설명 | 기본값 |
|----------|------|--------|
| `--max-steps` | 최대 계획 step 수 | `20` |
| `--step-max-iter` | step당 최대 이터레이션 | `10` |
| `--auto-approve` | 검토 건너뛰기 | |
| `--plan-only` | 계획 생성만 | |
| `--plan-model` | 계획 생성용 별도 모델 | `--model`과 동일 |
| `--save-plan` | 계획 파일 저장 | |
| `--resume` | 저장된 계획에서 재개 | |

### `chat` — 대화형 모드

```bash
agent-cli chat -p ollama -m qwen3:32b
```

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `-p, --provider` | `ollama` / `openai` / `anthropic` | `ollama` |
| `-m, --model` | 모델 ID | 프로바이더 기본값 |
| `--base-url` | API 엔드포인트 | 프로바이더 기본값 |
| `--api-key` | API 키 (환경 변수 자동 감지) | |
| `-n, --max-iter` | 턴당 최대 이터레이션 (0=무제한) | `0` |
| `--max-depth` | 서브에이전트 중첩 깊이 | `2` |
| `--delegate-timeout` | 서브에이전트 타임아웃 (초) | `300` |
| `-v, --verbose` | 원시 LLM 응답 + 컨텍스트 덤프 표시 | |
| `--resume <id>` | 이전 세션 이어서 작업 | |

대화 중 명령어:

| 명령어 | 설명 |
|--------|------|
| `/quit`, `/exit` | 세션 종료 (요약 생성 후 저장) |
| `/clear` | 컨텍스트 초기화 |
| `/sh <cmd>` | 셸 명령 실행 |
| `/plan <goal>` | 대화 중 계획 모드 진입 |
| `/skills` | 사용 가능한 스킬 목록 |
| `/ctx_window` | 현재 컨텍스트 윈도우 내용 덤프 (디버깅용) |
| `/<skill> <args>` | 스킬 실행 |

입력 히스토리: `~/.agent-cli/chat_history`에 자동 저장됩니다. 화살표 키(위/아래)로 이전 입력을 탐색하고, 좌/우 화살표와 readline 단축키(Ctrl+A/E/W/K)로 줄 편집이 가능합니다.

### `sessions` — 세션 관리

대화 이력은 `~/.agent-cli/context/`에 세션별로 자동 저장됩니다. 세션 종료 시 LLM이 요약을 생성하고, 다음 세션에 자동 주입됩니다.

```bash
# 현재 워크스페이스의 세션 목록
agent-cli sessions

# 특정 워크스페이스의 세션 목록
agent-cli sessions --workspace /path/to/project

# 이전 세션 이어서 작업
agent-cli chat -p ollama -m qwen3:32b --resume <session_id>
```

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `-w, --workspace` | 워크스페이스 경로 필터 | 현재 디렉토리 |

LLM은 `read_context` 도구로 이전 세션의 세부 이력을 조회할 수 있습니다.

## 스킬 (Prompt Skills)

특정 작업에 최적화된 재사용 가능한 프롬프트 템플릿. Claude Code 스킬 포맷과 호환.

### 기본 내장 스킬

```bash
# 코드 리뷰
agent-cli run "/review-code src/auth.py"

# 파일 요약
agent-cli run "/summarize README.md"

# 유닛 테스트 생성
agent-cli run "/test src/utils.py"

# 코드 최적화 분석
agent-cli run "/optimize ./agent_cli"

# chat 모드에서도 사용 가능
/review-code src/auth.py
/skills   # 사용 가능한 스킬 목록
```

### 커스텀 스킬 작성

`.agent-cli/skills/my-skill.md` 파일을 만들면 `/my-skill` 명령어가 자동 등록됩니다:

```markdown
---
name: my-skill
description: What this skill does
allowed-tools: [read_file, shell]
max-iter: 5
argument-hint: "<file_path>"
---

Your custom prompt template here. Use $ARGUMENTS for user input.
$0, $1 or $ARGUMENTS[0], $ARGUMENTS[1] for individual arguments (0-based).
${CLAUDE_SKILL_DIR} for the skill's directory path.
${SESSION_ID} for the current session ID.
```

| Frontmatter 필드 | 설명 | 필수 |
|-----------------|------|------|
| `name` | 슬래시 명령어 이름 (미지정 시 파일명) | |
| `description` | 스킬 설명 | ✓ |
| `allowed-tools` | 허용 도구 리스트 (미지정 시 전체) | |
| `max-iter` | 최대 이터레이션 (미지정 시 글로벌 설정 사용) | |
| `model` | 스킬 실행 시 모델 오버라이드 (미지정 시 현재 모델 사용) | |
| `context` | `fork`이면 독립 컨텍스트에서 실행 (부모 대화 히스토리 없음) | |
| `argument-hint` | `/skills` 표시 시 인자 힌트 | |

스킬 검색 경로:
1. `.agent-cli/skills/*.md` (프로젝트 로컬 플랫, 우선)
2. `.agent-cli/skills/<name>/SKILL.md` (프로젝트 로컬 디렉토리)
3. `~/.agent-cli/skills/*.md` (사용자 전역 플랫)
4. `~/.agent-cli/skills/<name>/SKILL.md` (사용자 전역 디렉토리)

같은 검색 경로 내에서 동일 이름의 플랫 파일과 디렉토리 스킬이 모두 존재하면 에러가 발생합니다.

## 프로바이더 설정

### Ollama (기본, 로컬)

```bash
# 기본: http://localhost:11434, 모델: qwen3:32b
agent-cli run "task"

# 다른 모델
agent-cli run "task" -m llama3.1:8b
```

### OpenAI

```bash
export OPENAI_API_KEY="sk-..."
agent-cli run "task" -p openai
```

### Anthropic

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
agent-cli run "task" -p anthropic
```

### vLLM / LM Studio / mlx-lm (OpenAI 호환)

```bash
# OpenAI 호환 API → --provider openai + --base-url 변경
agent-cli run "task" -p openai --base-url http://localhost:8000/v1 -m my-model
```

## 모델 레지스트리

`models.json`에 모델별 능력치를 정의합니다:

```json
{
  "models": {
    "qwen3:32b": {
      "provider": "ollama",
      "context_window": 32768,
      "max_output_tokens": 4096,
      "supports_structured_output": true,
      "supports_tool_calling": false,
      "supports_thinking": true,
      "thinking_budget": 4096,
      "supports_strict_schema": false,
      "thinking_format": "think"
    }
  }
}
```

### 파일 위치 및 정책

| 우선순위 | 위치 | 역할 | 자동 저장 |
|---------|------|------|----------|
| 1 | `.agent-cli/models.json` | 프로젝트 로컬 오버라이드 | 안 함 (읽기만) |
| 2 | `~/.agent-cli/models.json` | 사용자 전역 설정 | 새 모델 자동 저장 |
| 3 | `agent_cli/default_models.json` | 패키지 기본값 | 안 함 (읽기만) |

- 미등록 모델은 런타임 자동 감지 → `~/.agent-cli/models.json`에 저장
  - Ollama: `/api/show` (메타데이터) + 프로브 (thinking 감지)
  - OpenAI 호환: 프로브 (thinking 감지)
  - Thinking 감지: 프로브 프롬프트 → `message.thinking` 필드 또는 `<think>` 태그 확인 (하드코딩 없이 자동)
- 런타임 감지도 실패하면 사용자에게 대화형으로 context window, thinking 지원 여부를 질문 → `~/.agent-cli/models.json`에 저장
- 이미 등록된 모델은 덮어쓰지 않음 (사용자 설정 보호)
- 다음 실행 시 저장된 설정에서 로딩 (프로브/질문 재실행 없음)

| 필드 | 설명 |
|------|------|
| `context_window` | 컨텍스트 윈도우 크기 (토큰) |
| `max_output_tokens` | 최대 출력 토큰 |
| `supports_structured_output` | Constrained decoding 지원 |
| `supports_tool_calling` | 네이티브 tool calling API |
| `supports_thinking` | Thinking/reasoning 지원 |
| `thinking_budget` | Thinking 토큰 예산 |
| `thinking_format` | Thinking 블록 태그 (`"think"`, `""`) |
| `supports_strict_schema` | Strict JSON Schema 모드 |

**설정 우선순위**: `.agent-cli/models.json` (프로젝트) > `~/.agent-cli/models.json` (전역) > `default_models.json` (패키지) > 런타임 감지 > 보수적 기본값

## 도구

LLM이 사용할 수 있는 도구 목록:

| 도구 | 설명 |
|------|------|
| `read_file` | 파일 읽기 (hashline 태그 포함, 부분 읽기 지원) |
| `write_file` | 파일 생성/덮어쓰기 |
| `edit_file` | hashline 기반 정밀 편집 (퍼지 매칭 지원) |
| `shell` | 셸 명령 실행 |
| `delegate` | 서브에이전트에 독립 작업 위임 |
| `read_context` | 이전 세션 이력 조회 (목록/세부) |
| `complete` | 작업 완료 신호 (최종 결과 반환) |
| `ask` | 사용자에게 질문 (chat 모드 전용, 배열 지원) |

### read_file — 파일 읽기

파일을 읽고 각 줄에 `LINE#HASH:content` hashline 태그를 부여합니다.

```json
{"action": "read_file", "action_input": {"path": "src/main.py"}}
```

부분 읽기 (큰 파일에서 특정 범위만):

```json
{"action": "read_file", "action_input": {"path": "src/main.py", "line_start": 100, "line_end": 200}}
```

- `line_start`: 시작 줄 번호 (1-based, 생략 시 처음부터)
- `line_end`: 끝 줄 번호 (1-based, inclusive, 생략 시 끝까지)
- 큰 파일이 truncation되면 `[To read the rest, use: read_file with line_start=N]` 가이드가 표시됩니다.

### edit_file — Hashline 편집

`read_file`에서 받은 hashline 태그를 사용하여 정밀 편집합니다:

```
1#VR:def hello():
2#KT:    return "world"
3#ZZ:
```

```json
{"op": "replace", "pos": "2#KT", "lines": ["    return \"hello\""]}
{"op": "replace", "pos": "1#VR", "end": "3#ZZ", "lines": ["def greet():", "    pass"]}
{"op": "append", "pos": "1#VR", "lines": ["    # comment"]}
```

해시 불일치 시 퍼지 매칭으로 자동 보정합니다 (공백/따옴표/대시 정규화).

### complete — 작업 완료

LLM이 작업을 완료했을 때 호출하는 가상 도구입니다. `result` 필드에 최종 답변을 담습니다.

```json
{"action": "complete", "action_input": {"result": "작업이 완료되었습니다. 파일을 생성했습니다."}}
```

### read_context — 세션 이력 조회

이전 세션의 이력을 조회합니다. LLM이 과거 작업 맥락이 필요할 때 자발적으로 사용합니다.

```json
{"action": "read_context", "action_input": {"mode": "list"}}
{"action": "read_context", "action_input": {"mode": "detail", "session_id": "1774272070"}}
```

### ask — 사용자에게 질문 (chat 모드 전용)

LLM이 추가 정보가 필요할 때 사용자에게 질문합니다. 배열로 여러 질문을 한 번에 할 수 있습니다.

```json
{"action": "ask", "action_input": {"questions": ["어떤 파일을 수정할까요?"]}}
{"action": "ask", "action_input": {"questions": ["파일 경로는?", "사용할 언어는?"]}}
```

## 핵심 기능

### 프로바이더별 최적 도구 호출

| 프로바이더 | 방식 | 파싱 필요? |
|-----------|------|-----------|
| Anthropic | 네이티브 `tool_use` 블록 | 없음 |
| OpenAI | 네이티브 `function calling` | 없음 |
| Ollama | Constrained decoding (JSON Schema) | JSON만 |
| mlx-lm 등 | 텍스트 파싱 3단계 폴백 | 있음 |

### 3단계 파싱 폴백

1. **Stage 1**: `json.loads()` (직접 파싱)
2. **Stage 2**: JSON 복구 (깨진 JSON 자동 수정)
3. **Stage 3**: Regex 추출 (최후 수단)

Thinking 모델(`<think>...</think>`)은 파싱 전 자동 분리됩니다.

### 컨텍스트 압축

Chat 모드에서 컨텍스트 윈도우의 95%에 도달하면 LLM 기반 구조화 요약으로 자동 압축합니다. 첫 압축은 전체 요약, 이후는 증분 업데이트.

### 체크포인트 시스템

LLM이 도구를 반복 호출하며 `complete` 도구를 사용하지 않는 stuck 상태를 방지합니다:

- **50 iteration** 도달 시 첫 체크포인트 — 최근 20회 도구 호출 이력을 LLM에게 보여주고 자기 판단 요청
- 이후 **매 20 iteration**마다 반복 체크포인트
- 동일 도구를 같은 파라미터로 **3회 연속** 호출 시 자동 중단
- `echo`로 답하는 패턴 자동 감지 → `complete` 도구 호출로 변환

### 모델 적응형 출력 압축

도구 출력을 모델 context window의 **3%**를 기준으로 자동 절단합니다:

| Context Window | 최대 바이트 | 최대 줄 수 |
|---------------|-----------|-----------|
| 8K | 2,000 (최소) | 50 (최소) |
| 32K | 3,932 | 98 |
| 128K | 15,360 | 384 |
| 262K | 31,457 | 786 |
| 1M+ | 40,000 (최대) | 1,000 (최대) |

`read_file`이 truncation되면 나머지를 읽을 수 있는 `line_start` 가이드가 자동 표시됩니다.

## 프로젝트 구조

```
agent_cli/
├── main.py              CLI 명령어 (run, plan, chat, sessions)
├── loop.py              ReAct 에이전트 루프
├── config.py            models.json 로딩
├── render.py            터미널 렌더링
├── input_history.py     readline 히스토리 영속화
├── providers/           LLM 프로바이더 (Anthropic, OpenAI, Ollama)
├── parsing/             3단계 JSON 파서 + Thinking 블록 분리
├── tools/               도구 (read/write/edit/shell/delegate/context) + 출력 압축
├── context/             컨텍스트 관리 (오버플로 감지, 압축, 세션 영속화)
├── prompts/             조건부 시스템 프롬프트
└── planning/            Planning Mode (생성→검토→실행)
```

상세 아키텍처: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## 테스트

```bash
# 유닛 테스트만 (빠름)
pytest tests/ -m "not ollama_integration" -v

# 통합 테스트 (Ollama 필요)
pytest tests/test_integration.py -v

# 특정 모델로 통합 테스트
INTEGRATION_MODELS="qwen3-coder:30b" pytest tests/test_integration.py -v

# 전체
pytest tests/ -v
```

## 환경 변수

| 변수 | 설명 |
|------|------|
| `ANTHROPIC_API_KEY` | Anthropic API 키 |
| `OPENAI_API_KEY` | OpenAI API 키 |
| `OLLAMA_BASE_URL` | Ollama 엔드포인트 (기본: `http://localhost:11434`) |
| `AGENT_CLI_NO_READLINE` | `1`로 설정 시 readline 비활성화 (깨진 빌드 우회) |
| `INTEGRATION_MODELS` | 통합 테스트 모델 (쉼표 구분) |

## 라이선스

MIT License
