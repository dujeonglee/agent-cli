# 배선 seam — 주소 배달 + 포트 데이터

> 7판 (확정). 2판(블로커 2)·3판(블로커 4)·4판(결함 7)·5판(스펙 공백 3)·6판(스펙 1)
> fable 리뷰를 반영. **6판 판정: 구현 안전.** 7판은 그 마지막 한 줄을 넣은 것.
> **목표는 고정**(§0) — 축소하지 않는다.

## 0. 목표 (고정)

- **G1** — 모니터 알림은 **설치한 주소로** 배달된다. main 이 걸면 main 에게,
  에이전트가 걸면 그 에이전트에게.
- **G2** — 앞으로 배선이 필요한 기능이 붙을 때 **쉽게** 붙는다.

## 1. 문제

### 1.1 배선 누락이라는 버그 클래스

| # | 빠진 것 | 증상 | 어떻게 드러났나 |
|---|---|---|---|
| 1 | `run_loop` 가 `monitor_registry` 를 **파라미터로 안 받음** | 첫 LLM 턴에서 `TypeError` | 릴리스 **두 개**가 깨진 채 나간 뒤 |
| 2 | `wrap_single_op` 기본이 키에 접두사를 붙임 | monitor 가 v9.11.0 이래 **한 번도** 동작 안 함 | 사용자 라이브 제보 |
| 3 | waker 술어에 `monitors.has_pending()` 누락 | 발화해도 **유휴 main 이 안 깸** | 사용자 라이브 제보 |

셋 다 기능은 있었고 **잇는 선**이 없었다. 셋 다 유닛 테스트가 초록이었다 —
테스트가 협력자를 직접 만들어 검사하므로 main 이 그걸 만들어 넘기는지는 아무도
안 본다. #1 만 시끄러웠고(파라미터 부재 → `TypeError`), #3 은 `None` 으로 조용히
통과했다. **이 대비가 설계 원칙이 된다: 누락은 시끄러워야 한다.**

### 1.2 2판이 실패한 지점 — 라이브락

2판은 `MonitorRegistry.add(owner)`/`drain(owner)` 만으로 G1 이 된다고 했다. 에이전트
소유 감시가 발화하면 `on_report` 가 **main 의** `MailWaker`(`runtime.py:135`)를 울려
main 이 빈 wake 턴을 돌고, `on_run_end` 가 다시 무장 — 무한 루프. 게다가 `kill()`
이 모니터를 안 건드려 죽은 owner 의 `_pending` 이 `has_active_work()`
(`registry.py:156`)를 영원히 참으로 만들어 프로세스가 종료되지 않는다.

**교훈**: 모니터 보고는 "drain 대상" 이 아니라 **주소가 있는 배달물**이다.

### 1.3 3판이 실패한 지점 — owner 를 나를 길이 없다

3판은 "스킬·oneshot 루프는 부모의 owner 를 물려받는다" 고 **단언만** 했다. 실제로
그걸 나르는 것이 없다: `_handle_run_skill`(`skill_invoke.py:25-45`) ·
`execute_skill`(`executor.py:123-148`) · `tool_delegate`(`oneshot.py:359`) ·
`run_subagent_message`(`runner.py:152-180`) 에 그런 파라미터가 없고, `RunContext` 는
**그 루프 자신의 cfg** 로 매 루프 재조립된다(`tool_bridge.py:406-410`). 스레드
로컬도 안 된다 — `_run_parallel` 이 새 스레드에서 돈다(`oneshot.py:331`).

기본값 `"main"` 을 두면 **상주 에이전트 안의 스킬이 건 감시가 main 소유로
등록되고 아무 에러도 안 난다** — #1 의 조용한 형태. §3.4 가 이걸 푼다.

### 1.4 진짜 병 — 의도와 실수가 같은 모양이다

`hook_runner` 가 어디서도 안 넘어가는 걸 보고 버그로 오진했다. 실제로는 의도된
미연결이었다. **둘 다 `None` 이라서** 구분이 안 됐다. `ask_handler` 는 같은
모양이지만 **진짜 죽은 코드**다 — 생산자 없음(`oneshot.py:151`,
`agents_live.py:2214`), `dispatch.py:680-681` 도달 불가.

## 2. 실측

### 2.1 같은 사실을 세 곳에 손으로 적는다

`run_loop` **39** 파라미터 / `LoopConfig` **29** 필드 / `handler_resources` **3**
엔트리(`loop/core.py:151`). 새 능력 = 선언 3 + 호출 4 = **7곳**, 그중 여섯은
빠뜨려도 예외가 안 난다.

단, 11 포트 중 **4개는 `LoopConfig` 필드가 아니다**: `ctx`(협력자 넷에 위치 인자)
· `stop_event`(**가변** `LoopState` + setter `core.py:405-407` + 루프 자체 기본값
`core.py:217`) · `dequeue_user_message` · `route_message`(`self.*`).

### 2.2 조립 지점은 다섯이다

`run_loop` 호출은 넷이지만 agent 는 성격이 다른 두 생산자의 깔때기다 —
`oneshot.py:151` 과 `agents_live.py:2207`.

| 포트 | run | web | skill | oneshot | resident |
|---|:--:|:--:|:--:|:--:|:--:|
| `ctx`                  | ✔ | ✔ | ✔ | ✔ | ✔ |
| `stop_event`           | · | ✔ | ✔ | · | ✔ |
| `dequeue_user_message` | · | ✔ | · | · | · |
| `route_message`        | · | ✔ | · | · | · |
| `mcp_manager`          | ✔ | ✔ | · | · | · |
| `hook_runner`          | · | · | · | · | · |
| `agent_registry`       | ✔ | ✔ | ✔ | · | · |
| `monitor_registry`     | ✔ | ✔ | · | · | · |
| `message_handler`      | · | · | · | · | ✔ |
| `questions`            | ✔ | ✔ | · | · | ✔ |

