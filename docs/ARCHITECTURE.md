# Agent-CLI 아키텍처

> 이 문서는 **지금의 구조를 그린 지도**다. 이력은 git log·릴리스 노트에, 설계 근거는 `docs/<영역>/DESIGN.md` 에 있다.
>
> - 지금의 구조만 적는다. 줄 수, 파일·테스트 개수, 버전 표기, 날짜, "종전엔 …"·"…에서 바뀌었다" 류의 이력 서술을 적지 않는다.
> - 고칠 때: 파일이 생기거나 없어지거나 이름·역할이 바뀔 때, 의존 방향이 바뀔 때, 데이터 구조·흐름이 바뀔 때. 커밋마다 고치는 문서가 아니다.
> - `tests/test_architecture_doc.py` 가 지키는 것:
>   §2 트리의 경로가 전부 있다 · `agent_cli/` 의 `.py` 와 `web/static/` 의 `app.js`·`js/*.js` 가 전부 §2 에 있다 · §2 의 줄은 200자 이하이고 줄 수 표기가 없다.
> - `tests/test_import_graph.py` 가 지키는 것: 모듈 수준 import 순환 0 · 순환을 피하는 지연 import 는 테스트의 이름 붙은 목록과 같다(§3).
> - 문서 전체에 `v<숫자>.<숫자>` 꼴 버전 표기가 없고, 백틱 안의 저장소 경로(`….py`·`….js`·`docs/….md` 등)는 실제로 있다 — `tests/test_architecture_doc.py`.

---

## 1. 프로젝트 개요

Agent-CLI는 on-premise LLM을 위한 모듈형 에이전트 CLI입니다. ReAct(Reasoning + Acting) 패턴으로 LLM이 도구를 사용하여 단계적으로 작업을 수행합니다.

### 핵심 특징

- **멀티 프로바이더**: Anthropic Messages API, OpenAI 호환 `/chat/completions`(vLLM, LM Studio, omlx 등)
- **방언(dialect)**: 모델이 도구를 부르는 와이어 모양을 스펙(데이터)으로 정의하고 엔진 하나가 렌더·파싱·문법·구제를 만든다. 텍스트 방언(`json_fc`·`xml_fc`·`hermes_json`·`glm_argkey`)과 서버 네이티브 함수 호출(`native_fc`) (§5.2·§5.3)
- **Hashline 편집**: 줄마다 CRC32 기반 2글자 태그(`LINE#HASH`)로 가리키는 편집, 불일치 시 주변 줄과 새 ref 를 돌려준다 (§6.4)
- **컨텍스트 관리**: 매 LLM 호출 직전 목표치를 넘으면 LLM 요약 압축, 부족하면 FIFO 축출, 호출 뒤 서버 실측으로 재조정, 서버가 컨텍스트 초과로 거절하면 축소 후 재시도 (§5.4)
- **모델 적응형**: 컨텍스트 창·사고·문법·vision 지원을 models.json 또는 런타임 프로브로 알아낸다 (§4.2)

### 외부 의존성

하한은 `pyproject.toml` 이 정한다.

| 패키지 | 용도 |
|--------|------|
| `typer` | CLI 프레임워크 |
| `rich` | 터미널 렌더링 |
| `requests` | HTTP 클라이언트 (LLM API 호출) |
| `pyyaml` | 스킬·프로파일 frontmatter 파싱 |
| `tree-sitter` | code_index 파서 코어 |
| `tree-sitter-python` / `-javascript` / `-typescript` / `-cpp` / `-go` / `-rust` / `-java` | code_index 언어 grammar |
| `tree-sitter-markdown` | code_index markdown 제목 색인 |
| `pysqlite3-binary` | code_index SQLite 폴백 (Linux only — `sqlite3` 없는 CPython 대비) |
| `pillow` (선택, `[image]`) | 이미지 입력 — 보내기 전 축소·재인코딩. 없으면 원본을 그대로 보내고 5MB 를 넘는 것만 거절한다 |

**Optional**: `[web]` → `fastapi` / `uvicorn[standard]` / `sse-starlette`. `[browser]` → `playwright` (`browser` 도구).
**Dev**: `pytest`, `pytest-asyncio`, `httpx`, `hypothesis`, `playwright`, `xgrammar`, `jsonschema`, `pillow`. `[macos]` → `gnureadline`.

**하한 원칙**: 핀은 우리가 쓰는 API 가 들어온 버전이다. 어떤 조합에서 깨지는 전이 의존성 때문에 직접 핀을 올리지 않는다. CI `min-deps` 잡이 Python 3.10 + `uv pip install --resolution lowest-direct` + `.github/constraints-min-deps.txt`(하한이 스스로 못 막는 전이 패키지만)로 유닛 수트를 돈다.

**시스템 패키지**: C/C++ 색인의 `unifdef` 단계는 번들된 pure-Python(`code_index/_unifdef.py`)이 처리한다. 시스템 `unifdef` 바이너리가 있으면 그것을 쓴다.

---

## 2. 디렉토리 구조

파일 하나 = 한 줄 = 지금 그 파일이 하는 일. 디렉터리 줄에는 한 구절 설명과, 있으면 설계 문서. 경로·누락·줄 길이는 `tests/test_architecture_doc.py` 가 지킨다.

