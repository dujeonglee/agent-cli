# 리뷰 대응 플랜 2 — v0.9 → v1.0

> 2026-08-07. 대상 리뷰: `15-reviewer-simulation-v0.9.md` (CHI 렌즈, Recommendation 3.0 경계선).
> 1차 플랜(`14-review-response-plan.md`)과 같은 원칙: 수치가 바뀌는 작업을 먼저, 표현은 마지막.
> 이번 라운드의 구조적 차이: **가장 큰 항목(Track G)이 실험이 아니라 "venue 결정 + 인간 연구"라서, 사용자 결정 없이는 착수할 수 없다.** 나머지 트랙(H, I)은 결정과 무관하게 전부 진행 가능하고, 어느 venue 로 가든 논문을 강화한다.

---

## 리뷰 요지 → 트랙 매핑

| 리뷰 항목 | 성격 | 트랙 |
|---|---|---|
| R2-W1 인간 증거 부재 / venue fit | **결정 + 연구 설계** | G |
| R2-W3 현실적 혼선 기저율 미지 | 라이브 실험 1팔 (저비용) | H3 |
| R2-W4 스테일니스 미측정 (1차 Q4 잔여) | 계측 + 재분석 (저비용) | H1 |
| R2-W5 혼합 워크로드 effect-layer 공정성 (1차 Q5 잔여) | 목 실험 1셀 (저비용) | H2 |
| R2-W6 통계 검정 부재 | 재계산만, 재실험 불필요 | H4 |
| 내부 불일치 4건 + minor 다수 | 글만 | I |
| R2-W2 abstract 과잉 | 글만 (수치 확정 후) | I |

---

## Track G — R2-W1: venue 결정과 인간 증거 (최대 항목, **사용자 결정 필요**)

리뷰어의 진단이 정확하다: v0.9는 "CSCW 서론을 입은 시스템 논문"이고, 10개 RQ 전부 기계로 답해진다. 선택지는 셋이고 상호 배타적이지 않다.

### G0. 결정 사항 (착수 전 사용자와 합의)

| 선택지 | 내용 | 비용 | 기대 효과 |
|---|---|---|---|
| **G-a. First-use study 추가 → CHI 정조준** | 3–5팀 × 2–3인, 실과제 1개, serial/parallel within-subject | 참가자 모집 + 세션당 ~1시간 + 분석 1–2주. **IRB/동의 절차 확인 필요** | 리뷰 기준 3.0 → 4.0+ 축. §7 가설이 처음으로 데이터와 접촉 |
| **G-b. UIST/시스템 트랙으로 재조준** | 현 평가 유지, 기고 유형을 artifact/systems 로 명시 (Ledo et al. CHI 2018 인용해 evaluation-by-demonstration 방어) | 글만 | 리뷰어 자백대로 "지금 상태로 champion 후보" |
| **G-c. 병행** | G-b 프레이밍을 지금 넣고, G-a 를 소규모로 실행해 §6.12 로 편입 | G-a 와 동일 | 어느 venue 든 최강. 추천 |

**추천: G-c.** 소규모 study 는 CHI 가 아니어도 논문을 강화하고(UIST 도 first-use 관찰을 환영한다), G-b 프레이밍 문장은 어차피 써야 한다. 단 G-a/G-c 는 참가자·일정·동의 절차가 필요하므로 **이 플랜에서는 설계만 확정하고 실행 여부는 사용자가 결정한다.**

### G1. First-use study 설계 (실행 시)

- **참가자**: 2인 1팀 × 4–6팀 (동료 개발자; 저자 제외). 팀당 1세션 ~60분.
- **과제**: 소형 리포에서 현실적으로 구분되는 2개 과제(서로 다른 모듈, 서로 다른 동사 — H3 워크로드 재사용)를 두 사람이 동시에 에이전트에게 지시. 한 번은 serial, 한 번은 parallel(순서 counterbalance, 과제 쌍도 교차).
- **측정 (기계측 — 이미 있는 계측 재사용)**: TTFT 실측, 개입(인터럽트) 사용 횟수, 혼선 사건(H3 판정기 재사용), 대기 중 재제출/포기.
- **측정 (인간측)**: 세션 후 7점 척도 설문(체감 응답성, 상대 작업 인지(awareness), 혼선 인지, 통제감), 반구조화 인터뷰 10분(§7.1–7.3 가설 각 1문항: 늦게 합류한 사람의 이해, 남의 턴을 멈추고 싶었던 순간, 상대 스트림을 실제로 읽었는가).
- **분석**: n 이 작으므로 기술 통계 + 사건 코딩 + 인용문. 가설 검정을 주장하지 않는다 — "first-use observations" 로 명명하고 §6.12 신설, §7 각 절에 관찰 1–2개씩 역참조.
- **수용 기준**: 세션 4개 이상 완료, 혼선·개입 사건이 기계 로그와 인터뷰 양쪽에서 교차 확인될 것. 결과가 계약에 불리해도(예: 사용자가 parallel 의 동시 스트림을 읽기 힘들어함) 그대로 보고 — §7.4 의 "새 articulation work" 주장의 첫 증거가 된다.

