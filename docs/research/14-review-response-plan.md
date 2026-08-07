# 리뷰 대응 플랜 — v0.8 → v0.9

> 2026-08-07. 대상 리뷰: `13-reviewer-simulation-v0.8.md` (Major Revision).
> 네 트랙: **A 해명(W2)** · **B–C 실험 재설계/재실험(W1, W3, W6)** · **D–E 인용/서술 보강(W4, W5)** · **F Abstract·표현 재보강**.
> 원칙: 수치가 바뀌는 작업(Phase 1–2)을 먼저 끝내고 Abstract(Phase 3)는 마지막에 쓴다.

---

## 사전 확인된 사실 (플랜의 근거)

코드 확인 결과 (2026-08-07):

1. **Mid-run injection은 턴 경계 전용이다.** `agent_cli/loop/core.py:604` `_inject_queued_messages()`는 에이전트 루프의 *내부 턴 경계*(LLM 스텝 사이)에서만 큐에서 **한 건**을 꺼내 주입한다. 실행 중인 LLM 호출 도중에는 주입 지점이 없다.
2. **§6.1의 A 태스크는 단일 호출이다.** `bench/multiuser/e2_hol.py:51` — `[[bench ttft=200 tok=… n=… id=a]]`는 툴 스텝 없는 한 번의 생성이므로 A의 런에는 턴 경계가 아예 없다. 따라서 serial에서 B의 질문은 A의 생성이 끝난 뒤에야 주입·처리되고, **TTFT_B ≈ L은 injection을 끈 결과가 아니라 메커니즘의 귀결이다.**
3. **§6.10의 3질문→2호출**은 라이브 워크로드가 멀티스텝(툴 호출 포함)이어서 턴 경계가 존재했기 때문에 injection이 실제로 접붙인 사례다. 두 절은 모순이 아니라 *경계 밀도*의 차이인데, 논문이 이를 말하지 않고 있다.

→ W2는 **재실험 없이 해명 가능**하지만, 해명을 수치로 뒷받침하는 저비용 실험(E2b)을 추가하면 오히려 baseline이 강해져 W1 방어에도 쓸 수 있다.

---

## 진행 기록

- **Phase 0 완료** (커밋 `a9287184`): 아래 A1·D·E·F1-3 의 문서 작업. 수치 무변경.
- **Phase 1 완료**: B3(§6.1 통계 보강), E2b·e2c·e2d 신규 실험 3건. 전부 §6.1 에 반영, 원시 데이터 `out/` 커밋.
- **Phase 2 완료**: Track C 구현(`--turn-scoping`)+목 절제(`n3b_scoping.py`) → 이후 엔드포인트 확보로 라이브 4 건(C2-live, Q3, B1/B2, F2) 전부 완료.

### Phase 2 라이브 결과 요약 (Qwen3.6-27B-MLX-8bit)

**C2-live (`out/n3c-scoping-real.json`) — W3 의 열린 절반을 닫았다.** 팔당 20 회, 팔 교대 실행.

| 팔 | 남의 파일을 쓴 턴 | 자기 과제 완수 턴 | 두 과제 모두 옳은 실행 | 턴 길이 중앙값 |
|---|---|---|---|---|
| `off` | 40 중 26 | 40 중 34 | 20 중 14 | 79.0 s |
| `on` | **40 중 0** | **40 중 40** | **20 중 20** | 62.8 s |

- 두 팔 모두 실제 동시 실행 확인(짧은 턴이 긴 턴의 최소 65% 를 덮음, 중앙값 off 85% / on 74%) → `on` 의 0 은 겹침 부재 탓이 아니다.
- 가드 방향도 옳게 움직임 — 스코핑이 모델을 위축시키지 않았고 오히려 자기 과제 완수가 34→40 으로 올랐다.
- **발견된 실패 양상이 §6.4 의 일화와 다르다**: 지배적 패턴은 "남의 일을 대신"이 아니라 **"둘 다 함"**(자기 파일 + 이웃 파일). 그래서 34/40 이 자기 일을 완수했는데도 26/40 이 남의 영역을 침범했다.
- **측정 설계 교훈**: 초기 지표는 최종 파일 집합에서 cross-task 를 추론했는데 그러면 *남의 일을 함* 과 *제 일에 실패함* 이 섞인다. `reply_to`×`files` 조인으로 **턴별 귀속** 판정으로 교체했다. §6.4 의 기존 `classify()` 도 같은 약점이 있으므로 재사용하지 않았다.