```
agent_cli/
├── __init__.py              패키지 버전(`__version__`) 한 줄
├── __main__.py              `python -m agent_cli` 진입점
├── main.py                  typer CLI — `run`·`web`·`setup`·`mcp`·`knobs`·`sessions`·`stats`·`update` 명령과 `@agent`·`/skill` 디스패치
├── runtime.py               run/web 세션 조립(`SessionSpec`→stores→owners→`wire_session`)·정리, `MainLoop`
├── wake.py                  한 주소를 무엇이 깨우나(`wake_sources`)·체크인 알림·메일 → 레코드·`release_owner`, 루프의 우편 창구 `Mailbox`(포트 `mailbox`)
├── pump.py                  세션 펌프 — 큐에서 꺼내 깨우기·라우팅·런·정지 판정. 수명 정책과 표면만 호스트가 꽂는다 (docs/pump/DESIGN.md)
├── command_table.py         사용자 명령 표 하나 — 웹 `/help` 와 `run "/help"` 가 여기서 나온다
├── config.py                config.json·models.json 레지스트리 읽기·저장, 프로바이더 기본값, 방언 바인딩(`bind_user_dialect`)
├── setup.py                 첫 실행 설정 마법사 `SetupWizard` (Rich TUI — 프로바이더·연결·모델)
├── model_check.py           부팅 때 모델 이름 검증 — "없는 모델"(`ModelNotFound`)과 "확인할 수 없음"을 가른다
├── knobs.py                 세션 노브 선언 `KNOBS` 하나에서 env·CLI 플래그·web 칩 표면을 파생 (docs/knobs/DESIGN.md)
├── constants.py             공용 상수와 작은 판정 함수(`parse_duration`·`format_duration`·`open_debts`·env 노브 읽기)
├── paths.py                 `.agent-cli` 경로의 단일 소스 — 프로젝트·유저·세션·그림 보관소·스크래치 디렉터리, 모델에게 보이는 경로 표기(`display_path`)
├── fsio.py                  파일 저장 패턴 셋의 소유자 — 원자 교체(`atomic_write_*`)·가드 append(`append_line`)
├── resource_loader.py       여러 경로에서 frontmatter 있는 .md 를 우선순위로 찾는 `ResourceLoader` (스킬·에이전트·지시사항)
├── extras.py                선택 의존성(extra) 설치 안내 — 빠진 패키지 이름을 설치 메타데이터에서 읽어 직접 댄다
├── net_errors.py            `requests` 예외 사슬 순회와 TLS 사유 한 줄 — 도구 층·공급자 층이 같이 쓴다(표준 라이브러리만 import)
├── token_estimator.py       토큰 추정(chars/4)과 `utf8_len` — import 없는 잎
├── instance_file.py         세션별 `web.json`·`status.json` 쓰기·읽기·지우기 — 서버와 렌더러가 같이 쓴다(`fsio` 만 import)
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
├── session_slots.py         세션 슬롯 다섯 — 잡·모니터·예약 레지스트리, 훅 러너, 에이전트 레지스트리의 프로세스 전역 자리(세션 끝에 비운다)
├── verbose.py               `--verbose` 기록기 — 모든 루프의 LLM 호출을 `<session>/verbose.jsonl` 에
├── default_models.json      패키지 기본 모델 정의 (models.json 검색 경로의 마지막)
├── default_web_sources.json 기본 정보원 목록 (`web_sources.py` 가 읽는다)
├── loop/                    루프 엔진 — `AgentLoop` 와 협력자들, 루프 종류 (docs/loop-kinds/DESIGN.md)
│   ├── __init__.py          패키지 표식(docstring) — 재수출하지 않는다, 서브모듈을 직접 import
│   ├── kind.py              `LoopKind` 기본 틀 — 종류가 정체·포트·ctx·스코프·결과 포장을 결정, `PrivateLoop`·`Caller`·`Start`
│   ├── run.py               `run_loop(kind, start, ctx)` — `AgentLoop(...).run()` 의 함수 꼴, 종류가 부르는 엔진 이음매
│   ├── core.py              `AgentLoop` 오케스트레이터 — 런·턴 루프, 입력·메일 흡수, 응답 마감(절단·폭주·서버 오류)
│   ├── state.py             `LoopConfig`(런 시작 때 계산한 것)와 `LoopState`(협력자들이 공유하는 per-run 가변 상태)
│   ├── ports.py             루프에 주입되는 능력 포트 묶음 `LoopPorts` — 기본값 없음, 비움 사유·훅 포트·세션 자원 확인 (docs/wiring/DESIGN.md)
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
│   ├── credentials.py       `Credentials` — API 키, 요청 인증 헤더를 만드는 유일한 곳(`headers(provider)`)
│   ├── http.py              HTTP 재시도·SSE 스트림 읽기·재접속·연결 오류 분류
│   ├── runaway.py           스트리밍 경로의 내용 무관 폭주 감지기 `RunawayDetector`
│   ├── openai.py            `OpenAIProvider` — `/v1/chat/completions` 호환 서버(vLLM·LM Studio·omlx)
│   └── anthropic.py         `AnthropicProvider` — Anthropic Messages API
├── context/                 컨텍스트·세션 영속화 (docs/context-compaction/DESIGN.md)
│   ├── __init__.py          `ContextManager`·`is_context_overflow` 재수출
│   ├── manager.py           `ContextManager` — history.jsonl·인메모리 캐시·예산 압축·서버 실측 reconcile
│   ├── render.py            record → LLM 메시지 렌더, 예산 추정, 압축 요약 입력 (무상태)
│   ├── store.py             세션 영속화 디스크 I/O primitive(history 추가·compaction.json·fork)
│   ├── exchanges.py         history 의 사용자 요청↔답 쌍 — 세션 목록·resume 의 요약(방언 알림 접두를 거른다)
│   └── overflow.py          프로바이더별 컨텍스트 초과 감지·`ContextOverflowError`
├── session/                 세션 디렉터리 저장소 — 맨 아래 층, 위로 아무것도 import 하지 않는다
│   ├── __init__.py          패키지 표식(docstring) — 아무것도 내보내지 않는다
│   ├── store.py             세션 디렉터리 배치·메타 `session.jsonl`·목록·resume
│   ├── records.py           디스크 레코드 모양의 단일 리더·분류기(ops·개입 종류)
│   └── lock.py              한 세션 한 프로세스 — `session.lock` flock
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
│   ├── registry.py          `TOOLS` 인스턴스화, 도구 설명 렌더, 입력 검증, 액션 추론, 기록에서 건드린 파일 뽑기
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
│   └── registry.py          `MonitorRegistry` — 조건 평가 스레드, 설치한 주소로 보고 배달
├── schedule/                세션 소유 예약 실행 (docs/schedule/DESIGN.md)
│   ├── __init__.py          패키지 표식(docstring)
│   ├── cron.py              5필드 cron 식 엔진(의존성 0)
│   ├── registry.py          `ScheduleRegistry` — 예약 소유·발화 스레드, `schedules.json`, 놓친 발화는 질문으로
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
│   ├── shell.py             셸 훅(`hooks.json`) — PreToolUse·PostToolUse·PostToolUseFailure
│   └── builtin/             패키지 내장 Python 훅
│       ├── __init__.py      내장 훅 관례(docstring)
│       └── 10_snapshot.py   워크스페이스 스냅샷 훅 — 세션 시작 안내, 변경 도구 직전 체크포인트
├── skills/                  프롬프트 스킬 (Claude Code 호환 포맷)
│   ├── __init__.py          로더 쪽만 재수출(`Skill`·`load_skills`·`model_skills`) — 실행기는 `skills.executor` 에서
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
| 7 | `runtime` · `subagent/` · `skills/` · `harness/` |
| 6 | `loop/` · `__main__` |
| 5 | `main` · `recovery/` |
| 4 | `context/` · `prompts/` · `providers/` (한 묶음) |
| 3 | `dialects/` · `mcp/` · `web/` |
| 2 | `tools/` · `render/` · `pump` · `setup` |
| 1 | `session/` · `config` · `hooks/` · `images` · `memory` · `monitor/` · `schedule/` · `verbose` · `input_queue` · `input_history` · `browser` · `web_sources` · `instance_file` · `wake` |
| 0 | `paths` · `fsio` · `constants` · `item_ids` · `thinking_tags` · `net_errors` · `jobs` · `snapshot` · `code_index/` · `knobs` · `command_table` · `extras` · `model_check` · `resource_loader` · `token_estimator` · `agent_icon` · `session_slots` · `agents/` |

`context/`·`prompts/`·`providers/` 는 패키지 단위로 서로를 모듈 수준에서 import 한다(모듈 단위로는 순환이 없다).
그래서 한 층의 한 묶음으로 적는다. `main` 은 명령 함수 안에서 지연 import 하는 것이 많아(`runtime`·`loop/` 등) 층이 낮게 나온다.

### 3.2 규칙

- **모듈 수준 import 순환은 0 이다.** `tests/test_import_graph.py::test_no_module_level_import_cycle` 이 지킨다.
- **순환을 피하려고 지연인 import** — 대상 모듈이 모듈 수준 import 만으로 출발 모듈에 닿는 간선 — 는 테스트 안의 이름 붙은 목록
  `CYCLE_AVOIDING_LAZY` 와 정확히 같아야 한다(`test_cycle_avoiding_lazy_imports_are_the_named_list`). 간선 목록 자체는 테스트가 주인이다.
  남은 것은 하나, 재귀인 `loop.kind` → `loop.run` 이다(종류가 엔진을 돌리고 엔진은 `agent`·`run_skill` 도구로
  다시 종류를 만든다 — 기본 엔진은 호출 시점에 읽는다). 종류·엔진이 읽는 세션 상태는 조립(`runtime`)이 아니라
  아래층의 자리에서 온다 — 세션 슬롯 `session_slots`, 깨우기·메일·거두기 `wake`, 포트 비움 사유·세션 자원 `loop.ports`.
  `loop/__init__.py` 는 아무것도 재수출하지 않는다 — 재수출하면 `loop.kind` 만 쓰는 세션 쪽이 패키지 머리를 거쳐 엔진 전체를 끌고 와
  `loop.core`·`loop.dispatch`·`loop.tool_bridge`·`loop.skill_invoke` 의 세션 쪽 지연 import 까지 순환 회피가 된다.
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
    reasoning_tokens: int = 0             # output_tokens 안에 든 추론 몫
    total_input_tokens (property)         # input + cache_creation + cache_read

@dataclass
class LLMResponse:
    content: str                          # 텍스트 응답
    tool_calls: list[dict] | None = None  # 서버가 파싱해 준 함수 호출
    usage: TokenUsage | None = None
    stop_reason: str | None = None        # 루프 어휘 (§7.2)
    stop_detail: str = ""                 # 잘린 이유·감지 규칙
    thinking: str = ""                    # provider-side reasoning 채널
```

`thinking` 채널: Anthropic 은 `thinking` 블록(스트리밍 `thinking_delta`), OpenAI 호환은 `reasoning_content`. 채널이 없으면 `""`.
content 안의 `<think>…</think>` 태그는 별개다 — 방언의 stage 0(`strip_thinking`, `thinking_tags.py` 단일 소스)이 `ParsedTurn.thinking` 으로 분리한다.

토큰 칸: OpenAI 경로는 `providers/openai._usage_counts` 한 함수가 usage 를 읽는다 — `prompt_tokens` 에서 `cached_tokens` 를 뺀 것이 `input_tokens`,
적중분이 `cache_read_input_tokens`, `reasoning_tokens` 는 `output_tokens` 에서 빼지 않는다. 그래서 `total_input_tokens` 는 서버의 `prompt_tokens` 와 같다.

### 4.2 모델 능력치 (`providers/capabilities.py`)

```python
@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int
    max_output_tokens: int
    supports_thinking: bool               # 사고 스위치를 보낼지의 단독 게이트
    supports_grammar: bool | None = None  # 서버가 디코딩 문법을 강제하는가 (None = 미확인)
    supports_vision: bool | None = None   # 이미지를 보는가 (None = 미확인 — 처음 필요할 때 프로브)
    temperature: float | None = None      # 등록된 샘플링 값
    top_p: float | None = None
```

능력치 조회 우선순위 (`get_capabilities`):
1. `models.json` 등록값
2. 런타임 감지 `_detect_capabilities(model, transport)` — 프로바이더가 `capability_transport` 로 전송(끝점·인증·본문 모양)을 주고,
   흐름은 `_ProbeTransport` 한 곳이 소유한다. 감지 결과는 `max_output = context // 4` 로 만들고, 창이 `MIN_CONTEXT_WINDOW`(16K) 미만이면 `UnsupportedModelError`.
   감지 결과는 `~/.agent-cli/models.json` 에 `_auto_detected: true` 로 저장된다.
3. `DEFAULT_CAPABILITIES` — 창 `DEFAULT_CONTEXT_WINDOW`(256K)·출력 2048, 기능 전부 끔. 창은 일부러 낙관적이다: 실제가 더 작으면
   첫 호출이 서버에서 거절되고 `ContextManager.force_fit`(§5.4 flow 2)이 줄여 재시도한다.

컨텍스트 창 감지 3단 (`_ProbeTransport.context_window()`):
1. `_metadata_window()` — `/models` 메타데이터의 `max_model_len` → `context_length`.
2. `_overflow_window()` — 상한을 넘는 prompt 를 보내 400 본문에서 상한 숫자를 읽는다(`parse_overflow_amounts`). 토크나이즈 직후 거절되므로 생성이 돌지 않는다.
3. `_DEFAULT_CONTEXT_FALLBACK`(128K) — 둘 다 숫자를 못 주면.

첫 실행 프로브는 `constants.DETECTION_PROBE_TIMEOUT` 을 공유한다. vision 은 등록값이 없으면 `vision_support` 가 프로세스당 한 번 프로브하고
(두 색 PNG 를 보내 순서대로 답하는지), 판정이 나면 models.json 의 빠진 키에 적는다.

