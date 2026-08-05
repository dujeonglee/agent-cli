# Coagora 기술 분석 보고서

> 조사 기준일: 2026년 8월. 대상: D:\yoon\codes\Aidit\Coagora. 활용처: 논문 시스템 섹션.

## 0. 요약

Coagora는 **"게시글 하나 = 살아있는 코드 에이전트 세션"** 모델의 협업 코딩 플랫폼이다. 게시글마다 호스트 디렉토리로 격리된 샌드박스가 1:1로 생성되고, 그 안에서 도는 코드 에이전트(pi agent)에 여러 사용자가 동시에 attach하여 SSE로 토큰·도구 호출·파일 변경을 실시간 공유한다.

학술적 핵심 주장은 **"병렬 추론 + 직렬 부수효과(parallel inference, serialized side effects)"** 계약이며, 이것이 논문(`docs/PAPER.html`)과 특허(`docs/PATENT.html`)의 중심 명제다. 리포지토리에는 이 주장을 뒷받침하는 실측 하네스와 원자료가 커밋되어 있다(`backend/bench/`, `backend/bench/out/`).

메신저(Slack/Discord/Telegram) 연동 코드는 **존재하지 않는다.** Slack은 `docs/IMPLEMENTATION_NOTES.md:725,733,736`에서 선행기술(CopilotKit OpenTag) 비교 대상으로만 언급된다 — 후술 §5.

---

## 1. 프로젝트의 핵심 목적과 주요 기능

### 1.1 문제 정의 (근거: `README.md` §1, `CLAUDE.md`)

1. **v0.1** — 여러 사람이 하나의 살아있는 코드 에이전트 세션을 공유하며 함께 지시·관전·스티어링할 공간이 없다. → "게시글 = 에이전트 세션" 모델로 해결.
2. **v2 (핵심 차별점)** — v0.1의 **머리-막힘(head-of-line blocking)**. 동시 질문이 단일 활성 턴 + FIFO 큐로 직렬화되어 내 질문이 남의 긴 작업 뒤에 대기한다. → **병렬 추론 + 직렬 부수효과** 모델로 해결.

### 1.2 측정된 효과 (`README.md`, `docs/EXPERIMENTS.md`, `backend/bench/out/`)

A가 길이 L의 작업을 시작하고 1초 뒤 B가 한 줄 질문을 던졌을 때 B의 첫 토큰까지 걸린 시간(n=180):

| 동시성 계약 | L=2s | L=6s | L=15s | TTFT~L 기울기 |
|---|---|---|---|---|
| 직렬 (v0.1 FIFO) | 1.93s | 5.93s | 14.93s | 1.000 (완전 종속) |
| 거절 + 재시도 | 2.26s | 6.30s | 15.39s | 1.010 (더 나쁨) |
| **병렬 (v2)** | **0.24s** | **0.24s** | **0.24s** | **0.000 (완전 독립)** |

논문적으로 중요한 지점은 배수(L=15s에서 63.5배)가 아니라 **기울기 0** — 남의 작업 길이가 내 대기시간에 영향을 주지 않는다는 점이다.

**E1 ablation(부수효과 안전성)**: 직렬 실행기를 우회하면 같은 파일 동시 쓰기에서 위반률 **74%**(두 writer 마커 공존, 3회 모두 최종 파일 오염), 락 적용 시 **0%**.

**정직하게 문서화된 한계**: 위 수치는 동시 질문이 **추론 중심**일 때다. 동시 질문이 **모두 파일을 수정**하면 직렬 실행기가 병목이 되어 이점이 **1.07×(결정적)~1.35×(실 LLM)** 로 축소된다(`docs/EXPERIMENTS.md` §E2·E2-B).

### 1.3 주요 기능

- **인증**: username+password 또는 **게스트 진입**(닉네임만, 서버가 `#hex4` 부여). JWT 슬라이딩 갱신(7일). BYOK 전면 폐기 — LLM 키는 서버 `.env`에만.
- **홈**: hot/new 정렬 게시글 피드, 커서 기반 무한 스크롤.
- **글 작성 → 샌드박스 1:1 자동 생성**: CREATING→READY 비동기 프로비저닝, AI 1차 답변 옵션, v2 동시 협업 opt-in 체크박스.
- **협업 채팅 스레드**: 5종 버블(HUMAN / AGENT_REPLY / TOOL_CALL / TOOL_RESULT / SYSTEM), 전원 SSE 실시간 중계.
- **AI on/off 토글**, **워크스페이스 파일 트리**(`file.changed` 라이브 갱신), **인터럽트/스티어링**(v2에서는 특정 `turnId` 대상), **샌드박스 라이프사이클**(create → attach → run → interrupt → suspend → resume → cleanup), 추천·북마크·프로필·이미지 첨부·i18n(KO/EN).

