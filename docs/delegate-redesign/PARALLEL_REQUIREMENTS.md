# Parallel Delegate — 요구사항 문서

> 작성일: 2026-04-04
> 상태: 리뷰 대기
> 선행: delegate-redesign (in-process run_loop 전환 완료)

---

## 1. 배경

delegate가 in-process run_loop으로 전환되었으므로, threading 기반 병렬 실행이 가능해졌다.
vLLM, Anthropic API, OpenAI API는 동시 요청을 병렬 처리하므로 실질적 throughput 향상이 가능하다.
Ollama는 단일 GPU 큐잉으로 병렬 이점이 없지만, 동작에 문제는 없다.

## 2. 기능 요구사항

### 2.1 API — tasks 배열 일원화

`task` (string) 파라미터를 제거하고 `tasks` (array)로 일원화:

```json
// 단일 작업
{"tasks": [{"task": "Analyze module A", "context": "fork"}]}

// 복수 작업 (병렬)
{"tasks": [
    {"task": "Analyze module A", "context": "fork"},
    {"task": "Analyze module B", "context": "fork", "tools": ["read_file", "shell"]}
]}
```

- `tasks` 길이 1: 동기 실행
- `tasks` 길이 2+: 병렬 실행 (threading)
- 빈 배열: 에러

### 2.2 tasks 배열 항목 구조

각 항목은 동일한 필드를 지원:

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `task` | string | O | 작업 설명 |
| `context` | string | X | none (기본) / fork / inherit |
| `tools` | array | X | 허용 도구 (미지정 시 기본 세트) |

### 2.3 컨텍스트 모드 제한

- `tasks` 길이 1: `none`, `fork`, `inherit` 모두 허용
- `tasks` 길이 2+: `none`, `fork`만 허용
  - `inherit`가 하나라도 포함되면 전체 거부
  - 사유: 같은 ctx에 여러 스레드가 동시 쓰기 → race condition

### 2.4 실행 모델

- `tasks` 길이 1: 동기 실행 (현재 단일 delegate와 동일)
- `tasks` 길이 2+: `threading.Thread`로 각 task를 동시 실행
- 모든 스레드 완료 후 결과를 수집하여 단일 observation으로 반환
- 타임아웃: 전체 병렬 실행에 대한 단일 타임아웃 (기본 300초)

### 2.5 렌더링

- 단일 task: 기존 동작 유지 (suppress_output 설정에 따름)
- 복수 task (병렬): 항상 `suppress_output=True`로 실행
  - 개별 서브에이전트의 진행 표시 없음 (출력 뒤섞임 방지)
  - 결과는 모아서 한 번에 반환

### 2.6 결과 포맷

단일 task: 기존 포맷 유지 (STATUS/RESULT/Files touched)

복수 task:
```
STATUS: success
RESULT:
[Task 1] Analyze module A
Done. Found 3 issues.

[Files touched]
- Read: module_a.py
- Modified: (none)

[Task 2] Analyze module B
Done. Found 1 issue.

[Files touched]
- Read: module_b.py

[Parallel execution: 2 tasks, all succeeded]
```

실패한 task가 있으면:
```
[Parallel execution: 2 tasks, 1 succeeded, 1 failed]
```

### 2.7 Thread-safety

- `run_loop`의 signal handler 설치를 메인 스레드에서만 수행 (이미 구현됨)
- Provider는 stateless HTTP 클라이언트이므로 thread-safe
- 각 스레드가 독립된 ContextManager를 사용 (none/fork)

## 3. 비고

- Ollama는 단일 GPU 큐잉으로 병렬 이점 없으나, threading 오버헤드가 무시 가능하므로 별도 fallback 불필요