### 4.3 파서 결과 — `ParsedTurn` · `Op` · `ParsedAction` (`dialects/base.py`)

방언이 돌려주는 경계 타입이다. 루프는 방언과 무관하게 이 타입만 소비한다.

```python
@dataclass
class Op:                      # 호출 하나
    action: str | None
    action_input: dict | str | None
    truncated: bool            # 절단 복구로 닫았다
    rewritten_from: dict | None  # 도구를 바꾸는 층(`loop/reroute.py`)이 바꿨으면 원래 호출

@dataclass
class ParsedTurn:              # 응답 하나
    thought: str | None
    ops: list[Op]
    terminal: bool             # 종결 op(`complete` 등)로 끝났다
    raw: str
    parse_stage: int           # 0 파싱 실패, 1 그대로 읽힘, 2 구제해서 읽힘
    thinking: str | None
    record: dict | None        # history 에 저장할 assistant 레코드(`TurnDispatcher._handle_text_path` 가 한 번 만든다)
    split_calls: bool          # 호출이 산문으로 나뉜 두 군데 이상에 있었다(형식 거부로 되묻는다)

@dataclass
class ParsedAction:            # 단수 투영 — thought·action·action_input·raw·parse_stage·thinking·truncated
```

### 4.4 도구 추상화 표면 (`tools/base.py` + `tools/registry.py`)

각 도구는 `Tool` 서브클래스 하나가 스키마(`name`/`description`/`parameters`)·검증·실행(`_run`)을 소유한다.
`tools/registry.py` 가 `_ALL_TOOLS` 를 한 번 인스턴스화해 `TOOLS` 를 만들고, 시스템 프롬프트의 도구 설명·입력 검증·액션 추론을 그 하나에서 파생한다.
`TOOLS` 의 순서가 프롬프트의 도구 순서다(`effective_tool_names`, MCP 도구는 내장 뒤).

- **광고 조건 `requires_handler`**: 루프에 그 자원이 없으면 도구가 프롬프트에서 빠진다(예: `ask`→`ctx`, `browser`→`browser`, `snapshot`→`snapshot`, `view_image`→`vision`, `run_skill`→`skills`, `wait`→`wake`, `reply`→`questions`).
  세션 자원은 `loop.ports.browser_resource`·`snapshot_resource`·`skills_resource` 가 답한다.
- **지연 설명 `deferred`**: 지연 도구는 프롬프트에 한 줄 요약과 인자 뼈대만 싣고, 전문은 `tool_help` 가 관찰로 준다 (docs/tool-help/DESIGN.md).
- **`RunContext`** (frozen): 루프가 도구 호출마다 아는 값(`session_dir`·`oversized_cap` 등)을 한 객체로 넘긴다. `ToolBridge._run_ctx()` 가 단일 생성점이다.
- **가상 도구** (`tools/virtual.py`): `complete`·`wait`·`ask`·`message`·`reply`·`run_skill` 은 스키마만 있고 루프가 이름으로 가로채 처리한다.

---

## 5. 핵심 플로우

### 5.1 ReAct 에이전트 루프 (`loop/` — `AgentLoop`)

엔진은 하나이고, 들어가고 나오는 길은 루프 종류(`LoopKind`, `loop/kind.py`)가 정한다 — **종류가 결정하고 엔진이 돈다** (docs/loop-kinds/DESIGN.md).

| 종류 | 자리 | 무엇 |
|------|------|------|
| `MainLoop` | `runtime.py` | 세션의 main — 펌프가 요청마다 `run(start)` |
| `ResidentLoop` | `subagent/resident_loop.py` | 상주 에이전트 — 받은편지함 묶음마다 |
| `OneShotLoop` | `subagent/oneshot.py` | `agent` `run` 의 일회성 서브에이전트 (`PrivateLoop`) |
| `SkillLoop` | `skills/executor.py` | `run_skill`·`/skill` |
| `HarnessLoop` | `harness/jobs.py` | 하니스 작업(감사자) (`PrivateLoop`) |

종류가 정체(주소·깊이·스택·바인딩·도구·훅 설정·포트·정지 이벤트), ctx 생성, 스코프 열기·닫기, 결과 포장, 정리, 턴 경계에서 받을 것(`intake`)을 정한다.
엔진 진입점은 `run_loop(kind, start, ctx)`(`loop/run.py`) = `AgentLoop(kind, start, ctx).run()`. `AgentLoop` 는 협력자에게 일을 나눈다:
`SystemPromptSvc`(`loop/prompt.py`) · `LLMCaller`(`loop/llm.py`) · `TurnDispatcher`(`loop/dispatch.py`) · `ToolBridge`(`loop/tool_bridge.py`),
공유 상태는 `LoopConfig`·`LoopState`(`loop/state.py`), 장부는 `Ledger`(`loop/ledger.py`).

#### 루프 플로우

```
AgentLoop.run()
    ├─ (graceful_interrupt) _install_signal_handler()   ← Ctrl+C 를 플래그 + stop_event 로
    ├─ _setup()                                         ← 시스템 프롬프트, 첫 사용자 메시지
    ├─ OnSessionStart 훅 → 주입된 시스템 섹션 반영
    ├─ while _should_continue():                        ← stop_event 가 서면 _interrupted
    │    ├─ _interrupted → _on_interrupt()
    │    ├─ _intake()            ← 종류의 intake(): 입력 큐·메일·받은편지함 항목을 흡수
    │    ├─ show_open_items()    ← 장부를 화면에 (main 런만)
    │    └─ _execute_turn()
    │         ├─ 지시사항·메모리 변경 반영, PreLLMCall 훅
    │         ├─ LLMCaller.call → LLMResponse (§5.4 flow 1·2)
    │         ├─ tool_calls: 서버 파싱 방언이면 ops 로, 아니면 텍스트로 렌더해 이어 붙임
    │         ├─ PostLLMCall 훅
    │         └─ _finish_response → 잘림(length)·폭주·서버 오류는 각자의 처리로, 그 밖은 TurnDispatcher 로
    │              └─ 파싱 → op 순회(가드·같은 파일 edit 묶음·연속 `agent` run 병렬 묶음) → 관찰 기록 → OnTurnEnd
    └─ finally: 장부 표시, StaleGuard 정리, OnSessionEnd 훅, 시그널 핸들러 복원
```

**정지는 턴 경계에서.** 신호는 `stop_event`(threading.Event) 하나다. CLI 는 첫 Ctrl+C 가 `_interrupted` 와 `stop_event` 를 세우고 두 번째가 `KeyboardInterrupt` 다.
web 의 Stop 은 `POST /api/stop` → `WebServer.trigger_stop()` → 펌프 표면이 `set_stop_handle` 로 걸어 둔 이벤트를 세운다.
`_on_interrupt` 는 `{role: user, tool: "interrupt", content: INTERRUPT_NOTICE}` 관찰 레코드를 남긴다.

#### 저장과 표현

history.jsonl 은 구조화 레코드, LLM 에 가는 것은 방언이 렌더한 메시지다.

| 메시지 | 저장 |
|--------|------|
| 사용자 입력 | `{"role":"user", "content":"..."}` |
| assistant 턴 | 멀티-op `{"role":"assistant", "thought":…, "ops":[{"action", "action_input"}, …]}` 또는 단수 `{"action", "action_input"}` |
| 도구 결과 | `{"role":"user", "tool":…, "content":…}` |

assistant 레코드를 읽는 곳은 `session/records.py::iter_record_ops` 하나다(두 모양 모두).

### 5.2 프로바이더별 도구 호출 방식

방언이 정한다. 프로바이더 코드는 요청에 `tools` 를 싣느냐만 다르다.

- **텍스트 방언** (`json_fc`·`xml_fc`·`hermes_json`·`glm_argkey`): 모델이 본문에 호출을 쓰고 하니스가 파싱한다. 서버가 문법을 강제할 수 있으면
  (`supports_grammar`) 방언의 EBNF(`dialects/grammar.py`)를 `guided_grammar` 로 싣는다(OpenAI 호환만 — Anthropic 은 받을 곳이 없다).
- **서버 네이티브** (`native_fc`, `server_parsed`): 요청에 함수 스키마(`tools`, `tool_choice: "auto"`)를 싣고 서버가 파싱한 `tool_calls` 를 받는다.
  루프는 `ops_from_server_calls` 로 op 를 만든다 (docs/dialects/NATIVE.md).
- 텍스트 방언인데 서버가 `tool_calls` 를 돌려주면 루프가 그 방언의 모양으로 렌더해 본문 뒤에 붙인다(`_render_server_tool_calls`).

방언은 모델에 묶인다: `--dialect` 강제 > models.json 엔트리의 `dialect` 키 > 없으면 `DialectUnbound` (`dialects.resolve_dialect`). 기본값은 없다.

### 5.3 단계적 파싱 폴백 (`dialects/engine.py` + `dialects/recovery/`)

엔진의 `parse_turn` 파이프라인 (자세한 것은 docs/dialects/PHASE5.md):

