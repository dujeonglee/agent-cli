# agent-cli 기술 분석 (D:\yoon\codes\agent-cli, v7.28.1)

> 조사 기준일: 2026년 8월. 활용처: 논문 시스템 섹션 및 기술적 차별화 근거.

## 1. 프로젝트 핵심 목적과 주요 기능

**정체성**: `pyproject.toml:9` — *"ReAct pattern agentic loop CLI for on-premise LLMs"*. 온프렘/로컬 LLM(vLLM, LM Studio, mlx-lm/omlx) 환경에서 신뢰성 있게 도는 에이전트 하네스가 목표이며, 프론티어 API 전제의 기존 하네스(LangChain, CrewAI, AutoGen)와 대비되는 설계 의도가 `docs/robust-harness/DESIGN.md`에 명시되어 있습니다: *"로컬 35B급 모델을 Ollama·mlx-vlm·vLLM으로 돌리면 JSON envelope drift, tool name 환각, action_input 스키마 위반, 무한 루프 같은 실패가 일상적이다. 기존 agent harness들은 cloud API를 전제로 만들어졌고 실패 회복은 예외 처리로 붙여져 있다."* — 즉 **실패 회복을 1급 아키텍처 구성요소로 승격한 ReAct 하네스**가 이 프로젝트의 논문화 가능한 핵심 주장입니다.

**규모** (실측): Python 소스 36,378 LOC (약 100 파일), 테스트 49,847 LOC (88개 최상위 테스트 파일 + `tests/code_index/`, `tests/browser/` 하위 스위트). 프런트엔드 JS/CSS/HTML 약 5,840 LOC (`agent_cli/web/static/`). 문서 9,582 LOC (docs/ 30개 파일). 버전 `agent_cli/__init__.py:3` = `7.28.1`.

**의존성 최소주의**: typer, rich, requests, pyyaml, tree-sitter(+9개 언어 grammar)만 필수. 웹 UI는 optional extra(`agent-cli[web]` → fastapi/uvicorn/sse-starlette). `CLAUDE.md`에 "새 의존성 추가 최소화 (on-premise 배포 고려)"가 프로젝트 규칙으로 박혀 있습니다.

**주요 기능 축** (README.md 목차 기준):
- 멀티 프로바이더 (OpenAI 호환 / Anthropic), strict JSON Schema 미사용 — basic JSON mode + 3단계 파싱 폴백
- 교체 가능한 **wire format 플러그인** (`json_fc` 기본, `xml_fc`) — 모델별 바인딩
- 토큰 예산 기반 **컨텍스트 압축(compaction)**
- **code_index** — tree-sitter + SQLite 영구 심볼/참조 인덱스
- **서브에이전트** — 일회성 `run` + 상주 `spawn` 통합 (`agent` 도구)
- **LAN 웹 UI** — 다중 뷰어 협업
- Hooks (11개 라이프사이클 이벤트), Skills, MCP 연동, Jira export

---

## 2. 다중 사용자 세션 관리 — **존재함, 단 "공유 워커" 모델**

### 2.1 세션의 물리적 구조

`agent_cli/context/session.py` (216 LOC)는 놀랄 만큼 얇습니다. `SessionMeta`(session.py:27-35)는 `session_id`(생성 시각 epoch 문자열), `workspace`, `updated_at`, `response_format` 네 필드뿐이고 — **사용자 ID나 소유자 개념이 전혀 없습니다**. 저장 위치는 `{project}/.agent-cli/sessions/{session_id}/`:

```
session.jsonl      # 단일 라인 메타 (atomic_write_text, session.py:77)
history.jsonl      # 전체 대화 (append-only JSON Lines)
compaction.json    # 압축 상태 (resume 복원용)
memory.jsonl       # LLM 큐레이션 메모리
turns.jsonl        # 턴별 관찰 메타데이터
web.json           # 웹 인스턴스 sidecar (host/port/token/pid)
status.json        # 라이브 상태 (busy / awaiting_input / viewers / agents)
agents.json        # 상주 에이전트 manifest
agents/<key>/conversation.jsonl, replies/   # 에이전트별 대화·회신
run_{name}_{hash}_{ts}/, skill_{name}_{hash}_{ts}/   # 서브루프 subdir
```

