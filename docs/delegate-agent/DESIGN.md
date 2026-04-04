# Delegate Agent 파라미터 — 설계 문서

> 작성일: 2026-04-04
> 상태: 리뷰 대기

---

## 1. 변경 개요

delegate 도구의 tasks 항목에 선택적 `agent` 필드를 추가한다.
지정 시 `.agent-cli/agents/{name}.md` 파일을 로드하여 서브에이전트의 시스템 프롬프트에 역할 프롬프트를 주입한다.

## 2. 파일 변경 목록

### 2.1 수정 파일

| 파일 | 변경 내용 |
|------|----------|
| `agent_cli/tools/registry.py` | DELEGATE_TOOL_SCHEMA에 `agent` 필드 추가 (라인 218-242) |
| `agent_cli/tools/delegate.py` | 에이전트 파일 로딩 함수 + `_run_single` 수정 (라인 120-207) |
| `agent_cli/prompts/system_prompt.py` | `build_system_prompt`에 `agent_role` 파라미터 추가 + `_DELEGATE_INLINE` 업데이트 (라인 92-108, 248-290) |

### 2.2 신규/삭제 파일

없음. 에이전트 로딩 로직은 `delegate.py`에 포함한다 (별도 모듈 불필요한 수준의 코드량).

## 3. 상세 변경

### 3.1 `agent_cli/tools/registry.py` — DELEGATE_TOOL_SCHEMA

**현재 코드 (라인 218-242):**

```python
"items": {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "Task description for the subagent",
        },
        "context": {
            "type": "string",
            "enum": ["none", "fork", "inherit"],
            "description": "none (independent), fork (copy context), inherit (share context)",
        },
        "tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Allowed tools (omit for default set)",
        },
    },
    "required": ["task"],
},
```

**변경 후:**

```python
"items": {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "Task description for the subagent",
        },
        "context": {
            "type": "string",
            "enum": ["none", "fork", "inherit"],
            "description": "none (independent), fork (copy context), inherit (share context)",
        },
        "tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Allowed tools (omit for default set)",
        },
        "agent": {
            "type": "string",
            "description": "Agent name to load role/config from .agent-cli/agents/{name}.md",
        },
    },
    "required": ["task"],
},
```

변경 사항: `agent` 필드 1개 추가. optional이므로 `required`에 포함하지 않음.

### 3.2 `agent_cli/tools/delegate.py` — 에이전트 파일 로딩

파일 상단에 에이전트 로딩 관련 함수 2개를 추가한다.

#### 3.2.1 `_validate_agent_name(name: str) -> bool`

```python
import re

_AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

def _validate_agent_name(name: str) -> bool:
    """Validate agent name: alphanumeric, hyphens, underscores only."""
    return bool(_AGENT_NAME_PATTERN.match(name))
```

경로 순회 방지를 위한 이름 검증. `../`, `/`, 공백, 특수문자 차단.

#### 3.2.2 `_load_agent(name: str) -> tuple[str | None, dict, str | None]`

```python
_AGENT_SEARCH_PATHS = [
    Path.cwd() / ".agent-cli" / "agents",
    Path.home() / ".agent-cli" / "agents",
]

_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)",
    re.S,
)


def _load_agent(name: str) -> tuple[str | None, dict, str | None]:
    """Load agent definition file.

    Returns:
        (role_prompt, config_dict, error_message)
        - 성공: (본문, {allowed-tools, model, ...}, None)
        - 실패: (None, {}, error_message)
    """
    if not _validate_agent_name(name):
        return None, {}, f"Invalid agent name '{name}': only [a-zA-Z0-9_-] allowed"

    for search_dir in _AGENT_SEARCH_PATHS:
        agent_file = search_dir / f"{name}.md"
        if agent_file.is_file():
            try:
                text = agent_file.read_text(encoding="utf-8")
            except OSError as e:
                return None, {}, f"Cannot read agent file {agent_file}: {e}"

            # Parse frontmatter
            match = _FRONTMATTER_PATTERN.match(text)
            if match:
                frontmatter_text = match.group(1)
                body = match.group(2).strip()
                try:
                    import yaml
                    config = yaml.safe_load(frontmatter_text)
                    if not isinstance(config, dict):
                        config = {}
                except Exception:
                    # YAML 파싱 실패 시 frontmatter 무시
                    config = {}
                    body = text.strip()
            else:
                config = {}
                body = text.strip()

            if not body:
                return None, {}, f"Agent file '{name}.md' has no content"

            return body, config, None

    paths_str = ", ".join(str(p / f"{name}.md") for p in _AGENT_SEARCH_PATHS)
    return None, {}, f"Agent '{name}' not found. Searched: {paths_str}"
```

