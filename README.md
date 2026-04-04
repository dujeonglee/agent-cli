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

설치 후 `agent-cli` 명령어가 PATH에 등록됩니다. 처음 실행하면 설정 마법사가 자동으로 시작됩니다.

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
agent-cli sessions --help
```

## 설정

### 첫 실행 (Setup Wizard)

설정 파일(`config.json`)이 없으면 자동으로 설정 마법사가 시작됩니다:

```
$ agent-cli run "hello"

No configuration found. Starting setup wizard...

╭─ Agent-CLI Setup ─────────────────────────────────────╮
│                                                        │
╰─ ReAct pattern agent CLI for on-premise LLMs ─────────╯

1. Select LLM Provider
   [1] Ollama (local, default)
   [2] OpenAI compatible (vLLM, LM Studio, mlx-lm)
   [3] Anthropic

2. Connection → base URL, API key, 연결 테스트
3. Model → 사용 가능한 모델 자동 탐색 및 선택
4. Review → 설정 확인 후 저장 위치 선택
```

수동으로 다시 실행:
```bash
agent-cli setup
```

### config.json

설정은 JSON 파일로 저장됩니다:

```json
{
  "provider": "ollama",
  "base_url": "http://localhost:11434",
  "api_key": "",
  "default_model": "qwen3:32b"
}
```

### 설정 우선순위

높은 게 낮은 걸 덮어씁니다 (필드 단위 병합):

| 우선순위 | 위치 | 용도 |
|---------|------|------|
| 1 (최고) | CLI 파라미터 (`-p`, `-m`, `--base-url`, `--api-key`) | 임시 오버라이드 |
| 2 | `.agent-cli/config.json` (프로젝트) | 워크스페이스별 설정 |
| 3 | `~/.agent-cli/config.json` (사용자) | 전역 기본 설정 |
| 4 (최저) | 환경변수 | 시스템 레벨 |

예: `~/.agent-cli/config.json`에 `qwen3:32b`가 기본이지만 `-m nemotron:120b`로 임시 실행:
```bash
agent-cli run "task" -m nemotron:120b
```

### Git 상태 스냅샷

Git 저장소 안에서 실행하면, 시스템 프롬프트에 현재 Git 상태가 자동 포함됩니다:

- `git status --short --branch` — 현재 브랜치와 변경 파일 목록
- `git diff HEAD` — 스테이징 전/후 diff (최대 4,000자, 초과 시 잘림)

Git이 설치되지 않았거나, Git 저장소가 아닌 경우에는 해당 섹션이 생략됩니다.

### DIRECTIVE.md — 프로젝트 지시사항

에이전트가 항상 따라야 하는 규칙을 `DIRECTIVE.md` 파일에 작성하면 매 세션의 시스템 프롬프트에 자동 주입됩니다.

| 경로 | 용도 |
|------|------|
| `.agent-cli/DIRECTIVE.md` | 프로젝트별 규칙 (코딩 컨벤션, 테스트 정책 등) |
| `~/.agent-cli/DIRECTIVE.md` | 사용자 전역 규칙 (응답 언어, 개인 선호 등) |

- 두 파일 모두 존재하면 **둘 다 로드** (프로젝트 먼저, 유저 전역 뒤에)
- 동일 내용이면 중복 제거
- 파일당 최대 4,000자, 전체 최대 8,000자 (초과 시 잘림)

예시 (`.agent-cli/DIRECTIVE.md`):
```markdown
# 코드 규칙
- 코드 수정 시 관련 유닛 테스트를 반드시 추가한다
- ruff check와 ruff format을 통과해야 한다
- Python 3.10+ 호환을 유지한다
```

### 환경변수

| 변수 | config.json 키 | 설명 |
|------|---------------|------|
| `AGENT_CLI_PROVIDER` | `provider` | LLM 프로바이더 |
| `AGENT_CLI_BASE_URL` | `base_url` | API 엔드포인트 |
| `AGENT_CLI_API_KEY` | `api_key` | API 키 |
| `AGENT_CLI_MODEL` | `default_model` | 기본 모델 |
| `ANTHROPIC_API_KEY` | — | Anthropic API 키 (기존 호환) |
| `OPENAI_API_KEY` | — | OpenAI API 키 (기존 호환) |
| `OLLAMA_BASE_URL` | — | Ollama 엔드포인트 (기존 호환) |
| `AGENT_CLI_NO_READLINE` | — | readline 비활성화 |
| `INTEGRATION_MODELS` | — | 통합 테스트 모델 |

## 모델 권장 사양

에이전트 루프는 JSON 포맷 준수 + 도구 선택 + 멀티스텝 reasoning을 동시에 요구합니다. 모델 크기에 따라 성능 차이가 큽니다:

| 모델 크기 | 멀티스텝 태스크 | 단순 질의 | 비고 |
|-----------|---------------|----------|------|
| **7B 이하** | ❌ | △ | 도구 혼동, JSON 포맷 불안정, 반복 실패 빈번 |
| **14-30B** | △ | ✅ | 간단한 도구 사용 가능, 복잡한 스킬은 불안정 |
| **32B+** | ✅ | ✅ | 안정적 — 권장 최소 사양 |
| **70B+** | ✅✅ | ✅ | delegate, 복잡한 스킬 등 고급 기능 안정 |

**최소: 30B, 권장: 32B+**

테스트 검증 모델: `qwen3-coder:30b`, `glm-4.7-flash:q8_0`, `qwen3.5:35b` (65개 통합 테스트 전체 통과)

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

`run` 실행 후 세션이 자동 저장됩니다. `chat --resume <id>`로 이어서 작업할 수 있습니다:

```bash
$ agent-cli run "Analyze the project structure"
# ... 실행 결과 ...
# Session 1774752167 saved. Resume with: agent-cli chat --resume 1774752167

