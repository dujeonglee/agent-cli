# Agent Channels — DESIGN

**한 줄**: 별도 "Agents" resident-agent 대화 드로어를 없애고, **메인 입력창 옆 드롭박스로 대화 상대(main / 각 agent)를 고르는 채널 모델**로 개요에 통합한다. 질문·확인은 채널과 무관한 **글로벌 트레이**로 분리한다.

관련 목업: `scratchpad/agent-chat-mockup.html` (사인오프 완료 — 채널 모델 + 글로벌 ask 트레이 + 타 채널 ask + 죽은 agent).

---

## 배경 / 문제 (현재)

지금 사람이 resident agent와 대화하려면 **헤더의 별도 드로어**를 연다: 로스터 칩에서 agent를 고르고, 전용 입력창으로 `POST /api/agent/<key>/input`. 이 드로어는 개요/흐름/전문과 **분리된 표면**이라:

- 대화 맥락이 개요(주 화면)와 단절돼 있고,
- 사람이 여러 agent와 병행 대화할 때 UX가 파편화되며,
- 입력창이 하나가 아니라 "main 입력창 + 드로어 입력창"으로 이원화돼 있다.

**목표**: 대화 상대 선택을 개요 입력 옆 드롭박스로 통합. main이 기본, agent 선택 시 그 agent와의 대화를 개요에서 이어간다.

---

## 사실 (코드 확인)

- **agent로 보내는 유일 경로**: `POST /api/agent/{key}/input` (`server.py:1166`) → `registry.request(key, content, author="user:<nick>")` (`agents_live.py`). 웹에서 agent inbox에 넣는 다른 경로 없음.
- **agent 큐/실행**: 각 `AgentInstance`가 자기 `inbox: SimpleQueue`(`agents_live.py:322`) + 독립 worker 스레드(`_worker`, `:1143`). worker는 `item = tm.inbox.get()`으로 **한 건씩 블로킹 소비**(`:1181`) → `_run_message` 한 턴. **drain-all 아님**(main과 다름).
- **main 큐/실행**: 서버 `_pending`(deque) → 턴 경계에서 **drain-all** 주입(`loop/core.py::_inject_queued_messages`, v8.16.1). 단일 loop, 모든 뷰어 공유.
- **agent 상태**: `starting|idle|busy|waiting_ask|dead` (`agents_live.py:327`). `waiting_ask`= 질문 답변 대기. `waiting_ask_keys()`(`:472`)로 대기 중 agent 열거 — **여러 agent가 동시에 waiting_ask 가능**. main의 `input_required`(renderer sticky)와 **독립**.
- **agent 질문**: worker가 물으면 `agent_msg{direction:"question", to:current_author}` emit + `state="waiting_ask"`(`:1316`, `:1336`). 답은 `/api/agent/input`이 소비("main과 선착순", `server.py:1169`).
- **회신 목적지(⑥)**: `/api/agent/input` 발생 문답 회신은 **main 컨텍스트 미배달, 대화창 전용**(`server.py:1170`). main-위임 요청 회신만 `_pending`→`drain_replies`→main 턴 경계로 배달.
- **회신 귀속**: main-위임 요청 item은 `answers=list(_current_run_authors)` 스냅샷(`:625`); 사람-직접 item은 author `user:<nick>`.
- **SSE 이벤트**: `agent_roster`(key/name/state/handled/pending_requests), `agent_msg`(direction in/out/question, author, to, seq, ts), `scope_start`/`scope_end`(task_id, depth, kind, agent, label), `assistant_turn`(final, task_id, thought, answers).
- **프론트 현황**:
  - 드로어 IIFE(`app.js:~3860+`): `roster`/`msgs[key]`/`selected`, `agentcli:tm-roster`·`tm-msg`·`tm-cleared` 이벤트로 갱신, `sendInput`→`api/agent/<key>/input`, kill/resume 버튼.
  - 개요 `ov*` 모듈: 플랫 로그(`ovEntries`), main final=`ovOnFinal`, top-level scope final=`ovOnScopedFinal`, 주체 배지(v8.23.0, `ovScopeSrc`/`.ov-src`).
  - 흐름 `TeamView`(스윔레인): 글로벌 팀 뷰.
  - main 입력창: `chat`/`prompt`/`confirm` 3모드(`setInputMode`). `/api/input` 게이트 `awaiting_input_kind`(v7.2.0).

---

## 설계 개요 — 두 축 분리