---

## 2. 다중 사용자 세션 관리 구조

### 2.1 사용자 식별

**파일**: `backend/src/routes/auth.ts`, `backend/src/plugins/auth.ts`, `backend/prisma/schema.prisma`

- 세 가지 진입: `POST /auth/register`(bcrypt), `POST /auth/session`(로그인), `POST /auth/guest`(닉네임만).
- 게스트는 서버가 `randomBytes(2)` 기반 `#hex4` 접미사를 붙여 전역 유일화(`auth.ts:28-30, 92-104`). 예: `철수#a3f9`.
- JWT 페이로드는 `{ userId, username }` 만 — LLM 키 절대 미포함(`plugins/auth.ts:24-28`).
- `requireAuth` / `optionalAuth` 두 preHandler (`plugins/auth.ts:53-88`).

### 2.2 세션 생성/공유/참여 방식

**핵심 설계**: 별도의 "멤버십/참여자" 모델이 **없다**. 참여는 곧 **게시글 진입**이며, 접근 제어는 두 단계로만 구성된다.

| 동작 | 라우트 | 인증 수준 | 의미 |
|---|---|---|---|
| 관전(구독) | `GET /posts/:id/stream` | `optionalAuth` | 비로그인도 실시간 관전 가능 |
| 메시지 전송 | `POST /posts/:id/messages` | `requireAuth` | 로그인만 하면 누구나 발화 |
| 세션 시작/attach | `POST /posts/:id/session` | `requireAuth` | 작성자 여부 무관 |
| 글 수정/삭제 | `PATCH`/`DELETE /posts/:id` | `requireAuth` + 작성자 검사 | 비작성자 403 |

즉 **글 소유권은 글 자체에만 적용되고, 세션 참여는 개방형**이다.

**세션 확보 임계구역**: `backend/src/agent/sessionStart.ts` — `startOrAttach()`가 lookup→attach→정규화→spawn→AgentSession 생성 전 과정을 캡슐화(`sessionStart.ts:99-121`). 활성 세션이 있으면 `runtime.attach()`로 **새 프로세스 없이 기존 자식 프로세스를 공유** — 다중 클라이언트 fan-out의 물리적 근거(`pi.ts:517-525`). stale RUNNING 복구("Race B") 및 fresh-start 폴스루 포함.

### 2.3 동시성 처리 — 4계층 방어

#### (a) 세션 시작 coalescing — per-sandbox mutex
`sessionStart.ts:74, 102-120`. `inFlight: Map<sandboxId, Promise>`. 같은 샌드박스에 대한 동시 호출은 하나의 in-flight Promise로 합쳐진다. `pi.ts` spawn 자체도 멱등(`pi.ts:399-405`, `:442-449` — loser 자식 SIGTERM).

#### (b) 턴 디스패치 — 이중 경로 (레거시 FIFO vs. v2 병렬)
**파일**: `backend/src/agent/pi.ts`

`RuntimeHandle`(`pi.ts:135-161`)이 두 자료구조를 완전 분리 보유:

```
activeTurn:      TurnSink | null          ← 레거시 단일 활성 턴
queue:           QueuedTurn[]             ← 레거시 FIFO 대기열
activeTurns:     Map<turnId, TurnSink>    ← v2 concurrent 활성 턴 레지스트리
concurrentQueue: ConcurrentQueuedTurn[]   ← v2 cap 초과 대기열
turnSeq:         number                   ← turnId 단조 카운터
```

분기점은 `send()`의 `concurrent` 인자(`pi.ts:536-584`). **v2 opt-in 게이트**: `Sandbox.meta` JSON의 `concurrentTurns` 플래그, 글 생성 시 1회 확정. meta 손상 시 안전측 false(직렬) 폴백(`service.ts:79-91`).

#### (c) 부하 제어 — XC-CAP (cap + per-user 1활성턴 + 공정 큐)
`pi.ts:206-269`

