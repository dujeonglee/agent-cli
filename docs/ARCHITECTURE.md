# Agent-CLI 아키텍처

> 이 문서는 **지금의 구조를 그린 지도**다. 이력은 git log·릴리스 노트에, 설계 근거는 `docs/<영역>/DESIGN.md` 에 있다.
>
> - 지금의 구조만 적는다. 줄 수, 파일·테스트 개수, 버전 표기, 날짜, "종전엔 …"·"…에서 바뀌었다" 류의 이력 서술을 적지 않는다.
> - 고칠 때: 파일이 생기거나 없어지거나 이름·역할이 바뀔 때, 의존 방향이 바뀔 때, 데이터 구조·흐름이 바뀔 때. 커밋마다 고치는 문서가 아니다.
> - `tests/test_architecture_doc.py` 가 지키는 것:
>   §2 트리의 경로가 전부 있다 · `agent_cli/` 의 `.py` 와 `web/static/` 의 `app.js`·`js/*.js` 가 전부 §2 에 있다 · §2 의 줄은 200자 이하이고 줄 수 표기가 없다.
> - `tests/test_import_graph.py` 가 지키는 것: 모듈 수준 import 순환 0 · 순환을 피하는 지연 import 는 테스트의 이름 붙은 목록과 같다(§3).

---

## 1. 프로젝트 개요

Agent-CLI는 on-premise LLM을 위한 모듈형 에이전트 CLI입니다. ReAct(Reasoning + Acting) 패턴으로 LLM이 도구를 사용하여 단계적으로 작업을 수행합니다.

### 핵심 특징

- **멀티 프로바이더**: Anthropic, OpenAI 호환(vLLM, LM Studio, mlx-lm/omlx)
- **3단계 파싱 폴백**: json.loads → JSON repair → regex 추출
- **Basic JSON Mode**: OpenAI `dialect={"type":"json_object"}`, Anthropic tool calling (strict JSON Schema는 확장성 위해 미사용)
- **Hashline 편집**: CRC32 해시 기반 정밀 파일 편집 + 퍼지 매칭
- **컨텍스트 관리**: 매 LLM 호출 직전 `C×r−S−St (r=compaction_ratio 0.8 기본, St=세션상태; **max_output 예약 제거, 요청-시 클램프로 대체 — v8.53.0**)`(S=system 실측) 초과 시 LLM 요약 compaction (recursive single-call), 실패/재구성 후 미충족 시 FIFO drop으로 belt-and-braces fallback, 호출 직후 서버 실측 `usage`로 reconcile (flow 1 예방); 추정이 빗나가 서버가 400(prompt too long)을 던지면 `force_fit`으로 compact→FIFO 사후 축소 후 bounded 재시도 (flow 2 반응); history.jsonl 영속화 + compaction.json (resume용 dynamic_start_index)
- **모델 적응형**: context window, thinking budget에 따른 자동 조정

### 외부 의존성

| 패키지 | 버전 | 용도 |
|--------|------|------|
| `typer` | >=0.12.4 | CLI 프레임워크 (PEP 604 `X \| None` 옵션 타입 — v10.124.1) |
| `rich` | >=13.0 | 터미널 렌더링 (Panel, Table, Rule 등) |
| `requests` | >=2.28 | HTTP 클라이언트 (LLM API 호출) |
| `pyyaml` | >=6.0 | 스킬 frontmatter 파싱 |
| `tree-sitter` | >=0.25 | code_index 파서 코어 (`QueryCursor` — v10.124.1) |
| `tree-sitter-python` / `-javascript` / `-typescript` / `-cpp` / `-go` / `-rust` / `-java` | >=0.23 | code_index 언어 grammar |
| `tree-sitter-markdown` | >=0.3 | code_index markdown heading 인덱스 |
| `pysqlite3-binary` | >=0.5 | code_index SQLite fallback (Linux only — `--without-sqlite` 빌드된 CPython 대비) |
| `pillow` (선택, `[image]`) | >=10 | 이미지 입력(docs/vision/DESIGN.md) — 보내기 전 축소·JPEG 재인코딩에만. **v10.61.1 선택 의존성**(사내 미러에 없는 배포 실측): 크기 읽기(`image_size`, 헤더 파싱)·능력 프로브(상수 PNG)는 표준 라이브러리, 없으면 원본 그대로 보내고 5MB 초과만 거절(`ImageTooLarge` → 관찰 사유). 테스트는 dev extra 로 받는다. |

**Optional**: `agent-cli[web]` → `fastapi` / `uvicorn[standard]` / `sse-starlette`. `agent-cli[browser]` → `playwright` (**v10.39.0**, `browser` 도구; 브라우저 엔진은 설치된 Chrome 또는 `playwright install chromium`).
**Dev**: `pytest`, `pytest-asyncio`, `httpx`, `hypothesis` (property-based 테스트), `xgrammar>=0.1.20`(`accept_string`·현재 EBNF 이스케이프).

**하한 원칙 (v10.124.1)**: 핀은 우리가 쓰는 API 가 들어온 버전이다. 어떤 조합에서 깨지는 전이 의존성 때문에 직접 핀을 올리지 않는다(멀쩡한 기존 환경·사내 미러에 업그레이드를 강요한다). CI `min-deps` 잡이 Python 3.10 + `uv pip install --resolution lowest-direct` + `.github/constraints-min-deps.txt`(하한이 스스로 못 막는 전이 패키지만 — `click<8.2`·`pydantic<2.14`)로 유닛 수트를 돈다.

**시스템 패키지**: C/C++ 인덱싱의 `unifdef` 단계는 번들된 pure-Python (`_unifdef.py`) 이 기본 처리 — 설치 불필요. 시스템 `unifdef` 바이너리가 있으면 자동 우선 사용 (battle-tested C).

표준 라이브러리: json, re, dataclasses, pathlib, os, sys, zlib, textwrap, unicodedata, copy, tempfile, threading, sqlite3 (code_index — stdlib 우선, 미존재 시 `_sqlite.py` shim 이 `pysqlite3-binary` 로 폴백)

---

## 2. 디렉토리 구조

파일 하나 = 한 줄 = 지금 그 파일이 하는 일. 디렉터리 줄에는 한 구절 설명과, 있으면 설계 문서. 경로·누락·줄 길이는 `tests/test_architecture_doc.py` 가 지킨다.

```
agent_cli/
├── __init__.py              패키지 버전(`__version__`) 한 줄
├── __main__.py              `python -m agent_cli` 진입점
├── main.py                  typer CLI — `run`·`web`·`setup`·`mcp`·`knobs`·`sessions`·`stats`·`update` 명령과 `@agent`·`/skill` 디스패치
├── runtime.py               run/web 세션 조립(`SessionSpec`→stores→owners→`wire_session`)·정리, `MainLoop`, 깨우기 판정(`wake_sources`)
├── pump.py                  세션 펌프 — 큐에서 꺼내 깨우기·라우팅·런·정지 판정. 수명 정책과 표면만 호스트가 꽂는다 (docs/pump/DESIGN.md)
├── command_table.py         사용자 명령 표 하나 — 웹 `/help` 와 `run "/help"` 가 여기서 나온다
├── config.py                config.json·models.json 레지스트리 읽기·저장, 프로바이더 기본값, 방언 바인딩(`bind_user_dialect`)
├── setup.py                 첫 실행 설정 마법사 `SetupWizard` (Rich TUI — 프로바이더·연결·모델)
├── model_check.py           부팅 때 모델 이름 검증 — "없는 모델"(`ModelNotFound`)과 "확인할 수 없음"을 가른다
├── knobs.py                 세션 노브 선언 `KNOBS` 하나에서 env·CLI 플래그·web 칩 표면을 파생 (docs/knobs/DESIGN.md)
├── constants.py             공용 상수와 작은 판정 함수(`parse_duration`·`format_duration`·`open_debts`·env 노브 읽기)
├── paths.py                 `.agent-cli` 경로의 단일 소스 — 프로젝트·유저·세션·그림 보관소·스크래치 디렉터리
├── fsio.py                  파일 저장 패턴 셋의 소유자 — 원자 교체(`atomic_write_*`)·가드 append(`append_line`)
├── resource_loader.py       여러 경로에서 frontmatter 있는 .md 를 우선순위로 찾는 `ResourceLoader` (스킬·에이전트·지시사항)
├── extras.py                선택 의존성(extra) 설치 안내 — 빠진 패키지 이름을 설치 메타데이터에서 읽어 직접 댄다
├── net_errors.py            `requests` 예외 사슬 순회와 TLS 사유 한 줄 — 도구 층·공급자 층이 같이 쓴다(표준 라이브러리만 import)
├── input_queue.py           세션 입력 큐 `InputQueue` — run 과 web 이 공유하는 펌프의 큐(사용자 입력·깨우기 항목)
├── input_history.py         readline 입력 히스토리 — `ask` 답과 설정 마법사의 터미널 입력
├── item_ids.py              열린 항목 id `<접두>-<토큰>` 의 할당·해석 — 접두가 닫는 법을 말한다 (docs/ledger/DESIGN.md)
├── images.py                이미지 입력 — `ImageRef` 참조·보관본(`.agent-cli/images/`)·공급자 경계 `expand` (docs/vision/DESIGN.md)
├── memory.py                세션 메모리 `memory.jsonl` — 압축·resume 를 넘기는 노트와 꼬리 색인 (docs/session-memory/DESIGN.md)
├── jobs.py                  백그라운드 작업 `JobRegistry` — `shell` `background` 의 자식 프로세스·로그·종료 통지 (docs/jobs/DESIGN.md)
├── snapshot.py              워크스페이스 git 그림자 저장소 스냅샷 `GitSnapshot` (docs/snapshot/DESIGN.md)
├── browser.py               헤드리스 브라우저 세션 — `browser` 도구의 엔진(접근성 트리·ref·전용 스레드) (docs/browser/DESIGN.md)
├── web_sources.py           꼬리 `## Web Sources` 정보원 목록 — 패키지 기본값 + 프로젝트의 바꾼 것만 (docs/web-sources/DESIGN.md)
├── thinking_tags.py         content 안 사고 태그를 걷어내는 `strip_think_blocks` 단일 소스
├── agent_icon.py            상주 에이전트 key → 아이콘·별명(표시 전용) — 프런트와 같은 풀·해시
├── verbose.py               `--verbose` 기록기 — 모든 루프의 LLM 호출을 `<session>/verbose.jsonl` 에
├── default_models.json      패키지 기본 모델 정의 (models.json 검색 경로의 마지막)
├── default_web_sources.json 기본 정보원 목록 (`web_sources.py` 가 읽는다)
├── loop/                    루프 엔진 — `AgentLoop` 와 협력자들, 루프 종류 (docs/loop-kinds/DESIGN.md)
│   ├── __init__.py          패키지 공개 표면 재수출(`AgentLoop`·`run_loop`·`LoopConfig` …)
│   ├── kind.py              `LoopKind` 기본 틀 — 종류가 정체·포트·ctx·스코프·결과 포장을 결정, `PrivateLoop`·`Caller`·`Start`
│   ├── run.py               `run_loop(kind, start, ctx)` — `AgentLoop(...).run()` 의 함수 꼴, 종류가 부르는 엔진 이음매
│   ├── core.py              `AgentLoop` 오케스트레이터 — 런·턴 루프, 입력·메일 흡수, 응답 마감(절단·폭주·서버 오류)
│   ├── state.py             `LoopConfig`(런 시작 때 계산한 것)와 `LoopState`(협력자들이 공유하는 per-run 가변 상태)
│   ├── ports.py             루프에 주입되는 능력 포트 묶음 `LoopPorts` — 기본값 없음 (docs/wiring/DESIGN.md)
│   ├── prompt.py            `SystemPromptSvc` — 이름 붙은 섹션 리스트가 진실, 시스템 프롬프트 문자열은 그 join
│   ├── llm.py               `LLMCaller` — 스트리밍 호출·예방 압축·오버플로 재시도·압축 요약 호출
│   ├── dispatch.py          `TurnDispatcher` — 파싱된 턴의 op 순회·가드·배치·관찰 조립·recovery, `ask`·`complete` 처리
│   ├── tool_bridge.py       `ToolBridge` — 훅·도구 실행(일반·delegate)·`RunContext`·결과→관찰
│   ├── skill_invoke.py      `run_skill` op 처리 — 이름·인자 검사 뒤 `SkillLoop` 실행
│   ├── reroute.py           결과가 같다고 보장될 때만 도구 호출을 다른 도구로 바꾸는 층(`read_file` 이미지 → `view_image`)
│   └── ledger.py            열린 항목 장부 `Ledger` — 꼬리 `## Open Items` 와 화면용 사본 (docs/ledger/DESIGN.md)
├── dialects/                방언 — LLM 응답의 와이어 모양. 스펙(데이터) + 엔진 하나 (docs/dialects/PHASE5.md)
│   ├── __init__.py          방언 레지스트리·해석(`resolve_dialect`·`dialect_for_model`)·`try_foreign_parse`
│   ├── base.py              `DialectBase` 프로토콜과 `ParsedAction`·`Op`·`ParsedTurn`, history 직렬화
│   ├── spec.py              `DialectSpec` 과 축(`ArgStyle`·`NameSlot`·`Lenient`·`Prose`) — 메서드 없는 데이터
│   ├── engine.py            `Dialect` 엔진 — 스펙 하나로 렌더·파서·문법·산문·history 왕복
│   ├── grammar.py           디코딩 문법 — 와이어 모양을 서버가 강제할 EBNF 로
│   ├── specs/               방언 스펙 — 모듈 하나 = `DialectSpec` 하나
│   │   ├── __init__.py      패키지 표식(docstring)
│   │   ├── json_fc.py       `json_fc` — 산문 thought + flat op JSON 배열
│   │   ├── xml_fc.py        `xml_fc` — 태그-파라미터 function call (Qwen3-Coder XML 문법)
│   │   ├── hermes_json.py   `hermes_json` — `<tool_call>{"name", "arguments"}</tool_call>`
│   │   ├── glm_argkey.py    `glm_argkey` — `<arg_key>`/`<arg_value>` 쌍 태그 (GLM 네이티브 모양)
│   │   └── native_fc.py     `native_fc` — 서버 네이티브 함수 호출(`tools`/`tool_calls`) (docs/dialects/NATIVE.md)
│   └── recovery/            드리프트한 출력을 ops 로 되살리는 구제 기계
│       ├── __init__.py      공용 앞단 — 인용 영역(펜스·인라인 코드) 계산 `quote_spans`
│       ├── json.py          JSON 구제 — 토큰 수리·진단·op 추출 (json_fc·hermes_json)
│       └── tagged.py        태그 구제 — 변종 태그·closer·값 끝 (xml_fc·glm_argkey)
├── recovery/                회복 계층 — 감지기·회복 primitive·개입 조립·턴 기록 (docs/robust-harness/DESIGN.md)
│   ├── __init__.py          primitive·detector·observability 재수출
│   ├── primitives.py        포맷 무관 회복 primitive — 다음 user 메시지에 들어갈 글 조각
│   ├── intervention.py      `Intervention` — primitive 합성 결과(주입할 메시지 + 메타)
│   ├── common_recovery.py   방언과 무관한 개입 조립(행동 고리 넛지)
│   ├── dialect_recovery.py  방언 문구를 쓰는 개입 조립(형식 재시도·넛지)
│   ├── detectors.py         실패 감지기 — 상태 있는 `ActionLoopDetector`·`StaleGuard`, 상태 없는 `detect_*`
│   ├── recursion.py         재귀·깊이 상한 차단 시의 관찰 문구
│   ├── observability.py     턴 기록 `TurnRecorder` → `<session>/turns.jsonl`, 출력 상한 분류
│   ├── failures.py          쓸 수 없던 생성의 본문 기록 → `<session>/failures.jsonl`
│   └── stats.py             `agent-cli stats` — turns.jsonl 만 읽는 본문 없는 집계
├── providers/               LLM 프로바이더 어댑터
│   ├── __init__.py          프로바이더 self-register 와 `create_provider` 팩토리
│   ├── base.py              `LLMProvider` 프로토콜, `LLMResponse`·`TokenUsage`·`CallSettings`, 샘플링·사고 스위치
│   ├── binding.py           `ModelBinding` — 세션이 지금 쓰는 모델 하나(이름·능력·방언·예산)
│   ├── capabilities.py      `ModelCapabilities` 조회(models.json > 런타임 탐지 > 기본값), vision·문법 프로브
│   ├── http.py              HTTP 재시도·SSE 스트림 읽기·재접속·연결 오류 분류
│   ├── runaway.py           스트리밍 경로의 내용 무관 폭주 감지기 `RunawayDetector`
│   ├── openai.py            `OpenAIProvider` — `/v1/chat/completions` 호환 서버(vLLM·LM Studio·omlx)
│   └── anthropic.py         `AnthropicProvider` — Anthropic Messages API
├── context/                 컨텍스트·세션 영속화 (docs/context-compaction/DESIGN.md)
│   ├── __init__.py          `ContextManager`·`estimate_tokens`·`is_context_overflow` 재수출
│   ├── manager.py           `ContextManager` — history.jsonl·인메모리 캐시·예산 압축·서버 실측 reconcile
│   ├── render.py            record → LLM 메시지 렌더, 예산 추정, 압축 요약 입력 (무상태)
│   ├── records.py           디스크 레코드 모양의 단일 리더·분류기(ops·개입 종류)
│   ├── store.py             세션 영속화 디스크 I/O primitive(history 추가·compaction.json·fork)
│   ├── session.py           세션 디렉터리 배치·메타 `session.jsonl`·목록·resume
│   ├── session_lock.py      한 세션 한 프로세스 — `session.lock` flock
│   ├── overflow.py          프로바이더별 컨텍스트 초과 감지·`ContextOverflowError`
│   ├── token_estimator.py   토큰 추정(chars/4)과 `utf8_len`
│   └── _file_extract.py     축출된 메시지에서 도구가 만진 파일 목록 추출(`Tool.touched_paths` 위임)
├── prompts/                 모델이 읽는 글 — 시스템 프롬프트·꼬리·압축 지침 (docs/prompt-notation/DESIGN.md)
│   ├── __init__.py          패키지 표식(docstring)
│   ├── system_prompt.py     능력에 맞춘 시스템 프롬프트 섹션 조립, 지시사항 파일 읽기·쓰기, 도구 설명 교체
│   ├── session_state.py     꼬리(세션 상태 블록) — 마지막 메시지에 붙는 휘발 상태 (docs/tail/DESIGN.md)
│   ├── compaction.py        압축 요약 호출의 글 — 제목 일곱 줄, 경계 지침
│   ├── inventory.py         프롬프트 인벤토리 — 루프가 부르는 함수로 모델이 받는 글 전부를 JSON·HTML 로
│   └── inventory.html       `inventory.py --html` 이 데이터를 끼워 넣는 HTML 틀
├── tools/                   도구 — `Tool` 하위 클래스 하나가 스키마·검증·실행을 소유
│   ├── __init__.py          레지스트리 표면 재수출(`TOOLS`·`RunContext`·`ToolResult` …)
│   ├── base.py              `Tool` 기반 클래스·`RunContext`·지연 로딩 뼈대·큰 출력 저장 (docs/tool-help/DESIGN.md)
│   ├── registry.py          `TOOLS` 인스턴스화, 도구 설명 렌더, 입력 검증, 액션 추론
│   ├── result.py            `ToolResult` 데이터클래스
│   ├── virtual.py           루프가 가로채는 스키마 전용 도구 — `complete`·`wait`·`ask`·`message`·`reply`·`run_skill`
│   ├── read_file.py         `read_file` — 해시라인(`LINE#HASH`) 형식 읽기
│   ├── write_file.py        `write_file` — 파일 생성·덮어쓰기, 결과는 해시라인
│   ├── edit_file.py         `edit_file` — 해시라인 검증 편집, 단일·같은 파일 묶음이 한 적용 코어
│   ├── _diff.py             write_file·edit_file 공용 unified diff 포맷터
│   ├── _change_echo.py      write_file·edit_file 공용 변이 후 echo(diff + 바뀐 구간 새 ref)
│   ├── view_image.py        `view_image` — 이미지 파일을 모델에게
│   ├── shell.py             `shell` — 전경 실행·`background` 작업, 위험 명령 확인
│   ├── _confine.py          워크스페이스 봉쇄 — 밖을 바꾸는 쓰기·셸에 확인
│   ├── _sensitive.py        민감 경로 사전 — 자격증명 읽기에 확인
│   ├── human_note.py        확인 대화상자에서 사람이 남긴 말을 모아 도구 결과에 붙인다
│   ├── fetch.py             `fetch` — 웹 페이지를 마크다운·텍스트로, 바이너리는 파일로, 실패 원인 분류
│   ├── monitor_tool.py      `monitor` — 조건 → 보고 감시의 add·list·delete (docs/monitor/DESIGN.md)
│   ├── schedule.py          `schedule` — 이 세션의 반복 프롬프트 (docs/schedule/DESIGN.md)
│   ├── snapshot_tool.py     `snapshot` — 워크스페이스 스냅샷에서 파일 복구
│   ├── browser_tool.py      `browser` — 헤드리스 브라우저 도구 표면(엔진은 `browser.py`)
│   ├── agent_tool.py        `agent` — 스키마·의미 검증(실행은 `subagent/agents_live.py`)
│   ├── history.py           `history` — 세션 기록을 SQL 로 조회
│   ├── memory_tool.py       `memory` — 세션 메모리 add·get·update·delete
│   ├── code_index.py        `code_index` — `agent_cli.code_index` 패키지의 도구 표면
│   └── tool_help.py         `tool_help` — 지연 도구 하나의 전문을 관찰로
├── monitor/                 조건 → 보고 감시 (docs/monitor/DESIGN.md)
│   ├── __init__.py          패키지 표식(docstring)
│   ├── conditions.py        감시 조건 레지스트리 — `match`·`silence`·`command`
│   ├── registry.py          `MonitorRegistry` — 조건 평가 스레드, 설치한 주소로 보고 배달
│   └── runtime.py           프로세스 단위 monitor 레지스트리 전역(`set_`/`get_monitor_registry`)
├── schedule/                세션 소유 예약 실행 (docs/schedule/DESIGN.md)
│   ├── __init__.py          패키지 표식(docstring)
│   ├── cron.py              5필드 cron 식 엔진(의존성 0)
│   ├── registry.py          `ScheduleRegistry` — 예약 소유·발화 스레드, `schedules.json`, 놓친 발화는 질문으로
│   ├── runtime.py           프로세스 단위 schedule 레지스트리 전역
│   └── view.py              예약의 화면용 뷰(web API·SSE 공용)
├── subagent/                서브에이전트 — 일회성·상주 실행 (docs/teammate/DESIGN.md · docs/agent-unification/DESIGN.md)
│   ├── __init__.py          패키지 표식(docstring)
│   ├── runner.py            종류들이 공유하는 두 단계 — 역할 오버레이(`apply_role_overrides`)·ctx 생성
│   ├── profiles.py          에이전트 프로파일 카탈로그 로더(`agents/` 하나)
│   ├── oneshot.py           일회성 서브에이전트 `OneShotLoop` 와 호스트 `tool_delegate`(병렬 과제)
│   ├── resident_loop.py     상주 에이전트의 루프 종류 `ResidentLoop`
│   ├── agents_live.py       상주 에이전트 `AgentRegistry`·`AgentInstance` — spawn·메일함·질문 포트·회신 배달·agents.json
│   └── report.py            서브에이전트 결과 포장 `DelegateResult`·런 디렉터리 이름·본문 역추출
├── harness/                 하니스가 자기 몫으로 돌리는 일회성 작업 — 팀원이 아니다 (docs/auditor/DESIGN.md)
│   ├── __init__.py          패키지 표식(docstring)
│   ├── jobs.py              `HarnessJobs` 실행기와 `HarnessLoop`(private 종류), 프로파일은 `agents/harness/`
│   └── auditor.py           `SessionAuditor` — main 의 사용자 런마다 감사 작업 하나
├── hooks/                   Python·셸 라이프사이클 훅 (docs/hook-redesign/DESIGN.md)
│   ├── __init__.py          셸 훅 API 재수출
│   ├── events.py            이벤트 이름 상수와 이벤트 → 함수 이름 매핑
│   ├── context.py           훅 함수가 받는 `HookContext`
│   ├── loader.py            Python 훅 파일 스캔·로드(내장 먼저, 프로젝트 뒤)
│   ├── runner.py            `HookRunner` — 이벤트마다 Python(+선택적으로 셸) 훅 실행
│   ├── runtime.py           세션 단위 훅 러너 전역 — 모든 루프 종류가 같은 인스턴스
│   ├── shell.py             셸 훅(`hooks.json`) — PreToolUse·PostToolUse·PostToolUseFailure
│   └── builtin/             패키지 내장 Python 훅
│       ├── __init__.py      내장 훅 관례(docstring)
│       └── 10_snapshot.py   워크스페이스 스냅샷 훅 — 세션 시작 안내, 변경 도구 직전 체크포인트
├── skills/                  프롬프트 스킬 (Claude Code 호환 포맷)
│   ├── __init__.py          패키지 표식(docstring)
│   ├── models.py            `Skill` 데이터 모델
│   ├── loader.py            스킬 검색·파싱(`load_skills`·`model_skills`)
│   ├── executor.py          인자 치환 `substitute_arguments` 와 루프 종류 `SkillLoop`
│   └── builtin/             패키지 내장 스킬
│       ├── create-skill/    스킬 생성 메타 스킬(사용자 전용, `SKILL.md`·`references/`)
│       ├── create-agent.md  에이전트 프로파일 생성 메타 스킬(사용자 전용)
│       ├── plan.md          구현 계획 작성
│       ├── orchestrate.md   계획 + 워커 조율
│       └── windows-dump/    Windows 덤프 분류 워크플로(`SKILL.md`·`references/`)
│           └── kdump.py     덤프 분류 도구(표준 라이브러리만) — info·modules·stack·pdbcheck
├── agents/                  에이전트 프로파일(.md + frontmatter)
│   ├── __init__.py          패키지 표식(docstring)
│   ├── builtin/             패키지 내장 프로파일 — spawn·delegate 가 고른다
│   │   ├── general.md       전문가가 없는 일
│   │   ├── code-writer.md   구현
│   │   ├── code-reviewer.md 읽기 전용 리뷰
│   │   ├── code-analyst.md  읽기 전용 분석
│   │   ├── unittest-writer.md 테스트 작성
│   │   ├── log-analyst.md   읽기 전용 로그·크래시 근본원인
│   │   └── orchestrator.md  spawn 전용 조율자
│   └── harness/             하니스 작업 프로파일 — 모델이 spawn 하지 않는다
│       └── auditor.md       런 감사자
├── code_index/              tree-sitter SQLite 코드 색인 (docs/code-index/DESIGN.md)
│   ├── __init__.py          공개 API 재수출(`build`·`load_index`·`callers_of` …)
│   ├── schema.py            공유 상수와 데이터 모양(`Symbol`·`Ref`·`Scope`·`Binding`)
│   ├── builder.py           증분 색인 빌더 — 파일 분류·정의/참조 수집·SQLite 쓰기
│   ├── store.py             SQLite 색인 리더 `IndexStore`
│   ├── callgraph.py         저장된 이름 해석에서 읽는 정의 단위 호출 그래프
│   ├── slice.py             이름 하나의 소스와 주변(호출자·피호출자·타입·매크로)을 마크다운으로
│   ├── preproc.py           C/C++ 전처리 — 정규식 재작성 + unifdef 구동, 줄 번호 보존
│   ├── _unifdef.py          `unifdef -b` 의 순수 Python 부분 구현
│   ├── _sqlite.py           sqlite3 import shim(없으면 `pysqlite3`)
│   ├── resolve/             색인 테이블에서 하는 이름 해석
│   │   ├── __init__.py      `resolve_files` — 해석 실행과 `resolve_deps` 기록
│   │   ├── python.py        Python 해석기 `Resolver`
│   │   └── c.py             C 해석기 `Resolver`(스코프 사슬·이름공간)
│   └── languages/           언어별 walker
│       ├── __init__.py      `LangSpec`·`LANGUAGES`·확장자 조회
│       ├── _shared.py       walker 공용 helper(`text`·`qualify`)
│       ├── python.py        Python walker(정의·스코프)
│       ├── c.py             C walker(정의·스코프)
│       ├── cpp.py           C++ walker
│       ├── go.py            Go walker
│       ├── rust.py          Rust walker
│       ├── java.py          Java walker
│       ├── javascript.py    JavaScript walker
│       ├── typescript.py    TypeScript walker(javascript helper 재사용)
│       └── markdown.py      Markdown 제목 → `section` 심볼
├── mcp/                     MCP(Model Context Protocol) 통합 (docs/mcp-integration/DESIGN.md)
│   ├── __init__.py          패키지 표식(docstring)
│   ├── config.py            `.agent-cli/mcp.json` 로더
│   ├── client.py            `McpClientManager` — stdio·SSE 서버 연결과 동기 래퍼
│   ├── adapter.py           MCP 도구를 `Tool` 로 감싸 `TOOLS` 에 등록
│   └── wizard.py            `agent-cli mcp` — 서버 등록·진단(저장 전 연결 시험)
├── render/                  렌더러 — 루프가 사용자를 보는 창(출력과 입력)
│   ├── __init__.py          활성 렌더러 전역과 `render_*` 함수 표면
│   ├── base.py              `Renderer` 기본 인터페이스와 `ConfirmOption`
│   ├── minimal.py           `MinimalRenderer` — 상자 없는 들여쓰기 터미널 렌더러
│   ├── web.py               `WebRenderer` — 렌더러 이벤트를 SSE 로, 입력은 `POST /api/input` 에서
│   └── web_hub.py           `EventHub` — SSE 구독자·재접속 재생 버퍼·스티키·뷰어 별명
└── web/                     `agent-cli web` 서버와 정적 UI (선택 의존성)
    ├── __init__.py          패키지 표식(docstring)
    ├── server.py            FastAPI 앱 `create_app` 과 `WebServer`(렌더러·워커 스레드·API 상태)
    ├── surface.py           web 의 펌프 표면 `WebSurface`
    ├── slash.py             웹 슬래시 명령(`/help`·`/sh`·`/compact`)과 디스패치 출력 어댑터
    ├── directives.py        지시사항 편집기의 ✨ 생성 로직
    ├── inspector.py         모델 시점 틀 — 시작 시 시스템 프롬프트 캡처
    ├── instance_file.py     세션별 `web.json`·`status.json` 쓰기·읽기·지우기
    ├── idle.py              `--idle-timeout` 자기 종료 판정 `IdleMonitor`
    └── static/              Vanilla JS 프런트엔드 — 빌드 없는 ES 모듈 (docs/web-modules/DESIGN.md)
        ├── index.html       페이지 뼈대와 첫 페인트 전 테마 적용
        ├── style.css        전체 스타일과 테마
        ├── app.js           진입 모듈 — 모듈 적재 순서만
        ├── fonts/           Jetendard 웹폰트(woff2)와 `OFL.txt`
        └── js/
            ├── boot.js          주소창 `?token=` 걷기, 인증 없을 때 안내
            ├── dom.js           공유 DOM 참조와 `el`·`elHtml`
            ├── state.js         여러 모듈이 대입하는 세션 전역 상태 `session`
            ├── markdown.js      HTML 이스케이프·최소 마크다운·diff 색칠·`imageUrl` (순수 모듈)
            ├── scroll.js        따라가기 — 자동 스크롤·새 줄 점프 버튼
            ├── sse.js           `EventSource` 하나(`es`)와 연결 상태, 클립보드 helper
            ├── copy.js          카드·타임라인 마크다운 복사
            ├── attach.js        📎 이미지 첨부(버튼·붙여넣기·끌어놓기)와 업로드 칩
            ├── ledger.js        📋 열린 항목 띠
            ├── stuck.js         🔁 제자리 고리 띠
            ├── jobs.js          🧵 백그라운드 작업 칩과 ■ 멈춤
            ├── channels.js      채널·트레이 상태, 채널 필터·점프 — 순환 묶음의 진입점
            ├── timeline.js      카드 시각, 접히는 그룹, 타임라인 추가, `scope_*` 배선
            ├── frames.js        모델 시점(`ctx_view`)과 틀(맨 위 카드·맨 아래 띠)
            ├── progress.js      스트림 무진전 표시와 생성 중 표시
            ├── thumbs.js        사용자 말풍선과 이미지 썸네일 줄
            ├── cards.js         한 줄 리듬 `makeRow`·도구 칩·스텝 카드
            ├── input.js         입력 — Send/Stop, POST, 확인 대기, 입력 바인딩
            ├── share.js         🔗 공유 링크·토큰 사용량·대화 카드 SSE 핸들러
            ├── runs.js          에이전트 런 블록과 채널 입력 라우팅
            ├── roster.js        identity·뷰어 로스터·별명·대기열
            ├── directives.js    📝 지시사항 편집기 서랍
            ├── workspace.js     📁 워크스페이스 파일 서랍(다운로드·업로드)
            ├── theme.js         🎨 테마 메뉴
            ├── knobs_client.js  노브 클라이언트(`api/knobs`)
            ├── knob_chips.js    서버 노브 표에서 칩 렌더
            ├── knob_popup.js    노브 칩 팝업 토글
            ├── mcp.js           🔌 MCP 상태 칩(읽기 전용)
            ├── presence.js      탭 존재 비콘(BroadcastChannel)
            ├── schedule.js      ⏰ 예약 서랍·배지·놓친 예약 카드
            └── web_sources.js   🌐 정보원 서랍

