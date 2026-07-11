# Teammate — 상주 세션 에이전트 설계

> 상태: **설계 승인 대기** (2026-07-11 사용자와 공동 설계)
> 이 문서는 living plan — 각 Phase 완료 시 진행 로그를 갱신한다.

## 1. 목표

현재 `delegate` 는 fire-and-forget 이다: 서브에이전트를 스폰 → 완주 → result.md
반환 → **ctx 폐기**. 답변을 받은 뒤 이어서 질문할 수 없다.

`teammate` 는 이를 **상주 팀원**으로 격상한다:

```
delegate  = 일회성 파견   "이거 해줘" → 결과 → 소멸          (기존, 표면 불변)
teammate  = 상주 팀원     spawn → key 반환 → request/회신 반복
                          → 컨텍스트 유지 → kill 까지 생존
```

main LLM 이 teammate 의 "사용자" 역할을 한다. 핵심 통찰: `run_loop` 는 이미
"메시지 1건 처리기"이고 main REPL 이 영속 ctx 로 반복 호출하는 구조 — teammate
는 그 패턴에서 사용자 자리에 main LLM(또는 웹의 인간 사용자)을 앉히는 것이다.
새 실행 기계는 거의 필요 없고, **ctx 를 버리지 않는 것**이 본질이다.

## 2. 확정된 설계 결정 (사용자 결정 로그, 2026-07-11)

| # | 결정 | 근거 |
|---|------|------|
| D1 | **비동기 mailbox 모델** — request 즉시 반환, 회신은 harness 가 배달 | "시켜놓고 딴 일" UX. LLM 폴링은 금지 — status 반복 호출로 턴을 낭비하지 않는다 |
| D2 | **회신 배달 = harness 주도** — 턴 경계에서 mailbox 확인 → 관찰로 자동 주입 | LLM 이 폴링 규율을 지킬 것으로 기대하지 않는다 (27B 현실) |
| D3 | **main idle 시 자동 재기동** — 회신 도착이 새 run 을 깨운다 | "어렵다고 좋은 UX를 놓치지 않는다" (사용자). web 은 입력 큐 주입으로 자연 구현. **CLI 도 같은 입력 큐 모델로 통일** (사용자 결정 — 사용자 입력도 큐에 넣고, 큐에 뭔가 있으면 재기동. P5) |
| D4 | **ask→main 라우팅 포함** — teammate 가 `ask` 하면 질문이 main mailbox 로 | 양방향 대화가 같은 기계로 완성. Phase 2 |
| D5 | **A안: delegate 공존** — delegate 표면 바이트 불변, 내부 러너만 공유 | 검증된 delegate prior(150턴 0.7% 형식실패)를 버리지 않는다 |
| D6 | **도구 이름 `teammate`** — `agent`(delegate 의 역할 파라미터와 충돌)·`session`(메인 세션과 겹침) 기각 | "delegate=파견 / teammate=상주 동료" 대비가 설명 한 줄로 전달 |
| D7 | **resume 시 teammate 자동 재생성** — 살아있던 teammate 는 세션 resume 때 되살아난다 | manifest + ctx resume=True. 세션의 일부로 취급 |
| D8 | **WebUI teammate 대화 창 + 사용자 개입** — 사람이 직접 teammate 와 대화 가능 | Phase 4 |
| D9 | **프롬프트 인스펙터에 teammate 스코프 상시 표시** | v4.52.0 스코프 스택 재사용 — spawn 시 push, kill 시 고정 |
| D10 | **회귀 없도록 유닛 테스트 철저** — Phase 마다 테스트 게이트 통과 후 출하 | 사용자 명시 요구 |
| D11 | **역할 정의는 전용 md** — `.agent-cli/teammates/{name}.md` 전용 디렉토리, 본문이 teammate 의 system prompt 로 로드 | (b) 확정 — teammate 전용 frontmatter(auto-spawn 등)가 자랄 자리를 처음부터 분리. 파싱은 resource_loader 공유 |

## 3. 아키텍처 개요

