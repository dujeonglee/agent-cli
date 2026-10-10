# 세션 펌프 통합 — run(텍스트 UI) 과 web 이 같은 루프를 돈다

상태: 2026-10-11. **1단계 완료 (v10.32.1)** `session_has_live_work` ·
**2단계 완료 (v10.33.0)** `agent_cli/pump.py` + `_ConsoleSurface` 로 run 전환,
`ports_for_main` · **3단계 남음** web 을 `WebSurface` 로.

2단계에서 설계와 달라진 점: `RunRequest` 가 ``run_loop`` 의 query*/stop
인자 묶음으로 추가됐고, ``@agent task`` 의 결과 파일은 ``DispatchOutput.
agent_result(result, ok=)`` 가 쓴다(run 전용 분기 삭제). run 의 ``/skill``
디스패치 뒤 ``warn_stuck`` 이 메인 경로와 같이 켜진다(종전 False — 조기
반환 경로가 사라졌다).

## 1. 왜

`agent-cli run` 과 `agent-cli web` 은 이미 같은 부품을 쓴다 — `InputQueue`,
`MailWaker`, `run_loop`, `runtime.py` 조립기(registry·메일·teardown), 렌더러
플러그인. 그런데 그 부품을 **꿰는 루프가 두 벌**이다:

| | run `_run_message_pump` (main.py 1606) | web `_worker_loop` (main.py 2375) |
|---|---|---|
| 줄 수 | ~65 (+ `_run_one` 30) | ~200 |
| 수명 | 큐 비고·에이전트 없고·모니터 없고·예약 없으면 **종료** (`_quiet`, 0.5s 폴) | SHUTDOWN 까지 **영원히** (타임아웃 없는 `dequeue_blocking`) |
| 스레드 | 메인 | 워커 (uvicorn 이 메인) |
| 아이템 라우팅 | 첫 질의만 펌프 **앞에서** `try_dispatch_agent_or_skill` (`/sh` 제외 가드) | 매 아이템 `route_one` = `handle_slash_command` + `try_dispatch_agent_or_skill`, 런 도중 주입분도 같은 함수로 |
| 아이템 메타 | text 만 | nickname·request_id·author → `run_loop(query_author=…, query_request_id=…)` |
| 화면 신호 | wake 때 콘솔 한 줄 | `worker_idle/busy`, 에코 카드(`push_user_message`/`agent_wake`), `set_current_run_authors` |
| 중단 | Ctrl-C (`graceful_interrupt=False`) | 아이템마다 `stop_event` + `server.set_stop_handle` (Stop 버튼), `graceful_interrupt=True` |
| 런 예외 | 전파 → `_finalize_run` → 비정상 종료 | `renderer.error` 후 다음 아이템 |
| 런 결과 | `--result-file` 기록 (v10.31.2: 런 성공 즉시) | 없음 (화면이 결과) |
| 부트 알림 | 콘솔 print 3종 (재생성·미답·auto-spawn) + 모니터 통지 | `_announce_agent_boot(renderer, …)` + `renderer.status` |

두 벌이라 생긴 일: 수명 정책이 run 에만 있어 `--result-file` 이 펌프 뒤에
써지는 구멍이 run 에만 있었고(v10.31.2 수리), 모니터(v9.11.0)·예약(v10.12.0)
이 붙을 때마다 "펌프 + `web_instance_is_active` 두 곳에 같은 한 줄" 을
넣어야 했다(ARCHITECTURE 의 monitor 항목이 이 비대칭을 명시). hooks 배선
누락(v8.39.0), 디스패치 경로 분기(v7.17.0) 도 같은 뿌리다.

## 2. 목표 / 비목표

**목표**
- 루프 **한 벌** (`agent_cli/pump.py`): 큐에서 꺼내 → 깨우기 판정 → 라우팅 →
  `run_loop` → 런 종료 처리 → 정지 판정. run 과 web 은 이 루프에 **표면**
  (surface) 과 **수명 정책** 만 꽂는다.
- 모니터·예약·에이전트 활동 같은 "살아 있어야 하는 이유" 를 **한 술어**
  (`has_live_work`) 로 모아 펌프 정지와 web idle self-reap 이 같은 함수를 쓴다.
- 부트 알림(재생성·미답 질문·auto-spawn·이전 모니터) 을 조립기 한 곳에서
  표면으로 내보낸다.

**비목표**
- `run_loop` 내부, 렌더러 프로토콜, `InputQueue`/`MailWaker` 는 손대지 않는다.
- run 을 "끝나지 않는 세션" 으로 바꾸지 않는다 — 수명 정책이 다른 것은 의도다.
- web 전용 명령(`/help`·`/sh`·`/compact`)을 run 에 열지 않는다 — 표면이 결정.