$ agent-cli chat --resume 1774752167   # 이전 작업 이어서 대화
```

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
| `/help`, `/?` | 사용 가능한 명령어 목록 |
| `/quit`, `/exit` | 세션 종료 (요약 생성 후 저장) |
| `/clear` | 컨텍스트 초기화 |
| `/sh <cmd>` | 셸 명령 실행 |
| `/compact [prompt]` | 컨텍스트 압축 (선택적 포커스 프롬프트) |
| `/skills` | 사용 가능한 스킬 목록 |
| `/<skill> <args>` | 스킬 실행 (예: `/optimize ./`) |
| `/ctx_window` | 현재 컨텍스트 윈도우 내용 덤프 (디버깅용) |

입력 히스토리: `~/.agent-cli/chat_history`에 자동 저장됩니다. 화살표 키(위/아래)로 이전 입력을 탐색하고, 좌/우 화살표와 readline 단축키(Ctrl+A/E/W/K)로 줄 편집이 가능합니다.

**Graceful Interrupt (Ctrl+C):** 에이전트 실행 중 Ctrl+C를 누르면 현재 스텝(LLM 호출 또는 도구 실행)이 완료된 후 안전하게 멈춥니다. 컨텍스트와 scratchpad가 보존되어 다음 입력에서 바로 이어갈 수 있습니다. 즉시 종료하려면 Ctrl+C를 두 번 누르세요.

```bash
# 에이전트가 잘못된 방향으로 갈 때:
You: hooks.py 분석하고 리팩토링해줘
  [iteration 3... 파일 읽는 중]
  [Ctrl+C]
  ⚡ Finishing current step...
  ⚡ Interrupted after iteration 3.

You: 그 파일 말고 config.py를 먼저 봐   ← 방향 수정 후 이어서 작업
```

`run` 모드에서는 Ctrl+C로 즉시 종료됩니다 (돌아갈 input loop이 없으므로). 세션은 자동 저장되어 `chat --resume`으로 이어갈 수 있습니다.

### `setup` — 설정 마법사

```bash
agent-cli setup
```

프로바이더, 접속 정보, 기본 모델을 대화형으로 설정합니다. 설정이 없을 때 자동으로 실행되며, 언제든 수동으로 다시 실행할 수 있습니다.

### `sessions` — 세션 관리

`run`과 `chat` 모두 세션을 `.agent-cli/sessions/{session_id}/`에 자동 저장합니다. 세션 종료 시 컨텍스트 윈도우 내용이 요약으로 저장됩니다. `--resume`으로 이전 세션을 이어서 작업할 수 있습니다.

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

### 패키지 내장 스킬 (built-in)

패키지와 함께 배포되는 메타 스킬:

| 스킬 | 설명 |
|------|------|
| `/create-skill <name>` | 새 스킬 파일을 대화형으로 생성 (SKILL.md + scripts/) |
| `/create-agent <name>` | 새 에이전트 정의 파일을 대화형으로 생성 |
| `/plan <feature>` | 기능 요청을 작업 분해 + 의존성 + 범위 추정으로 구조화하여 `plan/` 에 저장 |

사용자가 같은 이름의 스킬을 `.agent-cli/skills/`에 만들면 built-in을 오버라이드합니다.

### 프로젝트 스킬 예시

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

`/create-skill my-skill` 명령으로 대화형 생성하거나, `.agent-cli/skills/my-skill.md` 파일을 직접 만들면 `/my-skill` 명령어가 자동 등록됩니다:

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
!`command` for dynamic context injection (shell command output).
```