```
                    main 세션 (main LLM)
                    │
     ┌──────────────┼────────────────────────────────┐
     │  teammate 도구 (mode enum)                     │
     │  spawn / request / wait / status / kill        │
     └──────────────┬────────────────────────────────┘
                    ▼
     TeammateRegistry (main 루프 수명 소유, manifest 로 디스크 반영)
     │
     ├─ Teammate "agt-3f2a" ────────────────────────────────┐
     │    worker thread:  inbox.get() ──▶ run_loop(msg,      │
     │                        ▲           ctx=영속 ctx)      │
     │    inbox  ◀── request ─┘              │               │
     │    (main·인간 공용)                    ▼               │
     │                              main mailbox 로 회신 push │
     └───────────────────────────────────────────────────────┘
                    │
                    ▼
     main mailbox (회신·질문 수신함)
     │
     ├─ main run 진행 중 → 턴 경계에서 관찰로 주입 (D2)
     └─ main idle      → web: 입력 큐에 합성 아이템 → run 자동 재기동 (D3)
                         CLI: 알림 출력 + 다음 입력 턴에 배달 (§7 결정 포인트)
```

메시지 흐름 (행복 경로):

```
main 턴 N:   {"action":"teammate","mode":"spawn","agent":"explorer","task":"레포 훑어봐"}
             → Observation: "agt-3f2a 생성됨, 초기 task 진행 중"     (즉시)
main 턴 N+1: shell("pytest ...")                                    (그동안 딴 일)
main 턴 N+2: (턴 경계) harness 가 mailbox 확인 → 회신 도착
             → Observation 에 "── agt-3f2a 회신 ──\n<탐색 결과>" 주입
main 턴 N+3: {"action":"teammate","mode":"request","key":"agt-3f2a",
              "message":"진입점이 어디야?"}                          (이어서 질문)
```

## 4. 구성 요소

### 4.1 공용 러너 (Phase 0 — 순수 리팩터)

`tools/delegate/exec.py::_run_single` 의 몸통을 추출한다:

```
agent_cli/subagent/runner.py (v4.53.0 구현 완료)
  apply_role_overrides(config, ...)   : 역할 md config 오버레이 — 로더-불가지론적
                                        (delegate=agents/, teammate=teammates/ 로
                                        로더 분리(D11), config dict 만 소화)
  create_subagent_ctx(mode, parent, dir): none/fork ctx + wire_format·예산 상속
                                        + 인스펙터 스코프 등록 (v4.52.0)
  run_subagent_message(query, ctx, ...) : run_loop 1회 (depth+1) + 소요시간
```

- delegate 는 이 러너를 호출하는 thin wrapper 가 되고 **동작·관찰 문구·
  result.md 바이트 불변** (기존 delegate 테스트 전체가 무수정 통과 = 게이트).
- teammate 는 같은 러너를 "ctx 를 버리지 않고" 반복 호출한다.
- wire-format self-contained 원칙과 무관 — 이것은 실행 계층 공유다.

### 4.2 Teammate / TeammateRegistry (Phase 1)

```python
class Teammate:
    key: str                  # "agt-" + uuid4 hex 앞 8자리
    agent_name: str           # 역할 정의 (없으면 익명)
    ctx: ContextManager       # 영속 — session_dir/teammates/<key>/
    config: dict              # tools, model, context_mode 등 spawn 시 고정
    inbox: SimpleQueue        # main request + 인간 개입 + ask 답변
    state: str                # idle | busy | waiting_ask | dead
    stop_event: threading.Event   # kill 전용 (main 인터럽트와 독립)
    worker: threading.Thread  # daemon; inbox.get() 블록 → run_loop → 회신 push

class TeammateRegistry:
    teammates: dict[str, Teammate]
    mailbox: SimpleQueue      # main 수신함: 회신·질문 (누가 보냈는지 태깅)
    manifest_path: Path       # session_dir/teammates.json (fsio 원자 교체)
```

- **소유**: main 루프(LoopState 또는 run/web 부트스트랩)가 1개 보유.
  delegate 처럼 tool_bridge 인터셉트 지점에서 주입 (provider/capabilities 필요).
- **worker 루프**: `inbox.get()` 블록 → 메시지 → `run_loop(query=msg, ctx)` →
  최종 답변을 `registry.mailbox` 에 push → 다시 블록. 상태 전이가 이 루프 하나에
  전부 담긴다.