세션 요약(`session_summary`, `recent_exchanges`, session.py:138-216)은 별도 메타 필드가 아니라 **history.jsonl에서 마지막 user↔complete 페어를 재구성**해 만듭니다 — 상태 중복 제거 설계.

### 2.2 다중 사용자 공동 세션: "모두 동등한 뷰어" 모델

`agent_cli/render/web.py:21-24`의 아키텍처 주석이 계약을 정의합니다:

> *"Multi-viewer, all equal: every authenticated connection is kept in `_connections`, receives the fan-out, AND may send input / queue messages. The worker thread is unaware of clients — it just emits, the renderer fans out to all."*

| 계층 | 구현 | 설명 |
|---|---|---|
| 프로세스 | `agent-cli web` 1 프로세스 = 1 세션 = 1 AgentLoop 워커 스레드 | 사용자별 격리 없음 |
| 접속 | `WebConnection` (id + SimpleQueue + closed Event), `render/web.py:128-135` | SSE 구독자 1개 |
| 인증 | 단일 공유 토큰 (`secrets.token_urlsafe(32)`), `secrets.compare_digest` 상수시간 비교, `web/server.py:274-281` | **사용자별 계정/권한 없음** |
| 신원 | 접속 시 랜덤 배정되는 "재미있는 닉네임" 20종 풀 (`_NICKNAMES`, web.py:104-125), 사용자가 편집 가능 (`POST /api/nickname`, server.py:1144) | 브라우저가 OS 사용자명을 못 읽으므로 친근한 자동 라벨이 실용적 대안이라고 코드 주석이 명시 (web.py:101-103) |
| 로스터 | `_viewers_payload_locked()` → `{count, viewers:[{id,name}]}` 를 전원에게 브로드캐스트 (web.py:415-432) | 접속/이탈 시 재방송 |

### 2.3 동시성 조정 메커니즘 (논문에서 다룰 만한 부분)

여러 사람이 한 에이전트를 공유할 때의 충돌을 세 가지 장치로 해결합니다:

**(a) 공유 입력 큐 (steering)** — `agent_cli/input_queue.py` (109 LOC). 아이템 shape `{id, conn_id, nickname, text}`. 에이전트가 실행 중이어도 누구나 메시지를 넣을 수 있고, 큐 상태가 전원에게 실시간 브로드캐스트되며(`WebServer._broadcast_queue`, server.py:258-262), **매 턴 경계에서 하나씩 디큐되어 대화에 주입**됩니다 (`AgentLoop._inject_queued_messages`, `loop/core.py:602-636`). 주입된 메시지는 run-starter와 **동일한 라우팅 경로**를 거칩니다 (`route_message` — `/sh`, `/compact`, `@agent`, `/skill`이 도착 시점과 무관하게 같게 동작; 근거 문서 `docs/intake-unification/DESIGN.md`). 취소는 소유자만 가능 (`cancel_pending`이 `conn_id` 일치를 검사, server.py:245-248).

**(b) 발신자 귀속(attribution)** — `loop/core.py:574-594`. 모든 사용자 메시지는 `[닉네임]: 텍스트` 형태로 라벨링되어 `task_log`와 `history.jsonl`에 누적되고, 레코드에 `author` 키가 additive로 실립니다. CLI/단일 사용자는 라벨 없이 raw 유지 (core.py:580 주석). 즉 **history가 다중 화자 트랜스크립트로 기능**합니다.

**(c) 선착순 응답 게이트** — `web/server.py:1100-1113`. 두 사람이 같은 `ask`/`confirm` 프롬프트에 동시에 답하면, `awaiting_input_kind()`와 kind가 일치할 때만 수용하고 늦은 답은 **HTTP 409**로 거절합니다. 주석이 실패 모드를 명시합니다: *"A keyless answer (flushed stale clicks from a connection-starved browser, the loser of a two-viewers race) must not sit in the input queue and auto-answer the NEXT prompt."* 클라이언트 쪽에서는 stale 다이얼로그가 자동으로 접힙니다.

