# Agent-CLI v2 아키텍처 문서

> **이 문서는 코드와 함께 유지보수되어야 합니다.**
> 코드 수정 시 관련 섹션을 반드시 업데이트하세요.
>
> 최종 업데이트: 2026-05-25
> 버전: 2.0.0-dev
> 총 소스: ~22,800 LOC (89 Python 파일) + ~27,200 LOC 테스트 (70 파일)
> 총 테스트: 1814 유닛 + 22 통합 (36 ollama_integration deselected)

---

## 1. 프로젝트 개요

Agent-CLI는 on-premise LLM을 위한 모듈형 에이전트 CLI입니다. ReAct(Reasoning + Acting) 패턴으로 LLM이 도구를 사용하여 단계적으로 작업을 수행합니다.

### 핵심 특징

- **멀티 프로바이더**: Anthropic, OpenAI 호환(vLLM, LM Studio, mlx-lm), Ollama
- **3단계 파싱 폴백**: json.loads → JSON repair → regex 추출
- **Basic JSON Mode**: Ollama `format="json"`, OpenAI `response_format={"type":"json_object"}`, Anthropic tool calling (strict JSON Schema는 확장성 위해 미사용)
- **Hashline 편집**: CRC32 해시 기반 정밀 파일 편집 + 퍼지 매칭
- **컨텍스트 관리**: 매 LLM 호출 직전 `(C−S−O)×0.8`(S=system 실측) 초과 시 LLM 요약 compaction (recursive single-call), 실패/재구성 후 미충족 시 FIFO drop으로 belt-and-braces fallback, 호출 직후 서버 실측 `usage`로 reconcile (flow 1 예방); 추정이 빗나가 서버가 400(prompt too long)을 던지면 `force_fit`으로 compact→FIFO 사후 축소 후 bounded 재시도 (flow 2 반응); history.jsonl 영속화 + compaction.json (resume용 dynamic_start_index)
- **모델 적응형**: context window, thinking budget에 따른 자동 조정

### 외부 의존성

| 패키지 | 버전 | 용도 |
|--------|------|------|
| `typer` | >=0.9 | CLI 프레임워크 |
| `rich` | >=13.0 | 터미널 렌더링 (Panel, Table, Rule 등) |
| `requests` | >=2.28 | HTTP 클라이언트 (LLM API 호출) |
| `pyyaml` | >=6.0 | 스킬 frontmatter 파싱 |
| `tree-sitter` | >=0.23 | code_index 파서 코어 |
| `tree-sitter-python` / `-javascript` / `-typescript` / `-cpp` / `-go` / `-rust` / `-java` | >=0.23 | code_index 언어 grammar |
| `tree-sitter-markdown` | >=0.3 | code_index markdown heading 인덱스 |
| `pysqlite3-binary` | >=0.5 | code_index SQLite fallback (Linux only — `--without-sqlite` 빌드된 CPython 대비) |

**Optional**: `agent-cli[web]` → `fastapi` / `uvicorn[standard]` / `sse-starlette`.
**Dev**: `pytest`, `pytest-asyncio`, `httpx`, `hypothesis` (property-based 테스트).

**시스템 패키지**: C/C++ 인덱싱의 `unifdef` 단계는 번들된 pure-Python (`_unifdef.py`) 이 기본 처리 — 설치 불필요. 시스템 `unifdef` 바이너리가 있으면 자동 우선 사용 (battle-tested C). `AGENT_CLI_UNIFDEF=pure|system|auto` 환경변수로 명시적 강제 가능.

표준 라이브러리: json, re, dataclasses, pathlib, os, sys, zlib, textwrap, unicodedata, copy, tempfile, threading, sqlite3 (code_index — stdlib 우선, 미존재 시 `_sqlite.py` shim 이 `pysqlite3-binary` 로 폴백)

---

## 2. 디렉토리 구조