| Frontmatter 필드 | 설명 | 필수 |
|-----------------|------|------|
| `name` | 슬래시 명령어 이름 (미지정 시 파일명) | |
| `description` | 스킬 설명 | ✓ |
| `allowed-tools` | 허용 도구 리스트 (미지정 시 전체) | |
| `max-iter` | 최대 이터레이션 (미지정 시 글로벌 설정 사용) | |
| `model` | 스킬 실행 시 모델 오버라이드 (미지정 시 현재 모델 사용) | |
| `context` | `fork`이면 독립 컨텍스트에서 실행 (부모 대화 히스토리 없음) | |
| `hooks` | 스킬 스코프 lifecycle hooks (PreToolUse, PostToolUse 등) | |
| `disable-model-invocation` | `true`이면 LLM 자동 호출 금지 (사용자만 `/명령`으로 호출 가능) | |
| `user-invocable` | `false`이면 `/skills` 메뉴에서 숨김 (LLM만 호출 가능) | |
| `argument-hint` | `/skills` 표시 시 인자 힌트 | |

스킬 검색 경로:
1. `.agent-cli/skills/*.md` (프로젝트 로컬 플랫, 우선)
2. `.agent-cli/skills/<name>/SKILL.md` (프로젝트 로컬 디렉토리)
3. `~/.agent-cli/skills/*.md` (사용자 전역 플랫)
4. `~/.agent-cli/skills/<name>/SKILL.md` (사용자 전역 디렉토리)

같은 검색 경로 내에서 동일 이름의 플랫 파일과 디렉토리 스킬이 모두 존재하면 에러가 발생합니다.

## Hooks

도구 실행 전후에 셸 명령을 자동 실행하는 lifecycle hook 시스템입니다.

### 설정

`.agent-cli/hooks.json`:

```json
{
  "PreToolUse": [
    {
      "matcher": "shell",
      "hooks": [
        {"command": "./block-dangerous.sh", "timeout": 30}
      ]
    }
  ],
  "PostToolUse": [
    {
      "matcher": "edit_file",
      "hooks": [
        {"command": "ruff format $(cat | jq -r '.tool_input.path')"}
      ]
    }
  ]
}
```

### Hook 이벤트

| 이벤트 | 시점 | 차단 가능 |
|--------|------|-----------|
| `PreToolUse` | 도구 실행 직전 | ✓ (exit 2) |
| `PostToolUse` | 도구 성공 직후 | |
| `PostToolUseFailure` | 도구 실패 직후 | |

### 동작 방식

- stdin으로 JSON 전달: `{"hook_event_name", "tool_name", "tool_input", "tool_result"}`
- `matcher`: 도구 이름 regex (빈 문자열 = 모든 도구)
- exit 0 = 통과, exit 2 = 차단 (PreToolUse만)
- stdout JSON의 `updatedInput`으로 도구 인자 수정 가능 (PreToolUse만)

### 스킬 내 hooks

스킬 frontmatter에서도 hooks를 정의할 수 있습니다 (해당 스킬 실행 중에만 활성):

```yaml
---
name: safe-deploy
description: Deploy safely
hooks:
  PreToolUse:
    - matcher: shell
      hooks:
        - command: "./validate-deploy.sh"
---
```

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
| `delegate` | 서브에이전트에 작업 위임 (에이전트 역할 지정 가능) |
| `read_context` | 이전 세션 이력 조회 (목록/세부) |
| `complete` | 작업 완료 신호 (최종 결과 반환) |
| `ask` | 사용자에게 질문 (chat 모드 전용, 배열 지원) |
| `run_skill` | 등록된 스킬 실행 (LLM이 자동으로 호출 가능) |
| `read_artifact` | 이전 도구 결과 읽기/목록/검색 (컨텍스트 복구용, hashline 없음) |

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