### 2.4 세션 공유/참여의 실제 경로

- **동일 세션 다중 참여**: 토큰이 담긴 URL을 공유 → 여러 탭/PC가 같은 SSE 스트림을 보고 모두 입력 가능. 재접속 시 `_event_buffer`(최대 5,000 이벤트, `_EVENT_BUFFER_MAX`, web.py:57)를 replay하고, 초과분은 `transcript_truncated` 노티스로 알립니다 (web.py:26-34).
- **세션 이어받기**: `--resume <id>`. `run`과 `web`이 같은 on-disk 세션을 쓰므로 **run↔web 상호 이어가기**가 가능합니다 (README, v4.46.0).
- **부재 중 질문 대기** (v7.8.0): 보는 사람이 0명이어도 `ask`/위험 shell 승인/워크스페이스 밖 접근 승인이 즉시 "(no response)"로 포기하지 않고 `awaiting_input` 상태로 대기 → 나중에 접속한 사람이 replay된 질문에 답합니다.
- **외부 오케스트레이터("board")와의 계약**: `agent_cli/web/instance_file.py`가 핵심입니다. 도크스트링이 명시적으로 설명합니다 — 세션 디렉토리에 `web.json`(`{session_id, host, port, token, pid}`)을 쓰고 종료 시 제거해, 외부 "board" 서비스가 *"이 세션의 web이 떠 있나, 어디로?"* 를 파일 하나로 판정해 **spawn-or-attach**합니다(present + pid alive → 프록시; missing/dead → `agent-cli web --resume <id> --idle-timeout N` 재기동). 짝을 이루는 것이 `web/idle.py`의 `IdleMonitor` — 뷰어 0 + 워커 유휴 + 큐 비어 있음이 `--idle-timeout` 초 지속되면 자가 종료해 오케스트레이터가 프로세스를 추적·종료할 필요가 없습니다. **board 자체는 이 리포지토리에 없습니다** (코드 전반에 참조만 존재: `main.py:2173,2190,2265`, `render/web.py:155,1425,1464,1663`). 즉 다중 "방(room)" = 다중 게시물 워크스페이스를 관리하는 상위 멀티플레이어 계층은 별도 프로젝트입니다.

### 2.5 없는 것 (한계로 명시할 부분)

- 사용자 계정, 인증 주체, 권한 분리, 감사 로그 — 전무. 토큰 하나를 아는 사람은 모두 동등한 전권.
- 사용자별 컨텍스트 격리 — 없음. 모든 참여자가 **하나의 컨텍스트 윈도우와 하나의 워커 스레드**를 공유합니다 (그것이 협업 모델의 본질).
- 전송 암호화 — LAN 평문 HTTP 전제. README가 반복적으로 "신뢰된 네트워크에서만" 경고하며, Jira 자격증명은 서버에 저장하지 않고 각 사용자 브라우저의 localStorage에만 두어 **코멘트 작성자가 서버 계정이 아니라 그 사용자 본인**이 되게 합니다 (README §Jira export) — 이것이 다중 사용자 신원을 다루는 유일한 실질적 장치입니다.
- 브라우저 origin당 6연결(HTTP/1.1) 한도가 실제 운영 제약으로 문서화되어 있고, 승인 클릭이 3초 내 응답 없으면 "연결 정체" 경고를 띄우는 완화책이 있습니다.

---

## 3. 에이전트 실행 구조

### 3.1 루프 (`agent_cli/loop/*` — 약 3,200 LOC)

god-object였던 단일 `loop.py`를 협력 객체로 분해한 리팩터링(주석의 "C1 PR-1/2/3") 결과입니다:

| 파일 | LOC | 역할 |
|---|---|---|
| `core.py` | 834 | `AgentLoop` — 턴 실행, 세션 배선, 큐 주입, 에이전트 메일 배달 |
| `dispatch.py` | 1,329 | `TurnDispatcher` — 파싱 결과 → 도구 디스패치 + **회복 개입(recovery intervention) 분기** |
| `tool_bridge.py` | 416 | `ToolBridge` — hooks·invoke·RunContext·결과→관찰 seam 소유 |
| `llm.py` | 302 | `LLMCaller` — 프로바이더 호출, 오버플로 감지·재시도, 압축 트리거 (`_MAX_OVERFLOW_RETRIES = 5`) |
| `prompt.py` | 106 | `SystemPromptSvc` — 섹션 리스트가 단일 진실, `system` 문자열은 항상 join 파생 |
| `state.py` | — | `LoopConfig`(frozen dataclass) + `LoopState` + 센티널 `_CONTINUE`/`_RETRY`/`_NOT_HANDLED` |
| `run.py` | — | `run_loop()` 진입점 |
| `skill_invoke.py` | — | `run_skill` 처리 |

`recovery/` 패키지(7 모듈)가 하네스의 차별점입니다: `detectors.py`(stateful `ActionLoopDetector` + stateless `detect_unknown_tool`/`detect_schema_mismatch`/`detect_nested_envelope`/`detect_thought_missing`), `primitives.py`(순수 함수 개입 조각), `intervention.py`(개입 타입), `wf_recovery.py`(wire-format 의존 개입), `common_recovery.py`(wf 무관), `recursion.py`(재귀/깊이 차단 시 3가지 recovery option 제시), `observability.py`(`TurnRecord` → `turns.jsonl`; **LLM 생성 텍스트·사용자 프롬프트를 의도적으로 제외**한 구조 메타데이터만 기록 — 프라이버시 명시, observability.py:12-15).

### 3.2 에이전트 정의 (`agent_cli/agents/builtin/*.md`)

6개 내장 프로파일. YAML frontmatter(`name`/`description`/`allowed-tools`/`model`/`hooks`/`auto-spawn`/`disable-model-invocation`) + 본문(역할 = 서브에이전트 시스템 프롬프트의 Role 섹션 전체 교체). 검색 경로: `.agent-cli/agents/` → `~/.agent-cli/agents/` → 패키지 내장 (`subagent/profiles.py:1-11`).

- `code-writer` (read/write/edit/shell/code_index/memory/ask) — 파일 스코프 규율, `Files touched:` 보고
- `code-reviewer` (읽기 전용) — 구체적 실패 시나리오로 검증된 결함만 severity + `file:line`으로
- `code-analyst` (읽기 전용) — "어떻게 동작하나" 콜패스·수명 추적, 결함 판정 안 함(reviewer 몫)
- `unittest-writer` — 뮤테이션으로 무는지 증명
- `log-analyst` (읽기 전용) — 근본 원인
- `orchestrator` — **spawn 전용 조율자**, 도구가 `read_file/shell/code_index/memory/message`뿐 (스스로 spawn 불가)

역할 프롬프트는 `compose_role_prompt(profile_body, instructions)`로 파일 본문 + 인라인 오버레이("instant-agent")를 합성합니다 (`agents_live.py:135`).

### 3.3 오케스트레이션 / 서브에이전트 (`agent_cli/subagent/*`)

v5.0.0에서 구 `delegate`(일회성)와 `teammate`(상주)를 **단일 `agent` 도구**로 통합했습니다 (`docs/agent-unification/DESIGN.md`). `mode`로 두 수명 모델을 오갑니다.

**일회성 (`mode:"run"`)** — `subagent/oneshot.py` (461 LOC): 컨텍스트 모드 `none`(독립) / `fork`(부모 컨텍스트 복사). 한 턴에 run op 여러 개를 내면 **threading으로 병렬 팬아웃**하며, 상주 모드 op이 섞이면 순차로 떨어지는 mode-aware 배칭. 결과는 구조화 포맷(`STATUS:` / `RESULT:` / `[Subagent activity]` / `[Files touched]` / `[Duration]`)으로 반환되고 `run_*/result.md`에 저장 (`subagent/report.py`, 265 LOC).

**상주 (`spawn`/`request`/`status`/`resume`/`kill`)** — `subagent/agents_live.py` (1,578 LOC). 스레딩 모델: *"teammate 하나 = 데몬 worker 스레드 하나. worker는 자기 inbox를 블록해 메시지당 `run_subagent_message` 1회를 돌리고 회신을 registry의 공용 pending 리스트에 push한다. 상태 전이(idle→busy→idle→…→dead)는 worker 루프 한 곳에만 있다."*