1. **대화 = 채널별 개요.** 입력창 옆 드롭박스가 **지금 보는 대화(채널)** = `main` | `agt-<key>`. 개요는 **선택 채널의 스레드만** 렌더. 입력은 그 채널로 라우팅.
2. **질문·확인 = 글로벌 트레이.** main prompt/confirm + 모든 agent question을 **채널 무관 고정 트레이**에 stack. 각 항목은 asker 라벨 + 인라인 답변. 답은 그 asker로 고정 라우팅(드롭박스와 무관).

→ "먼저 말 걸기(initiate, 채널)"와 "질문에 답하기(respond, 트레이)"가 완전히 분리돼 입력 모드 충돌이 원천 소멸. 흐름(스윔레인)은 **글로벌 조망으로 유지**(무변경).

---

## 상세 설계

### A. 채널 모델 (프론트)

- **상태**: `activeChannel`("main" | agent key), per-뷰어 로컬. **결정 A-1 (확정)**: **휘발** — 리로드/새 세션 시 항상 "main"으로 초기화(localStorage 미사용). 드롭박스 옵션 = `["main", ...roster(살아있는 agent)]`.
- **채널 데이터**:
  - `main` 채널 = 현재 개요(`ovEntries`) 그대로.
  - `agt-<key>` 채널 = 그 agent의 대화 스트림. **데이터 출처는 이미 존재**: 드로어가 모으던 `agent_msg`(direction in/out/question)를 채널별 엔트리 리스트로 재사용. `ovChannels[key] = [{dir, author, text, ts, ...}]`.
- **뷰 스코핑**: `ovRender`가 `activeChannel` 기준으로 렌더 소스를 고름(main=`ovEntries`, agent=`ovChannels[key]`). 채널 전환은 재렌더만(데이터는 계속 수신·축적).
- **채널 바/드롭박스**: 각 채널 칩에 상태 표시 — 새 회신 `🔔/N`(안 본 사이 도착), 답 대기 `❓`(그 agent가 waiting_ask), dead 표식. active 칩 강조.
- **배지/rail(④)**: 발신 메시지 `👤<나> → 🤝<agent>`(target 배지, `.ov-src` 발신형 확장), 회신 `🤝<agent>`(기존 주체 배지). 채널별 색 rail로 소속 시각화.

### B. 입력 라우팅

- `activeChannel === "main"` → `POST /api/input`(기존 큐, drain-all).
- `activeChannel === "agt-<key>"` → `POST /api/agent/<key>/input`(그 inbox). **엔드포인트 불변**(확인됨).
- 입력창 placeholder/드롭박스 색이 채널 반영. 전송 성공/실패(404=dead) 처리.

### C. ① agent inbox drain-all 배칭 (백엔드)

**현재**: `_worker` 루프가 `inbox.get()`으로 1건씩. **목표**: main과 동일하게 대기분을 한 턴에 배치.

```
while not stop:
    first = tm.inbox.get()            # 첫 건 블로킹
    batch = [first] + drain_nowait(tm.inbox)   # 현재 큐 전부
    process_batch(tm, batch)          # 한 턴: 모든 메시지 + "모든 요청 처리" notice
```

- `process_batch`는 배치 내 메시지를 `[author]: text` 라벨로 순서대로 주입 + main의 `QUEUED_REQUEST_NOTICE`에 대응하는 안내(agent용) 1회 → agent가 **한 응답으로 전부** 처리.
- 스윔레인 요청 화살표/카드는 배치 내 각 메시지별 `ts`를 유지(현행 앵커 규칙).

> **결정 C-1 (확정)**: **목적지별 배치 분리** (제안 A).
> - **사람-직접 item**(author `user:<nick>`) → drain된 것끼리 **한 배치로 묶어 한 번에 회신**(→대화창, ⑥).
> - **main-위임 item**(author `main`, `expects_reply`/`answers` 보유) → **하나씩 개별 처리**(회신→main mailbox). 배치와 섞지 않음.
> - worker가 inbox를 drain하며 두 그룹으로 분류: 사람-직접 그룹만 배치 주입, main-위임은 FIFO 개별. (한 번의 drain에서 두 종류가 다 나오면, 각 그룹을 순서대로 처리.)

### D. ③ 글로벌 ask 트레이 (프론트 + 약간의 서버)