- **절대 cap**: `config.maxConcurrentTurns`(기본 4).
- **per-user 1활성턴**: `hasActiveUser()`(`:212-218`) — `activeTurns`가 단일 진실원천.
- **공정 큐**: `pumpConcurrent()`가 큐를 head→tail 스캔, 첫 적격 항목을 splice 디스패치 — **기아 방지**.
- `turnId`는 **디스패치 시점** 부여(`dispatchConcurrent`, `:243-269`).

#### (d) 부수효과 직렬화 — XC-SERIAL + XC-SCOPE (계층 락)
**파일**: `backend/src/agent/sandboxLock.ts` — 논문의 핵심 기여물

E2-B 실측(파일 수정 동시 턴에서 1.07×로 붕괴) 후 직렬 경계를 "샌드박스"에서 "**충돌 단위**"로 좁힌 **호환성 행렬 기반 계층 락**(`sandboxLock.ts:16-18`):

| 조합 | 결과 |
|---|---|
| FILE_WRITE/READ(경로 P) ↔ FILE_WRITE/READ(경로 Q≠P) | **병렬** |
| FILE_WRITE/READ(P) ↔ FILE_WRITE/READ(P) | 직렬 |
| 그 외 모든 조합(SHELL/PACKAGE/FILE_DELETE/미지) | 직렬(배타) |

설계 근거(코드 주석): SHELL/PACKAGE 배타 — 파이프·변수전개·서브셸 때문에 접촉 파일을 정적으로 알 수 없음. FILE_DELETE 배타 — `rm -r src/`와 `write src/x.py`의 병렬 진입이 새 ENOENT 레이스 클래스를 만들기 때문.

**공정성**: 샌드박스별 **엄격 FIFO(추월 금지)** — 큐 머리가 호환되지 않으면 즉시 break(`:76-87`). **경로 키 정규화**: `normalizeLockPath()`(`:60-63`) — win32 소문자화 포함. **롤백 스위치**: `LOCK_SCOPE=sandbox` 환경변수(`config.ts:128-129`). **호출 지점**: `turn.ts:253-255`의 `withScopedSandboxLock`, `lockScopeFor()`는 모호하면 배타(`turn.ts:109-117`).

#### (e) 공유 컨텍스트 정합 — 스냅샷 읽기 + 원자 커밋
**파일**: `backend/src/agent/piWorker.mjs`

- `convo`는 세션 단일 공유 배열(`:192`). 턴별로 분리하지 않는다.
- **읽기 = 스냅샷**: 각 step 시작 시 `convo.slice()` (`:360`).
- **쓰기 = 직렬 커밋**: `commitToConvo()`가 `committing` Promise 체인 위에 push를 직렬화(`:194-231`).
- **짝 정합 불변식**(`:381-383`): `assistant`(tool_calls)와 대응 `role:tool` 메시지는 반드시 같은 커밋으로 함께 push — 쪼개면 동시 턴이 끼어들어 OpenAI 400.

이것이 "단일 공유 컨텍스트를 유지하면서 병렬 추론"의 구현 실체 — 격리 후 병합(merge) 계열과 구별되는 지점.

### 2.4 상태 동기화 메커니즘

#### seq — 순서의 단일 출처(SoT)
`backend/src/domain/seq.ts` — `nextSeq(tx, postId)`는 반드시 Prisma 트랜잭션 안에서 `max(seq)+1`. `@@unique([postId, seq])`가 backstop.

#### SSE fan-out + 재생
`backend/src/realtime/stream.ts`, `pubsub.ts` — 채널 = postId. `InMemoryPubSub`은 publish 시 스냅샷 순회 + 예외 격리. 주석에 "L10에서 Redis pub/sub로 교체될 seam" 명시(`pubsub.ts:5-6`). **재생**: `Last-Event-ID` 또는 `?afterSeq=` 기준 DB 스냅샷 재생 후 라이브 구독 전환(`stream.ts:77-114`). heartbeat 15초.

#### 이벤트 스키마 (9종 discriminated union)
`realtime/events.ts` — `sandbox.status`, `session.status`, `message.created`, `agent.token`, `message.updated`, `tool.call`, `tool.output`, `tool.result`, `file.changed`. 모든 이벤트는 `makeXxxEvent()` 빌더 경유 — **키 필드를 넣을 구조적 경로가 없음**.

#### activeTurns — 권위 카운트 전파 (RT-MULTI)
`session.status` 이벤트에 옵셔널 `activeTurns` 정수. 서버 계산 `pi.ts:736-740`. RUNNING 전이 시 +1 보정(`turn.ts:174`), IDLE 전이는 마지막 턴에서만(`turn.ts:339-340`).

