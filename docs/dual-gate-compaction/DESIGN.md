# 이중 게이트 컴팩션 — 설계 문서

> 작성일: 2026-04-04
> 상태: 리뷰 대기

---

## 1. 변경 개요

`ContextManager.add()` 메서드의 압축 트리거 조건을 단일 게이트에서 이중 게이트로 변경한다.

### Before (단일 게이트)

```python
# agent_cli/context/manager.py:84
if self._total_chars() > self.max_context_chars:
    self._compress()
```

문자 수 초과만으로 압축 트리거. 메시지가 2개뿐이어도 하나가 거대하면 압축 시도.

### After (이중 게이트)

```python
# agent_cli/context/manager.py:84
if (
    len(self.messages) > self.keep_recent * 2
    and self._total_chars() > self.max_context_chars
):
    self._compress()
```

메시지 수가 보존 대상보다 많고 **동시에** 문자 수도 초과할 때만 압축.

## 2. 파일 변경 목록

### 2.1 수정 파일

| 파일 | 라인 | 변경 내용 |
|------|------|----------|
| `agent_cli/context/manager.py` | 84 | `add()` 내 압축 조건에 메시지 수 게이트 추가 |

### 2.2 신규/삭제 파일

없음.

## 3. 상세 변경

### 3.1 `agent_cli/context/manager.py` — `add()` 메서드

**현재 코드 (라인 80-85):**

```python
def add(self, role: str, content: str) -> None:
    """Add a message and trigger compression if needed."""
    self.messages.append({"role": role, "content": content})
    self._msg_chars += len(content)
    if self._total_chars() > self.max_context_chars:
        self._compress()
```

**변경 후:**

```python
def add(self, role: str, content: str) -> None:
    """Add a message and trigger compression if needed.

    Dual-gate compaction: compression triggers only when BOTH conditions met:
    1. Message count exceeds compactable threshold (keep_recent * 2)
    2. Total character count exceeds max_context_chars
    """
    self.messages.append({"role": role, "content": content})
    self._msg_chars += len(content)
    if (
        len(self.messages) > self.keep_recent * 2
        and self._total_chars() > self.max_context_chars
    ):
        self._compress()
```

### 3.2 변경하지 않는 코드

다음 코드는 의도적으로 **변경하지 않는다**:

1. **`_compress()` 내부의 가드** (라인 150-151):
   ```python
   keep = self.keep_recent * 2
   if len(self.messages) <= keep:
       return
   ```
   `add()`의 게이트 1과 동일한 조건이지만, `force_compress()` 경로를 위한 안전장치로 유지.

2. **`force_compress()`** (라인 135-138):
   ```python
   def force_compress(self, user_instruction: str = "") -> None:
       if len(self.messages) > self.keep_recent * 2:
           self._compress(user_instruction=user_instruction)
   ```
   사용자 명시적 요청이므로 문자 수 게이트 적용하지 않음.

3. **`__init__` 시그니처**: 새 파라미터 불필요. 게이트 1 임계값은 기존 `keep_recent`에서 파생.

4. **`_total_chars()`**: 로직 변경 없음.

5. **압축 실패 추적**: `max_context_chars` 동적 조정 로직 그대로 유지. 게이트 2에 자연스럽게 반영.

## 4. 동작 시나리오

### 4.1 정상 대화 — 점진적 축적

```
turn 1: 메시지 2개, 800자  → 게이트1 ✗ (2 ≤ 8), 게이트2 ✗ → 압축 안 함
turn 2: 메시지 4개, 1600자 → 게이트1 ✗ (4 ≤ 8), 게이트2 ✗ → 압축 안 함
turn 5: 메시지 10개, 4000자 → 게이트1 ✓ (10 > 8), 게이트2 ✓ (> max) → 압축!
```

### 4.2 거대 단일 메시지

```
turn 1: 메시지 2개, 50000자 (대용량 파일 읽기)
  → 게이트1 ✗ (2 ≤ 8), 게이트2 ✓ → 압축 안 함 (개선!)
  기존: 즉시 압축하여 방금 받은 파일 내용이 사라짐
```

### 4.3 짧은 메시지 대량 축적

```
turn 50: 메시지 100개, 각 10자 = 1000자
  → 게이트1 ✓ (100 > 8), 게이트2 ✗ (1000 < max) → 압축 안 함
  메시지가 많지만 총 크기가 작으므로 전체 히스토리 유지
```

### 4.4 force_compress (사용자 /compact)

```
사용자가 /compact 실행 → force_compress() 호출
  → 메시지 수 > keep_recent * 2 이면 무조건 압축
  → 이중 게이트 무관
```

## 5. 참조 구현 비교

### claw-code

```
compactable.len() > preserve_recent_messages AND estimated_tokens >= max_estimated_tokens
```

### agent-cli (본 설계)

```
len(self.messages) > keep_recent * 2 AND _total_chars() > max_context_chars
```

의미적으로 동일:
- `compactable.len()` ≈ `len(self.messages)` (전체 메시지에서 보존 대상 빼기 vs 전체 메시지 수 비교)
- `preserve_recent_messages` ≈ `keep_recent * 2`
- `estimated_tokens` ≈ `_total_chars() / CHARS_PER_TOKEN` (4chars/token 휴리스틱)
- `max_estimated_tokens` ≈ `max_context_chars / CHARS_PER_TOKEN`

미세 차이: claw-code는 `compactable` (보존 대상 제외한 나머지)의 길이를 보지만,
agent-cli는 전체 메시지 수를 보고 `_compress()` 내부에서 보존 대상을 분리한다.
결과적 동작은 동일하다 — `len(messages) > keep * 2` 이면 `messages[:-keep]`이 비어있지 않다.