(`ask_handler` 는 실값 생산자가 없어 뺐다 — §4.1 에서 삭제.)

### 2.3 주소 배달은 **여섯 곳**에 있고, 인자가 제각각이다

3판은 "같은 모양이 세 번" 이라고 썼다. **틀렸다.** 여섯이고, 공통은 *두 백엔드*
뿐이다:

| 호출부 | addr 형식 | `author` | `expects_reply` | 그 외 |
|---|---|---|---|---|
| `_deliver_question` `:762` | `"main"`/`"agent:k"` | asker 파생 | False | `question_id`, `to_human` 조기반환, `_render_question` 선행 |
| `_deliver_answer` `:976` | **맨 키** `q.asker` (`:1000`) | `q.target` | **True** | — |
| `remind_owed` `:900` | `"main"`/`"agent:k"` | 첫 asker 파생 `:971` | False | question_id 없음 |
| `_deliver_peer_reply` `:1293` | 맨 키 | — | False | `hop+1`, hop 상한, 사망 시 조용히 드롭 |
| `message_to_main` `:1326` | main 전용 | — | — | 메일박스만 |
| `_make_message_handler` `:1381` | 맨 키 | — | **True** | `_log_outbound` |

main 측 페이로드도 kind 마다 다르다(`id` 는 question/answer 만, `profile`/`name` 은
question 만). **주소 어휘조차 통일돼 있지 않다** — `_deliver_answer` 는 맨 키를 쓴다.

그래서 접히는 것은 **main 분기(`_push_reply(mail)`) + `agent:` 디코드**뿐이고,
정직한 서명은 이렇다:

```python
def deliver(self, addr: str, *, mail: dict, text: str,
            author: str, expects_reply: bool,
            question_id: str = "", hop: int = 0) -> str:
    """addr: "main" | "agent:<key>". 에러 문자열 또는 ""."""
```

**페이로드가 둘인 이유**: 두 백엔드는 나르는 것이 다르다. 메일박스 아이템은
**구조**를 싣고(`kind`/`id`/`key`/`profile` — UI 렌더와 `answer(id)` 가 읽는다),
inbox 는 **평문**을 싣는다. 답·독촉은 같은 문자열을 두 번 주지만 **질문만
본문이 갈린다** — main 은 질문 원문, 에이전트는 출처를 머리에 단 한 줄. 하나의
`item` 으로는 그 경로를 원형 그대로 재현할 수 없다. (C1 구현에서 확정.)

**6줄짜리 헬퍼**다. "같은 두 호출을 같은 인자로" 가 아니다.

### 2.4 이미 같은 처방을 한 적이 있다

`runtime.py` 의 `AgentRuntime`(13키 단일 정의)·`teardown_session`(종료 경로 5→1)은
이 클래스를 잡으려고 만든 것이고, 그 뒤 그 두 종류는 재발하지 않았다.

## 3. 설계 — 축 A: 주소 배달 (G1 + G2 의 절반)

### 3.1 `AgentRegistry.deliver(addr, *, mail, text, …)`

§2.3 의 서명 그대로. `addr == "main"` → `_push_reply(mail)`;
`addr.startswith("agent:")` → `request(key, …)`. 여섯 호출부 중 **주소 분기가 있는
셋**(`_deliver_question`·`_deliver_answer`·`remind_owed`)을 이걸로 접는다.
`_deliver_answer` 의 맨 키는 호출부에서 `f"agent:{q.asker}"` 로 만든다.

등가성 검증: 접기 **전후로 각 호출부의 `request()` kwargs 를 스냅샷**해 비교한다
(인자가 사이트마다 다르므로 "같은 두 호출" 이라는 추론은 증거가 아니다).

### 3.2 모니터는 네 번째 고객

- `Monitor.owner: str`. `MonitorRegistry.add(..., owner)`.
- 발화 시 `_pending` 에 쌓지 않고 **즉시**
  `deliver(owner, mail={"kind": "monitor", "success": True, "output": report},
  text=report, author="main", expects_reply=False)`.

**`"success": True` 는 빠뜨리면 안 된다** (리뷰 B1): `_deliver_agent_mail` 이
`success=bool(reply.get("success"))` 로 렌더한다(`core.py:770`). 없으면 모든
모니터 보고가 **실패 카드(빨강)** 로 그려진다 — 오늘 `core.py:730` 은 `True` 다.
다른 메일박스 생산자는 전부 명시한다(`:776`, `:964`, `:990`, `:1333`, `:1961`,
`:2103`).
- `MonitorRegistry` 는 콜러블 하나(`registry.deliver`)를 받는다.

**`author="main"` 은 필수다** (리뷰 B3): `_handle_request` 가
`tm.current_author = author`(`:2011`)로 두고, 이 값이 그 런에서 에이전트가 거는
`ask` 의 **대상**이 된다(`QuestionPort.ask` `:526`). 주소가 아닌 author(예:
`"monitor"`)면 `_deliver_question` 이 `"unroutable question target"`(`:801`)을
반환하고 `register_question` 이 질문을 취소한다(`:748`) — 에이전트의 ask 가 깨진다.
부작용으로 팀뷰 화살표가 main→agent 로 읽히고 `query` 는 보고 원문이 된다. 적어둔다.