### shell — 셸 명령 실행

셸 명령을 실행하고 stdout/stderr를 반환합니다. 타임아웃 기본 30초.

```json
{"action": "shell", "action_input": {"command": "find agent_cli -name '*.py' | wc -l"}}
```

### write_file — 파일 생성

새 파일을 생성하거나 기존 파일을 덮어씁니다. 기존 파일 수정은 `edit_file` 권장.

```json
{"action": "write_file", "action_input": {"path": "output.txt", "content": "hello world"}}
```

### delegate — 서브에이전트 위임

작업을 in-process 서브에이전트에 위임합니다. 컨텍스트 모드로 서브에이전트가 부모 맥락을 얼마나 알지 제어합니다:

| 모드 | 동작 |
|------|------|
| `none` (기본) | 독립 실행. task에 모든 정보 포함 필요 |
| `fork` | 부모 컨텍스트를 복사하여 실행. 맥락 인지 + 독립 |
| `inherit` | 부모 컨텍스트를 공유. 메시지가 부모에 누적 |

```json
{"action": "delegate", "action_input": {"tasks": [{"task": "Read /tmp/data.csv and count rows"}]}}
{"action": "delegate", "action_input": {"tasks": [{"task": "Fix the bug we found", "context": "fork"}]}}
{"action": "delegate", "action_input": {"tasks": [{"task": "Review changes", "context": "fork", "tools": ["read_file", "shell"]}]}}
{"action": "delegate", "action_input": {"tasks": [{"task": "Review this code for vulnerabilities", "agent": "security-reviewer"}]}}
{"action": "delegate", "action_input": {"tasks": [{"task": "Fix the bug", "agent": "fixer", "context": "fork"}]}}
```

`tools` 파라미터로 서브에이전트가 사용할 수 있는 도구를 제한할 수 있습니다.

`agent` 파라미터로 `.agent-cli/agents/{name}.md` 파일에서 정의된 에이전트 역할을 로드할 수 있습니다. 에이전트 파일은 YAML frontmatter로 `allowed-tools`, `model` 등을 설정하고, 본문에 역할/원칙을 정의합니다. 검색 경로: 프로젝트 로컬(`.agent-cli/agents/`) → 유저 전역(`~/.agent-cli/agents/`).

**산출물 포맷**: delegate 실행 결과는 구조화된 형식으로 반환됩니다:

```
STATUS: success
RESULT:
(서브에이전트 출력)

[Subagent activity]
- iter 1: read_file auth.py
- iter 2: shell pytest
- iter 3: edit_file auth.py

[Files touched]
- Read: auth.py, config.py
- Modified: auth.py

[Duration: 12.3s] [Subagent used 3 iterations]
```

실패 시에는 `[Last actions before failure]` 섹션이 추가되어 디버깅에 필요한 마지막 액션과 에러 메시지를 확인할 수 있습니다. 결과는 scratchpad artifact로도 자동 저장됩니다.

### run_skill — 스킬 실행

등록된 스킬을 도구로 호출합니다. LLM이 스스로 판단하여 적절한 스킬을 선택합니다. 시스템 프롬프트에 사용 가능한 스킬 목록이 안내됩니다.

```json
{"action": "run_skill", "action_input": {"name": "optimize", "arguments": "./"}}
{"action": "run_skill", "action_input": {"name": "summarize", "arguments": "README.md"}}
```

스킬 내부에서는 별도 ReAct 루프가 실행되며, 결과가 artifact로 저장됩니다. `disable-model-invocation: true`인 스킬은 LLM이 호출할 수 없습니다.

결과 포맷:
```
STATUS: success
RESULT:
SKILL: summarize(./)
The agent-cli directory contains a ReAct pattern-based agent CLI...

[Internal skill calls during this execution:]
- run_skill(optimize): Task completed: Analysis done.
```

`SKILL:` 헤더로 실행된 스킬과 인자를 식별하고, `[Internal skill calls]`로 내부에서 호출된 스킬 이력을 표시합니다.

#### 스킬 스택 (재귀 방지)

스킬 내부에서 다른 스킬을 호출할 수 있지만 재귀는 차단됩니다:

- `A→B` 허용: summarize 내부에서 optimize 호출 가능
- `A→A` 차단: summarize 내부에서 summarize 호출 불가
- `A→B→A` 차단: 순환 호출 불가
- 시스템 프롬프트에서 현재 실행 중인 스킬은 자동으로 숨김

