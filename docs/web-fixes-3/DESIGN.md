# Web UI 3가지 문제 — Design

> Status: Draft
> Date: 2026-05-22
> Owner: architect (web-fixes-3 team)
> Companion: [REQUIREMENTS.md](REQUIREMENTS.md), [TEST_PLAN.md](TEST_PLAN.md)

## 0. 개요

세 문제 모두 web 서피스에 한정된다. CLI(`chat`) 경로는 손대지 않는다. 변경 파일은 다음으로 한정한다:

| 파일 | 변경 성격 |
|---|---|
| `agent_cli/web/static/app.js` | markdown 렌더링 함수 추가 / 호출부 확장 |
| `agent_cli/main.py` | `web` 명령에 `--resume` 옵션 + 종료 시그널 핸들러 |
| `agent_cli/render/web.py` | (옵션 1) history → event 재생 헬퍼 추가 / shutdown sentinel 처리 |
| `agent_cli/web/server.py` | shutdown 시 connection / worker 정리 훅 |
| `README.md` | `--resume` 사용법 + Ctrl+C 매뉴얼 |
| `docs/ARCHITECTURE.md` | LOC + flow 갱신 |
| `tests/test_web_*.py`, `tests/test_app_markdown.py`(신규) | 단위 테스트 |

신규 파일은 최소화하며, JS 단위 테스트는 별도 러너 도입 없이 Python 측에서 `subprocess`로 `node` 실행이 가능한지 검토(없으면 함수만 export하고 사양 문서만으로 갈음).

---

## 1. 문제 1 — Markdown 렌더링

### 1.1 아키텍처

```
escapeAndFormat(s)
   │
   ├─ escapeHtml(s)                   ← 기존
   │
   ├─ extractCodeFences(html)         ← 신규: ``` … ``` 를 placeholder로 빼낸다
   │     returns: { stripped, blocks: [{token, html}, ...] }
   │
   ├─ markdownInline(stripped)        ← 신규:
   │     - GFM table       (multi-line scan)
   │     - ATX heading     (#, ##, ###)
   │     - unordered list  (-, *)
   │     - ordered list    (1., 2., ...)
   │     - bold / italic   (**, *)
   │     - inline `code`   (기존 한 줄 짜리)
   │
   └─ restoreCodeFences(html, blocks) ← placeholder를 <pre> 블록으로 복원
```

### 1.2 데이터 흐름 / 토큰 처리

**Placeholder 전략**: 코드 펜스는 `<!--cf:NN-->` (NN = index) 같은 HTML 코멘트 토큰으로 치환한다. 이유:
- 코드 안의 markdown 토큰이 인라인 변환에 휘말리지 않도록 한다.
- HTML 코멘트는 escaped 텍스트에 자연 발생하지 않고, 후속 정규식과 안전하게 분리된다.

**escapeHtml 후 매칭** 원칙(NFR-MD-2): 모든 markdown 정규식은 입력이 escape된 상태에서 동작한다. 즉:
- `**` / `*` / `#` / `|` 는 entity가 아니므로 그대로 매칭된다.
- 줄바꿈은 `\n`로 살아 있다(`escapeHtml`이 `\n`를 변경하지 않음).
- 사용자 입력의 `<script>`는 이미 `&lt;script&gt;`로 변환되어 있으므로 markdown 변환이 새 태그를 만들지 않는 한 안전하다. 신규 변환은 모두 정해진 한정 태그(`h1-h3, table, tr, th, td, ul, ol, li, strong, em`)만 생성한다.

### 1.3 정규식 사양 (구체 명세)

