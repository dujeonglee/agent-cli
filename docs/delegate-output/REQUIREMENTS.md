# Delegate 산출물 개선 — 요구사항 문서

> 작성일: 2026-04-04
> 상태: 리뷰 대기

---

## 1. 배경

현재 delegate 결과(`_format_delegate_output`)는 최종 output, files touched, iteration 수만 반환한다.
이로 인해:
- 서브에이전트가 **무엇을 했는지** (어떤 파일을 읽고, 어떤 명령을 실행했는지) 알 수 없음
- 실패 시 **마지막에 무엇이 잘못됐는지** 디버깅 정보가 없음
- **얼마나 걸렸는지** 시간 정보가 없음
- 컴팩션 후 delegate 결과가 **유실**됨 (복구 불가)

## 2. 목표

delegate 산출물을 4가지 측면에서 개선하여 가시성과 디버깅 능력을 강화한다:

1. **작업 이력 (Activity Log)**: 서브에이전트의 각 이터레이션별 행동 요약
2. **에러 상세화 (Error Detail)**: 실패 시 마지막 액션들 포함
3. **소요 시간 (Duration)**: 실행 시간 측정 및 출력
4. **디스크 영속화 (Persistence)**: 기존 아티팩트 시스템을 활용한 결과 저장

## 3. 기능 요구사항

### 3.1 작업 이력 (Activity Log)

**목적**: 서브에이전트가 각 이터레이션에서 수행한 행동을 요약하여 부모에게 전달.

**요구사항**:
- `ctx.messages`에서 `role="assistant"` 메시지의 `action` 필드를 추출
- 이터레이션 번호와 함께 액션 + 주요 인자를 한 줄로 요약
- 포맷: `"iter 1: read_file auth.py (245 lines)"`, `"iter 3: shell pytest → exit 0"`
- `_format_delegate_output`에 `[Subagent activity]` 섹션으로 추가
- 액션이 너무 많으면 최대 N개까지만 표시 (기본 20)

**추출 대상 액션별 요약 형태**:

| 액션 | 요약 형태 |
|------|----------|
| `read_file` | `read_file {path} ({lines} lines)` |
| `write_file` | `write_file {path}` |
| `edit_file` | `edit_file {path}` |
| `shell` | `shell {command[:60]} → exit {code}` |
| `delegate` | `delegate "{task[:40]}"` |
| 기타 | `{action}` |

**참고 코드**: `ContextManager._extract_files_touched` (line 236~263) — 메시지에서 action/action_input 파싱하는 패턴 동일.

### 3.2 에러 상세화 (Error Detail)

**목적**: 실패 시 마지막 몇 개 액션을 포함하여 디버깅을 용이하게 함.

**요구사항**:
- `_run_single`에서 `result_str is None` (실패) 시 활성화
- `ctx.messages`에서 마지막 N개 assistant 액션 추출 (기본 5)
- 에러 결과에 `[Last actions before failure]` 섹션 추가
- 포맷: 3.1의 activity log 요약과 동일한 형태 사용
- observation (user 메시지) 중 에러 내용이 있으면 함께 포함
  - `"→ ERROR: ..."` 또는 `"→ exit 1"` 형태

### 3.3 소요 시간 (Duration)

**목적**: delegate 실행에 걸린 시간을 측정하여 성능 파악 및 병목 진단.

**요구사항**:
- `_run_single` 시작/끝에 `time.monotonic()` 타이머 배치
- `DelegateResult`에 `duration_secs: float` 필드 추가 (기본값 0.0)
- `_format_delegate_output`에서 `[Duration: {secs:.1f}s]` 출력
- 병렬 실행(`_run_parallel`) 시에도 각 태스크별 duration 측정
- `_format_parallel_results`에서 각 태스크별 duration 표시

### 3.4 디스크 영속화 (Persistence)

**목적**: delegate 결과를 세션 아티팩트로 저장하여 컴팩션 후에도 복구 가능하게 함.

**요구사항**:
- `_run_single` 완료 후 결과를 `save_artifact`로 저장
  - tags: `["delegate", "depth:{depth}"]`
  - summary: `"delegate: {task[:60]}"`
  - content: `_format_delegate_output`의 전체 출력
- scratchpad에 progress 항목 추가 (`append_progress`)
  - `"delegate completed: {task[:60]} ({duration:.1f}s, {iterations} iters)"`
- 실패 시에도 저장 (에러 상세 포함)
- `scratchpad_dir`은 `_resolve_scratchpad_dir`에서 이미 결정됨 — 그대로 사용
- 병렬 실행 시 각 태스크별 개별 아티팩트 + 종합 결과 아티팩트

**참고 코드**: `scratchpad.save_artifact` (line 262~301), `scratchpad.append_progress` (line 187~223).

## 4. 비기능 요구사항

### 4.1 성능

- activity log 추출은 O(N) (N = 메시지 수). LLM 호출 대비 무시 가능.
- `time.monotonic()`는 syscall 하나. 오버헤드 없음.
- 아티팩트 저장은 디스크 I/O 1회. 비동기 불필요.

### 4.2 호환성

- 기존 `_format_delegate_output` 출력의 상위 호환. 기존 섹션 유지, 새 섹션 추가.
- `DelegateResult`에 새 필드 추가는 기본값이 있으므로 기존 코드 호환.
- 기존 테스트(`test_delegate.py`)가 깨지지 않아야 함.

### 4.3 출력 크기 제한

- activity log는 최대 20개 항목으로 제한 (초과 시 `"... and N more"`)
- 에러 상세의 마지막 액션은 최대 5개
- 부모 컨텍스트 윈도우 압박을 최소화

## 5. 범위 외

- LLM 기반 activity 요약 (규칙 기반만)
- 아티팩트의 자동 컨텍스트 주입 (현재 비활성 상태 — TODO 주석 참고)
- 병렬 실행의 실시간 진행 상황 스트리밍