**Q3 (`out/p6b-provider-concurrency.json`)** — 엔드포인트 동시 요청 N=1/2/4/8 → 벽시계 8.2/13.7/15.5/21.8 s. 8 개가 **8 배가 아니라 2.68 배**, 처리량 14.7→44.1 tok/s. "제공자 동시성이 얼마냐"에 수치로 답.

**B1/B2 (`out/p6-real-llm.json`)** — 순위: 직렬 38.2 s vs 병렬 10.8 s, 계약당 12 회에서 **범위가 겹치지 않음**(37.8~38.4 / 10.6~10.9). 토큰: warmup 0 → 1.49×, warmup 5 → **1.47×**. **한계 (1) 의 "이력이 길어지면 커진다"는 자인이 틀렸다** — 이력은 두 계약을 같은 비율로 부풀려 비에서 약분된다(c_par(S+H)/c_ser(S+H) = c_par/c_ser).

**F2 (`out/p2-shell-real.json`)** — 셸 3 회/턴: sleep 1 s 에서 효과 비중 0.025·락 대기 0 ms, 5 s 에서 0.094·락 대기 4.1 s. 파일 쓰기(10⁻⁵)보다 서너 자릿수 위라 **반론은 실재**하지만 여전히 50% 무릎의 1/5 이고 유효 병렬도 1.84~1.87(상한 2.00).

### Phase 2 의 가장 큰 발견 — 38% 는 안정된 양이 아니었다

§6.7 이 "최악 38%" 로 인용해 온 의미적 혼선률을 **동일 구성으로 반복**하니 크게 흔들렸다: 오늘 같은 호스트에서 4%, 10%, 그리고 5 회 반복이 13/17/19/19/21%. 혼선은 턴이 실제로 겹치는 곳에서만 생기고 겹침 정도는 호스트 스케줄링의 성질이라, **단발 값은 상한이 아니다.** 논문에서 그 주장을 철회하고 분포로 대체했다. 반면 **구조적 귀속은 16 회 실행 전부에서 완전**(전단사, 중복 0, 미매칭 0) — 논문의 본 주장은 그대로다.

주의: `out/n3-attribution.json` 을 재실행으로 덮어썼다(38% → 4%). 구값은 git 이력(`8ce8308d` 이전)에 남아 있고, 단발 아티팩트보다 `n3b-scoping.json` 의 5 회 반복 분포가 더 나은 근거다.

### Track C 결과 (`out/n3b-scoping.json`) — 목으로 잴 수 있는 것과 없는 것

| 팔 | 의미적 불일치 min/median/max (5회) | reply_to 전단사 |
|---|---|---|
| `off` 스코핑 없음 | 13% / 19% / 21% | 5/5 완전 |
| `ignore` 스코핑 on + 시스템 프롬프트 안 읽는 목 | 1% / 21% / 26% | 5/5 완전 |
| `honor` 스코핑 on + 스코프 따르는 목 | 0% / 0% / 0% | 5/5 완전 |

- `ignore` ≈ `off` 는 **음성 대조군 통과** — 프롬프트가 길어진 부수 효과가 아니라 순응이 변수임을 확인.
- `honor` 0/5 는 "실모델이 순응한다"가 아니라 **① 순응하면 메커니즘이 충분** ② **스코프가 동시 500 턴 각각에 자기 요청을 싣고 도달**했다는 종단 확인. ②는 thread-local 버그(§5)와 같은 실패 표면이라 사소하지 않다.
- **효과 자체는 목으로 측정 불가** — 순응이 시험 대상인데 목의 순응은 우리가 코딩하는 것이기 때문. `[TODO: C2-live]`.

### Phase 1 에서 새로 드러난 것