```
agent_cli/
├── __init__.py              (3)    패키지 버전 (__version__ = "2.0.0-dev")
├── __main__.py              (5)    python -m agent_cli 진입점
├── main.py                  (~1680) CLI 명령어: run, chat, web, setup, sessions, --style, --response-format, resume preview. **`DispatchOutput` Protocol + `_ConsoleDispatchOutput` + `try_dispatch_agent_or_skill`** — `@<agent>`/`/<skill>` 접두사 처리 (listing, invocation, not-found) 공유 dispatcher. chat REPL은 `_ConsoleDispatchOutput`(Rich 색상), `agent-cli web` worker는 `web.server.WebDispatchOutput`(observation 이벤트) 어댑터 주입. unknown `@`/`/` 명령은 LLM으로 통과하지 않고 error observation 발사 (오타로 인한 사고성 LLM round-trip 방지). **`web` 명령**: `--resume <id>` 지원 — provider 핸드셰이크 전에 `load_session` pre-check로 unknown ID fail-fast, `ContextManager(resume=True)` 로 캐시 복구 후 `renderer.replay_from_history(ctx)` 한 번 호출해 persistent event buffer를 재구성 → 이후 새 SSE 연결의 snapshot replay로 이전 turn이 그대로 UI에 복원. **graceful shutdown**: `uvicorn.Server(config).run()` 직접 호출 + `KeyboardInterrupt` swallow + `finally` 블록에서 `renderer.shutdown_all_connections()` → `server.shutdown()` → `worker.join(timeout=2s)` → `finalize_session(...)` 순서로 정리 (lifespan shutdown 훅이 SSE generator를 먼저 닫아도 idempotent).
├── resource_loader.py       (144)  ResourceLoader — 파일 검색/우선순위 (스킬/에이전트/지시사항)
├── config.py                (217)  config.json 3레이어 로딩 + models.json 레지스트리
├── setup.py                 (329)  SetupWizard (Rich TUI, 첫 실행 설정 마법사 — 기존 config 노출 + 프로브 진행 표시). 모델 선택: ollama는 `/api/tags`, OpenAI 호환(omlx·vLLM·LM Studio)은 `/v1/models`(`_list_openai_models`)로 목록 표시 후 선택, 실패 시 수동 입력. anthropic은 수동
├── constants.py             (~25)  공유 상수 (timeout, observation 템플릿, INTERRUPT_NOTICE). 외부 모듈 의존 없음 — 저층 레이어. wire-format-specific 상수 (FORMAT_RULES, RETRY_HINT_*, SYSTEM_USER_PREFIXES) 는 ``wire_formats/`` 의 plugin이 소유
├── wire_formats/                   Wire format 플러그인 시스템 — 모델 응답 형식 추상화
│   ├── __init__.py          (132)  Registry (`register` / `get` / `list_names`) + `all_system_user_prefixes()` (format-agnostic + plugin prefix 통합 entry point). builtin plugin (react, prefix_md) 자동 등록.
│   ├── base.py              (410)  `WireFormat` ABC + `ParsedAction` dataclass. Plugin 베이스 클래스 — abstract method (format-specific 부분만, plugin이 반드시 구현)와 concrete default (lifecycle / 식별 hook, 보통 그대로 상속) 분리. Abstract: render_full_example / format_rules_anchor / format_rules_field_specific / parse / 6개 recovery wording / system_user_prefixes. Default: format_rules = `build_format_rules(self)`, render_action_input = identity, normalize_assistant_for_messages = identity, provider_call_kwargs = `{}`, prefill = `""`, serialize_assistant_for_history = `self.parse()` + 구조화 필드 추출, render_assistant_from_history = `self.render_full_example()` 호출로 wire shape 재방출. 모듈 docstring에 assistant turn lifecycle (A → B/C, B → D) 표 포함. plugin 추가 = WireFormat 상속한 새 파일 1개, main code 0 변경.
│   ├── react.py             (655)  ReActFormat — 기본 plugin. ReAct-shape 문자열 (JSON `{thought, action, action_input}`) + recovery wording + 3-stage fallback parser (`parse_react`) + stage-2 JSON repair helper (`repair_json`) 모두 self-contained. WireFormat ABC 상속해 lifecycle default 사용 — format-specific 메서드만 정의. (이전 EnvelopeFormat은 2026-05-10 측정 후 폐기 — Phase 1 bakeoff에서 mistral 0% / qwen thought 9.5%로 wire-shape 결정성 약점 확인)
│   └── prefix_md.py         (~436) PrefixMdFormat — 마크다운 H2 헤딩 wire format (`## Thought / ## Action / ## Input`). small-LLM이 XML envelope보다 자연스럽게 emit하도록 설계. parser: strict `^## X$` line-anchored 매칭, last-wins on `## Action`+`## Input` (sub-header drift 방어), action body는 단일 토큰 (`^[\w.-]+$`) 검증. 4-state parse_stage (0=no Action, 1=full, 2=Action 있고 Input 깨짐, 3=Action body invalid). provider_call_kwargs override (`skip_json_format=True`) — Ollama format=json 모드가 `{` 강제하는데 markdown은 `## `로 시작이라 충돌. 나머지 lifecycle은 ABC default 사용.
├── recovery/                       Robust Harness Recovery Layer (docs/robust-harness/DESIGN.md)
│   ├── __init__.py                 primitive·detector·observability 재export (common_recovery / wf_recovery는 호출처가 import — 패키지 자체 format-agnostic 보존)
│   ├── common_recovery.py   (~65)  WF-agnostic Intervention factory — `format_action_loop_intervention` (B1). 모든 plugin이 같은 텍스트를 봄. 새 wire-format plugin 추가 시 0 변경
│   ├── wf_recovery.py       (~110) WF-aware Intervention factory — `format_no_json_retry` (A1a), `format_no_action_retry` (A3). plugin의 framing/reminder/static fallback 사용. WF 의존이 한 파일에 모여 audit 용이. ReAct-only NO_THOUGHT recovery는 `ReActFormat.format_no_thought_retry` 메서드 (plugin = boundary)
│   ├── detectors.py         (~250) 감지기 모음. stateful: `ActionLoopDetector` (B1, turn 간 (action, args) 추적). stateless: `detect_unknown_tool` (A4), `detect_schema_mismatch` (A5, `validate_tool_input` wrap), `detect_nested_envelope` (A6, complete 결과의 이중 래핑 감지 — 관찰 전용), `detect_thought_missing` (A7, action 있고 thought 없음 — mimicry-strengthening loop trigger; loop이 `wire_format.thought_required` 가드 후 호출. `complete` 액션은 제외 — 최종 답이라 next-turn 의무 없음, Phase 2 bakeoff 2026-05-18에서 27b prefix_md complete_direct 5/5 recovery loop 해소 측정).
│   ├── intervention.py      (~30)  `Intervention` dataclass — primitive 합성 결과 (message + 적용된 primitive 이름)
│   ├── observability.py     (~160) `TurnRecorder` — 세션별 `turns.jsonl` 추가-only writer; `TurnRecord` 스키마(seq, model, parse_stage, failure_signal, primitives_applied). FAILURE_* 라벨 8종 (NO_JSON / NO_OUTPUT / NO_ACTION / NO_THOUGHT / UNKNOWN_TOOL / SCHEMA_MISMATCH / NESTED_ENVELOPE / ACTION_LOOP)
│   └── primitives.py        (~109) format-agnostic 회복 primitive (`echo_prior_output`, `probe_progress`, `restate_task`) — provider/모델/채널/wire format 이름 모름. ReAct-shape constraint reminders는 ``ReActFormat`` 가 소유
├── default_models.json             패키지 기본 모델 정의 (6개 모델)
├── hooks/                          Hook 시스템 (Python + Shell 라이프사이클 훅)
│   ├── __init__.py          (24)   shell hook API re-export (하위 호환)
│   ├── shell.py             (236)  Shell hook (PreToolUse/PostToolUse/PostToolUseFailure)
│   ├── events.py            (53)   11개 이벤트 상수 + EVENT_TO_FUNC 매핑
│   ├── context.py           (145)  HookContext (messages 조작, system prompt 주입, MCP 메모리, 도구 제어)
│   ├── loader.py            (88)   Python hook 파일 스캔/로드 (.agent-cli/hooks/*.py)
│   └── runner.py            (95)   HookRunner (이벤트 발화, Python→Shell 순서 실행)
├── input_history.py         (174)  readline/gnureadline 설정 + 채팅 히스토리 영속화 (CJK 지원, paste/IME 디코드 오류 방어)
├── verbose.py               (27)   공용 verbose 플래그 + debug_log (providers가 loop을 역참조하지 않도록 추출)
├── loop.py                  (~1980) AgentLoop 클래스 + 에이전트 루프 (wire_format plugin 통합 — parse / system prompt / recovery builders / NO_THOUGHT 가드 / messages 버퍼·history.jsonl 저장의 assistant 표현, token-budget compaction + FIFO fallback, hook, streaming, nested depth rendering, failure-grounding retry). 생성 시 `ctx.set_compactor(self._llm_compact_summarize)` + `ctx.set_recorder(self.recorder)`로 compaction 진입점을 ContextManager에 주입; `--no-compaction` / `AGENT_CLI_COMPACTION=off`면 미주입 → FIFO만 동작. **Tool dispatch safety net**: `_dispatch_tool_with_hooks` 가 invoke 단계 (`_invoke_regular` / `_invoke_delegate`) 를 try/except Exception 으로 감싸 unhandled exception 을 `ToolResult(False, error="Tool 'X' raised … retry or different approach")` 로 변환 → post-hooks + observation 정상 흐름, LLM 이 다음 turn 에서 retry 결정 가능. `KeyboardInterrupt` / `SystemExit` 는 의도적으로 통과시켜 Ctrl+C 종료 보장. 전체 traceback 은 `_debug_log` 로 보존, LLM observation 은 짧게 유지. **Unified call-depth ceiling**: `__init__` 가 `depth >= max_depth` 시 `delegate` AND `run_skill` 둘 다 tools_list 에서 제거 (대칭). `execute_skill` 이 `parent_depth + 1` 전달 → skill 체인도 depth 카운트. cycle (`skill_stack` / `agent_stack` 검사) + depth 한계 위반 시 `recovery/recursion.py` 의 actionable helper (3가지 recovery option) 로 응답. dispatch 단계 belt-and-suspenders check 가 직접 caller 도 보호. 시스템 프롬프트 `## Execution Context` 가 `depth N/M` 표시 + 한계 도달 시 명시 (KV cache: section 위치 그대로 — 한 loop 내 depth 불변이라 영향 0).
├── render/                         플러그인 가능 렌더링 + 사용자 입력 시스템
│   ├── __init__.py          (~270) 렌더러 디스패치 + load_renderer_by_name + render crash 방어 + observation success 전달
│   ├── base.py              (~320) Renderer ABC + `ConfirmOption` dataclass. 출력 메서드 19개 (depth, capture, group, thread_status, thinking 등) + 입력 메서드 2개 (`prompt_user` 자유 입력 — optional `context` kwarg로 pre-input 안내(예: ask 도구의 질문 블록)을 전달, `confirm` 선택지+코멘트). 입력도 추상화에 포함해 web UI 같은 비-CLI renderer가 SSE+POST로 같은 인터페이스 만족할 수 있게. **`begin_delegate_task` / `end_delegate_task`** concrete no-op lifecycle 메서드 — CLI 렌더러는 그대로 무시(rich.Live가 자체 처리), WebRenderer만 override해서 thread→task_id 매핑 + SSE 마커 발사. `delegate.py::_run_parallel` 워커는 둘을 무조건 호출 → 렌더러 타입 분기 없음.
│   ├── minimal.py           (~600) MinimalRenderer — 유일한 번들 렌더러. **출력**: nested depth, markdown, ASCII-art talking-face streaming progress with token counter + 시간 기반 프레임 throttle + 폭 통일 패딩 + 좁은 터미널 안전망 + resize-recovery, ASCII-art thinking spinner, `FrameClock` 공유 (delegate 병렬 패널이 동일 cadence로 reuse), write_file/edit_file unified-diff 렌더링, ToolResult.success 직접 전달로 정확한 ✓/✗ 표시, capture, group blocks, CJK+Ambiguous width, verbose에서 provider thinking 블록 표시. **입력**: `prompt_user`는 multiline 시 `input_history.read_rich_input` (paste + `"""..."""` 블록 지원), 단일 줄은 stdin `input()`; EOF/Ctrl+C는 호출자 정책 분기를 위해 전파. `confirm`은 첫 토큰 매칭 (key + aliases, case-insensitive), EOF/empty/unrecognized는 `default_key` 반환. 커스텀은 `render/{name}.py`에 Renderer 서브클래스를 두면 `--style {name}`으로 로드됨
│   └── web.py               (~680) WebRenderer — `agent-cli web` 전용. 모든 Renderer emit이 (1) `_event_buffer`에 (persistent만) 누적 + (2) 활성 SSE connection의 queue에 push. `thought()` 는 즉시 emit 안 하고 다음 `action()` / `final()` 에서 `assistant_turn` 한 이벤트로 묶음 (LLM 한 emission = 프런트 카드 한 개). `prompt_user` / `confirm` 은 `input_required` 이벤트 push 후 worker thread에서 `_input_queue.get()` blocking, POST /api/input 이 도착하면 깨움. `prompt_user(context=...)` 는 ask 도구의 질문 텍스트를 `input_required.context` 필드로 그대로 전달 → 프런트가 ANSWERING 칩 옆 패널로 렌더 (스크롤 없이 질문 즉시 노출). **세션 정보 ``ready`` 이벤트는 별도 ``_latest_ready`` slot에 보관** — buffer와 분리해서 chat REPL 재진입 시 N개 누적 방지 + 새 connection snapshot 앞에 prepend → 첫 chat turn 전에 페이지 새로고침해도 top-bar 즉시 채워짐. **`_latest_worker_state` slot도 같은 패턴**: `worker_busy()` / `worker_idle()` 가 chat 메시지 전후로 호출되어 transient `worker_state` SSE 이벤트 emit + 슬롯 갱신. 새 connection snapshot 끝에 prepend → 사용자가 메시지 보낸 뒤 새로고침/재접속해도 send 버튼이 worker 완료 전엔 다시 enable 되지 않음. main.py의 `_worker_loop` 가 `pop_chat` 직전에 idle, 직후에 busy 호출 (SHUTDOWN 은 busy 안 함). 중첩 AgentLoop(`skill_name`/`skill_args` 세팅)에서의 header()는 무시 (sub-flow가 top-bar를 클로버하지 않도록). `unregister_connection` 은 `__close__` sentinel push로 SSE generator의 executor blocking call을 즉시 깨움 — register/unregister 페어의 대칭성으로 production cleanup latency 0. **`shutdown_all_connections()`** — 모든 active connection에 `__close__` sentinel을 일괄 push하고 리스트를 비움; FastAPI lifespan shutdown 훅과 main.py `finally` 양쪽에서 호출되며 idempotent (두 번째 호출은 빈 리스트 위에서 no-op). **`replay_from_history(ctx)`** — `--resume` 시 worker 시작 + SSE 연결 이전에 한 번 호출, `ctx.get_raw_messages()`를 walk해 user/tool/assistant 메시지를 각각 `push_user_message` / `observation` / `thought+action` 또는 `thought+final` 시퀀스로 재방출 → 새 클라이언트의 snapshot replay가 자연스럽게 이전 turn을 복원 (transient stream_chunk/status/spinner는 on-disk 기록 없음 = 재생 안 함). `__init__(workspace=...)` 로 workspace 경로 받아 ready 이벤트에 포함. **Parallel delegate visibility**: `_thread_to_task` dict + `_emit` 자동 task_id 첨부 + `begin_delegate_task` / `end_delegate_task` / `set_thread_status` override로 worker thread별 SSE 이벤트 라우팅. 프런트는 task_id 보고 collapsible group 카드로 격리 표시 → 두 parallel worker 출력이 인터리브하지 않음.
├── web/                            agent-cli web 서버 + 정적 UI (optional dep, `pip install agent-cli[web]`)
│   ├── __init__.py
│   ├── server.py            (~545) FastAPI app. `pick_port(host, preferred)` — `--port` 생략 시 main.py가 호출. preferred(8080) 사용 가능하면 그대로, 사용 중이면 `bind((host, 0))` 으로 OS 할당. 명시한 `--port N` 은 probe 없이 그대로 uvicorn에 전달 (충돌 시 uvicorn이 에러). `_NoCacheStaticFiles` + `_NO_CACHE_HEADERS` — `/static/*` 와 `/` 응답에 `Cache-Control: no-cache, must-revalidate` 자동 stamp. editable install로 CSS/JS 수정해도 사용자가 hard-refresh(Cmd+Shift+R) 안 해도 서버 재기동만으로 반영됨 — `no-store` 가 아닌 `no-cache` 라 변경 없으면 304 fast path 유지. 엔드포인트: `GET /` (정적 index.html), `GET /static/*` (앱 JS/CSS), `GET /api/health` (auth 없음), `GET /api/stream` (SSE, 토큰 인증, takeover-aware), `POST /api/input` (chat/prompt/confirm 통합), `POST /api/abort` (`prompt_user`/`confirm` 인터럽트). 단일 active client — takeover 모델 (`__close__` sentinel로 깔끔). 토큰은 `secrets.compare_digest` 상수시간 비교. `stream_events` async generator가 snapshot replay → live loop 순서로 yield. **`handle_slash_command(message, renderer)`** — 웹 전용 stateless 명령어 (`/help`, `/sh <cmd>`)만 처리. **`WebDispatchOutput`** — `main.try_dispatch_agent_or_skill` 에 넘기는 `DispatchOutput` 어댑터: `/skills`/`@agents` 리스트, `@<name> <task>`/`/<skill> <args>` invocation, not-found 에러를 전부 `observation` 이벤트로 변환. chat REPL과 dispatcher 공유. **`SHUTDOWN` sentinel + `shutdown()` 메서드** — `_chat_queue`에 identity sentinel(`is` 비교)을 put해 worker thread의 blocking `pop_chat()`을 깨움; worker는 `message is server.SHUTDOWN` 분기로 루프를 빠져나간다. **lifespan shutdown 훅** (`@asynccontextmanager async def _lifespan`) — uvicorn SIGINT 경로에서 `server.renderer.shutdown_all_connections()` 호출 → sse-starlette ping coroutine이 CancelledError 트레이스 없이 조용히 종료; main.py finally 블록과 idempotent하게 페어링.
│   └── static/                     Vanilla JS 프런트엔드 (의존성 0)
│       ├── index.html       (~25)  단일 HTML 셸 — header / messages / footer + textarea. JS가 URL ``?token=…``에서 토큰 추출, SSE 연결.
│       ├── app.js           (~720) SSE 이벤트 디스패치 + DOM 렌더링. event_buffer (snapshot) replay → live. 카드 종류: user_message (우측 파란 bubble), assistant_turn (thought + final OR action), observation (✓/✗ + tool_name), error, streaming (점선, 토큰 누적). prune 이벤트 시 가장 오래된 N개 카드 DOM에서 제거. input mode 3개 (chat / prompt / confirm). confirm 모드는 ConfirmOption.label 버튼 + 코멘트 텍스트. takeover 시 배너 + 입력 비활성. **markdown 헬퍼 (`escapeAndFormat` → `extractCodeFences` → `markdownInline` → `restoreCodeFences`)** — 의존성 0의 자체 미니 파서: 헤더(`#`/`##`/`###` → `<h1>`/`<h2>`/`<h3>`), GFM 파이프 표(헤더 행 + `---` separator + body), 순서/비순서 리스트 (`-`/`*`/`1.` 연속 라인 ↔ `<ul>`/`<ol>`), `**bold**`/`*italic*`, 인라인 코드, 펜스 코드(```` ``` ````). **XSS 안전(NFR-MD-2)**: `escapeHtml`이 가장 먼저 실행되어 `<`를 `&lt;`로 치환, 펜스를 placeholder로 빼낸 후 markdown 패스를 stripped body에 적용, 마지막에 pre-rendered `<pre><code>`로 복원 — markdown 패스가 사용자 입력 HTML을 실행 가능 토큰으로 되돌릴 경로가 없음. richMarkupToHtml은 observation 본문에만 적용.
│       └── style.css        (~200) chat UI 스타일 — 가독성 우선, 모바일 폴백 단일 컬럼. 메시지/카드 색상, 입력창 sticky, takeover 배너 등.
├── providers/                      LLM 프로바이더 어댑터
│   ├── __init__.py          (33)   create_provider() 팩토리
│   ├── base.py              (50)   LLMProvider 프로토콜, LLMResponse(+thinking), TokenUsage(+cache_creation/cache_read tokens)
│   ├── compat.py            (508)  ModelCapabilities + 프로브 감지 (thinking + format + context-window overflow probe) + 진행 콜백 + 자동 저장. OpenAI 호환 context window는 `/v1/models` 메타 → overflow probe → 128K fallback 3-tier
│   ├── http.py              (147)  post_with_retry (Timeout/ConnectionError 재시도, pre-stream only, 고정 1초 백오프)
│   ├── anthropic.py         (216)  Anthropic Messages API (tool_use + thinking blocks + streaming + TTFT + prompt cache via cache_control)
│   ├── openai_compat.py     (194)  OpenAI 호환 API (function calling + reasoning_content + streaming + TTFT)
│   └── ollama.py            (176)  Ollama API (basic JSON mode + message.thinking + streaming + TTFT)
│
├── tools/                          도구 시스템
│   ├── __init__.py          (77)   TOOLS dict (실제+가상) + _execute_tool() (internal primitive)
│   ├── result.py            (15)   ToolResult 데이터클래스 (success, output, error, artifact)
│   ├── action_summary.py    (34)   tool 이름 분기 자연어 요약 헬퍼: `summarize_tool_args` (observation record 측 — `{"tool":"<tool>","args":{...}}`)만 남음. manager._to_natural_language(observation 브랜치)가 `[<tool>] <args summary>` 헤더 합성에 사용. assistant emission 측은 `wire_format.render_assistant_from_history`가 JSON 재직렬화로 round-trip하므로 별도 요약 함수 불필요 (이전 `summarize_action_args`는 2026-05-15 제거).
│   ├── registry.py          (~600) 스키마 정의, 검증 (3-tuple 리턴), inline 가이드
│   ├── _diff.py             (113)  write_file/edit_file 공용 unified-diff 포매터 (Rich markup, OLD/NEW line-number gutter, 100줄 cap)
│   ├── read_file.py         (~280) 파일 읽기 + hashline 포맷팅 + 부분 읽기/검색/stat 모드 + 대용량 가드 → ToolResult
│   ├── write_file.py        (43)   파일 생성/덮어쓰기 + 변경사항 colored diff → ToolResult
│   ├── edit_file.py         (280)  파일 편집 (hashline + 퍼지 매칭 + 중복 ref/range overlap 거부 + edits 필터링 + colored diff) → ToolResult
│   ├── shell.py             (162)  셸 명령 실행 + 위험 명령 (rm/rmdir/mv) y/n/a 확인 (decision + 선택적 코멘트, env로 비활성 가능) → ToolResult. 출력은 잘리지 않고 그대로 LLM observation으로 전달 (이전 shell_artifact 가드는 2026-05-19 제거 — head/tail 미리보기가 중간 디버깅 정보를 silent하게 누락시키는 사례 발견, 컨텍스트 budget은 compaction/FIFO가 처리)
│   ├── fetch.py             (230)  웹 페이지 fetch → 마크다운 변환 → ToolResult
│   ├── delegate.py          (~770) in-process 서브에이전트 (fork/none, 병렬 + Live 상태 패널은 render.minimal `FrameClock` reuse, subdir, agent_stack, stop_event)
│   ├── context.py           (574)  read_context 도구 (list / search: scope+sessions 필터 / fetch: loc+range)
│   └── code_index.py        (598)  code_index 도구 — `agent_cli.code_index` 패키지의 native-tool wrapper. 10 mode dispatch (list/fetch/lookup/kind/file/refs/callers/callees/slice/build). 인덱스 root 자동 해석 (cwd 또는 가장 가까운 조상 `.agent-cli/`), lazy build + per-query incremental refresh. list/fetch는 root 바깥 path에 대해 on-demand parse fallback (DB 갱신 없음); 나머지 모드는 index-scoped (out-of-root 명시적 거부). fetch 결과는 hashline 포맷 → edit_file 직결. `post_hook(path)`는 edit_file/write_file 성공 직후 호출되어 자동 incremental refresh — 모든 예외 swallow (인덱싱 hiccup이 user-facing op 막지 않음). `_resolve_defs_path(root)`가 `<root>/.agent-cli/defconfig` 존재 시 `build(defs_path=...)`로 전달 — kernel/driver처럼 `#ifdef CONFIG_*` 가 함수 시그니처를 분기하는 코드에서 tree-sitter 파싱이 ERROR로 떨어져 정의가 누락되는 케이스를 unifdef 사전 분기 제거로 살림. 파일 부재 시 `None`이 그대로 통과해 기존 무전처리 동작 유지. 모듈 레벨 `_BUILD_LOCK` (threading.Lock) 이 `_ensure_index` / `post_hook` / `_do_build` 의 `build()` 호출을 직렬화 — 병렬 delegate worker 가 동시 진입해도 중복 빌드 없음. (atomic write 가 correctness 를 책임지고, 락은 효율 + SQLite 락 경합 회피 책임).
│
├── code_index/                     code_index 패키지 — tree-sitter SQLite 코드 인덱서 (`minish.ai/Agent-tools tsindex.py` Apache 2.0 port — NOTICE 참조). 총 ~5,000 LOC. `_sqlite.py` shim 이 stdlib `sqlite3` 우선 / 미존재(`--without-sqlite` CPython) 시 `pysqlite3-binary` 폴백 — Linux 잠금 서버에서도 무설정 동작.
│   ├── __init__.py          (56)   public API: build / load_index / build_callgraph / cmd_slice / IndexStore / Symbol / Ref / NAME_KINDS / CODE_NAME_KINDS / REF_KINDS / SCHEMA_VERSION
│   ├── schema.py            (~140) SCHEMA_VERSION=2 (v2: `qualified_name` 컬럼 추가, walker가 emit 시 full display form 산출 — Python/JS/TS/Java/Go/Rust/Markdown은 `.`, C++는 `::`, C는 flat=name; tool handler가 qualified_name 우선 lookup + bare-leaf fallback). Symbol/Ref dataclass, NAME_KINDS(5-vocab: function/type/variable/constant/section), CODE_NAME_KINDS(=NAME_KINDS-{section}, cross-file ref name resolution 전용 4-vocab), REF_KINDS(call/name/type). `section`은 markdown heading 5번째 vocab으로 추가됨 (upstream 4-vocab → 5-vocab).
│   ├── preproc.py           (498)  C/C++ 전처리: unifdef 드라이버 + rewriter chain (foreach/decl_macro/bare_attribute/variadic/ifdef_zero/define_comments/pp_trailing_ws/consecutive_attr/pp_continuation/type_arg). `_apply_unifdef` 헬퍼가 백엔드 선택 (시스템 `UNIFDEF_BIN` 우선, 없거나 `AGENT_CLI_UNIFDEF=pure` 면 `_unifdef.run_unifdef` 사용). `compute_preproc`이 fingerprint 산출 — defs file 내용 변경 시 인덱스 자동 invalidate. 백엔드 선택 정보 `preproc_info["backend"]` 에 노출.
│   ├── _unifdef.py          (653)  Pure-Python `unifdef -b` 구현 — Pratt-style 표현식 parser (defined/논리/비교/산술/비트), UNKNOWN 전파 + short-circuit 평가, directive walker (TAKEN/NOT_TAKEN/PASS_THROUGH 상태 스택). `-b` 라인 보존 contract 준수. 시스템 unifdef 와 byte-identical parity (parity 테스트로 8 케이스 보장). preproc.py 가 백엔드 fallback 으로 사용 — 시스템 binary 없는 잠금 서버에서도 무설정 동작.
│   ├── store.py             (257)  IndexStore (SQLite reader). find_symbols/find_refs/find_refs_in_range, normalize_file_path (exact/absolute/basename/suffix), kind_counts/ref_kind_counts/top_ref_names. dict-style 접근(`idx['symbols']`)도 호환 유지.
│   ├── builder.py           (492)  build() — Pass-1(definitions) + Pass-2(refs) + sha1 incremental + Option-B re-Pass2 (변경 파일의 새 이름을 mention하는 unchanged 파일 자동 re-walk). `iter_source_files`가 `_SKIP_DIRS` (.git/.agent-cli/.claude/.venv/node_modules/build/dist 등) prune → 인덱스 폭주 방지. 무효화 트리거 3개: schema_version mismatch / meta.root 변경 / preproc_fingerprint 변경. `write_sqlite_index` 는 atomic tmp + `os.replace` 패턴 — 활성 DB 파일을 절대 unlink/truncate 하지 않고 옆에 새 tmp 파일을 만든 뒤 한 번의 rename 으로 swap. 병렬 delegate worker 가 같은 인덱스에 동시 접근해도 `sqlite3.OperationalError: disk I/O error` race 안 남.
│   ├── callgraph.py         (115)  build_callgraph → (calls_of, callers_of, sites_of). 호출 사이트 (caller, callee, file, line) dedup으로 walker의 call+name 더블 emit을 1 edge로 정리. callback-only(kind='name' 단독) 사이트는 1× 그대로 유지.
│   ├── slice.py             (194)  cmd_slice → LLM-context markdown blob (definition + 선택적 callees/callers/types/macros, depth/max_bytes 캡). stdout 출력 대신 str 반환 (tool 통합).
│   └── languages/                  per-language walker 모듈 (lazy import — Python-only 프로젝트가 Rust grammar wheel 비용 안 냄)
│       ├── __init__.py      (~160) LangSpec dataclass + LANGUAGES dict + lazy `_ensure_loaded()` + `language_of(path)` / `get_supported_extensions()` helpers (prompt inline guide + error 메시지 single source)
│       ├── _shared.py       (36)   `text(node, src)` 공통 helper
│       ├── python.py        (~330) 함수/클래스/decorated/UPPER_SNAKE → constant. async/decorator modifiers. nested def/class도 emit (parent dotted chain).
│       ├── go.py            (~290) func/method (receiver type → parent), type/const/var, exported(uppercase) modifier. selector_expression call site.
│       ├── rust.py          (~420) function_item / function_signature_item (trait body sig은 is_definition=False with parent=trait_name), struct/enum/trait/type_item, impl block methods. macro_rules! → kind=function.
│       ├── java.py          (~340) class/interface/enum/abstract method/field. interface method = is_definition=False. generics, variadic args.
│       ├── javascript.py    (~490) function/class/method/field/lexical. const/let/var → kind 결정. arrow fn / generator (`function*`) → modifiers ['generator']. JS 헬퍼는 typescript.py가 import해 재사용.
│       ├── typescript.py    (~270) interface/type_alias/enum + js의 헬퍼 재사용. walk_refs는 type_identifier 추가 처리 → kind='type' ref emit (정의 사이트 제외).
│       ├── c.py             (~550) self-contained C walker (add_function_def/declaration/record/typedef/macro/c_walk_definitions/c_walk_refs). preprocess slot은 preproc.preprocess_source.
│       ├── cpp.py           (~725) self-contained C++ walker (template/namespace/class). C helper를 복제 보유 — upstream의 'language="c" inside .cpp' oddity 회피, .cpp 파일은 일관되게 language="cpp".
│       └── markdown.py      (~225) ATX (`## heading`) + setext heading walker. kind='section', kind_raw='atx_heading_N'/'setext_heading_N', parent stack chain, end_line은 다음 same-or-higher level heading 직전. refs 없음.
│
├── context/                        컨텍스트 관리
│   ├── __init__.py          (14)   re-export
│   ├── token_estimator.py   (23)   토큰 추정 (chars/4)
│   ├── overflow.py          (108)  프로바이더별 오버플로 감지 (`is_context_overflow` 패턴 — Anthropic/OpenAI/Ollama/omlx 커버) + `parse_overflow_amounts`로 400 메시지에서 실제 prompt 토큰·상한 추출 (omlx "N tokens exceeds max context window of M tokens" / Anthropic "N tokens > M maximum" / OpenAI 순서 역전 모두 대응). omlx 패턴은 실서버 검증 (2026-05-30)
│   ├── manager.py           (775)  ContextManager (토큰 budget 압축 + FIFO fallback + history.jsonl + 자연어 변환). **`ensure_within(target)`** (flow 1 예방형): loop이 매 호출 직전 `target=(C−S−O)×0.8`(S=system 실측)로 호출 — `_cache_tokens > target`면 LLM 요약 compaction 시도 (system anchor만 보존 → oldest 절반 evict → 단일 호출로 요약, 이전 summary가 있으면 같은 호출에 prepend하여 recursive 갱신 → `_file_extract`로 touched paths 누적 dedup → `[system][summary][file_list][retained]`로 캐시 재구성 → `compaction.json` atomic write). 요약 실패하거나 재구성된 캐시가 여전히 target 초과면 belt-and-braces로 `_evict_fifo(target)` 발동 — 무한 트리거 루프 방지. `add()`는 compaction 트리거 안 함(append만). **`reconcile_actual_tokens(actual, system_tokens)`**: 호출 직후 서버 실측(`usage.input_tokens`+cache)으로 `_cache_tokens = actual − system`으로 re-anchor → chars/4의 CJK 과소평가가 턴 간 누적되지 않음(drift 1턴치). **`force_fit(target, actual_tokens)`** (flow 2 반응형): 서버가 400(prompt too long)으로 거부하면 loop이 호출 — 로컬 추정(chars/4, CJK 과소)을 못 믿으므로 서버가 알려준 `actual_tokens`로 reconcile 후 compact→FIFO로 비율 축소. keep_ratio=target/actual로 줄여 추정 과소배율이 분자분모에서 상쇄(추정 절대정확도 불필요); progress 보장(매 호출 최소 1개 evict, anchor=최신 1개 보존). `actual_tokens` 없으면 ~25% trim fallback. `compaction_enabled=False` 또는 `AGENT_CLI_COMPACTION=off`로 끄면 기존 FIFO만 동작. Resume: `compaction.json`의 `dynamic_start_index`로 history.jsonl 후방 슬라이스만 cache 복원해 summarised tail과 중복 방지. 인스턴스마다 wire_format plugin attach (`__init__(wire_format=...)`, default fallback="react"). `get_messages()`는 system은 verbatim, user/tool branch만 자체 처리하고 assistant branch는 `wire_format.render_assistant_from_history`에 위임 — 한 세션 = 한 wire_format으로 격리. Compactor 콜백(`set_compactor`)과 `TurnRecorder`(`set_recorder`)는 `AgentLoop`가 후입식으로 주입 — unit-test 경로는 미주입 상태로 즉시 사용 가능.
│   ├── _file_extract.py     (86)   `extract_file_paths(messages)` — `_PATH_TOOLS = {write_file, edit_file, read_file, code_index}` 호출과 tool result에서 `path` 추출, delegate는 `<delegate:agent_name>` placeholder로 보존, 입력 순서 dedup. compaction 시 evict 묶음에서 touched files를 끄집어내는 단일 진입점
│   └── session.py           (~190) 세션 메타데이터 (session.jsonl) + resume용 user↔assistant 페어 추출 (recent_exchanges). System-injected user 메시지 필터는 `wire_formats.all_system_user_prefixes()` (format-agnostic 프리픽스 + 등록된 모든 plugin의 framing prefix) 단일 진입점 사용 — 새 wire format plugin 추가가 자동 반영
│
├── prompts/                        프롬프트 템플릿
│   ├── __init__.py          (1)
│   └── system_prompt.py     (~690) Attention 최적화 시스템 프롬프트 빌더 (Primacy/Middle/Recency, Role 상속, Context Recovery Guide). `build_system_prompt(wire_format=…)` — Response Format 섹션은 `wire_format.format_rules()`, 스킬·에이전트 호출 예시는 `wire_format.render_full_example(thought=None, ...)`, 도구 inline 가이드의 action_input 단편은 `wire_format.render_action_input(...)`로 렌더링 (ReAct는 identity; action_input shape이 다른 미래 plugin이 swap할 수 있는 hook). 인라인 예시는 wire 셰이프로 감싸지 않음 — 와이어 셰이프 학습은 Format Rules + skill/agent 예시(각 1번)에서 일어나고, 인라인은 mode 분기 / 의미론 학습. Recency 순서: Environment → Recovery → Directives → Execution Context (passive→active, persistent→immediate; Execution Context만 동적이라 끝에 배치 → 앞 3개 KV cache 안정). Tool inline 가이드는 `_build_tool_inline_guides(active_tools, wire_format)` 가 매 호출마다 빌드 — `read_file` 가이드의 Flow 문장이 `code_index` 활성 여부에 따라 분기 (활성 시 supported 확장자 파일은 `code_index mode='list'`로 우회 — 확장자 목록은 `code_index.languages.get_supported_extensions()` 단일 출처에서 가져와 walker 추가가 자동 전파). code_index 가이드는 per-file (list/fetch) vs index-wide (lookup/kind/file/refs/callers/callees/slice) scope 경계를 명시, on-demand parse fallback 위치도 안내. edit_file `_HASHLINE_INLINE`는 (1) 편집 직전에 CURRENT turn에서 read 하도록 요구(code_index mode='fetch'도 fresh read로 카운트) (2) hash mismatch를 failure가 아닌 guardrail로 reframe해 모델이 panic 없이 re-read/retry 하도록 톤 조정.
│
├── skills/                         프롬프트 스킬 시스템
│   ├── __init__.py          (7)    re-export
│   ├── models.py            (21)   Skill 데이터 모델 (model/context/hooks/invocation)
│   ├── loader.py            (95)   스킬 파일 검색/파싱 (ResourceLoader 기반, 캐싱)
│   ├── executor.py          (209)  인자 치환 + 도구 교집합 + Role 상속 + skill subdir + stop_event
│   └── builtin/                    패키지 내장 스킬
│       ├── create-skill.md         스킬 생성 메타 스킬
│       ├── create-agent.md         에이전트 생성 메타 스킬
│       ├── plan.md                 구현 계획 생성 (plan/ 디렉토리에 저장)
│       └── create-team/            에이전트 팀 구성 메타 스킬
│           ├── SKILL.md            6단계 워크플로 (분석→설계→에이전트→스킬→오케스트레이터→검증)
│           └── references/         단계별 가이드 (design-patterns, agent-writing, skill-writing)
│
├── agents/                         에이전트 정의 패키지
│   ├── __init__.py          (1)
│   └── builtin/                    패키지 내장 에이전트
│       └── explorer.md             읽기 전용 코드베이스 탐색 에이전트
│
├── mcp/                            MCP (Model Context Protocol) 통합
│   ├── __init__.py          (1)
│   ├── config.py            (108)  mcp.json 로드/병합 (프로젝트 > 유저)
│   ├── client.py            (258)  McpClientManager (stdio/SSE 연결, 도구 호출, stderr 격리)
│   └── adapter.py           (95)   MCP 도구 → ToolResult 래핑, TOOLS dict 등록

pyproject.toml                      패키지 설정
agent-cli.py                        하위 호환 래퍼 (4줄)
```

괄호 안 숫자는 LOC(Lines of Code)입니다.

---

## 3. 모듈 의존성 그래프

### 3.1 전체 의존성 플로우

```
┌─────────────┐
│  main.py    │ ← __main__.py, agent-cli.py
│ (CLI 진입)  │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  loop.py    │
│ (에이전트   │
│  루프)      │
└──────┬──────┘
       ├────────┬────────┬────────┬────────┐
       ▼        ▼        ▼        ▼        ▼
┌──────────┐┌───────┐┌────────┐┌────────┐┌──────────┐
│providers/││tools/ ││context/││prompts/││wire_     │
│          ││       ││        ││        ││formats/  │
│anthropic ││regis- ││manager ││system_ ││base      │
│openai_   ││try    ││overflow││prompt  ││react     │
│compat    ││read_  ││token_  ││        ││  (parser │
│ollama    ││write_ ││estima- ││        ││  + repair│
│compat    ││edit_  ││tor     ││        ││  + rules)│
│base      ││shell  ││session ││        ││registry  │
│          ││fetch  ││        ││        ││+ all_    │
│          ││dele-  ││        ││        ││system_   │
│          ││gate   ││        ││        ││user_     │
│          ││action_││        ││        ││prefixes()│
│          ││summary││        ││        ││          │
└──────────┘└───────┘└────────┘└────────┘└──────────┘
       │                  │         │
       ▼                  ▼         ▼
┌──────────┐       ┌──────────┐┌──────────┐
│config.py │       │render.py ││models.   │
│          │       │          ││json      │
└──────────┘       └──────────┘└──────────┘
```

### 3.2 모듈별 import 관계

**순환 의존 없음.** 단방향 흐름: config → compat → base → adapters → loop → main

```
config.py           → (외부만: json, pathlib)
constants.py        → (외부만: 없음, 순수 상수)
verbose.py          → (외부만: sys, time) — providers/http, loop가 공유
providers/compat.py → config
providers/base.py   → providers/compat
providers/http.py   → verbose, render (lazy)
providers/*.py      → providers/base, providers/compat, providers/http
wire_formats/base   → (외부만: dataclasses, typing)
wire_formats/react  → recovery/intervention, recovery/primitives,
                      tools/action_summary, wire_formats/base
wire_formats/prefix_md → recovery/intervention, recovery/primitives,
                      wire_formats/base
wire_formats/__init.→ wire_formats/base, wire_formats/react, wire_formats/prefix_md
                      (builtin 등록)
tools/action_summary→ (외부만: 없음 — 순수 string formatter)
tools/result.py     → (외부만: dataclasses, 순수 데이터 타입)
tools/read_file.py  → tools/result, (외부만: re, zlib, pathlib)
tools/edit_file.py  → tools/read_file, tools/result
tools/shell.py      → tools/result
tools/write_file.py → tools/result
tools/context.py    → tools/result, context/session
tools/delegate.py   → tools/result, context/manager, resource_loader, loop (lazy import)
tools/registry.py   → (외부만: json, dataclasses)
context/token_est.  → (외부만: 없음)
context/overflow.py → context/token_estimator, providers/compat
context/manager.py  → context/token_estimator, tools/action_summary, wire_formats
prompts/system_pr.  → providers/compat, tools/registry, wire_formats
context/session.py  → wire_formats (recent_exchanges가 all_system_user_prefixes 호출)
recovery/common_recovery → recovery/intervention, recovery/primitives
                      (WF 의존 없음 — 모든 plugin이 같은 텍스트를 봄)
recovery/wf_recovery   → recovery/intervention, recovery/primitives, wire_formats
                      (recovery/__init__.py 는 wf_recovery 를 re-export 안 함 —
                       패키지 자체는 format-agnostic, 직접 import 만이 wire_formats 끌어옴)
loop.py             → constants, context/manager, context/overflow,
                      prompts/system_prompt, providers/base, providers/compat,
                      render, tools, tools/delegate, tools/registry,
                      verbose, wire_formats
skills/loader.py    → skills/models, resource_loader
resource_loader.py  → yaml (optional)
skills/executor.py  → loop, skills/models, providers/base, providers/compat
main.py             → config, context/manager, loop, providers, render, skills
```

---

## 4. 핵심 데이터 구조

### 4.1 LLM 응답 (`providers/base.py`)

```python
@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int

@dataclass
class LLMResponse:
    content: str                          # 텍스트 응답
    tool_calls: list[dict] | None = None  # 네이티브 tool calling 결과
    usage: TokenUsage | None = None
    stop_reason: str | None = None
    thinking: str = ""                    # provider-side reasoning 채널

# tool_calls 항목 형식:
# {"id": "tu_1", "name": "read_file", "input": {"path": "a.py"}}
```

`thinking`은 모델이 별도 reasoning 채널로 노출한 텍스트를 운반합니다. 채널 매핑:
- **Ollama**: `message.thinking` 필드 (Qwen3 / Qwen3.5 / Qwen3.6 family)
- **Anthropic**: `content[].type == "thinking"` 블록 + 스트리밍 `thinking_delta`
- **OpenAI 호환**: `choice.message.reasoning_content` (vLLM 컨벤션)
- 위 채널이 없으면 `""` (plain OpenAI Chat Completions 등 — graceful)
- `<think>...</think>` 태그가 content 안에 있는 경우는 별도 — `parse_react`가 `ParsedAction.thinking`으로 분리 추출

**소비처 (v1):** verbose 모드의 `render_thinking` 디버그 출력 *전용*. recovery 레이어(`format_no_*_retry`, `recovery/primitives.py`)는 thinking을 *읽지 않음* — primitive contract가 channel-agnostic이어야 누더기를 막기 때문 (`docs/robust-harness/DESIGN.md` §2.2).

### 4.2 모델 능력치 (`providers/compat.py`)

```python
@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int               # 컨텍스트 윈도우 크기 (토큰)
    max_output_tokens: int            # 최대 출력 토큰
    supports_structured_output: bool  # basic JSON mode 가능 (Ollama format="json" / OpenAI response_format)
    supports_thinking: bool           # thinking/reasoning 지원
    thinking_budget: int              # thinking 토큰 예산 (0=비활성)
    supports_strict_schema: bool      # (dormant) strict JSON Schema 표식 — 현재 어떤 provider도 이 플래그로 동작 분기 안 함. 향후 opt-in strict schema 재도입 시 사용 예정.
    thinking_format: str = ""         # thinking 블록 태그 ("think", "reasoning", "")
```

`thinking_format` 값:
- `"think"` — `<think>...</think>` 형식 (Qwen3, DeepSeek-R1)
- `"reasoning"` — `<reasoning>...</reasoning>` 형식
- `""` — thinking 블록 미사용 (Anthropic API 레벨 처리, GPT 등)

능력치 조회 우선순위:
1. `models.json` 정적 설정 (최우선)
2. 런타임 API 감지 (Ollama `/api/show` + thinking/format probe; OpenAI 호환 `/v1/models` + context overflow probe)
3. 보수적 기본값 (4096 context, 모든 기능 비활성 — `DEFAULT_CAPABILITIES`, provider/base_url 없을 때만)

**런타임 감지 세부 (Ollama):**
1. `/api/show` — 메타데이터 (context_length 등)
2. thinking probe — `"What is 2+2?"`를 평문으로 보내 `message.thinking` 필드 혹은 `<think>` 태그 여부 검사 → `supports_thinking`, `thinking_format` 결정
3. **format probe** — `format="json"`을 붙여 똑같이 단순 요청을 보낸 뒤 HTTP 200 + `error` 없는 응답이 오는지 검사. mlx 엔진으로 패키징된 일부 모델(예: bf16 safetensors) 이 `format` 파라미터에서 깨지기 때문에 사전에 걸러냄. 실패 시 `supports_structured_output=False`로 저장하고 stderr에 `[warn]` 한 줄 기록 → 이후 실 요청이 `format` 파라미터 자체를 생략.

첫 감지 시 probe 2번(thinking, format)이 `/api/chat`에 가기 때문에 cold-load 비용이 1회 발생 (~10초). 감지 결과는 `~/.agent-cli/models.json`에 저장되어 이후엔 재실행 없음.

**런타임 감지 세부 (OpenAI 호환 / omlx · vLLM · mlx-lm):** `_detect_openai_context_window` 가 3-tier로 context window 결정 —
1. `/v1/models` 메타데이터 `max_model_len`(vLLM) / `context_length` — 있으면 그대로 (가장 쌈·정확).
2. **overflow probe** (`_probe_context_window_via_overflow`) — 메타데이터에 없는 서버(omlx 등)는 의도적으로 상한 초과 prompt(`"word "×2M` ≈ 1.5M 토큰)를 보내 400을 유발하고 `parse_overflow_amounts`로 응답의 상한 숫자를 추출 (omlx: `exceeds max context window of 262144 tokens`). 상한 초과 prompt는 **토크나이즈 직후 즉시 거부**되어 eval/생성이 없으므로 서버 점유 없음(실서버 검증 2026-05-30) — 그래서 경계로 수렴하는 binary search는 **안 함**(상한 *이하* probe는 full prompt-eval을 유발해 서버를 점유시킴).
3. `_DEFAULT_CONTEXT_FALLBACK` = **128K**(`131072`) — 메타데이터·probe 모두 숫자를 못 주면. 보수적/under-set이라 자체적으로 400을 유발하지 않고, 실제가 더 작으면 flow 2 런타임 복구가 교정. (이전 4096 기본값을 대체 — 4096은 256K 서버에서 컨텍스트의 1.5%만 쓰는 심각한 낭비였음.)

모든 첫-실행 probe(thinking / format / context overflow)는 `constants.DETECTION_PROBE_TIMEOUT`(60s) 공유 — cold-load를 감내하는 여유값이며 사용자 셸 명령용 `SHELL_COMMAND_TIMEOUT`(30s)과 구분.

### 4.3 파서 결과 — `ParsedAction` (`wire_formats/base.py`)

모든 wire-format plugin이 반환하는 boundary 데이터타입. ReActFormat의 `parse_react`(같은 plugin 안)는 이 타입을 *직접* 반환하며, 미래 plugin도 같은 타입을 사용해 loop이 plugin과 무관하게 동작.

```python
@dataclass
class ParsedAction:
    thought: str | None = None
    action: str | None = None     # "complete" = 작업 완료
    action_input: dict | str | None = None
    raw: str = ""                # 원본 LLM 텍스트 (thinking 제거 후)
    parse_stage: int = 0         # 0=실패, 1=json.loads, 2=json_repair, 3=regex (plugin이 정의)
    thinking: str | None = None  # 추출된 thinking 블록 내용
    truncated: bool = False      # JSON 복구가 닫지 못한 브래킷/문자열을 보충했을 때 True
```

### 4.4 도구 스키마 (`tools/registry.py`)

```python
@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict  # JSON Schema 형태

# 등록된 도구: read_file, write_file, edit_file, shell, read_context,
#               complete, ask, run_skill, ready_for_review, fetch, code_index, delegate
# 가상 도구 (loop에서 인터셉트, 별도 set 상수 없음 — loop.py if-cascade가 단일 진실원):
#   complete, ask, run_skill, ready_for_review, delegate
# _ALWAYS_INCLUDE = ("complete", "ready_for_review") — allowed_tools와 무관하게 항상 API tool 목록에 포함
# delegate는 TOOL_SCHEMAS의 일반 항목으로 등록 (`include_delegate` 플래그로 시스템 프롬프트 노출 제어)
```

가상 도구 인터셉트 분기는 일반 dispatch 경로(`§5.x render_step("action", ...)`)를
거치지 않으므로 분기 진입 시 명시적으로 `render_step("action", ...)` 을 호출해
`assistant_turn` 이벤트를 발사한다. 이게 없으면 WebRenderer의 streaming-text 카드가
교체되지 않아 다음 턴의 stream_chunks가 동일 카드에 누적되어 "이전 메시지에 답변이
붙어 보이는" UX 버그가 발생한다 (`complete` / echo-as-final 은 `render_step("final", ...)` 을
호출하므로 동일 경로로 해결됨).

---

## 5. 핵심 플로우

### 5.1 ReAct 에이전트 루프 (`loop.py` — `AgentLoop` 클래스)

#### 컨텍스트 윈도우 레이아웃

`ctx.get_messages()` 반환: history.jsonl의 마지막 N개를 자연어 변환 (assistant turn은 ctx의 wire_format plugin이 변환 — §5.4 참조)

```
[system]   Role (main/delegate/skill별 상이)
           Task Guidelines + Format Rules (thought에 목적+이유 필수)
           Available Tools / Skills / Agents
           DIRECTIVE.md / Environment
           Context Recovery Guide ("read_file({session_dir}/history.jsonl)")

[messages] user: "hooks.py 분석해줘"
           assistant: hooks.py를 분석하기 위해 파일을 읽겠다. → read_file(hooks.py)
           user: [read_file] hooks.py\n(전문)
           assistant: 분석이 완료되었다. hooks.py는 3개의 hook 타입을 지원...
```

- Scratchpad 별도 inject 없음. messages만 (토큰 budget 자동 계산, 90% 초과 시 compaction → 그 외 FIFO drop)
- 저장: history.jsonl (JSON Lines, 구조화)
- 표현: 자연어 변환 (thought → "목적. → action(인자)")

#### ctx.add 저장 포맷

| 메시지 타입 | history.jsonl 저장 형태 |
|------------|----------------------|
| 사용자 입력 | `{"role":"user", "content":"..."}` |
| LLM action | `{"role":"assistant", "thought":"...", "action":"...", "action_input":{...}}` |
| 도구 결과 | `{"role":"user", "content":"Observation: ..."}` |
| complete | `{"role":"assistant", "thought":"...", "action":"complete", "action_input":{"result":"..."}}` |

#### 루프 플로우

```
AgentLoop.run()
    │
    ├─ _install_signal_handler()   ← Ctrl+C를 flag로 변환
    ├─ _setup()
    │   ├─ 시스템 프롬프트 빌드 (capabilities, tools, session_dir, agent_role)
    │   └─ ctx.add(user query) → ctx.get_messages() (자연어 변환)
    │
    ├─ while _should_continue():
    │    │
    │    ├─ ★ CHECK: _interrupted? → _on_interrupt() → return None
    │    │
    │    ├─ _begin_iteration() → turn separator 렌더링
    │    │
    │    ├─ _call_llm() → LLMResponse (overflow 400 시 force_fit으로 compact→FIFO 축소 후 bounded 재시도 — flow 2)
    │    │
    │    └─ _handle_text_path()  ← text parsing only (native tool calling 제거)
    │         │
    │         ├─ [ready_for_review] → 원본 query를 observation으로 반환
    │         │
    │         ├─ [complete] → ctx.add(structured dict) → return answer
    │         │
    │         ├─ [run_skill] → 내부 AgentLoop (별도 skill subdir)
    │         │
    │         └─ [도구] → execute → ctx.add(assistant + observation)
    │
    └─ _restore_signal_handler()
```

**Graceful Interrupt (`graceful_interrupt=True`, chat 전용):**
- 1st Ctrl+C: `_interrupted` flag 설정 → 현재 스텝 완료 후 다음 turn 시작 시 탈출
- 2nd Ctrl+C: `KeyboardInterrupt` 즉시 발생 (기본 핸들러 복원 후)
- 인터럽트 시 ctx에 기록되어 history.jsonl에 영속화

**run 모드 Ctrl+C:** signal handler 미설치, `KeyboardInterrupt` 즉시 발생 → `try/except`로 세션 저장 후 종료

#### 중첩 렌더링: `push_depth` / `pop_depth` + 그룹 블록

스킬/delegate 실행 시 출력을 시각적으로 감싸기 위해 `group_start`/`group_end`와
depth 기반 prefix(`│ `)를 사용. 병렬 delegate는 worker별 capture 후 Live 패널로
실시간 상태 표시, 완료 후 block replay.

| 시점 | 호출 | 출력 |
|------|------|------|
| 스킬/delegate 시작 | `render_group_start(label, icon)` | `┌─ 🪄 skill:plan` |
| 내부 턴 | `push_depth` 상태에서 `_p()` | `│ 💭 thought...` |
| 스킬/delegate 종료 | `render_group_end(label, success, dur)` | `└─ ✓ skill:plan (5.2s)` |

### 5.2 프로바이더별 도구 호출 방식

모든 프로바이더가 **ReAct 텍스트 파싱**만 씁니다. 네이티브 tool calling API (Anthropic `tool_use`, OpenAI `function calling`)는 **사용하지 않습니다** — 프로바이더 편차 제거와 구현 단순성을 위한 선택. 따라서 `supports_tool_calling` 같은 플래그는 존재하지 않고, 모든 분기는 JSON 출력 여부 (`supports_structured_output`) 하나로 수렴합니다.

```
              ┌─ supports_structured_output=True ─┐
              │                                    │
        ┌─────┴──────┐                     ┌──────┴──────┐
        │ Ollama     │                     │ OpenAI      │
        │ format:    │                     │ response_   │
        │ "json"     │                     │ format      │
        │ (basic)    │                     │ json_object │
        └────────────┘                     └─────────────┘
              파싱 필요                             파싱 필요
              (JSON 출력)                          (JSON 출력)

              ┌─ False ─────────────────────────────┐
              │                                      │
        ┌─────┴──────┐                              │
        │ 텍스트 자유  │                              │
        │ 형식        │                              │
        └────────────┘                              │
              파싱 필요                               │
              (비구조화 텍스트)                         │

  모든 경우: 3단계 폴백 파서 (json.loads → json_repair → regex)가 도구 호출 추출
```

### 5.3 3단계 파싱 폴백 (`wire_formats/react.py`)

```
LLM 텍스트 응답
    │
    ▼
유니코드 서로게이트 제거 (_sanitize_surrogates)
    │
    ▼
Thinking 블록 분리 (_strip_thinking_blocks)
    │  ├─ <think>...</think> 제거 → thinking 필드에 보존
    │  ├─ <thinking>...</thinking> 제거
    │  ├─ <reasoning>...</reasoning> 제거
    │  └─ <reflection>...</reflection> 제거
    │
    ▼
Stage 1: 마크다운 펜스 제거 → json.loads()
    ├─ 성공 → ParsedAction (parse_stage=1)
    │
    ▼ 실패
Stage 2: repair_json() — 6단계 복구 파이프라인 (같은 모듈 안 helper)
    │  ├─ JSON 블록 추출 (brace depth tracking)
    │  ├─ 작은따옴표 → 큰따옴표
    │  ├─ 따옴표 없는 키 수정
    │  ├─ trailing comma 제거
    │  ├─ 닫히지 않은 문자열 닫기
    │  └─ 누락된 괄호 추가
    ├─ 성공 → ParsedAction (parse_stage=2)
    │
    ▼ 실패
Stage 3: regex 필드 추출
    │  ├─ "thought": "..." 추출
    │  ├─ "action": "..." 추출
    │  └─ "action_input": {...} 추출
    ├─ 성공 → ParsedAction (parse_stage=3)
    │
    ▼ 실패
ParsedAction (parse_stage=0, 모든 필드 None)
```

`repair_json` 등 stage 2/3 helper는 모두 ReAct plugin 모듈(`wire_formats/react.py`) 안에 함께 산다. `parsing/` 별도 패키지를 두지 않음으로써 plugin이 *폴더 째 삭제 가능*한 boundary를 유지 — 다른 plugin은 자기만의 recovery 전략을 정의 (의존 X). 같은 algorithm을 재사용해야 할 plugin이 등장하면 그 시점에 공통화 결정.

#### 형제 키 정규화 (action_input hoist, 2-레이어)

일부 모델(qwen3 계열 등)은 action 인자를 `action_input` 안에 **중첩하지 않고 top-level 형제 키로** 뱉는 드리프트를 보입니다:

```json
// 드리프트 A: 가상 툴 payload가 top-level
{"thought": "done", "action": "complete", "result": "final answer"}

// 드리프트 B: 실제 툴 인자가 top-level (pcie_scsc 세션에서 관찰)
{"thought": "find files", "action": "shell", "command": "ls"}

// 둘 다의 기대 형태
{"thought": "...", "action": "...", "action_input": {...}}
```

JSON 자체는 valid하고 action 이름도 올바른데, loop이 `action_input.X`를 찾기 때문에 조용히 실패 (가상 툴은 "Completed without result", 실제 툴은 "Missing required field" → repeated-call guard). strict JSON Schema로도 막히지 않음 — 과거 schema가 `thought`만 required로 두고 `additionalProperties` 제한이 없었기 때문.

`_normalize_action_input()`이 파싱 직후 두 레이어로 정규화합니다 (`wire_formats/react.py`):

**Layer 1 — 가상 툴 별칭 매핑.** `complete` / `ready_for_review` / `ask`에 대해 정해진 후보 키를 canonical target 키로 매핑:

| action | target key | top-level fallback 순위 |
|---|---|---|
| `complete` | `action_input.result` | `result` > `answer` > `response` > `final` > `output` |
| `ready_for_review` | `action_input.summary` | `summary` |
| `ask` | `action_input.questions` | `questions` > `question` (`_extract_questions`가 str→list 처리) |

알려진 가상 툴인데 후보가 하나도 없으면 **fall-through 안 함** — `action_input=None` 유지해서 downstream이 "no payload" 경로로 처리 (복귀 가능).

**Layer 2 — 실제 툴 / 미지의 action의 형제 키 번들링.** 가상 툴이 아니고 `action_input`이 없으면, 예약되지 않은 top-level 키 전부를 `action_input`으로 모아줌:

```json
// 입력
{"thought":"...", "action":"shell", "command":"ls", "timeout":10}
// 정규화 후
{"thought":"...", "action":"shell", "action_input":{"command":"ls","timeout":10}}
```

MCP 제공 툴처럼 `action` 이름이 레지스트리에 없어도 같은 룰 적용.

**예약어 블랙리스트 (`_REACT_RESERVED`).** 다음 키들은 형제로 나타나도 `action_input`에 담기지 않습니다:

- `thought` / `action` / `action_input` — ReAct 프로토콜 필드
- `observation` — 시스템 프롬프트가 금지하지만 드리프트 시 혼입 가능
- `reasoning` / `reflection` — thinking 태그 변종 (태그로 나타나면 `_strip_thinking_blocks`가 잡지만 top-level 키 형태로도 등장 가능)
- `role` / `_meta` — 저장/세션 계층 메타 필드

**우선순위 규칙.** `action_input`이 이미 있고 truthy면 Layer 1, 2 모두 skip — 모델이 명시적으로 nested를 선택했다고 보고 형제 키는 무시. `action_input`이 `None` 또는 `{}`면 레이어 로직 발동.

이 정규화는 strict JSON Schema 도입 없이 작동하며, flat form을 정식 canonical로 승격하는 미래 변경(`plan/schema-flatten.md` 참조)의 파서 기반이 됩니다.

#### Failure Grounding Retry (`recovery/primitives.py` + `constants.py` + `loop.py`)

> 설계 문서: `docs/robust-harness/DESIGN.md` (4-layer 디자인, primitive 도구함, playbook)

3단계 파싱이 모두 실패하거나(JSON 깨짐 — `parse_stage=0`) JSON은 파싱됐는데 `action`이 없으면 (`parse_stage>0` & `action=None`), `loop.py`가 user role 메시지를 한 개 주입하고 같은 turn을 재시도합니다 (`turn -= 1`로 카운트 제외). 메시지는 `recovery/primitives.py`의 순수 함수들을 합성한 결과:

```
Your response was not valid JSON.

Your prior output:               ← echo_prior_output: head 400자 (구조 마커 보존)
---
{LLM이 방금 토출한 content}
---

Honor that. Output ONLY a JSON object: {...}.   ← constrain_format_json
```

**v1 design — content-only echo.** thinking 채널 echo는 격리 측정값 없이 runtime 의존성만 유발하므로 v1에서 제외. Step 2 observability (TurnRecord JSONL) 데이터로 필요성이 검증되면 별도 primitive로 추가. (자세한 결정 배경은 `docs/robust-harness/DESIGN.md` §2.2.)

`prior_content`가 비면 정적 fallback (`RETRY_HINT_NO_JSON` / `RETRY_HINT_NO_ACTION`) — graceful path.

**A1a (NO_JSON) vs A1b (NO_OUTPUT) 라벨 분리.** parse stage 0 실패는 두 가지 운영 모드가 섞여 있음 — (a) 모델이 *내용은 있는데* JSON 형식에서 드리프트 (YAML 키, prose, code fence 등), (b) 모델이 *아무것도* 안 뱉음 (whitespace-only). `loop.py`의 `_handle_text_path`가 `llm_text.strip()` 검사로 둘을 분리해 `failure_signal` 을 `FAILURE_NO_JSON` 또는 `FAILURE_NO_OUTPUT` 으로 기록. 회복 경로는 동일(둘 다 `format_no_json_retry`) — A1b는 echo 대상이 없어 자연스럽게 정적 fallback path로 떨어지고 `primitives_applied=[]` 가 됨. 라벨 분리의 목적은 *관찰성*이며, 두 모드가 회복률 분포에서 어떻게 갈리는지 데이터를 모은 뒤 별도 primitive 도입 여부를 결정 (DESIGN.md §1, A1a/A1b).

**근거 (failure grounding):** 추상적 *"your response was invalid"*는 모델이 무엇을 위반했는지 모르게 함 — 같은 출력을 반복할 가능성 높음. retry에 자기 출력을 인용해 보여주면 모델이 자기 드리프트(YAML-style 키, 함수-호출 신택스, bare prose 등)를 직접 보고 self-diagnose 가능. 구조 마커가 보통 출력 시작 부분이라 head-truncate.

**Primitive 계약 (누더기 방지):** primitive는 provider/모델/채널 이름을 절대 참조하지 않음. 새 실패 모드는 *primitive 합성과 매핑 한 줄*로 처리 — `if "ollama"`, `response.thinking` 같은 분기를 primitive 시그니처에 두면 invariant 위반.

**Prefix 호환성:** retry 메시지 시작은 항상 정적 템플릿과 같은 문장 (`"Your response was not valid JSON."` / `"Your JSON was parsed but has no action."` / `"Your JSON was missing the 'thought' field."`)으로 시작하므로 `SYSTEM_USER_PREFIXES` 매칭이 그대로 유지됨 → resume 시 자연어 변환에서 noise로 표시되지 않음.

**A7 (NO_THOUGHT) — mimicry-strengthening loop 차단.** parser 가 성공해 `action`은 있지만 `thought`가 비어 있으면(또는 `None`/whitespace-only) `_dispatch_text_path` 가 dispatch 직전에 차단하고 `format_no_thought_retry`로 retry. drift-shaped 응답 1건이 transcript에 들어가면 in-context learning 으로 이어지는 turn 들이 같은 구조를 mimicry해 thought-drop 이 연쇄로 번지는 패턴(qwen3.6)을 끊는 것이 목적. 정적 fallback 메시지 + echo path 모두 "Your JSON was missing the 'thought' field." 로 시작 — `SYSTEM_USER_PREFIXES` 에 동일 prefix 등록. constraint 메시지("must include 'thought' stating purpose / reason")는 builder 내부에 inline — primitive로 승격하면 v1 단일-caller 상황에서 anti-patchwork invariant ("primitive reused by ≥2 failures") 위반이라 두 번째 caller 등장 시점까지 보류. **예외: `complete` 액션은 검사에서 제외** — 최종 답 액션이라 reasoning slot이 next-turn 의무를 지지 않음. Phase 2 bakeoff (2026-05-18) 측정: 27b prefix_md `complete_direct`에서 5/5 unnecessary recovery + 평균 +3.1s latency, 35b는 영향 없음 (이미 thought 100% 채움).

#### Per-Turn Observability (`recovery/observability.py`)

`format_no_*_retry`는 단순 문자열이 아니라 `Intervention` (message + primitives 이름) 을 반환합니다. `_handle_text_path`는 try/finally로 매 턴 한 번씩 `TurnRecorder.record()`를 호출 — 성공/실패/예외 모든 경로에서 정확히 한 줄이 기록됩니다.

**스키마 (`TurnRecord`, `{session_dir}/turns.jsonl` 한 줄당 한 row):**
- `seq` — 세션 내 모노토닉 (0, 1, 2, ...)
- `model` — 어떤 모델이 응답했는지 (분석 시 그룹 키)
- `timestamp` — ISO 8601 UTC
- `parse_stage` — 0(실패), 1(json.loads), 2(json_repair), 3(regex)
- `failure_signal` — `"NO_JSON"` / `"NO_OUTPUT"` / `"NO_ACTION"` / `"NO_THOUGHT"` / `"UNKNOWN_TOOL"` / `"SCHEMA_MISMATCH"` / `"NESTED_ENVELOPE"` / `"ACTION_LOOP"` / `null`
- `primitives_applied` — 합성된 primitive 이름 list (실패 retry 시에만 채워짐)

**프라이버시 계약:** 사용자 prompt나 LLM 응답 본문은 절대 기록되지 않음 — 구조 메타만. 회복률은 *저장하지 않고* 분석 시 walk-forward로 계산 (실패 row 다음 row의 failure_signal을 봐서 회복 여부 판단). retrospective 쓰기 회피.

**활성화 조건:**
- `ctx is not None` (in-process subagent 의 일부 헬퍼 경로에선 ctx 미주입 → 비활성)
- `record_turns=True` (CLI: `--record-turns/--no-record-turns`, 기본 켜짐)

**활용:** Step 3·4의 playbook 튜닝 데이터 누적이 주 목적. 분석은 별도 스크립트 (`jq`로도 충분). 자세한 설계는 `docs/robust-harness/DESIGN.md` §3.3.

#### B1 — Action Loop 감지 + 회복 (`recovery/detectors.py` + B1 playbook)

같은 `(action, args)` 호출이 연속 2회 이상이면 모델이 막힌 상태로 보고 단계적 개입을 발동합니다. 기존 hard-fail (`_detect_repeated_calls`)을 대체.

**Detector (`ActionLoopDetector`):** turn 간 stateful — `_last_signature`, `_consecutive_count`, `_fire_count` 보유. `observe(action, args, prev_was_error=False)`가 호출될 때마다 escalation level 반환:
- 0 — 임계값 미만 또는 error retry로 카운터 리셋
- 1 — 첫 발동 (probe_progress)
- 2 — 두 번째 발동 (restate_task)
- 3+ — 회복 소진, hard-fail

`prev_was_error=True`면 카운터 리셋 — 정당한 재시도 false-positive 방지. 다른 action이 끼면 카운터 리셋. Args canonicalization은 `json.dumps(sort_keys=True)` (dict 키 순서 무시).

**Playbook (`format_action_loop_intervention`):**
- Level 1: `probe_progress` — 가벼운 nudge ("이미 가진 응답을 다시 봐, complete 또는 다른 action 선택")
- Level 2: `restate_task` — 원본 task 재고정 + 진단 질문 ("task가 이 호출을 왜 필요로 하나? 못 얻고 있는 정보가 뭔가?")
- Level 3+: `None` 반환 — caller가 hard-fail (어떤 primitive를 시도했는지 에러 메시지에 포함)

**Temperature-down 컬럼 의도적 누락:** DESIGN.md §2.3에 명시된 escalation 컬럼 중 "+temp↓"은 v1에서 제외. provider별 temperature 처리가 다양해 primitive 계약(provider-agnostic) 위반 위험. Step 4에서 데이터 보고 재검토.

**감지 시점:** dispatch *전*. tool은 실행되지 않고, 모델은 다음 turn에 새 prompt(`probe_progress` 또는 `restate_task`)와 함께 같은 결정을 다시 내림. 중복 비용 0.

#### A4 / A5 — Pre-dispatch Detection (`recovery/detectors.py`)

LLM이 emit한 action·input이 도구 레지스트리·스키마와 안 맞을 때:

- **A4 (Unknown tool)** — `detect_unknown_tool(action, tools_list)` → `action not in tools_list`
- **A5 (Schema mismatch)** — `detect_schema_mismatch(action, action_input)` → `validate_tool_input` wrap, `(mismatched, error_message, normalized_input)` 반환

**감지 위치:** `_dispatch_text_path` 안, B1 detector → render_action 직후, `_dispatch_tool_with_hooks` 호출 직전. 모든 *pre-dispatch* 검사가 한 자리에 모임 (DESIGN.md §3.1 detection layer).

**처리:** 라벨링 + observation 주입 + dispatch 우회.
```
A4: outcome["failure_signal"] = FAILURE_UNKNOWN_TOOL
    Observation: "Unknown tool 'X'. Available: ..."
A5: outcome["failure_signal"] = FAILURE_SCHEMA_MISMATCH
    Observation: "Missing required field(s) for 'X': ... Expected: {...} Fix action_input and retry."
```

**v1은 라벨링만 — 별도 primitive 없음.** 이유: 현재 메시지(레지스트리·스키마 정보 포함)가 이미 grounding 역할 수행 중. 별도 primitive(`probe_tool_name`, `echo_diff` 등)가 *측정 효과*를 내는지는 TurnRecord 통계 보고 결정 (Step 4b).

**B1 detector와의 순서:** B1이 먼저 (A4·A5 무관 *반복 자체*가 더 큰 신호). 같은 unknown tool을 2번 emit하면 B1이 잡아 `probe_progress`를 줌 — A4 메시지가 무한 반복되지 않음.

**`_dispatch_tool_with_hooks` 내부 검증 제거됨:** Step 4a 전엔 `_execute_single_tool` 안에서도 `validate_tool_input` 호출 + `tool_name in tools_list` 체크가 있었음. 이젠 recovery 레이어가 단일 진실 원천 — 중복 제거. 2026-05-03: `tools/__init__.py:execute_tool`의 boundary 방어도 제거 + `_execute_tool`로 internal rename (REMAINING_DEBT.md #2/#3 청산).

**남은 부채:** 이 작업 중 *알면서 남긴* 부채는 `docs/robust-harness/REMAINING_DEBT.md`에 명시 기록.

#### A6 — Nested Envelope Detection (관찰 전용)

`complete` action의 결과 페이로드가 다시 `{"result": "..."}` JSON 객체로 래핑되어 들어오는 경우 — qwen3.5/3.6 계열에서 산발적으로 관찰됨. 사용자에게 `✅ {"result": "..."}` 같은 문자열이 그대로 표시되는 UX 회귀로 이어짐.

- **감지** — `detect_nested_envelope(result_value)` → `str` 인지 확인 → `lstrip().startswith('{"result"')` → `json.loads` 성공 → 결과 dict의 top-level `result` 키 존재. 한 단계라도 실패하면 false (오탐 방지).
- **위치** — `loop.py`의 `complete` 분기, `answer` 결정 *직후*. 라벨링만 하고 출력은 그대로 둠.
- **라벨** — `outcome["failure_signal"] = FAILURE_NESTED_ENVELOPE` (TurnRecord에 기록).
- **자동 unwrap 안 함 (의도적)** — 빈도·재현 모델 분포 측정 후 4b에서 결정. v1에서 unwrap을 하면 (a) 모델이 의도적으로 그렇게 답한 경우 데이터를 잃고 (b) anti-patchwork 원칙(측정 후 결정) 위반.

### 5.4 컨텍스트 관리 (`context/manager.py`)

> 상세 설계: `docs/context-redesign/DESIGN.md`, `docs/context-compaction/DESIGN.md`

#### 2-Tier: Compaction (LLM 요약) → FIFO Fallback

> 두 흐름이 있다. **flow 1 (예방)** — 매 LLM 호출 *직전* `ensure_within((C−S−O)×0.8)`, 호출 *직후* 서버 실측으로 reconcile.
> **flow 2 (반응)** — 예방이 빗나가 서버가 400을 던지면 `force_fit`으로 사후 축소+재시도.
> 아래는 flow 1; flow 2는 이어지는 박스 참조.

```
add(msg): 캐시 append + 토큰 누적 + history.jsonl 한 줄 append  (compaction 트리거 안 함)

매 LLM 호출 (_call_llm):
    │
    ├─ [호출 직전] flow 1 예방: ctx.ensure_within(target)
    │     S = estimate_tokens(self.system)        ← 매 호출 실측 (가변 system 반영)
    │     target = (C − S − O) × 0.8              ← C=context_window, O=max_output
    │     _cache_tokens > target 면:
    │        1. compaction_enabled=False/콜백 미주입 → _evict_fifo(target)
    │        2. 그 외 → _compact() (Split→oldest 절반 evict→단일호출 요약(recursive)
    │           →_file_extract path dedup→[system][summary≤8K][file_list][retained]
    │           →compaction.json atomic write)
    │        3. Belt-and-braces: 여전히 > target 이면 _evict_fifo(target)
    │     self.messages = ctx.get_messages()       ← 축소 반영
    │
    ├─ provider.call(...)
    │
    └─ [호출 직후·성공] flow 1 reconcile: ctx.reconcile_actual_tokens(actual, S)
          actual = usage.input_tokens + cache_creation + cache_read  ← 서버 ground truth
          _cache_tokens = actual − S              ← messages 실측으로 re-anchor
          (usage 없으면 no-op → 추정 유지)

Threshold 계산:
    target = (context_window − system(실측) − max_output) × 0.8
    예: 262K − ~4K system − 4K out ≈ 254K × 0.8 ≈ 203K 임계
    기존(add 시 0.9 × (C−O−4000), system 고정 4000)을 대체 — system 실측 + 매
    호출 reconcile 로 chars/4 의 CJK 과소평가가 누적되지 않음(drift 1턴치로 제한).

LLM 호출 시 messages:
    [system verbatim][summary (있으면)][file_list (있으면)][자연어 변환된 dynamic]

세션 재개 시:
    compaction.json 로드 → dynamic_start_index 유효하면 history[index:]만 forward 파싱,
    아니면 history.jsonl 뒤에서부터 budget 내 메시지 파싱 (legacy 경로)
```

**flow 2 — Reactive overflow recovery (`force_fit`)**

```
provider.call() → 예외
    │
    └─ is_context_overflow(err)? (overflow.py 패턴: Anthropic/OpenAI/Ollama/omlx)
         │ yes & ctx 있음 & overflow_retries < _MAX_OVERFLOW_RETRIES(5)
         ↓
         parse_overflow_amounts(err) → (actual, limit)
             omlx "N tokens exceeds max context window of M tokens" → (N, M)
         target = (limit or budget) × 0.8
         ctx.force_fit(target, actual_tokens=actual)
             1. compact 시도 (enabled면)
             2. 부족하면 _evict_fifo(floor)  ; floor = _cache_tokens × (target/actual)
             3. progress 보장: 아무것도 안 줄면 oldest 1개 강제 pop
         → shrank? messages 갱신 + turn-=1 + _RETRY (재요청)
         → anchor만 남아 force_fit=False → 깔끔히 실패
    │
    └─ 성공 시 overflow_retries=0 리셋 (다음 turn은 fresh 예산)

배경: 로컬 추정(chars/4)이 CJK를 4~8배 과소평가 → flow 1 임계 미달 → 서버 400.
flow 2는 서버 신호(400 + 실제 토큰 수)를 ground truth로 삼아 사후 복구.
비율 축소라 추정 절대정확도 불필요; bounded(5회)라 무한 루프 없음.
```

- **압축 비활성화**: `--no-compaction` 또는 `AGENT_CLI_COMPACTION=off` → 플레인 FIFO만 동작 (env가 flag보다 우선; 운영자 kill switch)
- **Belt-and-braces**: LLM 요약 실패(`CompactionError`)나 재구성 후 캐시 미충족 모두 같은 FIFO 경로로 수렴 → 무한 트리거 루프 없음
- **Observability**: `TurnRecorder.record_compaction(tokens_before/after, evicted_count, fallback_used, failure_signal, duration_ms)` → `turns.jsonl`에 `event: "compaction"` 기록
- **UI**: `render_compaction_progress(phase, ...)` 단일 helper로 start / done / warning 라이프사이클 출력 (CLI·웹·SSE는 renderer 레벨에서 분기)
- **Scratchpad 없음.** history.jsonl이 대화 기록이자 artifact 인덱스
- **Context inject 없음.** LLM이 필요할 때 read_file로 pull
- System prompt에 Context Recovery Guide 포함
- 스킬/delegate는 부모 budget 상속

#### 저장과 표현의 분리

- **저장**: history.jsonl (JSON Lines) — 구조화된 메시지
- **표현**: 자연어 변환 — LLM에 전달되는 user/assistant 메시지

```
저장: {"role":"assistant","thought":"auth.py를 읽겠다","action":"read_file","action_input":{"path":"src/auth.py"}}
표현: auth.py를 읽어 구조를 파악해야 한다. → read_file(src/auth.py)
```

#### Assistant turn lifecycle — 4 forms, 3 plugin-owned transitions

assistant turn 한 번은 4가지 형태(A/B/C/D)로 conversation pipeline을 통과한다. 각 형태는 **소비자가 다르고** 따라서 **요구하는 셰이프도 다르다**. 형태 간 변환은 모두 wire_format plugin이 소유 — 새 wire format 추가 시 lifecycle 전체가 자동으로 그 plugin의 wire shape을 따른다.

| 형태 | 소비자 | 요구 셰이프 | 어디서 |
|---|---|---|---|
| (A) Emit | model이 produces | plugin wire shape, raw string | provider response |
| (B) Store | history.jsonl reader / 분석 스크립트 | 구조화 dict `{thought, action, action_input}` | `history.jsonl` |
| (C) Feed live | LLM (같은 세션의 다음 turn) | plugin wire shape | in-memory `messages` 버퍼 |
| (D) Feed 복원 | LLM (overflow / resume 후) | plugin wire shape ≈ (A) | recovered `messages` |

세 plugin 메서드가 형태 간 다리를 놓는다:

| 전이 | Plugin 메서드 | 입력 → 출력 | 호출 사이트 |
|---|---|---|---|
| (A) → (C) | `normalize_assistant_for_messages(raw)` | LLM raw text → in-memory messages 버퍼에 들어갈 문자열 | `loop._append_observation` |
| (A) → (B) | `serialize_assistant_for_history(raw)` | LLM raw text → 디스크에 쓰일 dict | `loop._append_observation` (`ctx.add` 직전) |
| (B) → (D) | `render_assistant_from_history(record)` | history.jsonl record → chat completion 메시지 dict | `manager._to_natural_language` (assistant branch) |

`serialize ↔ render`는 **서로의 역연산**: round-trip이 닫혀 있어 (A) ≈ (D). overflow / resume 후에도 모델이 자기 wire shape 그대로 봄 (self-reinforcement 보존). byte-level 차이는 JSON 정규화 (key 순서, 공백) 뿐, semantic 동일.

**WireFormat ABC가 lifecycle 디폴트 제공**: `serialize_assistant_for_history` 디폴트 = `self.parse()` + 구조화 필드 추출, `render_assistant_from_history` 디폴트 = `self.render_full_example()` 호출. `normalize_assistant_for_messages` = identity, `render_action_input` = identity, `prefill` = `""`, `provider_call_kwargs` = `{}`, `format_rules` = `build_format_rules(self)`. 새 plugin은 **format-specific 메서드만 구현**하면 lifecycle 전체가 자동으로 작동:
- `parse(llm_text)` — wire shape 파싱
- `render_full_example(thought, action, action_input)` — wire shape 출력 (Format Rules section + history round-trip 양쪽 이용)
- `format_rules_anchor()`, `format_rules_field_specific()` — 안내 문구
- 6개 recovery wording (framing × 2, reminder × 2, static hint × 2)
- `system_user_prefixes()` — recent_exchanges 필터링

`manager._to_natural_language`는 user / tool branch만 직접 처리하고 assistant branch는 plugin에 위임 — `context/`는 format-agnostic, plugin이 format-aware. 의존 방향: `context → wire_formats` (downward, lazy import 없음).

#### 세션 파일 구조

```
.agent-cli/sessions/{session_id}/
├── history.jsonl                              ← main 대화 기록
├── main_plan_e8d4_20260405T143112890.md       ← main artifact (flat)
│
├── delegate_coder_f1a9_20260405T143230456/    ← delegate subdir
│   ├── history.jsonl                          ← delegate 내부 대화
│   └── result.md                              ← delegate 최종 결과
│
└── skill_summarize_d4e1_20260405T143200100/   ← skill subdir
    ├── history.jsonl                          ← skill 내부 대화
    └── result.md                              ← skill 최종 결과
```

- main: root에 flat artifact
- delegate/skill: subdir에 history.jsonl + result.md (재귀 중첩 가능)
- fork 모드: parent history.jsonl 복사 → delegate가 이어서 append

---

## 6. 도구 시스템

### 6.1 등록된 도구

**실제 도구** — 파일/셸/네트워크 작업 수행:

| 도구 | 설명 | 필수 입력 | 출력 |
|------|------|----------|------|
| `read_file` | 파일 읽기 (hashline 포맷). 모드: `stat` (메타데이터 + 앞 20줄), `search` (정규식 grep), `line_start/line_end` (부분 범위 — 범위가 파일 전체를 덮으면 whole-file read), 또는 mode 없이 full read. Mode 없는 bare full read는 파일이 threshold(`AGENT_CLI_READ_FILE_LIMIT` env, 기본 300줄) 초과 시 거부되고 stat-형태 응답으로 대안 제시 — 거부 메시지가 전체 파일이 필요한 경우를 위해 `line_start=1, line_end=<total>` 구체 예시까지 박아줌. 전용 escape-hatch 파라미터는 없음 (line_start/line_end로 일원화). | `path` | `LINE#HASH:content` 형식 또는 `[refused-full-read]` |
| `write_file` | 파일 생성/덮어쓰기 | `path`, `content` | 저장 확인 메시지 |
| `edit_file` | hashline 기반 파일 편집 | `path`, `edits[]` | 편집 확인 메시지 |
| `shell` | 셸 명령 실행 | `command` | stdout + stderr + exit code |
| `delegate` | in-process 서브에이전트 위임 | `tasks[]` (각 항목: task, context?, tools?, agent?) | 구조화된 결과 (output + activity log + duration) + delegate subdir 경로, 복수 시 병렬 |
| `read_context` | 세션 이력 조회 | `mode`, `keyword`, `scope?`, `sessions?`, `loc?`, `range?` | **list**: 전체 세션 목록. **search**: 기본 현재 세션, `sessions="all"` 또는 ID로 확장; `scope`로 필드 필터 (reasoning/tool/observation/query); 결과 턴 블록 + preview 200자 cap + 50건 truncation + fetch hint footer. **fetch**: `loc='{session}/{path}:{line}'` (search 결과 그대로) 로 전체 턴 회상; `loc` 단일/배열 (max 10), `range` 0-5 (앞뒤 N턴). multi-line 보존, action_input compact JSON, all-or-nothing 시멘틱. |
| `fetch` | 웹 페이지 fetch → 마크다운 변환 | `url` | 재귀 링크 추출, 에러 힌트 |

**가상 도구** — loop.py if-cascade가 인터셉트해 직접 처리 (실제 tool dispatch 우회). LLM에게는 일반 도구처럼 노출 (시스템 프롬프트의 ``## Available Tools`` 섹션 포함):

| 도구 | 설명 | 필수 입력 | 비고 |
|------|------|----------|------|
| `complete` | 작업 완료 신호 | `result` | 루프 종료 |
| `ask` | 사용자에게 질문 | `questions` | 대화형 전용 (ctx 없으면 제거) |
| `run_skill` | 스킬 실행 | `name` | loop 레벨 인터셉트, skill subdir 생성 |
| `ready_for_review` | 작업 검증 요청 | `summary` | 원본 query 반환하여 self-check |

### 6.2 delegate agent 로딩

`delegate` 도구의 `agent` 파라미터로 사전 정의된 에이전트 역할을 로드할 수 있습니다:

```
검색 경로 (우선순위 순):
  1. .agent-cli/agents/{name}.md  (프로젝트 로컬)
  2. ~/.agent-cli/agents/{name}.md (유저 전역)

에이전트 파일 형식:
  ---
  allowed-tools: [read_file, shell]   # 선택: 허용 도구 제한
  model: claude-sonnet-4-6            # 선택: 모델 오버라이드
  ---
  에이전트 역할/원칙 본문 (시스템 프롬프트의 Agent Role 섹션에 주입)
```

**핵심 함수** (`tools/delegate.py`):
- `_validate_agent_name(name)` — 이름 검증 (`[a-zA-Z0-9_-]`만 허용)
- `_load_agent(name)` — 파일 탐색 + YAML frontmatter 파싱 → `(role_prompt, config, error)`
- `_extract_activity_log(messages)` — 컨텍스트 메시지에서 per-turn 액션 요약 추출
- `_summarize_action(action, action_input)` — 단일 액션을 한 줄 요약으로 포맷
- `_extract_last_actions(messages, n)` — 마지막 N개 액션 + 에러 observation 추출
- `_persist_delegate_result(formatted, delegate_dir)` — result.md를 delegate subdir에 저장
- `_format_delegate_output(result)` — DelegateResult를 구조화된 observation 문자열로 포맷
- `_AGENT_SEARCH_PATHS` — 검색 경로 리스트
- `_FRONTMATTER_PATTERN` — `---` frontmatter 정규식

**DelegateResult 필드**: `output`, `duration_secs`, `activity_log`, `last_actions`, `iterations`

**산출물 구조**: delegate 실행 결과는 다음 섹션을 포함:
1. 서브에이전트 출력 (output 또는 "(subagent returned no result)")
2. `[Subagent activity]` — per-turn 액션 로그 (최대 20개)
3. `[Last actions before failure]` — 실패 시 마지막 5개 액션 + 에러 힌트
4. `[Duration: Ns]` + `[Subagent used N turns]` — 실행 메타데이터
5. `→ delegate_{name}_{hash}_{ts}/` — delegate subdir 경로 (history.jsonl + result.md)

**적용 우선순위**: task에 명시된 `tools`/`model`이 agent 파일 설정보다 우선합니다.

**병렬 delegate lifecycle 통합 인터페이스** (`_run_parallel`):
- 각 worker thread 는 `renderer.begin_delegate_task(task_id, ...)` → `_run_single` → `renderer.end_delegate_task(task_id, ...)` 만 호출. 그 외 panel/capture 오케스트레이션 전부 renderer 책임.
- MinimalRenderer 가 첫 begin 에서 Live 영역 띄움 (`is_terminal` 체크 — non-tty 면 skip), 자체적으로 thread → task slot → capture buffer 매핑. emit (`thought`/`action`/`observation`) 마다 `set_thread_status` 로 카드 상태 업데이트, `_capture_line` 으로 버퍼 누적.
- 마지막 end 에서 Live 종료 + 각 task 의 captured 출력을 `┌─ 🦀 [N] agent: task` group 으로 wrapping 해서 replay (등록 순서).
- WebRenderer 는 같은 begin/end 마커로 SSE `delegate_task_start` / `delegate_task_end` 이벤트 emit, `_thread_to_task` 로 후속 emit 들에 task_id 자동 첨부 → 프론트가 collapsible card 로 routing.
- 두 renderer 의 lifecycle surface 가 동일 (begin/end 만). 새 renderer 추가 시 이 두 메서드만 override 하면 됨.

### 6.3 run_skill 결과 포맷

`run_skill` 실행 결과에는 스킬 식별 헤더가 포함:

```
STATUS: success
RESULT:
SKILL: summarize(./)
The agent-cli directory contains a ReAct pattern-based agent CLI...
```

- `SKILL: name(arguments)` — 실행된 스킬과 인자
- 스킬은 자체 subdir에 history.jsonl + result.md 저장
- 도구 교집합: skill allowed-tools ∩ parent allowed-tools (빈 교집합 시 거부)
- Role 상속: parent의 Role을 이어받음

### 6.4 Hashline 시스템 (`tools/read_file.py`)

```
원본 파일:             hashline 출력:
def hello():    →    1#VR:def hello():
    return "hi"      2#KT:    return "hi"
                     3#ZZ:

해시 알고리즘: CRC32(line_content, seed) & 0xFF → 2-char 태그
시드: 내용 있는 줄 → 0, 빈 줄 → line_number
알파벳: ZPMQVRWSNKTXJBYH (16자 기반 256 조합)
```

편집 연산:
```json
{"op": "replace", "pos": "2#KT", "lines": ["    return 'hello'"]}
{"op": "replace", "pos": "1#VR", "end": "3#ZZ", "lines": ["def greet():", "    pass"]}
{"op": "append",  "pos": "1#VR", "lines": ["    # 주석"]}
{"op": "prepend", "pos": "1#VR", "lines": ["# 헤더"]}
{"op": "append",  "lines": ["# EOF"]}  // pos 없으면 파일 끝
```

퍼지 매칭 (`edit_file.py`): 해시 불일치 시 공백/따옴표/대시 정규화 후 재매칭. LLM 재호출 없이 비용 제로 보정.

**Multi-edit 안전장치.** `edits[]` 한 호출에 여러 편집이 들어올 때 두 가지 모호성을 사전에 거부:

- **중복 ref 거부 (Layer 1)** — 두 편집이 같은 `pos` 또는 같은 `end` 태그를 참조하면 reject. 같은 줄을 두 번 다른 방식으로 바꾸려는 의도가 정의되지 않음.
- **범위 겹침 거부 (Layer 2)** — 각 edit을 (start_line, end_line) 구간으로 환산해 pairwise 검사. 범위가 겹치면 어느 쪽이 먼저 적용돼야 할지 모호하므로 reject. 같은 위치에 append/prepend 같이 의도적으로 동일 좌표를 쓰는 케이스는 별도로 허용.

거부 시 단일 fail 메시지로 전체 호출이 atomically 실패 — 일부만 적용된 후 hash mismatch로 멈추는 케이스 차단.

### 6.5 Tool Output 전달 방식

Tool output은 **잘림(truncation) 없이 전체를 그대로** LLM에 전달합니다. 이전에는 context window의 3% 비율로 잘랐으나(`tools/truncation.py`, 삭제됨), LLM이 불완전한 정보로 판단하는 성능 열화가 확인되어 제거. context가 budget의 90%를 넘으면 `context/manager.py`의 compaction이 oldest 절반을 LLM 요약으로 흡수하고, 실패/미충족이면 belt-and-braces로 FIFO drop이 메시지 단위로 떨궈냄.

### 6.5.0 `read_file` Full-Read Guard

큰 파일을 mode 없이 읽어 컨텍스트 예산을 순식간에 소진하는 패턴을
억제하는 tool-level guard. 동작:

- `read_file(path)` + 파일이 threshold(`AGENT_CLI_READ_FILE_LIMIT` env
  var, 기본 300줄) 초과 → `[refused-full-read]` 응답 (stat-형태의
  메타데이터 + 앞 20줄 + 대안 예시).
- `stat=true` / `search=` / `line_start/line_end` 모드는 guard 무시.
- 실제 **전체가 필요하면** 거부 메시지에 명시된 형태 그대로
  `read_file(path, line_start=1, line_end=<total>)` 호출. `<total>`
  값은 거부 응답에 이미 박혀있어서 LLM이 그대로 복사하면 됨.
  별도 boolean escape-hatch 파라미터 (예: `full=true`)는 **없음** —
  line_start/line_end 단일 경로로 통일.
- Threshold ≤ 0 → guard 비활성 (CI/배치에서 유용).

**왜 별도 escape hatch 없이 line-range로 통일했나**:
`full=true` 같은 단어 하나짜리 플래그는 LLM이 반사적으로 고르기
쉬운 "최소 저항 경로". 반면 `line_start=1, line_end=370` 은 LLM이
파일 크기를 **명시적으로 인지해서 숫자로 적어야** 하는 행위. 후자가
더 강한 "의식적 선택" 신호. 게다가 API 표면이 작아지고, 부분/전체
read의 의미론이 "범위 지정"이라는 한 개념으로 일원화됨. LLM이
`line_start=1, line_end=<very large>` 같은 "사실상 full read" 패턴을
쓰는 것은 막지 않음 — 그것도 의식적 선택으로 간주.

**설계 경계** — guard는 **bare full read의 습관적 사용**만 차단한다.
`line_start/line_end` 로 범위 지정, `search=` 로 패턴 지정,
`stat=true` 로 메타 조회는 모두 **LLM의 의식적 선택**으로 간주하여
threshold를 적용하지 않는다. 사용자가 "1-to-1200" 범위 같은 bypass
패턴을 관찰해도 이는 정상 동작 — guard의 목적은 "반사적 blunder
방지"이지 "1회 전송량 상한"이 아니다. 상한이 필요하다면 context
manager(대화 압축)가 downstream에서 처리한다.

### 6.5.0b Shell Output: full passthrough (이전 artifact guard 제거됨)

이전엔 shell 출력이 한도(기본 500줄 / 20KB) 초과 시 head/tail 미리보기로
치환하고 전체를 `<session>/shell/`에 저장하는 guard가 있었음. 2026-05-19
제거 — 실사용에서 **head/tail이 중간 디버깅 정보(error trace, 핵심
로그 라인)를 silent하게 누락**시켜 task가 풀리지 않는 사례 두 차례
관찰. 가드의 절약 효과보다 silent loss의 비용이 컸음.

현재 정책: **shell 출력은 잘리지 않고 그대로 LLM observation으로 전달**.
컨텍스트 budget 관리는 messages buffer의 2-tier 관리가 담당
(`context/manager.py`) — 90% 초과 시 oldest 절반을 LLM 요약으로 흡수,
요약 실패/미충족이면 FIFO drop으로 떨궈냄. 의도된 거대 출력
(`find /`, `cat huge.log`)은 모델이 자기 비용 인지하에 호출한 것으로 간주.

LLM이 출력을 좁히고 싶으면 도구 호출 자체를 좁혀야 함 (`tail -n 100`,
`grep ERROR`, `head -c 4096` 등). silent truncation 없음.

관련 환경변수 (`AGENT_CLI_SHELL_OUTPUT_LIMIT_*`, `AGENT_CLI_SHELL_ARTIFACT_*`)
도 함께 제거됨. read_file의 full-read guard (§6.5.0a)는 유지 — 파일은
재현 가능하니 escape hatch가 의미 있지만, shell은 부작용/비결정성으로
재실행이 안전하지 않다는 차이.

### 6.5.1 Fulfillment Review (`ready_for_review`)

LLM이 작업 완료 전 자기 검증을 수행하는 가상 도구입니다.

1. LLM이 `ready_for_review(summary="...")` 호출
2. Loop이 intercept → **원본 query + summary + 검증 절차**를 observation으로 반환
3. LLM이 요청 vs 실행 내역을 대조 → 빠뜨린 게 있으면 계속, 다 했으면 `complete` 호출

`_ALWAYS_INCLUDE`에 등록되어 skill의 `allowed_tools`와 무관하게 항상 API tool 목록에 포함됩니다.

Observation은 `_build_review_observation` (loop.py)이 합성합니다:
`--- ORIGINAL REQUEST ---` / `--- YOUR SUMMARY ---` / *(옵션)* `--- YOUR TOOL CALLS ---` /
`--- REVIEW INSTRUCTIONS ---` / `Format your review like this:`.

마지막 섹션은 모델이 자유 텍스트로 "Done" 한 줄 응답하지 못하도록
`Requirement N: ... → [DONE | MISSING]: ...` / `Decision: complete | continue` 출력 템플릿을
강제합니다. self-review가 *생성* 되어야 reasoning이 따라오는 작은 모델 특성에 맞춘 디자인.

`--- YOUR TOOL CALLS ---` 섹션은 `_format_tool_calls_for_review(ctx)`가 ctx의 raw
messages에서 assistant tool calls만 추출해 컴팩트하게 렌더 (`tool(k=v, ...)`)합니다.
virtual tools(`complete` / `ask` / `ready_for_review`)는 제외. 30개 초과 시 최근 30개만
유지하고 `(last 30 of N)` 표기. 긴 string 인자는 40자로, 비스칼라 인자는 `<list>`/`<dict>`
타입 마커로 축약. 이 섹션이 도구 호출 사실 목록을 모델에 명시 노출해, Observation 본문이
context FIFO로 evict되어도 "내가 무슨 도구를 불렀는지"는 review 시점에 보존됩니다.
ctx가 None이거나 실제 도구 호출이 없으면 섹션은 생성되지 않습니다.

### 6.6 스키마 검증 (`tools/registry.py`)

검증 순서:
1. 도구 존재 확인
2. action_input이 string이면 → dict 자동 변환 시도
3. 필수 필드 존재 확인
4. 타입 검증 + 자동 변환:
   - `"30"` (string) → `30` (integer)
   - `{}` (dict) → `[{}]` (array)
   - `42` (int) → `"42"` (string)

---

## 7. 프로바이더 시스템

### 7.1 LLMProvider 프로토콜 (`providers/base.py`)

```python
class LLMProvider(Protocol):
    def call(
        self,
        messages: list[dict],
        system: str,
        model: str,
        capabilities: ModelCapabilities,
        **kwargs,          # tools, skip_json_format 등
    ) -> LLMResponse: ...
```

### 7.2 프로바이더별 구현

| 프로바이더 | 엔드포인트 | 인증 | 구조화 출력 | Thinking |
|-----------|-----------|------|-----------|---------|
| **Anthropic** | `/messages` | x-api-key | - | budget_tokens |
| **OpenAI Compat** | `/chat/completions` | Bearer token | `response_format={"type":"json_object"}` (basic JSON) | reasoning_effort |
| **Ollama** | `/api/chat` | 없음 | `format="json"` (basic JSON) | num_predict |

네이티브 tool calling (Anthropic `tool_use`, OpenAI `function calling`)은 **사용하지 않습니다**. 모든 프로바이더가 동일하게 ReAct 텍스트 파싱을 거치므로 provider-specific 코드 경로가 줄고, 프로바이더 편차가 거의 없어집니다.

**구조화 출력 정책**: 세 프로바이더 모두 **basic JSON mode**만 사용하고, **strict JSON Schema는 쓰지 않습니다**. 이는 확장성을 위한 선택이며 다음과 같은 배경이 있습니다:

- 이전 구현은 Ollama에서 `format=<REACT_JSON_SCHEMA>`(strict)를 보냈지만, Ollama의 mlx 엔진으로 패키징된 일부 모델(예: safetensors 포맷)에서 HTTP 200 + 스트림 중간 `{"error": "mlx runner failed"}`로 조용히 깨졌음.
- Basic JSON mode(`format="json"` / `response_format={"type":"json_object"}`)는 "유효한 JSON을 내라"는 신호만 주고 스키마는 강제하지 않음. 거의 모든 백엔드가 지원.
- ReAct JSON 구조 강제는 대신 시스템 프롬프트의 `FORMAT_RULES`와 3단계 파서(json.loads → json_repair → regex)가 담당. 32B+ 모델에서 신뢰성 충분.
- 7-14B 모델은 schema 없을 때 포맷 drift가 늘지만, 이 사이즈는 README에서 이미 비권장 구간.

향후 특정 백엔드가 strict schema를 반드시 필요로 하면, 현재 기본값을 건드리지 말고 **opt-in 플래그**로 다시 도입할 것. mlx 패키지 모델에서 재발 여지가 있으므로 기본 활성화는 금지.

### 7.3 프로바이더 팩토리 (`providers/__init__.py`)

```python
create_provider("anthropic", base_url, api_key)  → AnthropicProvider
create_provider("openai", base_url, api_key)     → OpenAICompatProvider
create_provider("ollama", base_url, api_key)      → OllamaProvider
```

OpenAICompatProvider 하나로 OpenAI, vLLM, LM Studio, mlx-lm을 `--base-url`만 바꿔서 커버.

### 7.4 Thinking Budget 적용

| 프로바이더 | 파라미터 | 동작 | thinking_format |
|-----------|---------|------|----------------|
| Ollama | `options.num_predict = budget + max_output` | thinking + 출력 토큰 합산 | `"think"` (Qwen3, DeepSeek-R1) |
| Anthropic | `thinking.budget_tokens = budget`, `max_tokens += budget` | Anthropic이 max_tokens에서 thinking 차감 | `""` (API 레벨 처리) |
| OpenAI | `reasoning_effort = low/medium/high` | budget ≤1024→low, ≤8192→medium, >8192→high | `""` (API 레벨 처리) |

Thinking 블록 처리 플로우:
1. Ollama thinking 모델 → `<think>...</think>` 블록을 텍스트에 출력
2. `parse_react()`가 `_strip_thinking_blocks()`로 블록 분리
3. 분리된 thinking 내용은 `ParsedAction.thinking`에 보존
4. 나머지 텍스트(JSON)만 파싱 → Stage 1 직접 성공률 향상

### 7.5 재시도 헬퍼 (`providers/http.py`)

세 프로바이더 모두 동일한 재시도 래퍼 `post_with_retry(requests.post, url, **kwargs)`를 거쳐 HTTP를 발송합니다. 목적은 on-prem LLM 서버(Ollama / vLLM)에서 간헐적으로 발생하는 일시적 네트워크 오류 — 서버 재시작 직후의 `ConnectionError`, 첫 호출 시 모델 로딩이 늦어서 발생하는 `Timeout` — 을 사용자 레벨로 노출하지 않고 복구하는 것입니다.

**범위: pre-stream only.** `requests.post()` 호출 자체에서 발생한 예외만 재시도합니다. 스트리밍이 시작된 이후(즉 `requests.post(stream=True)`가 Response를 돌려준 뒤) 청크를 읽다가 발생한 오류는 재시도 대상 아님 — 이미 소비된 청크가 중복되면 LLM 출력이 깨지기 때문.

**재시도 대상 예외:**
- `requests.Timeout` (ConnectTimeout, ReadTimeout 포함)
- `requests.ConnectionError`
- HTTP 4xx/5xx는 재시도 **안 함**. `raise_for_status()`는 `post_with_retry` 반환 *뒤에* 호출되어 서버의 거절 응답을 그대로 caller로 전달.

**백오프:** 고정 1초 (지수 아님). on-prem 단일 사용자 전제라 rate-limit / thundering-herd 대책이 필요 없고, `ConnectionError` 직후 서버 부팅 마무리에만 약간의 헤드룸을 주면 충분. `Timeout`은 이미 긴 대기였으므로 추가 대기 효과는 작지만 해롭지도 않음.

**설정:**
- `AGENT_CLI_LLM_RETRY_ATTEMPTS` (기본 3, 최초 포함 총 시도 횟수; 0/음수는 1로 clamp)
- `AGENT_CLI_LLM_RETRY_DELAY` (기본 1.0초)

**가시성:** 재시도 시 `render_status("running", ...)` 한 줄로 사용자에게 표시(예: `LLM request failed (Timeout) — retrying (2/3)`). spinner는 계속 돌아감. 모두 실패하면 `render_status("error", ...)` 후 마지막 예외를 그대로 raise. verbose 모드에서는 `agent_cli.verbose.debug_log`로 stderr에도 한 줄 남김.

**테스트 호환:** `post_with_retry`는 `post_fn`을 인자로 받고, 각 프로바이더는 자기 네임스페이스의 `requests.post`를 명시적으로 넘깁니다. 덕분에 기존 테스트가 `agent_cli.providers.{name}.requests.post`를 패치하는 패턴이 그대로 동작.

### 7.6 공용 debug 유틸 (`verbose.py`)

`agent_cli/verbose.py`가 verbose 플래그와 `debug_log()`의 단일 소유자입니다. 과거에는 `loop.py` 모듈 안에 `_debug_verbose` / `_debug_log`로 있었으나, `providers/http.py`가 재시도 로그를 찍어야 하면서 provider 레이어가 loop를 역참조하지 않도록 추출했습니다. `loop.py`는 하위 호환을 위해 해당 심볼을 그대로 재-export합니다.

---

## 8. 설정 시스템

### 8.0 config.json (프로바이더/모델 설정)

```json
{
  "provider": "ollama",
  "base_url": "http://localhost:11434",
  "api_key": "",
  "default_model": "qwen3:32b"
}
```

**3레이어 병합** (`load_config()`):
```
env vars (AGENT_CLI_*)  →  최저 우선순위
~/.agent-cli/config.json →  사용자 전역
.agent-cli/config.json   →  워크스페이스 (최고)
+ CLI 파라미터             →  임시 오버라이드
```

필드 단위 병합: 상위 레이어가 해당 필드를 가지면 덮어씀, 없으면 하위에서 상속.

**SetupWizard** (`setup.py`): 설정 파일이 없으면 자동 실행.
`agent-cli setup`으로 수동 재설정 가능.

**DIRECTIVE.md** — 프로젝트 지시사항 (`prompts/system_prompt.py`):
```
.agent-cli/DIRECTIVE.md   →  프로젝트별 규칙 (우선 로드)
~/.agent-cli/DIRECTIVE.md →  사용자 전역 규칙
```
- 둘 다 존재하면 모두 로드 (content hash 중복 제거)
- content hash 중복 제거, truncation 없음 (ResourceLoader 기반)
- 매 세션 시작 시 system prompt 동적 영역에 주입

### 8.1 models.json 구조

```json
{
  "models": {
    "<model_id>": {
      "provider": "anthropic | openai | ollama",
      "context_window": 32768,
      "max_output_tokens": 4096,
      "supports_structured_output": true,
      "supports_thinking": true,
      "thinking_budget": 4096,
      "supports_strict_schema": false
    }
  },
  "provider_defaults": {
    "ollama": {"base_url": "http://localhost:11434", "default_model": "qwen3:32b"},
    "openai": {"base_url": "https://api.openai.com/v1", "default_model": "gpt-4o"},
    "anthropic": {"base_url": "https://api.anthropic.com/v1", "default_model": "claude-sonnet-4-20250514"}
  }
}
```

### 8.2 파일 위치 및 정책

| 우선순위 | 위치 | 역할 | 자동 저장 |
|---------|------|------|----------|
| 1 | `.agent-cli/models.json` | 프로젝트 로컬 오버라이드 | 안 함 (읽기만) |
| 2 | `~/.agent-cli/models.json` | 사용자 전역 설정 | 새 모델 자동 저장 |
| 3 | `agent_cli/default_models.json` | 패키지 기본값 | 안 함 (읽기만) |

### 8.3 설정 로딩 우선순위 (`config.py`)

3개 파일을 병합하되, 높은 우선순위가 낮은 우선순위를 오버라이드:
1. `agent_cli/default_models.json` (패키지) — 먼저 로딩
2. `~/.agent-cli/models.json` (전역) — 동일 키 덮어쓰기
3. `.agent-cli/models.json` (프로젝트 로컬) — 동일 키 덮어쓰기 (최종)
4. 하드코딩 폴백 (모든 파일 없어도 동작)

### 8.4 능력치 조회 우선순위 (`providers/compat.py`)

1. `models.json` 정적 설정 (병합된 결과)
2. 런타임 감지 → **`~/.agent-cli/models.json`에 자동 저장**
   - Ollama: `/api/show` (메타데이터) + `/api/chat` (thinking 프로브 + format 프로브)
   - OpenAI 호환: `/chat/completions` (thinking 프로브)
3. `DEFAULT_CAPABILITIES` (context_window=4096, 모든 기능 비활성)

프로브는 진행 콜백을 받아 첫 실행 시 어느 단계가 돌고 있는지 사용자에게 표시 (`set_progress_callback`). 한 번 감지된 결과는 `_auto_detected: true` 마커와 함께 저장되어 재실행 시 프로브 생략.

### 8.6 Thinking 감지 방식

하드코딩 패턴 매칭이 아닌 **프로브 기반 감지**:
1. 모델에 "What is 2+2?" 프롬프트 전송
2. 두 가지 위치에서 thinking 확인:
   - `message.thinking` 필드 (Ollama API — Qwen3, Qwen3.5, GLM 등)
   - `<think>`, `<thinking>`, `<reasoning>`, `<reflection>` 태그 in content (DeepSeek-R1 등)
3. 감지되면 → `supports_thinking=True`, `thinking_format=감지방식`
4. 결과를 `~/.agent-cli/models.json`에 저장 (`_auto_detected: true`) → 다음 실행 시 프로브 불필요
5. 모델 업데이트 시 자동 감지 항목은 재감지로 갱신됨 (수동 등록 항목은 보호)

새 모델이 추가되어도 코드 수정 없이 자동 감지됩니다.

OpenAI 호환 서버(vLLM 등)에서는 `/v1/models` API로 context window도 감지합니다 (`max_model_len` 필드).

#### Format 프로브 (Ollama 전용)

일부 모델/백엔드 조합 (예: Ollama의 mlx tensor 포맷)은 `format="json"` 파라미터를 받으면 런타임 에러를 냅니다. 프로브가 형식 강제 호출을 한 번 시도해 성공하면 `supports_structured_output=True`, 실패하면 False — 결과는 모델별로 캐시되어 이후 호출에서 자동 적용. 사용자가 직접 모델 호환성을 추측할 필요가 없습니다.

### 8.5 모델 정보 출력

| 상황 | 출력 |
|------|------|
| 새 모델 감지 + 저장 | Rich Panel (상세 — context, thinking, tool calling 등) |
| 기존 모델 로딩 | 한 줄 요약 (`● Model: name (ctx=N, thinking=✓)`) |

---

## 9. 시스템 프롬프트 아키텍처 (`prompts/system_prompt.py`)

LLM attention 패턴에 최적화된 섹션 순서 — Primacy(앞), Middle(중간), Recency(끝):

```
build_system_prompt(capabilities, active_tools, include_delegate, skill_stack, session_id, agent_role)
    │
    │  ── Primacy: 정체성 + 핵심 원칙 (강한 attention) ──
    │
    ├─ ROLE_PROMPT (항상 포함 — 에이전트 역할 정의)
    │
    ├─ CONTEXT_DISCIPLINE (항상 포함 — 컨텍스트 창이 핵심 리소스임을 교육)
    │   └─ "읽을 것만 읽어라 / thought 간결 / 불필요한 덤프 금지"
    │
    ├─ TASK_GUIDELINES (항상 포함 — 코드 작업 원칙 7개)
    │   └─ 코드 읽기 선행, 범위 제한, 보안, 정직한 보고 등
    │
    ├─ FORMAT_RULES (항상 포함 — JSON ReAct 포맷 + 규칙 10개)
    │   └─ ready_for_review → complete 워크플로, 재귀 금지, 단일 액션 강제,
    │      효율적 액션 선택 (batch 필드 활용 / shell 파이프라이닝 / 좁은 read 모드 우선)
    │
    │  ── Middle: 레퍼런스 (필요시 참조) ──
    │
    ├─ Available Tools (active_tools + _ALWAYS_INCLUDE)
    │   └─ 정적 도구 먼저 (KV cache 안정), 조건부 도구 뒤에
    │   └─ 가이드가 해당 도구에 inline (별도 섹션 없음):
    │       - edit_file ← Hashline Guide
    │       - delegate ← Delegation Guide
    │
    ├─ Available Skills (skill_stack에 없는 스킬만, run_skill 사용 안내)
    │
    ├─ Available Agents (depth < max_depth + agent_stack 재귀 방지)
    │   └─ .agent-cli/agents/ + ~/.agent-cli/agents/ + builtin/ 스캔
    │
    │  ── Recency: passive reference → active rules → immediate constraint ──
    │
    ├─ Environment (항상 포함 — CWD, 플랫폼)
    │   └─ 날짜는 의도적으로 제외 — KV prefix cache 안정성 (자정 rollover 방지)
    │
    ├─ Context Recovery Guide (session_dir가 있을 때만)
    │   └─ "이전 대화 내용이 필요하면 read_file({session_dir}/history.jsonl)"
    │
    ├─ Directives (DIRECTIVE.md가 존재할 때만)
    │   └─ .agent-cli/DIRECTIVE.md (프로젝트) + ~/.agent-cli/DIRECTIVE.md (유저 전역)
    │
    └─ Execution Context (skill_stack/agent_stack이 있을 때만 — Recency 마지막)
        ├─ "Call stack: main → agent:reviewer → skill:plan"
        ├─ "Do not delegate to or invoke: reviewer, plan (already in call stack)"
        └─ 세션 내 변동 가능한 유일한 Recency 섹션 → 끝에 두어 앞 3개를 안정적
           KV prefix로 보존
    
    Role 선택 (Primacy 영역):
    - main: 기본 ROLE_PROMPT
    - delegate: Agent Role이 기본 Role을 대체
    - skill: parent의 Role 상속
```

---

## 10. 테스트 아키텍처

### 10.1 테스트 분류

| 분류 | 파일 수 | 테스트 수 | 실행 방법 |
|------|---------|----------|----------|
| 유닛 테스트 | ~69 | ~1814 | `pytest tests/ -m "not ollama_integration"` |
| 통합 테스트 | 1 | 22 | `pytest tests/test_integration.py` |
| **전체** | **70** | **~1850** | `pytest tests/` |

### 10.2 통합 테스트 모델 구성 (`tests/conftest.py`)

```python
DEFAULT_MODELS = [
    "qwen3-coder:30b",       # Thinking + 코딩 특화
    "glm-4.7-flash:q8_0",    # Non-thinking 범용
    "qwen3.5:35b",            # 최신 세대 범용
]
```

모델 변경:
```bash
# 환경 변수로 변경
INTEGRATION_MODELS="model1,model2" pytest tests/test_integration.py

# conftest.py의 DEFAULT_MODELS 리스트 수정
```

### 10.3 테스트 실행

```bash
# 유닛 테스트만 (빠름, CI용)
pytest tests/ -m "not ollama_integration" -v

# 통합 테스트만 (Ollama 필요)
pytest tests/test_integration.py -v

# 전체
pytest tests/ -v

# 특정 모듈
pytest tests/test_react_parser.py -v
```

---

## 11. CLI 명령어 레퍼런스

### 11.1 `run` — 단발 실행

```bash
agent-cli run "task description" [options]
  -p, --provider    ollama | openai | anthropic    (기본: ollama)
  -m, --model       모델 ID                       (기본: 프로바이더 기본값)
  --base-url        API 엔드포인트
  --api-key         API 키 (환경 변수 자동 감지)
  -n, --max-turns    최대 턴 (0=무제한)
  --max-depth       서브에이전트 중첩 깊이 (기본: 2)
  --delegate-timeout 서브에이전트 타임아웃 초 (기본: 300)
  -v, --verbose     원시 LLM 응답 표시

  /sh <cmd>         LLM 없이 셸 명령 직접 실행
```

`run`도 `chat`과 동일하게 세션/컨텍스트(compaction + FIFO fallback + history.jsonl + compaction.json)를 관리합니다. 완료 후 세션 ID가 출력되며 `chat --resume <id>`로 이어서 작업할 수 있습니다 (compaction state는 `dynamic_start_index`로 복원되어 summarised tail과 중복 없음).

### 11.2 `chat` — 대화형 모드

```bash
agent-cli chat [options]
  (run 옵션 포함)

  # 대화 중 명령어:
  /help, /?          명령어 목록
  /quit, /exit       세션 종료
  /clear             컨텍스트 초기화
  /sh <cmd>          셸 명령 실행
  /skills            사용 가능한 스킬 목록
  /<skill> <args>    스킬 실행
  /ctx_window        컨텍스트 윈도우 덤프 (디버그)
```

---

## 12. 확장 가이드

### 12.1 새 프로바이더 추가

1. `providers/` 디렉토리에 새 파일 생성 (예: `google.py`)
2. `LLMProvider` 프로토콜을 만족하는 클래스 구현:
   ```python
   class GoogleProvider:
       def __init__(self, base_url: str, api_key: str): ...
       def call(self, messages, system, model, capabilities, **kwargs) -> LLMResponse: ...
   ```
3. `providers/__init__.py`의 `create_provider()`에 분기 추가
4. `config.py`의 `_PROVIDER_FALLBACKS`에 기본값 추가
5. `models.json`에 모델 등록
6. `tests/test_providers.py`에 테스트 추가

### 12.2 새 도구 추가

1. `tools/` 디렉토리에 새 파일 생성 (예: `search.py`)
2. `tool_search(args: dict) -> str` 함수 구현
3. `tools/registry.py`의 `TOOL_SCHEMAS`에 스키마 추가
4. `tools/__init__.py`의 `TOOLS` dict에 등록
   - 가상 도구(loop 인터셉트)면 `loop.py`의 if-cascade에 `if parsed.action == "<name>":` 분기 추가
   - 항상 포함되어야 하면 `registry.py`의 `_ALWAYS_INCLUDE`에도 추가
5. `tests/test_registry.py`에 검증 테스트 추가

### 12.3 새 모델 등록

`models.json`에 항목 추가:
```json
"new-model:14b": {
  "provider": "ollama",
  "context_window": 16384,
  "max_output_tokens": 4096,
  "supports_structured_output": true,
  "supports_thinking": false,
  "thinking_budget": 0,
  "supports_strict_schema": false
}
```

미등록 모델은 런타임 감지(Ollama) 또는 보수적 기본값으로 동작합니다.

### 12.4 새 wire format 추가

ReAct 외 새 응답 형식(예: PREFIX-MD 마크다운, OpenAI 스타일 tool call,
실험용 multi-action 등)을 추가하려면 `agent_cli/wire_formats/`에 새 모듈 한 개를
만들면 됩니다. **main code path(loop.py / system_prompt.py / recovery/)는 수정하지
않습니다** — 분기점이 `WireFormat` ABC 안에 격리되어 있기 때문입니다.

ABC가 lifecycle / 식별 hook의 default를 제공하므로 plugin은 **format-specific
abstract method만 구현**하면 됩니다. 나머지는 자동 작동.

1. `agent_cli/wire_formats/<name>.py` 생성:
   ```python
   from agent_cli.wire_formats.base import ParsedAction, WireFormat

   class MyFormat(WireFormat):
       name = "my_format"
       thought_required = True  # thought가 schema 필수 필드면 True

       # ── 필수 abstract (format-specific) ──
       def parse(self, llm_text) -> ParsedAction: ...
       def render_full_example(self, *, thought, action, action_input) -> str: ...
       def format_rules_anchor(self) -> str: ...
       def format_rules_field_specific(self) -> str: ...
       def constraint_reminder_call(self) -> str: ...
       def constraint_reminder_action_required(self) -> str: ...
       def failure_framing_parse_fail(self) -> str: ...
       def failure_framing_no_action(self) -> str: ...
       def static_retry_hint_no_json(self) -> str: ...
       def static_retry_hint_no_action(self) -> str: ...
       def system_user_prefixes(self) -> tuple[str, ...]: ...

       # ── 선택 override (그 plugin이 default와 달라야 할 때만) ──
       # def prefill(self) -> str: ...               # default ""
       # def provider_call_kwargs(self) -> dict: ... # default {}
       # def normalize_assistant_for_messages(self, raw) -> str: ...  # default identity
       # def render_action_input(self, action_input) -> str: ...      # default identity
       # serialize_assistant_for_history / render_assistant_from_history /
       # format_rules도 base default 사용 가능
   ```

2. `agent_cli/wire_formats/__init__.py`의 `_register_builtin_plugins()`에 등록 추가:
   ```python
   from agent_cli.wire_formats.my_format import MyFormat
   register(MyFormat())
   ```

3. `tests/test_wire_formats_<name>.py`에 동작 테스트 추가
4. 사용:
   ```bash
   agent-cli run "task" --response-format my_format
   ```

`thought_required=True`인 plugin은 추가로 `format_no_thought_retry(prior_content=…) -> Intervention` 인스턴스 메서드를 구현해야 합니다 (ABC base 외 — duck typing; loop이 `thought_required` 가드 후 호출). ReActFormat이 참고 구현입니다.

폐기는 폴더에서 파일을 지우고 `_register_builtin_plugins()`에서 등록 줄을 빼면 끝 — main code 변경 없음.

---

## 13. 스킬 시스템 (`skills/`)

### 13.1 개요

프롬프트 스킬은 특정 작업에 최적화된 재사용 가능한 프롬프트 템플릿입니다. Claude Code의 스킬 파일 포맷과 호환되도록 설계되었습니다.

### 13.2 스킬 파일 포맷 (Claude Code 호환)

```markdown
---
name: review-code
description: Review code for bugs and security
allowed-tools: [read_file]
max-turns: 5
argument-hint: "<file_path>"
---

You are a code reviewer. Read $ARGUMENTS and analyze for bugs.
```

| Frontmatter 필드 | 타입 | 설명 |
|-----------------|------|------|
| `name` | string | 슬래시 명령어 이름 |
| `description` | string | 스킬 설명 |
| `allowed-tools` | list[str] | 허용 도구 (미지정 시 전체) |
| `max-turns` | int | 최대 턴 (미지정 시 기본값) |
| `argument-hint` | string | 인자 힌트 |

### 13.3 인자 치환

| 패턴 | 설명 |
|------|------|
| `$ARGUMENTS` | 전체 인자 문자열 |
| `$0`, `$1`, ... | N번째 인자 (0-indexed) |

### 13.4 스킬 검색 경로

1. `.agent-cli/skills/*.md` (프로젝트 로컬, 최우선)
2. `~/.agent-cli/skills/*.md` (사용자 전역)
3. `agent_cli/skills/builtin/*.md` (패키지 내장, 최하위)

동일 name의 스킬이 여러 위치에 있으면 상위 우선순위가 오버라이드합니다.

패키지 내장 스킬:
- `create-skill` — 새 스킬 파일 대화형 생성
- `create-agent` — 새 에이전트 정의 파일 대화형 생성
- `plan` — 기능 요청을 작업 분해 + 의존성 + 범위 추정으로 구조화 (plan/ 저장)

### 13.5 실행 플로우

```
사용자 입력: /review-code src/auth.py
    │
    ▼
load_skills() — 호출 시점마다 디스크 재스캔, 파일 파싱
    │  └─ 캐시 없음. /create-skill로 방금 만든 스킬도 재시작 없이 즉시 인식
    ▼
스킬 매칭: "review-code" → Skill 객체
    │
    ▼
substitute_arguments() — $ARGUMENTS → "src/auth.py" 치환
    │
    ▼
run_loop(query=치환된_프롬프트, allowed_tools=["read_file"], max_turns=5)
    │  └─ loop.py의 기존 인프라 그대로 활용
    ▼
결과 반환
```

### 13.6 스킬 스택 (재귀 방지)

스킬이 `run_skill`로 다른 스킬을 호출할 수 있지만, 재귀는 방지:

```
A→B: 허용 (summarize → optimize)
A→A: 차단 (summarize → summarize)
A→B→A: 차단 (summarize → optimize → summarize)
```

방어 메커니즘 3단계:
1. **skill_stack** — `run_loop`이 `skill_stack: list[str]`를 추적. `_handle_run_skill`이 스택에 같은 이름이 있으면 에러 반환.
2. **시스템 프롬프트** — `build_skill_descriptions(exclude_names=skill_stack)`로 현재 실행 중인 스킬을 Available Skills에서 숨김. LLM이 재귀 시도 자체를 하지 않도록 유도.
3. **프롬프트 규칙** — Rule 7: "NEVER invoke yourself recursively via shell"

### 13.7 커스텀 스킬 작성

`.agent-cli/skills/my-skill.md` 파일을 생성하면 자동으로 `/my-skill` 명령어가 등록됩니다.

### 13.8 기본 내장 스킬

| 스킬 | 도구 | 설명 |
|------|------|------|
| `/review-code <file>` | read_file, shell | 코드 리뷰 (버그, 보안, 성능) |
| `/summarize <path>` | read_file, shell | 파일/디렉토리 요약 |
| `/test <file>` | read_file, write_file, shell | 유닛 테스트 생성 |
| `/optimize <path>` | read_file, shell, write_file | 코드 최적화 분석 → OptimizationToDo.md |

---

## 14. Hook 시스템 (`hooks/`)

### 14.1 개요

Python hook + shell hook 두 가지 방식의 라이프사이클 훅을 지원한다.
- **Python hook**: `.agent-cli/hooks/*.py` — context window 조작, MCP 메모리 접근 가능
- **Shell hook**: `.agent-cli/hooks.json` — 외부 명령 실행 (기존 방식, 하위 호환)
- **Skill-local shell hook**: SKILL.md frontmatter의 `hooks:` 섹션 — 해당 스킬이 실행되는 동안만 적용되는 로컬 matcher. 호출자의 hooks_config와 `merge_hooks_configs(parent, skill.hooks)`로 합쳐져서 부모 훅과 함께 발동.
- **Agent-local shell hook**: 에이전트 정의 파일(`.agent-cli/agents/*.md`) frontmatter의 `hooks:` 섹션 — 해당 에이전트로 delegate 되는 동안만 적용되는 로컬 matcher. skill과 동일한 merge 계약: `merge_hooks_configs(parent, agent.hooks)`로 부모 훅 뒤에 덧붙여 fire.
- **Delegate 전파**: `tool_delegate`가 `hooks_config`를 subagent `run_loop`에 그대로 전달. 즉 전역/프로젝트/스킬 훅은 모두 상속되고, 에이전트 자신의 overlay까지 그 위에 얹힘.

### 14.2 라이프사이클 이벤트 (11개)

| 이벤트 | 시점 | 함수명 |
|--------|------|--------|
| OnSessionStart | 세션 시작 후 | `on_session_start(ctx)` |
| PreLLMCall | LLM 호출 직전 (매 턴) | `pre_llm_call(ctx)` |
| PostLLMCall | LLM 응답 수신 후 | `post_llm_call(ctx)` |
| PreToolUse | 도구 실행 직전 | `pre_tool_use(ctx)` |
| PostToolUse | 도구 실행 직후 | `post_tool_use(ctx)` |
| OnTurnEnd | 턴 종료 후 | `on_turn_end(ctx)` |
| OnDelegateStart | delegate 실행 직전 | `on_delegate_start(ctx)` |
| OnDelegateEnd | delegate 완료 후 | `on_delegate_end(ctx)` |
| OnSkillStart | skill 실행 직전 | `on_skill_start(ctx)` |
| OnSkillEnd | skill 완료 후 | `on_skill_end(ctx)` |
| OnSessionEnd | 세션 종료 시 | `on_session_end(ctx)` |

### 14.3 Python Hook 파일 규약

```python
# .agent-cli/hooks/00_memory.py
EVENTS = ["OnSessionStart", "OnTurnEnd"]

def on_session_start(ctx):
    memories = ctx.search_memory("project context")
    if memories:
        ctx.inject_system_section("Memory", format_memories(memories))

def on_turn_end(ctx):
    ctx.store_memory([{"name": "...", "entityType": "decision", "observations": [...]}])
```

- 파일명 숫자 prefix 순서 실행 (`00_` → `10_` → `20_`)
- 프로젝트 hooks → 유저 hooks 순서
- `EVENTS` 리스트로 구독할 이벤트 선언
- 에러 발생 시 해당 hook 건너뜀 (에이전트 루프 중단 없음)

### 14.4 HookContext

hook 함수가 받는 컨텍스트 객체:
- **읽기**: `event`, `messages`, `session_dir`, `turn`, `tool_name`, `tool_input`, `tool_result`, `llm_response`
- **context 조작**: `inject_message()`, `inject_system_section()`, `remove_system_section()`
- **도구 제어** (PreToolUse): `block(reason)`, `modify_input(new_input)`
- **MCP 메모리**: `store_memory()`, `search_memory()`, `read_memory()`

### 14.5 실행 순서

```
이벤트 발생 → HookContext 생성 → Python hooks (파일명 순) → Shell hooks (hooks.json)
```

### 14.6 loop.py 통합

```
AgentLoop.run()
  ├─ _setup() → OnSessionStart
  ├─ _execute_turn()
  │   ├─ PreLLMCall → system_sections 적용
  │   ├─ _call_llm()
  │   ├─ PostLLMCall
  │   ├─ self._dispatch_tool_with_hooks()
  │   │   ├─ PreToolUse (Python) → PreToolUse (Shell)
  │   │   ├─ OnDelegateStart / OnSkillStart
  │   │   ├─ 도구 실행
  │   │   ├─ OnDelegateEnd / OnSkillEnd
  │   │   └─ PostToolUse (Python) → PostToolUse (Shell)
  │   └─ OnTurnEnd
  └─ OnSessionEnd (finally)
```

---

## 15. 설계 원칙

1. **모델은 commodity, harness가 성패를 결정한다** — 파싱 폴백, 도구 출력 압축, 퍼지 편집 등 harness 레벨 최적화가 핵심
2. **프로바이더별 최선의 방식 자동 선택** — 네이티브 tool calling > basic JSON mode > 텍스트 파싱 (strict JSON Schema는 확장성 이슈로 미사용)
3. **소형 모델 우선 설계** — 보수적 기본값, 적응형 출력 압축, 스키마 자동 변환
4. **비용 제로 보정 우선** — LLM 재호출 없이 harness에서 보정 (퍼지 매칭, 타입 변환)
5. **점진적 기능 저하** — 기능 미지원 시 에러 대신 다음 폴백으로 graceful degradation
6. **순환 의존 없는 단방향 모듈 구조** — config → compat → base → adapters → loop → main