설계 포인트:
- `skills/loader.py`의 `_FRONTMATTER_PATTERN`과 동일한 정규식 사용
- `yaml.safe_load`는 try/except로 감싸서 실패 시 본문 전체 사용 (관대한 파싱)
- `yaml` import는 함수 내부에서 수행 (에이전트 미사용 시 import 비용 회피)
- 탐색 순서: 프로젝트 로컬 → 사용자 전역 (먼저 찾으면 반환)

### 3.3 `agent_cli/tools/delegate.py` — `_run_single` 수정

**현재 코드 (라인 120-139):**

```python
def _run_single(
    task: str,
    context_mode: str = "none",
    allowed_tools: list[str] | None = None,
    parent_ctx: ContextManager | None = None,
    provider: LLMProvider | None = None,
    model: str = "",
    ...
) -> ToolResult:
```

**변경 후:**

```python
def _run_single(
    task: str,
    context_mode: str = "none",
    allowed_tools: list[str] | None = None,
    agent_name: str = "",
    parent_ctx: ContextManager | None = None,
    provider: LLMProvider | None = None,
    model: str = "",
    ...
) -> ToolResult:
    """Execute a single delegate task."""
    from agent_cli.loop import run_loop

    if not task.strip():
        return ToolResult(False, error="Delegation rejected: empty task")

    if provider is None or capabilities is None:
        return ToolResult(
            False, error="Delegation rejected: missing provider/capabilities"
        )

    # ── Agent loading ──
    agent_role = ""
    if agent_name:
        role_prompt, agent_config, error = _load_agent(agent_name)
        if error:
            return ToolResult(False, error=f"Delegation rejected: {error}")

        agent_role = role_prompt

        # Agent config overrides (lower priority than explicit task params)
        if allowed_tools is None and agent_config.get("allowed-tools"):
            allowed_tools = agent_config["allowed-tools"]

        agent_model = agent_config.get("model")
        if agent_model and isinstance(agent_model, str):
            model = agent_model

    # ... (이하 기존 context mode 처리 + run_loop 호출)
```

`run_loop` 호출에 `agent_role` 전달:

```python
    result_str = run_loop(
        query=task,
        provider=provider,
        ...
        agent_role=agent_role,  # NEW
    )
```

### 3.4 `agent_cli/tools/delegate.py` — `_run_parallel` 수정

`worker` 함수에서 `_run_single` 호출 시 `agent_name` 전달 추가:

```python
def worker(index: int, spec: dict) -> None:
    results[index] = _run_single(
        task=spec["task"],
        context_mode=spec.get("context", "none"),
        allowed_tools=spec.get("tools"),
        agent_name=spec.get("agent", ""),  # NEW
        ...
    )
```

### 3.5 `agent_cli/tools/delegate.py` — `tool_delegate` 수정

단일 태스크 경로에서도 `agent_name` 전달:

```python
if len(tasks) == 1:
    spec = tasks[0]
    return _run_single(
        task=spec.get("task", ""),
        context_mode=spec.get("context", "none"),
        allowed_tools=spec.get("tools"),
        agent_name=spec.get("agent", ""),  # NEW
        ...
    )
```

### 3.6 `agent_cli/loop.py` — `run_loop` 및 `AgentLoop`

`run_loop`에 `agent_role` 파라미터 추가:

```python
def run_loop(
    ...
    agent_role: str = "",  # NEW
) -> str | None:
```

`AgentLoop.__init__`에서 `self.agent_role = agent_role` 저장.

`AgentLoop._setup`에서 `build_system_prompt` 호출 시 전달:

```python
self.system = build_system_prompt(
    capabilities=self.capabilities,
    active_tools=self.tools_list,
    include_delegate=self.include_delegate,
    skill_stack=self.skill_stack,
    session_id=session_id,
    agent_role=self.agent_role,  # NEW
)
```

### 3.7 `agent_cli/prompts/system_prompt.py` — `build_system_prompt`

**현재 시그니처:**

```python
def build_system_prompt(
    capabilities: ModelCapabilities,
    active_tools: list[str],
    include_delegate: bool = False,
    skill_stack: list[str] | None = None,
    session_id: str = "",
) -> str:
```

**변경 후:**

```python
def build_system_prompt(
    capabilities: ModelCapabilities,
    active_tools: list[str],
    include_delegate: bool = False,
    skill_stack: list[str] | None = None,
    session_id: str = "",
    agent_role: str = "",
) -> str:
```

recency 영역에 agent_role 섹션 삽입 (Directives 바로 앞):

```python
    # ── Recency: current context + user rules ──
    if session_id:
        sections.append(f"## Session\nCurrent session ID: {session_id}")

    sections.append(_build_environment_section())

    git_context = _build_git_context_section()
    if git_context:
        sections.append(git_context)

    # Agent role injection (before directives for strong attention)
    if agent_role:
        sections.append(f"## Agent Role\n{agent_role}")

    directives = _load_directives()
    if directives:
        sections.append(directives)
```

배치 이유:
- recency 영역 끝부분은 LLM이 강한 attention을 주는 위치
- Directives 바로 앞에 배치하여 역할 프롬프트가 task 지시사항과 가까운 위치에 놓임
- 기존 섹션 순서를 파괴하지 않음

### 3.8 `agent_cli/prompts/system_prompt.py` — `_DELEGATE_INLINE` 업데이트

**현재 코드 (라인 92-108):**

```python
_DELEGATE_INLINE = """\

  Always use the "tasks" array format. Single item = sync, multiple = parallel.
  Context modes per task:
  - "none" (default): subagent starts with no context. Task must be self-contained.
  - "fork": subagent receives a copy of the current conversation context.
  - "inherit": subagent shares your context directly (single task only, not parallel).
  - "tools": optionally restrict which tools the subagent can use.
  Constraints:
  - inherit cannot be used with multiple tasks.
  - Multiple tasks run in PARALLEL. If task B depends on task A's result,
    call delegate twice: first A, then use A's result to call B.
  Examples:
  - Single: {"tasks": [{"task": "Read /tmp/data.csv and count rows"}]}
  - With context: {"tasks": [{"task": "Fix the bug we found", "context": "fork"}]}
  - Parallel (independent): {"tasks": [{"task": "Analyze A", "context": "fork"}, {"task": "Analyze B", "context": "fork"}]}
  - Read-only: {"tasks": [{"task": "Review changes", "context": "fork", "tools": ["read_file", "shell"]}]}"""
```

**변경 후:**

```python
_DELEGATE_INLINE = """\

  Always use the "tasks" array format. Single item = sync, multiple = parallel.
  Context modes per task:
  - "none" (default): subagent starts with no context. Task must be self-contained.
  - "fork": subagent receives a copy of the current conversation context.
  - "inherit": subagent shares your context directly (single task only, not parallel).
  - "tools": optionally restrict which tools the subagent can use.
  - "agent": optionally specify a predefined agent from .agent-cli/agents/{name}.md.
    The agent file defines the subagent's role/principles and can set allowed-tools/model.
  Constraints:
  - inherit cannot be used with multiple tasks.
  - Multiple tasks run in PARALLEL. If task B depends on task A's result,
    call delegate twice: first A, then use A's result to call B.
  Examples:
  - Single: {"tasks": [{"task": "Read /tmp/data.csv and count rows"}]}
  - With context: {"tasks": [{"task": "Fix the bug we found", "context": "fork"}]}
  - With agent: {"tasks": [{"task": "Review this code for vulnerabilities", "agent": "security-reviewer"}]}
  - Agent + context: {"tasks": [{"task": "Fix the bug", "agent": "fixer", "context": "fork"}]}
  - Parallel (independent): {"tasks": [{"task": "Analyze A", "context": "fork"}, {"task": "Analyze B", "context": "fork"}]}
  - Read-only: {"tasks": [{"task": "Review changes", "context": "fork", "tools": ["read_file", "shell"]}]}"""
```