### G2. 프레이밍 (글만, 즉시 가능)

- §1 끝 또는 §6 서두에 기고 유형 명시 1문단: artifact + empirical systems contribution; 평가 방법론을 Ledo et al. [신규 인용] 의 demonstration 유형으로 위치 지정.
- §6.11 의 "not that teams using it collaborate better" 문장을 G1 실행 시 "a first-use study (§6.12) provides initial observations; controlled studies remain future work" 로 갱신.

---

## Track H — 저비용 측정 4건 (결정 불요, 즉시 착수 가능)

### H1. 스테일니스 측정 (R2-W4, 1차 Q4 잔여)

- **정의**: 턴 T 가 스냅샷을 읽은 시점의 컨텍스트 seq(또는 세대) = g_read, T 의 커밋 시점까지 다른 턴이 커밋한 블록 수 = staleness(T). staleness > 0 인 턴의 비율과 분포를 보고.
- **계측**: `--turn-metrics` 이벤트에 스냅샷 시점 컨텍스트 식별자 1필드 추가 (agent_cli 코드 변경 → **CLAUDE.md 규칙 적용**: 유닛 테스트, ruff, ARCHITECTURE.md, 단일 커밋).
- **측정 2단**: ① 기존 라이브 런의 히스토리(`out/` 커밋본)에서 커밋 순서×reply_to 로 소급 추정 가능한지 먼저 확인 — 가능하면 재실험 없이 답이 나온다. ② 불가능하면 목 3-user 워크로드 1회 + 라이브 scoping 런 구성 재사용 소수 회.
- **본문**: Limitation 5 를 한 줄 공개에서 측정 문단으로 교체. 정성 사례 1개(스테일 스냅샷 위에서 나온 답이 실제로 틀렸는가/무해했는가) 포함.
- **수용 기준**: "how often + 사례 1개" — 리뷰 질문 3에 문장으로 답할 수 있을 것.

### H2. 혼합 워크로드 effect-layer 공정성 (R2-W5, 1차 Q5 잔여)

- **설계**: 목, 2-user. A 는 5 s 셸 × 3회/턴(F2 구성 재사용), B 는 파일 쓰기 워크로드. B 의 effect-lock wait p50/p95 를 (i) A 동시 실행 vs (ii) B 단독 baseline 으로 비교. 예상: B 의 대기 상한 ≈ 셸 1회 hold 시간 (no-overtaking 의 가격).
- **산출**: `p4b_mixed_fairness.py` → `out/p4b-mixed.json`. §6.8 끝에 "admission fairness 가 다스리지 않는 층" 1문단 — §4.4 의 "deliberate sacrifice" 에 가격표를 붙인다. 결과가 나빠도(수 초 대기) 그것이 정직한 보고이고, §4.4 의 설계 논거(공정성 하드 보장)와 정합적이다.
- **수용 기준**: B 의 대기 분포가 셸 hold 시간과의 관계식으로 설명될 것.

### H2b. 수리 후 재검증 (Phase B 중 발견된 §4.3 배선 결함의 후속)

- **원칙**: 공개는 수정의 대체물이 아니다. 수리된 seam 위에서 영향권 실험을 재실행해 수치 유지 여부를 확인한다.
- **영향권**: 결함은 병렬 계약의 히스토리 레코드 순서에만 영향 → 재검증 대상은 히스토리 순서/트랜스크립트 내용에 의존 가능한 병렬 mock 실험(n3, n3b, n1, p4, p7). 타이밍만 읽는 실험(e2*, p2 락 대기)과 직렬 팔은 구조적으로 무관.
- **방법**: `--out out/postfix` 로 커밋 raw 를 덮지 않고 재실행, 구조 검사는 동일해야 하고 스케줄링 민감 수치(혼선률 등)는 기보고 범위 안이어야 한다. 결과를 §5 공개 문단에 "수리 후 재검증" 문장으로 반영.
- **라이브 잔여 → Phase C**: n3c 스코핑 20+20, §6.4 작동점 런은 엔드포인트 확보 시 수리된 코드로 재측정하고, 그때까지 §5 의 노출 문구를 유지한다.

### H3. 현실적 혼선 기저율 (R2-W3)

- **설계**: `n3c_scoping_real.py` 의 off 팔만, 워크로드를 "태그+파일명만 다른 쌍" → "현실적으로 구분되는 과제 쌍"(다른 모듈, 다른 동사; 예: A 는 `parser/` 버그 수정, B 는 `README` 절 추가)으로 교체. n=20, 판정기(reply_to×files 턴별 귀속) 재사용.
- **해석 분기**: 기저율이 낮으면(예: 0–2/40) → "혼선은 지시가 혼동 가능할 때의 현상이고 통상 작업에서는 드물다" 로 배치 스토리 강화. 높으면 → scoping 의 중요성 강화. **어느 쪽이든 논문이 좋아진다** (리뷰어 자신이 이렇게 명시했다).
- **선택 확장 (저비용)**: 리뷰 질문 5 — 기존 scoping 런 40+40 트랜스크립트에서 텍스트 수준 오답변도 소급 카운트 (재실험 없음, 판정 스크립트만).
- **산출**: §6.7 라이브 절에 "realistic-pair base rate" 1문단 + Limitation 3 갱신.
- **수용 기준**: off 팔 20 런 완주, 겹침 검증(≥65% 커버리지) 통과.

