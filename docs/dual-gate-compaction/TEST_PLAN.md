# 이중 게이트 컴팩션 — 테스트 계획

> 작성일: 2026-04-04
> 상태: 리뷰 대기

---

## 1. 단위 테스트 (`tests/test_context_manager.py` 확장)

### 1.1 이중 게이트 트리거 조건

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DG-01 | `test_no_compress_when_only_chars_exceed` | 메시지 수 ≤ `keep_recent * 2`이고 문자 수 > `max_context_chars`일 때 압축이 발생하지 않음 (게이트2만 충족) |
| DG-02 | `test_no_compress_when_only_message_count_exceeds` | 메시지 수 > `keep_recent * 2`이고 문자 수 ≤ `max_context_chars`일 때 압축이 발생하지 않음 (게이트1만 충족) |
| DG-03 | `test_compress_when_both_gates_met` | 메시지 수 > `keep_recent * 2` **이고** 문자 수 > `max_context_chars`일 때 압축 실행 |
| DG-04 | `test_no_compress_when_neither_gate_met` | 양쪽 모두 미충족 시 압축 없음 |

### 1.2 경계값 테스트

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DG-05 | `test_message_count_at_exact_threshold` | `len(messages) == keep_recent * 2` (경계값)일 때 압축 안 함 (> 이므로 초과해야 함) |
| DG-06 | `test_chars_at_exact_threshold` | `_total_chars() == max_context_chars` (경계값)일 때 압축 안 함 (> 이므로 초과해야 함) |
| DG-07 | `test_message_count_one_over_threshold` | `len(messages) == keep_recent * 2 + 1`이고 문자 초과 시 압축 실행 |

### 1.3 force_compress 독립성

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DG-08 | `test_force_compress_ignores_char_gate` | 문자 수 < `max_context_chars`이더라도 `force_compress()` 호출 시 메시지 수 조건만으로 압축 실행 |
| DG-09 | `test_force_compress_still_checks_message_count` | 메시지 수 ≤ `keep_recent * 2`이면 `force_compress()`도 압축하지 않음 (기존 동작 유지) |

### 1.4 실패 추적과의 상호작용

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DG-10 | `test_failure_raised_threshold_affects_gate2` | 압축 실패로 `max_context_chars`가 상승한 후, 게이트 2의 임계값이 상승한 것을 반영하여 다음 트리거 시점이 지연됨 |
| DG-11 | `test_success_resets_threshold_restores_gate2` | 압축 성공 후 `max_context_chars`가 원래 값으로 복원되어 게이트 2 정상 동작 |

### 1.5 기존 테스트 회귀 확인

| ID | 테스트 | 검증 항목 |
|----|--------|----------|
| DG-12 | `test_compression_triggered` (기존) | 메시지 10쌍 + 각 500자 → 양쪽 게이트 모두 충족하여 기존과 동일하게 압축됨 |
| DG-13 | `test_incremental_update` (기존) | 기존 summary 있는 상태에서 10쌍 추가 → 양쪽 게이트 충족하여 incremental 압축 동작 유지 |

## 2. 테스트 구현 가이드

### 2.1 DG-01 구현 예시

```python
def test_no_compress_when_only_chars_exceed(self, mock_provider, caps, tmp_path):
    """Gate 2 only (chars exceed) should NOT trigger compression."""
    ctx = ContextManager(
        mock_provider, "test-model", caps, keep_recent=4, scratchpad_dir=tmp_path
    )
    # keep_recent * 2 = 8. Add only 2 messages but with huge content.
    ctx.add("user", "x" * (ctx.max_context_chars + 1))
    ctx.add("assistant", "short reply")

    # Only 2 messages <= 8 threshold, so no compression despite char overflow
    assert not mock_provider.call.called
    assert ctx._summary is None
    assert len(ctx.messages) == 2
```

### 2.2 DG-02 구현 예시

```python
def test_no_compress_when_only_message_count_exceeds(self, mock_provider, caps, tmp_path):
    """Gate 1 only (message count exceed) should NOT trigger compression."""
    ctx = ContextManager(
        mock_provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
    )
    # keep_recent * 2 = 2. Add 4 messages but each very short.
    for _ in range(2):
        ctx.add("user", "hi")
        ctx.add("assistant", "ok")

    # 4 messages > 2 threshold, but total chars << max_context_chars
    assert not mock_provider.call.called
    assert ctx._summary is None
    assert len(ctx.messages) == 4
```

### 2.3 DG-03 구현 예시

```python
def test_compress_when_both_gates_met(self, mock_provider, caps, tmp_path):
    """Both gates met should trigger compression."""
    ctx = ContextManager(
        mock_provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
    )
    # keep_recent * 2 = 2. Add many messages with large content.
    for _ in range(10):
        ctx.add("user", "x" * 500)
        ctx.add("assistant", "y" * 500)

    assert mock_provider.call.called
    assert ctx._summary is not None
```

## 3. 테스트 우선순위

### P0 (필수, 구현과 동시)

DG-01 ~ DG-04 (이중 게이트 핵심), DG-05 ~ DG-07 (경계값), DG-12 ~ DG-13 (회귀)

### P1 (중요, 구현 직후)

DG-08 ~ DG-09 (force_compress 독립성), DG-10 ~ DG-11 (실패 추적 상호작용)

## 4. 기존 테스트 영향 분석

| 기존 테스트 | 영향 | 이유 |
|------------|------|------|
| `test_add_and_get` | 없음 | 메시지 2개, 압축 미트리거 (기존에도 동일) |
| `test_summary_prepended` | 없음 | summary를 직접 설정, add()로 트리거하지 않음 |
| `test_compression_triggered` | 없음 | 10쌍 (20메시지) + 각 500자 → 양쪽 게이트 충족 |
| `test_incremental_update` | 없음 | 동일 조건으로 양쪽 게이트 충족 |
| `test_force_compress` | 없음 | force_compress()는 이중 게이트 무관 |
| `test_failure_increments_counter` | 없음 | 10쌍 + 각 200자 → `keep_recent=1`이므로 양쪽 충족 |
| `test_threshold_capped_at_2x` | 없음 | 50쌍 + 각 200자 → 양쪽 충족 |
| `test_success_resets_counter` | 없음 | messages 직접 추가 후 `_compress()` 직접 호출 |
| `test_alerts_user_after_max_failures` | 없음 | messages 직접 추가 후 `_compress()` 직접 호출 |

**결론: 기존 테스트에 대한 회귀 영향 없음.** 모든 기존 압축 테스트는 `keep_recent=1` (게이트1 임계값=2)에 10쌍 이상의 메시지를 추가하므로 게이트 1을 자연스럽게 통과한다.