```js
// 1) code fence extraction
const RE_FENCE = /```([\w-]*)\n([\s\S]*?)```/g;

// 2) ATX heading — 줄 시작, # 1~3개, 공백 1개+, 본문
const RE_H = /^(#{1,3})\s+(.+?)\s*$/gm;

// 3) GFM table — 헤더 라인 + --- 구분 라인 + 본문 라인
//    | a | b |
//    |---|---|
//    | 1 | 2 |
//    스캔: 줄 단위 split, 헤더 + 구분 + 본문 연속 그룹을 식별.
//    구현은 줄 기반 상태 머신(정규식 한 방으로 처리 X)으로.

// 4) bold (** 또는 __ → <strong>; 1차 범위는 ** 만)
const RE_BOLD = /\*\*([^*\n]+?)\*\*/g;

// 5) italic — * 한 개 + 안쪽 비공백 + * — **bold**를 보존하도록 bold 이후에 처리
const RE_ITAL = /(^|[^*])\*([^*\n]+?)\*(?!\*)/g;

// 6) lists (줄 기반)
//    "^- " 또는 "^\* " 가 연속되면 <ul><li>… 그룹화
//    "^\d+\. " 가 연속되면 <ol><li>… 그룹화

// 7) inline code (기존 유지)
const RE_ICODE = /`([^`\n]+)`/g;
```

### 1.4 함수 시그니처

`app.js` 최상위 IIFE 안에 다음을 추가한다:

```js
function extractCodeFences(s)        // returns { stripped: string, blocks: [{token, html}] }
function restoreCodeFences(s, blocks) // returns string
function renderTables(s)              // returns string (table block 단위 줄 스캔)
function renderHeadings(s)            // ATX → <h1-3>
function renderLists(s)               // ul / ol 그룹화
function renderEmphasis(s)            // ** then * 순서로 처리
function markdownInline(s)            // pipeline orchestrator
function escapeAndFormat(s)           // 기존 시그니처 유지 (호환 보장)
```

호출자(`renderUserMessage`, `renderAssistantTurn`) 변경 없음. 기존 시그니처를 유지하고 안쪽만 강화한다.

### 1.5 변환 순서

```
1. escapeHtml
2. extractCodeFences  →  placeholder
3. renderTables       (블록 변환: 줄 단위 스캔)
4. renderHeadings     (블록 변환: ATX)
5. renderLists        (블록 변환: 연속 줄 그룹)
6. renderEmphasis     (인라인: ** then *)
7. inline code (`…`)
8. restoreCodeFences  →  <pre><code>
```

표/헤더/리스트 같은 블록 변환을 인라인 변환보다 먼저 두면, 인라인 정규식이 헤더 라인의 `#`을 건드릴 일이 없다(헤더는 이미 `<h3>` 안으로 들어가 있음).

### 1.6 호환성 / 회귀 방지

- 기존 fenced code block test가 통과해야 한다(현재는 grep 결과 별도 unit test 없음 — 신규 테스트로 보강).
- `richMarkupToHtml`은 observation 본문 전용으로 그대로 둔다(이번 변경 영향 없음).

---

## 2. 문제 2 — `web --resume`

### 2.1 아키텍처

```
agent-cli web --resume <id>
   │
   ├─ load_session(id)              ← 기존 함수, 그대로 사용
   │     None → "Session not found" + exit(1)
   │
   ├─ ContextManager(..., resume=True)
   │     · history.jsonl 캐시 복원
   │
   ├─ replay_history_to_renderer(ctx, renderer)   ← 신규
   │     · ctx.get_raw_messages() 순회
   │     · role별 분류 → 적합한 renderer 메서드 호출
   │     · 결과적으로 _event_buffer가 채워짐
   │
   └─ uvicorn.run(...)
        · 신규 SSE 연결 → register_connection이 snapshot 재생
        · 클라이언트는 이전 대화가 timeline 위에서부터 그려진 상태로 시작
```

### 2.2 신규 옵션 정의 (main.py)

```python
@app.command()
def web(
    ...,
    resume: Optional[str] = typer.Option(
        None, "--resume",
        help="Resume a previous session by ID (use `agent-cli sessions` to list).",
    ),
) -> None:
    ...
```

세션 setup 블록(현재 1506-1522):

```python
from agent_cli.context.session import (
    create_session, finalize_session, get_session_dir, load_session, save_meta,
)

if resume:
    session = load_session(resume)
    if session is None:
        console.print(f"[red]Session '{resume}' not found.[/]")
        raise typer.Exit(code=1)
    console.print(f"[{C['accent']}]Resuming session {resume}[/]")
else:
    session = create_session()
save_meta(session)

ctx = ContextManager(
    get_session_dir(session),
    max_context_tokens=max_context_tokens,
    resume=bool(resume),
    wire_format=wire_format_plugin,
)
```

### 2.3 신규 헬퍼: `replay_history_to_renderer`

위치 선택 (트레이드오프):

- 옵션 A) `agent_cli/render/web.py` 내부 메서드 `WebRenderer.replay_from_history(ctx)`.
- 옵션 B) `agent_cli/web/server.py` 모듈 함수.
- 옵션 C) `agent_cli/main.py` 내부 헬퍼.