pyproject.toml               패키지 메타데이터·의존성 하한·extra
agent-cli.py                 하위 호환 래퍼 — `agent_cli.main.app` 실행
```

---

## 3. 모듈 의존성 그래프

여기서 **모듈 수준 import** 는 모듈이 import 될 때 실행되는 import(모듈·클래스 본문, 모듈 수준 `if`/`try` 안,
`if TYPE_CHECKING:` 블록 제외), **지연 import** 는 함수 본문 안의 import 다. 정의와 계산은 `tests/import_graph.py`.

### 3.1 층

최상위 패키지·모듈을 모듈 수준 import 방향으로 쌓은 것이다. 각 항목은 아래 층의 것만 모듈 수준에서 import 한다
(같은 층끼리는 서로 import 하지 않는다). 위층으로 가는 import 는 전부 지연 import 다(§3.2).

| 층 | 항목 |
|----|------|
| 6 | `runtime` · `subagent/` · `skills/` · `harness/` |
| 5 | `loop/` · `__main__` |
| 4 | `main` · `recovery/` · `mcp/` · `browser` |
| 3 | `context/` · `prompts/` · `providers/` · `tools/` (한 묶음) · `web/` |
| 2 | `render/` · `pump` · `setup` |
| 1 | `config` · `dialects/` · `hooks/` · `images` · `memory` · `monitor/` · `schedule/` · `verbose` · `input_queue` · `input_history` |
| 0 | `paths` · `fsio` · `constants` · `item_ids` · `thinking_tags` · `net_errors` · `jobs` · `snapshot` · `code_index/` · `knobs` · `command_table` · `extras` · `model_check` · `resource_loader` · `web_sources` · `agent_icon` · `agents/` |

`context/`·`prompts/`·`providers/`·`tools/` 는 패키지 단위로 서로를 모듈 수준에서 import 한다(모듈 단위로는 순환이 없다).
그래서 한 층의 한 묶음으로 적는다. `main` 은 명령 함수 안에서 지연 import 하는 것이 많아(`runtime`·`loop/` 등) 층이 낮게 나온다.

### 3.2 규칙

- **모듈 수준 import 순환은 0 이다.** `tests/test_import_graph.py::test_no_module_level_import_cycle` 이 지킨다.
- **순환을 피하려고 지연인 import** — 대상 모듈이 모듈 수준 import 만으로 출발 모듈에 닿는 간선 — 는 테스트 안의 이름 붙은 목록
  `CYCLE_AVOIDING_LAZY` 와 정확히 같아야 한다(`test_cycle_avoiding_lazy_imports_are_the_named_list`). 간선 목록 자체는 테스트가 주인이다.
  세 무리가 있다:
  - **루프 엔진이 세션·구체 종류를 올려다본다** — `loop.core`·`loop.dispatch`·`loop.kind`·`loop.tool_bridge` → `runtime`
    (세션 자원 `browser_resource`·`skills_resource`·`snapshot_resource`, 깨우기 판정 `wake_sources`·`arrived_mail`·`harness_work_running`,
    `release_owner`, `AgentRuntime`)·`subagent.agents_live`(`main_registry`·`tool_agent`·메일 레코드)·`subagent.oneshot`(`tool_delegate`),
    `loop.skill_invoke` → `skills`(`load_skills`·`SkillLoop`).
  - **아래층이 도구 목록을 읽는다** — `context._file_extract`·`context.render`·`dialects.base` → `tools`(`TOOLS`).
  - **낱개** — `images` → `tools`(`display_path`), `prompts.system_prompt` → `skills`(`load_skills`),
    `providers.capabilities` → `providers.anthropic`, `render.minimal` → `context`(`estimate_tokens`),
    `render.web` → `web`(`write_status_file`), `tools.tool_help` → `tools.registry`(`effective_tool_names`).
- 그 밖의 지연 import 는 순환과 무관하고 가드하지 않는다. 출발 모듈로는 `main`·`runtime`·`prompts.inventory` 에 가장 많다.
  이유는 자리마다 다르다 — 예컨대 `main` 의 `web` 명령은 `agent_cli.web.server`(fastapi 선택 의존성)를 명령 안에서 import 해,
  없으면 그 명령만 안내와 함께 실패한다.
- 실제 콜드 스타트 순서(진입점 하나만 먼저 import 했을 때 터지는지)는 `tests/test_import_cycles.py` 가 서브프로세스로 본다.

---

## 4. 핵심 데이터 구조

### 4.1 LLM 응답 (`providers/base.py`)

```python
@dataclass
class TokenUsage:
    input_tokens: int                     # 캐시를 뺀 입력
    output_tokens: int                    # 추론 포함
    prompt_eval_ns / eval_ns / ttft_ns    # 클라이언트 측정 (스트리밍)
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    reasoning_tokens: int = 0             # v10.128.0 — output_tokens 안에 든 몫
    total_input_tokens (property)         # input + cache_creation + cache_read

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
- **Anthropic**: `content[].type == "thinking"` 블록 + 스트리밍 `thinking_delta`
- **OpenAI 호환**: `choice.message.reasoning_content` (vLLM 컨벤션)
- 위 채널이 없으면 `""` (plain OpenAI Chat Completions 등 — graceful)

토큰 칸 나눔 (v10.128.0): OpenAI 경로는 `providers/openai._usage_counts` 한 함수로 usage 를 읽는다(스트리밍 매퍼·비스트리밍 공용). `prompt_tokens`(캐시 포함) − `prompt_tokens_details.cached_tokens` → `input_tokens`, 적중분 → `cache_read_input_tokens`; `completion_tokens_details.reasoning_tokens` → `reasoning_tokens`(`output_tokens` 에서 빼지 않는다). 없음·`null`·정수 아님·음수는 0, 각자 합계로 자른다. 그래서 `total_input_tokens` 는 서버의 `prompt_tokens` 그대로 — 예산 재조정·ctx%·압축 트리거는 적중과 무관하고, 바뀌는 것은 `in_speed`(캐시 아닌 토큰 기준의 참값)와 `cache_read`·`reasoning` 칸뿐. 서버가 주는 시간 필드(`time_to_first_token` 등)는 쓰지 않는다 — 시간은 클라이언트 측정 하나. Anthropic 경로는 추론 수를 따로 주지 않아 0.
- `<think>...</think>` 태그가 content 안에 있는 경우는 별도 — 각 플러그인의 stage 0(`strip_thinking` — thinking_tags 단일 소스)가 `ParsedTurn.thinking`으로 분리 추출

**소비처 (v1):** verbose 모드의 `render_thinking` 디버그 출력 *전용*. recovery 레이어(`format_no_*_retry`, `recovery/primitives.py`)는 thinking을 *읽지 않음* — primitive contract가 channel-agnostic이어야 누더기를 막기 때문 (`docs/robust-harness/DESIGN.md` §2.2).

### 4.2 모델 능력치 (`providers/capabilities.py`)

```python
@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int               # 컨텍스트 윈도우 크기 (토큰)
    max_output_tokens: int            # 최대 출력 토큰
    supports_structured_output: bool  # basic JSON mode 가능 (OpenAI dialect / Anthropic tool calling)
    supports_thinking: bool           # thinking/reasoning 지원 (단독 게이트)
    supports_vision: bool | None      # 이미지를 보는가 (v10.59.0). None = 미확인 — 첫 이미지에서 한 번 프로브
    supports_strict_schema: bool      # (dormant) strict JSON Schema 표식 — 현재 어떤 provider도 이 플래그로 동작 분기 안 함. 향후 opt-in strict schema 재도입 시 사용 예정.
```

