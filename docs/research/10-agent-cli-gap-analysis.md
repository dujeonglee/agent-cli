# agent-cli 격차 분석 — Coagora(구 Aidit-Code) 기능 대비, 실험 이식 관점

> 작성일: 2026-08-05. 목적: 실험(08 문서 P1–P8)을 agent-cli 위에서 재구성할 때, **Coagora에만 있고 agent-cli에 없는 기능**을 식별하고 구현 난이도·대안을 평가.
> 근거: `01`(Coagora 분석), `03`(agent-cli 분석) + 본 세션에서 코드 재검증한 항목은 ✔ 표시.
>
> **[2026-08-05 개정 — 관계 재정의]** 프로젝트 관계가 확정됨: **agent-cli가 본류(mainline)이고 Coagora는 그 포크(fork)** 다. 따라서 본 문서의 격차는 "타 시스템 기능의 이식 대상"이 아니라 **"포크에서 개발·검증된 기능 중 아직 본류에 병합되지 않은 백로그"** 로 읽어야 한다. §3의 전략 α/β는 이 재정의로 폐기되었으며, 공식 병합 절차는 [`11-upstream-merge-plan.md`](11-upstream-merge-plan.md)가 대체한다. §1–§2의 격차 매트릭스와 실험 판정은 사실 관계로서 계속 유효하다.

## 0. 요약

agent-cli는 **다중 뷰어 공유 세션(직렬 계약)** 까지는 이미 갖추고 있으나, 논문의 핵심인 **병렬 턴 계층 전체가 부재**하다. 구체적으로 (1) 동시 inflight 턴/turnId 다중화, (2) 부수효과 계층 락, (3) 턴 단위 인터럽트, (4) cap·per-user 공정 큐, (5) 응답↔질문 1:1 귀속(replyToId)이 없다. 반면 P6(모의 LLM)·P7(장기 세션)·P8(SWE-bench)은 agent-cli가 **오히려 더 적합**하다. 결론: **P1의 직렬·거부 계약 측정과 P6–P8은 agent-cli로 즉시/저비용 가능, P1 병렬·P2·P3·P4·P5는 병렬 턴 아키텍처 구현(대공사)이 선행**되어야 한다.

## 1. 기능 격차 매트릭스

### A. 동시성 계약 계층 — 논문 핵심, 전부 부재

| # | Coagora 기능 | agent-cli 현황 | 격차 크기 |
|---|---|---|---|
| A1 | **동시 병렬 턴** — `activeTurns: Map<turnId, TurnSink>`, 독립 추론 즉시 개시, 동시 스트리밍 (`pi.ts:135-161`) | 1 워커 스레드 = 1 활성 턴. `InputQueue`는 순수 FIFO deque, 턴 경계에서 `dequeue_nowait()`로 하나씩 주입 ✔ (`input_queue.py:75-82`, `loop/core.py:602-636`) | **대** — AgentLoop가 단일 스레드 동기 설계. 워커 풀 + 턴 레지스트리 + per-turn 스트림 다중화 신설 필요 |
| A2 | **turnId 다중화** — 토큰·툴콜·ack가 (callId, turnId) 복합키로 턴별 sink 라우팅 | 없음. 렌더러 이벤트는 세션 단일 스트림. 서브에이전트 팬아웃(oneshot threading)은 있으나 **사용자 턴이 아닌 위임 작업** 단위 | 대 (A1에 종속) |
| A3 | **부수효과 계층 락** — `sandboxLock.ts` 호환성 행렬(경로별 병렬/셸·삭제 배타), 엄격 FIFO, `LOCK_SCOPE` 롤백 스위치 | 없음(단일 워커라 자연 직렬 — 락이 필요 없었음). 도구 실행에 인텐트 분류 계층도 없음 | **대** — 도구 호출에 intent 분류(`FILE_WRITE(path)`/`SHELL`/…)를 먼저 도입해야 락을 걸 수 있음 |
| A4 | **턴 단위 인터럽트** — `interrupt(turnId)`가 지정 턴만 취소, 타 턴 불간섭, 부분 본문 보존 | `/api/abort`·`/api/stop`은 **세션 전역** ✔ (`web/server.py:1157-1164`). 큐 대기분 취소는 소유자만 가능(`InputQueue.cancel`, conn_id 일치 ✔) | 중 (A1 이후엔 소) |
| A5 | **cap + per-user 1활성턴 + 라운드로빈 공정 큐** — `MAX_CONCURRENT_TURNS`, `hasActiveUser()`, 적격 항목 splice | FIFO만. per-user 게이트·cap·공정성 로직 없음 ✔ | 중 — InputQueue 확장으로 국소 구현 가능(단 A1 전제) |
| A6 | **응답↔질문 1:1 귀속** — `Message.replyToId` FK, AGENT_REPLY가 유발 HUMAN에 데이터 레벨로 결속 | `[닉네임]:` 라벨 + history 레코드 `author` 키만(다중 화자 트랜스크립트). 응답이 어느 메시지에 대한 것인지는 **직렬이라 암묵적** — 병렬화 즉시 소실됨 | 중 — history 스키마에 `reply_to` additive 필드 추가 |
| A7 | **활성 턴 수 기반 세션 상태** — activeTurns 카운트, RUNNING/IDLE 전이 보정 | busy/idle 단일 플래그 (`status.json`) | 소 (A1에 종속) |