- **상한**: 동시 생존 teammate 기본 4 (env `AGENT_CLI_MAX_TEAMMATES`).
- **깊이**: teammate 루프에서 `teammate` 도구는 **제거** (Phase 1 — 트리 금지,
  레지스트리 소유권 단순화). `delegate` 는 teammate 안에서 기존 규칙대로 허용.
- **인터럽트 분리**: main 의 stop(/api/stop, Ctrl+C)은 main run 만 중단 —
  teammate 는 계속 일한다 (백그라운드 팀원). 종료는 명시 `kill` 또는 세션 종료.
- **세션 종료**: 전원 stop_event set → join(timeout=5s) → manifest 에 상태 기록.

### 4.3 도구 표면 (Phase 1)

단일 도구 + mode enum (memory_tool 검증 패턴). flat-native, `parallel_safe=False`
(mode 별 비블로킹이라 병렬 배칭 불필요 — spawn/request 는 즉시 반환).

```json
{"action":"teammate","mode":"spawn","agent":"explorer","task":"...(옵션)","tools":[...],"context":"none|fork"}
  → "agt-3f2a 생성됨" (task 를 줬으면 첫 request 로 inbox 에 즉시 큐잉)
{"action":"teammate","mode":"request","key":"agt-3f2a","message":"..."}
  → "전송됨 (회신은 도착 시 배달)"                      (즉시 반환)
{"action":"teammate","mode":"wait","key":"agt-3f2a"}
  → 회신 도착까지 블록 후 회신 반환                      (명시적 join — 폴링 대체)
{"action":"teammate","mode":"status","key":"...(생략=전체)"}
  → 상태·턴 수·ctx 토큰·대기 중 질문/회신 수
{"action":"teammate","mode":"kill","key":"agt-3f2a"}
  → 종료 + 인스펙터 스코프 고정 (디스크 잔존)
```

- spawn 의 `task` 는 옵션 — 주면 "spawn + 첫 request" 와 동치 (mailbox 모델의
  자연스러운 귀결).
- **역할 정의 md (D11, (b) 확정)**: spawn 의 `role` 파라미터가
  `.agent-cli/teammates/{name}.md` (프로젝트) / `~/.agent-cli/teammates/`
  (전역) 를 로드한다 — **전용 디렉토리** (agents/ 와 분리, teammate 전용
  frontmatter 가 자랄 자리). md 본문(role)이 teammate 서브루프의 **system
  prompt** 에 로드되고, frontmatter 의 `allowed-tools`/`model`/`hooks`
  오버라이드는 agent md 와 동일 키·동일 의미. 파일 파싱·탐색은
  `resource_loader` 공유 (포맷 자체를 복제하지 않는다 — 디렉토리와 로더
  진입점만 분리). teammate 는 장수명이라 역할 프롬프트가 세션 내내 지속 —
  "역할에 맞는 상주 팀원"이 파일 하나로 정의된다. `role` 생략 시 익명.
- `wait` 는 "회신만 기다리면 되는 상황"의 낭비 턴 0 해법. timeout 은
  delegate_timeout 재사용, 초과 시 실패 관찰 + teammate 는 계속 진행.
- 도구 설명에 delegate 와 상호 참조: "일회성 독립 작업 → delegate /
  이어서 문답할 협업 → teammate".

### 4.4 회신 배달 (Phase 1) — D2/D3

**main run 진행 중**: 턴 경계(다음 LLM 호출 직전, TurnDispatcher)에서
`registry.mailbox` 를 비우고 레코드로 주입:

```python
{"tool": "teammate", "action_input": {"mode": "deliver", "key": "agt-3f2a"},
 "observation": "── agt-3f2a 회신 ──\n<본문>", "success": True,
 "source": "teammate_reply"}   # additive 필드
```

- `tool=""` 는 v4.51.0 형식-개입 레거시 마커라 **금지** — `is_format_intervention`
  이 오인하지 않도록 tool="teammate" + `source` additive 필드.
- 큰 회신은 delegate 와 동일한 over-cap 정책: 전문은 `teammates/<key>/replies/`
  에 영속, 관찰에는 on-disk nudge.