#### 클라이언트 측 동기화
`frontend/src/stream/useThreadStream.ts`, `stores/threadStore.ts`, `lib/threadSelectors.ts`
- `EventSource` 자동 재연결 + `Last-Event-ID` 재전송, store가 중복 흡수.
- **이중 dedupe**: id 1차, seq 2차(`threadStore.ts:159-183`). `reconcileByClientId`로 낙관적 버블 화해.
- **멱등 전송**: `clientId` 기반 `findFirst` (`messages.ts:152-160`).

#### 1:1 귀속과 UI 게이팅
`frontend/src/lib/threadSelectors.ts` — 순수 함수.
- `buildAttribution()`: AGENT_REPLY를 `replyToId`→HUMAN 체인으로 질문자에 귀속.
- `hasMyActiveTurn()`: self-concurrency=1 게이팅. **남의 턴은 절대 나를 잠그지 않는다**(HOL 제거의 UI 측 표현).
- `attributeToolBubble()`: TOOL_CALL/RESULT는 seq 순서상 직전 AGENT_REPLY에서 상속 — **단일 턴 정확, 동시 모드 근사** (문서화된 한계, `:101-106`).

#### 레이트리밋
`backend/src/plugins/rateLimit.ts` — 인메모리 고정 윈도우. posts 30회/분, messages 120회/분.

---

## 3. 에이전트 실행 구조

### 3.1 전체 파이프라인

```
POST /posts/:id/messages (routes/messages.ts)
  → tx: nextSeq + HUMAN 영속화 → message.created publish → 201 즉시 응답
  → void runAgentTurn(...)                              [fire-and-forget]
       ↓
    agent/turn.ts — AGENT_REPLY(PENDING, replyToId) 생성 → session RUNNING
       ↓
    agent/pi.ts — pumpConcurrent(cap+per-user 게이트) 또는 pumpQueue(FIFO)
       ↓  stdin: {type:'input', turnId?, text, lang}
    agent/piWorker.mjs (child process) — function-calling 루프
       ↓  fetch → OpenAI-compatible /chat/completions (stream:true)
       ↓  stdout: {type:'token'|'tool'|'done'|'error', turnId?}
    pumpTurnLines → sink 라우팅 → onToken/onTool
       ↓
    agent/toolBridge.ts ← withScopedSandboxLock(XC-SERIAL)
       ↓
    agent/toolExec.ts — 실제 fs/shell + pathGuard
       ↓
    realtime/publish.ts → pub/sub → SSE → 전 참가자
```

### 3.2 에이전트 루프 (`piWorker.mjs`)

`runLlmAgent()`(`:342-411`) — OpenAI function-calling 루프, 최대 `MAX_AGENT_STEPS`(기본 8). 턴별 `AbortController` 스트리밍. **STUB_MODE**(`:54-60`): `AGENT_STUB=1`/vitest/키 누락 시 결정적 에코 모드 — **실 LLM 키·네트워크 없이 전체 테스트와 벤치마크 재현 가능**. LLM 타임아웃 `AGENT_LLM_TIMEOUT_MS`(기본 60초). 컨텍스트 트리밍은 `role:tool` 경계 보호(`:211-214`).

### 3.3 도구 정의와 권한 경계

**LLM 노출 도구 4종**: `write_file`, `read_file`, `delete_file`, `bash`.

**결정적 설계**: 워커는 fs/shell을 직접 건드리지 않는다. 워커는 도구 *의도*만 방출하고, 실제 효과는 부모 서버 프로세스(`toolExec.ts`)가 낸다 — "권한 경계는 서버"(`piWorker.mjs:13-14`).

**턴별 도구 ack 프로토콜**: `{type:'tool', ..., callId, turnId?}` → 서버 실행 → `{type:'tool-done', turnId?, result}` → `keyOf(msg)`로 정확한 턴의 resolver 호출. `turnId` 누락 시 영구 hang 위험을 명시적으로 봉합(`pi.ts:596-599`, `turn.ts:262-264`).

**인자 검증 게이트**(`piWorker.mjs:388-394`): 빈/불량 인자는 인텐트를 방출하지 않고 에러 문구를 LLM에 되먹여 재시도 유도.

### 3.4 권한/승인 흐름

