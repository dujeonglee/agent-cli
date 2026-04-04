# Delegate Agent 파라미터 — 테스트 계획

> 작성일: 2026-04-04
> 상태: 리뷰 대기

---

## 1. 단위 테스트

### 1.1 에이전트 이름 검증 (`tests/test_delegate.py` 확장)

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| AG-01 | `test_validate_agent_name_valid` | 유효한 이름 (`reviewer`, `security-reviewer`, `my_agent_01`) → True |
| AG-02 | `test_validate_agent_name_path_traversal` | `../etc/passwd`, `../../secret` → False |
| AG-03 | `test_validate_agent_name_slash` | `foo/bar`, `/absolute` → False |
| AG-04 | `test_validate_agent_name_special_chars` | `agent name`, `agent;rm`, `agent.md` → False |
| AG-05 | `test_validate_agent_name_empty` | `""` → False |

### 1.2 에이전트 파일 로딩 (`tests/test_delegate.py` 확장)

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| AG-06 | `test_load_agent_body_only` | frontmatter 없는 파일 → (body, {}, None) |
| AG-07 | `test_load_agent_with_frontmatter` | frontmatter + body → (body, {allowed-tools: [...], model: "..."}, None) |
| AG-08 | `test_load_agent_frontmatter_parse_error` | 잘못된 YAML → frontmatter 무시, 파일 전체를 body로 사용 |
| AG-09 | `test_load_agent_not_found` | 존재하지 않는 에이전트 → (None, {}, "not found ...") |
| AG-10 | `test_load_agent_invalid_name` | `../hack` → (None, {}, "Invalid agent name ...") |
| AG-11 | `test_load_agent_empty_body` | frontmatter만 있고 body 없음 → (None, {}, "no content") |
| AG-12 | `test_load_agent_project_overrides_global` | 프로젝트와 전역 모두 같은 이름 존재 → 프로젝트 파일 사용 |
| AG-13 | `test_load_agent_falls_back_to_global` | 프로젝트에 없고 전역에만 존재 → 전역 파일 사용 |
| AG-14 | `test_load_agent_unknown_frontmatter_fields_ignored` | Claude Code 호환: 미인식 필드 무시, 에러 없음 |

### 1.3 delegate 실행과 에이전트 통합 (`tests/test_delegate.py` 확장)

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| AG-15 | `test_run_single_with_agent_injects_role` | `agent_name="reviewer"` → run_loop에 `agent_role` 전달됨 |
| AG-16 | `test_run_single_agent_not_found_returns_error` | 존재하지 않는 에이전트 → ToolResult(False, error=...) |
| AG-17 | `test_run_single_agent_tools_override` | 에이전트 `allowed-tools: [read_file]` → allowed_tools=[read_file]로 전달 |
| AG-18 | `test_run_single_task_tools_overrides_agent_tools` | task에 `tools: [shell]` + 에이전트에 `allowed-tools: [read_file]` → tools=[shell] 사용 |
| AG-19 | `test_run_single_agent_model_override` | 에이전트 `model: other-model` → model="other-model"로 전달 |
| AG-20 | `test_run_single_without_agent_unchanged` | `agent_name=""` → 기존 동작과 동일 (agent_role="" 전달) |
| AG-21 | `test_tool_delegate_passes_agent_name` | tool_delegate에서 spec["agent"]를 _run_single에 전달하는지 확인 |
| AG-22 | `test_parallel_with_different_agents` | 병렬 실행 시 각 task가 서로 다른 agent를 사용할 수 있음 |

### 1.4 시스템 프롬프트 주입 (`tests/test_system_prompt.py` 확장)

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| AG-23 | `test_build_system_prompt_with_agent_role` | `agent_role="You are a reviewer"` → 프롬프트에 `## Agent Role` 섹션 포함 |
| AG-24 | `test_build_system_prompt_without_agent_role` | `agent_role=""` → `## Agent Role` 섹션 없음 |
| AG-25 | `test_agent_role_before_directives` | agent_role 섹션이 Directives 섹션보다 앞에 위치 |
| AG-26 | `test_agent_role_after_git_context` | agent_role 섹션이 Git Context 섹션보다 뒤에 위치 |

### 1.5 인라인 가이드 (`tests/test_system_prompt.py` 확장)

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| AG-27 | `test_delegate_inline_mentions_agent` | `_DELEGATE_INLINE`에 `"agent"` 필드 설명 포함 |
| AG-28 | `test_delegate_inline_agent_example` | 에이전트 사용 예시 포함 (`"agent": "security-reviewer"`) |

### 1.6 스키마 변경 (`tests/test_registry.py` 확장)

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| AG-29 | `test_delegate_schema_has_agent_field` | DELEGATE_TOOL_SCHEMA의 items.properties에 `agent` 필드 존재 |
| AG-30 | `test_delegate_schema_agent_not_required` | `agent`가 required에 포함되지 않음 |

## 2. 테스트 구현 가이드

### 2.1 AG-06 구현 예시