1. **§6.1 의 n 이 부정확했다.** 논문은 "n = 240" 이라 했으나 커밋된 `out/e2-hol.jsonl` 재검 결과 240 회 중 **236 회**만 귀속 가능한 first_token 을 냈다(병렬 L=2s·6s 각 1회, 거부 L=2s·15s 각 1회). 원인 미규명. §6.1 에 그대로 공개하도록 수정했다.
2. **계측 공백 — 주입된 메시지에는 귀속 first_token 이 없다.** `loop/llm.py:66` 의 `_first_token_emitted` 는 run_loop 당 1회 래치다. 그래서 직렬 계약에서 실행 중 턴에 접혀 들어간 질문은 잴 TTFT 자체가 없다. E2b 는 `query_added` 기반 **time-to-inclusion** 으로 우회했고, 그것이 직렬에 유리한 하한임을 논문에 명시했다. 계측을 확장할지는 Phase 2 판단 사항.
3. **[Phase 2 후보] 주입 이후 `reply_to` 가 전환된다.** 탐사 실행에서 B 의 질문이 A 의 도는 턴에 주입된 뒤, 그 턴의 후속 assistant 레코드가 `reply_to=u2`(B 의 질문)로 기록됐다. 즉 직렬+주입에서는 A 의 남은 작업이 B 에게 귀속된다. 병렬 계약이 "턴 하나 = 질문 하나"로 구성상 피하는 문제라 §6.7 논거를 강화할 수 있으나, **관측 1회뿐이라 논문에 넣지 않았다.** Track C 의 귀속 작업과 함께 제대로 측정할 것.

### E2b 결과 (`out/e2b-injection.jsonl`, `out/e2b-summary.json`)

L = 15 s 고정, k ∈ {1,2,4,8}, 셀당 10 회. inclusion p50: 15.10 / 7.30 / 3.39 / 1.44 s.
- 경계 간격에 대한 회귀: **기울기 1.041, R² = 0.99995, 절편 −514 ms**(= B 의 500 ms 제출 지연).
- k=1 주입 발동 0/10, k≥2 는 10/10 — Phase 0 공개가 예측한 그대로.
- k=1 의 TTFT 15.41 s vs §6.1 직렬 L=15s 셀 15.34 s → **0.50% 일치**(정합성 체크 통과).

### e2c 결과 — 거부 벌점 대 재시도 간격 (`out/e2c-summary.json`)

L = 15 s, 같은 세션의 직렬 기준선 p50 = 15,480 ms.

| 재시도 간격 | 거부 p50 | 직렬 대비 벌점 | 간격 대비 비율 | 재시도 횟수 p50 |
|---|---|---|---|---|
| 250 ms | 15,653 ms | +172 ms | 0.69 | 61 |
| 1,000 ms | 16,274 ms | +794 ms | 0.79 | 16 |

벌점은 간격에 비례해 커지되 **간격 하나 안에 머문다**(0.69, 0.79 < 1) — 논문의 "위상 의존 분수" 해석을 직접 확인. 두 간격 모두에서 거부 > 직렬. 각 셀 n=9(10 회 중 1 회는 first_token 미귀속, §6.1 의 그 현상과 동류).

### e2d 결과 — 사용자 수 축 (`out/e2d-summary.json`)

A(L=15s) + (N−1) 동시 질문자, 상한 = N, 병렬 계약, 셀당 10 회.

| N | 질문자 | 질문자 턴 수 | TTFT p50 | p95 |
|---|---|---|---|---|
| 2 | 1 | 10 | 236 ms | 239 ms |
| 4 | 3 | 30 | 265 ms | 270 ms |
| 8 | 7 | 70 | 328 ms | 361 ms |

질문자 7 명이 1 명 대비 **7× 가 아니라 39%** 추가 비용. 증가분은 런타임의 동시 턴 유지 비용(스레드·연결·팬아웃)이지 타인 작업 대기가 아니다. 평탄함이 2인 설정의 산물이 아님을 확인.

---

## Track A — W2/Q1 해명 (Phase 0–1)

### A1. 본문 해명 (글만, Phase 0)

- **§6.1 프로토콜 문단에 추가**: serial 팔은 mid-run injection이 *켜진 채* 측정되었음을 명시하고, injection 지점은 내부 턴 경계뿐이며 A 태스크가 단일 생성이므로 경계가 없음 → TTFT_B = L은 mechanism의 답임을 서술.
- **§6.10에 역참조**: "3질문 2호출"이 같은 메커니즘의 멀티스텝 케이스임을 명시. 서로가 서로의 각주가 되게 한다.
- **측정 기점 표현 통일** (리뷰 minor): §5 "measured from the server's first 409" vs §6.1 "from B's first submission attempt" — 실제 하니스 코드(`e2_hol.py`의 reject 분기)를 확인해 한 문장으로 통일. 첫 (거부된) POST의 왕복이 포함되는지 명시.