**대화형 도구 승인(human-in-the-loop approval) 단계가 없다.** 대신 **격리 경계로 안전성을 확보**: *"호스트 디렉토리 격리(path)가 격리 경계. 내부는 모든 permission 허용"* (`schema.prisma` Sandbox 주석).

승인 대신 작동하는 4개의 강제 장치:

1. **경로 탈출 차단** — `sandbox/pathGuard.ts`. 위협 모델 3종: `..` 트래버설 / 절대경로 주입 / symlink 탈출. `realResolve()`(`:38-58`)가 존재하는 가장 가까운 조상까지 realpath로 풀고 비존재 꼬리를 이어붙여 실제 inode 기준 검사.
2. **ENV 화이트리스트 (XC-ENV)** — `toolExec.ts:81-120`. **실제 발생 결함 기록**: 과거 `echo $API_KEY` 출력이 TOOL_RESULT 버블로 전원에게 SSE 스트리밍됐음. 수정: deny-by-default, `ENV_ALLOW_COMMON` + `ENV_ALLOW_WIN`만 전달. 회귀 테스트 + CI 게이트 `backend/scripts/key-grep-gate.mjs`.
3. **리소스 제한 (XC-ISO)** — `sandbox/limits.ts`. 벽시계 타임아웃(30초, Windows는 `taskkill /T /F` 트리 킬), per-sandbox 프로세스 cap(16). *"cgroup/메모리/CPU 쿼터는 Windows 이식 불가라 흉내내지 않는다(가짜 제한 금지)"* — 한계 정직 문서화. 컨테이너 격리는 PoC만(`backend/bench/docker-isolation-poc.mjs`).
4. **삭제 가드** — `sandbox/service.ts:158-165`.

### 3.5 LLM 연동과 키 격리

**키 격리 3중 장치**: `redactConfig()`(`config.ts:146-165`), `redactSpawnEnv()`(`pi.ts:372-381`), 이벤트 빌더 구조 차단. 키는 `buildInjectedEnv()`(`pi.ts:349-366`)로 **오직 child process env로만** 주입.

### 3.6 오류 처리 — 프로세스 다운 사고 기록

`turn.ts:70-94`: fire-and-forget 예외가 unhandled rejection → Node 20+ 프로세스 즉시 종료. "턴 진행 중 게시글 삭제" → P2025 → 서버 다운이 **E2 하네스 180런 중 39런 연쇄 실패로 발견**됨. 수정: 최상위 래퍼 흡수 + P2025는 정상 조기 종료로 해석. **벤치마크 하네스가 프로덕션 버그를 발견한 사례.**

**점진 영속화**(`turn.ts:185-225`): 인터럽트가 부분 본문을 보존. **프로세스 사망 시 4개 자료구조 일괄 마감**(`pi.ts:470-491`).

### 3.7 인터럽트/스티어링

`POST /posts/:id/interrupt` → `runtime.interrupt(session, steer?, turnId?)`.
- **turnId 지정(v2)**: 그 턴만 취소, 다른 동시 턴 불간섭. steer+concurrent 조합은 미지원(순수 취소만, `pi.ts:638`).
- **미지정(레거시)**: activeTurn 취소, steer 주입 또는 큐 이어 처리.
- stale done/error 이중 억제(워커 + 부모). 부분 본문은 COMPLETE로 확정 보존.

---

## 4. 아키텍처 개요

### 4.1 기술 스택

| 영역 | 스택 |
|---|---|
| 백엔드 | Node 20 + TypeScript(ESM), Fastify 5, Prisma 6 + SQLite, JWT + bcryptjs |
| 실시간 | **SSE** + 인메모리 pub/sub (WebSocket 미사용) |
| 에이전트 런타임 | pi — child process 워커, OpenAI-compatible 스트리밍 |
| 프론트엔드 | React 18 + Vite 5 + TS 5, zustand 4, Tailwind (CRT 팔레트), marked + dompurify |
| 테스트 | Vitest 2 (backend 35개 + frontend), E2E 단언 스크립트 |

### 4.2 통신 방식 — WebSocket이 아니라 SSE

**하향**: SSE 단일 스트림 (`EventSource` 자동 재연결 + `Last-Event-ID` 재생). **상향**: 일반 HTTP POST. 논문적 의의: 재연결/재생 로직을 직접 구현할 필요가 없고, 이벤트는 SoT(DB)의 보조 알림 채널일 뿐(무상태 서버 원칙, `pubsub.ts:7-8`).