```python
def test_load_agent_body_only(tmp_path, monkeypatch):
    """Agent file without frontmatter uses entire content as role prompt."""
    agents_dir = tmp_path / ".agent-cli" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text(
        "You are a code reviewer.\nCheck for bugs."
    )
    monkeypatch.setattr(
        "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
        [agents_dir],
    )

    role, config, error = _load_agent("reviewer")

    assert error is None
    assert role == "You are a code reviewer.\nCheck for bugs."
    assert config == {}
```

### 2.2 AG-07 구현 예시

```python
def test_load_agent_with_frontmatter(tmp_path, monkeypatch):
    """Agent file with frontmatter parses config and body separately."""
    agents_dir = tmp_path / ".agent-cli" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "secure.md").write_text(
        "---\nallowed-tools:\n  - read_file\n  - shell\nmodel: test-model\n---\n\nYou are a security reviewer."
    )
    monkeypatch.setattr(
        "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
        [agents_dir],
    )

    role, config, error = _load_agent("secure")

    assert error is None
    assert role == "You are a security reviewer."
    assert config["allowed-tools"] == ["read_file", "shell"]
    assert config["model"] == "test-model"
```

### 2.3 AG-12 구현 예시

```python
def test_load_agent_project_overrides_global(tmp_path, monkeypatch):
    """Project-local agent takes priority over user-global."""
    project_dir = tmp_path / "project" / ".agent-cli" / "agents"
    global_dir = tmp_path / "global" / ".agent-cli" / "agents"
    project_dir.mkdir(parents=True)
    global_dir.mkdir(parents=True)

    (project_dir / "reviewer.md").write_text("Project reviewer")
    (global_dir / "reviewer.md").write_text("Global reviewer")

    monkeypatch.setattr(
        "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
        [project_dir, global_dir],
    )

    role, config, error = _load_agent("reviewer")

    assert error is None
    assert role == "Project reviewer"
```

### 2.4 AG-15 구현 예시

```python
def test_run_single_with_agent_injects_role(tmp_path, monkeypatch, mock_provider, caps):
    """Agent name triggers role prompt injection into run_loop."""
    agents_dir = tmp_path / ".agent-cli" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "tester.md").write_text("You are a test engineer.")
    monkeypatch.setattr(
        "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
        [agents_dir],
    )

    captured_kwargs = {}
    def mock_run_loop(**kwargs):
        captured_kwargs.update(kwargs)
        return "done"

    monkeypatch.setattr("agent_cli.tools.delegate.run_loop", mock_run_loop)

    result = _run_single(
        task="Write tests",
        agent_name="tester",
        provider=mock_provider,
        model="test",
        capabilities=caps,
    )

    assert result.success
    assert captured_kwargs["agent_role"] == "You are a test engineer."
```

### 2.5 AG-18 구현 예시

```python
def test_run_single_task_tools_overrides_agent_tools(tmp_path, monkeypatch, mock_provider, caps):
    """Task-level tools take priority over agent allowed-tools."""
    agents_dir = tmp_path / ".agent-cli" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reader.md").write_text(
        "---\nallowed-tools:\n  - read_file\n---\n\nYou are a reader."
    )
    monkeypatch.setattr(
        "agent_cli.tools.delegate._AGENT_SEARCH_PATHS",
        [agents_dir],
    )

    captured_kwargs = {}
    def mock_run_loop(**kwargs):
        captured_kwargs.update(kwargs)
        return "done"

    monkeypatch.setattr("agent_cli.tools.delegate.run_loop", mock_run_loop)

    result = _run_single(
        task="Run shell",
        agent_name="reader",
        allowed_tools=["shell"],  # explicit task tools
        provider=mock_provider,
        model="test",
        capabilities=caps,
    )

    assert result.success
    assert captured_kwargs["active_tools"] == ["shell"]
```

## 3. 테스트 우선순위

### P0 (필수, 구현과 동시)

AG-01 ~ AG-05 (이름 검증), AG-06 ~ AG-10 (로딩 핵심), AG-15 ~ AG-20 (delegate 통합), AG-23 ~ AG-24 (프롬프트 주입), AG-29 ~ AG-30 (스키마)

### P1 (중요, 구현 직후)

AG-11 ~ AG-14 (로딩 엣지 케이스), AG-21 ~ AG-22 (전달 경로), AG-25 ~ AG-28 (순서/가이드)

## 4. 기존 테스트 영향 분석

| 기존 테스트 | 영향 | 이유 |
|------------|------|------|
| `test_delegate_*` (delegate.py 테스트) | 없음 | agent_name 기본값 `""` → 에이전트 로딩 건너뜀 |
| `test_system_prompt_*` | 없음 | agent_role 기본값 `""` → Agent Role 섹션 생략 |
| `test_registry_*` | 없음 | agent 필드 추가는 optional, required 변경 없음 |
| `test_loop_*` | 없음 | agent_role 기본값 `""` → build_system_prompt에 영향 없음 |

**결론: 기존 테스트에 대한 회귀 영향 없음.** 모든 새 파라미터의 기본값이 기존 동작을 유지한다.