변경 사항:
- `agent` 필드 설명 1줄 추가
- 에이전트 사용 예시 2개 추가 (기본, context 결합)

## 4. 데이터 흐름

### 4.1 agent 필드가 있는 delegate 호출

```
tool_delegate(args)
  │
  ├─ tasks[0].agent = "reviewer"
  │
  └─ _run_single(task=..., agent_name="reviewer")
       │
       ├─ _validate_agent_name("reviewer") → True
       │
       ├─ _load_agent("reviewer")
       │    ├─ .agent-cli/agents/reviewer.md 존재? → 파일 읽기
       │    ├─ frontmatter 파싱 → {allowed-tools: [...], model: "..."}
       │    └─ return (body, config, None)
       │
       ├─ agent_role = body
       ├─ allowed_tools = config["allowed-tools"] (task tools 없을 때만)
       ├─ model = config["model"] (있을 때만)
       │
       └─ run_loop(query=task, agent_role=agent_role, ...)
            │
            └─ build_system_prompt(agent_role=agent_role, ...)
                 │
                 └─ "## Agent Role\n{body}" 섹션 삽입
```

### 4.2 agent 필드가 없는 delegate 호출 (기존 동작)

```
tool_delegate(args)
  │
  ├─ tasks[0].agent = "" (미지정)
  │
  └─ _run_single(task=..., agent_name="")
       │
       ├─ agent_name이 빈 문자열 → 에이전트 로딩 건너뜀
       │
       └─ run_loop(query=task, agent_role="", ...)
            │
            └─ build_system_prompt(agent_role="", ...)
                 │
                 └─ agent_role이 빈 문자열 → "## Agent Role" 섹션 생략
```

## 5. 에이전트 파일 예시

### 5.1 frontmatter 없는 간단한 에이전트

`.agent-cli/agents/reviewer.md`:

```markdown
You are a code reviewer focused on quality and correctness.

Principles:
- Check for logic errors, edge cases, and potential bugs
- Verify error handling is appropriate
- Flag security concerns (injection, path traversal, etc.)
- Suggest improvements only when they fix real issues

Report findings concisely. If no issues found, say so.
```

### 5.2 frontmatter 있는 에이전트

`.agent-cli/agents/security-reviewer.md`:

```markdown
---
allowed-tools:
  - read_file
  - shell
model: claude-sonnet-4-20250514
---

You are a security specialist reviewing code for vulnerabilities.

Focus areas:
- OWASP Top 10
- Command injection, path traversal, SSRF
- Authentication and authorization flaws
- Sensitive data exposure

Always explain the risk level and provide a fix suggestion.
Do not modify any files — read-only review only.
```

## 6. 변경하지 않는 코드

1. **`skills/loader.py`**: 에이전트 파일은 스킬이 아님. 별도 로딩 로직 사용.
2. **`config.py`**: 에이전트 파일은 설정이 아님. delegate.py에서 직접 로딩.
3. **`_run_parallel` 내부 로직**: worker에 `agent_name` 전달만 추가. 병렬 실행 메커니즘 변경 없음.
4. **`tool_delegate` 시그니처**: `args` dict에서 꺼내므로 시그니처 변경 불필요.
5. **`ContextManager`**: 에이전트 로딩은 컨텍스트 관리와 무관.

## 7. 참조: 기존 패턴과의 유사성

| 항목 | 에이전트 파일 | 스킬 파일 | DIRECTIVE.md |
|------|-------------|----------|--------------|
| 포맷 | YAML frontmatter + MD body | YAML frontmatter + MD body | MD only |
| 탐색 경로 | .agent-cli/agents/ → ~/.agent-cli/agents/ | .agent-cli/skills/ → ~/.agent-cli/skills/ | .agent-cli/ → ~/.agent-cli/ |
| 프로젝트 우선 | O | O | O |
| 캐싱 | X (매번 로딩) | O (전역 캐시) | X (매 프롬프트 빌드) |
| 용도 | 서브에이전트 역할 정의 | 프롬프트 워크플로 | 사용자 전역 지시 |
