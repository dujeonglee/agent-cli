# Delegate 산출물 개선 — 테스트 계획

> 작성일: 2026-04-04
> 상태: 리뷰 대기

---

## 1. 단위 테스트 (`tests/test_delegate.py` 확장)

### 1.1 Activity Log 추출

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DO-01 | `test_extract_activity_log_basic` | assistant 메시지에서 action 추출, `"iter 1: read_file auth.py"` 형태 반환 |
| DO-02 | `test_extract_activity_log_empty_messages` | 빈 메시지 리스트 → 빈 리스트 반환 |
| DO-03 | `test_extract_activity_log_no_actions` | action 없는 assistant 메시지 → 빈 리스트 |
| DO-04 | `test_extract_activity_log_max_entries` | 25개 액션 메시지, max_entries=20 → 20개 + `"... and 5 more"` |
| DO-05 | `test_extract_activity_log_mixed_roles` | user/assistant 혼합 → assistant의 action만 추출 |
| DO-06 | `test_extract_activity_log_malformed_json` | 잘못된 JSON assistant 메시지 → 건너뛰기 |

### 1.2 Action 요약

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DO-07 | `test_summarize_action_read_file` | `read_file` + path → `"read_file auth.py"` (basename만) |
| DO-08 | `test_summarize_action_write_file` | `write_file` + path → `"write_file config.py"` |
| DO-09 | `test_summarize_action_edit_file` | `edit_file` + path → `"edit_file main.py"` |
| DO-10 | `test_summarize_action_shell` | `shell` + command → `"shell pytest tests/"` (60자 제한) |
| DO-11 | `test_summarize_action_delegate` | `delegate` + task → `'delegate "Fix the bug"'` (40자 제한) |
| DO-12 | `test_summarize_action_unknown` | 알 수 없는 액션 → 액션 이름만 반환 |
| DO-13 | `test_summarize_action_no_dict_input` | action_input이 dict가 아닌 경우 → 액션 이름만 반환 |

### 1.3 에러 상세화

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DO-14 | `test_extract_last_actions_basic` | 10개 액션에서 마지막 5개 추출 |
| DO-15 | `test_extract_last_actions_with_error_hint` | 다음 user 메시지에 "ERROR" 포함 → `"→ ERROR: ..."` 힌트 추가 |
| DO-16 | `test_extract_last_actions_fewer_than_n` | 3개 액션, n=5 → 3개 모두 반환 |
| DO-17 | `test_extract_last_actions_no_observation` | 마지막 assistant 메시지 뒤에 user 메시지 없음 → 힌트 없이 반환 |
| DO-18 | `test_extract_last_actions_empty` | 빈 메시지 → 빈 리스트 |

### 1.4 소요 시간

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DO-19 | `test_delegate_result_duration_field` | `DelegateResult(duration_secs=45.2)` 생성 가능, 기본값 0.0 |
| DO-20 | `test_run_single_measures_duration` | `_run_single` 호출 후 결과에 `[Duration:` 문자열 포함 |
| DO-21 | `test_duration_zero_not_shown` | `duration_secs=0.0` → `[Duration:]` 출력 안 함 |

### 1.5 출력 포맷

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DO-22 | `test_format_output_with_activity_log` | activity_log 있으면 `[Subagent activity]` 섹션 포함 |
| DO-23 | `test_format_output_without_activity_log` | activity_log 비어있으면 해당 섹션 생략 |
| DO-24 | `test_format_output_with_last_actions` | last_actions 있으면 `[Last actions before failure]` 섹션 포함 |
| DO-25 | `test_format_output_success_no_last_actions` | 성공 시 last_actions 섹션 없음 |
| DO-26 | `test_format_output_duration_and_iterations` | duration + iterations → `[Duration: 45.2s] [Subagent used 5 iterations]` |
| DO-27 | `test_format_output_backward_compatible` | 기존 필드만 설정 시 기존 포맷과 동일 |

### 1.6 디스크 영속화

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DO-28 | `test_persist_delegate_result_saves_artifact` | `save_artifact` 호출되며 tags에 "delegate" 포함 |
| DO-29 | `test_persist_delegate_result_appends_progress` | `append_progress` 호출되며 summary에 task/duration/iters 포함 |
| DO-30 | `test_persist_delegate_result_failure_tagged` | 실패 시 tags에 "failed" 포함 |
| DO-31 | `test_persist_delegate_result_error_ignored` | `save_artifact`가 예외 발생해도 `_persist_delegate_result`는 예외 없이 반환 |
| DO-32 | `test_run_single_calls_persist` | `_run_single` 호출 후 아티팩트 파일이 디스크에 존재 |

### 1.7 iterations 카운트

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DO-33 | `test_iterations_from_activity_log` | activity_log 5개 → iterations=5 |
| DO-34 | `test_iterations_excludes_ellipsis` | activity_log에 `"... and 3 more"` 포함 → 해당 항목 제외 후 카운트 |

## 2. 기존 테스트 회귀 확인

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DO-35 | `test_existing_delegate_tests_pass` | 기존 `tests/test_delegate.py`의 모든 테스트 통과 |
| DO-36 | `test_delegate_result_default_fields` | `DelegateResult()` 기본 생성 시 새 필드 기본값 정상 |

## 3. 통합 테스트 (`tests/test_delegate.py` 확장)

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DO-37 | `test_run_single_success_full_output` | 성공 시 전체 출력에 activity + files + duration + iterations 모두 포함 |
| DO-38 | `test_run_single_failure_full_output` | 실패 시 전체 출력에 activity + last_actions + duration 포함 |
| DO-39 | `test_parallel_each_task_has_duration` | 병렬 실행 결과의 각 태스크 출력에 `[Duration:]` 포함 |
| DO-40 | `test_run_single_persist_and_scratchpad` | `_run_single` 후 아티팩트 파일 존재 + scratchpad에 delegate progress 기록 |

## 4. 테스트 우선순위

### P0 (필수, 구현과 동시)

DO-01 ~ DO-06 (activity log 추출)
DO-07 ~ DO-13 (action 요약)
DO-19 ~ DO-21 (duration)
DO-22 ~ DO-27 (출력 포맷)
DO-35 ~ DO-36 (회귀)

### P1 (중요, 구현 직후)

DO-14 ~ DO-18 (에러 상세화)
DO-28 ~ DO-34 (영속화, iterations)
DO-37 ~ DO-38 (통합)

### P2 (후속)

DO-39 ~ DO-40 (병렬 + 영속화 통합)

## 5. 테스트 헬퍼

### 5.1 ReAct 메시지 생성 헬퍼

테스트 전반에서 사용할 assistant 메시지 생성 헬퍼:

```python
def _make_action_msg(action: str, action_input: dict) -> dict:
    """Create a mock assistant message with ReAct JSON."""
    return {
        "role": "assistant",
        "content": json.dumps({
            "thought": "test thought",
            "action": action,
            "action_input": action_input,
        }),
    }

def _make_obs_msg(content: str) -> dict:
    """Create a mock user/observation message."""
    return {"role": "user", "content": content}
```

### 5.2 Mocking 전략

- `_run_single` 테스트: `run_loop`를 `unittest.mock.patch`로 모킹
- `_persist_delegate_result` 테스트: `save_artifact`, `append_progress`를 모킹하거나 `tmp_path` 사용
- duration 테스트: `time.monotonic`를 모킹하여 정확한 값 검증
