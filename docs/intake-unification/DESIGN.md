# Intake Unification — DESIGN

사용자 메시지 intake 를 **단일 라우팅 경로**로 통합한다. run-starter 와
mid-run injected(큐 스티어링) 메시지가 동일한 라우팅을 거쳐, `/sh`·`/compact`·
`@agent`·`/skill` 이 **언제 도착하든 같게** 동작하게 한다.

## 배경 / 문제 (현재)

intake 가 두 갈래로 포크됨:

| | run-starter | mid-run injected |
|---|---|---|
| 위치 | worker (run 사이) | 루프 `_inject_queued_messages` (턴 경계) |
| 라우팅 | `handle_slash_command` → `try_dispatch_agent_or_skill` → else `run_loop` | **없음** — `[nick]: text` 를 user 메시지로 add |
| author | `query_label` 별도 인자 | queue item `nickname` |

결과 버그/비대칭:
- 중간에 `/sh ls` → 실행 안 되고 `[nick]: /sh ls` 가 리터럴 chat 으로 LLM 에 들어감.
- `/compact`·`@agent`·`/skill` 중간 주입도 전부 리터럴 텍스트.
- author 배관이 두 갈래(`query_label` vs nickname) — 라벨링 로직 중복(loop.py:498-506 vs 528-534).

## 사실 (코드 확인)

- `@agent` → `_dispatch_agent` → **`tool_delegate(parent_ctx=ctx)`** (main.py). 모델 `delegate` op 와 **이미 동일 기계장치에 수렴**. `_dispatch_agent` 는 `ctx.add(...)` 로 공유 ctx 에 작용.
- `/sh`(`_handle_sh`), `/help` 은 `renderer.observation(...)` 렌더만 — **ctx 무영향(display-only)**. 의도된 동작.
- `/compact` 은 `ctx.compact_now()` 로 ctx 변경.
- `/skill` → `_dispatch_skill` → `execute_skill` → `run_loop`(공유 ctx).
- 루프 turn 경계 주입 seam: 생성자 `dequeue_user_message` 콜백 + `_inject_queued_messages()` (loop.py:519-534), `run()` 루프 loop.py:370 에서 호출.

→ **"완전 대칭" = run-start 와 동일 라우팅**. "모두 모델에 먹인다"가 아님 — 각 명령의 본래 효과(display-only/ctx-변경)는 그대로 보존.

## 설계

### 1) 단일 라우팅 함수 (web 레이어)
worker 가 starter 에 쓰는 라우팅을 **재사용 가능한 함수**로 추출:

```python
# web 레이어(main.py 또는 server.py). on_chat 콜백만 호출처별로 다름.
def route_user_message(message, nickname, *, on_chat, renderer, ctx, **dispatch_kwargs) -> None:
    if handle_slash_command(message, renderer, ctx=ctx):     # /help /sh /compact
        return
    if try_dispatch_agent_or_skill(message, web_output, ctx=ctx, **dispatch_kwargs):  # @agent /skill
        return
    on_chat(message, nickname)                                # 평문 chat
```

- **worker starter**: `on_chat = lambda m, n: run_loop(query=m, query_author=n, route_injected=<콜백>, ...)`
- **turn-boundary inject**: `on_chat = lambda m, n: loop._add_user_message(m, n)` (현 run 에 주입)

→ starter 와 injected 가 **같은 `route_user_message` 통과**. 차이는 `on_chat` 연속(=run 시작 vs 주입)뿐.

### 2) 코어 루프: 두 콜백(dequeue + route) — 닭-달걀 회피
구현 seam = 기존 `dequeue_user_message`(item 반환) 유지 + 신규 **`route_message(text)->bool`**(명령 라우팅). 루프가 dequeue·echo·chat-inject 를 소유하고, 명령 라우팅만 콜백에 위임 (콜백이 루프 인스턴스를 알 필요 없음 — `_add_user_message` 호출은 루프 내부에서):

```python
def _inject_queued_messages(self):
    if self.dequeue_user_message is None:   # CLI: no-op
        return
    item = self.dequeue_user_message()
    if not item:
        return
    text, author = item.get("text") or "", item.get("nickname")
    labeled = f"[{author}]: {text}" if author else text
    renderer.push_user_message(labeled)     # run-starter echo 대칭(턴 경계 echo)
    if self.route_message is not None and self.route_message(text):
        self.task_log.append(labeled)       # 명령 — ask 기록; 라우팅이 ctx 변경했을 수 있음
        if self.ctx: self.messages = self.ctx.get_messages()
        return
    self._add_user_message(text, author)    # 평문 → 스티어링 주입
```