### A2. 신규 실험 E2b — "serial + injection의 실제 상한" (Phase 1, mock, 저비용)

- **설계**: A의 태스크를 총 길이 L=15 s 고정, 스텝 수 k ∈ {1, 2, 4, 8}로 분할(스텝마다 무해한 툴 호출로 경계 생성). serial 계약에서 B의 TTFT를 측정.
- **예상**: TTFT_B ≈ (다음 경계까지 시간) + (B 답 생성 시간) — k가 클수록 감소. k=1이 §6.1의 15.34 s를 재현해야 함(정합성 체크 겸용).
- **mock 함정 검토 완료**: §5의 shared-transcript directive-collapse는 *동시 턴*이 서로 다른 스크립트를 돌릴 때의 문제. serial 계약은 동시 턴이 없고 B는 큐 대기 질문이므로 이 실험은 mock으로 안전하다.
- **산출물**: `bench/multiuser/e2b_injection.py` → `out/e2b-injection.json`. 논문에는 §6.1 하위 문단으로 편입: "serial의 진짜 상한은 L이 아니라 경계 간격이다; 경계가 조밀한 워크로드에서 serial은 parallel에 접근하지만, 긴 생성·긴 쉘 실행이 경계를 없앤다" — baseline을 스스로 강화해 리뷰어의 W2를 정면으로 흡수.
- **수용 기준**: k=1 셀이 기존 §6.1 serial L=15 셀과 ±5% 내 일치. k에 대한 단조 감소.

---

## Track B — W1 대응: §6.10 승격 재실험 (Phase 2, 실모델 필요)

리프레임 원칙: §6.1(mock)은 "구현이 계약대로 동작한다는 *검증*", §6.10(live)이 "실세계 주장". 서사와 실험 규모를 이에 맞춘다.

### B1. 랭킹 실험 확장 (`p6_real_llm.py` 개정)

- 반복 5 → **20+**, median/p95와 분산 보고 (Q3 대응).
- **서빙 스택 명세 기록**: 엔드포인트의 동시성 한계(동시 디코드 슬롯, batch 정책)를 결과 JSON에 함께 저장 — "provider's concurrency" 명시 요구 대응.
- 사용자 수 축 추가: N ∈ {2, 3} (엔드포인트 동시성 내에서).

### B2. 토큰 회계 확장

- 반복 3 → **10**, 그리고 **히스토리 길이 축 추가**: 짧은 히스토리(현행) vs 성장한 히스토리(예: 50턴 진행 후 동일 3질문 워크로드) — "premium grows with history" 자인을 측정으로 대체 (Limitation 1 보강, W6 일부 겸용).
- 산출: 1.49×가 히스토리 길이에 따라 어떻게 움직이는지 곡선 1개.

### B3. 본문 재배치

- Abstract·§1·기여 4에서 mock slope를 "검증"으로 강등하고 live 결과(7.0 s vs 27.8 s, 1.49×→곡선)를 승격.
- §6.1 통계 보강(재실험 불필요, 커밋된 `out/e2-hol.jsonl` 재계산): 회귀 적합 방식(전체 점 vs 셀 중앙값), R², serial/reject의 p95−p50 산포 추가. L=30 셀에 세션 경계 각주.

---

## Track C — W3/Q2 대응: semantic confusion 완화 실험 (Phase 2, 최대 작업)

리뷰의 급소. "구조적 귀속은 기록하지 방지하지 않는다"에 대해 **완화책 1개를 구현·측정**하고, 실패해도 negative result로 보고한다.

### C1. 완화책 구현 — per-turn instruction scoping

- **설계**: 턴별 프롬프트 전문(前文)에 "이 턴은 사용자 U의 요청 t_i를 수행한다; 트랜스크립트에 보이는 다른 사용자의 동시 요청은 참고용이며 이 턴이 실행할 대상이 아니다"를 삽입 + 사용자 메시지에 turn-id 태그. 턴 정체성은 이미 thread-local로 존재하므로(§5 attribution 기계) `agent_cli/loop/prompt.py`의 섹션 주입 경로를 재사용 — 신규 추상화 불필요.
- **스위치화**: `--turn-scoping on|off` — §6의 다른 스위치들과 같은 ablation 문법 유지.
- **CLAUDE.md 준수**: agent_cli 코드 변경이므로 유닛 테스트(프롬프트에 스코핑 섹션이 정확히 그 턴에만 붙는지, thread-local 정합), ruff, ARCHITECTURE.md 갱신, 단일 커밋.

