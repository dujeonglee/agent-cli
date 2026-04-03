# Git 상태 스냅샷 — 테스트 계획

> 작성일: 2026-04-04
> 상태: 리뷰 대기

---

## 1. 단위 테스트 (`tests/test_system_prompt.py` 확장)

### 1.1 `_run_git_cmd()` 헬퍼

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| G-01 | `test_run_git_cmd_success` | 정상 실행 시 stdout 문자열 반환 |
| G-02 | `test_run_git_cmd_nonzero_exit` | exit code != 0 시 None 반환 |
| G-03 | `test_run_git_cmd_timeout` | `subprocess.TimeoutExpired` 발생 시 None 반환 |
| G-04 | `test_run_git_cmd_file_not_found` | git 바이너리 없을 때 `FileNotFoundError` → None 반환 |
| G-05 | `test_run_git_cmd_os_error` | 기타 `OSError` 발생 시 None 반환 |

### 1.2 `_build_git_context_section()`

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| G-06 | `test_git_context_no_git_binary` | `shutil.which("git")` → None 시 빈 문자열 반환 |
| G-07 | `test_git_context_not_a_repo` | `git status` 실패 시 빈 문자열 반환 |
| G-08 | `test_git_context_with_status_and_diff` | status + diff 모두 있을 때 `## Git Context` 섹션 포함 |
| G-09 | `test_git_context_status_only_no_diff` | diff 빈 문자열일 때 status만 포함, diff 부분 생략 |
| G-10 | `test_git_context_diff_truncation` | diff > `MAX_GIT_DIFF_CHARS` 시 잘림 + `[diff truncated — Nchars total]` 메시지 |
| G-11 | `test_git_context_diff_at_budget_boundary` | diff == `MAX_GIT_DIFF_CHARS` 시 truncate하지 않음 |
| G-12 | `test_git_context_diff_failure_shows_status` | `git diff HEAD` 실패(None) 시 status만 포함 |

### 1.3 `build_system_prompt()` 통합

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| G-13 | `test_system_prompt_includes_git_context` | Git 저장소 내에서 `build_system_prompt()` 결과에 `## Git Context` 포함 |
| G-14 | `test_system_prompt_git_context_after_environment` | Git Context 섹션이 Environment 섹션 뒤에 위치 |
| G-15 | `test_system_prompt_git_context_before_directives` | Git Context 섹션이 Directives 섹션 앞에 위치 |
| G-16 | `test_system_prompt_no_git_context_when_no_git` | Git 없을 때 시스템 프롬프트에 `## Git Context` 미포함 |

## 2. 모킹 전략

### 2.1 `_run_git_cmd()` 테스트 (G-01 ~ G-05)

`subprocess.run`을 `unittest.mock.patch`로 모킹:

```python
@patch("agent_cli.prompts.system_prompt.subprocess.run")
def test_run_git_cmd_success(mock_run):
    mock_run.return_value = CompletedProcess(args=[], returncode=0, stdout="output\n")
    result = _run_git_cmd(["git", "status"])
    assert result == "output\n"
    mock_run.assert_called_once()
```

### 2.2 `_build_git_context_section()` 테스트 (G-06 ~ G-12)

`shutil.which`와 `_run_git_cmd`를 모킹:

```python
@patch("agent_cli.prompts.system_prompt._run_git_cmd")
@patch("agent_cli.prompts.system_prompt.shutil.which", return_value="/usr/bin/git")
def test_git_context_with_status_and_diff(mock_which, mock_git):
    mock_git.side_effect = [
        "## main\n M file.py\n",  # git status
        "diff --git ...\n",        # git diff
    ]
    result = _build_git_context_section()
    assert "## Git Context" in result
    assert "git status --short --branch" in result
    assert "git diff HEAD" in result
```

### 2.3 `build_system_prompt()` 통합 테스트 (G-13 ~ G-16)

`_build_git_context_section`을 모킹하여 시스템 프롬프트 전체 조립 검증:

```python
@patch("agent_cli.prompts.system_prompt._build_git_context_section")
def test_system_prompt_includes_git_context(mock_git_ctx):
    mock_git_ctx.return_value = "## Git Context\n$ git status\n## main"
    prompt = build_system_prompt(capabilities=..., active_tools=[...])
    assert "## Git Context" in prompt
```

## 3. 테스트 우선순위

### P0 (필수, 구현과 동시)

G-01 ~ G-05 (`_run_git_cmd` 헬퍼), G-06 ~ G-09 (기본 섹션 빌드), G-13, G-16 (시스템 프롬프트 통합)

### P1 (중요, 구현 직후)

G-10 ~ G-12 (엣지 케이스), G-14 ~ G-15 (섹션 순서)

## 4. 테스트 실행

```bash
# 전체 테스트
pytest tests/ -m "not ollama_integration"

# Git 스냅샷 관련만
pytest tests/test_system_prompt.py -k "git" -v
```