**`expects_reply=False`** (§7 B 확정): 그 런의 산출물은 `_persist_reply` ·
에이전트 창(`:2053-2066`) · `conversation.jsonl` 에 **그대로 남는다**. 억제되는 건
main 으로의 재주입뿐이고, 질문·독촉·peer 회신과 **동일한 의미**다(`:2095-2099`).
`True` 로 두면 발화마다 main 메일박스에 `kind:"reply"` 가 꽂히고 main 이 발화마다
깨며 `answers` 가 그때 main 이 돌던 런에서 잘못 찍힌다. 대신 `_deliver_peer_reply`
가 쓰는 안내 꼬리(`:1311-1316`)를 붙인다 — "보고할 게 있으면 `message` 로 main 에".

### 3.3 배선 지점 — `build_agent_registry` 직후

3판은 `wire_agent_mail(monitors=…)` 를 삭제한다고 썼다. 틀렸다:
`build_monitor_registry` 가 **agent registry 보다 먼저** 돈다(run `main.py:1379`
vs `:1387`; web `:2334` 모듈 수준 vs `:2363` 워커 스레드 안).

4판은 그래서 `wire_agent_mail`(`:1452`/`:2388`)에 배선하려 했다. **그것도
늦다** (리뷰 B3): run 의 **스킬 조기-반환 경로**(`try_dispatch_agent_or_skill`
`:1424`)가 `wire_agent_mail`(`:1452`)**보다 먼저** 돈다 → `agent-cli run "/skill …"`
에서 `monitor add` 가 거부된다.

agent registry 는 run `:1387`·web `:2363` 에서 이미 서 있고 **둘 다 어떤 루프보다
먼저**다. 그러므로 배선은 거기다:

```python
monitors.deliver = registry.deliver     # build_agent_registry 직후
registry.monitors = monitors            # 역방향 — §3.6 이 쓴다
```

배선 안 된 `AgentRegistry`(테스트 픽스처 다수)는 `monitors` 가 `None` 이다 —
워커 `finally` 와 `kill`/`shutdown_all` 의 조기 드롭은 **`if self.monitors is not
None` 으로 가드**한다. 안 그러면 에이전트를 kill 하는 기존 테스트가 전부
`AttributeError` 를 낸다.

**역방향이 필요한 이유** (리뷰 B2): `AgentRegistry` 는 오늘 모니터 레지스트리를
**전혀 모른다**(`grep monitor agents_live.py` → 0건). §3.6 이 워커 `finally` 에서
`drop_owner` 를 부르려면 참조가 있어야 한다. 도구가 쓰는 전역
(`monitor_tool.py:18-21`)을 `finally` 에서 집어올 수도 있지만, **주입**으로 둔다 —
레지스트리 테스트들이 가짜를 꽂을 수 있어야 한다.

**위치가 아니라 제약이 본질이다** (리뷰 A4): 배선은 `wire_agent_mail` 의
`restore`/`auto_spawn`(run `:1452`, web `:2388`) **보다 먼저** 끝나야 한다 —
`_adopt_questions`(`:1755-1768`)가 부활한 inbox 로 질문을 재배달하고 그 워커들이
즉시 루프를 시작하기 때문이다. `build_agent_registry` 직후(run `:1387`,
web `:2363`)가 그 제약을 만족하는 가장 이른 지점이다.

(`build_agent_registry(..., monitors=…)` 로 넣거나 두 줄짜리
`wire_monitor_delivery` 를 바로 뒤에 부른다. `wire_agent_mail` 의 `monitors`
인자는 `on_report` 배선이 없어지므로 **삭제**된다 — 3판이 옳았고 4판이 틀렸다.)

이러면 거부 창이 **두 호스트 모두에서 사라진다**: web 은 모든 도구 호출이
`:2388` 이후 워커 안에서 일어나고, run 은 `:1387` 이 스킬 디스패치보다 앞선다.

그래도 `MonitorRegistry.add` 는 `deliver` 가 없으면 **ToolResult 에러로 거부**한다
— §1.1 의 원칙("누락은 시끄러워야 한다"). 이제 그 거부는 **도달 불가**가 목표고,
도달하면 배선이 깨졌다는 뜻이다.

### 3.4 owner 전달 — 기본값 없이, 모든 seam 에

§1.3 이 이 설계의 가장 약한 지점이었다. 해법은 **기본값을 두지 않는 것**이다.

- `LoopPorts.owner`(§4.2, 기본값 없음) → `LoopConfig.owner` →
  `RunContext.owner`(`tool_bridge.py:406-410` 조립. owner 는 **루프 상수**라 기존
  캐시 무효화 조건 "per-call-VARYING" 에 안 걸린다) → `MonitorTool._run(args, *,
  ctx)` 가 `ctx.owner` 로 등록.
- 상주 루프의 값은 `f"agent:{tm.key}"`(`agents_live.py:2207`), 그 외 `"main"`.
  (`QuestionPort.me`(`:517`)가 같은 값이지만 스킬 루프는 `questions` 를 안 받아
  파생이 불가능하다 — 넘겨야 한다.)

**seam 전수** (4판의 "9곳" 은 과소집계였다 — 리뷰 A1):

| 분류 | 지점 |
|---|---|
| 선언 2 | `AgentLoop.__init__` · `LoopConfig` |
| 중첩 seam **7** | `run_loop` · `_handle_run_skill`(`skill_invoke.py:25`) · `execute_skill`(`executor.py:123`) · `tool_delegate`(`oneshot.py:359`) · **`_run_single`**(`oneshot.py:40`) · **`_run_parallel`**(`oneshot.py:210`) · `run_subagent_message`(`runner.py:152`) |
| 루프 **밖** 호출자 2 | `_dispatch_agent` → `tool_delegate` (`main.py:654`) · `_dispatch_skill` → `execute_skill` (`main.py:763`) — 둘 다 `try_dispatch_agent_or_skill` 로 run `:1424`·web `:2466` 에서 도달. `owner="main"` 고정 |
| 조립 지점 5 | §2.2 |
| DI seam | `AgentRegistry` 의 `runner=` 주입 — 실측상 **비용 없음**: 가짜 정의 22개가 전부 `def runner(query, ctx, **kw)` 라 새 kwarg 를 그냥 흡수한다 (C3 가 `ports=` 로 가면 시그니처 변경도 C3 몫) |