- **수집원**:
  - main prompt/confirm: 현재 main 입력창 모드 전환 대신 **트레이 항목**으로. (renderer의 `input_required`/`awaiting_input_kind` 신호 재사용.)
  - agent question: `agent_msg{direction:"question"}` + roster `state==="waiting_ask"`.
- **렌더**: 채널 무관 고정 영역(입력창 위 pinned). 각 항목 = `❓ <asker>가 물었습니다` + 질문 + 인라인 답변(input/confirm 버튼). 여러 건 stack.
- **라우팅**: main 항목 답 → `POST /api/input`(kind 게이트 유지); agent 항목 답 → `POST /api/agent/<key>/input`. **드롭박스 target과 무관, asker에 고정.**
- **다중 사용자 선착순(H)**: ask 상태는 서버 공유 → 한 뷰어가 답하면 소비되고 나머지 뷰어 트레이에서도 사라짐. `agent_msg`/roster 갱신으로 자연 반영. main confirm은 기존 409 게이트(`awaiting_input_kind` 불일치) 재사용.
- **채널 칩 ❓**: 그 agent가 waiting_ask면 칩에 ❓(새 회신 🔔과 구분). 클릭=그 채널로 점프(선택). **강제 전환 없음.**

### E. ④ 개요 렌더 채널화

- `ovRender`가 `activeChannel` 소스 선택 + target/source 배지 + rail. main 채널은 기존 동작 100% 보존.
- agent 채널 엔트리 = `agent_msg` 매핑: `in`(사람→agent, target 배지) / `out`(agent 회신, 주체 배지) / `question`(→ 트레이로도 승격).
- 스크롤/hero/복사 등 기존 개요 UX 재사용.

### F. ⑤ 죽은 agent

- roster `state==="dead"`인 agent가 `activeChannel`이면(또는 대화 중 죽으면): 입력창 **비활성 + static text**("✕ 이 에이전트는 종료됨 — 다른 대상 선택 또는 ↻ 되살리기"). 드롭박스는 dead 표식.
- roster 이벤트로 자동 감지. 되살리기 `POST /api/agent/<key>/resume`(기존), kill `POST /api/agent/<key>/kill`(기존).

### G. ⑥ 회신 가시성

- agent 직접 대화 회신은 **main 컨텍스트 미배달**(현행 유지). 채널(대화창)에만 표시. 변경 없음 — 확정.

### H. 다중 사용자 동시성

- **같은 agent, 다중 사용자**: inbox drain-all(①)로 한 턴에 배치. 회신은 대화창 브로드캐스트(모든 뷰어 공유), 메시지별 author 라벨로 추적.
- **다른 agent, 다중 사용자**: 독립 worker 스레드라 진짜 병렬(무변경, 강점).
- **ask 선착순**: 서버 공유 상태, 먼저 답한 사람 것으로 소비(D).

### I. kill / resume / spawn 컨트롤 위치

- **결정 I-1 (확정)**: kill(✕)/resume(↻)은 **agent 채널 상단 소형 컨트롤**로 이동(그 agent 채널을 보고 있을 때 상단 바에 노출). 드롭박스는 선택만 담당. spawn은 여전히 main 위임(대화로 요청) — 드롭박스에서 새 agent 생성은 범위 밖.

### J. 흐름 / 전문 뷰 영향

- **흐름(스윔레인)**: 글로벌 조망 — **무변경**.
- **전문(타임라인)**: 전체 대화 타임라인(main + agent 스코프 포함) — **무변경**(채널 스코핑 안 함).

---

## 이벤트 / 데이터 흐름 (요약)

```
사람 입력 (채널=agt-X)
  → POST /api/agent/X/input → registry.request(X) → tm.inbox.put
  → agent_msg{in, author:user:<nick>, to:X}  (SSE) → 채널 X 엔트리(발신)
  → worker: inbox drain-all → process_batch → 회신
  → agent_msg{out, author:X}  (SSE) → 채널 X 엔트리(회신)
  (회신 main 미배달 — ⑥)

agent Y가 질문 (사람이 채널 X 보는 중)
  → agent_msg{question, to:...}, roster Y.state=waiting_ask  (SSE)
  → 글로벌 트레이 항목(Y) + 채널 Y 칩 ❓
  → 사람 답 → POST /api/agent/Y/input (트레이에서, target 무관)
```

---

## 변경 파일