```
0. strip_thinking (thinking_tags 단일 소스)
1. 인용 영역 — 균형 ``` 쌍 + 인라인 `…` (recovery.quote_spans) — 산문 언급·펜스 예시는 호출 후보에서 뺀다
2. 트리거/앵커 — 첫 자격 있는 호출
3. 호출 분할 — 호출 단위 세그먼트
4. 이름 — 스펙의 NameSlot
5. 인자 — 스펙의 ArgStyle (TAGGED · TAGGED_PAIR · JSON_IN_TAG · JSON_NATIVE)
→ 그대로 읽히면 parse_stage 1, 구제가 필요했으면 2, 못 읽으면 0
```

구제 기계는 둘이고 코드 공유가 없다: `dialects/recovery/json.py`(토큰 수리·진단·op 추출 — `json_fc`·`hermes_json`)와
`dialects/recovery/tagged.py`(태그 변종·closer·값 끝 — `xml_fc`·`glm_argkey`). 태그 구제의 어느 규칙을 켤지는 스펙의 `Lenient` 가 정한다.
바인딩 방언이 op 0개로 읽은 응답은 `try_foreign_parse` 가 다른 등록 방언의 파서로 한 번 더 본다.

### 5.4 컨텍스트 관리 (`context/manager.py`)

> 설계: `docs/context-compaction/DESIGN.md`. 이미지: `docs/vision/DESIGN.md`.

레코드와 요청 메시지의 `content` 는 끝까지 문자열이다. 이미지는 user 레코드의 `images: [ImageRef]`(경로·크기·sha256)로 실리고
`content` 에는 자리표시 줄 `[image: path (W×H)]` 이 들어간다. 파일은 공급자 경계(`images.expand`)에서만 읽는다.

#### 세 흐름

- **flow 1 (예방)** — 매 LLM 호출 직전 `LLMCaller` 가 목표치
  `context_window × compaction_ratio − system(실측) − 세션상태 − (1024 + 512)` 를 계산해 `ctx.ensure_within(target)` 을 부른다
  (`compaction_ratio` 기본 1.0, 범위 0.5–1.0, 끝 항은 요청의 최소 출력 몫). 캐시가 목표치를 넘을 때만:
  1. 압축이 꺼져 있거나 요약기가 없으면 `_evict_fifo(target)`
  2. 그 밖에는 압축 — 가장 오래된 절반을 축출하고 한 번의 LLM 호출로 요약한다(이전 요약과 재귀 병합). 요약 호출의 모양은 경계 모양(본 호출 요청을
     마지막 축출 메시지까지 자르고 지침을 붙임), 실패하거나 창을 넘으면 전사 모양으로 한 번 (`prompts/compaction.py`).
     결과는 `[system][summary][file_list][retained]` 로 다시 짜이고 `compaction.json` 에 원자 저장된다.
  3. 그래도 목표치를 넘거나 요약이 실패(`CompactionError`)하면 `_evict_fifo(target)`
  호출이 성공하면 `ctx.reconcile_actual_tokens(usage.total_input_tokens, …)` 가 캐시 추정을 서버 실측에 다시 맞춘다(바이트당 토큰 비율).
- **flow 0 (창에 닿은 절단)** — 출력이 요청 클램프(`max_tokens = min(등록 출력 상한, 창 − 프롬프트 추정 − 512)`, 하한 1024)에 닿아 `length` 로 끝났고 `classify_output_cap` 이
  `context_clamp` 라고 하면, `core._on_output_truncated` 가 `compact_now()` 로 그 자리에서 압축하고 재시도시킨다.
- **flow 2 (반응)** — 서버가 컨텍스트 초과로 거절한 경우.

```
provider.call() → 예외
    └─ classify_overflow(e) → (actual, limit) | None
         = ContextOverflowError(경계의 raise_for_status_with_body 가 400 + overflow 본문을 typed 로 올림)
           또는 HTTPError 이고 status ∈ {400, 413} 이고 overflow 본문
           그 밖 → None → force_fit 안 함(재전파)
         non-None & overflow_retries < _MAX_OVERFLOW_RETRIES(5)
         → target = (limit or budget) × compaction_ratio
         → ctx.force_fit(target, actual_tokens=actual): 압축 → 부족하면 비율 축출 → 아무것도 안 줄면 가장 오래된 1개
         → 줄었으면 재요청, 앵커만 남았으면 실패
```

#### 그 밖의 규칙

- **과대 도구 출력**: 관찰이 `max(context_window, MIN_CONTEXT_WINDOW) // 10`(`ToolBridge._oversized_cap`)을 넘으면 본문 대신 over-cap 관찰을 넣는다.
  정책은 `Tool.render_oversized` 하나다 — 전문은 파일로(도구가 이미 쓴 파일이 아니면 `persist_oversized` 가 `<session_dir>/oversized/` 에),
  head/tail 발췌(`oversized_excerpt`)와 좁혀 읽는 길을 함께. 경로는 `paths.display_path()` 로 cwd 상대.
- **세션 상태 블록(꼬리)**: 매 턴 바뀌는 상태를 마지막 메시지 뒤에 붙인다 (`prompts/session_state.py`, docs/tail/DESIGN.md). 첫 줄은 `TAIL_BOUNDARY`.
  Task Guidelines, `## Open Items`(장부), `## Session Memory`, `## Background jobs`, `## Web Sources`, 상주 에이전트 목록, 턴 예산, 압축 안내
  (`COMPACTION_NOTICE_STEPS`)가 여기 있다. 컨텍스트 사용량 숫자는 싣지 않는다.
- **관측**: 압축마다 `TurnRecorder.record_compaction` 이 turns.jsonl 에 `event: "compaction"` 줄을 쓴다 (§7.7).
  화면은 `render_compaction_progress` → `Renderer.compaction`(web 은 `compaction` SSE 이벤트).
- **resume**: `compaction.json` 의 `dynamic_start_index` 가 유효하면 그 뒤부터 다시 읽는다.

#### assistant 턴의 저장과 재공급

assistant 턴 하나는 세 형태를 지난다 — (A) 모델이 낸 원문 · (B) history.jsonl 의 레코드 · (C) 다음 호출에 다시 들어가는 메시지.
(A)→(B) 는 방언의 `serialize_assistant_for_history`(종결 op 는 `serialize_terminal_for_history`), (B)→(C) 는 `render_assistant_from_history` 다.
같은 세션의 다음 턴도 resume 도 (B) 에서 (C) 를 다시 만든다 — 모델이 흘린 와이어 조각은 저장 때(`sanitize_thought`) 한 번 정제되어 다시 새지 않는다.
`context/render.py` 는 user·도구 분기만 직접 처리하고 assistant 분기는 방언에 맡긴다.

#### 세션 파일 구조 (`session/store.py`)

```
.agent-cli/sessions/{session_id}/
├── session.jsonl            ← 한 줄 메타
├── history.jsonl            ← main 대화
├── compaction.json          ← 압축 상태(요약·파일 목록·dynamic_start_index)
├── turns.jsonl · failures.jsonl · verbose.jsonl   ← §7.6·§7.7
├── memory.jsonl             ← 세션 메모리
├── agents/<key>/            ← 상주 에이전트
├── run_<name>_<hash>_<ts>/  ← 일회성 서브에이전트 (history.jsonl + result.md, 중첩 가능)
├── skill_<name>_<hash>_<ts>/← 스킬 런
└── harness/<job>/<label>/   ← 하니스 작업 (세션 대화가 아니다)
```

fork 모드 서브에이전트는 부모 history.jsonl 을 복사해 이어 쓴다(`ContextManager.fork_history_to`).

---

## 6. 도구 시스템

### 6.1 등록된 도구

`tools/registry.py::_ALL_TOOLS` 의 순서 그대로다. 인자는 각 `Tool.parameters` 가 주인이다 — 모델이 받는 설명은 `python -m agent_cli.prompts.inventory` 로 본다.

| 도구 | 모듈 | 광고 조건·비고 |
|------|------|----------------|
| `read_file` | `tools/read_file.py` | 해시라인 읽기 — 줄 범위·`search`·`stat` |
| `edit_file` | `tools/edit_file.py` | 해시라인 편집 — `op` = replace·append·prepend·delete |
| `write_file` | `tools/write_file.py` | 생성·덮어쓰기, 결과는 해시라인 |
| `code_index` | `tools/code_index.py` | 지연 설명 |
| `view_image` | `tools/view_image.py` | `vision` — 모델이 이미지를 볼 때만 |
| `shell` | `tools/shell.py` | 전경 실행 또는 `background` 작업(`jobs.py`) |
| `fetch` | `tools/fetch.py` | 지연 설명 |
| `browser` | `tools/browser_tool.py` | 지연 설명 · `browser` — playwright 가 있을 때만 |
| `monitor` | `tools/monitor_tool.py` | 지연 설명 |
| `schedule` | `tools/schedule.py` | 지연 설명 |
| `snapshot` | `tools/snapshot_tool.py` | 지연 설명 · `snapshot` — 저장소가 섰을 때만 |
| `history` | `tools/history.py` | 지연 설명 — 세션 기록을 SQL 로 |
| `memory` | `tools/memory_tool.py` | 지연 설명 |
| `ask` | `tools/virtual.py` | 가상 · `ctx` |
| `message` | `tools/virtual.py` | 가상 · 지연 설명 · `message_handler` |
| `reply` | `tools/virtual.py` | 가상 · 지연 설명 · `questions` |
| `agent` | `tools/agent_tool.py` | 지연 설명 — 실행은 `subagent/agents_live.py`(상주)·`subagent/oneshot.py`(`run`) |
| `run_skill` | `tools/virtual.py` | 가상 · `skills` — 실행은 `loop/skill_invoke.py` |
| `wait` | `tools/virtual.py` | 가상 · `wake` — 깨울 것이 있을 때만 끝내지 않고 기다린다 |
| `tool_help` | `tools/tool_help.py` | `deferred` — 지연 도구가 남은 루프에만 |
| `complete` | `tools/virtual.py` | 가상 · 항상 포함(`_ALWAYS_INCLUDE`) |

MCP 도구는 `mcp/adapter.py` 가 `Tool` 로 감싸 `TOOLS` 에 등록한다.

### 6.2 agent 프로파일 로딩

`agent` 의 `profile` 은 `subagent/profiles.py` 가 찾는다 — 일회성(`run`)과 상주(`spawn`)가 같은 카탈로그를 쓴다.