전부 **기본값 없음** → 빠뜨리면 `TypeError`. 부모가 값을 준다: `dispatch.py:851`
과 `tool_bridge.py:349` 는 `self.cfg` 를 쥐고 있으므로 `owner=self.cfg.owner`.

**루프에 닿는 다른 길은 없다** (리뷰가 전수 확인): `restore`/`resume_teammate` 는
`_worker`→`_handle_request`→`_run_message`→`runner(...)`(`:2201-2236`) 로 가고,
`_run_parallel` 의 새 스레드도 `_run_single` 을 지나며, 웹의 메시지별 재조립은
조립 지점 #2 다. 훅(`hooks/`)과 MCP(`mcp/`)는 루프를 시작하지 않는다.

**인정하는 긴장**: 이건 §2.1 이 비판한 "손배선" 과 같은 모양이다. 차이는
**기본값이 없다**는 것뿐이고, 그 차이가 #1(시끄러움)과 #3(조용함)을 가른다.
그래서 `owner` 는 `LoopPorts` 의 필드로 태운다 — C3 를 C2 **앞**에 두는 이유이자
(§6), 4판이 틀렸던 지점이다.

### 3.5 렌더 — 한 줄이 필요하다

3판은 "렌더 불변" 이라고 썼다. **틀렸다** (리뷰 A2): `_deliver_agent_mail` 은
`tool_name="agent"` 를 **하드코딩**한다(`core.py:765-771`). 그대로 두면 모니터
보고가 빈 칩을 단 에이전트 회신 카드로 그려진다(`app.js:984` 가 `tool === "agent"`
를 특수 처리).

→ `render_step(..., tool_name=record["tool"], ...)` 로 바꾼다. ctx 레코드는
`build_reply_record` 의 `kind:"monitor"` 분기가 오늘 `_deliver_monitor_reports` 가
만드는 것과 **동일하게** 반환한다(`{"role":"user","tool":"monitor","success":True,
"content":report}`, `core.py:719-726`). 기존 kind 는 전부 `tool="agent"` 라 이
한 줄은 행동 불변이다.

### 3.6 수명 — kill·종료·유령

**`drop_owner(f"agent:{key}")` 의 정본 위치는 워커의 `finally`**
(`agents_live.py:1948-1970`, `_purge_questions_for` 옆)다 — kill·크래시·세션
종료가 **모두 수렴하는 유일한 지점**이고, 죽음이 확정된 곳이다. (독촉이
`:1934-1937` 에서 같은 논거로 그 자리를 쓴다.)

4판은 이걸 `kill()` 안에서 `stop_event.set()` **앞**에만 두려 했다. 그것만으론
누수가 남는다: busy 에이전트는 `stop_event.set()` 뒤에도 현재 턴을 마저
돈다(`join(2.0)` 은 best-effort). 그 턴에서 `monitor add` 를 부르면 **드롭 뒤에**
`owner=agent:k` 모니터가 새로 생겨 고아가 된다. 그래서 **정확성을 지는 것은
`finally` 쪽**이다.

**조기 드롭은 `kill()` 과 `shutdown_all()` 둘 다에 둔다 — 편의가 아니라 정확성
때문이다** (리뷰 B4). `stop_event.set()` 과 워커 `finally` 사이에는 두 창이
있다: `kill()` 의 `join(2.0)`(`:1498`)과 `shutdown_all()` 의
`join(5.0)`(`:1516` — 여기엔 4판에 조기 드롭이 아예 없었다). 두 곳 모두
`stop_event.set()` **앞**에서 `drop_owner` 를 부르면 그 창이 닫힌다.

- **`deliver`/`request` 는 `tm.stop_event.is_set()` 을 dead 로 취급**한다.
  `state="dead"` 는 워커 `finally`(`:1949`)에서야 찍혀 busy 런이면 몇 분 뒤고,
  그 창에 발화하면 항목이 `_SHUTDOWN` 뒤에 줄 서서 **영영 안 읽힌다**(`:1892`).
  이 판정이 안전한 근거: `tm.stop_event` 를 세우는 곳은 `kill`(`:1495`) 과
  `shutdown_all`(`:1513`) **둘뿐**이다. `core.py:518` 의 SIGINT 핸들러는 메인
  스레드에만 설치되고(`:506-507`) 상주 워커는 메인 스레드가 아니다. 웹
  `/api/stop` 은 main 의 이벤트를 세우지 상주의 것을 세우지 않는다. **죽지 않는데
  stop_event 가 서는 경로는 없다.**