- **resume 호환 semver 체크**: additive 레코드 필드 + 신규 파일뿐 — 구 세션
  resume 무영향, 신 세션을 구 버전이 resume 해도 잉여 파일 무시 → **minor**.

**main idle**:

- **web**: `server.enqueue()` 에 합성 아이템 (`kind:"teammate_reply"`) →
  `_worker_loop` 가 기존 큐 경로로 새 run 을 시작, 회신 레코드 주입 후 LLM 이
  이어서 행동 (D3 자동 재기동). 사용자 메시지와 동일 FIFO 라 경합 없음.
- **CLI**: 사용자 결정 — **web 과 같은 입력 큐 모델로 통일** (P5). reader
  스레드가 `input()` → 큐에 push, worker 는 큐 블록 → run. teammate 회신도
  같은 큐에 합성 아이템으로 들어가 CLI 도 자동 재기동. C4 부트스트랩 통일의
  연장선. 구현 주의: Ctrl+C 인터럽트가 main 스레드(input 블록)에 떨어지는
  구조 변화 + 타이핑 중 run 출력 시작 시 readline 표시 겹침(셸 백그라운드 잡
  알림과 같은 수준으로 수용, 알림 1줄 후 진행). **P5 전까지의 과도기(P1~P4)**
  에는 CLI 는 "📨 회신 도착" 알림 1줄 + 다음 입력 턴 배달.

### 4.5 ask→main 라우팅 (Phase 2) — D4

teammate 루프의 `ask` 인터셉트(virtual tool, 루프가 이름으로 처리)를 분기:

```
teammate 의 ask("어느 브랜치 기준인가요?")
  → main mailbox 에 {kind:"question", key, question} push
  → teammate worker 는 inbox 에서 답변 대기 (state=waiting_ask)
main 관찰: "── agt-3f2a 질문 ── 어느 브랜치 기준인가요?"
main: {"action":"teammate","mode":"request","key":"agt-3f2a","message":"main 브랜치"}
  → teammate inbox 로 → ask 관찰로 재개
```

- 웹 대화 창(Phase 4)에서는 **인간이 먼저 답해도 됨** — inbox 도착 순서가 답.
- 질문에도 attribution: teammate ctx 에는 `[main]` / `[user:닉네임]` 접두로
  화자를 구분해 기록 (teammate 가 두 화자를 인지).

### 4.6 resume 자동 재생성 (Phase 3) — D7

`session_dir/teammates.json` manifest (fsio 원자 교체):

```json
{"version": 1, "teammates": [
  {"key": "agt-3f2a", "agent": "explorer", "state": "idle",
   "config": {...}, "created": "...", "dir": "teammates/agt-3f2a"}]}
```

- 갱신 시점: spawn / kill / 세션 종료.
- **main 세션 resume 시**: manifest 의 `dead` 아닌 teammate 전원을
  `ctx = ContextManager(dir, resume=True)` 로 재생성 — worker 재기동(idle),
  인스펙터 스코프 재등록. teammate 는 자기 히스토리를 전부 기억한 채 살아난다.
- **미배달 회신**: mailbox 는 휘발이므로 push 시점에
  `teammates/<key>/outbox.jsonl` 에도 append(가드 append), 배달 시점에 소비
  마킹. resume 후 미소비분을 첫 턴 경계에 배달 — 회신 유실 없음.
- busy 중 죽은 경우: 진행 중이던 request 는 유실될 수 있음을 status 에 명시
  ("interrupted by session exit") — 재요청은 main LLM 판단.

### 4.7 WebUI 대화 창 + 사용자 개입 (Phase 4) — D8

- **SSE 라우팅**: teammate worker 스레드는 v4.52.0 스코프 스택에 이미 올라가
  있으므로(spawn 시 begin_prompt_scope), 그 출력 이벤트를 scope id 로 태깅해
  프런트로 — delegate 접이식 카드의 상주 버전인 **teammate 창** (사이드 패널
  또는 탭)에 스트림.
- **사용자 개입**: 창 하단 입력 → `POST /api/teammate/{key}/input` →
  해당 inbox 에 `[user:닉네임]` 태깅으로 push. teammate 의 ask 질문에도 이
  경로로 답 가능 (main 과 선착순).