### H4. 통계 보강 (R2-W6, 재실험 불필요)

- Setup 에 정책 1문장: "쌍 비교는 Fisher's exact, 중앙값 구간은 BCa bootstrap 95% CI, n 은 각 표에 명시."
- 적용 지점: 26/40 vs 0/40 (Fisher p < 10⁻⁸), 14/20 vs 20/20, §6.8 76 ms vs 1,807 ms (bootstrap CI), §6.10 랭킹 12+12 (비겹침 범위 서술 유지 + CI 병기).
- 전부 커밋된 raw 에서 재계산. 스크립트는 `bench/multiuser/` 에 `stats_recompute.py` 로 남겨 재현 가능하게.

---

## Track I — 본문 결함·표현 (글만)

### I1. 내부 불일치 4건 (즉시, 무조건)

1. **§6.10 헤더**: "5 repetitions … 3 for token accounting" → "twelve … six" (본문과 일치시킴).
2. **§6.11 자기모순**: "no failures … did not trigger" 와 공존하는 "The single failure is …" 잔존 문장 삭제.
3. **§8 결론**: "write-heavy regimes (down to 1.10×)" → "effect-heavy (exclusive share ≥ 0.9)" — §6.3 의 1.10 은 셸 90% 지점이다.
4. **§8 결론**: "8.2% unlocked → 0%" 에 forced-overlap ablation 표기 복원 (abstract 와 정합).

### I2. Abstract 헤지 (R2-W2, H 트랙 수치 확정 후)

- slope 문장에 검증 표기: "…(slope 0.00 vs 1.03, validated on a deterministic harness and confirmed live)".
- 40/40 문장에 범위 단서: "…in 40 of 40 live turns *on one model and a deliberately confusable workload*" — H3 결과가 좋으면 "and the realistic base rate is X" 를 병기.
- 리뷰 제안 수용 검토: 마지막 문장을 어젠다 대신 mitigation 결과로 끝낼지 — abstract 250단어 상한 내에서 조정.

### I3. 정리 항목

- **변경 이력 블록**(문서 서두 ~1,200단어) → `docs/research/09-CHANGELOG.md` 분리. 논문 파일 첫인상 회복.
- 리뷰 질문 6: "500 concurrent turns" 산술 명시 (5 runs × 100 turns).
- Table 1 [NOTE] → 제출 포맷 작업과 함께 처리 (venue 확정 후).
- 참고문헌: [1]·[3]·[21]·[22] 저자 보완, 전체 venue-format 정규화. Ledo et al. (G2) 추가.
- [TODO: S1]: 이번 사이클에서 필드 로그를 얻을 수 없으면 문구를 "synthetic; a field workload distribution remains future work" 로 확정하고 TODO 태그 제거 — 태그를 남긴 채 제출하지 않는다.
- 익명화 [NOTE] 2건: venue 결정(G0)과 함께 처리.

---

## 실행 순서

| Phase | 작업 | 실험? | 의존성 |
|---|---|---|---|
| **A** | I1 불일치 4건, I3 changelog 분리, G2 프레이밍 문단 | 없음 | 없음 — 즉시 |
| **B** | H1 계측+측정, H2 혼합 공정성, H4 통계 재계산 | mock (+기존 raw) | H1 은 agent_cli 코드 변경 → CLAUDE.md 규칙 (테스트·ruff·ARCHITECTURE.md·단일 커밋) |
| **C** | H3 현실 기저율 + 텍스트 소급 카운트 + **H2b 라이브 잔여**(수리된 seam 위 n3c 20+20·§6.4 작동점 재측정) | live | 엔드포인트 가용 시 |
| **D** | **G0 결정** → (G-c 시) G1 study 실행·분석·§6.12 | 인간 | **사용자 결정 + 참가자·동의 절차** |
| **E** | I2 abstract 최종화, 참고문헌·익명화·포맷 → **v1.0** | 없음 | B–D 수치 확정 후 |

**리스크**: (1) H1 소급 추정이 불가능하면 계측 추가 후 재실행 비용 발생 — 라이브는 scoping 런 구성 재사용으로 최소화. (2) H3 에서 기저율 0 이 나오면 "n=20 에서 관측 0" 의 구간 보고(rule of three: 95% 상한 ≈ 15%)로 정직하게 서술 — 무사건을 무위험으로 쓰지 않는다. (3) G1 은 참가자 가용성이 병목 — 세션 4개 미만이면 §6.12 대신 부록의 pilot observations 로 강등하고 CHI 대신 G-b 를 택한다.

**이 플랜이 닫으면 v1.0 은**: 1차 리뷰 W1–W6 전부 + 2차 리뷰 R2-W2–W6 전부 해소, R2-W1 은 G0 결정에 따라 (G-a/G-c) 해소 또는 (G-b) venue 재조준으로 무효화.