- 재부모화하지 않는다. 폐기 건수는 `died`/종료 통지에 한 줄.
- **배달 실패 시 main 통지는 만들지 않는다.** 5판은 "`deliver` 실패 ∧
  `not mon.dropped` 일 때 통지" 규칙을 뒀는데, 위의 두 조기 드롭 + §3.7 의 배달
  직전 `alive` 재확인이 들어가면 그 분기는 **도달 불가**다(리뷰 B4): 남는 경합은
  "재확인 통과 → 드롭·사망 확정 → `request()` 실패" 뿐이고 그때는
  `mon.dropped` 가 참이다. `request()` 가 `"unknown agent"` 를 낼 일도 없다 —
  묘비는 `_agents` 에서 제거되지 않는다(`kill :1494`, `restore :1722-1726`).
  도달 불가 분기를 남기지 않는다 — **삭제**하고, 대신 `deliver` 실패에
  `debug_log` 를 남겨 미래의 회귀가 보이게 한다. 모니터는 은퇴시킨다.
  (`Monitor.dropped` 플래그는 그대로 둔다 — 재확인과 로그의 판정 축이다.)

### 3.7 생존 판정

- `MonitorRegistry.has_active_work()` 는 **살아 있는 모니터만** 센다 — `_pending`
  이 없다(즉시 배달).
- 배달된 뒤 안 읽힌 것: `run` 펌프(`main.py:1639`)는
  `AgentRegistry.has_active_work()`(메일박스 `_pending` ∨ busy ∨ `inbox.qsize()`,
  `:682-694`)가 **이미** 덮는다. **웹은 다르다**(리뷰 A4): `web_instance_is_active`
  (`main.py:1614`)는 `any_activity()`(`:651-668`)를 쓰는데 그건 **의도적으로**
  메일박스 `_pending` 을 보지 않는다. 웹은 `server.pending_count()` +
  `worker_is_busy()` 로 덮인다 — 같은 결론, **다른 기계**다. 3판의 근거는 두 호스트
  중 하나에서 틀렸다.
- **retire-before-deliver 공백을 닫는다**: `_retire` 는 `mon.retired` 를 세운
  뒤(`:279-281`) `_run_side_effect`(서브프로세스, 최대 `COMMAND_TIMEOUT_S`)를
  돌고 **그 다음** 배달한다. 그 사이 두 술어가 모두 거짓이 되어 `_quiet()` 이
  참 → `teardown_session` 이 보고를 날리며 종료할 수 있다.

  **"배달 후 retire" 로 고치면 안 된다** (리뷰 B5): `retired` 선행은
  `drop_owner`/`delete` 가 폴링 스레드와 경합할 때의 **멱등성 가드**다. 또
  `_run_side_effect` 선행도 유지한다 — 그 종료 코드가 보고의 일부다(`:224-228`).
  대신 **in-flight 카운터**: `_lock` 아래서 부작용 **전에** 증가,
  **`finally` 에서** 감소. `has_active_work()` = 살아 있는 모니터 ∨
  `_inflight > 0`.

  **`finally` 가 필수다** (리뷰 B1): `_loop` 는 `tick` 을
  `except Exception: pass` 로 감싼다(`registry.py:201-204`). 증가와 감소 사이에
  예외가 한 번 나면 `_inflight` 가 **영원히** 0 이 아니고 →
  `has_active_work()` 영원히 참 → `_quiet()`(`main.py:1637-1639`)이 영영 거짓,
  `web_instance_is_active` 도 영영 유휴 아님. **2판의 누수가 다른 카운터로
  부활한다.** 같은 이유로 **`deliver` 는 예외를 던지지 않는다** — 에러 문자열을
  반환한다(`_push_reply` 의 `on_reply` 는 이미 그렇지만 `:1290-1293`,
  `request()` 의 렌더러·`_log_conversation` 파일 IO 는 아니다).
- **배달 직전 `_lock` 아래서 `mon.alive` 재확인**한다. 그러면 드롭된 모니터는
  `deliver` 를 **아예 시도하지 않는다**(§3.6 의 "드롭은 통지 안 함" 이 그만큼
  단순해진다). `drop_owner` 와 `delete` 는 둘 다 `retired`(+`dropped`)를 세운다 —
  오늘 `delete()` 는 pop 만 해서(`registry.py:129-134`) 스냅샷된 tick 이 삭제 후
  한 번 더 발화할 수 있다(기존 결함).
- **폴링 스레드를 종료가 멈춘다** (리뷰 B3): `MonitorRegistry.stop()` 은 오늘
  **호출자가 없고** `teardown_session`(`runtime.py:165-172`)에 `monitors`
  파라미터가 없다. 지금은 세션 종료 후 발화해도 읽히지 않는 `_pending` 에
  쌓일 뿐이라 무해하지만, C2 이후엔 **배달된다** — main 소유면
  `finalize_session` **뒤에** `_save_state()` 가 돌고, 에이전트 소유면 세션과
  함께 죽은 모니터의 폐기 흔적이 다음 세션에 떠오른다.
  → `teardown_session(..., monitors=…)` 를 더하고 **`shutdown_all` 앞에서**
  `monitors.stop()` + **`closed` 플래그**를 세운다. 종료 시퀀스의 단일 소유자가
  그 함수라는 게 이미 그 docstring 의 주장이다. (이것도 §2.1 이 비판하는 손배선
  한 줄이다. 다만 호출자가 둘뿐이고(`main.py:1746`, `:2687`) 기본값이 누락을
  가리지 않는다.)

  **전체 드롭이 아니라 `closed` 플래그인 이유** (리뷰 B1): `drop_owner` 가
  `delete` 처럼 `_save()` 를 부르면(`registry.py:136`) `_save` 는 **살아 있는
  행만** 쓰므로(`:295-302`) 정상 종료가 `monitors.json` 을 **비운다**. 그러면
  다음 세션의 "🔔 이전 세션의 모니터 N건은 복원되지 않았습니다"
  통지(`main.py:1473`/`:2396`, `describe_previous`)가 조용히 사라진다 —
  문서화된 §8 동작의 회귀다. 그래서 둘 다 못박는다:

  - **`drop_owner` 는 `_save()` 를 부르지 않는다** (`_retire` 와 같다).
  - 종료는 드롭 대신 `closed` 를 세운다. `add`(거부)와 배달 직전 `alive`
    재확인(건너뜀 — `_inflight` 는 `finally` 가 감소)이 **이 둘만** 이걸 본다.
    부수 효과로 `start()` 의 `_stop.clear()`(`:187`)가 무해해진다 — 안 그러면
    마지막 턴의 `add` 가 폴링 스레드를 되살릴 수 있다.
  - **`closed` 를 `has_active_work()` 에 섞지 않는다.** 두 생존 소비처는 종료가
    *시작되기 전에만* 읽힌다 — `_quiet()` 이 참이 되는 것이 런 펌프를 끝내고
    (`main.py:1637-1639`) 그 뒤에 teardown 이 오며, 웹은 `IdleMonitor` 가
    `should_exit` 를 세운 뒤 `_idle_loop` 가 틱을 멈춘다(`:2657`). `closed` 가
    서는 시점엔 아무도 그 술어를 다시 안 읽으므로 값이 무의미하고, 섞으면 오히려
    **틀린다**: 증가를 이미 지난 틱은 자기 `finally` 까지 `_inflight > 0` 이고 그
    수는 그 찰나 동안 정직해야 한다. `list_all`·`_loop`·`_save` 는 손댈 것이
    없다.
