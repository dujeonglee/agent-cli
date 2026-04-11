# 코드베이스 일관성 감사 리포트

> Date: 2026-04-12
> 범위: 반환 타입, 파라미터 네이밍, 에러 처리, import 패턴, 매직 넘버, 데드 코드

## HIGH 우선순위

### 1. ToolResult inline import 14회 (loop.py)

loop.py에서 `from agent_cli.tools.result import ToolResult`가 함수 내부에 14번 반복.
다른 파일은 모두 top-level import.

**원인**: run_loop ToolResult 통일 작업에서 각 반환부에 inline 추가.
**수정**: top-level import로 통합.

### 2. _handle_text_path 반환 타입 어노��이션 불일치

```python
def _handle_text_path(self, llm_text: str) -> str | None:  # ← 틀림
    ...
    return ToolResult(True, output=answer)  # 실제는 ToolResult
```

**수정**: 타입 어노테이션 제거 또는 수정.

### 3. YAML 스킬 메타데이터 키: `max-turns` vs `max-turns`

skills/loader.py:87에서 `meta.get("max-turns")` 사용.
다른 모든 코드는 `max_turns`. 다른 YAML 키는 kebab-case (`allowed-tools`, `argument-hint`).

**수정**: `max-turns` → `max-turns`로 통일. 기존 스킬 파일 호환 위해 fallback 유지.

## MEDIUM 우선순위

### 4. Shell timeout 하드코딩 (30초)

constants.py에 `SHELL_COMMAND_TIMEOUT = 30` 정의되어 있지만 미사용.
main.py:38, executor.py:24, compat.py:187,237에서 `timeout=30` 하드코딩.

**수정**: `SHELL_COMMAND_TIMEOUT` 상수 사용.

### 5. DELEGATE_DEFAULT_TIMEOUT 미사용

constants.py:7에 `DELEGATE_DEFAULT_TIMEOUT = 300` 정의되어 있지만 어디서도 import 안 됨.
loop.py, main.py, delegate.py에서 `300` 하드코딩.

**수정**: 상수 사용 또는 삭제.

### 6. inline import로 숨겨진 순환 의존성 (loop.py)

loop.py:789에서 `from agent_cli.skills import load_skills`를 inline import.
skills/executor.py가 loop.py의 `run_loop`을 import하므로 순환 의존.
inline import로 우회 중이지만 명시적으로 문서화 필요.

## LOW 우선순위

### 7. fifo_size 이름 모호

`fifo_size`가 "메시지 개수"를 의미하지만 이름만으로는 불명확.
`max_context_messages` 또는 `keep_messages`가 더 명확.

### 8. 에러 메시지 포맷 불일치

- 일부: `"Interrupted by user"` (간단)
- 일부: `"Delegation rejected: empty tasks"` (접두어 + 설명)
- 일부: `"STATUS: error\nERROR: ..."` (구조화)

표준 포맷 결정 필요.

### 9. tool_ 접두어 네이밍 불일치

도구 함수: `tool_read_file`, `tool_shell`, `tool_delegate` (일관)
유틸 함수: `fuzzy_verify_ref`, `compute_line_hash`, `format_hashlines` (접두어 없음)

유틸은 외부 호출 안 되니 `_` ���두어로 private 처리 가능.

## 해결 완료

| 항목 | 상태 |
|------|------|
| run_loop / execute_skill 반환 타입 통일 | ✅ ToolResult |
| include_delegate flag 제거 | ✅ TOOLS dict 통합 |
| inherit 모드 잔재 | ✅ 전부 제거 |
| native tool calling 코드 | ✅ 전부 제거 |
| scratchpad / compression 잔재 | ✅ 전부 제거 |