### B. 신원·참여 모델

| # | Coagora 기능 | agent-cli 현황 | 격차 크기 |
|---|---|---|---|
| B1 | **사용자 계정/게스트 신원** — JWT(userId), 게스트 `#hex4` 전역 유일화, DB 귀속 | 계정 없음. 공유 토큰 1개 + 자율 닉네임(중복·사칭 자유). per-user 실험 통제(공정성 측정의 사용자 단위)에는 닉네임+conn_id로 충분하나 **보안 실험엔 부적합** | 중 (실험 목적엔 소) |
| B2 | **관전 모드** — 비로그인 SSE 구독(`optionalAuth`), 발화는 로그인 필요 | 토큰 보유자는 전원 입력 가능 — 읽기 전용 역할 없음 | 소 — 뷰어 role 플래그 추가 |
| B3 | **다중 방(post=세션) 내장** — 게시글 피드, 1:1 샌드박스 자동 프로비저닝, hot/new 정렬 | 1 프로세스 = 1 세션. 다중 방("board")은 **별도 프로젝트로 외부화**(`web.json` spawn-or-attach 계약만 존재, 03 §2.4) | 대 — 단, 실험엔 불필요(프로세스 N개 기동으로 대체) |
| B4 | **레이트리밋** | 없음 | 소 (실험 불필요) |

### C. 실행 환경·격리

| # | Coagora 기능 | agent-cli 현황 | 격차 크기 |
|---|---|---|---|
| C1 | **샌드박스 라이프사이클** — CREATING→READY→SUSPENDED(프로세스 종료·파일 보존)→resume | 세션 `--resume`이 유사 역할(디스크 세션 + 프로세스 재기동). 상태 기계·자동 suspend는 없음(IdleMonitor 자가 종료가 근사) | 소 — 실험 목적으론 등가 |
| C2 | **경로 탈출 방어(symlink-realpath)** — `pathGuard.realResolve()` inode 기준 검사 | `_confine.py`는 write/edit/shell 대상 "speed bump"로 자기 규정 — symlink 방어·shell 인자 내 경로는 미방어 (03 §3.4) | 중 (보안 주장 시), 실험엔 무관 |
| C3 | **ENV 화이트리스트(deny-by-default)** — 자식 셸에 allowlist만 전달, CI 키 유출 게이트 | 셸 도구가 부모 env 상속(화이트리스트 없음). 온프렘 로컬 전제라 위협 모델이 달랐음 | 중 (보안 주장 시) |
| C4 | **리소스 제한** — 툴 타임아웃 30s + 트리 킬, per-sandbox 프로세스 cap 16 | 셸 타임아웃은 있으나 프로세스 cap 없음 | 소 |

### D. 순서·재생·측정 인프라

| # | Coagora 기능 | agent-cli 현황 | 격차 크기 |
|---|---|---|---|
| D1 | **seq 단일 출처(DB 트랜잭션) + Last-Event-ID 결정적 재생** — 무제한, 귀속 보존 | 인메모리 이벤트 버퍼 **5,000건 상한** replay, 초과분 truncation ✔ (`render/web.py:57,169-173`). 영속 원본은 `history.jsonl`이지만 재생 경로는 버퍼 | 중 — 측정은 history.jsonl 파싱으로 대체 가능 |
| D2 | **9종 typed 이벤트 스키마** (tool.call/tool.result/file.changed…) | 렌더러 이벤트는 있으나 파일 변경 라이브 이벤트 없음(워크스페이스 트리는 폴링) | 소 |
| D3 | **HOL 벤치 하네스** — `mockLlm.mjs`(결정적 지연), `e2-hol.mjs`, `e1-ablation.mjs`, CDF 렌더러, 원자료 커밋 | 없음. 단 **bakeoff 하네스**(wire format A/B, 140run 게이트)와 **SWE-bench 하네스** 보유 — 실험 문화·골격은 존재 | 중 — **mockLlm.mjs는 OpenAI 호환 서버이므로 agent-cli의 base_url로 그대로 연결 가능**(재작성 불필요) |
| D4 | **결정적 STUB_MODE** | 테스트는 fake provider 사용. CLI 수준 stub 플래그는 없으나 mockLlm 연결로 등가 달성 | 소 |

### E. agent-cli에만 있는 것 (실험에 유리한 역격차)