- teammate 의 회신이 인간 개입에 대한 것이면 main mailbox 배달은 생략하고
  창에만 표시 (요청자 태깅으로 판별) — main 컨텍스트를 무관한 문답으로
  오염시키지 않는다.
- 창에 kill / status 버튼.

### 4.8 프롬프트 인스펙터 (Phase 1 에 포함, 거의 무비용) — D9

v4.52.0 기계 그대로: spawn 시 `begin_prompt_scope(key, label=f"teammate:{name}")`
+ `note_scope_ctx(ctx)` — 단 delegate 와 달리 **턴이 끝나도 pop 하지 않고**
kill/세션 종료 시에만 `end_prompt_scope`(고정 스냅샷). 살아있는 동안 인스펙터에
상시 칩 + live 동적 컨텍스트.

주의: 스코프 스택은 스레드 키라, worker 스레드에서 push 하고 그 스레드의
run_loop 출력이 같은 스코프로 라우팅되는지 확인 (begin_delegate_task 와 달리
스레드 생존이 길다 — 테스트 케이스 필수).

## 5. 가드 요약

| 가드 | 정책 |
|---|---|
| 동시 수 | 기본 4, env 로 조정. 초과 spawn 은 거부 관찰 |
| 재귀 | teammate 안 teammate 금지(도구 제거). delegate 는 depth 규칙대로 |
| 사이클 | agent_stack 이름 사이클 체크 재사용 |
| 타임아웃 | request 처리는 delegate_timeout, wait 도 동일 |
| 인터럽트 | main stop 은 teammate 무영향. kill 만 종료 |
| 컴팩션 | teammate ctx 도 기존 컴팩션 그대로 (장수명이라 오히려 중요) |
| 회신 크기 | over-cap 시 on-disk nudge (delegate 정책 재사용) |

## 6. 구현 계획 — Phase 게이트

각 Phase = 독립 브랜치 → 테스트 게이트 → README/ARCHITECTURE 동기 → ff-merge
→ wheel 릴리스 (확립된 출하 절차). **이전 Phase 의 게이트를 깨면 진행 금지.**

| Phase | 내용 | 버전 | 테스트 게이트 |
|---|---|---|---|
| **P0** | 공용 러너 추출 (`subagent/runner.py`) — delegate thin wrapper 화 | 4.53.0 | 기존 delegate 테스트 **무수정 전체 통과** + 러너 유닛. 관찰 문구·result.md 바이트 불변 확인 |
| **P1** | Registry + teammate 도구 5 mode + 턴 경계 배달 + CLI 알림 + 세션 종료 정리 + 인스펙터 칩 | 4.54.0 | §6.1 코어 케이스. 전체 회귀 |
| **P2** | ask→main 라우팅 (양방향 문답) | 4.55.0 | ask 분기·waiting_ask·선착순 답변. 기존 ask(사용자행) 회귀 0 |
| **P3** | resume 자동 재생성 (manifest + outbox 미배달 배달) | 4.56.0 | resume 계약 케이스 + **구 세션 resume 무영향** (semver 체크) |
| **P4** | WebUI 대화 창 + 사용자 개입 + idle 자동 재기동(웹) | 4.57.0 | SSE 라우팅·개입 e2e (실기동), main 오염 없음 확인 |
| **P5** | CLI 입력 큐 통일 → CLI 도 idle 자동 재기동 (과도기 알림 제거). **⚠ web 과 코드 공용화 우선** (사용자 지시) — CLI 전용 큐를 새로 만들지 말고 web 의 enqueue/dequeue_blocking 골격을 공용 계층으로 추출해 양쪽이 같은 코드를 쓴다 (C4 부트스트랩 통일의 완성) | 4.58.0 | CLI 인터랙션 회귀 (Ctrl+C·readline·/명령) + 큐 경유 재기동 + web 회귀 0 |

### 6.1 P1 테스트 케이스 (코어 — 최소 목록)

