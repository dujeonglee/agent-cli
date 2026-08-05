# 신규 논문 초안 (Draft v0.1)

> 작성일: 2026-08-05.
> 기반 자료: `PATENT.html`(청구항 1–17, 해결 과제 ①②③), 기존 `PAPER.html`(Parallel Inference, Serial Side Effects), 본 research 폴더 01–06.
> 기존 논문과의 관계: 기존 PAPER.html은 "정보 불일치 제거"를 중심으로 한 **시스템 단독 논문**이다. 본 초안은 research 조사에서 확인된 학술 공백(단일 사용자 가정의 편재, 메신저형 도구의 조정 프로토콜 부재, CSCW 이론 격차)을 반영해 **설계 공간 분류학 + 동시성 계약 + 실증**의 3중 기여로 재프레이밍한 **새 논문**이다.

---

## 0. 제목

**주 제안 (영문):**
> **Multiplayer Coding Agents: Concurrency Contracts for Synchronous Multi-User Sharing of a Single LLM Agent Session**

**주 제안 (국문):**
> 멀티플레이어 코딩 에이전트: 단일 LLM 에이전트 세션의 동기적 다중 사용자 공유를 위한 동시성 계약

**대안 제목:**
1. *From Single-Player to Multiplayer: A Design Space and System for Shared LLM Coding Agent Sessions* — 분류학 기여를 전면에.
2. *Who Steers the Agent? Parallel Inference with Serialized Side Effects for Multi-User Coding Agent Sessions* — 제어권 질문을 전면에 (CHI/CSCW 지향 시).
3. *Beyond the @-Mention: Synchronous Shared Sessions as an Interaction Model for Team Coding Agents* — 메신저 대비를 전면에.

선택 기준: UIST/ICSE(시스템 트랙)이면 주 제안, CSCW/CHI면 대안 2. 기존 PAPER.html 제목("Eliminating Information Inconsistency…")과 겹치지 않도록 "정보 불일치"는 부주제로 강등한다.

---

## 1. Abstract (초안)

> AI 코딩 에이전트는 파일을 쓰고 셸을 실행하며 소프트웨어 팀의 일원이 되어가고 있지만, 현존 도구는 예외 없이 **단일 사용자(single-player) 상호작용**을 전제한다. 팀이 에이전트를 함께 쓰는 유일한 통로인 메신저 태그 방식(Claude Tag, Codex, Copilot, Devin, Cursor의 Slack 연동)은 스레드를 트리거로, 클라우드 샌드박스를 실행 공간으로 분리하는 비동기 위임 구조여서, 실행 중 개입이 불가능하고 다중 사용자의 동시 지시에 대한 조정 프로토콜이 없다. 본 논문은 (i) 다중 사용자–코딩 에이전트 상호작용을 **상태의 위치 × 동시성 계약 × 귀속 단위 × 개입 지점**의 4축 설계 공간으로 정식화하고, (ii) 이 공간에서 비어 있던 좌표 — 여러 인간이 하나의 살아있는 에이전트 세션을 동기적으로 공유 — 를 실현하는 동시성 계약 **"병렬 추론 + 직렬 부수효과(parallel inference, serialized side effects)"** 를 제안하며, (iii) 이를 구현한 시스템 Coagora로 계약을 실증한다. 동시 도착한 사용자 입력을 독립 턴으로 병렬 추론하되, 파일 쓰기·도구 실행·공유 컨텍스트 커밋은 충돌 단위 계층 락으로 직렬화한다. 실측(n=180)에서 후속 사용자의 첫 토큰 지연은 선행 작업 길이와 완전히 독립적이었고(TTFT~L 기울기 0.000; 직렬 계약 1.000, 거부-재시도 계약 1.010), 직렬 실행기 제거 시 74%였던 파일 무결성 위반은 계약 적용 시 0%였다. 통상의 기술 상식("공유 컨텍스트면 추론을 직렬화하라" — LangGraph double-texting, CopilotKit OpenTag의 409 거부)에 반하는 이 계약은, 응답성·작업공간 정합성·컨텍스트 일관성을 단일 공유 세션을 포기하지 않고 동시에 달성하는 최초의 설계이다.

(≈200 words 영문 번역본은 투고 시 작성)

---

## 2. 논문 구조와 절별 내용

### §1 Introduction