> **thinking_format 필드 제거 (v8.19.0):** 예전엔 감지된 thinking 태그 스타일(`"think"`/`"reasoning"`)을 엔트리에 저장했으나, 어떤 요청도 이 값으로 형성되지 않는 순수 표시용 메타였다. `<think>` 계열 태그 감지는 여전히 `supports_thinking` 판정에만 쓰이고(`_detect_thinking` → bool), strip 경로는 `thinking_tags` 단일 vocab 을 그대로 사용한다. 구 models.json 의 `thinking_format` 키는 로더가 무시(무해).
>
> **thinking_budget 필드 제거 (v8.21.0):** 정적 per-model 예산도 제거하고 **`supports_thinking` 단독 게이트**로 전환. 사고 예산은 한때 런타임 `reasoning_effort` 가 결정했고(Anthropic effort→budget_tokens 번역, OpenAI `reasoning_effort` 필드. 오버라이드가 없으면 사고 지원 모델은 **기본 medium**(Anthropic budget 16384 / OpenAI effort "medium")으로 동작. 구 models.json 의 `thinking_budget` 키는 로더가 무시(무해).

> **supports_vision (v10.59.0, docs/vision/DESIGN.md):** 등록 엔트리에 키가 없으면 `None`(미확인). `vision_support(caps, transport_factory, base_url, model, api_key)` 가 등록값을 우선하고, 없으면 **프로세스당 한 번** 프로브해 `_vision_probed` 에 기억한다 — 사용자가 적은 엔트리는 파일에 다시 쓰지 않는다(supports_grammar 와 같은 정책). 프로브는 왼쪽 green·오른쪽 blue 128×64 PNG 를 보내 두 색을 **그 순서로** 답하는지 본다: "200 이면 참" 은 조각을 조용히 버리는 서버(omlx 의 tool 메시지 실측)에서 거짓 양성이라서다. 어떤 실패도 False. 호출 시점은 둘 — 공급자가 이미지를 가진 요청을 처음 보낼 때(`openai.py`/`anthropic.py`), `run --image` 부팅(`_require_vision`, 못 보면 즉시 종료). 감지(`_detect_capabilities`)는 4번째 프로브로 넣어 자동 등록 엔트리에는 값이 적힌다.

능력치 조회 우선순위:
1. `models.json` 정적 설정 (최우선)
2. 런타임 API 감지 (공유 `_detect_capabilities(model, transport)` — OpenAI 는 `/chat/completions`, Anthropic 은 `/messages` transport; 둘 다 `_ProbeTransport` 공통 메타(`max_model_len`)+overflow probe+thinking+문법+비전 감지). 감지 시 `max_output = context // 4`; context < 16K(`MIN_CONTEXT_WINDOW`)면 `UnsupportedModelError`로 reject
3. 기본값 `DEFAULT_CAPABILITIES` (context_window=`DEFAULT_CONTEXT_WINDOW`=256K·max_output_tokens=2048, 모든 기능 비활성 — 조회·감지 모두 실패했을 때). 창은 **일부러 낙관적**이다: 옛 4096 은 `oversized_cap`(= 창/10)을 409 토큰으로 만들어 거의 모든 관찰을 잘랐고 `MIN_CONTEXT_WINDOW`(16K) 와도 모순이었다. 실제 창이 더 작으면 첫 호출이 서버에서 거절되고 `ContextManager.force_fit`(flow 2)이 줄여 재시도한다 — 몇 번의 왕복이 망가진 세션보다 낫다. 출력 상한은 보수적으로 둔다(너무 큰 요청은 복구 경로 없이 거절되는 서버가 많다)

**런타임 감지 세부 (OpenAI 호환·Anthropic 공통, v10.114.0):** 프로브 전송 기반 클래스 `_ProbeTransport.context_window()` 가 3-tier로 context window 결정 — 서브클래스는 끝점(`/chat/completions`·`/messages`)·인증 헤더(Bearer · `anthropic-version`+`x-api-key`)·본문 모양만 정한다. GET `/models` 는 인증 헤더만(Content-Type 없음, timeout 10s), POST 는 `Content-Type: application/json` 을 더한다.
1. `_metadata_window()` — `/models` 메타데이터에서 이 모델 항목의 `max_model_len`(vLLM) → `context_length` 순, 양의 int — 있으면 그대로 (가장 쌈·정확). 실 Anthropic 은 이 필드가 없어 다음 단으로 간다.
2. **overflow probe** (`_overflow_window()`) — 메타데이터에 없는 서버(omlx 등)는 의도적으로 상한 초과 prompt(`"word "×2M` ≈ 1.5M 토큰)를 보내 400을 유발하고 `parse_overflow_amounts`로 응답의 상한 숫자를 추출 (omlx: `exceeds max context window of 262144 tokens`). 상한 초과 prompt는 **토크나이즈 직후 즉시 거부**되어 eval/생성이 없으므로 서버 점유 없음(실서버 검증 2026-05-30) — 그래서 경계로 수렴하는 binary search는 **안 함**(상한 *이하* probe는 full prompt-eval을 유발해 서버를 점유시킴).
3. `_DEFAULT_CONTEXT_FALLBACK` = **128K**(`131072`) — 메타데이터·probe 모두 숫자를 못 주면. 보수적/under-set이라 자체적으로 400을 유발하지 않고, 실제가 더 작으면 flow 2 런타임 복구가 교정. (이전 4096 기본값을 대체 — 4096은 256K 서버에서 컨텍스트의 1.5%만 쓰는 심각한 낭비였음.)

모든 첫-실행 probe(thinking / context overflow)는 `constants.DETECTION_PROBE_TIMEOUT`(60s) 공유 — cold-load를 감내하는 여유값이며 사용자 셸 명령용 `SHELL_COMMAND_TIMEOUT`(30s)과 구분.

### 4.3 파서 결과 — `ParsedAction` (`dialects/base.py`)

모든 dialect plugin이 반환하는 boundary 데이터타입 — loop 은 plugin 과 무관하게 이 타입만 소비.

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

### 4.4 도구 추상화 표면 (`tools/base.py` + `tools/registry.py`)

각 도구는 `Tool` ABC 서브클래스로, 스키마(`name`/`description`/`parameters`)·dispatch(`_run`)·wire-key prefix 를 한 곳에 응집한다 (이전의 중앙 `ToolSchema` dataclass + `__init__.TOOLS` 함수 dict 를 대체).

```python
class Tool(ABC):
    name: str
    description: str
    parameters: dict                      # JSON Schema
    @property
    def key_prefix(self) -> str: ...       # "{name}_"
    def strip_prefix(self, args): ...      # wire → 표준 키 (run()이 dispatch 직전 적용)
    def add_prefix(self, args): ...        # 표준 → wire (inline 가이드 예시용, strip의 역, 멱등)
    def claims(self, action_input): ...    # 키 prefix 로 소유 판정 (action 누락 추론)
    def run(self, args, *, ctx=None):      # strip_prefix → _run (ctx=RunContext|None)
    @abstractmethod
    def _run(self, args, *, ctx=None) -> ToolResult: ...
```

**per-call 루프 컨텍스트 `RunContext`** (frozen dataclass, `tools/base.py`): 루프가 도구 호출마다 아는 값들(`session_dir`·`oversized_cap`·`tools_available`)을 **하나의 객체**로 묶어 두 도구 표면(`run`/`_run` 실행 + `render_oversized` 렌더)에 넘긴다 — 새 per-call 값이 생겨도 필드 하나 추가일 뿐 13+7 개 시그니처를 다시 안 고친다(이전엔 `session_dir` 만 관통·render 는 `cap`/`tools_available`/`session_dir` 3개 loose arg 로 비대칭). frozen = 병렬 delegate 루프가 같은 인스턴스를 공유해도 안전(그래서 공유 `TOOLS` 싱글턴에 저장 금지·per-call 전달). **범위 규율**(god-object 방지): per-call 루프 컨텍스트만 — per-result 데이터(특정 결과의 `body`/`tokens`)는 `render_oversized` 의 명시 인자로 유지, 무관 기계류(parser/provider/history)는 넣지 않음. loop `_run_ctx()` 가 단일 생성점(두 seam 이 "이 호출의 컨텍스트"에서 어긋나지 않게).

**Wire-key prefix** (★ Step 3 완료로 **모든 builtin 도구가 flat-native** — 어떤 builtin 도 prefix 안 씀; MCP 도 prefix-less. 아래 dropped-action 복구 메커니즘은 **미래 prefixed 도구/포맷용 latent seam** — Step 4 에서 "삭제 대신 seam 보존" 결정): action_input 의 최상위 키를 `{tool}_{param}` 으로 네임스페이스한다 (예시는 가상의 prefixed 도구 `xtool_param`; 중첩 키 등은 그대로). 모델이 `## Action` 의 tool 이름을 누락해도(parse_stage 3) 키 모양으로 도구를 복원할 수 있다 — loop 이 parse 직후 (`dialect.action_required=False` 일 때만) `registry.infer_action(action_input)` 을 호출, 각 `Tool.claims`(prefix 매칭)가 투표해 **정확히 1개**가 소유하면 그 도구로 보정(0/2+는 NO_ACTION recovery로; `action_required=True` plugin 은 infer 를 건너뛰고 바로 NO_ACTION). 이 복구의 전제는 파서가 action 무효 시에도 action_input 을 보존하는 것(DialectBase.parse 계약). 보정에 성공하면 `_append_observation` 이 next-turn prior(messages)와 history record 를 **보정된 wire shape** 으로 재기록한다 — raw drift 를 prior 로 다시 먹이면 mimicry 가 강화되기 때문(다음 턴이, 또는 resume 시 복원된 prior 가 "action 이름을 빠뜨려도 된다"를 학습). 보정 자체는 `TurnRecorder`(parse_stage=3 + `action_inferred` primitive)로 추적되므로 형식 실패 분석 신호는 보존된다. prefix 는 **wire 표면에만** 존재: `Tool.run()` 이 dispatch 직전 `strip_prefix` 로 표준 키로 되돌려 `tool_*` 함수·virtual 처리·validate·기존 dispatch 가 전부 표준 키를 받는다(prefix 없는 키는 no-op → 모델이 표준 키를 보내도 동작). 키 prefix 가 변별을 구조적으로 보장하므로 claims 충돌(`{content, edits}` 류)이 원천 소멸 — 각 도구는 `name` 만 정하면 prefix/strip/claims 가 자동(override 0). **실측 근거**: omlx 27B/35B 에서 prefix 키 compliance 60/60(std-leak 0) — 표준 키와 동일하게 따름.

```text
# 실제 도구 (각 모듈에 Tool 서브클래스): read_file, write_file, edit_file, shell,
#   code_index, history, fetch, memory, agent (5.0.0 — 구 delegate+teammate 통합)
# 가상 도구 (tools/virtual.py — loop이 인터셉트, 표준 키 유지, prefix/추론 대상 아님):
#   complete, ask, run_skill
# schedule — native (v10.12.0): 스케줄러가 이 프로세스 안에 있어 항상 등록된다.
#   종전엔 agent-board 스케줄러의 파일 계약 클라이언트였고 env
#   AGENT_CLI_SCHEDULER=1 일 때만 등록됐다 (계약·게이트 모두 삭제).
# registry.py가 17개 Tool 인스턴스를 수집 → TOOLS(= TOOL_SCHEMAS alias).
#   인스턴스가 옛 ToolSchema와 같은 .name/.description/.parameters를 노출하므로
#   schema 소비처(system prompt, input validation, MCP adapter)는 무변경.
# _ALWAYS_INCLUDE = ("complete",)
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

`ctx.get_messages()` 반환: history.jsonl의 마지막 N개를 자연어 변환 (assistant turn은 ctx의 dialect plugin이 변환 — §5.4 참조)

```
[system]   Role (main/서브에이전트/skill별 상이)
           Task Guidelines + Format Rules (thought에 목적+이유 필수)
           Available Tools / Skills / Agents
           DIRECTIVE.md / Environment
           Context Recovery Guide (history 조회 안내 — 그 도구가 노출된 루프만, v10.11.2)

[messages] user: "hooks.py 분석해줘"
           assistant: hooks.py를 분석하기 위해 파일을 읽겠다. → read_file(hooks.py)
           user: [read_file] hooks.py\n(전문)
           assistant: 분석이 완료되었다. hooks.py는 3개의 hook 타입을 지원...
```

- Scratchpad 별도 inject 없음. messages만 (호출별 압축 목표치 자동 계산, 목표치 초과 시 compaction → 그 외 FIFO drop)
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
    │         │
    │         ├─ [complete] → ctx.add(structured dict) → return answer
    │         │
    │         ├─ [run_skill] → 내부 AgentLoop (별도 skill subdir)
    │         │
    │         └─ [도구] → execute → ctx.add(assistant + observation)
    │
    └─ _restore_signal_handler()
```

**Graceful Interrupt — turn 경계에서 멈춤. 두 surface 공통:**
- 공통 신호 = `stop_event` (threading.Event). `_should_continue()` 가 매 turn
  경계에서 `stop_event.is_set()` 을 평가 → set 이면 `_interrupted=True` →
  `_on_interrupt()`. stop 이 `run_loop` 진입 *전*에 와도 첫 체크에서 0-turn 으로 멈춤.
- **CLI (Ctrl+C, main thread)**: `_install_signal_handler` → 1st `_interrupted`
  flag + `stop_event.set()` (현재 스텝 완료 후 탈출), 2nd `KeyboardInterrupt`
  즉시 (기본 핸들러 복원). chat/skill/agent 모두.
- **web (Stop 버튼, worker thread)**: worker 가 turn 마다 `stop_event` 생성 →
  `run_loop(stop_event=…)` 전달 + `server.set_stop_handle()` 등록. `POST /api/stop`
  → `server.trigger_stop()` → `stop_event.set()`. worker thread 라 signal handler 는
  skip 되고 `stop_event` 경로만 작동. chat(`run_loop`)·`/skill`(→`execute_skill`)·
  `@agent`(→`tool_delegate`→delegate worker `run_loop`, 병렬 worker 가 같은 Event
  공유) 모두 같은 `stop_event` 가 전파되어 turn 경계에서 멈춤.
- **인터럽트 기록 + 렌더**: `_on_interrupt` 이 `{role:user, tool:"interrupt",
  success:False, content:INTERRUPT_NOTICE}` 를 ctx 에 add → `[interrupt] …`
  observation 으로 렌더. user-role bare 메시지가 아니라 observation 이므로 다음 실제
  user 입력과 role 이 겹치지 않고, `recent_exchanges` 가 `tool` 필드로 자동 제외.
  history.jsonl 영속화. (레거시 prefix `"⚡ User interrupted."` 는 옛 세션 하위호환)
  사용자 표시는 **`console.print` 직접 호출이 아니라 `render_step("observation", …)`**
  로 — CLI 는 console, web 은 SSE 로 가서 web 서버 터미널에 노이즈가 새지 않음
  (top-level 만 렌더, nested skill 인터럽트는 부모가 표시).

**run 모드 Ctrl+C:** signal handler 미설치, `KeyboardInterrupt` 즉시 발생 → `try/except`로 세션 저장 후 종료

#### 중첩 렌더링: `push_depth` / `pop_depth` + 그룹 블록

스킬/agent run 실행 시 출력을 시각적으로 감싸기 위해 `group_start`/`group_end`와
depth 기반 prefix(`│ `)를 사용. 병렬 run 은 worker별 capture 후 Live 패널로
실시간 상태 표시, 완료 후 block replay.

| 시점 | 호출 | 출력 |
|------|------|------|
| 스킬/run 시작 | `render_group_start(label, icon)` | `┌─ 🪄 skill:plan` |
| 내부 턴 | `push_depth` 상태에서 `_p()` | `│ 💭 thought...` |
| 스킬/run 종료 | `render_group_end(label, success, dur)` | `└─ ✓ skill:plan (5.2s)` |

### 5.2 프로바이더별 도구 호출 방식

모든 프로바이더가 **ReAct 텍스트 파싱**만 씁니다. 네이티브 tool calling API (Anthropic `tool_use`, OpenAI `function calling`)는 **사용하지 않습니다** — 프로바이더 편차 제거와 구현 단순성을 위한 선택. 따라서 `supports_tool_calling` 같은 플래그는 존재하지 않고, 모든 분기는 JSON 출력 여부 (`supports_structured_output`) 하나로 수렴합니다.

```
              ┌─ supports_structured_output=True ─┐
              │                                    │
        ┌─────┴──────┐                     ┌──────┴──────┐
        │ Anthropic  │                     │ OpenAI      │
        │ tool       │                     │ response_   │
        │ calling    │                     │ format      │
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

### 5.3 단계적 파싱 폴백 (`dialects/engine.py` JSON_NATIVE 경로 + `dialects/recovery/json.py`)

```
LLM 텍스트 응답
    │
    ▼
Stage 0: thinking 격리 (DialectBase.strip_thinking — thinking_tags 단일 소스)
    │
    ▼
legacy 헤더(`## Action`) 관용 경로 (stage 2 drift) ── 구 md_array emission
    │
    ▼ 헤더 없음
캐노니컬: 첫 [/{ 앞 산문 = thought, 이후 = op JSON
    ├─ strict 파스 성공 → ParsedTurn (parse_stage=1)
    ▼ 실패
_extract_op_json 수리 아스널 (전부 bail-if-invalid, 성공 시 parse_stage=2):
    anon-객체 unwrap · 재-오픈 배열 병합 · 미닫힘 괄호 EOF 닫기 ·
    여분 closer 드롭 · 따옴표 수리 · under-escape 배증 · strict=False 재파스
    ▼ 전부 실패
"action" 흔적 있으면 parse_stage=0 (NO_JSON 진단), 아니면 thought-only (NO_ACTION)
```

수리 helper 는 **`dialects/recovery/` 패키지**에 산다 (Phase 5 S1 —
종전엔 plugin 모듈 안; PHASE5.md §4.4). 구제는 한 역할이지만 기계는 둘(json·
tagged)이고 코드 공유가 0 이라 파일을 합치지 않는다. plugin 은 자기 모양만
소유하고 구제는 `ArgStyle` 로 고른다 — S2 의 엔진이 그 선택을 맡는다.


(react 의 형제-키 hoist/`_normalize_action_input` 기계는 v7.0.0 에서 react 와 함께 제거 — json_fc 는 flat op 이라 해당 드리프트 class 자체가 없음.)

### 5.4 컨텍스트 관리 (`context/manager.py`)

> 상세 설계: `docs/context-redesign/DESIGN.md`, `docs/context-compaction/DESIGN.md`

> **이미지 (v10.59.0, `docs/vision/DESIGN.md`):** 레코드·요청 메시지의 `content` 는 끝까지 문자열이다. 이미지는 user 레코드(질의·관찰)의 별도 필드 `images: [ImageRef]`(경로·크기·sha256, base64 없음)로 실리고 `content` 에는 자리표시 줄 `[image: path (W×H)]` 이 들어간다 — 요약기·텍스트 전용 모델·파일 소실 어디서나 같은 레코드가 읽힌다. `render_history_message` 가 참조를 요청 메시지에 옮기고(서버 파싱 방언은 `tool` 메시지 뒤 자리표시 user 메시지에), `get_messages` 끝에서 `images.keep_recent` 가 최근 N 장(`AGENT_CLI_IMAGE_KEEP`, 기본 6, 0=무제한)만 남긴다 — 이미지는 요약으로 줄지 않고 매 요청 다시 가므로. 파일은 **공급자 경계**(`images.expand`)에서만 읽는다. 토큰: 이미지는 픽셀/1000(실측 ≈1K px/token)으로 글과 별도로 세고(`_estimate_message_tokens`·`_scaled_tokens`), `request_bytes` 는 글만이며 `reconcile_actual_tokens(image_tokens=)` 가 실측에서 이미지 몫을 뺀 뒤 비율을 맞춘다. 요약기 호출은 `strip_images`.

#### 2-Tier: Compaction (LLM 요약) → FIFO Fallback

> 두 흐름이 있다. **flow 0 (창이 곧 트리거, v10.11.0)** — 기본 r=1.0; 요청 클램프 `max_tokens = C − 프롬프트 − 512` 에 닿아 `length` 로 끝난 생성(`classify_output_cap` → `context_clamp`)은 `core._on_output_truncated` 가 `compact_now()` 로 그 자리에서 압축하고 `CONTEXT_CLAMP_NOTICE` 로 재시도시킨다. **flow 1 (예방)** — 매 LLM 호출 *직전* `ensure_within(C×r−S−St−(1024+512) (r=compaction_ratio 1.0 기본, St=세션상태, 끝 항은 요청의 최소 출력 몫 — 1.0 에서 창을 넘는 요청 방지; **max_output 예약 제거, 요청-시 클램프로 대체 — v8.53.0**))`, 호출 *직후* 서버 실측으로 reconcile. **트리거는 "캐시 > 목표치" 하나뿐** — 별도 90% 임계는 없다(v9.25.1: 잔재 `_COMPACTION_THRESHOLD_RATIO` 제거, `fallback_used` 는 호출자가 넘긴 목표치 기준으로 기록, 세션 상태 꼬리의 `context:` 줄은 **v10.9.0 에서 제거** — 퍼센트를 본 모델이 목표치 바로 아래서 멈춰 압축이 영영 안 돌았다(67qcmb, 2시간); 꼬리는 턴 카운터만, 안내 단계는 80% 하나).
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
    │        2. 그 외 → _compact() (Split→oldest 절반 evict→단일호출 요약(recursive;
    │           v10.123.0: 경계 모양 = 본 호출 요청을 마지막 축출 메시지까지 + 지침,
    │           실패·창 초과면 전사 모양으로 한 번 — DESIGN §8.11)
    │           →_file_extract path dedup→[system][summary≤8K][file_list][retained]
    │           →compaction.json atomic write)
    │        3. Belt-and-braces: 여전히 > target 이면 _evict_fifo(target)
    │     self.messages = ctx.get_messages()       ← 축소 반영
    │
    ├─ provider.call(...)
    │
    └─ [호출 직후·성공] flow 1 reconcile: ctx.reconcile_actual_tokens(usage.total_input_tokens, S)
          usage.total_input_tokens = input + cache_creation + cache_read  ← 서버 ground truth (TokenUsage property)
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

> **전제 — 분류는 타입으로, 발화는 HTTP 상태로 게이트**: omlx 400 의 상한 정보는 **응답 BODY**(`...exceeds max context window of N tokens`)에 있다. `http.raise_for_status_with_body(r)` 가 **경계에서** `status==400 && is_context_overflow(body)` 이면 body 를 파싱해 typed **`ContextOverflowError(actual, limit)`** 를 raise 한다(그 외 400/오류는 본문 포함 generic `HTTPError`; success 경로 무손상 — 스트리밍 200 에선 `r.text` 안 읽음). 루프는 **`classify_overflow(e)`** 로 판정하는데, typed error 이거나 **HTTP 400/413 + overflow 본문**일 때만 amounts 를 돌려주고 그 외엔 `None`(→ force_fit 안 함). 이 **상태 게이트**가 핵심: 예전엔 `is_context_overflow(str(err))` 로 *임의* 예외의 문자열만 맞으면 파괴적 force_fit 이 발화해 비-overflow 오류(설정 오류·프록시 500 페이지)가 히스토리를 **영구 파쇄**했다(AUDIT L-1, v8.6.1 수리).

```
provider.call() → 예외
    │
    └─ classify_overflow(e) → (actual, limit) | None
         │ = ContextOverflowError 이거나 (HTTPError && status∈{400,413} && overflow 본문)
         │   그 외(비-HTTP·500·overflow아닌 400) → None → force_fit 안 함(재전파)
         │ non-None & ctx 있음 & overflow_retries < _MAX_OVERFLOW_RETRIES(5)
         ↓
         (actual, limit)  ← typed error 는 이미 파싱됨; 폴백은 str(e) 에서 파싱
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

- **압축 비활성화**: 내부 `compaction_enabled=False` → 플레인 FIFO만 동작. **서브에이전트 전파**: `compaction_enabled` 플래그가 부모 `AgentLoop`→`tool_delegate`/`_run_single`/`_run_parallel`(delegate)과 `_handle_run_skill`→`execute_skill`(skill)의 `run_loop` 호출까지 스레딩됨 → 부모에서 끄면 delegate/skill 서브에이전트에도 적용. (CLI 플래그·env 스위치는 v5.17.0 에서 제거 — 압축은 항상 켜지고 실패 시 belt-and-braces FIFO 로 폴백. 프로그램matic 비활성 경로만 유지.)
- **과대 도구 출력 캡 + nudge (loop `_tool_observation`, 결과→관찰 seam)**: 도구 관찰 토큰이 **`max(context_window, MIN_CONTEXT_WINDOW) // 10`**(loop `_oversized_cap`) 초과면 전체 출력을 컨텍스트·history 어디에도 안 넣고 **over-cap 관찰**로 치환 — 호출 자체는 성공. 캡 하한(`MIN_CONTEXT_WINDOW//10`)은 capabilities 미탐지/`context_window=0` 때 캡이 0(=무제한)으로 꺼지던 구멍을 막는다(하한값이 새 매직넘버가 아니라 "에이전트가 기동조차 안 하는 최소 윈도우"에서 파생돼 자기일관적). **정책은 단 하나** (`Tool.render_oversized` — v8.42.0 일원화; **어떤 도구도 오버라이드하지 않는다**): ① 전문은 **항상 파일로** — 도구가 이미 쓴 파일(`oversized_source_path`: read_file=원본 경로·agent run=`<run_dir>/result.md`) 아니면 `persist_oversized()` 가 `<session_dir>/oversized/<tool>-<sha1>.txt` 로 lazy 저장(세션 없으면 tempdir 폴백 — "넘치면 파일에 남는다"가 예외 없이 성립), ② **head/tail 발췌**(`oversized_excerpt`, cap×15% 클램프·tail 60% 가중 — 빌드/테스트 로그는 답이 꼬리에 있어 왕복 1회를 아낌; 병적으로 긴 단일 라인도 char 예산으로 하드슬라이스), ③ **회수 경로 3종**(regex `read_file(search=)` / 라인범위 / **분할정복 팬아웃** — 섹션 크기를 `cap×0.6` 목표로 산정해 각 섹션이 실제로 캡에 들어가게 하고(구 clamp-8 은 섹션이 여전히 over-cap 이라 한 단계 아래서 같은 벽에 부딪혔다), 상한 16 에 걸리면 "각 서브에이전트가 더 좁히라"고 명시; `agent ∈ tools_available` 일 때만), ④ **근본 원인 한 줄** = **`Tool.oversized_retry_hint`** — 도구별로 다른 유일한 부분(shell=명령에 grep/head/tail, read_file=search/범위/code_index fetch, fetch=좁은 URL·얕은 depth, agent=좁은 task, code_index=단일 심볼·search·max_bytes, history=LIMIT/projection/substr; 미선언 도구=`GENERIC_RETRY_HINT`). 종전의 빌더 3종(`default_`/`on_disk_`/`narrow_oversized_nudge`)+도구 오버라이드 6개는 이 하나로 흡수 — MCP 등 신규 도구가 아무 것도 선언하지 않아도 올바르게 동작하고, 파일조차 안 남기던 code_index·history 가 순수 개선됐다. per-result `body`/`tokens` 는 명시 인자, per-call 컨텍스트는 **`ctx`(`RunContext`)**(`session_dir`·`oversized_cap`·`tools_available`; loop `_run_ctx()` 단일 조립점, 실행 seam `run`/`_run` 과 동형). **턴 단위 예산 (`_turn_obs_budget` = cap × `TURN_OBS_BUDGET_MULT`(3), `ToolBridge.accumulate_observation`)**: 캡은 op 단위라 각각 캡 바로 아래인 op 이 N 개면 합계 N×cap — 10개로 윈도우가 찬다(그리고 시스템 프롬프트는 독립 read 를 한 턴에 몰라고 권장한다). 누적이 예산을 넘으면 **그 이후** op 의 관찰이 같은 정책으로 치환되고(앞쪽 op = 모델이 먼저 요청한 것이 원문 유지), nudge 의 `reason` 이 "이 결과가 컸다"가 아니라 "이 턴이 예산을 넘었다"고 정직하게 말한다(고치는 법이 다르므로). 각 누적 엔트리가 자기 토큰비용을 들고 있어 러닝합은 O(N). **`run_skill` 도 이 seam 을 지난다** (v8.42.0 — 종전 유일한 우회로: 스킬은 서브루프 전체 출력을 반환해 단일 관찰 중 가장 커질 수 있는데 무제한이었다). loop 가 `messages.append`·`ctx.add` **양쪽 전에** 최종 본문을 만들므로 일관 → **`add` 는 순수 저장**. 비-도구 관찰(개입·ask/message)·사용자/어시스턴트 메시지는 캡 대상 아님(ask/message 는 `accumulate_raw` 로 턴 예산에는 계상). 관찰 렌더는 `_append_observation` 단일 지점, multi-op 은 flush 합본 1카드. 스킬 템플릿의 `` !`cmd` `` 주입은 이 seam 밖이라 자체 상한(`_INJECT_MAX_CHARS`=4000, head+tail 보존)을 가진다.
- **경로 표기 규칙 (v8.48.0)**: 프로세스 cwd 는 수명 내내 고정이고(`os.chdir` 부재, 서브프로세스 `cwd=` 인자 부재) 파일 도구가 전부 `Path(path)` 라 상대경로는 cwd 기준으로 풀린다 — 즉 상대/절대가 같은 파일을 가리키고, `_confine` 은 상대경로를 워크스페이스 안으로 간주해 통과시키므로 상대 쪽이 마찰도 적다. 그런데 프롬프트가 `Working directory:` 만 알려주고 규칙을 안 줘서 실측 세션에서 **절대 23 : 상대 2** 였다. 경로는 `action_input` 으로 매 턴 재공급되고 compaction 파일 목록에도 누적되므로 비용이 반복 청구된다. ① `_build_environment_section` 이 규칙을 명시(도구 인자=상대, 예외는 작업 디렉토리 밖 파일과 최종 답변의 사용자 안내), ② 루프도 같은 규칙을 지킨다 — `tools/base.display_path()` 가 over-cap 넛지의 경로를 cwd 상대로 줄인다(작업 디렉토리 밖인 headless tempdir 폴백은 절대 유지). 넛지 하나에 경로가 3~4회 등장하므로 절약이 배가되고, 무엇보다 루프가 절대경로를 뱉으면 over-cap 마다 프롬프트 규칙의 **반대를 가르치는** 셈이 된다.
- **세션 상태 블록 (프롬프트 꼬리, v8.46.0 — `prompts/session_state.py`)**: 꼬리 첫 줄은 경계선 `TAIL_BOUNDARY`(v9.25.2 — 사용자 메시지 본문에 이어 붙는 꼬리라 모델이 사용자의 글과 하니스의 글을 가를 수 없었고, "이건 왜 필요한거야?" 에 Task Guidelines 를 설명했다; 사용자가 쓰지도 보지도 않는다는 **사실**만 적고 면책 문구는 넣지 않는다). 매 턴 바뀌는 휘발 상태(턴 예산·`## Live Agents`·`## Session Memory`·압축 안내)를 **마지막 메시지 뒤**에 붙인다. **컨텍스트 사용량 수치는 싣지 않는다 (v10.9.0)** — `context: ~used / budget (p% — compaction at 100%)` 줄을 본 상주 에이전트가 목표치의 95~99% 에 두 시간 머물며 편집을 전부 거부했고, 턴마다 몇백 토큰만 보태 100% 를 못 넘어 압축이 영영 안 돌았다(67qcmb). 숫자가 준 유일한 지렛대가 "일 줄이기" 라 숫자를 없앴다; `build_session_state` 는 `used_tokens`/`budget_tokens` 를 받지 않고, 시스템 프롬프트 Context Window Discipline 도 "차면 요약으로 바뀌고 작업은 이어진다" 로 바꿨다. **압축 안내 (v9.26.4)**: `COMPACTION_NOTICE_STEPS`(v10.9.0: 80% 하나 — 종전 70·80·90%)를 처음 넘는 턴에 한 번씩 — `LLMCaller._compaction_armed` 가 아직 안 띄운 단계를 들고 `compaction_notice_due(used, budget, armed)` 가 `(띄울지, 다음 armed)` 를 돌려준다(사용량이 단계 아래로 내려가면 재무장). 문구 `COMPACTION_NOTICE` 는 결핍 어휘 없이 "요약으로 대체되고 작업은 그대로" 만 말한다(v10.9.1: 낡은 메모리는 `memory(mode=delete)` 로 지우라는 안내 추가 — 색인은 꼬리에 매 턴 실리고 압축을 통과하므로 압축 전이 정리 시점) — 종전 75% 이상 매 턴 `⚠ nearly full … NOW` 가 모델을 과하게 보수적으로 만들었다(사용자 보고). **왜 꼬리인가**: 프로바이더는 프롬프트 접두사를 캐시하고 첫 불일치 토큰에서 재사용이 끊기므로 위치가 곧 비용이다. ① 시스템 프롬프트에 두면 그 섹션 이후 전부(남은 시스템 프롬프트 + **대화 전체**)가 무효화 — `memory add` 한 번이 전체 재프리필이었고, 이 압박 때문에 로스터가 busy/idle 을 못 실었고 `_build_environment_section` 이 날짜를 뺐다. ② "사용자 요청 뒤"는 에이전트 루프에서 프롬프트 **앞쪽**(요청 1개 뒤로 N턴이 자람)이라 직관과 반대로 최악. ③ 꼬리는 비용 0 — 턴 N 이 `… obs_{N-1} + STATE_N` 로 끝나고 턴 N+1 은 `… obs_{N-1}, assistant_N, obs_N + STATE_{N+1}`, 접두사 일치는 `obs_{N-1}` 에서 끝나는데 이는 새 토큰 때문에 **어차피 거기서 끝났을 자리**다(손실 = 이전 블록 자신, 대화 길이와 무관한 상수). recency attention 이 가장 강한 위치이기도 하다. **전달**: 새 메시지 추가가 아니라 마지막 메시지 본문에 append — 프로바이더가 `messages` 를 verbatim 전송해(`providers/anthropic.py:97`) 끝에 `role=user` 를 더하면 연속 동일-롤이 되고 처리가 프로바이더마다 갈린다. `_OBS_COMPLETE_NUDGE` 와 같은 seam(`ContextManager.get_messages` 꼬리 주석 블록, copy-before-mutate)이며 **history.jsonl 에 절대 미영속** (영속하면 낡은 수치가 매 턴 재공급 + resume 프리뷰 오염). **조립**: `LLMCaller._build_session_state` — 턴 카운터(state)·레지스트리(config)·예산이 다 거기 있어서. `ContextManager` 는 `set_session_state()` 저장만 한다. 블록은 요청에 들어가지만 `system` 이 아니라 **`_state_tokens` 로 압축 예산에서 별도 예약**(직전 턴 크기 — 정확한 수치는 `ensure_within` **후**에 만들어야 하는데 예산은 그 전에 필요하므로). 경고는 예산의 `COMPACTION_WARN_RATIO`(0.75)에서 발화하고, 문구는 조기 `complete` 유발을 피하려 "여유 없음"이 아니라 **행동 지시**(지금 memory 에 남겨라 / 끝낼 이유 아니다)다. **동반 제거**: 로스터가 매 턴 재조립되므로 `notify_agents_changed`·`consume_agents_reload`·`_membership_changed`·`Renderer.update_prompt_section`(+ web `prompt_changed` 이벤트와 프론트 중계)이 전부 소멸 — 남겨뒀다면 스냅샷에 없는 "Live Agents" 를 **삽입**해 LLM 이 받지 않은 유령 섹션을 인스펙터에 만들었을 것. `memory`/`teammates` 재조립 트리거도 제거되어 세션 중 시스템 프롬프트를 바꾸는 소스는 **DIRECTIVE.md 뿐**이다.
- **Prompt Inspector 이중 계상 제거 (v8.46.0)**: `build_inspector_sections` 가 압축 요약·touched-file 목록을 `"⊙ … (user-injected)"` **가짜 시스템 섹션**으로 합성해 `note_system_prompt` 로 보냈는데, 이 둘은 실제로는 `role=user` 메시지라 `_dynamic_context_sections`(= `ctx.get_messages()` 순회)가 **이미** 내보내고 있었다 → `/api/debug/prompt` 가 같은 내용을 두 번 반환하고 `est_tokens`/`total_chars` 를 중복 합산했다. 합성을 지우니 함수가 항등이 되어 통째로 삭제; 루프는 `prompt.sections` 를 그대로 넘긴다. 부수 효과로 인스펙터는 꼬리 주입물(세션 상태 블록 포함)을 **자동으로** 올바른 위치에 보여준다 — `get_messages()` 를 읽기 때문.
- **요약 프롬프트 (agentic resume 지향)**: `_llm_compact_summarize` 의 system 프롬프트가 단순 4-clause 가 아니라 **구조화 섹션**(TASK/STATE/DONE/PENDING/DECISIONS/FAILURES/FACTS, 빈 섹션 생략)을 요청 — 에이전트가 요약만으로 작업을 이어가야 하므로 남은 작업(PENDING)·실패한 시도(FAILURES)·verbatim 식별자(FACTS: 경로/명령/에러문자열) 보존 + "transcript 에 있는 것만, 지어내지 말 것" 규칙 포함. 재귀 병합(이전 요약 + 신규 transcript)은 "same section headings" 로 구조 유지. (실세션 검증: Qwen3.6-27B 가 구조 준수 + 실제 `AttributeError` 실패를 verbatim 포착, 6.4K→2.3K자.)
- **Belt-and-braces**: LLM 요약 실패(`CompactionError`)나 재구성 후 캐시 미충족 모두 같은 FIFO 경로로 수렴 → 무한 트리거 루프 없음
- **Observability**: `TurnRecorder.record_compaction(tokens_before/after, evicted_count, fallback_used, failure_signal, duration_ms)` → `turns.jsonl`에 `event: "compaction"` 기록
- **UI**: `render_compaction_progress(phase, ...)` 단일 helper가 `_renderer.compaction(phase, ...)` 으로 위임 — CLI-vs-web 라우팅은 renderer 가 담당. **base `Renderer.compaction` 기본 구현**은 `status` 한 줄(start/done/warning)로 출력(CLI/minimal 그대로). **`WebRenderer.compaction` 은 override** 하여 전용 `compaction` SSE 이벤트(구조화 payload: phase/old_tokens/new_tokens/evicted_count/reason)를 emit → 프론트(app.js `compaction` 리스너)가 **대화창 인라인 시스템 라인**(`.card-sys`: start "압축 중…" → done "압축됨 X→Y tok" 갱신, warning) 으로 렌더. transient(재접속 시 미재생). (이전엔 helper 가 generic `status` SSE 를 쐈으나 프론트 리스너가 없어 웹에선 안 보였음 — 전용 이벤트로 수리.)
- **main 앞 메일은 입력창 위 대기열에 선다 (v10.51.0, 종전 `agent_mail_hint` 웹 이벤트·`agent_wake` 카드 대체)**: 한 번의 도착이 세 군데(도착 힌트 · 🤝 깨우기 카드 · 관찰 줄)에 나오던 것을 사용자 요청과 같은 UX 하나로. `AgentRegistry._push_reply` 가 메일마다 `mail_id` 를 찍고(복원분도 새로) `pending_mail()` 이 미배달분을 대기열 줄(`mail_queue_item`: `{id:'mail-N', conn_id:'', nickname:<관찰 tool>, text:<곧 들어갈 관찰의 머리줄>, mail:true}`, 질문 제외 — ❓ 트레이)로 내고, 넣거나 비울 때 `on_pending_change` → `WebServer.broadcast_queue` → `queue_snapshot()`(InputQueue 스냅샷 + `mail_source`) → sticky `queue` 이벤트. 프런트(app.js 대기열 IIFE)는 메일 줄을 **1.5초 유예**(`Q_MAIL_GRACE_MS`) 뒤에만 그리고(유휴 main 은 그 안에 흡수 — 종전 깨우기 깜빡임 재발 방지), 취소 버튼이 없고, 큐에서 빠지면 곧 `✓ 주입됨`(메일박스 drain = 관찰 주입; 유예 안에 빠졌으면 영수증도 없음). 깨우기 런은 `WebSurface.echo(wake=True)` 가 아무것도 그리지 않고 resume 재생도 사람 아닌 작성자 레코드를 건너뛴다(`_CARD_EVENTS`·`REPLAY_CONTRACT` 에서 `agent_wake` 삭제). CLI: `wire_agent_mail(on_mail_notice=)` 가 `(reply, busy=not waker.idle)` 로 부르고 `_agent_mail_notice` 가 **바쁠 때만** `⏳ 대기 · [tool] 머리줄 — 지금 턴이 끝나면 main 에 전달`(질문은 늘) — 종류별 손 문구가 없어 사망 통지가 '회신 도착' 으로 떨어지던 오표기도 사라졌다. `_ConsoleSurface.echo` 의 '🤝 에이전트 회신 배달' 줄 삭제. web 은 `on_mail_notice=None`. 테스트 `test_agents_live::test_web_mail_waits_in_the_input_queue_not_a_hint`·`test_restored_mail_gets_fresh_queue_ids`, 브라우저 `tests/browser/test_mail_queue.py`(바쁨 대기→주입·유휴 무깜빡임·재접속·질문 제외·타임라인 무힌트).
- **📋 열린 항목 띠 (v10.119.0, 장부 P4 — docs/ledger/DESIGN.md §15)**: 꼬리 `## Open Items` 와 같은 장부를 사람에게. `Ledger.snapshot(turn)` → `dispatch.show_open_items`(main 런만; 런 시작·턴 경계·`complete` 정산·런 끝) → `Renderer.open_items` — web 은 스티키 `open_items`(늦은 접속 스냅샷·같은 payload 생략·`_flips_status` 로 status.json `open_items` 요약, /api/health 동형), CLI 는 `open: u-2 n-1` 한 줄. 항목 `hidx` 는 렌더러가 `request_id → hidx` 맵(루프의 `note_request`·재생의 레코드 `request_id`)으로 채운다. 프런트 `ledger.js`(순환 밖 잎)가 `#open-items` 를 ❓ 트레이와 ⏳ 대기열 사이에 그리고, 행 클릭은 `agentcli:jump` {hidx} → channels 가 main 카드로 점프. 모델 입력 불변(꼬리 golden).
- **🔁 제자리 고리 (v10.120.0, StaleGuard — docs/stale-guard/DESIGN.md)**: B1 과 별개로 비연속 고리를 결과로 본다. 턴의 관찰이 다 기록된 뒤 디스패처가 그 턴의 레코드(`ctx.records_since`)를 `stale_turn_pairs` → `StaleGuard.observe` 에 먹인다(규칙 C N3W8, 하니스 작업 중 리셋). 발화마다 failures.jsonl `stale_loop` + `Renderer.stale_loop(info)` — web 은 스티키 `stuck`(입력창 위 🔁 띠 + Stop, status.json·health `stuck`), CLI 는 흐린 한 줄. 두 번째 발화부터 웹 main 만 기존 `confirm` 으로 멈추고 묻는다(c Continue — 코멘트는 포트 `enqueue_user_message` 로 입력 큐 → 다음 경계에 `u-N` 사용자 메시지 / s Stop — `stop_event` 와 결과 문구). run·벤치·상주·one-shot·스킬은 기록·표시만. 모델 입력 불변(Hint 사용자 메시지만). 등가성: trajectory 1,153개에서 오프라인 규칙 C 와 38/38(첫 발화·발화 수), 라이브 쌍은 묶음 `parts` 분할로 6 trial 이 더 발화.
- **스트림 무진전 표시 (`stream_stall`, v8.60.0 — 종전 agent_mail_hint 웹 이벤트와 동형 패턴)**: `providers/http.py` 가 문자열 대신 **구조화**해 `render_stream_stall(kind, elapsed_s, limit_s, attempt, attempts)` 호출. 표시는 이벤트를 두 부류로 가른다 — `kind="wait"` 는 30초마다 **반복**(한 시도에 최대 20회)이라 **제자리 갱신**, `kind="resend"` 는 드문 **전이**라 기록, `kind="clear"` 는 대기 종료 정리. **base 기본**은 `status` 위임(CLI/커스텀 그대로). **MinimalRenderer override**: TTY 면 마르퀴와 같은 한 줄 슬롯에 `\r` 로 제자리 페인트(종전엔 시도당 20줄 × 4시도 = 80줄이 쌓여 정작 재전송·실패가 묻혔다), **비-TTY(harbor·파이프·instance.log)는 기본 구현(줄 출력) 유지** — `\r` 이 무의미하고 줄이 남는 편이 사후 진단에 유리. **WebRenderer override**: 전용 `stream_stall` SSE 이벤트 + wait 는 **sticky 슬롯**(긴 대기 중 접속한 뷰어도 현재 상태를 봐야 하므로 재접속 snapshot 에 실림; 전이/정리 시 슬롯 비움). 프론트는 `app.js` `stream_stall` 리스너 → `renderStreamStall` 이 `.stall-line`(진행바 포함) 하나를 재사용. **이것이 원래 버그의 수리** — 종전엔 이 알림이 전부 `status` 로 나갔고 app.js 에 그 리스너가 없어 **웹에서 통째로 드롭**, 사용자는 40분간 빈 화면 뒤 "LLM call failed" 만 봤다. 계약 테스트 `test_web_renderer.py::TestStreamStallDisplay`(프론트 배선 가드 포함)·`test_render.py::TestStreamStallCli`·브라우저 `TestStreamStallDisplay`.
- **부분 출력 폐기 (`stream_reset`, v8.61.0)**: 재전송 직전에 호출 — **base 기본 no-op**(부분 출력을 화면에 들고 있지 않은 렌더러는 할 일이 없다), MinimalRenderer 는 마르퀴 버퍼(`_stream_buf`/`_think_buf`/`_stream_chunks`)를 0 으로 되돌리고 줄을 지우며, WebRenderer 는 전용 transient `stream_reset` 이벤트(프론트 `clearStreamingCard(d.task_id)`). **`stream_end` 를 재사용할 수 없다** — 프론트가 그걸 "곧 assistant_turn 이 대체한다"로 읽어 카드를 남기기 때문(여기선 대체가 아니라 폐기). `_emit` 이 스코프 task_id 를 자동 부착해 서브에이전트 카드도 제 것만 지워진다. 계약 테스트 `test_render.py::TestStreamResetCli`·`test_web_renderer.py::TestStreamResetWeb`·브라우저 `test_reset_discards_partial_output`(중복 이어붙기 실재현).
- **Scratchpad 없음.** history.jsonl이 대화 기록이자 artifact 인덱스
- **Context inject 없음.** LLM이 필요할 때 history 로 pull
- System prompt에 Context Recovery Guide 포함
- 스킬/agent 서브에이전트는 부모 budget 상속

#### 저장과 표현의 분리

- **저장**: history.jsonl (JSON Lines) — 구조화된 메시지
- **표현**: 자연어 변환 — LLM에 전달되는 user/assistant 메시지

```
저장: {"role":"assistant","thought":"auth.py를 읽겠다","action":"read_file","action_input":{"path":"src/auth.py"}}
표현: auth.py를 읽어 구조를 파악해야 한다. → read_file(src/auth.py)
```

#### Assistant turn lifecycle — 4 forms, 3 plugin-owned transitions

assistant turn 한 번은 3가지 형태(A/B/C)로 conversation pipeline을 통과한다. 각 형태는 **소비자가 다르고** 따라서 **요구하는 셰이프도 다르다**. 형태 간 변환은 모두 dialect plugin이 소유 — 새 dialect 추가 시 lifecycle 전체가 자동으로 그 plugin의 wire shape을 따른다.

| 형태 | 소비자 | 요구 셰이프 | 어디서 |
|---|---|---|---|
| (A) Emit | model이 produces | plugin wire shape, raw string | provider response |
| (B) Store | history.jsonl reader / 분석 스크립트 | 구조화 dict `{thought, action, action_input}` (save-time sanitize) | `history.jsonl` |
| (C) Feed | LLM — 같은 세션 다음 turn(live prior) **AND** overflow/resume 복원 | plugin wire shape ≈ (A) | in-memory `messages` |

두 plugin 메서드가 형태 간 다리를 놓는다. **live prior 와 resume prior 는 같은 `(B) → render → (C)` transition** — 매 턴 prior 가 raw 가 아닌 저장된 record 에서 재구성된다:

| 전이 | Plugin 메서드 | 입력 → 출력 | 호출 사이트 |
|---|---|---|---|
| (A) → (B) | `serialize_assistant_for_history(raw)` | LLM raw → 디스크 dict (thought·bare 를 `sanitize_thought` 로 save-time 정제) | `loop._append_observation` (`ctx.add` 직전) |
| (A) → (B), terminal | `serialize_terminal_for_history(thought, result)` | (언랩된) complete 결과 → 디스크 dict (포맷 동질 모양) | `loop._dispatch_op` complete/echo-final 분기 (`ctx.add` 직전) |
| (B) → (C) | `render_assistant_from_history(record)` | record → chat 메시지 dict | `loop._append_observation` (live prior) + `manager._to_natural_language` (resume) |

`serialize ↔ render`는 **서로의 역연산**: round-trip이 닫혀 있어 (A) ≈ (C). 모델이 흘린 wire sentinel 은 save-time(B)에서 한 번 정제되므로 prior 로 다시 새지 않는다 (format-runaway mimicry 의 근본 차단). byte-level 차이는 JSON 정규화뿐, semantic 동일. **(이전엔 (A)→(C) 가 `normalize_assistant_for_messages`=identity 로 raw 를 그대로 live prior 에 먹여 mimicry 의 근본 고리였음 — render 통합으로 제거.)**

**DialectBase ABC가 lifecycle 디폴트 제공**: `serialize_assistant_for_history` 디폴트 = `self.parse()` + 구조화 필드 추출 (+ bare content `sanitize_thought`), `render_assistant_from_history` 디폴트 = `self.render_full_example()` 호출 (live + resume prior 양쪽). `sanitize_thought` = identity, `render_action_input` = identity, `prefill` = `""`, `provider_call_kwargs` = `{}`, `format_rules` = `build_format_rules(self)`. 새 plugin은 **format-specific 메서드만 구현**하면 lifecycle 전체가 자동으로 작동:
- `parse(llm_text)` — wire shape 파싱
- `render_full_example(thought, action, action_input)` — wire shape 출력 (Format Rules section + history round-trip 양쪽 이용)
- `format_rules_anchor()`, `format_rules_field_specific()` — 안내 문구
- 6개 recovery wording (framing × 2, reminder × 2, static hint × 2)
- `system_user_prefixes()` — recent_exchanges 필터링

`manager._to_natural_language`는 user / tool branch만 직접 처리하고 assistant branch는 plugin에 위임 — `context/`는 format-agnostic, plugin이 format-aware. 의존 방향: `context → dialects` (downward, lazy import 없음).

#### 세션 파일 구조

```
.agent-cli/sessions/{session_id}/
├── history.jsonl                              ← main 대화 기록
├── main_plan_e8d4_20260405T143112890.md       ← main artifact (flat)
│
├── run_coder_f1a9_20260405T143230456/         ← agent run subdir
│   ├── history.jsonl                          ← run 내부 대화
│   └── result.md                              ← run 최종 결과
│
└── skill_summarize_d4e1_20260405T143200100/   ← skill subdir
    ├── history.jsonl                          ← skill 내부 대화
    └── result.md                              ← skill 최종 결과
```

- main: root에 flat artifact
- run/skill: subdir에 history.jsonl + result.md (재귀 중첩 가능)
- fork 모드: parent history.jsonl 복사 → 서브에이전트가 이어서 append

---

## 6. 도구 시스템

### 6.1 등록된 도구

**실제 도구** — 파일/셸/네트워크 작업 수행:

| 도구 | 설명 | 필수 입력 | 출력 |
|------|------|----------|------|
| `read_file` | 파일 읽기 (hashline 포맷, flat-native — 한 op=한 파일). 모드: `stat` (메타데이터 + 앞 20줄), `search` (정규식 grep), `line_start/line_end` (부분 범위), 또는 mode 없이 full read (크기 제한 없음). 여러 파일은 멀티-op 으로 read_file op 을 여러 개 emit. | `path` 필수, `line_start?`/`line_end?`/`search?`/`context?`/`stat?` | `LINE#HASH:content` 형식 |
| `write_file` | 파일 생성/덮어쓰기. 작성 content 를 hashline 으로 반환 (read_file 없이 edit_file 직결) | `path`, `content` | 저장 확인 + hashline(edit refs) |
| `edit_file` | hashline 기반 파일 편집 (flat-native — 한 op=한 편집) | `path`, `op`, `pos`, `end?`, `lines?` | 편집 확인 메시지 + diff |
| `shell` | 셸 명령 실행 | `command` | stdout + stderr + exit code |
| `agent` | 서브에이전트 통합 도구 (5.0.0). `mode:"run"`=일회성 in-process 위임(flat-native — 한 op=한 task; 연속 run op = 병렬, mode-aware 배칭) / `spawn`/`request`/`status`/`resume`/`kill`=상주(main 전용, 회신 자동 배달) | `mode` 필수; run: `task`+`context?`/`tools?`/`profile?`/`instructions?`; 상주: `key`/`message` 등 | run: 구조화된 결과 (output + activity log + duration) + run subdir 경로. 상주: 즉시 반환 + 턴 경계 관찰 배달 |
| `memory` | 세션 메모리 (failure/discovery/decision/note 기록·조회, compaction 무관, resume 복원, 상시 `## Session Memory` 인덱스) | `mode`(add/get/update/delete/list), `type?`, `summary?`, `detail?`, `tags?`, `id?`, `tag?` | **add**: type+summary(+detail/tags) → id. **get**: id → 전체(detail 포함). **update**: id + 바꿀 필드. **delete**: id. **list**: type/tag 필터. detail 은 인덱스 미노출(요약만) — get 으로만 회수. |
| `history` | 세션 이력 SQL 질의 (v10.29.0, 구 `read_context`) | `query?`, `sessions?` | `query` 없으면 스키마·예시·세션 목록. `query`=읽기전용 SELECT over `history`(session/loc/seq/kind/turn/ts/tools/files/author/text), 행은 VERBATIM — 과대 결과는 거절+좁히기 nudge. 기본 범위 현재 세션, `sessions='all'`/id 로 확장. |
| `fetch` | 웹 페이지 fetch → 마크다운 변환 | `url` | 재귀 링크 추출, 에러 힌트 |

**가상 도구** — loop.py if-cascade가 인터셉트해 직접 처리 (실제 tool dispatch 우회). LLM에게는 일반 도구처럼 노출 (시스템 프롬프트의 ``## Available Tools`` 섹션 포함):

| 도구 | 설명 | 필수 입력 | 비고 |
|------|------|----------|------|
| `complete` | 작업 완료 신호 | `result` | 루프 종료 |
| `ask` | 사용자에게 질문 | `questions` | 대화형 전용 (ctx 없으면 제거) |
| `run_skill` | 스킬 실행 | `name` | loop 레벨 인터셉트, skill subdir 생성 |

### 6.2 agent 프로파일 로딩

`agent` 도구의 `profile` 파라미터로 사전 정의된 프로파일(역할)을 로드할 수 있습니다 (run/spawn 공용, `subagent/profiles.py`):

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

**핵심 함수** (`subagent/oneshot.py` + `subagent/profiles.py`):
- `profiles._PROFILE_NAME_PATTERN` — 이름 검증 (`[a-zA-Z0-9_-]`만 허용)
- `profiles.load_profile(name)` — 파일 탐색 + YAML frontmatter 파싱 → `(body, config, error)`
- `_extract_activity_log(messages)` — raw history 레코드에서 per-turn 액션 요약 추출. `manager.iter_record_ops` 로 **두 저장 shape 모두**(멀티-op `ops` + 단수 legacy `action`) 읽음 — 멀티-op 턴은 op 요약을 `"; "` 조인해 1 iter. (이전엔 `json.loads(content)` — dialect 리팩터(423608e) 후 레코드가 구조화 필드로 바뀌어 **모든 실 세션에서 빈 로그를 내던 silent 회귀**를 v4.35.1 에서 수리; 테스트 픽스처도 실제 저장 shape 으로 교체)
- `_summarize_action(action, action_input)` — 단일 액션을 한 줄 요약으로 포맷
- `_extract_last_actions(messages, n)` — 마지막 N개 액션(턴) + 에러 observation 추출 (같은 `iter_record_ops` 경로)
- `_persist_run_result(formatted, run_dir)` — result.md를 run subdir에 저장
- `_format_delegate_output(result)` — DelegateResult를 구조화된 observation 문자열로 포맷
- `_AGENT_SEARCH_PATHS` — 검색 경로 리스트
- `_FRONTMATTER_PATTERN` — `---` frontmatter 정규식

**DelegateResult 필드**: `output`, `duration_secs`, `activity_log`, `last_actions`, `iterations` (`iterations` = 루프가 실제로 돈 턴 수 `ctx.current_turn`, v9.24.1 — 20줄로 잘린 활동 로그의 줄 수를 세던 종전 값은 60턴을 "20 iterations" 로 보고했고, 기록 기반 계산은 압축·재시도 미기록에 흔들린다)

**산출물 구조**: run 실행 결과는 다음 섹션을 포함:
1. 서브에이전트 출력 (output 또는 "(subagent returned no result)")
2. `[Subagent activity]` — per-turn 액션 로그 (최대 20개)
3. `[Last actions before failure]` — 실패 시 마지막 5개 액션 + 에러 힌트
4. `[Duration: Ns]` + `[Subagent used N turns]` — 실행 메타데이터
5. `→ run_{name}_{hash}_{ts}/` — run subdir 경로 (history.jsonl + result.md)

**적용 우선순위**: op 에 명시된 `tools`/`model`이 프로파일 파일 설정보다 우선합니다.

**병렬 run lifecycle 통합 인터페이스** (`_run_parallel`):
- 각 worker thread 는 `renderer.begin_delegate_task(task_id, ...)` → `_run_single` → `renderer.end_delegate_task(task_id, ...)` 만 호출. 그 외 panel/capture 오케스트레이션 전부 renderer 책임.
- `task_id` 는 `delegate-{index}-{uuid4().hex}` (single 경로는 `delegate-single-{uuid4().hex}`). **thread id 가 아니라 uuid4** — `threading.get_ident()` 는 worker thread 종료 후 재사용되므로, 나중 delegate 호출의 worker 가 이전 호출과 동일한 id 를 받아 web 프론트 `ensureTaskGroup`(v7.13.0 접기 UX: 헤더 전체 토글 + **sticky 헤더**[긴 본문 스크롤 시 상단 고정=어느 위치서든 접기]+**본문 여백 클릭**[`e.target===body` 만, 중첩 카드/텍스트 미간섭]) 이 stale 항목에 short-circuit → 새 카드 미생성 버그가 있었음. uuid4 로 호출-간 유일성 보장. 프론트는 `delegate_task_end` 수신 시 `taskGroups[taskId]` 항목을 삭제(DOM 카드는 유지) → 전역 누적 방지 + stale 충돌 원천 차단.
- MinimalRenderer 가 첫 begin 에서 Live 영역 띄움 (`is_terminal` 체크 — non-tty 면 skip), 자체적으로 thread → task slot → capture buffer 매핑. emit (`thought`/`action`/`observation`) 마다 `set_thread_status` 로 카드 상태 업데이트, `_capture_line` 으로 버퍼 누적.
- 마지막 end 에서 Live 종료 + 각 task 의 captured 출력을 `┌─ 🦀 [N] agent: task` group 으로 wrapping 해서 replay (등록 순서).
- **단일 scope 추상화 (v-Phase2, CLI·web 동일 표면)**: skill 서브루프와 delegate/one-shot 워커를 `begin_scope`/`end_scope`(kind="skill"|"run") 하나로 통합. WebRenderer 는 persistent `scope_start`/`scope_end`(+kind) emit + `_thread_to_task` 로 후속 emit 에 task_id 자동 첨부(라우팅은 enclosing scope 복원=중첩 same-thread 안전) → 프론트가 kind별 collapsible card(🪄 skill/🦀 run). ★이전엔 skill 이 프론트 미처리 `group_start` 를 내 `/orchestrate` 가 카드를 안 그리던 버그를 근본 수리. CLI(minimal)는 kind 분기(skill 브래킷/run rich.Live 패널) — 표시만 매체별, 추상화는 동일. teammate begin/end_agent_work 도 scope_* 로 통일. **중첩 표현 (v7.27.0)**: 추상화 표면은 그대로 두고 `scope_start` 에 `parent`+`depth` 만 additive 추가 — 웹은 타임라인 **중첩 카드**(`ensureTaskGroup` → 부모 body append, 접힌 부모 헤더에 `.task-sub` 라이브 자식 힌트, 클릭-네비가 조상 체인 자동 펼침)로 포함관계를 표시하고, CLI 는 이미 `push_depth` 들여쓰기로 계층을 보이므로 **무변경**(parent/ts 인자는 수용 후 무시) — ★표시만 매체별 원칙 유지. `scopes.jsonl` 은 payload 를 그대로 기록하므로 resume 호환은 자동이고, 구 사이드카(필드 없음)는 프런트 기본값으로 **전부 slot 0**=이전 그림.
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

> **이미지 분기 (v10.59.0):** 매직 바이트가 PNG/JPEG/GIF/WebP 면 hashline 대신 `_read_image` — 결과 첫 줄은 자리표시 `[image: path (W×H)]`, 둘째 줄은 형식·바이트, `ToolResult.images` 에 참조. 모델은 종전에도 PNG 에 `read_file` 을 불렀다(UnicodeDecodeError) — 새 도구 대신 있던 길을 넓혔다. 줄 범위·검색·stat 은 이미지에 뜻이 없어 무시하고 그 사실을 적는다.

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

**해시 불일치 창 (`edit_file._mismatch_window`, v10.50.0).** 퍼지 매칭(공백·따옴표 정규화 후 재매칭)은 v10.48.0 에서 지웠다 — 성립 조건이 거의 없어 460건 중 0회. 대신 불일치 오류가 **그 줄 ±3줄의 현재 내용과 새 ref** 를 붙인다(apply 시점에 디스크에서 읽은 값이라 컨텍스트의 어떤 사본보다 최신). 종전 문구 "Re-read the file … then retry" 는 녹화된 실수 5건 재생에서 50/50 그대로 재읽기를 불러 실수마다 회복이 3턴(실패 → read → 재시도)이었다; ±3 창은 41/50 바로 재시도·엉뚱한 줄 0, ±2 는 의도한 줄이 창 밖이라 이웃 줄을 고친 1건, "태그가 있는 줄을 뜻했나?" 제안은 오히려 틀리게 유도(모델은 대개 줄 번호가 맞고 태그가 틀림)해 넣지 않았다. 자동 보정(n±1 적용)은 하지 않는다. 같은 파일 묶음에서는 거부된 op 마다 자기 창을 달고, 거부가 전부 불일치면 재전송 문장이 "보여 준 줄에서 ref 를 고쳐 전부 재전송"(창이 없는 거부 — 범위 밖·형식·op 오류 — 가 섞이면 종전 재읽기 문장). 범위 밖·형식 오류는 문구 불변.

**붙여넣은 줄 ref 구제 (`_parse_ref`, v10.31.0).** `pos`/`end` 가 `67#YB:def extract_total(text):` 처럼 read_file 이 보여 준 **줄 전체**로 오면(Qwen3.6-35B, Harbor financial-document-processor 에서 2회, 종전엔 "Invalid hashline ref" 로 거부 → 모델이 edit_file 을 포기하고 전체 재작성), 콜론 앞 `LINE#HASH` 만 취한다 — 모델이 뜻한 ref 가 그 태그라는 데 모호함이 없다([[grammar-rescue-principle]]: 구제할 수 있는 모양은 막지 않는다). 정규식 `^(\d+)#([A-Z]{2})(?::[^\n]*)?$` — **한 줄**만 구제하고 여러 줄 붙여넣기(어느 줄의 ref 인지 불명)는 그대로 거부. 같은 호출에서 `lines` 안에 `#YB:` 접두가 섞여 온 건 구제 대상이 아니다(파일 내용이라 하니스가 판단할 수 없음).

**변이 후 hashline echo (`_change_echo.render_change_echo`, write_file/edit_file 공용).** edit 는 원래 diff 만 반환했다 — diff 는 **무엇이** 바뀌었나는 보여주지만 `LINE#HASH` 태그가 없어, 모델이 방금 편집한 자리를 이어서 편집하려면 `read_file` 재왕복이 필요했다. 이제 성공한 편집은 diff 뒤에 바뀐 영역 ±3줄의 **fresh hashline 블록**을 붙여(절대 줄번호), 후속 edit 을 read 없이 체이닝할 수 있다. **이 조립을 `_change_echo` 로 추출해 `write_file` 의 소량-덮어쓰기 갈래도 동일하게 호출** — 이전엔 edit_file 은 diff+region, write_file 소량-덮어쓰기는 diff-only 라 "같은 diff 인데 출력이 다른" 비대칭이 있었는데, 공용 헬퍼로 **diff 를 내는 모든 관찰이 region refs 를 일관 동반**하도록 일원화했다(write_file 소량-덮어쓰기는 "다음엔 edit_file" 넛지가 붙는 자리라 region refs 와 시너지). 전체 파일 hashline 덤프(=비용 이점 파괴)는 `_MAX_REGION_LINES` 상한으로 막고 나머지는 read_file 로 안내한다. mimicry 우려는 무관 — 이건 **관찰-side**(도구 결과) 변형이라 action 재공급이 아니다(`feedback_refeed_own_output_mimicry` 는 관찰-side 는 안전이라 명시).

**한 op = 한 편집 (flat-native, Step 3).** edit_file 은 `{path, op, pos, end?, lines?}` 단일 편집을 받는다 — 옛 `edits[]` **op-내 중첩** 배열은 제거됐다('op 안에 배열 중첩이 27B 90% 깨뜨림' 실측, DESIGN Exp 8). **같은 파일 다중편집 = 루프 레벨 배치로 부활(다른 형태).** 연속된 같은-path edit_file op 들을 loop(`_dispatch_edit_batch`)이 모아 `apply_edits_batch` 로 처리: 원본 1회 read → 모든 ref 를 그 원본 기준 해석 → 범위 겹침 사전거부(`_find_overlap`) → bottom-up 정렬 적용 → 1회 쓰기, **all-or-nothing**. 옛 안전장치(범위 겹침 거부·bottom-up 정렬)가 **op-내 배열이 아니라 flat op 의 루프 그룹핑**으로 돌아온 셈 — nested-array 함정을 피하면서 "앞 편집이 줄을 밀어 뒤 편집 ref 가 stale" 문제를 원본-기준 해석으로 제거(fuzzy 는 같은 줄번호 정규화 한정이라 드리프트를 못 잡으므로, 애초에 드리프트를 안 만드는 이 방식이 정답 — 외부 검증: NousResearch hashline·5-edit-strategies 벤치마크의 bottom-up). 비연속·다른 파일은 per-op.

### 6.5 Tool Output 전달 방식

Tool output은 **잘림(truncation) 없이 전체를 그대로** LLM에 전달합니다 — 단, 한 관찰이 **`context_window // 10`**(loop `_oversized_cap`)를 넘는 병적 대용량(예: 레포 전체 `find`, 전 심볼 `code_index` 덤프)이면 컨텍스트에 안 들이고 **"좁히라"는 nudge 로 거절**합니다(전체는 어디에도 보존 안 함 — 호출 자체는 성공; 모델이 라인범위/`LIMIT`/`grep`/`tee→read_file` 로 다시 받음). 한 메시지가 윈도우를 넘겨 압축을 깨뜨리는 걸 방지(§5.4 과대 출력 캡 참조). 도구별로 `Tool.render_observation`(결과→관찰 본문)·`Tool.apply_oversized_cap`(기본 True)·`Tool.render_oversized`(캡 초과 시 낼 관찰 — 기본 제네릭 nudge, read_file 은 range/`read_symbols`/run 팬아웃 유도) 표면으로 제어. 이전에는 context window의 3% 비율로 잘랐으나(`tools/truncation.py`, 삭제됨) LLM이 불완전한 정보로 판단하는 성능 열화가 확인되어 제거했고, 그 뒤 청크-spill(history 보존 + `json_extract` 회수)도 거절-nudge 로 대체했습니다(spill 보관-회수 기계 제거 → 단순화). context가 호출별 압축 목표치를 넘으면 `context/manager.py`의 compaction이 oldest 절반을 LLM 요약으로 흡수하고, 실패/미충족이면 belt-and-braces로 FIFO drop이 메시지 단위로 떨궈냄.

### 6.5.0b Shell Output: full passthrough (이전 artifact guard 제거됨)

이전엔 shell 출력이 한도(기본 500줄 / 20KB) 초과 시 head/tail 미리보기로
치환하고 전체를 `<session>/shell/`에 저장하는 guard가 있었음. 2026-05-19
제거 — 실사용에서 **head/tail이 중간 디버깅 정보(error trace, 핵심
로그 라인)를 silent하게 누락**시켜 task가 풀리지 않는 사례 두 차례
관찰. 가드의 절약 효과보다 silent loss의 비용이 컸음.

현재 정책: **shell 출력은 잘리지 않고 그대로 LLM observation으로 전달**.
컨텍스트 budget 관리는 messages buffer의 2-tier 관리가 담당
(`context/manager.py`) — 호출별 목표치 초과 시 oldest 절반을 LLM 요약으로 흡수(미답 사용자 요청의 user 레코드는 원문 그대로 남긴다, v10.41.0),
요약 실패/미충족이면 FIFO drop으로 떨궈냄. 의도된 거대 출력
(`find /`, `cat huge.log`)은 모델이 자기 비용 인지하에 호출한 것으로 간주.

LLM이 출력을 좁히고 싶으면 도구 호출 자체를 좁혀야 함 (`tail -n 100`,
`grep ERROR`, `head -c 4096` 등). silent truncation 없음.

관련 환경변수 (`AGENT_CLI_SHELL_OUTPUT_LIMIT_*`, `AGENT_CLI_SHELL_ARTIFACT_*`)
도 함께 제거됨. read_file의 full-read guard 역시 이후 제거됨 — 큰 파일도
bare full read를 허용하고, 컨텍스트 관리는 모델 자율 + downstream
compaction에 맡긴다 (모델이 거부에 헤매던 비용이 더 컸음).

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
| **Anthropic** | `/messages` | x-api-key | - | `thinking: {type: adaptive}` 켬/끔 (v10.72.0) |
| **OpenAI Compat** | `/chat/completions` | Bearer token | `dialect={"type":"json_object"}` (basic JSON) | `chat_template_kwargs.enable_thinking` 켬/끔 (v10.72.0) |

네이티브 tool calling (Anthropic `tool_use`, OpenAI `function calling`)은 **사용하지 않습니다**. 모든 프로바이더가 동일하게 ReAct 텍스트 파싱을 거치므로 provider-specific 코드 경로가 줄고, 프로바이더 편차가 거의 없어집니다.

**구조화 출력 정책**: 두 프로바이더 모두 **basic JSON mode**만 사용하고, **strict JSON Schema는 쓰지 않습니다**. 이는 확장성을 위한 선택이며 다음과 같은 배경이 있습니다:

- strict JSON Schema 강제는 일부 백엔드(예: mlx 엔진으로 패키징된 모델)에서 런타임 에러나 조용한 출력 깨짐을 유발했으므로 미사용 — basic JSON mode만 사용.
- Basic JSON mode(`dialect={"type":"json_object"}`)는 "유효한 JSON을 내라"는 신호만 주고 스키마는 강제하지 않음. 거의 모든 백엔드가 지원.
- ReAct JSON 구조 강제는 대신 시스템 프롬프트의 `FORMAT_RULES`와 3단계 파서(json.loads → json_repair → regex)가 담당. 32B+ 모델에서 신뢰성 충분.
- 7-14B 모델은 schema 없을 때 포맷 drift가 늘지만, 이 사이즈는 README에서 이미 비권장 구간.

향후 특정 백엔드가 strict schema를 반드시 필요로 하면, 현재 기본값을 건드리지 말고 **opt-in 플래그**로 다시 도입할 것. mlx 패키지 모델에서 재발 여지가 있으므로 기본 활성화는 금지.

### 7.3 프로바이더 팩토리 (`providers/__init__.py`)

```python
create_provider("anthropic", base_url, api_key)  → AnthropicProvider
create_provider("openai", base_url, api_key)     → OpenAIProvider
# 그 외 → ValueError("Available: anthropic, openai")
```

OpenAIProvider 하나로 OpenAI, vLLM, LM Studio, mlx-lm을 `--base-url`만 바꿔서 커버.

### 7.3½ stop_reason 정규화 (P0-1, v8.34.0)

`LLMResponse.stop_reason` 은 **루프 어휘**로 통일된 계약이다: `"stop"`(정상 종료) / `"length"`(출력 절단) / 합성 `"interrupted"`·`"degenerate_runaway"`·`"runaway"`(v10.10.0 — `providers/runaway.py` 의 내용 무관 감지기가 스트림을 닫음; 규칙 이름은 `stop_detail`). 루프는 `"length"` 와 `"runaway"` 를 디스패치하지 않고 각자의 알림(`OUTPUT_TRUNCATED_NOTICE`/`RUNAWAY_NOTICE`)으로 재시도시키며, 둘 다 turns.jsonl 행(`stop_reason`·`stop_detail`; length 의 detail 은 `classify_output_cap` — model_cap/context_clamp/server_cap)으로 남긴다. 각 프로바이더가 자기 원어를 매핑할 책임을 진다 — anthropic 은 `_STOP_REASON_MAP`(end_turn·stop_sequence→stop, max_tokens→length), openai 는 finish_reason 이 이미 이 어휘. 종전엔 Anthropic 원어가 그대로 흘러 루프의 출력-절단 가드(`== "length"`)가 무발화, 잘린 write_file/shell 이 디스패치됐다.

### 7.3¾ 배치·절단 안전장치 (P0-2·P0-3, v8.35.0)

- **edit 배치 훅 경유 (P0-2)**: 같은 파일 edit_file 2건+ 배치는 `ToolBridge.dispatch_edit_batch` 를 지나 단건 경로와 **동일한 훅/이력 계약**을 탄다 — PreToolUse 는 edit 별(하나라도 블록 → 배치 전체 미적용, all-or-nothing 유지), PostToolUse 1회(물리 실행 1회), `recent_tool_history` 는 edit 별(B1 가시성), 예외 안전망·문구 단건 동형. 종전엔 dispatch 가 `apply_edits_batch` 를 직접 불러 배치에서만 훅·이력이 통째로 빠졌다(훅으로 금지한 편집이 배치로는 무사통과).
- **json_fc 절단 전파 (P0-3)**: EOF 절단 증거(수리 필요 + 미닫힘 괄호를 `close_unbalanced` 로 복구)를 `Op.truncated` 로 전파 — **마지막 op 에만**(절단은 EOF 이므로 앞선 온전한 op 를 과잉 수리하지 않게). 비-절단 수리(제어문자·이스케이프·과닫힘)는 미플래그. 단수 투영(`parse`)도 xml_fc 동형으로 플래그 승계. 종전엔 기본 포맷에서 dispatch 의 edit 절단 새니타이저가 상시 무발화였다.

### 7.3⅞ 컨텍스트 회계·프론트 렌더 규약 (P0-8·P0-6②, v8.36.0)

- **P0-8a 캐시↔history 정렬 북키핑**: `_cache_hidx`(캐시 레코드별 history 서수 병행 리스트, in-memory 전용). fold 는 캐시 **중간**을 제거하는데 오프셋(`_dynamic_start_index`)은 prefix-skip 모델이라 이를 표현 못 해, 종전 `+= 1`/`+= len(evict)` 은 접힌 수만큼 과소 전진 — fold·evict 교차 후 resume 에 잉여 레코드가 재유입됐다(수렴 테스트로 실증). 이제 front-pop/compact 가 "다음 레코드의 실제 history 위치"로 정확히 전진(evict=`hidx.pop(0)+1`, compact=첫 retained 의 hidx). resume 의 fold 재적용(v4.51.0)과 합쳐져 live↔resume 뷰가 반복 사이클에서도 수렴(계약 테스트 TestFoldOffsetResumeConvergence·TestHidxAlignmentInvariant).
- **P0-8b 토큰 단위 정합(`_token_scale`)** *(v10.18.0 에서 바이트당 실측 비율 `_token_ratio` 로 교체 — 위 manager.py 항목)*: `reconcile_actual_tokens` 가 실측/추정 비율(클램프 [0.25,8])을 계산해 이후 증감(add/evict/fold/force_fit)을 환산 — 실측-앵커 카운터에서 chars/4 추정을 빼던 단위 혼합(CJK 과소 추정 → 과잉 evict) 수리. 추정-기반 재계산(compact 재빌드·resume 로드) 시 1.0 리셋(basis 전환). scale 1.0 이면 산술 바이트 동일(기존 계약 보존).
- **P0-6② 프론트 el/elHtml 규약**: `el()` 3번째 인자 = **textContent**(기본이 안전 — 원문 그대로 넘겨도 마크업 실행 불가), HTML 은 `elHtml()` 로만 명시(escapeAndFormat 마크다운·colorizeDiffBody·highlightDangerHtml 등 의도 자명한 소스 한정). 종전 innerHTML-기본은 콜사이트별 escapeHtml 규율이 갈려 누락=self-XSS 였다. 텍스트 사이트의 기존 escapeHtml 은 제거(이중 이스케이프 방지).

### 7.4 Thinking 적용

**게이트 (v8.21.0)**: `supports_thinking` 단독. 이 키가 false 인 모델에는 사고 관련 필드를 **일절 싣지 않는다**(v8.21.1 — 비추론 모델에 `thinking` 블록/`chat_template_kwargs` 를 보내 엄격 백엔드에서 400 나는 것 방지). 공용 함수 `providers.base.thinking_switch(caps, flag)` 가 미지원이면 None, 지원이면 세션 값을 돌려준다.

**스위치 (v10.72.0, docs/knobs/DESIGN.md §8)**: 세션 노브 🧠 `thinking` 은 `on`/`off` 둘뿐이고 기본 `off`(`ctx.thinking: bool`, `CallSettings.thinking: bool`). 정도(`reasoning_effort`·`budget_tokens`)는 **보내지 않는다** — 모델마다 키·어휘·접는 방향이 달라(GLM 템플릿은 `medium` 을 `Max` 로 읽었다, 실측) 하니스가 번역하면 조용히 틀린다. 켰을 때 얼마나 사고하는지는 서버·모델 기본값의 몫.

| 프로바이더 | `thinking=False`(기본) | `thinking=True` |
|-----------|------------------------|-----------------|
| OpenAI 호환 | `chat_template_kwargs.enable_thinking=false` — **항상 명시**(Qwen 템플릿은 스위치가 없으면 사고를 켠다, v9.24.8; 문법 제약도 `<think>` 가 열려 있는지를 가정한다) | `chat_template_kwargs.enable_thinking=true` |
| Anthropic | 블록 없음(모델 기본 — `disabled` 는 현행 모델 일부가 거절) | `thinking: {type: adaptive}`(`_THINKING_ON`; `budget_tokens` 는 현행 Claude 가 400) · `max_tokens` 불변 |

종전(v8.37.0~v10.71.0) 의 `ThinkingPolicy`/`resolve_thinking_policy`/`_EFFORT_TO_BUDGET` 와 `auto`(서버 기본에 맡김) 선택지는 삭제됐다 — 사용자 결정(2026-10-25).

Thinking 블록 처리 플로우:
1. thinking 모델 → `<think>...</think>` 블록을 텍스트에 출력
2. 각 플러그인 stage 0(`strip_thinking`)이 블록 분리 (thinking_tags 단일 소스)
3. 분리된 thinking 내용은 `ParsedAction.thinking`에 보존
4. 나머지 텍스트(JSON)만 파싱 → Stage 1 직접 성공률 향상

### 7.5 재시도 헬퍼 (`providers/http.py`)

두 프로바이더 모두 동일한 재시도 래퍼 `post_with_retry(requests.post, url, **kwargs)`를 거쳐 HTTP를 발송합니다. 목적은 on-prem LLM 서버(vLLM, LM Studio, omlx)에서 간헐적으로 발생하는 일시적 네트워크 오류 — 서버 재시작 직후의 `ConnectionError`, 첫 호출 시 모델 로딩이 늦어서 발생하는 `Timeout` — 을 사용자 레벨로 노출하지 않고 복구하는 것입니다.

**범위: pre-stream only.** `requests.post()` 호출 자체에서 발생한 예외만 재시도합니다. 스트리밍이 시작된 이후(즉 `requests.post(stream=True)`가 Response를 돌려준 뒤) 청크를 읽다가 발생한 오류는 재시도 대상 아님 — 이미 소비된 청크가 중복되면 LLM 출력이 깨지기 때문.

**재시도 대상 예외:**
- `requests.Timeout` (ConnectTimeout, ReadTimeout 포함)
- `requests.ConnectionError`

**재시도 대상 HTTP 상태 (5.14.1):** 일시적 게이트웨이 5xx — **502/503/504** — 는 재시도 **함**(네트워크 예외와 **독립 예산**, `retry_statuses` kwarg 기본 `{502,503,504}`). on-prem LLM 앞단 리버스 프록시(nginx/caddy)가 업스트림 재시작·과부하 중 내는 오류라, 직접 연결 시의 `ConnectionError` 재시도와 같은 취지로 짧게 재전송하면 대개 회복. 에러 응답은 `r.close()` 후 처음부터 재전송(resume 없음). 나머지(4xx, bare 500)는 재시도 안 함 — `raise_for_status_with_body`가 `post_with_retry` 반환 *뒤에* 호출되어 서버의 거절 응답을 그대로 caller로 전달. **5xx 예산 소진 시엔 raise 하지 않고 마지막 에러 응답을 그대로 반환** → caller가 기존과 동일하게 body 포함해 표면화(context-overflow 400 인식 경로 무영향).

**재시도 대상 HTTP 상태 — 속도 제한 (v10.25.0):** **429**(표준)와 **529**(Anthropic overloaded)는 세 번째 독립 예산 `_RATE_LIMIT_ATTEMPTS`(8)로 재전송한다. 대기는 `retry_after_seconds(r)`(정수 초·HTTP-date 둘 다, 음수는 0, 상한 `_RETRY_AFTER_CAP`=120초)가 값을 주면 그것, 없으면 `_rate_limit_delay` 의 지수열 2·4·8·…·64초(`_RATE_LIMIT_BASE_DELAY`·`_RATE_LIMIT_MAX_DELAY`). 5xx 예산과 합치지 않는 이유는 `_BROKEN_STREAM_ATTEMPTS` 와 같다 — 1초 간격 3회로는 제한 창을 넘기지 못한다. 공유 게이트웨이(LiteLLM) 뒤에서 벤더 키를 나눠 쓰는 환경이 대상이며, 종전엔 4xx 로 분류되어 첫 429 에 턴 실패 → 런 종료였다. 상태 줄은 `LLM rate limited (HTTP 429) — retrying in 17s (2/8)`.

**백오프:** 네트워크 에러·5xx 는 고정 1초 (지수 아님). on-prem 단일 사용자 전제라 rate-limit / thundering-herd 대책이 필요 없고, `ConnectionError` 직후 서버 부팅 마무리에만 약간의 헤드룸을 주면 충분. `Timeout`은 이미 긴 대기였으므로 추가 대기 효과는 작지만 해롭지도 않음.

**설정:** 고정 상수 (env 조정 불가 — v5.17.0 에서 env 제거, 테스트는 `http` 모듈 상수 monkeypatch).
- `_DEFAULT_ATTEMPTS` (10, 최초 포함 총 시도 횟수) — Timeout / ConnectionError 전용
- `_DEFAULT_STATUS_ATTEMPTS` (3, 최초 포함) — 502/503/504 전용, `_DEFAULT_DELAY` 공용
- `_DEFAULT_DELAY` (1.0초, 네트워크 에러·5xx 공용)
- `_RATE_LIMIT_ATTEMPTS` (8, 최초 포함) — 429/529 전용; 간격은 `Retry-After` 또는 `_RATE_LIMIT_BASE_DELAY`(2초)부터 두 배씩 `_RATE_LIMIT_MAX_DELAY`(64초)까지, `_RETRY_AFTER_CAP`(120초)
- **Timeout 프로파일 2개** (requests `(connect, read)` 튜플): 비스트리밍 `LLM_API_TIMEOUT=(30,1200)` (post 가 전체 body 읽음 → read=전체생성 idle 상한, 느린 cold 27B 보호). 스트리밍 `LLM_STREAM_TIMEOUT=(30,30)` — post 는 헤더만 읽으므로 read=30 이 **헤더 대기 + 헤더 구간 interrupt 바운드**(broken 서버 ~20분 행 제거). **단일 소켓 timeout 이 헤더·body read 둘 다 지배**(5s post timeout→iter_lines 5s 예외, 실측)하므로, 헤더 수신 후 **`make_stream_patient(r, 1200)`** 가 urllib3 소켓 timeout 을 patient 로 재설정(best-effort, 실패 시 30s 가 body backstop). body stall 은 폴링-루프 idle 감지가 소유: **`interruptible_lines(idle_threshold=30, max_idle_ticks=20, on_idle=...)`** — 30초 무토큰마다 `on_idle`(UI 대기 알림), 20틱(10분) 연속 침묵이면 r.close()+**`StreamIdleTimeout`** raise → 토큰 오면 카운터 리셋. provider(openai)의 스트리밍 콜이 `StreamIdleTimeout` 을 잡아 **재연결+재전송**(생성 재시작, partial 폐기 — 서버 resume 없음), `STREAM_MAX_RECONNECTS=3` 회 후 propagate. interrupt 는 idle 보다 우선(0.2초 폴링) — body 구간 ~8초, 헤더 구간 ≤30초.

**가시성:** 재시도 시 `render_status("running", ...)` 한 줄로 사용자에게 표시(예: `LLM request failed (Timeout) — retrying (2/3)`). spinner는 계속 돌아감. 모두 실패하면 `render_status("error", ...)` 후 마지막 예외를 그대로 raise. verbose 모드에서는 `agent_cli.verbose.debug_log`로 stderr에도 한 줄 남김.

**테스트 호환:** `post_with_retry`는 `post_fn`을 인자로 받고, 각 프로바이더는 자기 네임스페이스의 `requests.post`를 명시적으로 넘깁니다. 덕분에 기존 테스트가 `agent_cli.providers.{name}.requests.post`를 패치하는 패턴이 그대로 동작.

### 7.6 공용 debug 유틸 (`verbose.py`)

`agent_cli/verbose.py`가 `--verbose` 기록과 `debug_log()`의 단일 소유자입니다(provider 레이어가 loop 를 역참조하지 않도록 분리). **v9.24.3 부터 verbose 는 화면이 아니라 파일로 간다**: 종전엔 원문·사고·컨텍스트 덤프를 CLI 화면에 찍거나 웹에선 아무도 받지 않는 일회성 SSE 로 흘렸고, 하위 에이전트는 플래그를 물려받지 못해(러너가 `verbose=False`) 사후 분석이 불가능했다(보드 1zfgc2: 상주 에이전트의 실패 9건, 원문 없음). 이제 메인 루프의 `_setup` 이 `configure(ctx.session_dir)` 로 전역 기록기를 켜고(하위 루프는 끄지 않는다), 코어가 응답마다 `llm_call` 한 줄 — 원문 옆에 디스패처의 판정(`TurnDispatcher.last_outcome`: parse_stage·failure_signal·primitives·ops) — 을 쓴다. 턴 번호는 **호출 시점** 값(형식 재시도가 턴을 되감으므로). `turns.jsonl` 은 여전히 항상-켬 구조 로그이며 모델 텍스트를 담지 않는다.

---

### 7.7 턴 기록 `turns.jsonl` (`recovery/observability.py`)

세션 디렉터리(에이전트는 각자)에 턴마다 한 줄, `--record-turns`(기본 켬). **구조 메타데이터만** — 프롬프트·응답 본문·사고·도구 인자·결과·경로는 싣지 않는다(본문은 `failures.jsonl`·`verbose.jsonl` 의 몫). 읽는 쪽은 모르는 키를 무시한다. 원칙(v10.129.0): **"안 쟀다" 와 "0" 을 구별한다** — 못 잰 값은 키를 쓰지 않는다(집계는 필드마다 `n` 을 센다). 버전·방언은 세션 메타가 아니라 줄마다 적는다 — 세션은 버전을 넘어 resume 되고, 웹은 런마다 recorder 를 새로 만든다.

| 필드 | 뜻 | 없을 때 |
|---|---|---|
| `model` · `timestamp`(ISO UTC) · `parse_stage` | 모델 이름·기록 시각·파서 단계(0 실패, 1~3) | 항상 |
| `failure_signal` | `FAILURE_*` 상수 값(`NO_JSON`·`OUTPUT_TRUNCATED` …) | 정상 턴 `null` |
| `primitives_applied` | 적용한 회복 primitive 이름(`foreign_parse:<방언>` 포함) | `[]` |
| `input_tokens` · `output_tokens` · `cache_read_input_tokens` · `cache_creation_input_tokens` | `TokenUsage` 그대로 — input 은 캐시를 뺀 값, 입력 합은 셋의 합 | usage 없으면 0 (v8.49.0 모양 유지) |
| `stop_reason` · `stop_detail` | 잘린 턴만(`length`·`incomplete_tool_call`·`server_error`·`runaway`); detail 은 상한 종류·감지 규칙, **서버 오류는 서버 문구(자유 문장)** | 디스패치한 턴 `null` |
| `harness_version` (v10.129.0) | `agent_cli.__version__` | 항상 |
| `dialect` (v10.129.0) | 이 루프의 방언 이름 | 항상 |
| `reasoning_tokens` (v10.129.0) | `output_tokens` 안에 든 추론 몫 (v10.128.0) | usage 없으면 키 없음 |
| `ttft_ms` (v10.129.0) | 요청 → 첫 토큰, 클라이언트 측정(`usage.ttft_ns`, 스트리밍만) | 못 쟀으면 키 없음 |
| `llm_ms` (v10.129.0) | `provider.call` 벽시계 — 공급자 안의 재시도·재연결 포함, 요청 조립 제외 | 항상(루프가 쓴 줄) |
| `output_chars` · `thinking_chars` (v10.129.0) | 공급자가 돌려준 본문·사고 글자 수(잘린 턴의 부분 글 포함, prefill 제외; 서버가 구조로 준 `tool_calls` 의 인자는 본문이 아니라 세지 않는다) | 항상, 0 가능 |
| `tool_ms` (v10.129.0) | `_dispatch_turn` 벽시계 — **사람의 승인(confirm) 대기 포함**(승인은 도구 안쪽 `renderer.confirm` 에서 기다려 따로 떼지 않는다) | 디스패치 없는 턴(잘린 턴) 키 없음 |
| `tools` (v10.129.0) | `[[이름, 성공, ms], …]` — 브리지를 거친 실행 한 번 = 한 줄(같은 파일 편집 묶음·`agent` 병렬 묶음도 한 줄, 훅이 막은 호출은 없음). 이름은 도구 이름 그대로(MCP 포함), ms 는 실행만 감싼 벽시계 | 실행이 없던 턴 키 없음 |

**내보내기 `agent-cli stats` (v10.130.0, `recovery/stats.py`)**: 다른 머신의 기록에서 숫자만 들고 나오는 길. 이 표의 줄만 읽고(`history.jsonl`·`failures.jsonl`·`session.json`·verbose·prompt_views 는 열지 않는다) 문자열은 닫힌 어휘로 접는다 — 어휘 밖(MCP·스킬 도구 이름, 모르는 신호, 서버 오류의 `stop_detail` 문장, `shape_fallback` 의 머리말 뒤 글)은 `other`. 경로·세션 id·제목은 내지 않고(경로 모양 모델 이름은 마지막 요소만) 날짜는 일 단위. JSON 은 `schema: 1`·실행한 `harness_version`·`contains`(이 파일에 든 것 한 줄)·`files_read`·`lines_read`·`lines_skipped`·`lines_filtered`·`groups[]`(세션·턴·세션당 턴 분포·첫날/끝날, 토큰 합·캐시 적중률·추론 비율과 `n`, `ttft`/`llm`/`tool` ms 의 n·중앙·p90·합과 LLM:도구 비, `parse_stage`·`failure_signal`·`stop_reason`·`stop_detail` 개수와 잘린 턴 수, 도구별 호출·실패·ms 합·중앙, 압축 횟수·토큰 전후 중앙·ms 중앙/p90·모양·사유·FIFO 개수). 새 필드가 없는 옛 줄은 그 항목의 분모에서 빠진다.

압축 이벤트 줄(`event: "compaction"`)은 `timestamp`·`tokens_before`·`tokens_after`·`evicted_count`·`fallback_used`·`failure_signal`·`duration_ms`·`summary_shape`·`shape_fallback` 에 같은 `harness_version`·`dialect` 를 싣는다. 쓰는 자리: 디스패치한 턴은 `loop/dispatch._handle_text_path` 의 `finally`, 잘린 턴 셋은 `loop/core` 의 `_on_output_truncated`·`_on_server_error`·`_on_runaway`, 압축은 `context/manager`.

## 8. 설정 시스템

### 8.0 config.json (프로바이더/모델 설정)

```json
{
  "provider": "openai",
  "base_url": "http://127.0.0.1:8000/v1",
  "api_key": "",
  "default_model": "gpt-4o"
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

**SetupWizard** (`setup.py`): 설정 파일이 없으면 자동 실행 — 대화형이고 `--base-url`·`-m`(또는 env)로 다 주지 않았을 때만 (v10.125.0, `main._maybe_setup`).
`agent-cli setup`으로 수동 재설정 가능.

**DIRECTIVE.md** — 프로젝트 지시사항 (`prompts/system_prompt.py`):
```
.agent-cli/DIRECTIVE.md   →  프로젝트별 규칙 (우선 로드)
~/.agent-cli/DIRECTIVE.md →  사용자 전역 규칙
```
- 둘 다 존재하면 모두 로드 (content hash 중복 제거)
- content hash 중복 제거, truncation 없음 (ResourceLoader 기반)
- scope 라벨은 위치 기반(`[project, user]`); cwd == home 이면 두 경로가 같은 파일로 resolve → path-dedup 으로 1회만 로드, project 로 라벨
- 매 세션 시작 시 system prompt 동적 영역에 주입

### 8.1 models.json 구조

```json
{
  "models": {
    "<model_id>": {
      "provider": "anthropic | openai",
      "context_window": 32768,
      "max_output_tokens": 4096,
      "supports_structured_output": true,
      "supports_thinking": true,
      "supports_strict_schema": false
    }
  },
  "provider_defaults": {
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

### 8.4 능력치 조회 우선순위 (`providers/capabilities.py`)

1. `models.json` 정적 설정 (병합된 결과)
2. 런타임 감지 → **`~/.agent-cli/models.json`에 자동 저장**
   - OpenAI 호환: `/v1/models` (`max_model_len`) + overflow probe fallback (context window) + `/chat/completions` (thinking 프로브)
3. `DEFAULT_CAPABILITIES` (context_window=256K `DEFAULT_CONTEXT_WINDOW`·max_output 2048, 모든 기능 비활성 — 일부러 낙관적: 실제가 작으면 첫 호출 거절 → `force_fit` 축소·재시도, 옛 4096 은 관찰을 거의 다 잘랐다)

프로브는 진행 콜백을 받아 첫 실행 시 어느 단계가 돌고 있는지 사용자에게 표시 (`set_progress_callback`). 한 번 감지된 결과는 `_auto_detected: true` 마커와 함께 저장되어 재실행 시 프로브 생략.

### 8.6 Thinking 감지 방식

하드코딩 패턴 매칭이 아닌 **프로브 기반 감지** (`transport.thinking_probe()`):
1. 모델에 "Say hello." 프롬프트 전송
2. 두 가지 위치에서 thinking 확인 (OpenAI transport 는 `reasoning_content`/`reasoning` 필드를 `<think>` 로 정규화해 태그 검출로 통합):
   - `reasoning_content` 필드 (OpenAI 호환 — vLLM reasoning 파서)
   - `<think>`, `<thinking>`, `<reasoning>`, `<reflection>` 태그 in content (DeepSeek-R1 등)
3. **2단계 재프로브 (v8.22.0)**: 1차(기본)에서 미검출이면 OpenAI transport 가 `chat_template_kwargs.enable_thinking=true` 로 **재프로브** — Qwen 처럼 사고가 **기본 OFF** 이고 chat_template 스위치로 켜지는 모델을 잡는다. 재프로브 실패(서버가 kwarg 거부 등)는 관용(→ 미지원). Anthropic 도 같은 2단(v8.37.0 부터 — 재프로브는 `thinking` 블록 `_THINKING_ON`); v10.114.0 부터 2단 본문은 `_ProbeTransport.thinking_probe` 한 곳이고 스위치 모양만 전송별 `_body(enable_thinking=True)`.
4. 감지되면 → `supports_thinking=True`
5. 결과를 `~/.agent-cli/models.json`에 저장 (`_auto_detected: true`) → 다음 실행 시 프로브 불필요
6. 모델 업데이트 시 자동 감지 항목은 재감지로 갱신됨 (수동 등록 항목은 보호)

새 모델이 추가되어도 코드 수정 없이 자동 감지됩니다. **주의**: 이미 저장된(과거 감지된) 엔트리는 재감지 전까지 갱신 안 됨 — 개선 이전에 `supports_thinking=false` 로 잘못 저장된 모델은 엔트리를 지워 재감지하거나 admin 에서 수동 수정해야 반영된다.

OpenAI 호환 서버(vLLM 등)에서는 `/v1/models` API로 context window도 감지합니다 (`max_model_len` 필드). 메타데이터에 없으면 overflow probe fallback으로 결정.

### 8.5 모델 정보 출력

| 상황 | 출력 |
|------|------|
| 새 모델 감지 + 저장 | Rich Panel (상세 — context, thinking, tool calling 등) |
| 기존 모델 로딩 | 한 줄 요약 (`● Model: name (ctx=N, thinking=✓)`) |

---

## 9. 시스템 프롬프트 아키텍처 (`prompts/system_prompt.py`)

LLM attention 패턴에 최적화된 섹션 순서 — Primacy(앞), Middle(중간), Recency(끝):

```
build_system_prompt(capabilities, active_tools, skill_stack, session_id, agent_role, agent_registry, ...)
    │
    │  ── Primacy: 정체성 + 핵심 원칙 (강한 attention) ──
    │
    ├─ ROLE_PROMPT (항상 포함 — 에이전트 역할 정의)
    │
    ├─ CONTEXT_DISCIPLINE (항상 포함 — 컨텍스트 창이 핵심 리소스임을 교육)
    │   └─ "읽을 것만 읽어라 / thought 간결 / 불필요한 덤프 금지"
    │
    ├─ TASK_GUIDELINES (작업 원칙 14개 — **v8.52.0 부터 세션-상태 꼬리 블록으로 렌더 이동**, 상수는 여기 단일 소스로 유지; **v10.28.0**: v8.51.0 의 백업 줄("copy the original somewhere safe")은 위치를 안 정해 백업이 소스 옆(main 3/3)이나 즉흥 위치로 갔고 Harbor db-wal-recovery 에서는 `.agent-cli/tmp/` 백업본에 다시 sqlite3 를 열어 둘 다 날렸다 — 별도 줄을 지우고 Scratch files 줄에 편입(백업도 `.agent-cli/tmp/`, 소비 명령 전에 거기로 복사) — 백업 과제 3종×3회 실측 main 대 후보: `.agent-cli/tmp/` 백업 7/9→9/9 런, 소비 명령 뒤에 늦게 백업 2→1, 평균 op 9.8→8.7, 18런 전부 정답·WAL 보존; **v10.27.0**: 임시 파일은 `.agent-cli/tmp/`(상대 경로) — 임시 파일 과제 8종×3회 실측에서 시스템 임시 경로 사용 15/24→1/24 런, 봉쇄 거절 13→3, 트리 찌꺼기 14→8 런; 절대 경로 변형은 그 경로가 셸 명령의 63% 에 `cd /abs && ` 로 번지고 op 가 두 배라 기각 — Environment 의 상대 경로 규칙과 한 목소리여야 한다; v8.51.0 에서 벤치 실측 2개 흡수: ① 입력 파일을 변경/소비할 수 있는 명령 전 원본 백업 — Harbor tb21 db-wal-recovery 에서 백업 없는 sqlite3.connect 가 WAL 을 소비하는 실수 2/2 재현, ② 긴 심사숙고 대신 작은 검증 스텝 — 사고 폭주로 시간 예산 소진 2/2 재현. 벤치 템플릿이 아니라 제품 프롬프트에 두는 이유: 측정 대상은 agent-cli 자체여야 하고(벤치 템플릿의 태스크 유래 힌트는 오버피팅), 일반 원칙은 모든 사용자에게 유효)
    │   └─ 코드 읽기 선행, 범위 제한, 보안, 정직한 보고 등
    │
    ├─ FORMAT_RULES (항상 포함 — JSON ReAct 포맷 + 규칙 10개)
    │   └─ 재귀 금지, 단일 액션 강제,
    │      효율적 액션 선택 (batch 필드 활용 / shell 파이프라이닝 / 좁은 read 모드 우선)
    │
    │  ── Middle: 레퍼런스 (필요시 참조) ──
    │
    ├─ Available Tools (active_tools + _ALWAYS_INCLUDE)
    │   └─ 순서 = 레지스트리 순서(`effective_tool_names`, v10.13.0): 같이 쓰는 도구끼리
    │      — 파일(read→edit→write→code_index) · 실행(shell·fetch·monitor·schedule)
    │      · 기억(history·memory) · 사람/에이전트(ask/answer·message/reply·agent·
    │      run_skill) · complete. 호출자가 준 목록의 순서는 무시, MCP 도구는 내장 뒤
    │   └─ 가이드가 해당 도구에 inline (별도 섹션 없음):
    │       - edit_file ← Hashline Guide
    │       - agent ← Delegation Guide (v10.26.0: 의존 제약 + 예시만 — context/profile/tools 설명은 파라미터 한 곳)
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
        ├─ "Call stack: main → agent:coder → skill:plan"
        ├─ "Do not run or invoke: coder, plan (already in call stack)"
        └─ 세션 내 변동 가능한 유일한 Recency 섹션 → 끝에 두어 앞 3개를 안정적
           KV prefix로 보존
    
    Role 선택 (Primacy 영역):
    - main: 기본 ROLE_PROMPT
    - 서브에이전트(run/spawn): 프로파일 본문이 기본 Role을 대체
    - skill: parent의 Role 상속
```

---

## 10. 테스트 아키텍처

### 10.1 테스트 분류

| 분류 | 파일 수 | 테스트 수 | 실행 방법 |
|------|---------|----------|----------|
| 유닛 테스트 | ~69 | ~2030 | `pytest tests/` |
| omlx 통합 (E2E) | 2 | ~25 | `pytest tests/ -m omlx_integration` |

**실브라우저 테스트 (`tests/browser/`, v7.12.0):** (v10.120.0: `test_stale_band.py` — 🔁 띠의 줄·자리(대기열 뒤·입력창 위)·N 갱신·Stop 이 중단 핸들에 닿음·숨김, 두 번째 발화의 트레이 확인(c/s 버튼·코멘트 칸 → 워커가 코멘트와 뷰어 닉네임을 받음), 묻는 중 띠 Stop = 확인에 s;) (v10.119.0: `test_open_items.py` — 📋 띠의 등장(❓ → 📋 → ⏳ 순서·닫는 법·턴 수·budget)·`✓ closed` 영수증 뒤 사라짐·예산 0 주황·전부 닫히면 숨김, 띠가 선 뒤 접속한 뷰어가 스티키로 같은 띠를 받고 행 클릭이 원문 카드로 점프; v10.60.0: `test_image_attach.py` — 📎 로 고른 PNG 가 칩이 되고 보내면 큐 아이템 `images` 에 참조로 실리는 왕복, 사용자·관찰 카드의 썸네일 `<img>` 가 `api/workspace/file` 에서 실제로 로드되는지(관찰은 접힌 행이라 펼친 뒤), `supports_vision=false` 면 📎 숨김.) (v10.45.0: `test_knob_chips.py` — 범용 노브 칩 렌더러의 계약: 표의 열 노브 → 칩 일곱+토글, 입력 한 번이 소유자까지 닿는 왕복, 잠김·묶음·두 뷰어 동기화·거부 되돌리기; `conftest.WebStack` 이 binding·registry·auditor·`renderer.knob_state` 까지 실 부트처럼 꽂는다) (v10.39.0: `test_browser_tool_live.py` — `browser` 도구의 엔진을 실 Chrome/Chromium 으로: 지연 렌더 표·버튼으로 열리는 영역·폼+드롭다운, 스레드 로컬 `http.server` 로 서빙; 단위 층 `tests/test_browser_tool.py` 는 가짜 페이지로 스레드·ref 세대·호스트 규칙·관찰 문구를 돈다) playwright + 헤드리스 chromium 으로 jsdom 유닛이 원리적으로 못 잡는 부류 — origin 당 6연결 고갈, secure context, CSS 렌더링(칩 세로 클리핑·flex ellipsis), 실 SSE 타이밍, 서버↔프런트 실계약 — 를 검증 (confirm-starvation 사가의 프런트 버그 4건이 전부 이 층에서만 잡힌 실증). `conftest.py::WebStack`=실 uvicorn 위 WebRenderer+WebServer(포트 0 임시할당) + worker 헬퍼(`start_confirm_loop`/`start_ask`, teardown 이 `push_abort` 로 블록된 worker 를 풀어 모듈-전역 interactive_lock 해제 — 테스트 간 간섭 방지). 시나리오: confirm 클릭·해결·comment·다중뷰어 409 fold, ANSWERING 왕복, 부재 중 pending replay, 헤더 칩 가시성·복사·ctx 팝오버, stall 경고 표출/정리. **카드 렌더 회귀 (`test_card_rendering.py`, v8.42.3)**: ① 관찰 헤더의 `<span class="icon">` 마크업이 `el()`(textContent)에 실려 화면에 태그가 문자로 노출(`<span class="icon">✓</span> shell`)된 실사고 — v8.36.0 el/elHtml 분리 때 놓친 콜사이트, `elHtml` 로 수리 + 아이콘이 **요소**로 렌더되는지·도구명 주입이 여전히 막히는지 DOM 으로 검증. ② 공백 없는 긴 절대경로가 `.action-detail` 박스를 가로로 넘던 것(뮤테이션 실측 **185px** 넘침) — `overflow-wrap:anywhere` 수리 + `scrollWidth<=clientWidth` 기하로 검증(짧은 경로 대조군 포함). **el() 오용 자동 탐지 (`test_web_server.TestStaticUI.test_no_markup_passed_to_text_el`)**: 소스를 파싱해 `el(` 호출 인자에 마크업 리터럴/HTML-생성 함수 결과가 있으면 실패 — 종전엔 콜사이트 전수를 손으로 감사해 이 버그를 놓쳤으므로 기계적 가드로 승격. **성능 회귀 가드 + KPI (`test_render_perf.py`, v8.42.2)**: 세션 로드(스냅샷 재생)의 렌더 병합을 **하드웨어 무관한 카운팅 불변식**으로 단언 — 240이벤트를 재생해도 `#overview` innerHTML 재작성·`#messages` scrollTop 쓰기가 프레임 수준 상수(실측 2회/1회, 상한 20; 병합 풀리면 121회/240회)여야 하고, 동시에 카드 수·개요 최신성·자동 스크롤 바닥 도달(기능 계약)과 **라이브 경로**도 함께 검증(스냅샷만 고치는 반쪽 회귀 방지). 계측은 `Element.prototype` 의 innerHTML/scrollTop setter 카운팅(MutationObserver 는 마이크로태스크 배칭으로 **과소계상** → 회귀를 놓치는 방향이라 미사용). 뮤테이션 검증: v8.41.0 app.js 로 되돌리면 3/3 실패. 벽시계 시간은 단언하지 않고 `TestRenderKpi`(`AGENT_CLI_PERF_KPI=1`)가 측정만 해 `docs/PERF-KPI.md` 표로 추세 관리 — 절대 임계는 머신 종속이라 느린 CI 오탐 ∧ 빠른 머신 무력의 이중 실패이기 때문. 정적 소스 핀 `test_web_server.TestRenderCoalescing`(직접 호출 부활 금지)과 상호 보완. **옵트인**: `AGENT_CLI_BROWSER_TESTS=1` 아니면 루트 conftest `collect_ignore=["browser"]` 로 **수집조차 안 함**(per-item skip 만으로는 pytest-asyncio 가 수집 단계 이벤트루프를 남겨 이후 async e2e 를 깨뜨림 — 실측 수리). CI 는 별도 `browser` 잡(chromium install). **omlx 통합 테스트:** `tests/test_integration_omlx.py` + `tests/test_integration_omlx_builtin.py`. 실 OpenAI 호환 omlx 서버를 대상으로 `run_loop`(질문/read/shell/write/edit/multi-step), `provider.call` ReAct 파싱, 런타임 capability 감지, 스킬 실행(fork·dynamic injection·allowed_tools·디렉토리 구조·bracket args), 훅(Pre/PostToolUse), agent run(none/fork), code-analyst 프로파일, plan 스킬, @<profile> dispatch를 검증. conftest fixtures(`omlx_provider`, `integration_model`, `model_capabilities`)는 서버 `/v1/models` 프로브로 가용성을 확인하고, 미가용 시 전부 skip → `pytest tests/`는 항상 green. 연결은 env(`OMLX_BASE_URL` 기본 `http://127.0.0.1:8000/v1`, `OMLX_API_KEY`, `INTEGRATION_MODELS` 기본 `Qwen3.6-27B-MLX-8bit`)로 override. 순수 로딩/프롬프트 검증은 유닛(test_builtin_skills/agents)에 있어 통합에서는 제외.

### 10.2 테스트 실행

```bash
# 전체 (유닛; 통합은 서버 미가용 시 자동 skip)
pytest tests/ -v

# 특정 모듈
pytest tests/test_dialects_json_fc.py -v

# omlx 통합 E2E (실 서버 필요)
pytest tests/ -m omlx_integration -v
```

**수트는 저장소 루트에 쓰지 않는다 (v10.130.3)**: `tests/conftest.py` 의 autouse 가드가 테스트 앞뒤로 저장소 루트와 `.agent-cli/` 아래 두 단계의 항목을 찍어(`tests/cwd_guard.py`, 루트는 conftest 위치에서 — cwd 아님) 새 항목이 생기면 그 테스트를 실패시킨다(빼는 것은 `__pycache__`·`.pytest_cache`·`.ruff_cache`·`.agent-cli/skills` 뿐). 저장소에 쓰는 테스트는 `monkeypatch.chdir(tmp_path)` 나 자기 격리 픽스처로 돈다 — 가드가 못 보는 삭제·이동(`rm`·`mv` 를 실제로 실행하는 위험 셸 확인 테스트)도 임시 디렉터리에서. hypothesis 는 예제 DB 를 끄고(`database=None` 프로필) 홈(수집 시점 캐시 `constants/`·`unicode_data/`)을 수트 임시 디렉터리로 옮긴다. 가드는 테스트 밖(종료 시 `atexit`)의 쓰기는 못 본다 — 그건 수트 뒤 `git status --short --ignored` 로 본다.

---

## 11. CLI 명령어 레퍼런스

### 11.1 `run` — 단발 실행

```bash
agent-cli run "task description" [options]
  -p, --provider    openai | anthropic    (기본: openai)
  -m, --model       모델 ID                       (기본: 프로바이더 기본값)
  --base-url        API 엔드포인트
  --api-key         API 키 (환경 변수 자동 감지)
  -n, --max-turns    최대 턴 (0=무제한)
  --max-depth       서브에이전트 중첩 깊이 (기본: 2)
  -v, --verbose     원시 LLM 응답 표시

  /sh <cmd>         LLM 없이 셸 명령 직접 실행
```

`run` 도 `web` 과 동일하게 세션/컨텍스트(compaction + FIFO fallback + history.jsonl + compaction.json)를 관리합니다. 완료 후 세션 ID가 출력되며 `web --resume <id>`로 이어서 작업할 수 있습니다 (compaction state는 `dynamic_start_index`로 복원되어 summarised tail과 중복 없음).

### 11.1½ `stats` — 본문 없는 집계 (v10.130.0)

```bash
agent-cli stats [PATH …] [--since YYYY-MM-DD] [--json] [--out FILE]
```

PATH 기본은 `paths.sessions_dir()`. 모델 서버·설정이 필요 없다. 종료 코드 0(0건 포함)·2(인자 오류·없는 PATH·직접 준 PATH 가 심볼릭 링크 `Not following a symbolic link: …`·`--out` 쓰기 실패 `Cannot write …` — 트리 안에서 만난 링크는 조용히 건너뛴다). 계약과 집계 항목은 §7.7.

### 11.2 `web` — 대화형 브라우저 UI

```bash
agent-cli web [options]
  (run 옵션 + --host/--port/--token/--no-browser/--resume/--idle-timeout/--trust-local/--base-path). **`--base-path <prefix>`(경로 prefix 라우팅)**: 리버스 프록시가 `/<prefix>/*` → 이 인스턴스(+prefix strip)로 라우팅할 때 — 프론트 URL 전부 **상대경로**(`api/...`/`static/...`)이고 `index()` 라우트가 serve 시 `<base href="<prefix>/">` 주입(기본 `<base href="/">`=루트, 동작 byte-동일). 서버 routes 무변경(프록시 strip→`/api/...` 수신). 회귀 가드 `test_web_base_path.py`(serve 프론트에 절대 `/api`·`/static` 0). **`--trust-local`(loopback 토큰 면제)**: 신뢰된 로컬 게이트웨이(127.0.0.1 바인드 인스턴스를 프록시·인증) 뒤에서 게이트웨이가 토큰을 매 요청 주입 안 하게 — pure-ASGI **`_AuthMiddleware`**(유일 인증 집행자, **default-deny**) 가 인증을 전담. **`/api/*`는 `/api/health` 빼고 전부 인증 필요**(`_needs_auth`), `/`·`/static`만 공개(`_is_public_path`). 인증 = trust-local(`is_trusted_client`=trust_local AND peer∈{127.0.0.1,::1}) **또는 유효 `act` 쿠키 또는 유효 `?token=`**. 미인증 보호경로는 **미들웨어가 401**(엔드포인트 미실행) — 등록 안 된 `/api` 경로도 401 이라 **새 라우트가 구조적으로 자동 보호**(엔드포인트는 인증 코드 0, `token: Query`·`_require_token` 제거). **C-5 쿠키 핸드셰이크**: 유효 부트스트랩 `?token=` 는 응답에 `Set-Cookie act`(HttpOnly·SameSite=Strict·Path=base_path·TLS 면 Secure)를 실어 → 브라우저는 이후 쿠키로 인증하고 토큰이 URL 에서 사라짐(주소창·Referer 누출 차단, 모든 응답 `Referrer-Policy: no-referrer`). 서버는 `?token=` 도 계속 수용(부트스트랩·curl·툴링) — 브라우저가 더는 emit 안 하므로 누출 무증가. `--trust-local` off·비-loopback+무쿠키 는 미인증 → 401. 브라우저 자동 오픈은 `_is_local_bind(host)`(loopback/wildcard) 일 때만 — 특정 IP(원격 bind)면 생략하고 URL 만 출력(서버에서 브라우저 무의미). **`--idle-timeout N`(self-reap)**: 외부 오케스트레이터(게시판류)가 인스턴스를 온디맨드로 띄우고 회수 안 하게 — N초 동안 비활성이면 스스로 종료(다음 접속 `--resume` 재기동). 순수 결정 로직 `web/idle.py::IdleMonitor`(clock 주입, 단위테스트) + web() 의 데몬 폴링 스레드가 `tick()` → `server_obj.should_exit=True`(기존 finally 가 teardown+세션저장). **활성(=안 죽임) = `renderer.hub.has_live_connections()` OR `renderer.worker_is_busy()` OR `server.pending_count()>0`** — busy(작업/질문대기) 면 mid-task 회수 안 함. 기본 0=비활성(하위호환). **인스턴스 파일 (`web/instance_file.py`)**: web 시작 시 `.agent-cli/sessions/<id>/web.json`(`{session_id, host, port, token, pid}`)을 기록하고 finally 에서 제거 — 외부 오케스트레이터가 "이 세션 web 떠 있나/어디로" 를 파일 하나로 알아 spawn-or-attach(pid 죽었으면 stale→재spawn). 순수 write/read/remove(서버 의존 0). idle-timeout(self-reap)+제거가 짝이라 오케스트레이터는 프로세스 추적·kill 불필요. **라이브 상태 사이드카 `status.json`(`{busy, awaiting_input, viewers}`)**: web.json(준정적 핸드셰이크)과 분리된 별도 파일로, 오케스트레이터가 `GET /api/health` 를 폴링하는 대신 **파일 read** 로 라이브니스를 읽게 한다. `WebRenderer` 가 viewer 등록/해제·busy 토글(worker_state sticky)·awaiting 토글(input_required sticky) 변화마다 원자적(**유니크 temp**(`tempfile.mkstemp`)+`os.replace`)으로 재기록 — 스냅샷만 lock 으로 잡고 디스크 I/O 는 lock 밖(재진입·블로킹 회피). 에이전트-루프 스레드와 웹 스레드가 동시에 쓰므로 tmp 는 반드시 write 별로 유니크해야 함(고정 tmp 는 `os.replace` 경합→`FileNotFoundError` 크래시, v4.27.1 수정). 시작 시 seed·finally 에서 제거(web.json 과 짝). `_STATUS_STICKY_KEYS={worker_state,input_required}` 만 재발행(무관 sticky 는 스킵).

  # 웹 명령어 (handle_slash_command + 공유 dispatch):
  /help              명령어 안내
  /sh <cmd>          LLM 우회 셸 실행
  /compact           수동 컨텍스트 compaction
  /skills            스킬 목록
  /<skill> <args>    스킬 실행
  @agents            에이전트 목록
  @<agent> <task>    에이전트에 위임
```
다중 뷰어 (모두 동등 — 모두 입력·큐 가능). 상세는 server.py / web.py 엔트리 참조.

**허브와 렌더러 (v10.115.0)**: 연결·버퍼·스티키·별명은 허브(`render/web_hub.py::EventHub`, `renderer.hub`), 이벤트 모양·스코프·재생·status.json 은 렌더러(`WebRenderer`). 렌더러의 `_emit` 이 이벤트 하나의 와이어 모양(task_id·note·hidx·ts)을 만들고 한 번 직렬화해 `hub.publish` 에 넘기면, 허브가 영속이면 재생 버퍼에 쌓고 열린 연결 전부의 큐에 넣는다. 스티키는 허브가 슬롯을 들고(새 연결 스냅샷에 재생) 렌더러 래퍼가 "이 슬롯이 status 를 뒤집는가"(`_flips_status`)를 판단한다. 서버(`stream_events`·`/api/nickname`·`/api/health`·종료 훅)와 `main.py`(idle 술어·finally)는 허브만 쓴다. 뷰어 수가 바뀌면 허브가 `on_viewers_changed` 를 부르고 렌더러가 거기 `_publish_status` 를 건다. 락은 둘(렌더러 `_lock`, 허브 `_lock`)이고 함께 잡는 자리가 없다 — `_publish_status` 는 렌더러의 busy·auditing, 허브의 스티키 사본, 허브의 뷰어 수를 각자 락 아래 떠서 합친다(status.json 은 전이마다 다시 쓰는 "마지막 쓰기가 최신" 거울이라 셋 사이 원자성이 필요 없다; 종전에도 쓰기는 락 밖이었다).

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
  "provider": "openai",
  "context_window": 16384,
  "max_output_tokens": 4096,
  "supports_structured_output": true,
  "supports_thinking": false,
  "supports_strict_schema": false
}
```

미등록 모델은 런타임 감지(OpenAI 호환) 또는 보수적 기본값으로 동작합니다.

### 12.4 새 dialect 추가

ReAct 외 새 응답 형식(예: PREFIX-MD 마크다운, OpenAI 스타일 tool call,
실험용 multi-action 등)을 추가하려면 `agent_cli/dialects/`에 새 모듈 한 개를
만들면 됩니다. **main code path(loop.py / system_prompt.py / recovery/)는 수정하지
않습니다** — 분기점이 `DialectBase` ABC 안에 격리되어 있기 때문입니다.

ABC가 lifecycle / 식별 hook의 default를 제공하므로 plugin은 **format-specific
abstract method만 구현**하면 됩니다. 나머지는 자동 작동.

1. `agent_cli/dialects/<name>.py` 생성:
   ```python
   from agent_cli.dialects.base import ParsedAction, DialectBase

   class MyFormat(DialectBase):
       name = "my_format"

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
       # def sanitize_thought(self, thought) -> str | None: ...       # default identity
       # def render_action_input(self, action_input) -> str: ...      # default identity
       # serialize_assistant_for_history / render_assistant_from_history /
       # format_rules도 base default 사용 가능
   ```

2. `agent_cli/dialects/__init__.py`의 `_register_builtin_plugins()`에 등록 추가:
   ```python
   from agent_cli.dialects.my_format import MyFormat
   register(MyFormat())
   ```

3. `tests/test_dialects_<name>.py`에 동작 테스트 추가
4. 사용:
   ```bash
   agent-cli run "task" --dialect my_format
   ```

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
- `windows-dump` — Windows 크래시 덤프 분류 (`kdump.py` 동봉, 모델 호출 가능, v10.105.0)
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
- **Agent-local shell hook**: 에이전트 정의 파일(`.agent-cli/agents/*.md`) frontmatter의 `hooks:` 섹션 — 해당 프로파일로 run/spawn 되는 동안만 적용되는 로컬 matcher. skill과 동일한 merge 계약: `merge_hooks_configs(parent, agent.hooks)`로 부모 훅 뒤에 덧붙여 fire.
- **agent 전파**: run 엔진(`tool_delegate`)이 `hooks_config`를 subagent `run_loop`에 그대로 전달 (상주 spawn 도 runtime 경유 동일). 즉 전역/프로젝트/스킬 훅은 모두 상속되고, 에이전트 자신의 overlay까지 그 위에 얹힘.

### 14.2 라이프사이클 이벤트 (11개)

| 이벤트 | 시점 | 함수명 |
|--------|------|--------|
| OnSessionStart | 세션 시작 후 | `on_session_start(ctx)` |
| PreLLMCall | LLM 호출 직전 (매 턴) | `pre_llm_call(ctx)` |
| PostLLMCall | LLM 응답 수신 후 | `post_llm_call(ctx)` |
| PreToolUse | 도구 실행 직전 | `pre_tool_use(ctx)` |
| PostToolUse | 도구 실행 직후 | `post_tool_use(ctx)` |
| OnTurnEnd | 턴 종료 후 | `on_turn_end(ctx)` |
| OnAgentStart | agent run 실행 직전 | `on_agent_start(ctx)` |
| OnAgentEnd | agent run 완료 후 | `on_agent_end(ctx)` |
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
  │   │   ├─ OnAgentStart / OnSkillStart
  │   │   ├─ 도구 실행
  │   │   ├─ OnAgentEnd / OnSkillEnd
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
6. **모듈 수준 import 순환 0** — 위층을 올려다보는 지연 import 는 이름 붙인 목록으로만 둔다 (§3.2, `tests/test_import_graph.py`)