## 3. 구조

```
                 ┌────────────── SessionPump.run() ──────────────┐
                 │ loop:                                          │
  InputQueue ───▶│  surface.idle() · waker.mark_idle()            │
  (run: argv 1건 │  item = queue.dequeue_blocking(poll)           │
   web: HTTP,    │  SHUTDOWN → return                             │
   예약, wake)   │  None(timeout) → if policy.should_stop(): return│
                 │  verdict = waker.handle_dequeued(text)         │
                 │  skip → continue                               │
                 │  surface.busy() · surface.echo(item, wake)     │
                 │  registry.set_current_run_authors(...)         │
                 │  stop = Event(); surface.bind_stop(stop)       │
                 │  try:                                          │
                 │    if surface.route(text): continue            │
                 │    res = run_loop(query=text, author…, stop…)  │
                 │    main_run_ended(registry, res.output)        │
                 │    surface.run_ended(res)                      │
                 │  except Exception as e:                        │
                 │    if not surface.run_failed(e): raise         │
                 │  finally: surface.bind_stop(None); waker.on_run_end()
                 └────────────────────────────────────────────────┘
        ▲ run: 메인 스레드, 표면=ConsoleSurface, policy=QuietPolicy
        ▲ web: 워커 스레드, 표면=WebSurface,     policy=ForeverPolicy
```

### 3.1 `SessionPump` (`agent_cli/pump.py`, 신규 ~120줄)

```python
@dataclass(frozen=True, kw_only=True)
class PumpDeps:          # 세션 수명 객체 — 한 번 조립
    queue: InputQueue
    waker: MailWaker
    agent_registry: AgentRegistry
    run_main: Callable[[RunRequest], ToolResult]   # run_loop 바인딩 (아래 3.4)
    surface: PumpSurface
    policy: LifetimePolicy

class SessionPump:
    def __init__(self, deps: PumpDeps): ...
    def run(self) -> None:   # 위 그림. KeyboardInterrupt 는 잡지 않는다 (호출자 정책)
```

`RunRequest` = `{text, author, author_is_user, request_id, stop_event, wake}` —
run_loop 의 query_* 인자를 한 덩어리로. run 은 author `""`·request_id `""`
로 보내며 `run_loop` 은 이미 둘 다 기본값으로 받는다(`loop/run.py:41-43`).

### 3.2 `LifetimePolicy` — "왜 살아 있는가" 한 곳

```python
class LifetimePolicy(Protocol):
    poll_secs: float | None          # None = 타임아웃 없이 대기
    def should_stop(self) -> bool    # 타임아웃 틱마다 질문
```

- `QuietPolicy(queue, registry, monitors, schedules, on_schedule_wait)` —
  지금 `_quiet()` + `_waiting_on_schedules()` 그대로. `poll_secs=0.5`.
- `ForeverPolicy()` — `poll_secs=None`, `should_stop()` 은 호출되지 않는다
  (SHUTDOWN 만 끝낸다).