핵심 설계 결정들:
- **비동기 mailbox + harness 배달**: `request`는 즉시 반환. 회신은 LLM 폴링이 아니라 main 루프가 턴 경계에서 `AgentRegistry.drain_replies()`(agents_live.py:744)를 비워 관찰 레코드로 주입 (`AgentLoop._deliver_agent_mail`, `core.py:638`).
- **인터럽트 분리**: worker는 자기 `stop_event`만 본다 — main의 Ctrl+C / `/api/stop`은 상주 에이전트를 죽이지 않는다 (agents_live.py:17-20).
- **에이전트↔에이전트 메시징** (v5.11.0): 커널 기본 탑재 `message` 도구. `{"to": "<key>|main", "text": "..."}`. 배달된 회신은 **terminal**(되받아치지 않음)이라 핑퐁이 없고, `_MAX_PEER_HOPS = 6`이 안전망 (agents_live.py:73-77).
- **양방향 문답**: 상주 에이전트의 `ask`는 사용자가 아니라 **main LLM의 mailbox**로 라우팅 (main이 그 에이전트의 "사용자"). run 서브에이전트의 `ask`는 종전대로 사용자에게.
- **영속화**: `agents.json` manifest — 역할 프롬프트까지 저장. `--resume` 시 살아있던 에이전트를 대화 이력 그대로 자동 재생성(`restore()`, agents_live.py:968), kill/사망 에이전트도 `mode:"resume"`로 부활(`resume_teammate()`, :838). `agents/<key>/conversation.jsonl` 재생(`_replay_conversation`, :1077)으로 웹 대화창까지 복원.
- **상한**: 동시 생존 기본 10 (`_DEFAULT_MAX_AGENTS`), 웹 UI에서 세션 한정 조절(`/api/max-agents`).
- **KV 캐시 의식적 설계**: `_membership_changed` threading.Event — 멤버십 변화에서만 `## Live Agents` 시스템 프롬프트 섹션 재조립 (agents_live.py:81-88).
- **`MailWaker`** (agents_live.py:1349): main이 idle이어도 회신 도착 시 run을 자동으로 깨워 배달. CLI `run` 커맨드도 같은 공용 `InputQueue` 위에서 도는 "큐 펌프"라, 모델이 complete해도 상주 에이전트가 일하는 중이면 quiescence까지 기다립니다 (`main.py:1385 _run_message_pump`).

**팀 부트스트랩**: `skills/builtin/orchestrate.md` — plan → spawn(orchestrator + 워커들) → 로스터 핸드오프 → 반환. 이후 orchestrator가 peer message로 배정→수집→리뷰→수리 루프를 main을 깨우지 않고 자율 주행.

### 3.4 도구 브리지 (`agent_cli/tools/*`)

`tools/registry.py`가 13개 Tool 인스턴스를 수집해 `TOOLS` dict 구성. 삽입 순서는 **시스템 프롬프트 KV 캐시 안정성을 위해 역사적 순서를 유지** (registry.py:37-39):

- 실도구 — `read_file`, `write_file`, `edit_file`(hashline/CRC32 정밀 편집), `shell`, `code_index`, `read_context`(history를 SQL로 질의), `memory`, `fetch`, `agent`
- 가상도구 (`tools/virtual.py`, 루프가 인터셉트) — `complete`, `ask`, `message`, `run_skill`

워크스페이스 경로 봉쇄는 `tools/_confine.py` (248 LOC) — write/edit/shell만 대상, read 제외. 코드/문서가 정직하게 한계를 밝힘: *"이건 사고 방지용 speed bump이지 샌드박스가 아니다."*

### 3.5 MCP 연동 (`agent_cli/mcp/*`)

