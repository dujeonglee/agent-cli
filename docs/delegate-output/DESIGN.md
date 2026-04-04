# Delegate 산출물 개선 — 설계 문서

> 작성일: 2026-04-04
> 상태: 리뷰 대기

---

## 1. 아키텍처 개요

### Before

```
_run_single()
  └─ run_loop() → result_str
  └─ DelegateResult(output, files_read, files_modified, iterations)
  └─ _format_delegate_output()
       └─ output + [Files touched] + [iterations]
```

### After

```
_run_single()
  ├─ t0 = time.monotonic()                          # 시간 측정 시작
  ├─ run_loop() → result_str
  ├─ duration = time.monotonic() - t0                # 시간 측정 종료
  ├─ activity = _extract_activity_log(ctx.messages)  # 작업 이력 추출
  ├─ DelegateResult(output, files_read, files_modified, iterations,
  │                  duration_secs, activity_log, last_actions)
  ├─ _format_delegate_output()                       # 확장된 포맷
  │    └─ output + [Subagent activity] + [Files touched]
  │       + [Duration] + [iterations]
  │       + (실패 시) [Last actions before failure]
  └─ _persist_delegate_result()                      # 아티팩트 저장
       ├─ save_artifact(content, tags=["delegate"])
       └─ append_progress(summary)
```

## 2. 데이터 구조 변경

### 2.1 DelegateResult 확장

```python
# agent_cli/tools/delegate.py (line 88~96)

@dataclass
class DelegateResult:
    """Structured result from delegate execution."""

    output: str | None = None
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    iterations: int = 0
    duration_secs: float = 0.0                         # NEW
    activity_log: list[str] = field(default_factory=list)  # NEW
    last_actions: list[str] = field(default_factory=list)  # NEW (실패 시만)
```

새 필드 모두 기본값이 있으므로 기존 코드 호환성 유지.

## 3. 신규 함수

### 3.1 `_extract_activity_log(messages, max_entries=20) -> list[str]`

`ctx.messages`에서 assistant의 ReAct action을 순회하며 이터레이션별 요약을 생성한다.

```python
def _extract_activity_log(
    messages: list[dict], max_entries: int = 20
) -> list[str]:
    """Extract per-iteration action summaries from context messages.

    Parses assistant messages for ReAct JSON (action/action_input),
    formats each into a one-line summary.

    Returns list of strings like:
      ["iter 1: read_file auth.py", "iter 2: shell pytest → exit 0"]
    """
    log: list[str] = []
    iter_num = 0

    for msg in messages:
        if msg.get("role") != "assistant":
            continue

        try:
            data = json.loads(msg["content"])
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        if not isinstance(data, dict):
            continue

        action = data.get("action", "")
        if not action:
            continue

        iter_num += 1
        action_input = data.get("action_input", {})
        summary = _summarize_action(action, action_input)
        log.append(f"iter {iter_num}: {summary}")

    if len(log) > max_entries:
        trimmed = log[:max_entries]
        trimmed.append(f"... and {len(log) - max_entries} more")
        return trimmed
    return log
```

**핵심 설계 결정**:
- `_extract_files_touched` (context/manager.py:236)와 동일한 JSON 파싱 패턴 사용
- 이터레이션 번호는 assistant 메시지 중 action이 있는 것만 카운트
- observation(user 메시지)의 결과는 별도 파싱하지 않음 (복잡도 대비 가치 낮음)

### 3.2 `_summarize_action(action, action_input) -> str`

개별 액션을 한 줄 요약으로 변환한다.

```python
def _summarize_action(action: str, action_input: dict) -> str:
    """Format a single action into a one-line summary."""
    if not isinstance(action_input, dict):
        return action

    path = action_input.get("path", "")
    if action == "read_file" and path:
        # Path만 표시 (basename이면 충분)
        return f"read_file {Path(path).name}"
    elif action in ("write_file", "edit_file") and path:
        return f"{action} {Path(path).name}"
    elif action == "shell":
        cmd = action_input.get("command", "")
        return f"shell {cmd[:60]}" if cmd else "shell"
    elif action == "delegate":
        task = action_input.get("task", "")
        return f'delegate "{task[:40]}"' if task else "delegate"
    else:
        return action
```