```
검색 경로 (우선순위 순):
  1. .agent-cli/agents/{name}.md     (프로젝트)
  2. agent_cli/agents/builtin/       (패키지 내장)

frontmatter: description · allowed-tools · model · hooks · disable-model-invocation · auto-spawn(상주 전용)
본문: 시스템 프롬프트의 Role 을 대신한다
```

하니스 작업 프로파일(`agents/harness/`)은 이 카탈로그 밖이다(`harness/jobs.py`).
op 에 준 `tools`·`model` 이 프로파일 값보다 우선한다(`subagent/runner.apply_role_overrides`).

일회성 run 의 결과(`subagent/report.py::_format_delegate_output`, `OneShotLoop` 이 `STATUS: success|error` 로 감싼다):
1. 서브에이전트 출력(없으면 `(subagent returned no result)`)
2. `[Subagent activity]` — 턴별 액션 요약
3. `[Last actions before failure]` — 실패 시
4. `[Duration: Ns] [Subagent used N iterations]`

`DelegateResult` 필드: `output`, `iterations`(루프가 실제로 돈 턴 수), `duration_secs`, `activity_log`, `last_actions`.
run 디렉터리(`run_<name>_<hash>_<ts>/`)에 history.jsonl 과 result.md 가 남는다.

### 6.3 run_skill 결과

`SkillLoop`(`skills/executor.py`)이 호출자 세션 아래 `skill_<name>_…/` 에 돌고 결과를 result.md 로 남긴다.
- 도구: `skill.allowed_tools ∩ 호출자 도구` (공통이 없으면 거절, 스킬에 제한이 없으면 호출자 범위)
- 역할: 호출자의 역할
- 깊이: 호출자 +1 — 스킬→스킬 재귀도 깊이가 막는다
- 결과가 없으면 `Skill returned no result`, 예외는 `run_skill(<name>) failed: …`

### 6.4 Hashline 시스템 (`tools/read_file.py`)

```
원본 파일:             hashline 출력:
def hello():    →    1#VR:def hello():
    return "hi"      2#KT:    return "hi"

해시: zlib.crc32(줄, seed) & 0xFF → 2글자 태그 (알파벳 ZPMQVRWSNKTXJBYH 의 두 니블)
seed: 글자·숫자가 있는 줄 → 0, 없는 줄 → 줄 번호
```

편집(`edit_file`, 한 op = 한 편집):
```json
{"op": "replace", "pos": "2#KT", "lines": ["    return 'hello'"]}
{"op": "replace", "pos": "1#VR", "end": "2#KT", "lines": ["def greet():", "    pass"]}
{"op": "append",  "pos": "1#VR", "lines": ["    # 주석"]}
{"op": "prepend", "pos": "1#VR", "lines": ["# 헤더"]}
{"op": "delete",  "pos": "2#KT"}
```

- **같은 파일 묶음**: 연속된 같은 path 의 `edit_file` op 는 `ToolBridge.dispatch_edit_batch` → `apply_edits_batch_parts` 가 한 번 읽은 원본 기준으로
  모든 ref 를 풀고, 범위 겹침을 거부하고, 아래에서 위로 적용해 한 번 쓴다(all-or-nothing). 단건도 크기 1 의 묶음이다. 훅·이력 계약은 단건과 같다.
- **해시 불일치**: 오류에 그 줄 ±3줄(`_MISMATCH_WINDOW`)의 현재 내용과 새 ref 를 붙인다. 자동 보정은 하지 않는다.
- **줄째 붙여 넣은 ref**: `pos`/`end` 가 `67#YB:def f():` 처럼 한 줄 전체면 콜론 앞 `LINE#HASH` 만 취한다(`_parse_ref`).
- **변이 후 echo**: write_file·edit_file 은 diff 뒤에 바뀐 구간 ±3줄의 새 해시라인을 붙인다(`_change_echo.render_change_echo`, 상한 `_MAX_REGION_LINES`) —
  read_file 왕복 없이 이어서 편집할 수 있다.
- **이미지**: `read_file` 로 연 이미지는 도구를 바꾸는 층(`loop/reroute.py`)이 `view_image` 호출로 바꾼다.

### 6.5 Tool Output 전달 방식

도구 출력은 잘리지 않고 그대로 관찰이 된다(shell 포함). 예외는 과대 출력 하나다 — 관찰이 `oversized_cap` 을 넘으면
본문 대신 over-cap 관찰(전문은 파일로, 발췌와 좁혀 읽는 길)을 넣는다 (§5.4). 도구별 표면: `Tool.render_observation`·`Tool.apply_oversized_cap`·`Tool.render_oversized`.
컨텍스트 예산은 압축과 FIFO 가 맡는다. 출력을 줄이려면 도구 호출 자체를 좁힌다.

### 6.6 스키마 검증 (`tools/registry.py::validate_tool_input`)

1. 도구 존재 확인
2. `action_input` 이 문자열이면 dict 변환 시도
3. 필수 필드 확인(`missing_fields_message`)
4. 타입 검증 + 자동 변환(`_try_coerce`): `"30"` → `30`(integer·number), `{}`·`"x"` → `[…]`(array), `42` → `"42"`(string)

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
        **kwargs,          # settings=CallSettings(...) 등
    ) -> LLMResponse: ...