- `main.py:1614`/`:1639` 의 `monitors is not None and monitors.has_active_work()`
  두 항은 **그대로 둔다**. 이건 깨우기가 아니라 **생존 판정**이고 주소와 무관하다.
  2판이 이 둘을 `Wakeable` 로 덮겠다고 한 게 범주 오류였다.

### 3.8 삭제되는 것

- `AgentLoop._deliver_monitor_reports`(`core.py:698-733`) — 두 번째 배달 지점.
- `LoopConfig.monitor_registry` 포트 — 배달에만 쓰였다. **#1 이 났던 그
  파라미터가 사라진다.**
- `wire_agent_mail` 의 `monitors` **인자 전체** — 술어의
  `or monitors.has_pending()` 는 메일박스가 흡수하므로 얹을 항이 없고,
  `monitors.on_report` 는 §3.3 으로 옮겨간다. `runtime.py:126-135` 가
  `monitors` 의 유일한 사용처라 인자가 통째로 없어진다.
  (`_run_message_pump(monitors=)` 와 `web_instance_is_active(..., monitors)` 는
  생존 판정이므로 §3.7 대로 남는다.)
- `Wakeable`/`Drainable` 프로토콜(2판 §3.3) — 폐기. `runtime_checkable` 은 이름
  존재만 보고(실측: 비콜러블 속성·틀린 서명·`MagicMock` 전부 통과) 두 drain 은
  페이로드가 달라 대체 불가다. 배달을 주소로 통일하면 순회 대상이 없어진다.

### 3.9 새로 생기는 표면 (리뷰 B5·B6)

- **main 소유 보고만 영속된다**: main 소유 보고와 폐기 통지는 `_push_reply` →
  `_save_state()`(`:1287`) → `agents.json` 의 `pending` 미러 → 다음 세션
  `restore()`(`:1680-1685`)가 재배달한다. 모니터 자체는 부활하지 않지만 **그
  보고는 부활한다.** `restore` 가 넣는 항목은 `key=""`/`label=""` 로 무해하게
  처리되고(`:295-296`) monitor 분기는 유효한 레코드를 낸다 — `success` 키가
  실려 있어야 한다(§3.2).
- **에이전트 소유 보고는 영속되지 않는다** (리뷰 A4): `tm.inbox` 는
  `SimpleQueue` 라 세션 종료 시 사라진다(`_adopt_questions` docstring
  `:1764-1768`). 회귀는 아니다 — 오늘은 **모든** 보고가 영속 안 되는
  `MonitorRegistry._pending` 에 있다. 다만 main 과 에이전트의 내구성이 **다르다**는
  것은 새 사실이므로 적어둔다.
- **부활한 보고의 경과 시간 머리말은 낡는다**: `_format_report` 가 포맷 시점에
  경과를 굳힌다(`registry.py:69-73`) — 며칠 뒤 복원돼도 "5m 경과" 로 읽힌다.
  사실과 어긋나지는 않지만(그때 그랬다) 오해 소지가 있다.
- **스킬 런 중에도 모니터 보고가 들어온다**: 스킬은 `agent_registry` 를 받으므로
  (`executor.py:275`) `_deliver_agent_mail` 이 돈다. 오늘 에이전트 회신에 대해
  이미 그렇고, 모니터가 그 집합에 추가된다.

### 3.10 G2 — 다음 기능은 어떻게 붙나

새 능력이 누군가에게 알려야 하면: **`deliver(addr, mail=…, text=…)` 를 부르고
`build_reply_record` 에 `kind` 분기를 하나 더한다.** 건드리지 않는 것 — waker
술어 · 생존 판정 · `run_loop` 시그니처 · `LoopConfig` · 배달 지점. 지금 고객
넷(질문·답·독촉·모니터)이 그 형태를 공유하고 다섯 번째도 같다.

## 4. 설계 — 축 B: `LoopPorts` (자원 주입 쪽 G2)

### 4.1 먼저 지울 것

**`ask_handler` 삭제** — 실값 생산자 없음. 포트 · `LoopConfig` 필드 ·
`dispatch.py:680-681` 분기를 지운다. **`monitor_registry` 포트 삭제** — §3.8.

남는 포트 **7개**: `questions` · `message_handler` · `agent_registry` ·
`mcp_manager` · `hook_runner` · `route_message` · `dequeue_user_message`.

