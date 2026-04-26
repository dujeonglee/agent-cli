# Robust Harness — Remaining Technical Debt

이 문서는 Step 4a 시점에 *알면서* 남긴 부채를 명시 기록합니다. 무지로 인한 누적
방지가 목적 — "이건 부채인 줄 알고 있고, 이런 이유로 지금은 청산 안 한다"를
공개적으로 둠으로써 미래 결정자가 빠르게 판단할 수 있게 합니다.

---

## 1. `_dispatch_tool_with_hooks`는 SRP 위반 (medium)

함수(이전 이름 `_execute_single_tool`)가 ~6가지 일을 한 곳에서 함:

1. PreToolUse hooks (Python runner + shell)
2. delegate 특수 분기 dispatch
3. `execute_tool` 호출 (일반 도구)
4. observation 크기 가드 (shell 큰 출력 → artifact)
5. read_file 아티팩트 mtime 갱신
6. `tools_called` / `recent_tool_history` 추적

**왜 부채인가**: 단일 책임 원칙 위반. 점진적 진화의 흔적 — hooks가 추가될 때, observability
가드가 추가될 때마다 한 함수에 쌓임. 이름은 정직해졌지만(`_dispatch_tool_with_hooks`)
크기는 그대로.

**왜 지금 안 청산하는가**: 청산하려면 함수를 `pre_hooks`/`dispatch`/`post_processing`
3단계로 쪼개야 함. 작업 자체는 깔끔하지만 *Step 4a의 본 목적*(A4/A5 라벨링)과 별개.
같은 커밋에 섞으면 둘 다 흐려짐.

**언제 청산할 수 있나**:
- Step 4b 이후, 테이블 위에 다른 핵심 일이 없을 때
- 또는 hooks/observability에 새 기능 추가가 필요해서 손대야 할 때 (자연스러운 청산
  타이밍)

**위험도**: 코드는 정확히 동작 중. 단지 미래 변경이 코스트 높음(8개 책임 하나에 박혀
있어 변경 영향 분석 어려움).

---

## 2. `execute_tool`의 boundary 방어는 테스트가 박아 둔 부채 (low)

`agent_cli/tools/__init__.py:execute_tool`은 `Unknown tool: '{name}'. Available: ...`
방어 체크를 갖고 있음. Step 4a에서 recovery 레이어로 검증을 옮긴 후, 이 체크는
*실질적*으로 `tests/test_tools_coverage.py`의 5+ 테스트 케이스에 의해서만 도달됨.

**왜 부채인가**: "공개 API boundary 방어"라는 명분은 진짜 외부 의존자가 있을 때 유효함.
현재는 외부 의존자 0이고, 내부 테스트가 검증을 핀으로 박은 셈. *우리 테스트가 우리
디자인 결정을 강제하는 역방향*.

**왜 지금 안 청산하는가**:
- 청산하려면 5+ 테스트를 *flow* 단위(loop dispatch 통합 테스트)로 옮겨야 함
- 청산 후에도 라이브러리로서의 약속을 명시 포기해야 함 (현재 `__all__` export 중)
- 청산 ROI 낮음 — 동작상 문제 없음, 그저 *디자인 정직성* 문제

**언제 청산할 수 있나**:
- 외부 의존자 0임을 확인 + 라이브러리 export 의도가 진짜 없다고 판단되는 시점
- 또는 `tools/`에 추가 boundary 검증이 들어와서 일관성 있게 정리할 시점

---

## 3. `execute_tool` vs `_dispatch_tool_with_hooks` 이름·책임 모호 (low)

두 함수가 *유사한 이름, 다른 책임*:
- `execute_tool` — 진짜 dispatch primitive (`TOOLS.get` + 호출)
- `_dispatch_tool_with_hooks` — orchestration wrapper (hooks + dispatch + post-processing)

**왜 부채인가**: 새 사람이 코드 읽으면 "왜 둘?"이 즉시 떠오름. 이름이 좋아져도
*존재 자체*가 점진 진화의 흔적.

**왜 지금 안 청산하는가**:
- 부채 1을 청산할 때 자연스럽게 같이 처리됨 (orchestration을 쪼개면 wrapper도 사라짐)
- 단독 청산하려면 부채 2도 같이 풀어야 함 (`execute_tool`을 내부 함수로 강등)

**연쇄 의존**: 부채 1·2와 묶여 있음. 셋 중 하나만 풀려고 하면 어색해짐. 셋을 한
번에 정리하는 게 깔끔.

---

## 4. `constants.py`가 `recovery`를 의존 → import cycle 잠재 (low)

`constants.py` 상단에서 `from agent_cli.recovery.intervention import Intervention`
+ `recovery.primitives` 를 import 함. 이는 `format_no_json_retry`/`format_no_action_retry`/
`format_action_loop_intervention`이 historical하게 constants에 자리잡은 결과 — 이 함수들은
*상수가 아니라* primitive 합성 factory임.

문제: 다른 모듈(특히 `tools/`)이 `constants` 를 import하면 cycle이 생길 수 있음. Step 4a
에서 `detectors.py`가 `tools.registry`를 import한 순간 실제 cycle이 발생했고, CLI 직접 실행
경로에서만 터졌음(테스트는 진입 순서가 달라 통과). lazy import으로 임시 우회.

**왜 부채인가**: 레이어 역전 — `constants`(저층)가 `recovery`(고층)를 의존. 반대 방향이
정상. lazy import은 *cycle을 우회*할 뿐, 레이어 위반 자체는 그대로.

**왜 지금 안 청산하는가**: 청산은 `format_no_json_retry` 등 factory 함수를 `constants.py`
밖으로 옮겨야 함(예: `recovery/builders.py`). 옮기면 `constants` import 위치를 모든
caller에서 갱신해야 함 — 무시 못 할 변경량. A6 작업과 별 commit으로 분리.

**언제 청산할 수 있나**: hooks/loop 리팩토링 등 constants 호출자를 어차피 손대는 시점.
혹은 새 cycle이 또 한 번 발생할 때(이번처럼 reactive 청산도 정상).

**완화책 (이미 적용)**: `tests/test_import_cycles.py`가 cold-start subprocess import를 검증해
회귀 자체는 막힘.

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