둘이 공유하는 술어 `has_live_work(queue, registry, monitors, schedules) -> bool`
을 `runtime.py` 에 두고 `QuietPolicy.should_stop = not has_live_work(...)`,
web 의 `web_instance_is_active = 뷰어 ∨ busy ∨ has_live_work(...)` 로 쓴다.
→ 새 "살아 있을 이유" 가 생기면 **한 줄**만 바뀐다 (ARCHITECTURE 의 "두 곳에
같은 한 줄" 해소).

### 3.3 `PumpSurface` — 표면이 다른 것 전부

```python
class PumpSurface(Protocol):
    def idle(self) -> None                       # web: renderer.worker_idle(); run: no-op
    def busy(self) -> None                       # web: renderer.worker_busy(); run: no-op
    def echo(self, item: dict, *, wake: bool)    # web: agent_wake / push_user_message 카드
                                                 # run: wake 면 "🤝 에이전트 회신 배달" 한 줄
    def bind_stop(self, ev: Event | None)        # web: server.set_stop_handle; run: no-op
    def route(self, text: str) -> bool           # web: handle_slash_command → try_dispatch…(web_output)
                                                 # run: /sh 제외 가드 + try_dispatch…(console)
    def run_ended(self, res: ToolResult)         # run: --result-file (성공 시); web: no-op
    def run_failed(self, exc) -> bool            # web: renderer.error → True(계속); run: False(전파)
    def announce(self, *, revived, auto, stale, monitor_notice)   # 부트 알림
    graceful_interrupt: bool                     # web True / run False (run_loop 인자)
```

- `ConsoleSurface` — `main.py` 의 run 전용 print 들을 모은다 (~50줄).
- `WebSurface(renderer, server, ctx, web_output, dispatch_kwargs)` —
  `agent_cli/web/surface.py` (~70줄). `route` 안의 `try_dispatch_agent_or_skill`
  호출에 `stop_event` 가 필요하므로 `bind_stop` 이 받은 이벤트를 들고 있다가
  `route` 에 넘긴다(지금의 `noqa: B023` 클로저를 명시 상태로).

**라우팅 정책 보존**: run 의 `route` 는 지금처럼 `/` 로 시작하고 `/sh` 가
아니며 `looks_like_slash_command` 일 때만 디스패치한다. 단 지금은 **첫 질의에만**
적용되고 펌프 아이템(wake)엔 적용되지 않았는데, wake 텍스트는 `WAKE_TEXT`
상수(`/`·`@` 로 시작하지 않음)라 매 아이템에 적용해도 동작이 같다.
`@agent task` 직행 경로(main.py 1482-1507) 도 `route` 로 흡수한다 — 이미
`try_dispatch_agent_or_skill` 이 web 에서 같은 일을 한다.

### 3.4 조립 — `runtime.assemble_main_session(...)`

지금 run(1381-1465) 과 web(2401-2437) 이 각자 하는 것: `build_agent_registry`
→ `wire_agent_mail` → 부트 알림 → 이전 모니터 통지. 한 함수로:

```python
def assemble_main_session(*, session_dir, runtime: AgentRuntime, max_agents,
                          monitors, enqueue_wake, on_mail_notice, parent_ctx,
                          surface: PumpSurface) -> tuple[AgentRegistry, MailWaker]
```
반환 뒤 호출자가 `run_main` 바인딩을 만들어 `PumpDeps` 에 꽂는다.
**런 도중 주입은 run 도 켠다** (사용자 결정 2026-10-11): `ports_for_run` 과
`ports_for_web` 의 차이(`dequeue_user_message`·`route_message`)가 사라지므로
둘을 `ports_for_main(agent_registry, mcp_manager, dequeue_user_message,
route_message)` 하나로 합친다. 주입 콜백은 둘 다 `queue.dequeue_nowait` 과
`surface.route`.

run 에서 런 도중 큐에 들어올 수 있는 것은 **예약 발화뿐**이다 — wake 는
`MailWaker.on_mail` 이 idle 일 때만 무장하고(런 중엔 메일박스가 턴 경계에서
직접 배달), stdin·HTTP 입력원은 없다. 즉 사용자 관점 변화는 "런 도중 울린
예약이 런이 끝난 뒤 새 런으로 돌던 것이 web 처럼 다음 턴 경계에 요청으로
합류한다" 하나다.

**wake 아이템은 펌프가 처리한다 (주입 경로에서 발견한 잠복 버그)**:
`AgentLoop._inject_queued_messages` 는 큐를 통째로 비우며 `system` 아이템
(wake)도 사용자 메시지로 넣고 `waker.handle_dequeued` 를 부르지 않는다. 그러면
`_armed` 가 영영 True 로 남아 이후 `on_mail`/`on_run_end`/`mark_idle` 의
무장이 전부 no-op 이 된다. 실제로 생기는 순서: `--resume` 로 미배달 회신이
있는 세션 → `mark_idle` 이 첫 질의를 꺼내기 **전에** 무장 → 큐 `[질의, WAKE]`
→ 첫 턴 경계에서 WAKE 가 주입으로 소비. web 은 다음 사람 메시지가 런을 열어
자연 회복되지만, run 은 `has_active_work()`(미배달 회신) 로 펌프가 영원히
폴링한다. 해결은 한 곳: `surface.route` 앞에 펌프 공용 전처리 —
`text == WAKE_TEXT` 면 `waker.handle_dequeued(text)` 만 부르고 `True`(처리됨)
를 돌려준다(메일 자체는 같은 턴 경계의 `_absorb`/메일박스가 배달하므로
텍스트를 컨텍스트에 넣을 이유가 없다 — web 의 컨텍스트 오염도 같이 사라짐).
2번 PR 에 테스트와 함께 넣는다.

### 3.5 스레드·종료는 그대로

- run: 메인 스레드에서 `pump.run()`; `KeyboardInterrupt` 는 지금처럼 `run()` 의
  `except` 가 받는다; `finally _finalize_run`.
- web: 워커 스레드에서 `assemble_main_session` + `pump.run()`
  (`_worker_loop_guarded` 의 traceback 보고는 유지); uvicorn·idle self-reap·
  `finally teardown_session` 은 그대로.

## 4. 바뀌는 것 / 안 바뀌는 것 (사용자 관점)

| | 전 | 후 |
|---|---|---|
| run 의 wake 아이템 라우팅 | 없음 | `route` 를 타지만 wake 텍스트는 어차피 통과 — **동일** |
| run 의 `@agent task` 첫 질의 | 전용 분기 (1482-1507) | `route` 로 흡수 — 결과 파일 기록 규칙(관찰 래퍼 벗김) 은 `ConsoleSurface.route` 안에 보존 |
| run 의 런 예외 | 전파·비정상 종료 | **동일** (`run_failed` 가 False) |
| web Stop 버튼 | 아이템마다 stop_event | **동일** (`bind_stop`) |
| web idle/busy 순서 | idle → dequeue → (skip이면 다시 idle) → busy | **동일** — 펌프가 순서를 갖는다 |
| 모니터·예약 수명 | run 펌프 + web 술어 두 곳 | `has_live_work` 한 곳 |
| `--result-file` | `_run_one` 안 | `ConsoleSurface.run_ended` |

## 5. 작업 순서 (PR 3개, 각각 독립 릴리스 가능)

1. **`has_live_work` 추출** (runtime.py) — `_quiet()` 와
   `web_instance_is_active` 가 같은 함수를 쓰게. 동작 불변. 테스트:
   `test_monitor_wiring`·`test_schedule_wiring` 의 펌프/술어 테스트가 그대로
   통과 + 술어 단위 테스트.
2. **`SessionPump` + `ConsoleSurface` 로 run 전환** — `_run_message_pump`·
   `_run_one`·`@agent` 분기 삭제. `test_runtime_assembly` 의 펌프 스텁
   테스트는 `SessionPump` 를 가짜 표면으로 모는 테스트로 바뀐다(결과 파일
   4종 포함). 펌프 자체 테스트: SHUTDOWN 종료, 타임아웃→정지 판정, skip 판정,
   예외 정책 두 갈래, idle/busy/bind_stop 호출 순서.
3. **`WebSurface` 로 web 전환** — `_worker_loop` 본문 삭제(조립+펌프 호출만
   남음). `test_web_renderer`·`test_web_server` 의 워커 관련 테스트 확인.
   라이브 확인: 보드 방 복사본으로 Stop 버튼·주입 메시지·wake 카드·idle
   self-reap.

LOC 추정: 신규 pump.py ~120 + web/surface.py ~70 + ConsoleSurface ~50 =
+240; 삭제 `_run_message_pump` 65 + `_run_one`/`@agent` 분기 60 +
`_worker_loop` 본문 ~170 = −295. main.py 는 ~2,450 으로.

## 6. 위험

- **web idle/busy 타이밍**: 프런트의 전송 버튼 상태가 이 신호를 본다. 펌프가
  순서를 고정하므로 오히려 안전하지만, 3번 PR 은 라이브로 확인한다.
- **run 의 author 메타**: 지금 run 은 `query_author` 를 안 넘긴다(None).
  펌프가 `""` 을 넘기면 `run_loop` 쪽 분기(`query_author_is_user`, 귀속)
  가 달라질 수 있다 — 2번 PR 에서 `None` 으로 보내 동작을 고정하고 테스트로
  핀.
- **run 의 라우팅 확대**: `@`·`/` 가 첫 질의 외에서도 해석된다. 들어올 수
  있는 아이템은 wake 와 예약 발화뿐이고 둘 다 상수/사용자 프롬프트라 실질
  변화 없음 — 예약 프롬프트가 `/` 로 시작하면 지금 web 과 같은 동작(디스패치)
  이 되는데, 이것은 web 과의 **일치** 다.
- **run 의 주입 가시성**: 주입된 아이템은 `renderer.push_user_message` 로
  에코되는데 `MinimalRenderer` 는 no-op 이라 콘솔엔 아무것도 안 보인다.
  예약 프롬프트가 런 중간에 합류한 사실은 보여야 하므로 `MinimalRenderer.
  push_user_message` 를 한 줄 출력(`⏰ [nickname]: …`)으로 채운다(2번 PR).
- **주입된 요청의 회계**: 주입 아이템은 `run_requests` 에 올라 답하기 전엔
  런이 안 닫힌다(v9.17.0 nag). web 과 같은 규칙이고 `--max-turns` 가 상한이라
  run 에서 무한정 길어지진 않는다 — 바뀌는 동작이므로 README 에 적는다.
- **`noqa: B023` 클로저**: web 의 `route_one`/`_run_main` 이 반복마다
  stop_event 를 캡처하던 것을 `WebSurface` 의 명시 상태로 바꾸면 ruff 예외가
  사라진다 — 상태를 들고 있는 동안 두 아이템이 겹칠 수 없음(워커 1개)을
  주석으로 적는다.
