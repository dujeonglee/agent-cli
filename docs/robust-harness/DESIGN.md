# Robust Agent Harness — Design

## Why this exists

Frontier API는 깨끗한 출력이 전제다. 하지만 로컬 35B급 모델을 Ollama·mlx-vlm·vLLM으로 돌리면
JSON envelope drift, tool name 환각, action_input 스키마 위반, 무한 루프 같은 실패가 일상적이다.
기존 agent harness들(LangChain, CrewAI, AutoGen 등)은 cloud API를 전제로 만들어졌고 실패 회복은
예외 처리로 붙여져 있다.

이 설계의 목표: **거지 같은 환경(brittle local runtime)에서도 안정적으로 도는 agent harness.**
실패를 1급 시민으로 다루고, 회복을 정상 경로의 분기로 만든다. robustness가 패치 누더기가 아니라
*자산*으로 누적되는 구조를 추구한다.

---

## 0. Foundational Principles

이게 흔들리면 디자인 전체가 누더기가 된다.

1. **실패는 1급 시민.** 예외가 아니라 정상 경로의 한 분기.
2. **Failure grounding이 메인 메커니즘.** 모델에게 자기 출력을 거울처럼 보여주면 모델이 자기 보고
   자기 고친다. constrain·reset은 grounding이 안 통할 때의 escalation.
3. **레이어 경계는 정규화된 데이터만 통과.** provider/runtime/model 이름이 위 레이어로 새면 안 됨.
4. **새 실패 = 코드 X, 매핑 한 줄 O.** 코드 수정 없이 매핑 테이블만으로 확장되어야 누더기가 안 된다.
   코드 수정해야 하면 도구함이 부족한 거.
5. **모든 실패와 회복은 관찰 가능.** 통계 누적이 진짜 자산이고 moat이다.

---

## 1. Target Failure Catalog

원칙: **우리가 *실제로* 본 것만 포함.** 가설은 안 넣음. 새 모드 발견되면 그때 추가.

### Layer A — Envelope 실패 (단일 응답으로 감지 가능)

| ID | 이름 | 감지 방법 |
|---|---|---|
| A1 | JSON 부재 (prose만) | parser stage 1+2+3 모두 실패 |
| A2 | JSON 손상 (truncation, bad quote 등) | stage 2 (json_repair) 성공 — **이미 in-band 회복됨, out-of-band 처리 불필요** |
| A3 | `action` 필드 누락 | parser 성공, but `action is None` |
| A4 | 알 수 없는 tool name | parser 성공, but `action not in tool_registry` |
| A5 | `action_input` 스키마 불일치 | tool dispatch에서 거부 |
| A6 | Nested envelope (이중 래핑된 complete) | `complete` action_input.result 가 다시 `{"result": "..."}` JSON 객체로 파싱됨 (qwen3.5/3.6 계열에서 관찰) |

### Layer B — 행동 실패 (다중 턴 관찰 필요)

| ID | 이름 | 감지 방법 |
|---|---|---|
| B1 | Action loop | 같은 `(action, args)` 해시 연속 ≥3회 |
| B2 | Tool result 무시 | 직전 turn 결과를 다음 thought가 참조 안 함 (휴리스틱) |

### Layer C — 자원 실패

| ID | 이름 | 감지 방법 |
|---|---|---|
| C1 | Context overflow | token 카운트 > model limit |

### v1 범위 밖 (검증 후 추가)

- Premature complete (task 미완료 종료) — completion oracle 필요
- Self-debate 진동 — thought 분석 휴리스틱 신뢰도 낮음
- Hallucinated tool result — 검증 메커니즘 무거움
- Context drift (오래된 task 망각) — 측정 어려움

---

## 2. Methodology

### 2.1 In-band vs Out-of-band Recovery

회복 축 두 개를 명확히 구분:

```
LLMResponse 도착
    │
    ▼
[In-band recovery]  ← 같은 응답 안에서 복구. 추가 LLM 호출 없음.
  - JSON repair (parser stage 2)
  - Regex extraction (parser stage 3)
  - 마크다운 펜스 제거
    │
    ├─ 성공 → ParseResult 사용
    │
    └─ 실패 → FailureSignal 발행
                │
                ▼
        [Out-of-band recovery]  ← 다음 턴에 개입
          - playbook 조회
          - primitive 합성 → Intervention
          - 메시지 주입 후 retry
```

In-band가 무료(추가 latency·token 0)이므로 항상 먼저. 못 고치는 것만 out-of-band로.

### 2.2 Recovery Primitive 도구함

**Primitive contract** (이게 깨지면 누더기 시작):

```
Input:  FailureSignal + Conversation + ToolRegistry + Task   (전부 harness-level)
Output: Intervention (텍스트 주입 | 파라미터 조정 | 상태 리셋)
금지:   provider 이름, 모델 이름, 채널 이름, runtime 분기 일체
```