**서사 구조 (문단 단위):**
1. **훅**: 코딩 에이전트는 "혼자 쓰는 도구"로 설계되었으나 소프트웨어는 팀이 만든다. GitHub Next Ace의 문제 선언("2026년 초 현재 모든 코딩 에이전트는 single-player") + Claude Code Issue #60082(공유 세션 기능 요청)를 산업 수요의 1차 증거로 인용 (04 §6-a, 6-b).
2. **현존 팀 경로의 한계**: 메신저 태그 방식의 공통 구조(태그 → 스레드 컨텍스트 흡수 → 클라우드 샌드박스 → PR 회신)와 세 가지 구조적 한계 — 실행 중 개입 부재, 다자 조정 프로토콜 부재, 스레드 경계에 갇힌 컨텍스트 (02 §4–6). 정확성 주의: "일회성 실행"이 아니라 "자동 반복은 하되 인간의 실시간 개입 지점이 없다"로 서술 (02 §7).
3. **소박한 대안의 실패**: 그렇다면 세션을 그냥 공유하면 되는가? 통상 상식은 둘 중 하나를 강제한다 — 추론 직렬화(HOL blocking; 내 질문이 남의 15초 작업 뒤에 갇힘) 또는 세션 포크(격리 후 병합; 컨텍스트 분단과 병합 충돌). 상용 실시례: OpenTag의 스레드당 1-run 강제·409 거부 (01 §5, PATENT [0005]).
4. **본 논문의 주장**: 셋째 길이 존재한다 — 추론은 병렬, 부수효과만 직렬. 응답성(HOL 제거)·작업공간 정합(무잠금 동시 쓰기 0)·컨텍스트 일관(툴콜 짝 정합)을 단일 공유 세션 위에서 동시 달성 (PATENT 과제 ①②③).
5. **기여 목록**:
   - C1. 다중 사용자–코딩 에이전트 상호작용의 4축 설계 공간과 기존 도구 9종의 좌표화 (§3)
   - C2. "병렬 추론 + 직렬 부수효과" 동시성 계약의 정식화 — 스냅샷 읽기/원자 커밋, turnId 다중화, 충돌 단위 계층 락, per-user 공정성 (§4)
   - C3. 오픈소스 구현 Coagora와 결정적 재현 하네스 (§5)
   - C4. 3계약 비교 실측 — TTFT 독립성(기울기 0), 무결성 ablation(74%→0%), 거부-재시도 계약이 직렬보다도 나쁨(1.010) (§6)
   - C5. CSCW 이론이 예측하는 새 문제(비대칭 grounding, 다자 제어권 중재)의 도출과 후속 연구 의제 (§7)

### §2 Related Work

4개 군으로 조직 (04 문서를 압축):

- **2.1 단일 개발자 + AI** — Imai ICSE'22, Chen et al. 2025(comprehension 장벽), SWE-chat 2026(39% 제동, 에이전트는 스스로 멈추지 않음). 전 계열이 1 human : 1 agent를 암묵 전제함을 명시. SWE-chat의 제동 수치는 §7의 "다자에서 누가 제동하는가"로 연결.
- **2.2 메신저 위임형 팀 에이전트** — 5개 상용 도구의 공통 파이프라인과 벤더 분기(귀속 모델: 에이전트 자체 계정 vs 요청자 권한 차용 vs 이메일 매핑). GitHub 공식 문서의 "스레드 전체가 PR에 저장" 경고를 컨텍스트=유출 표면의 벤더 자인 사례로 인용 (02 §3.1).
- **2.3 다중 사용자 + AI (비코딩/근접 도메인)** — Lehmann CHI'26(문서 편집; 팀은 에이전트를 팀원이 아닌 도구로 흡수), Johnson CSCW'25, Koala IUI'25(발언권), **Daryanto 2026(2인간+1AI — 반드시 명시적 구별: 교육 맥락·제안 기반·단발 과제·상태 없음)**, PR 리뷰 연구군(비동기·사후 협업만). GroupMemBench(메모리 시스템조차 단일 사용자 전용).
- **2.4 에이전트 동시성 제어** — CoAgent(낙관·수리형, "락은 추론을 블로킹하므로 부적합"이라 교시 — 본 계약은 추론을 블로킹하지 않는 락으로 이 교시를 우회), DeLM, OCC 고전, LangGraph double-texting(거부·큐잉·중단·롤백 — 동시 실행 없음), OpenTag. 격리-후-병합 계열(브랜치별 에이전트, git worktree 병렬)은 1인간:N에이전트로 직교함을 명시.
- **2.5 이론 렌즈** — Clark & Brennan grounding, Gutwin & Greenberg workspace awareness, Schmidt & Bannon articulation work, Horvitz mixed-initiative. §7에서 재소환.

