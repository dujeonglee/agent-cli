# Delegate Agent 파라미터 — 요구사항 문서

> 작성일: 2026-04-04
> 상태: 리뷰 대기

---

## 1. 배경

현재 delegate 도구의 tasks 배열 항목은 `task`, `context`, `tools` 필드만 지원한다.
서브에이전트의 역할이나 행동 원칙을 제어하려면 task 문자열 안에 모든 지시사항을 인라인으로 기술해야 한다.
이로 인해:
- **task 비대화**: 역할 정의, 원칙, 제약 등이 task 문자열에 섞여 가독성 저하
- **재사용 불가**: 동일한 역할을 반복 사용할 때마다 같은 지시사항을 복사·붙여넣기
- **Claude Code와의 불일치**: Claude Code는 `.claude/agents/` 디렉토리에 에이전트 정의 파일을 두고 재사용하는 패턴을 지원

## 2. 목표

delegate 도구의 tasks 항목에 선택적 `agent` 필드를 추가하여:
1. 미리 정의된 에이전트 파일을 로드하여 서브에이전트의 시스템 프롬프트에 역할/원칙 주입
2. YAML frontmatter를 통한 도구 제한, 모델 오버라이드 등 선언적 설정
3. `.agent-cli/agents/` 파일 기반으로 에이전트 역할의 재사용성 확보
4. Claude Code의 `.claude/agents/` 파일과 포맷 호환 (복사해서 사용 가능)
5. agent 필드 없는 기존 호출은 동작 변경 없음 (하위 호환)

## 3. 기능 요구사항

### 3.1 agent 필드

delegate 도구의 tasks 배열 항목에 선택적 `agent` 필드 추가:

```json
{
  "tasks": [
    {
      "task": "이 PR의 보안 취약점을 검토해줘",
      "agent": "reviewer"
    }
  ]
}
```

- `agent` 값은 에이전트 정의 파일의 이름 (확장자 제외)
- 미지정 시 기존 동작 유지

### 3.2 에이전트 파일 탐색 경로

다음 순서로 `{agent_name}.md` 파일을 탐색한다 (먼저 찾은 것 우선):

| 우선순위 | 경로 | 용도 |
|---------|------|------|
| 1 | `.agent-cli/agents/{name}.md` | 프로젝트 로컬 에이전트 |
| 2 | `~/.agent-cli/agents/{name}.md` | 사용자 전역 에이전트 |

- 프로젝트 로컬이 사용자 전역보다 우선 (기존 DIRECTIVE.md, skills, models.json과 동일한 패턴)
- 파일을 찾지 못하면 에러 반환: `"Agent '{name}' not found in .agent-cli/agents/ or ~/.agent-cli/agents/"`

### 3.3 에이전트 파일 포맷

YAML frontmatter + markdown body 형식. Claude Code의 `.claude/agents/*.md`와 동일한 포맷.

#### 3.3.1 frontmatter 없는 경우 (본문만)

```markdown
You are a security reviewer.
Focus on OWASP Top 10 vulnerabilities.
Always explain the risk and suggest a fix.
```

파일 본문 전체를 역할 프롬프트로 사용한다.

#### 3.3.2 frontmatter 있는 경우

```markdown
---
allowed-tools:
  - read_file
  - shell
model: claude-sonnet-4-20250514
---

You are a security reviewer.
Focus on OWASP Top 10 vulnerabilities.
Always explain the risk and suggest a fix.
```

frontmatter에서 파싱하는 필드:

| 필드 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| `allowed-tools` | `list[str]` | `null` (전체 허용) | 서브에이전트 사용 가능 도구 |
| `model` | `str` | `null` (호출자 모델) | 서브에이전트 사용 모델 오버라이드 |

- `allowed-tools`: task 항목의 `tools` 필드와 동일한 역할. 둘 다 지정 시 task 항목의 `tools`가 우선.
- `model`: task 항목에 model 필드는 없으므로 에이전트 파일에서만 설정 가능.

#### 3.3.3 Claude Code 호환성

Claude Code의 `.claude/agents/` 파일 포맷 호환:
- Claude Code 파일에는 `allowed-tools`, `model` 외에 다른 frontmatter 필드가 있을 수 있음
- agent-cli에서 인식하지 않는 frontmatter 필드는 무시 (무효 경고 없음)
- 파일을 `.claude/agents/`에서 `.agent-cli/agents/`로 복사하면 바로 사용 가능

