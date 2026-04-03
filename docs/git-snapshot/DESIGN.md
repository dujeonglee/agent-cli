# Git 상태 스냅샷 — 설계 문서

> 작성일: 2026-04-04
> 상태: 리뷰 대기

---

## 1. 아키텍처 개요

### Before

```
build_system_prompt()
  ├─ ROLE_PROMPT
  ├─ TASK_GUIDELINES
  ├─ FORMAT_RULES
  ├─ _build_tools_section()
  ├─ build_skill_descriptions()
  ├─ Session
  ├─ _build_environment_section()    ← Environment 섹션
  └─ _load_directives()
```

### After

```
build_system_prompt()
  ├─ ROLE_PROMPT
  ├─ TASK_GUIDELINES
  ├─ FORMAT_RULES
  ├─ _build_tools_section()
  ├─ build_skill_descriptions()
  ├─ Session
  ├─ _build_environment_section()    ← Environment 섹션
  ├─ _build_git_context_section()    ← 신규: Git Context 섹션
  └─ _load_directives()
```

## 2. 상수 정의

```python
# ── Git Context budget ──────────────────────────
MAX_GIT_DIFF_CHARS = 4000
_GIT_CMD_TIMEOUT = 3  # seconds
```

`system_prompt.py` 상단 상수 영역에 추가. 기존 `MAX_DIRECTIVE_FILE_CHARS` 등과 동일 패턴.

## 3. 신규 함수: `_build_git_context_section()`

### 3.1 시그니처

```python
def _build_git_context_section() -> str:
    """Build Git context section with current branch and diff.

    Returns formatted section string, or empty string if:
    - git is not installed
    - CWD is not a git repository
    - git commands fail or timeout
    """
```

### 3.2 구현 플로우

```
_build_git_context_section()
  │
  ├─ shutil.which("git") 확인
  │   └─ None → return ""
  │
  ├─ _run_git_cmd(["git", "status", "--short", "--branch"])
  │   └─ 실패/타임아웃 → return ""
  │
  ├─ _run_git_cmd(["git", "diff", "HEAD"])
  │   └─ 실패 → diff = "" (status만으로 섹션 구성)
  │
  ├─ diff 예산 적용
  │   └─ len(diff) > MAX_GIT_DIFF_CHARS → truncate + 메시지
  │
  └─ 섹션 문자열 조립 → return
```

### 3.3 헬퍼 함수: `_run_git_cmd()`

```python
def _run_git_cmd(args: list[str]) -> str | None:
    """Run a git command and return stdout, or None on failure.

    Returns None if:
    - Command exits with non-zero status
    - Command times out (> _GIT_CMD_TIMEOUT seconds)
    - Any other exception occurs
    """
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_GIT_CMD_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
```

- `shell=False` (기본값) — command injection 방지
- `capture_output=True` — stdout/stderr 분리 캡처
- `text=True` — 문자열 반환
- `timeout=_GIT_CMD_TIMEOUT` — 대형 저장소 방어

### 3.4 섹션 조립

```python
def _build_git_context_section() -> str:
    if shutil.which("git") is None:
        return ""

    status_output = _run_git_cmd(["git", "status", "--short", "--branch"])
    if status_output is None:
        return ""

    lines = ["## Git Context"]
    lines.append(f"$ git status --short --branch\n{status_output.rstrip()}")

    diff_output = _run_git_cmd(["git", "diff", "HEAD"])
    if diff_output:
        if len(diff_output) > MAX_GIT_DIFF_CHARS:
            total = len(diff_output)
            diff_output = (
                diff_output[:MAX_GIT_DIFF_CHARS]
                + f"\n[diff truncated — {total}chars total]"
            )
        lines.append(f"$ git diff HEAD\n{diff_output.rstrip()}")

    return "\n\n".join(lines)
```

## 4. `build_system_prompt()` 변경

### 4.1 변경 위치

`agent_cli/prompts/system_prompt.py:219` — `_build_environment_section()` 호출 직후, `_load_directives()` 직전.

### 4.2 변경 내용

```python
# 기존
sections.append(_build_environment_section())

directives = _load_directives()

# 변경 후
sections.append(_build_environment_section())

git_context = _build_git_context_section()
if git_context:
    sections.append(git_context)

directives = _load_directives()
```

## 5. 파일 변경 목록

### 5.1 수정 파일

| 파일 | 변경 내용 |
|------|----------|
| `agent_cli/prompts/system_prompt.py` | `_run_git_cmd()`, `_build_git_context_section()` 추가, `build_system_prompt()`에 Git Context 섹션 삽입 |

### 5.2 신규 파일

없음.

### 5.3 삭제 코드

없음.

## 6. import 추가

```python
import shutil
import subprocess
```

`system_prompt.py` 상단에 추가. 모두 표준 라이브러리 — 외부 의존성 없음.

## 7. 엣지 케이스

| 상황 | 동작 |
|------|------|
| Git 미설치 | `shutil.which("git")` → None → 섹션 생략 |
| Git 저장소 아님 | `git status` exit code 128 → `_run_git_cmd()` → None → 섹션 생략 |
| 빈 저장소 (커밋 없음) | `git diff HEAD` 실패 → diff 부분만 생략, status는 표시 |
| 변경 없음 | `git diff HEAD` stdout 빈 문자열 → diff 부분 생략 |
| 대형 diff (>4000자) | truncate + `[diff truncated — Nchars total]` |
| Git 명령 타임아웃 | `subprocess.TimeoutExpired` 예외 → None → 섹션 생략 |
| 권한 없음 (`.git` 읽기 불가) | `git status` 실패 → 섹션 생략 |

## 8. 향후 확장 (범위 외)

- 사용자 설정으로 기능 on/off 토글
- diff 예산을 사용자 설정 가능하게
- `git log --oneline -5` 최근 커밋 요약 추가
- 매 턴 갱신 옵션 (성능 고려 필요)