`config.py`(`.agent-cli/mcp.json` / `~/.agent-cli/mcp.json`, `${VAR}` 확장) + `client.py`(`McpClientManager` — stdio / SSE 두 transport) + `adapter.py`(`McpTool`). **MCP 도구를 `Tool` 서브클래스로 감싸서** 네이티브 도구와 완전히 동일한 검증·디스패치 경로를 타게 함. 도구 이름은 `{server}.{tool}`로 충돌 방지. 설계 문서 `docs/mcp-integration/DESIGN.md`.

---

## 4. 컨텍스트 관리

### 4.1 `agent_cli/context/` (약 1,690 LOC, 8 모듈)

`manager.py` (911 LOC)가 중심. 매 LLM 콜 직전 `ensure_within(target)`(**flow 1, 예방**) — 캐시가 `(context_window − system_실측 − max_output) × 0.8`을 넘으면 compaction 패스:
1. 캐시를 system anchor + dynamic으로 분할
2. dynamic의 오래된 절반을 **토큰 기준**으로 evict
3. 주입된 compactor 콜백으로 LLM이 evict 묶음을 **단일 호출**로 요약 (TASK/STATE/DONE/PENDING/DECISIONS/FAILURES/FACTS 구조화 섹션). 이전 요약이 있으면 같은 호출에 prepend — **recursive single-call**
4. evict 안에서 파일 경로 추출 (`_file_extract.py` — 각 Tool의 `touched_paths()`에 위임)
5. 캐시 재구성 `[system, summary, file_list, retained dynamic]`
6. `compaction.json` 영속화 (resume 시 이미 요약된 꼬리를 다시 읽지 않음)
7. **Belt-and-braces**: 실패 시 플레인 FIFO drop 폴백

콜 직후엔 서버 실측 `usage`로 reconcile하고, 서버가 400(prompt too long)을 던지면 `force_fit`으로 사후 축소 후 bounded 재시도(**flow 2, 반응**; `loop/llm.py`).

**모듈 분해**: `store.py`(71, 순수 디스크 I/O primitive — 클래스로 승격하지 않은 이유를 도크스트링에 기록), `records.py`(158, on-disk record shape 계약), `render.py`(218, 재공급 렌더와 예산 추정이 같은 view를 세는 쌍둥이 불변식), `overflow.py`(107, 프로바이더별 오버플로 에러 패턴 정규식 — "verified against a live omlx server, 2026-05-30"), `token_estimator.py`(10, `len(text) // 4` 휴리스틱 + 서버 실측 reconcile), `session.py`(216).

**메모리 (`agent_cli/memory.py`, 245 LOC)** — compaction 면역 저장소. 타입 4종(failure/discovery/decision/note), 요약 인덱스는 시스템 프롬프트 `## Session Memory`에 상시 노출, 전체 내용은 `mode=get` 온디맨드. 설계 문서 `docs/session-memory/DESIGN.md`.

**웹 노출**: 압축 임계 세션 한정 슬라이더(50~95%, `GET/POST /api/compaction`), web과 loop이 **같은 `ContextManager` 인스턴스를 공유**. Prompt Inspector(`⚡`, `GET /api/debug/prompt`)는 시스템 프롬프트 섹션별 토큰 스택바 + 실제 컨텍스트 윈도우 내용 표시.

### 4.2 `agent_cli/code_index/` (약 4,900 LOC, 21 모듈)

**출처**: minish.ai/Agent-tools의 `tsindex.py`에서 포팅 (Apache 2.0, `NOTICE`에 수정 목록).

**역할**: `read_file`이 텍스트(line range)에 답한다면 `code_index`는 **의미 단위(symbol)와 cross-file 관계(refs/callers/callees)**에 답합니다. tree-sitter로 전체 파싱 → `<root>/.agent-cli/code_index.db` 영구 SQLite. lazy build → sha1 비교 **incremental rebuild**, `edit_file`/`write_file` 성공 시 자동 post-hook 갱신.

**빌드 알고리즘** (`builder.py`, 491 LOC): Pass-1 `walk_definitions`(변경 파일) → 추가된 이름 집합 계산 → Pass-2 `walk_refs`(변경 파일 + unchanged-but-affected 파일) → 새 SQLite 파일로 원자 교체.