```

프로바이더 경계를 건너는 호출별 설정은 `CallSettings`(frozen) 하나에 담는다 — `thinking`·스트림 무진전 타임아웃·재시도 횟수·`max_output_tokens`(클램프)·
`grammar`·`tools`(native_fc 함수 스키마)·`sampling`·`blocked_images`. 루프가 매 호출 스냅샷을 만들어 넘긴다.

### 7.2 프로바이더별 구현

| 프로바이더 | 엔드포인트 | 인증 | 문법 | 사고 스위치 |
|-----------|-----------|------|------|-------------|
| `AnthropicProvider` | `/messages` | `x-api-key` + `anthropic-version` | 없음 | 켬 = `thinking: {type: adaptive}`(`capabilities.ANTHROPIC_THINKING_ON` — 프로브와 같은 모양), 끔 = 블록 없음 |
| `OpenAIProvider` | `/chat/completions` | Bearer | `guided_grammar` | `chat_template_kwargs.enable_thinking` — 끔도 명시 |

OpenAIProvider 하나로 OpenAI·vLLM·LM Studio·omlx 를 `--base-url` 만 바꿔 덮는다. native_fc 면 요청에 `tools`·`tool_choice: "auto"` 를 싣는다.

**stop_reason 은 루프 어휘다**: `"stop"`(정상) · `"length"`(출력 절단) · `"incomplete_tool_call"`(`STOP_INCOMPLETE_TOOL_CALL`) · `"server_error"`(`STOP_SERVER_ERROR`) ·
합성 `"interrupted"`·`"runaway"`·`"degenerate_runaway"`(`providers/runaway.py`·방언 감지기가 스트림을 닫음). 원어 매핑은 각 프로바이더의 책임이다
(Anthropic `_normalize_stop_reason`: `end_turn`→`stop`, `max_tokens`→`length`). 루프는 잘린 턴을 디스패치하지 않고 각자의 알림으로 되묻는다
(`OUTPUT_TRUNCATED_NOTICE`·`RUNAWAY_NOTICE`·`SERVER_ERROR_NOTICE`). 잘린 턴은 turns.jsonl 에 `stop_reason`·`stop_detail` 로, 본문은 failures.jsonl 에 남는다.

### 7.3 프로바이더 팩토리 (`providers/__init__.py`)

각 프로바이더 모듈이 import 될 때 `register_provider` 로 스스로 등록한다. 내장 목록은 `_BUILTIN_MODULES`(openai·anthropic).
`create_provider(name, base_url, credentials)` 가 이름으로 클래스를 찾고, 프로바이더 클래스는 능력 프로브 전송을 `capability_transport` 로 소유한다.
키는 `main._resolve_provider` 에서 `Credentials` 로 감싸여 provider·`ModelBinding.credentials`·능력 프로브·`model_check`(/models) 로 하나로 흐르고,
요청 헤더(OpenAI `Authorization: Bearer` / Anthropic `anthropic-version` + `x-api-key`)는 모든 자리가 `Credentials.headers(provider)` 로 만든다. `Content-Type` 은 POST 하는 쪽이 붙인다.

### 7.4 Thinking 적용

- **게이트**: `supports_thinking` 단독. false 인 모델에는 사고 관련 필드를 싣지 않는다 — `providers.base.thinking_switch(caps, flag)` 가 None.
- **스위치**: 세션 노브 `thinking` 은 on/off 둘, 기본 off (docs/knobs/DESIGN.md). 정도(`reasoning_effort`·`budget_tokens`)는 보내지 않는다 — 켰을 때 얼마나 사고하는지는 서버·모델 기본값의 몫.
- content 안의 사고 태그는 방언의 stage 0 이 떼어 `ParsedTurn.thinking` 으로 둔다.

### 7.5 재시도 헬퍼 (`providers/http.py`)

두 프로바이더 모두 `post_with_retry(post_fn, url, …)` 로 보내고 `stream_with_reconnect` 로 스트림을 읽는다.

| 실패 | 예산(최초 포함) | 간격 |
|------|-----------------|------|
| `requests.Timeout` · `ConnectionError` (전송 전) | `_DEFAULT_ATTEMPTS` 10 | `_DEFAULT_DELAY` 1초 |
| 502·503·504 (`_RETRYABLE_STATUS`) | `_DEFAULT_STATUS_ATTEMPTS` 3 | 1초 — 다 쓰면 마지막 응답을 그대로 돌려준다 |
| 429·529 (`_RATE_LIMIT_STATUS`) | `_RATE_LIMIT_ATTEMPTS` 8 | `Retry-After`(상한 `_RETRY_AFTER_CAP` 120초), 없으면 2초부터 두 배씩 64초까지 |
| 스트림 무진전(`StreamIdleTimeout`) | 세션 노브 `stream_max_attempts` | 재연결 |
| 종결 청크 없이 끊긴 스트림 | `_BROKEN_STREAM_ATTEMPTS` 10 | 1초 |

- 그 밖의 4xx·500 은 재시도하지 않는다 — `raise_for_status_with_body` 가 본문을 담아 올리고, 400 + overflow 본문이면 `ContextOverflowError`(§5.4 flow 2).
- 타임아웃(`constants.py`): 비스트리밍 `LLM_API_TIMEOUT`, 스트리밍 `LLM_STREAM_TIMEOUT`(헤더 대기). 헤더를 받은 뒤 `make_stream_patient` 가 본문 읽기 타임아웃을 늘린다.
- 재전송 전에는 `render_stream_reset()` 으로 부분 출력을 걷는다. 무진전 대기는 `render_stream_stall` 로 보인다.
- 연결 오류 분류(`classify_connection_error`)는 `net_errors.py` 의 예외 사슬을 본다.

### 7.6 공용 debug 유틸 (`verbose.py`)

`--verbose` 기록과 `debug_log()` 의 단일 소유자다(프로바이더 층이 루프를 역참조하지 않게). main 루프의 `_setup` 이 `configure(session_dir)` 로 켜고,
같은 프로세스의 모든 루프(서브에이전트 포함)가 `<session_dir>/verbose.jsonl` 에 LLM 호출마다 한 줄을 쓴다 — 원문과 디스패처의 판정(parse_stage·failure_signal·primitive·ops).
`scope` 는 main 세션 디렉터리 기준의 하위 루프 경로다.

### 7.7 턴 기록 `turns.jsonl` (`recovery/observability.py`)

세션 디렉터리(에이전트는 각자)에 턴마다 한 줄, `--record-turns`(기본 켬). **구조 메타데이터만** — 프롬프트·응답 본문·사고·도구 인자·결과·경로는 싣지 않는다
(본문은 `failures.jsonl`·`verbose.jsonl` 의 몫). 읽는 쪽은 모르는 키를 무시한다. **"안 쟀다" 와 "0" 을 구별한다** — 못 잰 값은 키를 쓰지 않는다.
버전·방언은 세션 메타가 아니라 줄마다 적는다 — 세션은 버전을 넘어 resume 되고, 웹은 런마다 recorder 를 새로 만든다.

| 필드 | 뜻 | 없을 때 |
|---|---|---|
| `model` · `timestamp`(ISO UTC) · `parse_stage` | 모델 이름·기록 시각·파서 단계 | 항상 |
| `failure_signal` | `FAILURE_*` 상수 값(`NO_JSON`·`OUTPUT_TRUNCATED` …) | 정상 턴 `null` |
| `primitives_applied` | 적용한 회복 primitive 이름(`foreign_parse:<방언>` 포함) | `[]` |
| `input_tokens` · `output_tokens` · `cache_read_input_tokens` · `cache_creation_input_tokens` | `TokenUsage` 그대로 — input 은 캐시를 뺀 값 | usage 없으면 0 |
| `stop_reason` · `stop_detail` | 잘린 턴만(`length`·`incomplete_tool_call`·`server_error`·`runaway`); detail 은 상한 종류·감지 규칙, 서버 오류는 서버 문구 | 디스패치한 턴 `null` |
| `harness_version` | `agent_cli.__version__` | 항상 |
| `dialect` | 이 루프의 방언 이름 | 항상 |
| `reasoning_tokens` | `output_tokens` 안에 든 추론 몫 | usage 없으면 키 없음 |
| `ttft_ms` | 요청 → 첫 토큰, 클라이언트 측정(스트리밍만) | 못 쟀으면 키 없음 |
| `llm_ms` | `provider.call` 벽시계 — 공급자 안의 재시도·재연결 포함 | 항상(루프가 쓴 줄) |
| `output_chars` · `thinking_chars` | 공급자가 돌려준 본문·사고 글자 수(서버가 구조로 준 `tool_calls` 인자는 세지 않는다) | 항상, 0 가능 |
| `tool_ms` | 턴 디스패치 벽시계 — 사람의 승인 대기 포함 | 디스패치 없는 턴은 키 없음 |
| `tools` | `[[이름, 성공, ms], …]` — 브리지를 거친 실행 한 번 = 한 줄(같은 파일 편집 묶음·`agent` 병렬 묶음도 한 줄) | 실행이 없던 턴은 키 없음 |

압축 이벤트 줄(`event: "compaction"`)은 `timestamp`·`tokens_before`·`tokens_after`·`evicted_count`·`fallback_used`·`failure_signal`·`duration_ms`·`summary_shape`·`shape_fallback`
에 같은 `harness_version`·`dialect` 를 싣는다. 쓰는 자리: 디스패치한 턴은 `loop/dispatch.py`, 잘린 턴은 `loop/core.py` 의 `_on_output_truncated`·`_on_server_error`·`_on_runaway`, 압축은 `context/manager.py`.

`agent-cli stats`(`recovery/stats.py`)는 이 파일만 읽어 숫자로 접는다 — 문자열은 닫힌 어휘로, 어휘 밖은 `other` (§11.3).

---

## 8. 설정 시스템

설정의 집은 하나씩이다 (`paths.py`, docs/config-scopes/DESIGN.md): 유저 `~/.agent-cli/` 에 `config.json`·`models.json`(머신 사실),
프로젝트 `.agent-cli/` 에 `mcp.json`·`skills/`·`agents/`·`hooks/`·`hooks.json`·`DIRECTIVE.md`, 세션은 `paths.sessions_dir()`(`AGENT_CLI_SESSIONS_DIR` 로 옮길 수 있다).

### 8.0 config.json (프로바이더/모델 설정)

```json
{
  "provider": "openai",
  "base_url": "http://127.0.0.1:8000/v1",
  "api_key": "",
  "default_model": "<model>"
}
```

`load_config()` 는 env(`AGENT_CLI_PROVIDER`·`AGENT_CLI_BASE_URL`·`AGENT_CLI_API_KEY`, 최저) 위에 `~/.agent-cli/config.json` 을 필드 단위로 덮는다.
CLI 파라미터가 그 위의 임시 오버라이드다. 파일이 있는데 못 읽으면 `ConfigError`.

**SetupWizard** (`setup.py`): 설정이 없고, 대화형이고, `--base-url`·`-m` 을 다 주지 않았을 때만 자동으로 뜬다(`main._maybe_setup`). `agent-cli setup` 으로 다시 띄운다.

**DIRECTIVE.md**: `.agent-cli/DIRECTIVE.md` 하나(`prompts/system_prompt.project_directive_file`). 공통·`## @main`·`## @agents` 청중 스코프로 나뉘고
(`split_directive_scopes`/`join_directive_scopes`), 시스템 프롬프트의 Directives 절에 들어간다. 바뀌면 다음 턴에 시스템 프롬프트를 다시 짠다.

### 8.1 models.json 구조

```json
{
  "models": {
    "<model_id>": {
      "provider": "anthropic | openai",
      "context_window": 32768,
      "max_output_tokens": 4096,
      "supports_thinking": true,
      "supports_grammar": true,
      "supports_vision": false,
      "temperature": 0.7,
      "dialect": "json_fc"
    }
  },
  "provider_defaults": {
    "openai": {"base_url": "..."}
  }
}
```

능력치로 읽는 키는 `_build_from_entry` 가 정한다(`context_window`·`max_output_tokens`·`supports_thinking`·`supports_grammar`·`supports_vision`·`temperature`·`top_p`).
`dialect` 는 `dialects.dialect_for_model` 이 읽는다 — 묶이지 않은 모델은 터미널이면 그 자리에서 묻고 저장한다(`config.bind_user_dialect`).

### 8.2 파일 위치 및 정책

| 우선순위 | 위치 | 역할 | 자동 저장 |
|---------|------|------|----------|
| 1 | `~/.agent-cli/models.json` | 사용자 설정·감지 캐시 | 감지한 모델을 `_auto_detected: true` 로 |
| 2 | `agent_cli/default_models.json` | 패키지 기본값 | 안 함 |

### 8.3 설정 로딩 우선순위 (`config.py`)

`_load_registry()` 가 기본값 파일 위에 유저 파일을 `models`·`provider_defaults` 키 단위로 덮는다. 파일 수정 시각이 바뀌면 다음 조회가 다시 읽는다.
프로바이더 기본 주소만 코드에 있고(`_PROVIDER_FALLBACK_URLS`), 모델 이름은 추측하지 않는다 — 아무것도 해석되지 않으면 `model_check.NoModelSelected`.

### 8.4 능력치 조회 우선순위 (`providers/capabilities.py`)

§4.2 와 같다: 등록값 > 런타임 감지(유저 models.json 에 저장) > `DEFAULT_CAPABILITIES`.
감지 진행은 `set_progress_callback` 으로 사용자에게 보인다. 수동 등록 엔트리(`_auto_detected` 없음)는 감지가 덮어쓰지 않는다(`config.save_model_entry`).
감지가 실패해 기본값이 되면, 터미널에서는 사용자에게 능력치를 묻는다.

### 8.5 모델 정보 출력

감지해 저장한 모델은 `render_model_detected`, 등록된 모델은 `render_model_loaded` 로 알린다(`main.py`).

### 8.6 Thinking 감지 방식