### 3.4 시스템 프롬프트 주입

에이전트 파일의 본문(역할 프롬프트)은 서브에이전트의 시스템 프롬프트에 주입된다.

주입 위치: 기존 시스템 프롬프트의 `## Directives` 섹션 앞, 별도 섹션으로 추가.

```
## Agent Role
{에이전트 파일 본문}
```

- 시스템 프롬프트의 recency 영역 (끝부분)에 배치하여 LLM attention 확보
- 기존 시스템 프롬프트 구조를 파괴하지 않음

### 3.5 설정 오버라이드 우선순위

에이전트 파일과 task 항목 필드가 겹칠 때의 우선순위:

| 설정 | 우선순위 (높은 → 낮은) |
|------|----------------------|
| `allowed-tools` / `tools` | task 항목 `tools` > 에이전트 `allowed-tools` > 기본값 (전체) |
| `model` | 에이전트 `model` > 호출자 모델 |

task 항목의 명시적 지정이 에이전트 파일 설정을 오버라이드한다 (tools 한정).

### 3.6 인라인 가이드 업데이트

`_DELEGATE_INLINE` (system_prompt.py)에 agent 필드 사용법 추가:

```
- "agent": optionally specify a predefined agent name (from .agent-cli/agents/).
Examples:
- With agent: {"tasks": [{"task": "Review this code", "agent": "reviewer"}]}
- Agent + context: {"tasks": [{"task": "Fix the bug", "agent": "fixer", "context": "fork"}]}
```

### 3.7 하위 호환성

- `agent` 필드 미지정 시 기존 동작과 완전 동일
- 기존 delegate 호출의 어떤 동작도 변경되지 않음
- `.agent-cli/agents/` 디렉토리가 없어도 에러 없음 (agent 필드 사용 시에만 탐색)

## 4. 비기능 요구사항

### 4.1 성능

- 에이전트 파일 로딩: 디스크 I/O 1-2회 (최대 2개 경로 탐색)
- 파일 크기 제한 없음 (DIRECTIVE.md와 달리 에이전트별 역할 프롬프트이므로 사용자 책임)
- YAML frontmatter 파싱은 기존 skills/loader.py와 동일한 패턴 사용

### 4.2 보안

- 에이전트 파일 경로는 `{name}.md` 패턴으로 고정. 경로 순회 방지 (`../`, `/` 포함 시 거부)
- 에이전트 이름에 허용되는 문자: `[a-zA-Z0-9_-]` (영숫자, 하이픈, 밑줄)

### 4.3 에러 처리

| 상황 | 동작 |
|------|------|
| agent 파일 미발견 | ToolResult(False, error="Agent '{name}' not found ...") |
| agent 이름에 부적절한 문자 | ToolResult(False, error="Invalid agent name ...") |
| 파일 읽기 실패 (권한 등) | ToolResult(False, error="Cannot read agent file ...") |
| frontmatter YAML 파싱 실패 | frontmatter 무시, 파일 전체를 본문으로 사용 |

### 4.4 코드 변경 범위

| 파일 | 변경 내용 |
|------|----------|
| `agent_cli/tools/registry.py` | DELEGATE_TOOL_SCHEMA에 `agent` 필드 추가 |
| `agent_cli/tools/delegate.py` | `_run_single`에 에이전트 파일 로딩 + 프롬프트 주입 로직 |
| `agent_cli/prompts/system_prompt.py` | `_DELEGATE_INLINE` 가이드 업데이트, `build_system_prompt`에 agent_role 파라미터 추가 |

## 5. 범위 외

- 에이전트 파일 캐싱 (매 호출마다 디스크에서 읽음 — 파일 수정 즉시 반영)
- 에이전트 파일의 디렉토리 구조 (`agents/reviewer/AGENT.md` 등 — 향후 확장)
- 에이전트 간 상속 (base agent + overlay)
- task 항목에 `model` 필드 추가 (에이전트 파일에서만 설정)
- `.claude/agents/` 경로 직접 탐색 (사용자가 수동으로 `.agent-cli/agents/`에 복사)