| 패밀리 | Primitive | 역할 |
|---|---|---|
| **Echo** | `echo_prior_output` | LLMResponse.content를 거울처럼 인용. **v1은 content-only.** thinking 채널 echo는 측정된 효과 없이 runtime 의존성만 유발하므로 제외. Step 2 observability 데이터로 필요성 검증되면 별도 primitive로 추가. |
| | `echo_diff` | (expected, got) JSON diff 인용 |
| | `echo_last_action` | 직전 (tool, args, result) 인용 |
| **Probe** | `probe_tool_name` | "X 없음, 가능: [...]" |
| | `probe_schema` | "필드 누락: [...], 스키마: {...}" |
| | `probe_progress` | "이 호출 N번째, 뭐가 달라지나?" |
| **Constrain** | `constrain_format` | "다음 응답: JSON만" |
| | `constrain_action` | "허용된 action: [...]" |
| **Reset** | `restate_task` | 원본 task 재고정 |
| | `compact_history` | 오래된 턴 요약·축소 |

### 2.3 Playbook (매핑)

**구성 데이터, 코드 아님.** 새 실패 추가 = 행 추가.

| Failure | Try 1 | Try 2 (재실패) | Try 3 |
|---|---|---|---|
| A1 | echo_prior_output | + constrain_format | restate_task |
| A3 | echo_prior_output + probe_schema | + constrain_format | |
| A4 | probe_tool_name | + constrain_action | |
| A5 | echo_diff + probe_schema | + constrain_format | |
| A6 | (관찰만 — v1 라벨링 전용) | | |
| B1 | probe_progress | + 파라미터 조정 (temp↓) | restate_task |
| B2 | echo_last_action | + probe_progress | |
| C1 | compact_history | | |

**Escalation rule**: 같은 `FailureSignal`이 N턴 연속이면 다음 컬럼으로 이동. 모든 컬럼 소진 시 fail-fast.

---

## 3. Architecture

### 3.1 4-Layer 구조

```
┌────────────────────────────────────────────────────┐
│ ① Provider Layer                                    │
│    - Ollama / Anthropic / OpenAI-compat 어댑터      │
│    - runtime quirk 전부 흡수                        │
│    - thinking 채널 합치기, finish_reason 정규화 등  │
│    OUTPUT: LLMResponse {content, thinking?, stop_reason, usage}
└────────────────────┬───────────────────────────────┘
                     │ 정규화된 LLMResponse
                     ▼
┌────────────────────────────────────────────────────┐
│ ② Parse Layer (in-band recovery)                    │
│    - 3-stage fallback (json.loads → repair → regex) │
│    - 마크다운 펜스 제거, 채널 정리                  │
│    OUTPUT: ParseResult or None                      │
└────────────────────┬───────────────────────────────┘
                     │ ParseResult / 실패
                     ▼
┌────────────────────────────────────────────────────┐
│ ③ Detection Layer                                   │
│    - 응답 + 대화 상태 → FailureSignal              │
│    - Detector는 stateless on response,             │
│      stateful across turns (loop detector 등)      │
│    OUTPUT: FailureSignal {type, severity, context} │
└────────────────────┬───────────────────────────────┘
                     │ FailureSignal
                     ▼
┌────────────────────────────────────────────────────┐
│ ④ Recovery Layer (out-of-band)                      │
│    - Playbook 조회 → Primitive 합성 → Intervention │
│    OUTPUT: Intervention                             │
└────────────────────┬───────────────────────────────┘
                     │
                     ▼
            Loop이 적용해서 다음 턴 진행
```

### 3.2 경계 계약

**Provider → Parse**: `LLMResponse` *오직* 이것만. provider 이름·모델 이름·채널 이름은 LLMResponse
어디에도 없음. 있으면 누수.

**Parse → Detect**: `ParseResult` 또는 `None`. parse_stage(1/2/3) 정보는 detector를 위해 보존.

**Detect → Recover**: `FailureSignal` (enum + context dict). 이전 턴 raw response 같은 건 context에
정규화된 형태로만.

**Recover → Loop**: `Intervention`. 실행 방법은 표준화된 종류로 제한:
- `MessageInjection(role=user, content=...)`
- `ParamAdjustment(temperature=0.3)`
- `StateReset(action="compact" | "restate")`

새 Intervention 종류 추가는 신중히 — 종류 늘면 누더기 시작 신호.

### 3.3 Observability

매 턴 기록 (`TurnRecord` JSONL):

```
{
  turn_id, model_id, parse_stage,
  failure_signal,
  primitives_applied,
  recovered_in_turns
}
```