**스키마** (`SCHEMA_VERSION = 2`): `NAME_KINDS` = function/type/variable/constant/section, `REF_KINDS` = call/name/type. `qualified_name` 컬럼 (Python/JS/TS/Java/Go/Rust/MD는 `.`, C++는 `::`).

**지원 언어 9종**, **C/C++ 전처리** (`preproc.py` 473 + `_unifdef.py` 632): `.agent-cli/defconfig` 기반 unifdef 분기 제거, 번들 pure-Python 구현 기본 + 시스템 바이너리와 byte-identical parity 테스트.

**도구 표면** (`tools/code_index.py`, 760 LOC): 10 모드 — list / fetch / lookup / kind / file / refs / callers / callees / slice / build. `fetch` 출력이 `read_file`과 동일한 hashline 포맷이라 **재읽기 없이 `edit_file`에 직결**. 설계 문서 `docs/code-index/DESIGN.md` (571 LOC).

---

## 5. 서버 / 원격 / 협업 기능

### 5.1 웹 서버 (`agent_cli/web/`, 1,824 LOC Python + 5,840 LOC 프런트엔드)

FastAPI + uvicorn + sse-starlette, optional extra. 기본 bind `0.0.0.0`(LAN 노출), 포트 `0xC0DE`(49374) 우선.

**API 표면** (약 30개 엔드포인트, 전부 토큰 인증):
- 스트림/입력: `GET /api/stream`(SSE), `POST /api/input`(chat/prompt/confirm), `/api/queue/cancel`, `/api/nickname`, `/api/abort`, `/api/stop`
- 상주 에이전트: `POST /api/agent/{key}/input|resume|kill`
- 검사/제어: `/api/health`, `/api/debug/prompt`, `/api/directives`, `/api/compaction`, `/api/max-agents`
- 워크스페이스: `/api/workspace/tree|download|delete|upload` — traversal 차단(`_safe_workspace_path`), 파일당 50MB 상한
- 내보내기: `/api/export/html`, `/api/export/jira`

**원격 배치 지원**: `--trust-local`(loopback 요청 토큰 생략 — 앞단 인증 게이트웨이용), `--base-path`(리버스 프록시 prefix), `--idle-timeout`(자가 종료).

**협업 시각화 — Team 스윔레인**: `static/team_model.js`(362) + `team_view.js`(558). 세로축이 균등 시간축이 아니라 **이벤트 기준** 시퀀스 다이어그램 — 에이전트 간 피드백 왕복 구조가 한눈에 보임. `task_id`로 스윔레인 막대와 타임라인 카드 연결 (v7.28.0).

**인간 개입(🤝 대화창)**: roster + 선택 에이전트의 요청/회신/질문 스트림. 사람이 직접 메시지 전송 가능(닉네임 attribution), 에이전트 `ask` 대기 중이면 그 메시지가 답으로 소비. **인간↔에이전트 문답은 창에만 표시되고 main 컨텍스트에 배달되지 않음**(오염 방지).

**실브라우저 테스트** (`tests/browser/`, playwright+chromium, 옵트인): confirm 흐름, 헤더/스톨, resume replay, 태스크 그룹 접기, 팀 스윔레인.

### 5.2 외부 통합

- **Jira** (`integrations/jira.py`, 236): Cloud(ADF) / Server·DC(wiki markup) 자동 판별. 자격증명은 각 사용자 브라우저 localStorage에만 — **코멘트 작성자가 그 사용자 본인**이 되게 하는 다중 사용자 설계.
- **HTML export** (`integrations/export.py`).
- **SWE-bench 하네스** (`bench/swebench/`) — 논문 정량 평가에 쓸 수 있는 기존 인프라.
- **Bakeoff 하네스** (`scripts/bakeoff/`): wire format A/B 실험. v6.0.0 전환이 **A/B 140run 게이트** 통과 후 이루어진 기록 (`docs/multi-wire-format/PHASE4.md`).

---

## 6. docs 폴더 — 30개 문서, 9,582 LOC

대부분 `Status / Date / Owner / Companion` 헤더, 다수가 DESIGN + REQUIREMENTS + TEST_PLAN 3종 세트.