### 3.3 `_extract_last_actions(messages, n=5) -> list[str]`

실패 시 마지막 N개 액션과 그 observation을 추출한다.

```python
def _extract_last_actions(messages: list[dict], n: int = 5) -> list[str]:
    """Extract last N actions with their observation results.

    Returns list of strings like:
      ["iter 4: shell pytest → ERROR: 3 tests failed",
       "iter 5: edit_file test_auth.py → hash mismatch"]
    """
    # 1. 모든 (action_msg_idx, action_summary) 쌍 수집
    actions: list[tuple[int, str]] = []
    iter_num = 0
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        try:
            data = json.loads(msg["content"])
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        if not isinstance(data, dict) or not data.get("action"):
            continue

        iter_num += 1
        summary = _summarize_action(data["action"], data.get("action_input", {}))
        actions.append((i, iter_num, summary))

    # 2. 마지막 n개 선택
    last_n = actions[-n:]

    # 3. 각 액션의 다음 user 메시지(observation)에서 에러 힌트 추출
    result: list[str] = []
    for msg_idx, it, summary in last_n:
        obs_hint = ""
        # 다음 user 메시지 찾기
        if msg_idx + 1 < len(messages) and messages[msg_idx + 1].get("role") == "user":
            obs = messages[msg_idx + 1]["content"]
            # 에러 키워드가 있으면 첫 줄 추출
            for line in obs.split("\n")[:5]:
                if any(kw in line.upper() for kw in ["ERROR", "FAIL", "EXCEPTION", "TRACEBACK"]):
                    obs_hint = f" → {line.strip()[:80]}"
                    break
        result.append(f"iter {it}: {summary}{obs_hint}")

    return result
```

## 4. 기존 함수 변경

### 4.1 `_run_single` 변경 (line 187~293)

```python
def _run_single(
    task: str,
    # ... 기존 파라미터 동일 ...
) -> ToolResult:
    """Execute a single delegate task."""
    from agent_cli.loop import run_loop

    # ... 기존 검증/에이전트 로딩/컨텍스트 준비 동일 (line 211~256) ...

    # NEW: 시간 측정 시작
    import time
    t0 = time.monotonic()

    result_str = run_loop(
        # ... 기존 파라미터 동일 (line 258~278) ...
    )

    # NEW: 시간 측정 종료
    duration = time.monotonic() - t0

    delegate_result = DelegateResult(output=result_str, duration_secs=duration)

    if context_mode != "inherit":
        files_read, files_modified = ctx._extract_files_touched(ctx.messages)
        delegate_result.files_read = sorted(files_read)
        delegate_result.files_modified = sorted(files_modified)

    # NEW: 작업 이력 추출
    delegate_result.activity_log = _extract_activity_log(ctx.messages)

    # NEW: 실패 시 마지막 액션 추출
    if result_str is None:
        delegate_result.last_actions = _extract_last_actions(ctx.messages)

    formatted = _format_delegate_output(delegate_result)

    # NEW: 디스크 영속화
    _persist_delegate_result(
        formatted=formatted,
        task=task,
        duration=duration,
        iterations=delegate_result.iterations,
        success=result_str is not None,
        scratchpad_dir=scratchpad_dir,
        depth=depth,
    )

    if result_str is not None:
        return ToolResult(True, output=f"STATUS: success\nRESULT:\n{formatted}")
    else:
        return ToolResult(
            False,
            error=f"STATUS: error\nERROR: Subagent did not complete\n{formatted}",
        )
```

**주의**: `iterations` 필드는 현재 `DelegateResult`에 있지만 `_run_single`에서 값을 설정하지 않고 있다.
`run_loop`의 반환값이 단순 문자열이므로 iteration 수를 직접 알 수 없다.
activity_log의 길이로 대체: `delegate_result.iterations = len(delegate_result.activity_log)`
(activity_log에서 `"... and N more"` 항목 제외 후 카운트)

