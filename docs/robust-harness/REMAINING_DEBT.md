# Robust Harness — Remaining Technical Debt

이 문서는 Step 4a 시점에 *알면서* 남긴 부채를 명시 기록합니다. 무지로 인한 누적
방지가 목적 — "이건 부채인 줄 알고 있고, 이런 이유로 지금은 청산 안 한다"를
공개적으로 둠으로써 미래 결정자가 빠르게 판단할 수 있게 합니다.

---

## 1. `_dispatch_tool_with_hooks`는 SRP 위반 — **청산됨 (2026-05-03)**

`AgentLoop._dispatch_tool_with_hooks`는 한때 6가지 책임을 한 메서드 본문에 가졌음:

1. PreToolUse hooks (Python runner + shell)
2. delegate 특수 분기 dispatch
3. `execute_tool` 호출 (일반 도구)
4. observation 크기 가드 (shell 큰 출력 → artifact)
5. read_file 아티팩트 mtime 갱신
6. `recent_tool_history` 추적 (B1 action-loop 감지용)

**진행 이력**:
- 2026-04-XX: free function `_dispatch_tool_with_hooks` 첫 도입 (이전 이름
  `_execute_single_tool`)
- 2026-05-02: free function → `AgentLoop` 메서드 전환 (24개 파라미터 중 22개가
  `self.X`였음). delegate_* 접두사 정리, 시그니처 `(self, tool_name, tool_input)`로
  축약.
- 2026-05-03: 6 책임을 1:1 매핑으로 7개 helper로 분해 (책임 4를 `_save_shell_artifact_if_oversized`
  + `_touch_artifact_on_read` 두 개로 정직하게 쪼갬). orchestrator 본문은 5단계
  recipe.

**현재 상태**: 청산. 각 helper는 단일 책임 + 자체 전제조건 검사 (early return).
Orchestrator는 ~10줄 glue. 미래 변경 시 영향 범위가 한 helper 내로 국한됨.

---

## 2. `execute_tool`의 boundary 방어 — **청산됨 (2026-05-03)**

`agent_cli/tools/__init__.py:execute_tool`은 `Unknown tool: ...` 방어 체크를 갖고 있었음.
Step 4a에서 recovery 레이어로 검증을 옮긴 후, 이 체크는 실질적으로 테스트 케이스에
의해서만 도달됨 — *테스트가 디자인 결정을 핀으로 박은 역방향*이었음.

**청산 (2026-05-03)**:
- 방어 4줄 제거 → 호출자 계약: "tool_name은 반드시 TOOLS에 있어야 함" (recovery
  레이어가 single source of truth). 잘못된 이름이면 KeyError로 즉시 실패.
- `execute_tool` → `_execute_tool` rename + `__all__`에서 제거 → **internal API임을
  명시**. 외부 호출자 0임을 확인 후 진행.
- defense를 직접 검증하던 테스트 3개 삭제 (`test_execute_tool_unknown_returns_error`,
  `TestExecuteTool::test_unknown_tool`, `test_error_message_includes_virtual_tools`).
  나머지 테스트는 `from agent_cli.tools import _execute_tool as execute_tool`
  로컬 alias로 적응.

부채 #3과 함께 청산됐음.

---

## 3. `execute_tool` vs `_dispatch_tool_with_hooks` 이름·책임 모호 — **청산됨 (2026-05-03)**

두 함수가 *유사한 이름, 다른 책임*이었음:
- `execute_tool` — dispatch primitive (`TOOLS.get` + 호출)
- `_dispatch_tool_with_hooks` — orchestration wrapper (hooks + dispatch + post-processing)

**청산 (2026-05-03)**:
- `execute_tool` → `_execute_tool` (underscore prefix로 internal 표시)
- 이름 계층이 명확해짐:
  - `AgentLoop._dispatch_tool_with_hooks` — public-facing orchestrator (loop entry)
  - `AgentLoop._invoke_regular` — orchestrator의 단일 책임 helper (책임 3)
  - `_execute_tool` — leaf primitive (internal)