### 4.3 백엔드 모듈 구조 (`backend/src/`, 총 ~5,600 LOC)

| 디렉토리 | 역할 | 주요 파일 |
|---|---|---|
| `agent/` | 에이전트 런타임 어댑터 | `pi.ts`(742), `piWorker.mjs`(651), `turn.ts`(404), `toolExec.ts`(404), `sessionStart.ts`(199), `sandboxLock.ts`(145), `toolBridge.ts`(135) |
| `realtime/` | 이벤트 스키마·pub/sub·SSE | `events.ts`, `pubsub.ts`, `publish.ts`, `stream.ts` |
| `domain/` | 순수 도메인 로직 | `seq.ts`, `hotScore.ts`, `cursor.ts`, `toolCall.ts` |
| `sandbox/` | 프로비저닝·격리 | `pathGuard.ts`, `limits.ts`, `provision.ts`, `service.ts` |
| `routes/` | HTTP 라우트 11개 모듈 | `posts.ts`(374), `messages.ts`(298), `session.ts`(184) 등 |
| `plugins/` | Fastify 플러그인 | `auth.ts`, `rateLimit.ts` |

의존성 방향 통제: `stream.ts`는 이벤트 소비만("writer와 결합 금지"), `pi.ts`는 messageId/seq를 모르고 토큰만 흘림(이벤트 조립은 `turn.ts` — "시임 설계").

### 4.4 데이터 모델 (`backend/prisma/schema.prisma`)

```
User → Post → Sandbox(1:1) → AgentSession → { Message, ToolCall }
```

- `Sandbox.postId @unique` — 1:1 강제. `path`가 격리 경계, `meta`가 XC-MODE 플래그.
- `AgentSession` — Sandbox와 분리하여 세션 재시작 이력 보존.
- `Message.seq` + `@@unique([postId, seq])` — 순서 SoT.
- `Message.replyToId` — AGENT_REPLY ↔ HUMAN **1:1 귀속**의 데이터 근거.
- `Message.clientId` — 전송 멱등키.
- **User는 API 키 필드를 갖지 않는다** — BYOK 폐기가 스키마에 각인.

### 4.5 API 표면 (27개 엔드포인트)

auth 4, posts 7, messages 2, session 3, stream 1(SSE), files 2, uploads/bookmark/users/runtime/metrics/health 등.

---

## 5. Slack/메신저 연동 — 부재, 그러나 선행기술 비교 기록 존재

**제품 코드에 메신저 연동은 존재하지 않는다** (`slack|discord|telegram|webhook` 검색 0건). 유일한 언급은 `docs/IMPLEMENTATION_NOTES.md:725,733,736`의 **선행기술 대비 논거** — CopilotKit OpenTag.

### 대비 대상: CopilotKit OpenTag

Slack 등 그룹 채팅 내 자가호스팅 AI 에이전트 봇. 공식 문서 확인 결과:
- 스레드당 **단일 활성 run을 Redis 분산 락(TTL 20s + 하트비트)으로 강제**
- 동시 run은 **409 Conflict로 거부**
- **실행 샌드박스·작업 디렉토리·파일 부수효과 계층 자체가 부재**

### 판정 (문서 기록)

**락의 목적이 정반대**:

| | OpenTag | Coagora |
|---|---|---|
| 락의 목적 | 병렬 실행 **차단** | 병렬 추론 **유지**, 부수효과만 직렬화 |
| 동시 요청 | 409 거부 | 독립 `turnId`로 즉시 병렬 스트리밍 |
| 실행 워크스페이스 | 없음 | 샌드박스 1:1 (계층 락으로 방어) |

OpenTag는 "공유 스레드면 직렬화·거부"라는 통상 상식의 상용 실시례 → **교시회피(teaching away) 논거의 증거**. README 실측에서 "거절+재시도" 계약이 직렬보다도 나쁘다(기울기 1.010)는 결과가 이 계약을 정량 반박.

---

## 6. 논문 작성 시 활용 가능한 자산

### 6.1 실험 하네스 (`backend/bench/`)

| 파일 | 역할 |
|---|---|
| `mockLlm.mjs` | OpenAI 호환 모의 LLM (결정적 지연 주입) |
| `e2-hol.mjs` | E2/E2-B: HOL 지연 분포, 3계약 비교 |
| `e1-ablation.mjs` | E1: 직렬 실행기 ablation |
| `docker-isolation-poc.mjs` | 부록 B: 컨테이너 격리 PoC |
| `render-cdf.mjs` | JSONL → CDF SVG |
| `out/` | **측정 원자료 19개 파일 (커밋됨)** |

