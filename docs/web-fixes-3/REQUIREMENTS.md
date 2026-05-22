# Web UI 3가지 문제 — Requirements

> Status: Draft
> Date: 2026-05-22
> Owner: architect (web-fixes-3 team)

## 0. 문서 범위

`agent-cli web` 명령으로 노출되는 LAN 웹 UI에서 발견된 세 가지 사용자 대면 결함을 정의한다. 각 문제는 독립적으로 머지 가능하나 한 PR(=하나의 커밋)로 묶어 처리한다(프로젝트 규칙 #6).

본 문서는 **무엇을 / 왜** 만 다룬다. **어떻게**는 [DESIGN.md](DESIGN.md)에서, **검증**은 [TEST_PLAN.md](TEST_PLAN.md)에서 다룬다.

## 1. 문제 1 — Markdown 렌더링 실패

### 1.1 증상

어시스턴트의 `final` / `thought` / observation 본문에 들어 있는 Markdown 표기가 raw text로 노출된다.

| 표기 | 현재 | 기대 |
|---|---|---|
| `### Heading` | 글자 그대로 표시 | `<h3>` 렌더 |
| `## Heading` | 글자 그대로 표시 | `<h2>` 렌더 |
| `# Heading`  | 글자 그대로 표시 | `<h1>` 렌더 |
| GitHub table (`\| col \|`) | `\|`와 `---` 행이 그대로 보임 | `<table>` 렌더 |
| `**bold**` / `*italic*` | 별표 그대로 노출 | `<strong>` / `<em>` 렌더 |
| `- item` / `1. item` | 하이픈/숫자 그대로 노출 | `<ul>` / `<ol>` 렌더 |

기존 `escapeAndFormat`은 fenced code block(` ``` `)과 inline code(` ` `)만 처리한다(`agent_cli/web/static/app.js:87-97`).

### 1.2 기능 요구사항 (FR-MD)

- **FR-MD-1**: ATX 헤더 `#`, `##`, `###`(공백 1개 이상 후 텍스트) 를 `<h1>`/`<h2>`/`<h3>`로 변환한다. `####` 이상은 변환하지 않는다(기존 raw 유지).
- **FR-MD-2**: GitHub-flavored pipe 테이블을 `<table>`로 변환한다. 헤더 행 + `---` 구분 행 + 본문 행 패턴을 인식한다. 정렬(`:--`, `:--:`, `--:`)은 1차 범위에서 지원하지 않는다(향후 작업).
- **FR-MD-3**: `**bold**`와 `*italic*`을 `<strong>` / `<em>`으로 변환한다. 한 단어 내에서만 매칭하지 않고, 줄 끝까지 lookahead 한다(코드 fence 안은 제외).
- **FR-MD-4**: 줄 시작이 `- ` 또는 `* `인 연속 줄을 `<ul>`로, `\d+\. `인 연속 줄을 `<ol>`로 묶는다. 중첩 리스트는 1차 범위 밖이다.
- **FR-MD-5**: 변환 순서는 `escapeHtml → code fences 보존(placeholder) → 헤더/테이블/리스트/볼드/이탤릭 → code fences 복원` 으로 고정한다. 코드 블록 안의 markdown 토큰은 변환되지 않아야 한다.
- **FR-MD-6**: 변환 대상은 `final`, `thought`, `user message bubble` 세 곳이다. `observation` body는 plain `<pre>` + Rich-tag 변환만 유지한다(diff 색깔 보존). action_input의 path/cmd 등 구조화된 표시 영역은 영향받지 않는다.

### 1.3 비기능 요구사항 (NFR-MD)

- **NFR-MD-1**: 외부 markdown 라이브러리(예: marked, markdown-it)는 추가하지 않는다. on-premise 배포 + zero-build-step 정책(`app.js` 단일 파일, ~543 LOC).
- **NFR-MD-2**: 입력은 항상 `escapeHtml` 후의 문자열을 받는다. XSS 표면 확장 없음. 변환 함수는 escaped HTML entity(`&amp;` / `&lt;` / `&#39;`)를 깨지 않고 패턴 매칭만 한다.
- **NFR-MD-3**: 추가 LOC는 100라인 이하(주석 포함). 함수 단위 단위 테스트 가능하도록 모듈 export 형태 유지.

### 1.4 범위 밖

- 정렬 지정자(`:--:`), 다중행 셀, HTML 임베드, 자동 링크, 이미지, 인용(`>`), 수평선(`---` 단독), Footnote/태스크 리스트.
- 코드 블록 내 syntax highlighting (별도 의존성 필요).

---

## 2. 문제 2 — `agent-cli web --resume` 미지원

### 2.1 증상

CLI `chat` 명령에는 `--resume <session_id>` 옵션이 있지만(`main.py:1174-1178`, `1224-1232`, `1249-1250`), `web` 명령에는 없다. 사용자가 이전 세션의 history.jsonl을 LAN UI에서 이어 진행할 방법이 없다.

### 2.2 기능 요구사항 (FR-RS)

- **FR-RS-1**: `agent-cli web --resume <session_id>` 형태로 `chat --resume`과 같은 ID 기반 재개를 지원한다.
- **FR-RS-2**: `--resume` 미지정 시 동작은 현재와 동일하다(새 세션 생성).
- **FR-RS-3**: `--resume <id>`로 지정한 세션이 존재하지 않으면, 다른 명령과 같은 형식의 에러 메시지를 stdout에 출력하고 `exit code 1`로 종료한다. uvicorn은 기동되지 않는다.
- **FR-RS-4**: 재개 성공 시:
  - `ContextManager(..., resume=True)`로 history.jsonl을 캐시에 복원한다.
  - 새로 연결되는 SSE 클라이언트가 이전 대화의 user / assistant turn / observation을 시간순으로 재생받아 화면에 표시한다.
  - 표시 분량은 `recent_exchanges(n=10)` 와 같은 정책으로 직전 10쌍의 user↔assistant 만 보여준다(컨텍스트 자체는 토큰 예산까지 모두 로드한다).
- **FR-RS-5**: stdout에 재개 사실을 알리는 안내 한 줄을 추가한다(`chat`과 같은 포맷):
  ```
  agent-cli web  (provider · model)
    UI:      http://...
    Token:   ...
    Session: <id>   (resumed)
  ```
- **FR-RS-6**: `--resume`은 `--workspace`(미존재)와 충돌하지 않는다. 세션의 `workspace` 필드는 메타에서 로드되며, 헤더 표시(`ready` 이벤트의 `workspace`)에 그대로 반영된다.

### 2.3 비기능 요구사항 (NFR-RS)

- **NFR-RS-1**: 세션 선택 UI는 추가하지 않는다(트레이드오프는 DESIGN.md 5절). `agent-cli sessions`로 ID를 찾고 명령행에 넘기는 것이 단일 경로다.
- **NFR-RS-2**: 재생되는 이벤트는 `_event_buffer` snapshot 메커니즘(`render/web.py:123-143`)을 재사용한다. 별도 replay 채널을 만들지 않는다.
- **NFR-RS-3**: history.jsonl → 이벤트 변환은 한 곳(`WebRenderer` 또는 별도 함수)에 모은다. main.py에 인라인 변환 로직을 두지 않는다.

### 2.4 범위 밖

- 웹 UI에서 세션 목록을 보고 선택하는 picker.
- 동일 세션을 두 프로세스가 동시에 resume할 때의 lock(현재 chat에도 없음 — 결정 보류).
- 재개 시 partial / interrupted turn 복구(현재 chat도 동일하게 `"(no completion)"` 마커로 표시).

---

## 3. 문제 3 — 서버 종료 시 exception

### 3.1 증상

`agent-cli web` 실행 중 Ctrl+C 입력 시 다음과 같은 traceback이 표준 출력에 노출된다(요약):

```
Traceback (most recent call last):
  File ".../sse_starlette/sse.py", line ..., in _ping
    ...
  File ".../asyncio/...", line ..., in _step
    result = coro.send(None)
asyncio.exceptions.CancelledError
```

추가로 worker thread(daemon)는 `_chat_queue.get()`에서 무한 대기 중이라 종료 시 정리 없이 강제 종료된다. session 파일이 저장되긴 하나(`finally: finalize_session`), 종료 직전에 진행 중이던 SSE 핸드오프/연결 클로즈가 정리되지 않는다.

### 3.2 기능 요구사항 (FR-SD)

- **FR-SD-1**: Ctrl+C 1회로 깨끗하게 종료된다(stack trace 노출 없음).
- **FR-SD-2**: 종료 시퀀스는 순서대로 수행된다:
  1. 활성 SSE 연결에 `__close__` sentinel을 push해 generator 루프를 빠져나오게 한다.
  2. worker thread(`_worker_loop`)에 종료 신호를 보내(`_chat_queue`에 종료 sentinel 또는 별도 event), 대기 중이면 깨어나 루프를 종료한다.
  3. `finalize_session(session, ctx)` 호출.
  4. stdout에 "Session <id> saved." 한 줄 출력 후 정상 종료.
- **FR-SD-3**: 두 번째 Ctrl+C(처리 중 강제 종료) 입력 시에도 traceback 없이 즉시 종료(`exit code 130`).
- **FR-SD-4**: 종료 도중 발생하는 `asyncio.CancelledError` / `KeyboardInterrupt`는 web 명령의 책임 범위에서 swallow한다. 다른 예외(파일 I/O 등)는 stderr에 한 줄 요약 + return code 1.

### 3.3 비기능 요구사항 (NFR-SD)

- **NFR-SD-1**: 새 의존성을 추가하지 않는다. `signal` 모듈은 stdlib.
- **NFR-SD-2**: README에 종료 매뉴얼 문단을 추가한다(예: "Ctrl+C로 종료하면 세션이 자동 저장됩니다. 같은 ID로 `agent-cli web --resume <id>` 재개 가능.").
- **NFR-SD-3**: uvicorn config는 `log_level="warning"`를 유지하되, server.py / main.py에 우리 자체의 종료 로그가 stdout에 명확히 출력되도록 한다.

### 3.4 범위 밖

- POSIX 시그널 외(SIGTERM 외)의 종료 경로(systemd, docker stop)는 현재와 동일하게 처리한다.
- 미저장 streaming token(`streaming` 카드)는 종료 시점에 사라진다(영속 상태가 아님).

---

## 4. 공통 / 비기능 요구사항

- 모든 변경은 **하나의 커밋**으로 묶인다(`CLAUDE.md` 규칙 #6).
- `pytest tests/ -m "not ollama_integration"` 전체 통과, `ruff check`/`ruff format --check` 통과.
- 회귀 금지: 기존 web 단위 테스트(`tests/test_web_renderer.py`, `tests/test_web_server.py`) 모두 그대로 통과해야 한다.
- 문서: `README.md`(웹 명령 옵션 + 종료 매뉴얼) / `docs/ARCHITECTURE.md`(LOC 갱신, web 섹션 보강)를 같은 커밋에 포함한다.

## 5. 우선순위 / 의존성

| 항목 | 우선순위 | 비고 |
|---|---|---|
| 문제 3 (종료 exception) | P0 | 사용자 경험 가장 큰 불편 — 즉시 노출 |
| 문제 1 (markdown) | P1 | 표/헤더가 자주 쓰임. P0 다음 |
| 문제 2 (resume) | P1 | chat 기능 parity. P0 이후 |

세 문제는 서로 독립이라 의존 관계가 없다. 하나의 PR에 묶어 검토한다.