부채 #2와 함께 청산됐음.

---

## 4. `constants.py`가 `recovery`를 의존 → import cycle — **청산됨 (2026-05-03)**

`constants.py` 상단에서 `from agent_cli.recovery.intervention import Intervention`
+ `recovery.primitives` 를 import 했었음. 이는 `format_no_json_retry`/`format_no_action_retry`/
`format_action_loop_intervention`이 historical하게 constants에 자리잡은 결과 — 이 함수들은
*상수가 아니라* primitive 합성 factory임. 레이어 역전 (`constants` 저층이 `recovery` 고층을
의존). Step 4a에서 `detectors.py`가 `tools.registry`를 import한 순간 실제 cycle 발생,
lazy import으로 임시 우회 (cycle 우회일 뿐, 레이어 위반은 그대로였음).

**청산 (2026-05-03)**:
- 3개 factory 함수를 `agent_cli/recovery/builders.py` 신규 파일로 이동
- `constants.py`에서 `recovery.intervention` / `recovery.primitives` import 제거 →
  `constants` 모듈은 외부 의존 0인 순수 저층 모듈로 환원 (43줄, ~95→43)
- `RETRY_HINT_NO_JSON`/`RETRY_HINT_NO_ACTION` 정적 메시지는 constants에 잔존 (정당한 상수,
  builders.py가 import해서 fallback에 사용)
- `loop.py` import 분할: 진짜 상수는 `constants`에서, factory 3개는 `recovery.builders`에서
- `tests/test_retry_builders.py` import 갱신 (모듈 docstring + factory 위치)
- `recovery/detectors.py`의 lazy import 제거 → cycle이 사라졌으므로 top-level 복구 가능 →
  복구함. 관련 docstring 4줄도 정리
- callsite 2곳 (`loop.py`, `test_retry_builders.py`)만 갱신하면 됐음 — 기존 추정
  "무시 못 할 변경량"은 보수적이었음

**현재 상태**: 청산. 의존 방향 `recovery → constants`로 정상화. import cycle 잠재 0.
회귀 방지는 `tests/test_import_cycles.py`가 계속 보장.

---

## 5. Step 4b는 데이터 누적 후 (정상 deferral)

A4·A5에 별도 primitive(`probe_tool_name`, `echo_diff` 등)를 추가할지는 *측정값 기반*
결정. 현재 라벨만 깔아 둔 상태로 며칠/몇 주 사용 → TurnRecord로 회복률 측정 →
필요시 primitive 추가.

이건 **부채가 아니라 의도된 기다림**. 라벨이 없으면 미래 결정 자체가 불가능 →
지금 라벨링은 이 기다림의 *전제 조건*.

---

## 청산 전략

부채 1·2·3은 *서로 묶여 있음*. 풀려면 한 번에:

1. `_dispatch_tool_with_hooks`를 `pre_hooks` / `dispatch_or_delegate` /
   `post_process` 세 함수로 쪼갬
2. 새 dispatch 함수가 `execute_tool`을 직접 호출 — `execute_tool`은 레이블 없는
   private helper로 강등 (혹은 단순 inline)
3. `tools/__init__.py:execute_tool`의 `__all__` export 제거
4. 5+ 의존 테스트를 loop-flow 통합 테스트로 옮김
5. `execute_tool` 자체 또는 그 이름을 제거

작업 추정: ~400줄, 위험 중간 (테스트 5+ 다시 씀). Step 4 이후 별도 step으로 진행.

---

## 부채 추가·갱신 규칙

이 문서는 *살아있는 문서*다. 다음 경우 갱신:
- 새 부채를 의도적으로 남길 때 → 항목 추가
- 부채를 청산할 때 → 항목 삭제 + 해당 커밋 SHA 기록
- 부채 분류·우선순위 바뀔 때 → 본문 갱신

부채를 *알면서* 남기는 건 정직. 부채를 *모르고* 누적시키는 게 부채 폭발의 원인.