### read_artifact — Artifact 읽기

이전 도구 실행 결과(artifact)를 읽습니다. `read_file`과 달리 hashline 태그 없이 원본 내용을 반환합니다. 컨텍스트 압축 후 이전 결과를 복구할 때 사용합니다.

```json
// 특정 artifact 읽기 (scratchpad progress에서 경로 확인)
{"action": "read_artifact", "action_input": {"path": "artifacts/turn_0003.md"}}

// 현재 세션의 artifact 목록
{"action": "read_artifact", "action_input": {"mode": "list"}}

// 태그로 검색 (파일명, 스킬명 등)
{"action": "read_artifact", "action_input": {"mode": "search", "tag": "hooks.py"}}
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

### 세션 & 컨텍스트 관리 시스템

장시간 태스크에서 초반 맥락 손실을 방지하고, 도구 실행 결과를 영속적으로 보존하는 시스템입니다.

#### 설계 배경

On-premise LLM(128K context)에서 긴 태스크를 수행하면 세 가지 문제가 발생합니다:

1. **초반 맥락 망각** — 턴이 많아지면 대화 초반의 목표/결정을 잊고 같은 시도를 반복
2. **도구 결과 손실** — 컨텍스트 압축(compaction) 발생 시 이전에 읽은 파일 내용의 디테일이 소실
3. **컨텍스트 오염** — 도구 결과 원문을 자동 주입하면 LLM이 과거 결과와 현재 대화를 혼동

이를 해결하기 위한 설계 원칙:

- **Scratchpad = 항상 보이는 나침반** — 태스크 목표와 진행 상황이 매 턴 주입되어 방향 유지
- **Artifact = 디스크에 보존, 필요할 때만 로드** — Lazy Loading 방식으로 LLM이 `read_artifact`로 선택적 복구
- **스킬 내부 격리** — 스킬(run_skill) 실행 시 외부 scratchpad를 주입하지 않아 내부 LLM의 집중도 보장

#### 디렉토리 구조

모든 세션 데이터는 프로젝트 로컬 `.agent-cli/sessions/` 아래에 세션별로 격리됩니다:

```
{project}/.agent-cli/
  skills/                               # 스킬 파일 (git 추적)
  hooks.json                            # hook 설정
  sessions/
    {session_id}/                       # 세션별 디렉토리
      session.jsonl                     # 이터레이션 로그 (append-only)
      session.summary.md                # 세션 종료 시 컨텍스트 요약
      scratchpad.md                     # 태스크 추적 (항상 컨텍스트에 주입)
      artifacts/                        # 도구 실행 결과 보존
        turn_0001.md                    # 일반 도구 결과
        turn_0002.md
        turn_0003_optimize/             # 스킬 내부 결과 (서브디렉토리)
          turn_0004.md
          turn_0005.md
        turn_0006.md
```

#### 핵심 컴포넌트

**Session (session.py)** — 대화 이력 영속화
- `session.jsonl`: 매 이터레이션의 thought/action/observation을 JSONL로 기록
- `session.summary.md`: 세션 종료 시 컨텍스트 윈도우를 저장 (Observation은 짧은 참조로 축약, 원본은 artifact에 보존)
- `--resume <session_id>`: 이전 세션을 이어서 작업
- `agent-cli sessions`: 세션 목록 조회

**Scratchpad (scratchpad.py)** — 태스크 추적 앵커
- 태스크 목표, 진행 상황, 결정 사항을 Markdown + YAML frontmatter로 기록
- 컨텍스트 압축(compaction) 후에도 **항상 살아남는 앵커** 역할
- LLM이 매 턴마다 scratchpad를 보고 이전 작업을 추적
- **스킬 내부 루프에서는 주입하지 않음** — 외부 태스크 정보가 스킬 LLM을 혼란시키는 것을 방지

```markdown
---
status: in_progress
updated_at: 2026-03-28T10:00:00Z
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