### §3 A Design Space for Multi-User Coding Agent Interaction (신규 — 기존 논문에 없음)

05 문서 §2를 정식화한 4축:

| 축 | 값 | 예시 |
|---|---|---|
| **D1. 상태의 위치** | 대화 밖(외부 샌드박스) / 대화 안(세션=실행 컨텍스트) | Codex cloud vs Aidit |
| **D2. 동시성 계약** | 거부(409) / 직렬(FIFO) / 배칭(단일 응답) / 격리-병합(OCC) / **병렬+직렬 부수효과** | OpenTag / v0.1 / Google 특허 / worktree 계열 / **본 논문** |
| **D3. 귀속 단위** | PR / 태스크 / 턴(메시지 1:1) | Copilot / Devin / Aidit `replyToId` |
| **D4. 개입 지점** | 발사 전·완료 후 / 실행 중(턴 단위) | 메신저 전 계열 / Aidit turnId 인터럽트 |

기존 도구 9종(Claude Tag, Codex, Copilot, Devin, Cursor, OpenTag, Ace, agent-cli 계열 공유 워커, Aidit v0.1/v2)을 좌표 배치한 표 1장. **채워지지 않은 좌표 (D1=안, D2=병렬+직렬, D3=턴, D4=실행 중)가 본 논문의 위치**임을 시각적으로 보인다. agent-cli의 "공유 워커 + 단일 큐" 모델(03 §2)은 D2=직렬의 성실한 구현 사례로 포함해 계열 내 스펙트럼을 보인다.

### §4 The Concurrency Contract (핵심 기술 절)

특허 청구항 1–10을 학술 서술로 변환:

- **4.1 모델**: 게시글–샌드박스–세션 1:1:1 결속(청구항 11), 복수 클라이언트 attach, 단일 생성–전원 fan-out. 형식화: 세션 S = (convo, workdir, turns), 턴 t_i는 독립 추론 단위.
- **4.2 병렬 추론**: 각 사용자 메시지 → 독립 턴, 즉시 개시, 동시 inflight, turnId 다중화(스트림 청크·툴콜·툴결과·인터럽트가 턴에 귀속; 청구항 4–6). 배칭 비채택 이유: 1:1 귀속·입력별 추론 파라미터·턴 단위 중단 윈도우 보존 (PATENT 과제 라).
- **4.3 직렬 부수효과**: 모든 파일 쓰기·도구 실행·컨텍스트 커밋이 단일 직렬화 수단을 통과(청구항 1, 3). OCC와의 대비 — 충돌 검출·롤백·보상 트랜잭션·병합이 **부재에 의해** 불필요(사전 방지 vs 사후 수리). last-wins는 물리 충돌이 아닌 논리 결과의 순서 규칙.
- **4.4 충돌 단위 계층 락 (v2 개선)**: 샌드박스 전역 mutex → 호환성 행렬(경로별 FILE_WRITE/READ 병렬, SHELL/PACKAGE/DELETE 배타)로 입도 세분화. 배타 판정의 설계 근거(셸의 정적 분석 불가, 삭제의 ENOENT 레이스 클래스)와 엄격 FIFO 공정성(추월 금지) (01 §2.3-d). E2-B의 1.07× 붕괴가 이 세분화의 동기였음을 서술 — 실측이 설계를 갱신한 사례.
- **4.5 공유 컨텍스트 정합**: 스냅샷 읽기(개시 시점 convo.slice) + 완료 순 원자 커밋 + assistant/tool 짝 정합 불변식(청구항 2, 15). 부분 비최신성(staleness)은 알려진 한계로 정직 개시.
- **4.6 부하·공정성**: cap(기본 4) + 초과분 무거부 공정 큐 + per-user 1활성턴(병렬은 사용자 간에만; 청구항 7). 거부(409) 계약과의 대비는 §6에서 실측으로.
- **4.7 상태·수명**: 활성 턴 수 기반 RUNNING/IDLE, suspend/resume(청구항 8), opt-in 게이트 1회 확정(청구항 9), seq 단일 출처 정렬·재생·멱등(청구항 10).

### §5 Implementation