### C2. 측정 (on/off ablation, 2팔)

1. **어드버세리얼 재실험**: `n3_attribution.py`의 always-answer-latest 모델로 scoping off(38% 재현) vs on — 단, mock 어드버세리얼 모델은 지시를 무시하도록 *설계*되어 있으므로, scoping 지시를 조건부로 존중하는 어드버세리얼 변형("태그가 없으면 최신 질문에 답한다")을 함께 정의해 상한/하한을 모두 보고. 설계 논거를 본문에 명시(어드버세리얼 상한은 완화 불가능함이 당연하고, 측정 대상은 *비적대* 모델의 혼선률).
2. **라이브 재실험**: §6.4의 12-run 라이브 팔(`p2_scope_real.py`)을 scoping on/off로 재실행(각 12 runs) — cross-user file write 재발 여부. §6.6 스타일의 경부하 혼선률(4/90)도 scoping on으로 재측정.

### C3. 본문 반영

- §6.7을 "구조적 귀속(해결) + 의미론적 혼선(측정→완화 시도)"의 2부 구조로 재편. §6.4의 1/12 사건을 "incidental"에서 명명된 결과로 승격.
- Limitation 3을 독립 항목으로 확장하고 Track D(위협 모델)와 연결.
- **수용 기준**: 완화 효과가 있으면 rate 감소 보고; 없으면 "prompt-level scoping은 불충분하다"를 negative result로 명시 — 어느 쪽이든 W3 답변이 된다.

---

## Track D — W5 대응: 위협 모델 절 신설 (Phase 0, 글만)

- §7.5 Limitation 7을 확장하거나 §7에 짧은 절 신설: **신뢰 모델 명시** — 본 시스템은 상호 신뢰하는 협업자를 가정한다; 공유 컨텍스트는 cross-principal 주입 채널이며 §6.7의 혼선이 그 채널이 살아있다는 증거다; 계약이 막는 것(물리적 무결성, 턴 소유권, read-only 격리)과 막지 않는 것(프롬프트 수준 교차 오염, 악의적 참가자)을 표로 구분.
- [30](multi-user policy)을 "보완재"로만 치우지 말고 이 절에서 실제로 접속: 권한 계층이 이 채널에 어떻게 덧씌워질 수 있는지 한 문단.
- AML.CS0035 인용을 메신저 비판에서만 쓰지 말고 자기 시스템에도 적용(리뷰어가 지적한 비대칭 해소).

## Track E — W4 대응: DB 동시성 제어 인용 보강 (Phase 0, 글만)

- **추가 인용 (필수)**: Gray, Lorie, Putzolu, Traiger — *Granularity of Locks and Degrees of Consistency* (1976, intention locks·계층 락); Berenson et al. — *A Critique of ANSI SQL Isolation Levels* (SIGMOD 1995, snapshot isolation·write-skew); Bernstein & Goodman — *Concurrency Control in Distributed Database Systems* (1981); Gray & Reuter — *Transaction Processing* (1993).
- **§4.3**: snapshot read + atomic commit이 스냅샷 격리와 동형임을 명시하고, **write-skew의 의미론적 유사물**(두 턴이 같은 스냅샷을 읽고 서로 다른 파일을 써서 불변식을 함께 깨는 경우)이 가능함을 인정 — Limitation 3과 연결.
- **§4.4**: 호환성 행렬 + 계층 락을 granular/intention locking의 이식으로 위치 지정. "last-wins by construction of order"가 고전 문헌에서 anomaly로 분류됨을 인정.
- **§2.4에 한 문단 신설**: "고전 CC와의 관계" — 기여의 novelty는 기법이 아니라 **배치**(inference는 어떤 락도 잡지 않는다; 효과층만 비관적)임을 선제 명시. 리뷰어의 "unawareness or overclaiming" 프레임을 차단.
- Q7 대응: "first user-level fairness mechanism" 주장을 "**에이전트 세션의 턴 granularity에서는** 최초로 아는 바"로 한정하고 API 게이트웨이류 per-user rate limiting과 구분.

## Track F — W6 + 표현/Abstract 재보강

### F1. 저비용 민감도 실험 (Phase 1, mock)