단순 JSONL로 추가만. 분석은 별도 도구로. 통계가 쌓이면:
- "qwen3.5-35B-mlx에서 A1 실패의 echo만으로 회복률 80%" 같은 *측정값*이 나옴
- playbook 순서를 데이터로 검증·튜닝 가능
- 새 모델 들어올 때 즉시 baseline 측정

---

## 4. 누더기 방지 불변식

설계만으론 부족, **invariant로 박아야** 함. 코드 리뷰·구현 시 체크리스트:

| 불변식 | 위반 신호 |
|---|---|
| Primitive는 provider/모델/채널 이름을 모른다 | `if "ollama"`, `if "qwen"`, `response.thinking` 직접 분기 |
| 새 failure type = 매핑 행 추가 (코드 0줄) | 새 if/else 분기 시 도구함 부족 — primitive를 늘려야지 분기를 늘리면 안 됨 |
| Detector는 부수 효과 없음 | detector가 conversation 수정하면 위반 |
| Intervention 종류는 ≤ 5개 유지 | 종류 늘면 표준화 실패 신호 |
| LLMResponse에 provider 식별자 없음 | 누수 시작 |
| 같은 primitive가 ≥2개 failure에서 재사용 | 안 그러면 사실상 패치 |

---

## 5. 명시적 범위 밖

v1에선 **유혹돼도 안 들임**:

- `logit_bias`, grammar/BNF, JSON mode (provider 어댑터 *내부*에서 활용은 OK, primitive 레이어엔 안 옴)
- streaming 중간 개입 (다른 정신모형)
- function-calling API 지원 (ReAct envelope과 병행 = 분기 2배)
- Premature complete / drift / hallucinated tool result 감지 (휴리스틱 신뢰도 미검증)
- 자동 모델 fallback (provider A 실패 → B 호출 같은 거)

---

## 6. Implementation Roadmap

각 단계마다 엄격한 테스트 + 문서 동시 업데이트.

| Step | 상태 | 작업 | 위험 | 성공 기준 |
|---|---|---|---|---|
| **1** | ✅ 완료 | 기존 코드를 새 어휘로 재표현 (동작 변경 0). `format_no_*_retry`를 primitive 합성으로 분해. | 낮음 (refactor) | 기존 테스트 전부 통과, 새 primitive 단위 테스트 추가 |
| **2** | ✅ 완료 | Observability 추가. `Intervention` 타입 도입, `TurnRecord` JSONL 세션별 기록. CLI `--record-turns/--no-record-turns`. | 낮음 (additive) | 회복률 통계 jq로 dump 가능 |
| **3** | ✅ 완료 | B1 (`ActionLoopDetector` + `probe_progress` + `restate_task`) 추가. 임계값 2, 옵션 (c) 채택 (level 1=probe, level 2=restate, level 3+=hard-fail; temp↓ 컬럼 제외). | 중간 (새 detector) | 인위적 loop 시나리오에서 ≤5턴 내 회복 ✓ |
| **4a** | ✅ 완료 | A4·A5 detection을 recovery 레이어로 이동 (`detect_unknown_tool`, `detect_schema_mismatch`). pre-dispatch에서 라벨링. `_execute_single_tool` 내부 중복 검증 제거 + `_dispatch_tool_with_hooks`로 리네임. 별도 primitive는 데이터 보고 4b에서 결정 — 알면서 남긴 부채는 `REMAINING_DEBT.md` 기록. | 낮음 (additive + rename) | A4/A5 TurnRecord에 라벨 기록 ✓ |
| **4a-1** | ✅ 완료 | A6 (Nested envelope) 추가 — `detect_nested_envelope` 라벨링 전용. 자동 unwrap 안 함 (anti-patchwork: 측정 후 결정). qwen3.5/3.6 계열에서 관찰된 `complete.action_input.result == '{"result": ...}'` 이중 래핑 패턴. | 낮음 (additive, observe-only) | A6 TurnRecord에 라벨 기록 ✓ |
| **4b** | 데이터 누적 후 | TurnRecord 통계로 회복률 측정 → 필요하면 `probe_tool_name` / `echo_diff` / A6 unwrap 등 primitive 추가 + playbook 매핑. | 낮음 (data-driven) | 측정값 기반 결정 |

각 step은 독립 커밋. CLAUDE.md 규칙 준수: 유닛 테스트 + ruff + README.md/ARCHITECTURE.md 업데이트
+ regression 0 후 단일 커밋·푸쉬.

---

## 7. 관련 문서

- `docs/ARCHITECTURE.md` — 전체 아키텍처 (이 작업으로 layer 4개 추가됨; step별로 업데이트)
- 추후 `IMPLEMENTATION_LOG.md` — step별 결정·트레이드오프 기록 (필요 시)