- `route_message` = web worker 의 `route_one(text)` 클로저(= `handle_slash_command` → `try_dispatch_agent_or_skill`). starter 도 같은 `route_one` 사용 → **동일 라우터**.
- 코어 루프는 **web 라우터를 모름**(콜백만 호출) → 레이어 분리 유지.

### 3) author 통합 + 라벨링 단일화
`query_label` 제거. 라벨링 중복(setup vs inject)을 헬퍼로 통합:

```python
def _add_user_message(self, text, author=None):
    labeled = f"[{author}]: {text}" if author else text
    self.task_log.append(labeled)
    if self.ctx:
        self.ctx.add({"role": "user", "content": labeled})
        self.messages = self.ctx.get_messages()
    else:
        self.messages.append({"role": "user", "content": labeled})
```

- setup(첫 메시지)·inject(평문 chat) 둘 다 `_add_user_message` 호출.
- 생성자 `query_label: str` → `query_author: str | None`(의미: run 시작자 author). CLI=None.

## 변경 파일

- `agent_cli/loop.py` — `_add_user_message` 추출; `query_label`→`query_author`; `route_message` 콜백 추가(`dequeue_user_message` 유지); `_inject_queued_messages` 재작성; `run_loop` 시그니처.
- `agent_cli/main.py` — worker 가 starter 라우팅을 `route_one(text)` 클로저로 묶어 starter 게이트 + `route_message=route_one` 로 inject 양쪽에 동일 사용; `query_author=nickname` 전달.
- `agent_cli/web/server.py` — (필요 시) `_handle_sh` 등 시그니처 정합. dequeue API 변동 없음.
- `tests/` — 회귀 + 신규(아래 TEST_PLAN).
- docs(ARCHITECTURE: intake 단일 라우팅 흐름).

## 무변경 보장 (회귀 0 목표)

- **CLI `run`**: `route_injected=None` → 주입 경로 no-op. 동작·history 바이트 동일.
- **모델 `delegate`**: 손대지 않음(tool 경로 그대로).
- **starter 라우팅 결과**: `/help`/`/sh`/`/compact`/`@agent`/`/skill`/chat 분기 동작 동일(같은 함수 재사용).
- **평문 스티어링 주입**: 기존과 동일(`_add_user_message`).
- **worker_busy/idle·echo·Stop·queue 표시 타이밍**: 블로킹 dequeue+echo 는 worker 에 유지 → 무변경.

## 범위 밖

- 스키마 enrich(kind/turn/ts/text/author) + read_context JSON 쿼리 — **다음 PR**(이 통합 위에).
- starter 의 블로킹 dequeue 를 루프로 이동 — 안 함(worker 라우팅 책임 유지).
- BM25/FTS5.

## TEST_PLAN (회귀 철저)

### 코어 루프 (provider/web 없이 단위)
- **R-1** CLI: `route_injected=None` → `_inject_queued_messages` no-op, 기존 단일-query run 회귀.
- **R-2** `_add_user_message(text, author)` → `[author]: text` 라벨 + task_log + ctx.add; author=None → 라벨 없음.
- **R-3** setup 과 inject 가 동일 라벨 모양 산출(중복 제거 검증).
- **R-4** `route_injected` 콜백이 ctx 를 바꾸면 다음 턴 `self.messages` 가 refresh 됨.
- **R-5** `query_author` 전달 경로(=run 시작자 author) 라벨 반영.

### 라우팅 대칭 (web 레이어, dispatch mock)
- **S-1** starter `/sh ls` 와 injected `/sh ls` 가 **동일 함수** 통과 → 둘 다 observation 렌더(ctx 무영향 동일).
- **S-2** injected `/compact` → `ctx.compact_now()` 호출(ctx 변경 반영).
- **S-3** injected `@agent x do` → `tool_delegate` 호출(모델 delegate 와 동일 인자 모양), 결과 ctx 반영.
- **S-4** injected `/skill foo` → `execute_skill`/`run_loop` 경로.
- **S-5** injected 평문 → `_add_user_message`(스티어링), 라우팅 분기 안 탐.
- **S-6** starter chat vs injected chat: `on_chat` 만 다르고 라우팅 동일(분기 동형) 확인.

### 통합/회귀
- **I-1** 기존 web 큐/스티어링 테스트 전부 green(회귀 0).
- **I-2** `query_label` 제거 후 호출처(main.py:1428) 정합 — 닉네임 라벨 여전히 history 에.
- **I-3** 전체 `pytest -m "not ollama_integration"` + ruff.