### 4.2 `LoopPorts`

```python
@dataclass(frozen=True, kw_only=True)
class LoopPorts:
    owner: str              # "main" | "agent:<key>" (§3.4)
    questions: Any          # 기본값 전무 — 빠뜨리면 생성 시 TypeError
    message_handler: Any
    agent_registry: Any
    mcp_manager: Any
    hook_runner: Any
    route_message: Any
    dequeue_user_message: Any
    unwired: Mapping[str, str] = field(default_factory=dict)

    def handler_resources(self) -> dict[str, Any]:      # 7키
        return {f.name: getattr(self, f.name)
                for f in fields(self) if f.name not in ("owner", "unwired")}
```

- `owner` 를 여기 태우는 것이 4판과의 차이다 — C3 가 C2 **앞**에 오므로(§6)
  C2 는 `owner` 를 220곳에 다시 꿰지 않고 이 필드 하나로 받는다.
- ABC 를 안 쓰는 이유: 포트 7 × 조립지점 5 = 메서드 35개가 되고, 값이 지연
  평가로 바뀌어 순서 의존이 생긴다(지금은 조립 시점 고정).
- **`ctx`·`stop_event` 는 포트가 아니다**: `ctx` 는 모든 호스트가 넘기는 기반이자
  협력자 넷의 위치 인자, `stop_event` 는 가변 `LoopState` + setter + 루프 자체
  기본값.
- `handler_resources` 파생 → `core.py:151` 의 손-유지 dict 소멸. 3키→7키지만
  **마운트는 안 바뀐다**: 충돌하는 `requires_handler` 없고 어떤 포트 클래스도
  `__bool__`/`__len__` 을 정의하지 않는다.
- **`monitor` 에 `requires_handler` 를 달지 않는다**: 오늘 선언이 없어 모든 루프에
  붙고 전역 `_MAIN` 으로 동작한다. 달면 스킬·delegate 가 도구를 **잃는다**.

### 4.3 사유는 `unwired` 맵에만 산다

2판은 `NotWired` 가 `is None` 을 "통과한다" 고 썼다. **죽는다** — 모든 `is None`
가드 다음 줄이 호출/속성 접근이다(`prompt.py:50`, `core.py:674/683/718/743`,
`dispatch.py:681` …). 특히 `stop_event=NotWired` 면 `core.py:517` 의
`if self.stop_event:` 가 `.set()` 을 건너뛰어 **Ctrl+C 가 조용히 죽는다**.

→ 사유는 **`unwired` 맵에만** 싣고 `LoopPorts` 필드에는 실객체 또는 `None` 을
담는다. 기존 `is None` 가드 40여 곳이 **한 글자도 안 바뀐다.**

(전 판들이 여기 `NotWired(why)` 래퍼 클래스를 뒀는데, C3 구현에서 **없앴다** —
사유가 맵으로 가면 래퍼가 런타임에 하는 일이 없다. 값으로 들고 다녀 봐야
`is None` 을 통과해 죽을 위험만 남는다.)

사유 문자열의 한계는 인정한다 — 2판이 `mcp_manager` 에 **거짓 사유**를 적었고
(MCP 도구는 전역 `TOOLS` 에 등록되므로 스킬·서브에이전트도 호출한다,
`mcp/adapter.py:101`; 포트는 프롬프트·훅 컨텍스트에만 쓰인다) 어떤 테스트도 못
잡았다. 강제되는 건 "사유가 있다" 뿐이다.

### 4.4 조립 지점 다섯 · 테스트 폭발 반경

`runtime.py` 에 `ports_for_run`·`_web`·`_skill`·`_oneshot`·`_resident`. **웹은
메시지마다 새로 짓는다** — `dequeue_user_message`/`route_message` 가 반복마다
새로 만드는 클로저(`main.py:2495-2496`).

`run_loop(` 158 · `AgentLoop(` 51 · `LoopConfig(` 13 ≈ **220곳**. conftest 팩토리를
둔다. "팩토리는 뒷문 기본값" 은 옳지만 **프로덕션 빌더엔 기본값이 없고 팩토리는
tests/ 안에만** 있다. 리뷰 권고대로 **add-then-remove 로 쪼개지 않는다** — 중간
상태의 이중 선언이 §1.1 그 자체다. 한 번의 기계적 커밋 + 호스트별 `LoopConfig`
스냅샷 등가성 테스트로 리뷰 가능성을 확보한다.

## 5. 무엇이 사라지고 무엇이 안 사라지나

| # | 이 설계로 | 기구 |
|---|---|---|
| G1 모니터 주소 배달 | **달성** | `deliver(owner, …)` + 기본값 없는 owner 전달 |
| 1 `run_loop` 파라미터 누락 | **구조적 소멸** | 무기본값 필드 → 생성 시 TypeError |
| 1.2 라이브락·누수 | **소멸** | main 은 자기 메일만 본다 + `drop_owner` |
| 1.3 owner 조용한 누락 | **소멸** | §3.4 의 16지점 전부 무기본값 |
| 3 waker 술어 누락 | **소멸** | 얹을 술어 자체가 없어진다 |
| 1.4 의도/사고 혼동 | **완화** | `unwired` 맵 — "사유 있음"만 강제, 진위는 아님 |
| 2 `wrap_single_op` 접두사 | **못 잡음** | 도구 op 포맷 층 — 범위 밖 |

## 6. 이행 — C1 → C3 → C2