## Open Questions
```

**Artifact (scratchpad.py)** — 도구 결과 보존 (Lazy Loading)
- 매 이터레이션의 도구 실행 결과를 YAML frontmatter와 함께 Markdown으로 저장
- 컨텍스트에 자동 주입하지 않음 — LLM이 필요할 때 `read_file`로 선택적 로드
- Scratchpad progress에 artifact 경로가 기록되어 LLM이 참조 가능
- 스킬 내부 artifact는 서브디렉토리에 격리: `artifacts/turn_N_skillname/`

```markdown
---
entry_id: turn_0001
turn: 1
tags: [read_file, agent_cli/hooks.py, skill:optimize]
summary: "read_file: hooks.py (215줄)"
token_count: 850
created_at: 2026-03-28T10:00:00Z
---

STATUS: success
RESULT:
1#PS:"""Hook system — PreToolUse...
```

Artifact는 컨텍스트에 자동 주입되지 않습니다. 대신 scratchpad progress에 경로가 기록되어 LLM이 필요할 때 `read_file`로 직접 로드합니다:

```
## Progress
- [턴3] read_file: hooks.py (215줄) → artifacts/turn_0003.md       ← LLM이 필요하면 이 경로를 read_file
- [턴5] shell: find agent_cli -name '*.py' → artifacts/turn_0005.md
- [턴7] Task completed: Agent-CLI is a ReAct... → artifacts/turn_0007.md
```

**ContextBudget (scratchpad.py)** — 모델 크기별 토큰 배분

컨텍스트 윈도우를 섹션별로 동적 배분합니다:

| 섹션 | 8K 모델 | 32K 모델 | 128K+ 모델 |
|------|---------|---------|-----------|
| System Prompt + Tools | 15% | 12% | 8% |
| Scratchpad (항상 로드) | 10% | 6% | 3% |
| Artifacts (선택적) | 15% | 25% | 35% |
| Conversation History | 52% | 51% | 50% |
| Response Budget | 8% | 6% | 4% |

#### 루프 내 동작 원리

#### 컨텍스트 윈도우 레이아웃

`ctx.get_messages()`가 반환하는 메시지 순서 (LLM이 보는 것):

```
[0] user:      [Scratchpad — persistent task context]
               goal, progress, decisions (compaction에서 살아남는 앵커)
               ※ 스킬 내부 루프에서는 주입하지 않음

[1] assistant: "Understood. I have the scratchpad context..."

[2] user:      "This conversation is being continued from earlier context
               that was compressed. ..."          ← compaction 발생 시만
               (요약 + resume 지시문 + artifact 복구 힌트)

[3] assistant: "Understood. Resuming where we left off."

[4] user:      "hooks.py 분석해줘"               ← 실제 사용자 입력
[5] assistant: {"action": "read_file", ...}      ← LLM 응답
[6] user:      "Observation: STATUS: success..." ← 도구 결과
[7] assistant: {"action": "complete", ...}       ← 최종 답변
[8] user:      "Used skill: optimize(./) — ..."  ← 스킬 호출 기록
[9] assistant: "Analysis complete..."             ← 스킬 결과
...
```

#### 컨텍스트 추가 주체 (ctx.add 위치)

main.py는 ctx.add를 직접 호출하지 않습니다. 각 컴포넌트가 자기 영역을 책임:

```
AgentLoop (일반 대화):
  ├─ _setup()           → ctx.add("user", query)
  ├─ _append_*()        → ctx.add(assistant, llm_text) + ctx.add(user, observation)
  ├─ _maybe_checkpoint  → ctx.add("user", checkpoint_msg)
  └─ complete/echo      → ctx.add("assistant", final_answer)

_dispatch_skill (스킬 호출):
  ├─ 호출 시   → ctx.add("user", "Used skill: name(args) — results follow")
  └─ 결과     → ctx.add("assistant", result)