`_ProbeTransport.thinking_probe()` — 2단계:
1. "Say hello." 를 보내 사고가 보이는지 본다(OpenAI 전송은 `reasoning_content`·`reasoning` 필드를 `<think>` 로 정규화; content 의 `<think>`·`<thinking>`·`<reasoning>`·`<reflection>` 태그).
2. 안 보이면 방언의 사고 켜기 스위치(OpenAI `chat_template_kwargs.enable_thinking=true`, Anthropic `thinking` 블록)로 다시 묻는다 — 기본 off 인 모델(Qwen 등)을 잡는다. 재프로브 실패는 미지원.

감지되면 `supports_thinking=True` 로 저장된다. 이미 저장된 엔트리는 다시 감지하지 않는다 — 고치려면 엔트리를 지우거나 손으로 고친다.

---

## 9. 시스템 프롬프트 아키텍처 (`prompts/system_prompt.py`)

`build_system_prompt_sections(...)` 가 이름 붙은 섹션 리스트를 만들고 `SystemPromptSvc` 가 그것을 진실로 들고 있다(시스템 문자열은 join).
순서는 앞(정체성·원칙) → 가운데(레퍼런스) → 끝(환경·즉시 제약)이다. 매 턴 바뀌는 것은 시스템 프롬프트가 아니라 꼬리(§5.4)에 둔다 — KV 프리픽스 캐시 때문이다.

```
Role                 main 은 ROLE_PROMPT, 서브에이전트는 프로파일 본문, 스킬은 호출자의 역할
Context Discipline
Response Format      방언의 format_rules()
Available Tools      서버 파싱 방언(native_fc)이면 없다 — 도구는 요청의 tools 로 간다
Skills               run_skill 이 있을 때만
Agent Profiles       agent 가 있을 때만
Live Agents          상주 에이전트에게 주는 로스터가 있을 때만
Reply Discipline     (Live Agents 와 함께)
Environment          작업 디렉터리·플랫폼 — 날짜는 일부러 뺀다(자정마다 프리픽스가 깨진다)
Context Recovery     세션이 있고 history 도구가 있을 때만
Directives           DIRECTIVE.md 의 해당 청중(main / agents) 몫
Execution Context    호출 스택이 있을 때만(서브에이전트·스킬) — 재귀 금지 목록
```

Task Guidelines(`TASK_GUIDELINES`)는 이 파일이 단일 소스지만 꼬리로 렌더된다. 모델이 받는 글 전부는 `python -m agent_cli.prompts.inventory`
(`--html` 이면 `prompts/inventory.html` 틀로)로 코드에서 바로 뽑는다. 표기 규칙은 docs/prompt-notation/DESIGN.md, 가드는 `tests/test_prompt_notation.py`.

---

## 10. 테스트 아키텍처

### 10.1 테스트 분류

| 분류 | 자리 | 실행 |
|------|------|------|
| 유닛·통합(가짜 서버) | `tests/` | `pytest tests/` |
| omlx 통합 (E2E) | `omlx_integration` 마커 | `pytest tests/ -m omlx_integration` — 서버가 없으면 skip |
| 실브라우저 | `tests/browser/` | `AGENT_CLI_BROWSER_TESTS=1 pytest tests/browser/` — 기본 수트에서는 skip |
| 하니스 벤치 어댑터 | `bench/harbor/` | `pytest bench/harbor` |

- **실브라우저 테스트**는 실서버(`WebRenderer`+`WebServer`+uvicorn)와 헤드리스 Chromium 으로 유닛이 못 보는 것 — 연결 풀, CSS 렌더링,
  실 SSE 타이밍, 서버↔프런트 계약 — 을 본다. 파일 하나가 화면의 한 부분(카드·띠·서랍·칩·스크롤·재생)이다. CI 의 브라우저 잡이 옵트인으로 돈다.
- **등가성 코퍼스** `tests/equivalence/` — 방언 엔진의 고정 출력.
- **구조 가드**: `tests/test_architecture_doc.py`(이 문서) · `tests/test_import_graph.py`(§3) · `tests/test_import_cycles.py`(콜드 스타트) · `tests/test_ui_english.py`(사용자 표면은 영어).
- 공용 도우미: `tests/loopkit.py`·`tests/loop_ports.py`·`tests/binding_support.py`·`tests/pump_support.py`·`tests/web_static.py`·`tests/import_graph.py`.

### 10.2 테스트 실행

```bash
pytest tests/                      # 전체 (통합은 서버가 없으면 skip)
pytest tests/test_dialects_json_fc.py
pytest tests/ -m omlx_integration  # 실 서버 필요
```

**수트는 저장소 루트에 쓰지 않는다**: `tests/conftest.py` 의 autouse 가드가 테스트 앞뒤로 저장소 루트와 `.agent-cli/` 아래 두 단계의 항목을 찍어
(`tests/cwd_guard.py`) 새 항목이 생기면 그 테스트를 실패시킨다. 저장소에 쓰는 테스트는 `monkeypatch.chdir(tmp_path)` 나 자기 격리 픽스처로 돈다.
hypothesis 는 예제 DB 를 끄고 홈을 수트 임시 디렉터리로 옮긴다. 가드는 테스트 밖(종료 시 `atexit`)의 쓰기는 못 본다 — 수트 뒤 `git status --short --ignored` 로 본다.

---

## 11. CLI 명령어 레퍼런스

명령과 옵션의 사용법은 docs/REFERENCE.md 가 주인이다. 여기는 구조만 적는다. 명령은 `main.py` 의 typer 앱 하나다:
`run` · `web` · `setup` · `mcp`(`add`·`test`·`remove`) · `knobs` · `sessions` · `stats` · `update`.
세션 노브(`knobs.py::KNOBS`)는 `@knob_options` 가 `run`·`web` 의 플래그로 합성한다 — 표 하나에서 env·플래그·web 칩이 나온다.
사용자 명령 표(`/help` 등)는 `command_table.py` 하나다.

### 11.1 `run` — 단발 실행

`run` 과 `web` 은 같은 세션 조립(`runtime.py`: `SessionSpec` → stores → owners → `wire_session`)과 같은 펌프(`pump.SessionPump`)를 쓴다.
다른 것은 펌프 표면과 수명 정책뿐이다 — `run` 은 큐·에이전트·모니터·예약이 전부 조용하면 끝나고(`QuietPolicy`), `web` 은 종료 요청까지 산다(`ForeverPolicy`).
세션은 `web --resume <id>` 로 이어 갈 수 있다.

종료 코드: 0 성공 · 1 실패(LLM 오류·턴 상한·실패한 스킬·모르는 명령) · 2 설정·사용 오류 · 130 중단. `/sh <cmd>` 는 그 명령의 종료 코드(시간 초과 124).

### 11.2 `web` — 대화형 브라우저 UI

- 서버: FastAPI 앱 `web/server.py::create_app` 이 `WebServer`(렌더러·워커 스레드·API 상태)를 감싼다. 엔드포인트 목록은 그 파일 머리에 있다.
- 렌더러와 허브: 이벤트 모양·스코프·재생·status.json 은 `WebRenderer`(`render/web.py`), 연결·재생 버퍼·스티키·뷰어 별명은 `EventHub`(`render/web_hub.py`).
  렌더러의 `_emit` 이 이벤트 하나를 한 번 직렬화해 `hub.publish` 에 넘기면, 허브가 재생 버퍼와 열린 연결 전부의 큐에 넣는다.
- 다중 뷰어 — 모두 동등하게 입력·대기열을 쓸 수 있다.
- `--base-path <prefix>`: 프런트 URL 이 전부 상대 경로이고 `index` 가 `<base href="<prefix>/">` 를 넣어, 리버스 프록시의 경로 접두 밑에서 돈다.
- 세션별 `web.json`(주소·토큰·pid)과 `status.json` 을 `instance_file.py` 가 쓴다 — 바깥 오케스트레이터(보드)가 읽는다. `--idle-timeout` 이면 `web/idle.py` 가 스스로 끝낸다.
- 프런트(`web/static/`)는 빌드 없는 ES 모듈이다(docs/web-modules/DESIGN.md). 요소는 `dom.js` 의 `el(tag, classes, text)`(세 번째 인자는 textContent)로 만들고,
  HTML 은 이미 이스케이프·조립된 것만 `elHtml` 로 넣는다.
- 웹 슬래시 명령(`/help`·`/sh`·`/compact`)은 `web/slash.py`, `@agent`·`/skill` 디스패치는 run 과 공용(`main.try_dispatch_agent_or_skill`).

### 11.3 `stats` — 본문 없는 집계

`agent-cli stats [PATH …] [--since YYYY-MM-DD] [--json] [--out FILE]` — `recovery/stats.py`. 이름이 정확히 `turns.jsonl` 인 파일만 열고
심볼릭 링크는 따라가지 않는다. 모델 서버·설정이 필요 없다. 출력의 문자열은 닫힌 어휘뿐이다(§7.7).

---

## 12. 확장 가이드

### 12.1 새 프로바이더 추가

1. `providers/<name>.py` 에 `LLMProvider` 를 만족하는 클래스 — `call(messages, system, model, capabilities, **kwargs) -> LLMResponse`, 원어 stop_reason 을 루프 어휘로 매핑(§7.2),
   능력 프로브 전송 `capability_transport`(`_ProbeTransport` 하위 클래스).
2. 모듈 끝에서 `register_provider("<name>", Cls)`, `providers/__init__.py::_BUILTIN_MODULES` 에 한 줄.
3. `config._PROVIDER_FALLBACK_URLS` 에 공개 엔드포인트가 있으면 추가.

### 12.2 새 도구 추가