| 문서 | LOC | 내용 |
|---|---|---|
| `ARCHITECTURE.md` | 1,507 | 15장 종합 아키텍처. **헤더 수치 stale (2.0.0-dev/23,600 LOC vs 실제 7.28.1/36,378 LOC) — 인용 시 코드 대조 필요** |
| `robust-harness/DESIGN.md` | 268 | 로컬 모델 실패 모드 taxonomy와 회복 계층. **논문의 핵심 논거** |
| `robust-harness/REMAINING_DEBT.md` | 133 | 의도적 기술부채 공개 관행 |
| `context-compaction/` | 1,288 | 압축 DESIGN/REQUIREMENTS/TEST_PLAN 3종 세트 |
| `context-redesign/` | 771 | ContextManager/Scratchpad/ArtifactStore 통합 |
| `code-index/DESIGN.md` | 571 | tsindex 포팅 계약 |
| `inputs-array-schema/DESIGN.md` | 454 | 멀티-op wire format — 미검증을 명시하는 정직한 문서 |
| `teammate/DESIGN.md` | 429 | 상주 세션 에이전트 설계 |
| `benchmark-openharness.md` | 349 | OpenHarness(HKUDS) 대비 아키텍처 비교 — **관련 연구 섹션 재료** |
| `multi-wire-format/` | 606 | Phase 1~4, **Qwen 27B/35B-A3B 실측 수치 포함** |
| `agent-unification/DESIGN.md` | 285 | delegate + teammate → `agent` (v5.0.0) |
| `mcp-integration/DESIGN.md` | 274 | MCP 통합 |
| `jira-http-and-nickname-edit/` | 591 | 닉네임 중간 변경 등 다중 사용자 UX 3종 세트 |
| `optimization-audit/REPORT.md` | 234 | 전수 교차검증 감사 |
| `directive-learning/DESIGN.md` | 230 | v4.25.0에서 자동생성을 CoT-leak 때문에 전면 제거한 기록 |
| `session-memory/DESIGN.md` | 152 | compaction 면역 저장소 |
| `intake-unification/DESIGN.md` | 134 | 사용자 메시지 intake 단일 라우팅 — **다중 사용자 큐 스티어링의 정합성 근거** |
| `history-schema/DESIGN.md` | 70 | history.jsonl enrich + read_context JSON 쿼리 |

---

## 7. 논문 작성 시 주목할 만한 지점

1. **"공유 워커 멀티플레이어" 모델**은 흔한 패턴이 아닙니다. 다중 사용자 에이전트 시스템은 보통 사용자별 세션 격리를 택하는데, 이 프로젝트는 반대로 **하나의 컨텍스트·하나의 워커를 여러 인간이 공유**하고 충돌을 큐 순서화 + 선착순 게이트(409) + 발신자 라벨링으로 해결합니다. 근거: `render/web.py:21-24`, `web/server.py:1100-1113`, `loop/core.py:574-594`, `input_queue.py`.

2. **인간과 에이전트가 같은 메시지 버스를 공유**합니다. `InputQueue`에 사용자 메시지도, `MailWaker`가 넣는 에이전트 회신 wake 아이템도 함께 들어가고, CLI `run`과 web이 동일한 큐 펌프 위에서 돕니다. "다중 인간 + 다중 에이전트"가 하나의 조정 구조로 통일된 사례입니다.

3. **실패 회복의 1급화**: `recovery/` 패키지의 primitive → intervention → composer 분리, `turns.jsonl`의 프라이버시 보존 관찰 스키마.

4. **KV 캐시를 의식한 프롬프트 구조**: 도구 순서 고정, 멤버십 변화에만 반응하는 `## Live Agents` 재조립, primacy/middle/recency 레이아웃.

5. **온프렘 제약의 체계적 대응**: strict JSON Schema 미사용, pure-Python unifdef 번들, SQLite 폴백 휠 marker 축소, 의존성 최소화.

6. **주의사항**: `docs/ARCHITECTURE.md` 헤더 수치는 stale. CHANGELOG도 7.27.x 다음 4.27.1 순서 이상 존재. 수치 인용 시 코드에서 직접 재측정할 것.