4판은 C1→C2→C3 였고 "C3 는 연기 가능" 이라고 했다. **틀렸다** (리뷰 A3): §3.4 의
무기본값 `owner` 가 `run_loop`·`AgentLoop`·`LoopConfig` 에 붙으므로, C2 가
§4.4 의 220곳을 **그대로 건드린다**. 실측 변경량:

| 순서 | 테스트 수정 |
|---|---|
| C1→C2→C3 (4판) | C2 가 222(`run_loop` 158 + `AgentLoop` 51 + `LoopConfig` 13) + 중첩 seam 약 80(`execute_skill` 36 · `_run_single` 18 · `tool_delegate` 13 · `_handle_run_skill` 7 · `_run_parallel` 5 · `run_subagent_message` 1) ≈ **300**, 이어서 C3 가 222 를 **다시** ≈ **470** |
| **C1→C3→C2** | C3 가 222 를 **한 번**(포트 + conftest 팩토리), C2 는 `owner` 를 `LoopPorts` 필드로 받아 그 222 를 팩토리가 흡수 → 중첩 seam 80 만 ≈ **300** |

(5판은 여기에 "가짜 러너 52" 를 더했다. **과대계상**이다 — 리뷰 A3: 그 52 는
`runner=` **출현 횟수**고, 실제 가짜 정의는 22개인데 **전부**
`def runner(query, ctx, **kw)` 라 새 kwarg 가 공짜다. 게다가 C3 가
`run_subagent_message(ports=…)` 로 가면 러너 시그니처 변경은 **C3** 몫이다.
방향은 안 바뀌고 두 수치가 각각 ~50 줄었다.)

- **C1 — `deliver` 추출 (행동 불변).** §2.3 의 6줄 서명, 주소 분기 있는 셋을
  접는다. 등가성: 호출부별 `request()` kwargs 스냅샷 전후 비교.
- **C3 — `LoopPorts` (행동 불변).** 포트 7 + `owner` + 빌더 5 + 파생
  `handler_resources` + `ask_handler` 삭제 + conftest 팩토리. 리뷰 권고대로
  **add-then-remove 로 쪼개지 않는다** — 중간 상태의 이중 선언이 §1.1 그
  자체다. 한 번의 기계적 커밋 + 호스트별 `LoopConfig` 스냅샷 등가성 테스트.
- **C2 — 모니터를 주소 배달로 (G1, 행동 변경).** `Monitor.owner`/`dropped` ·
  `RunContext.owner` · 중첩 seam 7 + 루프 밖 호출자 2 무기본값 전달 ·
  `build_agent_registry` 직후 `monitors.deliver`/`registry.monitors` 양방향
  배선(+`add` 거부) · `teardown_session(monitors=…)` 의 `stop()`+`closed` ·
  `kill`/`shutdown_all` 의 `stop_event.set()` 앞 조기 드롭 ·
  `build_reply_record` 의 `kind:"monitor"` · `_deliver_agent_mail` 이
  `record["tool"]` 로 렌더 · `_deliver_monitor_reports` 삭제 · waker 술어와
  `wire_agent_mail(monitors=)` 정리 · 워커 `finally` 의 `drop_owner` ·
  in-flight 카운터 + 배달 직전 `alive` 재확인 ·
  `tests/test_monitor_wiring.py` 약 8건 + `test_runtime_assembly.py:271` 재작성.

**C3 시점의 `owner` 값** (리뷰 B5): 그 커밋에서 `ports_for_skill` ·
`ports_for_oneshot` 은 부모 owner 를 **알 수 없다** — 그걸 나르는 seam 이 C2 의
몫이라서다. 그래서 `"main"` 을 싣는다. `ports_for_resident` 는 `tm.key` 가
`:2207` 에 이미 있으므로 진짜 값을 싣는다. **C2 전에는 아무도 `owner` 를 읽지
않으므로**(`RunContext.owner` 와 `MonitorTool` 이 C2 에 생긴다) 행동은 불변이다.
다만 "에이전트 안의 스킬" 이 한 커밋 동안 **알면서 틀린 값**을 들고 있다 — 이게
아래에서 거부하는 "기본값" 과 혼동되지 않도록 적어둔다. 차이: 이건 값이 없는
동안의 한시적 placeholder 고, 저건 영구적으로 누락을 가리는 장치다.

**대가**: G1 이 한 커밋 뒤로 밀린다. 대신 총 변경량이 170 줄고 모든 seam 이
무기본값으로 남는다. `run_loop` 에 `owner="main"` 기본값을 주어 C2 를 줄이는
안은 **거부한다** — 조용히 틀리는 지점이 정확히 하나 생기고(`agents_live.py:2207`),
그게 §1.1 의 원칙 위반이다.

## 7. 결정된 것 (전 판의 미결)

- **(A) 순서** → C1 → C3 → C2 (§6).
- **(B) `deliver` 의 자리** → `AgentRegistry` 의 **메서드**. 배선이
  `build_agent_registry` 한 줄이면 콜러블 결합은 평범한 속성이고 테스트는 람다로
  가짜를 낸다. 별도 `Mailroom` 객체는 메서드 하나짜리 두 번째 레지스트리가 된다.
- **(C) 드롭 식별** → `Monitor.dropped` 전용 플래그 + 배달 직전 `alive` 재확인
  (§3.6·§3.7). `mon.retired` 는 은퇴 전원이 truthy 라 쓸 수 없다.
- **(D) 폭주 상한** → 새로 만들지 않는다. `MAX_WAKES = 20` ·
  `MIN_INTERVAL_S = 30`(`registry.py:36-37`)이 이미 감시당 런 20개·30초 간격으로
  묶는다.
- **(E) `expects_reply`** → `False` + `_deliver_peer_reply` 식 안내 꼬리(§3.2).

남은 미결 없음.