**채택: A** — 이유:
- 이벤트 버퍼 / `_persistent_count` 등 내부 상태를 다루므로 renderer가 자기 자신을 채우는 게 응집도가 높다.
- `_emit`는 private이지만 같은 모듈 안에서 호출 가능.
- `WebDispatchOutput`처럼 web extra 경계를 침범하지 않는다(이미 `render.web`은 `[web]` 보호 영역이 아님).

구현 골격(코드 레벨):

```python
def replay_from_history(self, ctx) -> None:
    """Re-emit persistent events from ctx.history into the buffer.

    Called once at server startup when ``--resume`` was passed. Builds
    the same event sequence the live AgentLoop would emit so newly
    connecting clients see prior turns in chronological order.

    Only re-emits events that the live loop also stores as persistent:
    user_message, assistant_turn (thought + action / thought + final),
    observation. Streaming chunks / spinners / status are runtime UX
    only and have no counterpart on disk.
    """
    for msg in ctx.get_raw_messages():
        role = msg.get("role")
        if role == "user":
            tool = msg.get("tool")
            if tool:
                # tool result → observation
                content = msg.get("content", "")
                success = not (msg.get("error") or False)
                self.observation(content, turn=0, tool_name=tool, success=success)
            else:
                content = msg.get("content", "")
                if not content:
                    continue
                # echo as user_message (same path POST /api/input takes)
                self.push_user_message(content)
        elif role == "assistant":
            action = msg.get("action", "")
            thought = msg.get("thought", "")
            ai = msg.get("action_input", {})
            if action == "complete":
                final = ai.get("result", "") if isinstance(ai, dict) else str(ai)
                # emit thought + final as a single assistant_turn
                self._emit_assistant_turn(thought=thought, final=final, turn=0)
            elif action:
                self._emit_assistant_turn(
                    thought=thought,
                    action={"tool_name": action, "tool_input": json.dumps(ai)},
                    turn=0,
                )
```

`_emit_assistant_turn`은 기존 thought/action/final 흐름을 합쳐 한 번에 `assistant_turn` 이벤트를 만드는 헬퍼이거나, 기존 `thought()` + `action()`/`final()` 흐름을 그대로 호출해도 된다(기존 `_pending_thought` 슬롯이 처리).

### 2.4 main.py 호출 지점

`renderer.header(...)` 직후, worker thread 시작 전:

```python
if resume:
    renderer.replay_from_history(ctx)
```

이렇게 하면 신규 SSE 클라이언트가 `register_connection`을 부를 때 `_event_buffer`에 이미 과거 이벤트가 들어 있어 그대로 snapshot 재생된다.

### 2.5 UI 표시 결정 (FR-RS-4)

`recent_exchanges(n=10)`와 동일한 정책이지만 timeline 자체는 모든 메시지를 보여주는 것이 일관성 있다(기존 chat REPL과 다른 점: chat REPL은 "최근 10쌍 요약"을 보여주지만 web은 카드 형태이므로 그대로 다 그려도 무방하다). **결정**: 전체 캐시를 그린다. 즉 ContextManager 토큰 예산 안에 들어온 모든 메시지를 카드로 표시한다. 토큰 예산이 워낙 커서 과도하게 길어질 경우의 trim은 별도 작업으로 남긴다(REMAINING_DEBT 후보).

### 2.6 트레이드오프 (NFR-RS-1 의 근거)

| 선택 | 장점 | 단점 | 채택 |
|---|---|---|---|
| 명령행 ID 만 | 단순. `chat --resume`과 일관. 새 UI 코드 0. | 사용자가 ID를 외부 명령으로 찾아야 함. | **O** |
| 웹 UI picker (`/sessions` 페이지) | UX 매끄러움. | 새 라우트 + HTML + 인증 흐름 + 보안 검토. 범위 폭증. | X |
| 환경변수 `AGENT_CLI_RESUME` | 운영상 자동화 편함. | 매뉴얼 노출 빈도 낮음. 후순위. | X (후속) |

**결정**: 명령행 ID. 운영자가 `agent-cli sessions`로 ID 확인 → `agent-cli web --resume <id>` 한 번 흐름. 한 줄로 표현 가능, 보안/인증 표면 변경 없음.