| 기능 | 실험 활용 |
|---|---|
| **선착순 응답 게이트(409)** — ask/confirm 이중 응답 거절 ✔ (`web/server.py:1102-1111`) | Coagora에 없는 **다자 승인 중재 메커니즘** — RQ5(제어권 중재) 실험 소재로 역수출 가능 |
| **상주 서브에이전트 + 오케스트레이터 + 인간·에이전트 공용 메시지 버스** | N인간×M에이전트 확장 실험(논문 §7.4)의 유일한 실행 기반 |
| **컨텍스트 compaction** (토큰 예산, recursive single-call) | **P7(장기 세션·컨텍스트 포화)의 처치 변수** — Coagora는 compaction이 없어 대조군만 가능, agent-cli는 on/off 비교 가능 |
| **SWE-bench 하네스** | P8 그대로 실행 |
| **온프렘 LLM + wire format 플러그인 + recovery 계층** | 실 LLM 실험(P6)을 로컬 모델로 저비용 반복 가능 |
| **팀 스윔레인 시각화, Prompt Inspector** | 실험 중 관찰 도구 |

## 2. 실험별 실행 가능성 판정 (08 문서 P1–P8 기준)

| 실험 | agent-cli로 지금 가능? | 필요한 것 |
|---|---|---|
| P1 HOL 3계약 | **부분** — 직렬(현재 그대로) ✔, 거부(입력 시 busy면 409 반환하는 게이트 소규모 추가) 가능. **병렬 불가** | 병렬 셀만 A1 필요. 직렬·거부 2계약 비교는 1주 내 가능 |
| P2 혼합비 그리드 | 불가 | A1+A3 (병렬 턴 + 인텐트 분류 + 계층 락) |
| P3 락 ablation | 불가 | A3 + LOCK_SCOPE류 스위치 |
| P4 공정성·cap | 불가 | A1+A5 |
| P5 인터럽트 격리 | 불가 | A1+A4 |
| P6 실/모의 LLM 비용 | **가능** — mockLlm.mjs를 base_url로 연결, 온프렘 모델 반복 | 측정 로그 훅만 추가 |
| P7 장기 세션·포화 | **가능, 오히려 우월** — resume + compaction on/off 처치 | 없음 |
| P8 SWE-bench 협업 | **가능** — 기존 하네스 | 2-역할 스크립트 작성 |

## 3. 이식 전략 제안 (택1 또는 병행)

**전략 α — 역할 분담 유지 (권장, 저위험)**
Coagora = 병렬 계약 실험 플랫폼(P1–P5, 하네스 완비), agent-cli = 보완 실험 플랫폼(P6–P8 + compaction 처치 + 온프렘 반복). 논문 §6은 두 시스템 실측을 명시 구분 보고. 장점: 구현 0, 각 시스템의 강점 활용. 단점: "계약이 아키텍처 독립적"이라는 주장을 두 구현으로 보이지는 못함.

**전략 β — agent-cli에 병렬 계약 이식 (고비용, 고수익)**
구현 순서(의존 역순): ① 도구 인텐트 분류 + `reply_to` 귀속 필드(A6, 독립적·소규모) → ② 거부 게이트 + 측정 훅(P1 2계약 즉시 확보) → ③ AgentLoop 병렬화: 턴 워커 풀 + turnId 레지스트리 + 스냅샷 읽기/원자 커밋(`history.jsonl` append 직렬화는 이미 단일 프로세스라 유리) → ④ 계층 락(A3) + LOCK_SCOPE 스위치 → ⑤ cap·per-user 게이트(A5) → ⑥ 턴 단위 인터럽트(A4). ③이 임계 경로 — `loop/core.py`(834 LOC)·`dispatch.py`(1,329 LOC)의 단일 스레드 가정을 깨는 대공사이며, 상주 서브에이전트 스레딩 모델과의 간섭(공용 InputQueue, MailWaker) 검토 필수. 수익: **"동일 계약을 이질적 두 코드베이스(TS 서버형 / Python CLI형·온프렘)에 구현해 결과가 재현됨"** — 일반화 주장이 가능해져 논문이 한 단계 강해짐.

**판정 기준**: 논문 1 데드라인까지 여유가 8주 미만이면 α, 이상이면 β의 ①②까지만 먼저 하고(P1 2계약 + 귀속 확보) ③–⑥은 논문 2 시점에.

## 4. 재검증 메모

본 문서 작성 시 코드로 직접 확인한 사항(✔): `InputQueue`의 FIFO·소유자 취소·SHUTDOWN 센티널 구조, 세션 전역 `/api/abort`·`/api/stop`, 인메모리 5,000 이벤트 버퍼 상한, ask/confirm 선착순 409 게이트. 나머지는 03 문서의 파일:라인 근거를 신뢰하되, 전략 β 착수 시 `loop/core.py`·`dispatch.py`·`agents_live.py`의 스레딩 가정을 정독할 것.