### 4.2 `_format_delegate_output` 변경 (line 131~150)

```python
def _format_delegate_output(result: DelegateResult) -> str:
    """Format DelegateResult into observation string."""
    parts = []

    # 1. Output (기존)
    if result.output:
        parts.append(result.output)
    else:
        parts.append("(subagent returned no result)")

    # 2. Activity log (NEW)
    if result.activity_log:
        parts.append("")
        parts.append("[Subagent activity]")
        for entry in result.activity_log:
            parts.append(f"- {entry}")

    # 3. Last actions on failure (NEW)
    if result.last_actions:
        parts.append("")
        parts.append("[Last actions before failure]")
        for entry in result.last_actions:
            parts.append(f"- {entry}")

    # 4. Files touched (기존)
    if result.files_read or result.files_modified:
        parts.append("")
        parts.append("[Files touched]")
        if result.files_read:
            parts.append(f"- Read: {', '.join(sorted(result.files_read))}")
        if result.files_modified:
            parts.append(f"- Modified: {', '.join(sorted(result.files_modified))}")

    # 5. Duration (NEW) + Iterations (기존)
    footer = []
    if result.duration_secs > 0:
        footer.append(f"[Duration: {result.duration_secs:.1f}s]")
    if result.iterations > 0:
        footer.append(f"[Subagent used {result.iterations} iterations]")
    if footer:
        parts.append("")
        parts.append(" ".join(footer))

    return "\n".join(parts)
```

### 4.3 `_format_parallel_results` 변경 (line 153~184)

각 태스크별 duration 표시를 추가한다. 단, 이 함수는 `ToolResult`를 받으므로 duration 정보를
`ToolResult.output`에서 파싱하거나, `_run_single`이 duration을 포함한 출력을 생성하면 자동 반영된다.

**설계 결정**: `_format_delegate_output`이 이미 `[Duration: ...]`을 포함하므로 `_format_parallel_results`는 수정 불필요.
각 태스크의 `result.output`에 이미 duration이 포함되어 있다.

## 5. 신규 함수: `_persist_delegate_result`

```python
def _persist_delegate_result(
    formatted: str,
    task: str,
    duration: float,
    iterations: int,
    success: bool,
    scratchpad_dir: Path,
    depth: int,
) -> None:
    """Save delegate result as session artifact and update scratchpad progress.

    Uses existing scratchpad infrastructure (save_artifact, append_progress).
    Errors are silently caught to avoid disrupting delegate flow.
    """
    from agent_cli.context.scratchpad import append_progress, save_artifact

    try:
        # 1. Save as artifact
        status = "success" if success else "failed"
        save_artifact(
            turn=0,  # delegate는 turn 체계 밖 — 0으로 마킹
            content=formatted,
            tags=["delegate", f"depth:{depth}", status],
            summary=f"delegate: {task[:60]}",
            base=scratchpad_dir,
        )

        # 2. Update scratchpad progress
        status_str = "completed" if success else "FAILED"
        append_progress(
            turn=0,
            summary=(
                f"delegate {status_str}: {task[:60]} "
                f"({duration:.1f}s, {iterations} iters)"
            ),
            base=scratchpad_dir,
        )
    except Exception:
        pass  # 영속화 실패가 delegate 결과를 방해하면 안 됨
```

**핵심 설계 결정**:
- `turn=0`을 사용: delegate는 부모의 turn 체계 밖에서 실행됨. 아티팩트 파일명은 `turn_0000.md`가 되므로 일반 턴 아티팩트와 충돌하지 않음.
- 에러를 `pass`로 무시: 영속화는 부가 기능이므로 delegate 실행 자체를 방해하면 안 됨.
- `_run_single` 내에서 `scratchpad_dir` 변수는 이미 236행에서 결정되어 있으므로 그대로 전달.