```

#### 루프 동작 플로우

`run`과 `chat` 모두 동일한 세션/컨텍스트 관리를 사용합니다. `--headless` (서브에이전트)는 tmpdir 기반 휘발성 컨텍스트를 사용합니다.

```
run / chat 시작
  │
  ├─ [headless] → tmpdir 기반 ContextManager (세션 없음, 종료 시 휘발)
  ├─ [일반]    → create_session() + ContextManager(session_id=...)
  │
  ├─ (chat) 사용자 입력 루프 / (run) 단일 쿼리
  │    │
  │    ├─ /skill 입력 → _dispatch_skill
  │    │    ├─ [최초] init_task() → scratchpad.md 생성
  │    │    ├─ progress: "User: /name args"
  │    │    ├─ ctx.add("user", "Used skill: name(args) — results follow")
  │    │    ├─ execute_skill → 내부 AgentLoop 실행 (scratchpad 이미 존재하므로 재생성 안 함)
  │    │    └─ ctx.add("assistant", result)
  │    │
  │    └─ 일반 입력 → AgentLoop.run()
  │         ├─ [최초] init_task() → scratchpad.md 생성 (이미 있으면 스킵)
  │         ├─ progress: "User: 사용자 입력 텍스트"
  │         ├─ ctx.add("user", query)
  │         ├─ ctx.get_messages() → 위 레이아웃대로 조립
  │         │
  │         └─ while iteration:
  │              ├─ LLM 호출 → 응답
  │              ├─ [complete] → ctx.add("assistant", answer) → return
  │              ├─ [run_skill] → 내부 AgentLoop (ctx 공유, scratchpad 스킵)
  │              ├─ [도구] → ctx.add(assistant+observation) + artifact 저장
  │              └─ [compaction] → 요약 + artifact 복구 힌트
  │
  ├─ [일반] finalize_session(ctx) → session.summary.md 저장 + session_id 출력
  └─ [headless] tmpdir 정리 (자동)
```

#### 태그 체계

artifact의 태그는 나중에 관련 artifact를 찾을 때 사용됩니다:

| 상황 | 태그 | 예시 |
|------|------|------|
| 파일 읽기/쓰기/편집 | `[tool_name, filepath]` | `["read_file", "agent_cli/hooks.py"]` |
| 셸 명령 | `["shell"]` | `["shell"]` |
| 서브에이전트 위임 | `["delegate"]` | `["delegate"]` |
| 작업 완료 | `["complete"]` | `["complete"]` |
| 스킬 내부 도구 | `[tool_name, filepath, "skill:name"]` | `["read_file", "loop.py", "skill:optimize"]` |
| 스킬 내부 완료 | `["complete", "skill:name"]` | `["complete", "skill:optimize"]` |

현재 artifact는 **Lazy Loading** 방식으로 동작합니다:
- 컨텍스트에 자동 주입하지 않음 (LLM 혼란 방지)
- Scratchpad progress에 artifact 경로 기록
- 시스템 프롬프트에 "필요하면 read_artifact로 artifact를 읽으세요" 안내
- Compaction 후 "이전 도구 결과는 artifact에 있습니다" 힌트 주입
- LLM이 스스로 판단하여 필요한 artifact만 선택적으로 `read_file` 로드

#### 세션 관리 명령어

```bash
# 세션 목록
agent-cli sessions

# 이전 세션 이어서 작업
agent-cli chat --resume <session_id>
```

### 컨텍스트 압축

메시지 수와 문자 수를 모두 확인하는 dual-gate 조건을 충족하면 LLM 기반 구조화 요약으로 자동 압축합니다. 첫 압축은 전체 요약, 이후는 증분 업데이트. Scratchpad와 artifact는 압축 후에도 보존됩니다.

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
| 128K | 15,728 | 393 |
| 262K | 31,457 | 786 |
| 1M+ | 40,000 (최대) | 1,000 (최대) |

`read_file`이 truncation되면 나머지를 읽을 수 있는 `line_start` 가이드가 자동 표시됩니다.

## 프로젝트 구조

```
agent_cli/
├── main.py              CLI 명령어 (run, chat, setup, sessions)
├── loop.py              AgentLoop 클래스 + ReAct 에이전트 루프
├── config.py            config.json 3레이어 로딩 + models.json 레지스트리
├── setup.py             SetupWizard (첫 실행 설정 마법사)
├── constants.py         공유 상수 (타임아웃, 임계값)
├── hooks.py             Hook 시스템 (PreToolUse/PostToolUse)
├── render.py            Rich 터미널 렌더링
├── input_history.py     readline 히스토리 영속화
├── providers/           LLM 프로바이더 (Anthropic, OpenAI, Ollama)
├── parsing/             3단계 JSON 파서 + Thinking 블록 분리
├── tools/               도구 (read/write/edit/shell/delegate/context) + 출력 압축
├── context/             컨텍스트 관리 (scratchpad, artifact, 압축, 세션 영속화)
├── prompts/             조건부 시스템 프롬프트
└── skills/              프롬프트 스킬 시스템 (로더, 실행기, 모델)
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

설정 우선순위 및 전체 환경변수 목록은 [설정 섹션](#설정)을 참조하세요.

## 라이선스

MIT License