1. `tools/<name>.py` 에 `Tool` 하위 클래스 — `name`·`description`·`parameters`·`_run(args, *, ctx=None) -> ToolResult`. 필요하면 `requires_handler`·`deferred`·`render_oversized`.
2. `tools/registry.py::_ALL_TOOLS` 의 알맞은 자리에 인스턴스 하나(순서가 곧 프롬프트 순서).
3. 루프가 가로채야 하는 가상 도구면 `tools/virtual.py` 에 스키마를 두고 `loop/dispatch.py` 에서 이름으로 처리한다.
4. 모델이 받는 설명이 바뀌므로 `python -m agent_cli.prompts.inventory` 로 확인하고, 표기 가드(`tests/test_prompt_notation.py`)를 지킨다.

### 12.3 새 모델 등록

`~/.agent-cli/models.json` 에 엔트리(§8.1). 방언(`dialect`)은 반드시 묶는다 — 기본값이 없다. 미등록 모델은 런타임 감지 또는 기본값으로 돈다.

### 12.4 새 dialect 추가

방언은 데이터다 (docs/dialects/PHASE5.md):
1. `dialects/specs/<name>.py` 에 `DialectSpec` 하나 — 시작 트리거·호출 구분자·`NameSlot`·`ArgStyle`·종결 op·문법 opener·`Lenient`·`Prose`(모델이 읽는 산문 조각).
2. `dialects/__init__.py::_register_builtin_plugins()` 에 `register(Dialect(SPEC))` 한 줄.
3. 루프·프롬프트·회복 층은 고치지 않는다 — 그들은 `DialectBase` 와 `ParsedTurn` 만 안다.
4. 사용: models.json 엔트리의 `dialect` 키 또는 `--dialect <name>`.

---

## 13. 스킬 시스템 (`skills/`)

### 13.1 개요

프롬프트 스킬은 재사용 가능한 프롬프트 템플릿이다. 파일 포맷은 Claude Code 스킬과 호환된다. 사용법은 docs/REFERENCE.md.

### 13.2 스킬 파일 포맷

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

`Skill`(`skills/models.py`) 필드: `name`·`description`·`prompt_template`·`allowed_tools`·`max_turns`·`argument_hint`·`model`·`context`·`hooks`·
`disable_model_invocation`(모델이 `run_skill` 로 부르지 못함)·`user_invocable`(`/skills` 목록에서 숨김)·`source_path`.

### 13.3 인자 치환 (`skills/executor.substitute_arguments`)

`$ARGUMENTS`(전체) · `$ARGUMENTS[N]`·`$N`(N번째) · `${SKILL_DIR}`(별칭 `${CLAUDE_SKILL_DIR}`) · `${SESSION_ID}` · `` !`cmd` ``(명령 출력).

### 13.4 스킬 검색 경로 (`skills/loader.py`)

1. `.agent-cli/skills/*.md` 또는 `.agent-cli/skills/<name>/SKILL.md` (프로젝트, 우선)
2. `agent_cli/skills/builtin/` (패키지 내장)

`load_skills()` 는 부를 때마다 디스크를 다시 읽는다 — 세션 중에 만든 스킬도 바로 보인다. `model_skills()` 는 모델이 부를 수 있는 것만.

### 13.5 실행 플로우

```
/review-code src/auth.py  또는  run_skill {name, arguments}
    → load_skills() → Skill
    → substitute_arguments()
    → SkillLoop(skill, arguments, caller).run()   (§6.3 — 도구 교집합·호출자 역할·깊이 +1)
    → result.md 저장, ToolResult
```

### 13.6 스킬 스택 (재귀 방지)

- `run_skill` 처리(`loop/skill_invoke.py`)가 호출 스택에 같은 이름이 있으면 거절한다(`recovery/recursion.format_recursion_error`), 깊이 상한이면 `format_depth_limit_error`.
- 시스템 프롬프트의 Execution Context 절이 호출 스택과 금지 목록(`Do not run or invoke: …`), 깊이 상한에 닿았으면 그 사실을 적는다.

### 13.7 커스텀 스킬 작성

`.agent-cli/skills/my-skill.md`(또는 `my-skill/SKILL.md`)를 만들면 `/my-skill` 이 된다. 내장 `create-skill` 이 대화로 만들어 준다.

### 13.8 기본 내장 스킬

| 스킬 | 누가 부르나 | 무엇 |
|------|-------------|------|
| `plan` | 모델·사용자 | 구현 계획 작성 |
| `orchestrate` | 모델·사용자 | 계획 + 워커 조율 |
| `windows-dump` | 모델·사용자 | Windows 덤프 분류 — `kdump.py`(표준 라이브러리만) 동봉 |
| `create-skill` | 사용자 | 스킬 파일 생성 |
| `create-agent` | 사용자 | 에이전트 프로파일 생성 |

---

## 14. Hook 시스템 (`hooks/`)

### 14.1 개요

- **Python 훅**: `agent_cli/hooks/builtin/`(내장, 먼저) 다음 `.agent-cli/hooks/*.py`(프로젝트) — 컨텍스트 조작, MCP 메모리 접근.
  세션에 러너 하나(`runtime.build_hook_runner` → `session_slots.set_hook_runner`)를 모든 루프 종류가 같이 쓴다.
- **셸 훅**: `.agent-cli/hooks.json` — 외부 명령, stdin 으로 JSON, 종료 코드 0 허용 · 2 차단(PreToolUse) · 그 밖 경고 (`hooks/shell.py`).
- **스킬·에이전트 로컬 셸 훅**: SKILL.md 나 프로파일 frontmatter 의 `hooks:` — `merge_hooks_configs(parent, local)` 로 호출자의 훅 뒤에 붙는다.
- 내장 훅 `10_snapshot.py`: 세션 시작 안내와 변경 도구(`snapshot.MUTATING_TOOLS`) 직전 체크포인트 (docs/snapshot/DESIGN.md).

### 14.2 라이프사이클 이벤트 (`hooks/events.py`)

| 이벤트 | 시점 | 함수명 |
|--------|------|--------|
| OnSessionStart | 런 시작 | `on_session_start(ctx)` |
| PreLLMCall | LLM 호출 직전 (매 턴) | `pre_llm_call(ctx)` |
| PostLLMCall | LLM 응답 수신 후 | `post_llm_call(ctx)` |
| PreToolUse | 도구 실행 직전 | `pre_tool_use(ctx)` |
| PostToolUse | 도구 실행 직후 | `post_tool_use(ctx)` |
| OnTurnEnd | 턴 종료 후 | `on_turn_end(ctx)` |
| OnAgentStart / OnAgentEnd | agent run 앞뒤 | `on_agent_start(ctx)` / `on_agent_end(ctx)` |
| OnSkillStart / OnSkillEnd | 스킬 앞뒤 | `on_skill_start(ctx)` / `on_skill_end(ctx)` |
| OnSessionEnd | 런 끝 (finally) | `on_session_end(ctx)` |

### 14.3 Python Hook 파일 규약

```python
# .agent-cli/hooks/00_memory.py
EVENTS = ["OnSessionStart", "OnTurnEnd"]

def on_session_start(ctx):
    memories = ctx.search_memory("project context")
    if memories:
        ctx.inject_system_section("Memory", format_memories(memories))
```

- `EVENTS` 로 구독할 이벤트를 선언하고, 이벤트의 snake_case 이름으로 함수를 둔다(`EVENT_TO_FUNC`).
- 밑줄로 시작하는 파일은 훅이 아니다.

### 14.4 HookContext (`hooks/context.py`)

- **조작**: `inject_message()`, `inject_system_section()`, `remove_system_section()`
- **도구 제어** (PreToolUse): `block(reason)`, `modify_input(new_input)`
- **MCP 메모리**: `store_memory()`, `search_memory()`, `read_memory()` — MCP 매니저가 없으면 아무것도 안 한다

### 14.5 실행 순서

```
이벤트 → HookContext → Python 훅(내장 → 프로젝트, 파일 이름 순) → 셸 훅(hooks.json)
```

### 14.6 루프 통합

```
AgentLoop.run()
  ├─ _setup() 뒤 OnSessionStart
  ├─ _execute_turn()
  │   ├─ PreLLMCall → 주입된 시스템 섹션 반영
  │   ├─ LLM 호출
  │   ├─ PostLLMCall
  │   ├─ ToolBridge 실행 경로
  │   │   ├─ PreToolUse
  │   │   ├─ OnAgentStart / OnSkillStart (agent run·스킬)
  │   │   ├─ 도구 실행
  │   │   ├─ OnAgentEnd / OnSkillEnd
  │   │   └─ PostToolUse
  │   └─ OnTurnEnd
  └─ OnSessionEnd (finally)
```

---

## 15. 설계 원칙

1. **모델은 commodity, harness가 성패를 결정한다** — 파싱 구제, 과대 출력 처리, 해시라인 편집 등 harness 레벨의 처리가 핵심
2. **와이어 모양은 모델에 묶는다** — 방언은 스펙(데이터)이고 모델마다 바인딩한다. 서버가 함수 호출을 파싱하는 환경이면 `native_fc`
3. **소형 모델 우선 설계** — 보수적 출력 상한, 구제 가능한 모양은 막지 않는다, 스키마 자동 변환
4. **비용 제로 보정 우선** — LLM 재호출 없이 harness 에서 보정 (ref 구제, 타입 변환)
5. **점진적 기능 저하** — 기능 미지원 시 에러 대신 다음 폴백으로 (도구 광고 조건, 감지 실패 시 기본값)
6. **모듈 수준 import 순환 0** — 위층을 올려다보는 지연 import 는 이름 붙인 목록으로만 둔다 (§3.2, `tests/test_import_graph.py`)