## 6. 출력 포맷 예시

### 6.1 성공 시

```
STATUS: success
RESULT:
Analysis complete. Found 3 bugs in auth module.

[Subagent activity]
- iter 1: read_file auth.py
- iter 2: read_file config.py
- iter 3: shell pytest → ERROR: 3 tests failed
- iter 4: edit_file auth.py
- iter 5: shell pytest

[Files touched]
- Read: auth.py, config.py
- Modified: auth.py

[Duration: 45.2s] [Subagent used 5 iterations]
```

### 6.2 실패 시

```
STATUS: error
ERROR: Subagent did not complete
(subagent returned no result)

[Subagent activity]
- iter 1: read_file auth.py
- iter 2: edit_file auth.py
- iter 3: shell pytest → ERROR: 3 tests failed
- iter 4: edit_file auth.py
- iter 5: edit_file auth.py → hash mismatch

[Last actions before failure]
- iter 3: shell pytest → ERROR: 3 tests failed
- iter 4: edit_file auth.py
- iter 5: edit_file auth.py → hash mismatch

[Files touched]
- Read: auth.py
- Modified: auth.py

[Duration: 62.8s] [Subagent used 5 iterations]
```

### 6.3 병렬 실행 시

```
STATUS: success
RESULT:
[Task 1] Analyze module A
Analysis of module A complete. No issues found.

[Subagent activity]
- iter 1: read_file module_a.py
- iter 2: shell pytest tests/test_a.py

[Files touched]
- Read: module_a.py

[Duration: 12.3s] [Subagent used 2 iterations]

[Task 2] Analyze module B
Found 1 bug in module B.

[Subagent activity]
- iter 1: read_file module_b.py
- iter 2: shell pytest tests/test_b.py → ERROR: 1 test failed
- iter 3: edit_file module_b.py

[Files touched]
- Read: module_b.py
- Modified: module_b.py

[Duration: 28.7s] [Subagent used 3 iterations]

[Parallel execution: 2 tasks, all succeeded]
```

## 7. 파일 변경 목록

### 7.1 수정 파일

| 파일 | 변경 내용 |
|------|----------|
| `agent_cli/tools/delegate.py` | `DelegateResult` 필드 추가, `_run_single` 시간 측정/이력 추출/영속화 추가, `_format_delegate_output` 확장 |

### 7.2 신규 함수 (delegate.py 내)

| 함수 | 위치 | 설명 |
|------|------|------|
| `_extract_activity_log` | DelegateResult 아래 | 메시지에서 이터레이션별 액션 요약 추출 |
| `_summarize_action` | `_extract_activity_log` 아래 | 개별 액션을 한 줄 요약 |
| `_extract_last_actions` | `_summarize_action` 아래 | 마지막 N개 액션 + 에러 힌트 추출 |
| `_persist_delegate_result` | `_extract_last_actions` 아래 | 아티팩트 저장 + 스크래치패드 업데이트 |

### 7.3 신규/삭제 파일

없음. 모든 변경은 `agent_cli/tools/delegate.py` 내에서 이루어짐.

## 8. import 추가

```python
# delegate.py 상단 (line 7~14)
import time       # NEW — duration 측정용
import json       # NEW — activity log 파싱용 (기존 manager.py와 동일 패턴)
```

`time`과 `json`은 표준 라이브러리이므로 의존성 추가 없음.

## 9. iterations 필드 정확도

현재 `run_loop`는 iteration 수를 반환하지 않는다.
activity_log 추출 시 카운트한 이터레이션 수를 `DelegateResult.iterations`에 설정한다:

```python
delegate_result.activity_log = _extract_activity_log(ctx.messages)
# activity_log에서 "... and N more" 항목 제외 후 카운트
real_entries = [e for e in delegate_result.activity_log if not e.startswith("...")]
delegate_result.iterations = len(real_entries)
```

이 방식은 `_extract_files_touched`와 동일하게 메시지 기반으로 추출하므로 정확하다.