1. **Reject retry-interval 민감도**: L=15 셀 하나에 retry ∈ {250 ms, 1000 ms} — tax가 interval의 함수임을 1행으로 보이고 "never better than serial" 주장을 interval-독립으로 만든다.
2. **N 스케일링**: parallel 계약, N ∈ {2, 4, 8} 동시 사용자(cap 조정) — TTFT 평탄성이 N에서 유지되는지. fairness 게이트도 N=8 플러딩 1셀 추가.
3. **LangGraph enqueue = serial 팔 매핑 명시** (글만): §2.4와 §6.1에 한 줄.

### F2. Shell-heavy operating point (Phase 2, 라이브 1팔 — §4.4 긴장 해소)

- 리뷰 지적: 10⁻⁵ operating point는 파일쓰기 워크로드의 것; 코딩 에이전트의 지배적 효과는 쉘(빌드/테스트)이고 쉘은 배타 락이다.
- **실험**: 라이브 모델로 "테스트 실행 포함" 태스크 2-user 런 소수(예: 6 runs) — 쉘 hold 시간 기반 effect share를 측정해 §6.3의 collapse 곡선 위에 두 번째 operating point를 찍는다. 곡선의 knee(50%)와의 거리로 정직하게 보고.
- 산출: §6.4에 "two operating points" 문단.

### F3. Abstract·프레젠테이션 (Phase 3, 수치 확정 후)

- **Abstract 전면 재작성 (≤250 단어)**: 유지 — 설계 공간의 빈 좌표, 계약 한 문장, slope(검증) + live 결과(주장), 8.2%는 "forced-overlap ablation"으로 명기, 1.49×(+히스토리 곡선), 의미론적 혼선의 정직한 한 문장, 연구 어젠다. 삭제 — 실험 나열(§6.5–6.9 property list는 기여 목록으로 이동).
- **Table 1 D4 수정** (Q6): parallel 행을 "during, turn-scoped **(cancellation only)**"로, serial 행을 "during, session-scoped **+ mid-run steering**"으로 — 개입의 *종류*가 좁아졌음을 표에 기록.
- 표현 통일: "tax"→"phase penalty" 일원화, 반올림 정책 1회 명시, "shipped mode" 중복 5→2회, §6.5 버퍼 크기 명시, §6.9를 smoke test로 표현 조정, `[[bench …]]` 문법은 Appendix A로 전방 참조.
- **참고문헌 정비**: [10]·[11]·[12] 완성(한국어 잔존 텍스트 "비특허문헌" 제거), [23]·[26] 검증, Track E 인용 4건 추가, 익명화 [NOTE] 해소.

---

## 실행 순서·산출물 요약

| Phase | 작업 | 실험? | 모델 | 산출물 |
|---|---|---|---|---|
| 0 | A1 해명, D 위협모델, E 인용, F1-3 매핑 문장 | 없음 | — | 논문 §2.4, §4.3–4.4, §6.1, §6.10, §7.5 개정 |
| 1 | A2 E2b, F1 retry 민감도·N 스케일링, B3 통계 재계산 | mock | 불필요 | `e2b_injection.py`, `e2_hol.py` 확장 셀, `out/*` 신규 raw |
| 2 | C 완화 구현+측정, B1–B2 live 승격, F2 shell 지점 | live | on-prem 엔드포인트 | `--turn-scoping` 스위치 + 테스트, `p6_real_llm.py`·`p2_scope_real.py` 개정 |
| 3 | F3 Abstract·표·참고문헌 최종화 → **v0.9** | 없음 | — | `09-full-paper-draft.md` v0.9 (+ko) |

**의존성**: Phase 3은 Phase 1–2의 수치 확정 이후. Phase 0은 즉시 병행 가능. Track C의 코드 변경은 CLAUDE.md 규칙(테스트+ruff+ARCHITECTURE.md+단일 커밋) 적용 대상.

**리스크**: (1) C2 라이브 재실험에서 1/12 사건이 재현되지 않을 수 있음(희귀 사건) — scoping off 팔의 run 수를 12→24로 늘려 기저율을 먼저 안정화할지 결정 필요. (2) B1의 N축은 엔드포인트 동시성 한계에 의해 제약될 수 있음 — 한계 자체를 명세로 기록하는 것이 Q3의 답이므로 실패가 아님. (3) E2b에서 k=1 재현이 ±5%를 벗어나면 드리프트 통제 재측정(`e2-drift-control` 방식) 선행.