01 문서 §3–4 압축: Node/Fastify + SSE(WebSocket 비채택 근거 — Last-Event-ID 표준 재생), 워커 프로세스 분리("권한 경계는 서버"), 툴 ack의 (callId, turnId) 복합키 라우팅, 격리 스택(pathGuard의 symlink-realpath 검사, ENV deny-by-default — 실제 키 유출 사고와 수정 서사 포함 가능), 키 서버 내재화(청구항 12–13). 결정적 STUB_MODE와 커밋된 원자료 — 아티팩트 평가 지향.

### §6 Evaluation

기존 실측 재사용 + 08 문서의 신규 실험으로 보강:
- **E-HOL**: 3계약 × L∈{2,6,15}s, n=180 — TTFT 기울기 0.000/1.000/1.010. 핵심 주장: 배수가 아니라 **독립성(기울기)**.
- **E-INTEG**: 락 ablation 74%→0%.
- **E-MIX** (신규, 08 §3.2): 워크로드 혼합비에 따른 이점 보존/붕괴 경계 — 1.07×(전원 파일 수정)~63.5×(추론 중심)의 스펙트럼을 혼합비 함수로 매핑.
- **E-SCALE / E-FAIR** (신규): 사용자 수·cap 민감도, 사용자별 대기 분산.
- 브라우저 E2E(다중 스트림 UI), 장애 격리(fire-and-forget 사고 사례).

### §7 Discussion: What Sharing a Session Creates (신규 — CSCW 연결)

- **비대칭 grounding**: 세션 누적 common ground에 중간 참여자가 갖는 격차. seq 재생은 "다시 보기"지 "이해 따라잡기"가 아님 — 열린 설계 문제로 제시 (05 §3.2, 06 RQ4).
- **다자 제어권 중재**: per-user 게이트·턴 단위 인터럽트("남의 턴은 나를 잠그지 않고, 나는 남의 턴을 중단할 수 없다")는 하나의 정책 선택일 뿐 — 승인·되돌리기 권한의 일반 문제 제기 (06 RQ5). SWE-chat 39% 제동의 다자 버전.
- **peer accountability 가설**: Daryanto의 발견(동료 가시성 → AI 산출물 검증 증가)이 자율 에이전트 속도에서 유지되는가 (06 RQ6).
- **Limitations**: N배 토큰 비용, 컨텍스트 용량 포화, 논리적 last-wins 레이스, detach 프로세스는 락 밖, 스냅샷 staleness, 단일 인스턴스 전제, PoC 격리 수준, steer+concurrent 미지원, 동시 모드 tool 귀속 근사 (01 §7, PATENT 모두(冒頭) 주의문 재사용).

### §8 Conclusion + Artifact Availability

---

## 3. 기존 산출물과의 차별화 관리 (자기표절·중복 회피)

| 항목 | 기존 PAPER.html | 본 초안 |
|---|---|---|
| 중심 주장 | 정보 불일치 제거 | single-player 가정의 타파 + 설계 공간 + 동시성 계약 |
| Related Work | 시스템 계열 중심 | CSCW/HCI 4군 + 메신저 도구 실측 조사(02) 추가 |
| 신규 절 | — | §3 설계 공간, §7 CSCW discussion |
| 평가 | E-HOL, E-INTEG | + E-MIX, E-SCALE, E-FAIR (08 문서) |
| 특허와의 관계 | — | 청구항은 장치 서술, 논문은 계약 정식화·비교·이론 연결. 특허 출원(2026-06-29)이 논문 공개보다 선행하므로 신규성 상실 문제 없음(발명자 동일 전제; 국가별 grace period는 별도 확인) |

**주의**: 기존 PAPER.html을 투고한 적이 있다면 본 초안은 major revision/재프레이밍으로 처리해야 하며, 동시 이중 투고는 불가. 미투고 초안이라면 본 초안이 이를 대체·흡수한다.

## 4. 남은 작업 체크리스트

1. Abstract 영문화 + 기여 문장 압축 (C1–C5).
2. §3 좌표 표를 그림 1(2×2 또는 표)로 시각화.
3. §6 신규 실험 실행 (08 문서 P2, P3, P4 우선).
4. Daryanto·Wang position paper 원문 정독 후 §2.3 인용 확정 (04 §9 미확인 출처 검증 포함).
5. venue 확정: UIST 2027 / ICSE 2027 SEIP·기술 트랙 / CSCW 2027 중 택1 — §7 비중이 결정.
6. 아티팩트 패키지: bench 하네스 + 원자료 + STUB_MODE 재현 스크립트 + (가능 시) 익명화 세션 로그.