- spawn: key 반환·상한 초과 거부·잘못된 agent 이름 거부·task 옵션 시 inbox 큐잉
- request/회신: 턴 경계 주입 레코드 shape·`is_format_intervention` 비오인·
  fold 로직 무간섭·여러 회신 일괄 배달 순서
- wait: 회신 도착 해제·timeout·죽은 key 에 대한 실패 관찰
- status: 상태 전이(idle→busy→idle)·죽은 teammate 표시
- kill: worker 종료(join)·스코프 고정·재 kill 멱등·kill 후 request 거부
- 수명: 세션 종료 시 전원 정리·main 인터럽트가 teammate 를 죽이지 않음
- 스레드: worker 장수명 스코프 라우팅·동시 spawn 레이스(manifest 원자성)
- 러너 공유: delegate 회귀 0 (P0 게이트 상시 유지)

## 7. 결정 포인트 — 전원 해소 (2026-07-11 사용자 확정)

1. **CLI idle 자동 재기동** → **CLI 도 입력 큐 모델로 통일** (사용자 제안):
   사용자 입력도 큐에 넣고, 큐에 뭔가 있으면 재기동. P5 로 편성, 과도기(P1~P4)
   는 알림 1줄 + 다음 입력 턴 배달.
2. **인간 개입 문답의 main 비배달** → 동의. 인간↔teammate 문답은 창에만,
   main 이 요청한 회신만 main 컨텍스트에.
3. **P1 에서 teammate 안 teammate 금지** → 동의. delegate 는 teammate 안에서
   기존 규칙대로 허용.
4. **역할 md 위치** → **(b) 전용 `teammates/` 디렉토리**. teammate 전용
   frontmatter(auto-spawn 등)가 자랄 자리를 처음부터 분리. 파싱은
   resource_loader 공유 — 포맷 복제 없음.

## 대기 중인 후속 아이디어

- **`@teammates` / `@agt-<key> <메시지>` 명령** (2026-07-11 설계 합의, 미착수):
  `@agents` 대칭의 roster 목록 + 채팅박스/CLI 에서 teammate 직접 지목
  request. 회신 라우팅 D8 유지(main ctx 비오염) — 웹은 창, **CLI 는
  MinimalRenderer.teammate_message 를 콘솔 라인으로 구현**해 수신 (비-main
  화자 문답만 — 이중 표시 방지용 `to` 필드 추가 필요). 사용자 직접 스폰
  명령은 delegate 네임스페이스 혼동으로 보류 (범위 B 채택).

## 후속 확장 로그 (로드맵 완결 이후)

- 2026-07-11 **전문가 역할 확장 (v4.59.0)**: ①역할 발견 — 시스템 프롬프트
  Teammate Roles 섹션(agents 광고와 동형, teammate 도구 게이트) ②내장
  전문가 researcher/code-reviewer (`agent_cli/teammates/builtin/`) ③
  `/create-teammate` 스킬 ④auto-spawn frontmatter(D11(b) 때 예약한 자리 —
  restore 후 dedup 스폰) ⑤runtime 부트스트랩 프리필(restore/auto-spawn 분
  첫 접촉 대비) ⑥**worker 사망 통지**(사용자 Q4): 비정상 종료는
  kind:"died" mail 로 main 에 관찰 통지, kill/세션종료는 제외.

- 2026-07-11 **다중 인스턴스 + Live Teammates 광고 (v4.60.0)**: 같은 역할
  N명(파일 분담 병렬 개발) — spawn `name` 인스턴스 라벨(표시 전용, 주소는
  key)·내장 `coder`(파일 스코프 규율). **static/dynamic 분리 설계**:
  멤버십(key·역할·name·전문영역)은 static 시스템 프롬프트 Live Teammates
  섹션(compaction 면역·auto-spawn/resume 커버·멤버십 변화 플래그로만
  재조립=KV 보호), 활동(관찰·상태)은 dynamic 현행 유지. 이중 게이트로
  teammate 자신에겐 미노출, 인스펙터엔 섹션 단일 진실로 자동 노출. 통합
  테스트가 "spawn 다음 턴 프롬프트 광고 탑재" 관통 검증. 테스트 +9,
  전체 2998.