재현: `npm run bench:e2` / `bench:e1`. 모의 LLM이라 실 키·네트워크 불필요.

### 6.2 시각 자료 (`docs/assets/`)

TTFT CDF SVG 3종, `hol-clip.mp4`(20초 직렬 vs 병렬 대비, 실측 상수에서 결정적 생성).

### 6.3 검증 자산

- 백엔드 테스트 35개 — 동시성: `arMux`, `xcCap`, `xcMode`, `sandboxLock`, `sandboxLockScope`, `piWorkerConcurrency`, `provisionConcurrent`, `rtMulti`
- 보안 테스트: `redaction.test.ts`, `sandboxEnv.test.ts`
- 단언형 E2E: `frontend/e2e/concurrent-turns.assert.mjs`
- CI 키 유출 게이트: `backend/scripts/key-grep-gate.mjs`

### 6.4 문서 (`docs/`)

`PAPER.html`, `PATENT.html`, `EXPERIMENTS.md`, `PRD.md`, `TRD.md`, `PLAN.md`, `WIREFRAME.md`, `IMPLEMENTATION_NOTES.md`(docs-before-code 이력), `BUSINESS_VALUE.md`, `DEMO_SCENARIO.md`.

---

## 7. 분석자 관찰 — 논문에 반영할 만한 지점

**강점**
1. **주장과 증거가 코드 레벨에서 일치** — "병렬 추론 + 직렬 부수효과"가 `pi.ts`(디스패치 분리)와 `sandboxLock.ts`(계층 락)로 물리적으로 구현되고, 대응 실측(E2, E1)이 원자료와 함께 커밋.
2. **한계를 감추지 않음** — E2-B 1.07× 붕괴, cgroup 미구현, 네트워크 격리 deferred, tool→turn 귀속 근사성 모두 명시.
3. **설계 결정마다 근거가 주석으로 보존** — Design Rationale 섹션에 그대로 옮길 수 있는 수준.
4. **하위호환 구조적 보장** — v2/레거시 자료구조 완전 분리, opt-in 플래그 안전측 폴백, `LOCK_SCOPE` 롤백 스위치.

**논문에서 명확히 서술해야 할 한계**
1. **단일 인스턴스 전제** — pub/sub, 레이트리밋, sandboxLock, 핸들 레지스트리 모두 프로세스 인메모리. 단 `PubSub` seam으로 확장 경로는 설계에 반영.
2. **접근 제어 모델의 개방성** — 멤버십 개념 부재, 로그인한 누구나 임의 게시글 샌드박스에서 코드 실행 가능. 위협 모델 명시 필요.
3. **격리 강도** — 컨테이너·cgroup·네트워크 격리 미적용(PoC만). "PoC 수준 격리" 명시 필요.
4. **steer + concurrent 조합 미지원** (순수 취소만).
5. **동시 모드 tool 귀속 근사** — 1:1 귀속 주장의 범위를 AGENT_REPLY로 한정해 서술.

### 핵심 파일 경로 요약

| 주제 | 경로 (Coagora 기준 상대경로) |
|---|---|
| 동시성 계약의 중심 | `backend/src/agent/sandboxLock.ts` |
| 턴 디스패치·멀티플렉싱 | `backend/src/agent/pi.ts` |
| 턴 오케스트레이션 | `backend/src/agent/turn.ts` |
| LLM 루프·공유 컨텍스트 | `backend/src/agent/piWorker.mjs` |
| 세션 시작 임계구역 | `backend/src/agent/sessionStart.ts` |
| SSE 스트림·재생 | `backend/src/realtime/stream.ts` |
| 이벤트 스키마 | `backend/src/realtime/events.ts` |
| 순서 SoT | `backend/src/domain/seq.ts` |
| 도구 실행·ENV 경계 | `backend/src/agent/toolExec.ts` |
| 경로 탈출 가드 | `backend/src/sandbox/pathGuard.ts` |
| 데이터 모델 SoT | `backend/prisma/schema.prisma` |
| 귀속·게이팅 셀렉터 | `frontend/src/lib/threadSelectors.ts` |
| 실험 하네스·원자료 | `backend/bench/` |