**백엔드**
- `agent_cli/subagent/agents_live.py`: `_worker` inbox drain-all 배칭(C) + `process_batch`(목적지별 분리, C-1 결정 반영). `drain_nowait` 헬퍼.
- `agent_cli/web/server.py`: `/api/agent/<key>/input`은 불변(확인). 필요 시 트레이용 상태 노출 보강(대개 기존 이벤트로 충분).

**프론트**
- `agent_cli/web/static/app.js`: 채널 상태/드롭박스/뷰 스코핑(A·B), 개요 채널화(E), 글로벌 ask 트레이(D), 죽은 agent 처리(F). 기존 드로어 IIFE **제거**(데이터 수집 로직은 채널로 이관).
- `agent_cli/web/static/index.html`: 입력 옆 드롭박스 + 채널 바 + 트레이 마크업. 드로어 마크업 제거.
- `agent_cli/web/static/style.css`: 채널 칩/배지/rail/트레이/dead 입력 스타일. 드로어 스타일 제거.

**문서**
- `README.md`(사용자 대면 UI 변경), `docs/ARCHITECTURE.md`(app.js ov*/드로어 서술, agents_live worker 배칭).

---

## 무변경 보장 (회귀 0 목표)

- main 채널 개요 = 현행 동작 100% 보존(플랫 로그·주체 배지·복사/전체대화).
- 흐름 스윔레인·전문 타임라인 무변경.
- `/api/agent/<key>/input`·`/api/input`·roster/agent_msg 이벤트 계약 불변.
- ⑥(agent 회신 main 미배달) 불변.
- 게이트 e2e(`tests/browser/test_team_swimlane.py`) 유지.

---

## 범위 밖 (명시)

- 드롭박스에서 신규 agent spawn(여전히 main 위임).
- 전문/흐름의 채널 스코핑.
- agent↔agent 대화 UI(peer 통신은 백엔드 그대로, 사람 UI 아님).
- ask 상태의 서버측 재설계(기존 waiting_ask/input_required 재사용).

---

## 결정 로그 (확정)

- **C-1**: drain-all 배치에서 목적지 분리 — **사람-직접은 배치로 한 번에 회신, main-위임은 개별 처리(회신→mailbox)**. (제안 A)
- **I-1**: kill/resume 컨트롤 = **agent 채널 상단 소형 컨트롤**.
- **A-1**: 채널 상태 = **휘발**(리로드 시 main 초기화, localStorage 미사용).

---

## TEST_PLAN

**백엔드 (단위)**
- inbox drain-all: 큐에 N건 넣고 worker가 1턴에 배치 처리(회신 1회) — mock renderer.
- C-1 목적지 분리: 사람-직접 배치는 대화창 회신, main-위임은 mailbox 회신 검증.
- 회귀: 단일 메시지 = 종전과 동일(1건 배치).

**프론트 (node 하네스 + web 배선)**
- 채널 뷰 스코핑: `activeChannel` 별 렌더 소스 선택.
- 개요 target/source 배지 렌더(발신/회신).
- 글로벌 트레이: main confirm + agent question 동시 stack, 각 asker 라우팅 문자열.
- dead agent 입력 비활성 + static text.
- 드롭박스 옵션 = roster 반영, dead 표식.

**게이트 e2e**
- 채널 전환 → 해당 대화 렌더, 입력 라우팅.
- 타 채널 ask → 트레이 표시 + 칩 ❓ + 인라인 답변.
- 스윔레인/타임라인 회귀 무변경(`test_team_swimlane`).

**라이브**
- 다중 agent 대화 세션에서 채널 전환·타 채널 ask·drain-all 배치·dead 처리 실측.

---

## 단계별 구현 (각 단계 후 회귀 + 라이브 + 보고)

1. **백엔드 drain-all(C, C-1)** — worker 배칭 + 목적지 분리. 단위 테스트. (프론트 무변경, 회신 묶임만 관찰.)
2. **채널 모델 골격(A·B·E)** — 드롭박스 + activeChannel + 개요 채널 스코핑 + 라우팅 + 배지/rail. 드로어는 아직 공존(안전).
3. **글로벌 ask 트레이(D)** — main prompt/confirm + agent question 트레이 이관. 입력창 모드 단순화(chat 전용).
4. **죽은 agent(F) + 채널 칩 상태(🔔/❓/dead)**.
5. **드로어 제거** — 데이터 수집 채널 이관 확인 후 드로어 IIFE/마크업/스타일 삭제. 회귀 스윕.
6. **문서화 + 릴리스**.
