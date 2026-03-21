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
agent-cli plan --help
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
| `-v, --verbose` | 원시 LLM 응답 표시 | |

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

대화 중 명령어:

| 명령어 | 설명 |
|--------|------|
| `/quit`, `/exit` | 세션 종료 |
| `/clear` | 컨텍스트 초기화 |
| `/sh <cmd>` | 셸 명령 실행 |
| `/plan <goal>` | 대화 중 계획 모드 진입 |
| `/skills` | 사용 가능한 스킬 목록 |
| `/<skill> <args>` | 스킬 실행 |

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
active-tools: [read_file, shell]
max-iter: 5
argument-hint: "<file_path>"
---

Your custom prompt template here. Use $ARGUMENTS for user input.
$1, $2 for individual arguments.
```

스킬 검색 경로:
1. `.agent-cli/skills/*.md` (프로젝트 로컬, 우선)
2. `~/.agent-cli/skills/*.md` (사용자 전역)

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

| 위치 | 역할 | 자동 저장 |
|------|------|----------|
| `.agent-cli/models.json` | 프로젝트 로컬 오버라이드 (우선) | 안 함 (읽기만) |
| `~/.agent-cli/models.json` | 사용자 전역 설정 | 새 모델 자동 저장 |

- 미등록 모델은 런타임 자동 감지 → `~/.agent-cli/models.json`에 저장
  - Ollama: `/api/show` (메타데이터) + 프로브 (thinking 감지)
  - OpenAI 호환: 프로브 (thinking 감지)
  - Thinking 감지: "Say hello" 프롬프트 → 응답에 `<think>` 태그 확인 (하드코딩 없이 자동)
- 이미 등록된 모델은 덮어쓰지 않음 (사용자 설정 보호)
- 다음 실행 시 저장된 설정에서 로딩 (프로브 재실행 없음)

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

**설정 우선순위**: `.agent-cli/models.json` (프로젝트) > `~/.agent-cli/models.json` (전역) > 런타임 감지 > 보수적 기본값

## 도구

| 도구 | 설명 |
|------|------|
| `read_file` | 파일 읽기 (hashline 태그 포함) |
| `write_file` | 파일 생성/덮어쓰기 |
| `edit_file` | hashline 기반 정밀 편집 (퍼지 매칭 지원) |
| `shell` | 셸 명령 실행 |
| `delegate` | 서브에이전트에 독립 작업 위임 |

### Hashline 편집

`read_file`은 각 줄에 `LINE#HASH:content` 태그를 부여합니다:

```
1#VR:def hello():
2#KT:    return "world"
3#ZZ:
```

`edit_file`로 정밀 편집:

```json
{"op": "replace", "pos": "2#KT", "lines": ["    return \"hello\""]}
{"op": "replace", "pos": "1#VR", "end": "3#ZZ", "lines": ["def greet():", "    pass"]}
{"op": "append", "pos": "1#VR", "lines": ["    # comment"]}
```

해시 불일치 시 퍼지 매칭으로 자동 보정합니다 (공백/따옴표/대시 정규화).

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

Chat 모드에서 컨텍스트 윈도우 초과 시 LLM 기반 구조화 요약으로 자동 압축합니다. 첫 압축은 전체 요약, 이후는 증분 업데이트.

### 모델 적응형 출력 압축

도구 출력을 모델 context window에 맞춰 자동 절단합니다:

| Context Window | 최대 줄 수 | 최대 바이트 |
|---------------|-----------|-----------|
| ≤8K | 50 | 2,000 |
| ≤32K | 100 | 4,000 |
| >32K | 200 | 8,000 |

## 프로젝트 구조

```
agent_cli/
├── main.py              CLI 명령어 (run, plan, chat)
├── loop.py              ReAct 에이전트 루프
├── config.py            models.json 로딩
├── render.py            터미널 렌더링
├── providers/           LLM 프로바이더 (Anthropic, OpenAI, Ollama)
├── parsing/             3단계 JSON 파서 + Thinking 블록 분리
├── tools/               도구 (read/write/edit/shell/delegate) + 출력 압축
├── context/             컨텍스트 관리 (오버플로 감지, 압축)
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
| `INTEGRATION_MODELS` | 통합 테스트 모델 (쉼표 구분) |

## 라이선스

MIT License