- 2026-07-11 **v4.60.1 (사고 수리)**: `renderer.status()` 시그니처 오호출
  3곳 — web 부트의 재생성/auto-spawn 알림이 worker 스레드를 **부트에서
  죽여** "큐만 쌓이고 소비 안 됨"으로 발현 (web+resume+teammate 조합에서만
  — CLI e2e 는 console.print, P4 웹 e2e 는 fresh 세션이라 전부 비껴감).
  📨 도착 알림은 try/except 가 삼켜 조용히 죽어 있었음. 수리: status(state,
  message) 정호출 + `_announce_teammate_boot` 헬퍼 추출(실렌더러 Web/
  Minimal 로 시그니처 고정 테스트 3종) + **worker crash 가드**(_worker_
  loop_guarded — 사망 시 instance.log traceback + 접속 클라이언트에 에러
  카드, 조용한 큐 마비 재발 방지). 사용자 세션 그대로 재현→수리 검증
  (web resume→재생성→main 정리 요청 처리).

- 2026-07-11 **mode:"resume" (v4.61.0)**: 죽은 teammate 를 이전 컨텍스트
  그대로 부활 — dead 칩이 남는 이유("사후 검사")에 두 번째 이유("부활
  가능") 추가. 같은 key + ctx resume 모드(P3 재사용) + seq 연속 + revivable
  복귀. 이름 논의: resume(세션·ctx 와 동일 의미론) 채택, respawn(초기화
  오해)/restore(내부 API 충돌) 기각. 웹 dead 칩 ↻ 버튼 + 엔드포인트.
  테스트 +9 (kill/사망/툼스톤 3경로 부활·가드·seq·revivable 복귀).
  같은 릴리스에 **멤버십 변화의 인스펙터 즉시 반영**: 플래그-리로드는
  다음 턴에야 돌아 idle 중 창 kill 이 낡은 Live Teammates 를 남기던 갭 —
  `update_prompt_section`(web 스냅샷 외과 갱신: 교체/카탈로그-뒤 삽입/
  빈값 제거+총계 재계산) + transient `prompt_changed` → 열린 인스펙터
  refetch(memory_changed 패턴). notify_teammates_changed(registry) 가
  spawn/kill/died/restore/resume 전 지점에서 구동. 실제 재조립은 여전히
  플래그가 보장 (인스펙터는 다음 호출이 받을 내용을 앞당겨 표시).

- 2026-07-11 **v4.61.1 (resume 유도)**: 실사용 관찰 — 사용자가 key 까지
  지목하며 "다시 시작하자" 해도 모델(Qwen 35B-A3B)이 spawn 으로 새 키를
  만들어 컨텍스트 유실+칩 증식 (resume 모드는 스키마에 있었으나 결정
  시점의 유도 부재). 3지점 유도: kill 출력("context PRESERVED — resume
  로, 새 spawn 금지"), status 의 dead 항목 "resumable via ...", 같은
  역할 dead 존재 시 spawn 사후 힌트("NO memory — CONTINUE 였다면 kill
  후 resume"). 죽은 세대들의 디스크 컨텍스트는 그대로라 사후에도
  resume 가능함을 확인.

## 진행 로그

- 2026-07-11: 설계 공동 확정 (D1~D11), 문서 작성.
- 2026-07-11: §7 결정 4건 전원 해소 (CLI 큐 통일=P5 신설, 비배달 동의, 중첩
  금지 동의, 역할 md=(b) 전용 디렉토리). **P0 착수 승인.**
- 2026-07-11: **P5 완료 (v4.58.0)** — 큐 공용화 + CLI 자동 재기동.
  **설계 정정**: CLI 에는 인터랙티브 REPL 이 없음이 실측됨(`run` 은
  단발) — §4.4 의 "input() reader 스레드" 전제를 폐기하고 **run 을 큐
  펌프로 재해석**: 초기 질의도 wake 도 공용 InputQueue 로, 정지=큐 비고
  +활성 작업 없음(quiescence; waiting_ask 는 교착 방지로 제외·경고 후
  종료). 공용화는 지시대로 web 골격 추출(`input_queue.py` — WebServer
  는 thin wrapper, SHUTDOWN identity 계약 유지, web 테스트 무수정 317
  통과). 테스트 +14(InputQueue 8·quiescence 2·펌프 4), 전체 2979 +
  실기동 CLI e2e. **★로드맵 전체 완료.**
- 2026-07-11: **P4 완료 (v4.57.0)** — WebUI 대화 창(D8)+idle 자동 재기동
  (D3). 렌더러 표면 teammate_roster(sticky)/teammate_message(persistent),
  인간 개입 비배달 규칙(current_author — 질문 라우팅도 대칭), 엔드포인트
  input(닉네임 attribution)/kill, 🤝 드로어(roster 칩·대화 버블·입력).
  자동 재기동은 **MailWaker 로 추출**(main 클로저에 묻지 않고 단위 테스트
  — armed 중복 방지·run 종료 후 레이스 봉합·빈 wake skip). CSS 는 테마
  토큰만(가드 테스트가 raw hex 검출). 테스트 +18(비배달·질문 라우팅·
  waker 5종·엔드포인트 5종·sticky/persistent), 전체 2965.
- 2026-07-11: **P3 완료 (v4.56.0)** — resume 자동 재생성 (D7). 설계의
  outbox.jsonl 대신 **teammates.json 단일 파일**(manifest+pending 미러,
  fsio 원자 교체)로 단순화 — pending 이 in-memory 진실이므로 변경마다
  전체 스냅샷이 마킹-소비 jsonl 보다 정직. role_prompt 통째 저장(역할 md
  소실 무관), 논리 생사(kill=영구/세션종료=revivable), ctx "resume" 모드
  (runner 3번째 모드), stale 질문 마킹, seq 이어가기, 매 호출 runtime
  갱신. semver 체크: additive 파일 — 구 세션 무영향, minor. 테스트 +10
  (roundtrip·kill 영구·stale·역할 파일 삭제 생존·버전 가드), 전체 2947.
- 2026-07-11: **P2 완료 (v4.55.0)** — ask→main 라우팅 (D4). LoopConfig.
  ask_handler 훅(run_loop→dispatch `_handle_ask` 분기 — handler 없으면
  종전 시그니처 유지로 기존 patcher 무영향), worker 의 핸들러가 질문을
  mailbox `kind:"question"` 으로 올리고 inbox 다음 메시지를 답으로 소비
  ("도착 순서가 답" — P4 인간 개입 대비 `[author]:` attribution 포함).
  교착 방지: wait 는 kind 불문 반환(질문 우선 도착 시 답변 유도 후 재대기
  안내). _SHUTDOWN 이 답변 대기를 즉시 해제. 테스트 +9 (roundtrip·
  attribution·교착·종료 해제·delegate 경로 불변), 전체 2937.
- 2026-07-11: **P1 완료 (v4.54.0)** — Registry+Teammate(worker 스레딩, DI
  러너 seam)·teammate 도구 5 mode(C7 validate)·턴 경계 배달
  (`_deliver_teammate_replies`, 레코드 tool:"teammate"+source — fold 오인
  방지 테스트 고정)·인스펙터 상시 스코프(worker 가 begin_prompt_scope(key)
  1회 보유, 요청별 SSE 는 begin/end_teammate_work 분리 — web 은 delegate
  카드 이벤트 재사용으로 프런트 무수정, CLI 는 캡처 폐기)·main 인터럽트
  분리·세션 종료 정리·회신 상시 영속(replies/, P3 토대)·📨 도착 알림.
  설계 대비 조정: parallel_safe=False(모든 mode 즉시 반환이라 병렬 배칭
  불필요), 레지스트리 부재 시 도구 strip 이 "중첩 금지+기존 테스트 무영향"
  겸용 단일 가드. 테스트: teammate 계약 40종+web 라우팅 2종, 전체 2926.
- 2026-07-11: **P0 완료 (v4.53.0)** — `subagent/runner.py` 추출 (오버레이는
  로더-불가지론적으로 조정: D11(b) 확정으로 역할 md 로더 자체는 공유 대상이
  아님이 판명, config dict 소화만 공유). 게이트: delegate 테스트 151개
  무수정 전체 통과 + 러너 계약 14종 신설(순환 회피 lazy-import 계약 포함),
  전체 2886 passed.