### 2.7 헤더 워크스페이스 처리 (FR-RS-6)

`renderer.header(...)`는 `WebRenderer.__init__(workspace=...)`에서 받은 값을 `ready` 이벤트에 실어 보낸다. 현재 `web()`에서 `WebRenderer(workspace=session.workspace)` 로 전달되므로, `resume`된 세션의 `workspace` 필드가 자동 반영된다 — **추가 변경 불필요**.

---

## 3. 문제 3 — 종료 시 exception

### 3.1 아키텍처

```
Main thread (uvicorn.run, blocking)
   │
   ├─ SIGINT (Ctrl+C)
   │     · uvicorn은 자체 시그널 핸들러로 graceful shutdown 시도
   │     · 그러나 sse-starlette의 ping coroutine이 CancelledError로 종료 → traceback
   │
   ├─ Worker thread (daemon)
   │     · _chat_queue.get(timeout=None) 블록 중
   │     · daemon=True 이므로 메인 종료 시 강제 종료 → 정리 X
   │
   └─ 활성 SSE 연결
         · executor 안에서 queue.get(timeout=15) 대기
         · uvicorn shutdown이 연결을 끊으면 finally에서 unregister
```

### 3.2 변경 전략

세 가지 결함을 따로 처리한다:

**(a) uvicorn graceful shutdown — sse-starlette traceback 억제**

`uvicorn.Config` + `uvicorn.Server` 패턴으로 변경해 직접 `serve()`를 호출하고, `SystemExit`/`KeyboardInterrupt`를 잡는다:

```python
import uvicorn
config = uvicorn.Config(app_obj, host=host, port=port, log_level="warning")
server_obj = uvicorn.Server(config)

try:
    server_obj.run()  # blocks; handles SIGINT internally
except KeyboardInterrupt:
    pass  # second Ctrl+C — suppress
finally:
    _shutdown_web(renderer, server, worker, shutdown_event, session, ctx)
```

`server_obj.should_exit = True`는 uvicorn이 SIGINT에서 자동 설정.

**(b) Worker thread 정리**

worker 종료를 위해 `threading.Event` (`shutdown_event`) 또는 `pop_chat` 시 `None` sentinel을 사용한다. 후자가 기존 인터페이스에 부합:

- `WebServer.push_chat(None)` 호출 → worker 가 `message is None and shutdown_event.is_set()`를 보고 루프 탈출.
- 더 단순한 방안: `WebServer`에 `shutdown()` 메서드를 추가하고, `_chat_queue`에 sentinel을 넣는다. worker는 sentinel을 받으면 break.

```python
# agent_cli/web/server.py
_SHUTDOWN_SENTINEL = object()

class WebServer:
    ...
    def shutdown(self) -> None:
        self._chat_queue.put(_SHUTDOWN_SENTINEL)

    def pop_chat(self, timeout=None):
        try:
            item = self._chat_queue.get(timeout=timeout)
        except Empty:
            return None
        if item is _SHUTDOWN_SENTINEL:
            return _SHUTDOWN_SENTINEL  # caller handles
        return item
```

main.py worker:

```python
while True:
    message = server.pop_chat(timeout=None)
    if message is _SHUTDOWN_SENTINEL:
        break
    if message is None:
        continue
    ...
```

`_SHUTDOWN_SENTINEL`을 worker 인터페이스에 노출해야 하므로 `WebServer.SHUTDOWN`이라는 클래스 상수로 둔다(public, identity-comparable).

**(c) SSE 연결 정리**

`WebRenderer.shutdown_all_connections()` 추가:

```python
def shutdown_all_connections(self) -> None:
    """Close every active SSE generator without sending takeover."""
    with self._lock:
        for c in self._connections:
            if not c.closed.is_set():
                c.closed.set()
                c.queue.put(_CLOSE_SENTINEL)
        self._connections.clear()
```

main.py finally:

```python
def _shutdown_web(renderer, server, worker, session, ctx):
    renderer.shutdown_all_connections()
    server.shutdown()           # wake worker
    worker.join(timeout=2.0)    # best effort
    console.print(f"[{C['muted']}]Saving session...[/]")
    try:
        finalize_session(session, ctx)
        console.print(f"[{C['muted']}]Session {session.session_id} saved.[/]")
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Failed to save session: {e}[/]")
```

### 3.3 sse-starlette CancelledError 처리

ping coroutine이 종료 시 `CancelledError`를 raise하는 것은 sse-starlette 내부 사정이다. uvicorn shutdown이 task들을 cancel하는 정상 경로 — 우리 코드 입장에서는 trackback이 표면화되지 않도록 하면 된다.

세 가지 옵션:

1. **uvicorn log_level 유지 + Python 디폴트 stderr는 우리 코드 흐름이 아니다** — uvicorn의 lifespan 종료 시 발생하는 exception이 stderr에 노출되므로 위 (a)~(c) 정리만으로는 부족할 수 있다.
2. **`asyncio` exception handler 등록**: `loop.set_exception_handler`로 `CancelledError`만 silent하게 처리.
3. **lifespan shutdown 훅에서 sse-starlette 큐를 명시적으로 닫기**.

**채택**: (1) + (2). 즉, (a)~(c)로 빠르게 SSE 연결을 끊어 ping coroutine이 await 중인 큐가 빈 상태에서 cancel되도록 하고, FastAPI에 `@app.on_event("shutdown")` 훅을 등록해 `renderer.shutdown_all_connections()`를 다시 한 번 호출한다(원자적 보장).

```python
# agent_cli/web/server.py — create_app 안
@app.on_event("shutdown")
async def _on_shutdown():
    # Idempotent — main.py finally도 이걸 한 번 부른다.
    server.renderer.shutdown_all_connections()
```

이 정도면 sse-starlette의 ping cancel이 silent 하게 끝난다(검증은 TEST_PLAN.md S-3).

### 3.4 main.py 변경 요약

```python
# agent_cli/main.py — web 함수 finally
finally:
    _shutdown_web(renderer, server, worker, session, ctx)
```

`KeyboardInterrupt`는 `server_obj.run()` 안에서 처리되므로 main 단에서 추가로 catch할 필요는 없다(예외가 새 나오면 finally가 한 번 더 정리).

### 3.5 README 추가 문단

```
종료 방법

Ctrl+C 한 번으로 서버가 깨끗이 종료됩니다. 종료 시 세션이 자동
저장되며, 다음과 같이 같은 세션을 이어서 실행할 수 있습니다:

    agent-cli web --resume <session_id>

세션 ID는 `agent-cli sessions`로 조회합니다.
```

---

## 4. 변경 파일 요약

| 파일 | 변경 LOC (추정) | 종류 |
|---|---|---|
| `agent_cli/web/static/app.js` | +90 / -3 | markdown helpers + escapeAndFormat 확장 |
| `agent_cli/main.py` | +30 / -10 | `--resume` 옵션, shutdown 정리 |
| `agent_cli/render/web.py` | +40 / -0 | replay_from_history, shutdown_all_connections |
| `agent_cli/web/server.py` | +20 / -3 | shutdown sentinel, on_event shutdown |
| `README.md` | +25 | resume + 종료 매뉴얼 |
| `docs/ARCHITECTURE.md` | +15 | LOC, flow 갱신 |
| `tests/test_web_renderer.py` | +30 | replay + shutdown |
| `tests/test_web_server.py` | +20 | shutdown sentinel |
| `tests/test_app_markdown.py` (또는 `test_app_js_smoke.py`) | +50 | markdown helper 사양 검증 (node 사용 가능 시 직접 호출, 아니면 패턴 문서 검증) |

총 ~280 LOC 변경.

## 5. 위험 / 결정 보류

- **JS 단위 테스트 러너 부재**: 현재 프로젝트에 node 의존이 없다. 대안은 (a) `node`가 설치된 경우만 실행하는 옵셔널 테스트, (b) 정규식 사양 문서를 TEST_PLAN.md에 명시하고 수동 검증. **채택 (b)**, node 추가 없음.
- **GFM 테이블 정렬자**: 1차 범위 밖. `:--:` 만나면 그냥 일반 셀로 처리.
- **두 번 Ctrl+C 사용자 의도**: 강제 종료. 두 번째 SIGINT에서는 finalize_session을 건너뛸지 결정 필요 — 1차 범위는 "saved" 출력 후 즉시 exit. 미저장 누락 없음(매 turn append).
- **SSE shutdown idempotency**: `shutdown_all_connections`는 두 번 호출돼도 안전하다(connections 리스트 비움).
