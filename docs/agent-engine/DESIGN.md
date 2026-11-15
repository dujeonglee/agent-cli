# 에이전트 엔진 — 엔진 하나, 메시지 한 쌍, 수명 값 하나

모든 에이전트(`agent` `mode` "run" 의 run 에이전트와 `spawn` 의 상주 에이전트)는 레지스트리
하나(`subagent/agents_live.py::AgentRegistry`)에 오르고, 각자 자기 스레드(`agent-<key>`)에서 같은
루프 종류(`ResidentLoop`)로 돈다. run 과 spawn 의 차이는 에이전트 값 `ends_after_task` 하나다 —
참이면 런이 `complete` 로 끝날 때 내려온다(채널은 회색으로 남는다), 거짓이면 다음 메시지를 기다린다.
결과는 요청자에게 가는 **회신**(`reply`)이고, 요청은 누구든 `message` 로 보낸다.

이 문서는 지금 구조(P1·P2)와 남은 단계(P3)를 적는다.

## 1. 결정 (사용자)

- 엔진은 하나로 합친다 — 주소만 통일하는 안은 접었다.
- 결과는 요청자에게 가는 회신이다. 기다리는 회신이 남으면 `complete` 할 수 없다.
- 정지는 명시적 UI 로, run·spawn 공통이다. 멈추면 빚진 상대 모두에게 알린다(P3).
- 상한은 run 도 똑같이 센다. 화면은 모두 채널이다.
- main 은 기다리는 회신이 있는 동안 제자리에서 기다리되, 사용자 입력에 깨어 그 일을 하고 회신은 계속 기다린다.
- 방 ■ 는 main 만 멈춘다. 자식은 채널 ■ 또는 "모두 정지"(P3).
- P1 의 결과 어댑터는 회귀 방지로 두었다가 P2 에서 가드 테스트와 함께 지웠다 — 최종 디자인은 단일 모듈이다.
- 에이전트 사이의 말은 `message` / `reply` 한 쌍이다.

## 2. 구조

```
tool_bridge._invoke_agent ─▶ agents_live.tool_agent(args, registry, caller=Caller.from_loop(…), runtime)
                                 │ 모드 표 _MODE_HANDLERS: run · spawn · status · resume · kill
                                 ├─ run   ─▶ run_agent.start_run → launch_run
                                 │            ├─ delegation_checks.run_refusal  (빈 과제·provider·순환·깊이)
                                 │            ├─ AgentRegistry.spawn(ends_after_task=True, stop_event=LinkedStop(부른 런의 정지))
                                 │            ├─ OnAgentStart 훅 · on_task_end ← OnAgentEnd 훅
                                 │            └─ AgentRegistry.request(key, 과제, author=caller.owner, item_id=r-…)
                                 │            → "started agent … on request r-… — its reply comes back marked [r-…]" (바로 돌아온다)
                                 └─ spawn ─▶ AgentRegistry.spawn(…, agent_stack=호출자 것)  (같은 깊이·순환 검사)

워커(_worker) ─ ResidentLoop.run(받은편지함 묶음) … 런마다
              └─ ends_after_task 이고 런이 wait 로 끝나지 않았으면(complete 등) 내려온다(_come_down → dead)
```

| 모듈 | 하는 일 |
|------|---------|
| `subagent/run_agent.py` | `agent` run 의 진입 — 거부 판정 → spawn(`ends_after_task`) → 과제 요청. 묶인 정지 `LinkedStop`. run 의 훅. 레지스트리를 import 하지 않는 잎 |
| `subagent/delegation_checks.py` | 에이전트를 띄우기 전 거부 판정 — `run_refusal`(run), `depth_or_recursion_refusal`(run·spawn 공통). 쓰지도 띄우지도 않는다 |
| `subagent/resident_loop.py` | 에이전트(run·spawn)의 루프 종류 `ResidentLoop` — 주소 `agent:<key>`, 깊이 = 띄운 쪽 +1, 스택 = 띄운 쪽 것 |
| `subagent/agents_live.py` | 레지스트리 — 명단·상한·스레드·agents.json, `message` 라우팅(`message_handler(sender)`), 회신 배달, 보낸 쪽 기록(`_sent`)·`awaited_replies` |

### 2.1 도구 표면 — 보내는 길 하나, 갚는 길 하나

| 도구 | 누가 | 무엇 |
|------|------|------|
| `agent` | 깊이 상한 아래의 모든 루프 | run · spawn · status · resume · kill — 같은 스키마(루프별 축소판 없음) |
| `message` | main · 모든 에이전트 | 요청(기본 — 상대가 `reply` 를 빚진다) 또는 통지(`expects_reply` false). 받는 이 = 키·이름·`"main"` |
| `reply` | main · 모든 에이전트 | 빚 갚기 — run 에이전트의 "결과" 도 이것 |
| `wait` | 깨울 길이 있는 루프(강제 탑재) | main 은 기다리는 회신이 있으면 제자리, 에이전트는 런을 끝내고 받은편지함이 깨운다 |

사람의 `@agt-<key>` 명령과 웹 채널 직접 입력(`/api/agent/<key>/input`)은 그대로 `request(author="user:…")` 다 —
일하는 run 에이전트에도 쓸 수 있다(시안 C). 사람의 `@<profile> <task>` 는 루프 밖의 run 하나를 띄우고
그 회신을 main 메일함에서 꺼내 답으로 돌려준다(`main._dispatch_agent` → `launch_run` → `AgentRegistry.take_reply`).

### 2.2 결과 = 회신 · 장부 · 제자리 대기

- **보내기.** run 의 과제와 `message` 요청은 받는 쪽 받은편지함에 들어갈 때(`_enqueue`; main 앞이면 `message_to_main`)
  보낸 쪽 기록 `_sent[요청자][r-…]` 에 적힌다. 받는 쪽 빚이 끝날 때 지운다(`_close_sent` — `reply` 로 갚음, 요청자에게
  `message` 로 갚음, 런 끝 폴백, 정지로 버림). 받는 쪽이 죽었으면 보이지 않는다.
- **장부.** `Ledger.awaited`(질문 포트 `awaited` → `awaited_replies`)가 그것을 읽어 `## Open Items` 의 둘째 절
  "by arrival — `wait`; each reply closes its item when it comes" 에 싣는다(`complete` 절은 셋째로 밀린다).
  남아 있으면 `complete` 를 되돌린다 — 되돌림 예산 8·정책은 장부의 것 그대로. 예산이 다하면 결과 말미에
  "⏳ N repl… you asked for had not arrived when this run closed" 와 목록을 적는다(회신은 그래도 메일로 온다).
  웹 📋 띠는 같은 사본(`Ledger.snapshot`, source "awaiting")을 그린다.
- **제자리 대기.** `MainLoop.wait_policy` 는 기다리는 회신이 있으면 `WAIT_BLOCK` — `dispatch._wait_in_place` 가
  `Mailbox.wait` → `AgentRegistry.wait_mail` 로 메일(회신·작업 통지·체크인 — `arrived_for`) 또는 사람 입력
  (`wait_input`: 웹은 `InputQueue.wait_for_arrival`, run 은 터미널 줄을 큐에 넣는 `main._terminal_steer_input`)이 올 때까지
  막고, 깨면 같은 런의 다음 턴 경계 흡수(`MainLoop.intake`)가 읽는다. 사람 입력에 깨면 관찰
  "the wait was interrupted — the user wrote to you; … still awaited, so `wait` again when you are done" 를 남긴다.
  같은 런이라 감사(auditor)의 구간이 회신까지 덮는다. 기다리는 회신 없이 작업·모니터만 기다리면 지금처럼 런을 끝낸다.
  에이전트의 `wait` 는 그대로다(런 끝, 메일로 깸).
- **회신·통지는 항목이 아니다 (사용자 결정).** 내가 보낸 요청의 회신(명시 `reply`·런 끝 폴백 요약)은 도착하면 그 기다리는
  회신 항목을 닫고(보낸 쪽 기록이 지워진다) 내용만 전한다 — 새 장부 항목(`n-`)을 만들지 않는다. 통지(`expects_reply` false)도
  읽으면 끝이다. 요청(`expects_reply` true)만 받는 쪽에 빚을 만든다. main 은 `core.mail_item` 이 `is_reply_or_notice` 를 장부
  밖으로 두고, 에이전트(run·spawn)는 종전부터 회신·통지를 항목으로 올리지 않았다(시작 항목·흡수 항목 모두). 작업·모니터·
  사망·체크인·하니스 메일의 규칙은 그대로다.
- **내려옴 (사용자 결정).** run 에이전트는 런이 `complete` 로 끝날 때 내려온다 — `complete` 는 모든 루프에서 런을 끝내는
  동사이고, run 에이전트에게는 그게 곧 수명 끝이다(`_run_ended_by_wait` 가 아니면 `_come_down`; 턴 상한·오류로 끝난 런도
  끝이다). `wait` 로 끝난 런(자기 하위 run 을 기다림)은 내려오지 않고 깨어 계속하다 `complete` 때 내려온다. 런 중간에
  `reply` 를 먼저 보내도 내려오지 않는다. 회신은 장부가 보장한다 — 갚을 빚이 남으면 `complete` 가 되돌아오고, 그래도
  남으면 런 끝 폴백이 런 요약으로 회신한다. 정지(■·kill)로 끊긴 run 의 수명은 P1 그대로(내려온다) — 정리는 P3.

### 2.3 정지

run 에이전트의 정지는 `LinkedStop` — 자기 정지(kill·세션 종료)와 부른 런의 정지 중 어느 쪽이 서도 참이다(방 ■·Ctrl+C
가 run 을 멈춘다). 제자리 대기 중 Ctrl+C 로 스레드가 끊기면 런의 정지를 세우고 던진다. 상주의 평생 stop 은 run 과 무관하다.
멈춘 run 의 빚은 지금처럼 조용히 버려진다(요청자의 기다림도 닫힌다) — 빚진 상대 알림은 P3.

### 2.4 저장·resume

agents.json 에 run 에이전트는 `ends_after_task: true`·`task_id` 로, 모든 에이전트는 띄운 쪽의 `depth`·`agent_stack` 으로 남는다
(더해진 키 — 옛 파일도 읽는다; v10.140.0 의 run 에이전트는 `home` 이 `run_…` 이고 dead 라 회색 개체로 선다). 끝나기 전에
프로세스가 죽은 run 은 되살아나 회신을 마친다 — 끊긴 과제를 새 id 로 다시 넣고(`_requeue`, `task_id` 를 잇는다), 요청자가
돌아오지 않았으면 그 회신은 main 메일로 간다. 에이전트의 훅 설정은 hooks.json 모양으로 저장·복원된다(`hooks_to_json`·`hooks_from_json`).

### 2.5 훅

`OnAgentStart` 는 run 을 띄울 때(부른 자리의 턴·과제 인자), `OnAgentEnd` 는 그 run 이 과제를 마치고 내려올 때(마지막 런의
결과가 `delegate_result`) 발화한다. spawn 에 붙이는 일은 범위 밖이다.

### 2.6 화면

- 웹: run 은 다른 에이전트와 같은 채널(`agents/<key>`)이다. 과제 런의 `scope_start` 가 `run`·`caller_task`(부른 자리)를 실어
  부른 자리에 한 줄 카드 `→ agt-… <profile> · working/done · open channel` 을 놓는다(resume 재생도 같은 사이드카로).
  일하는 run 칩엔 ✕ 가 있고 채널 입력창이 열려 있다. 끝난 run 은 회색 칩(✕/↻ 없음)이고 입력창이 닫힌다.
  📋 띠는 기다리는 회신이 있으면 "📋 Waiting for N reply · r-21 from agt-3", main 이 제자리에서 기다리는 동안엔 띠 끝에
  "main is waiting for agt-3 — type to steer it or give new work"(스티키 `waiting_in_place`). main 의 `message` 는 발신 카드(→).
- CLI: 모든 에이전트의 진행 줄이 오는 순서대로 `  agt-3 │ shell  rg -n parse_tokens src/  ✓`. main 의 제자리 대기는
  `⏳ waiting for 1 reply (r-21) — type to steer or give new work, Enter to send`, main 의 `message` 는
  `✉ message → agt-3 (notice) …`, 바쁠 때 도착한 회신은 `↩ reply r-21 ← agt-3  …`.

## 3. 단계

| 단계 | 무엇 | 모델 바이트 |
|------|------|-------------|
| P1 | 엔진 하나 — run = 레지스트리 에이전트, 상한·저장·채널·훅·깊이 검사 공통. 화면: run 카드 → 채널 + 한 줄 카드, CLI 에이전트 줄. 결과 어댑터 | 불변 — 코퍼스 바이트 비교 |
| P2 (지금) | 결과 = 회신, `message`/`reply` 한 쌍(`agent` 의 요청 모드 → `message`, main 에도 `message`). 장부의 기다리는 회신, main 제자리 `wait`(회신·사용자 입력·체크인), 남으면 `complete` 되돌림. run 에이전트 되살리기. 모든 루프가 같은 `agent` 표면 | 바뀜 — 35B·GLM A/B(짝 기준, 교차 순서)로 판정 |
| P3 | 에이전트 런 단위 stop, 채널 머리 ■/✕, ■ 메뉴(main · 모든 에이전트 · 전부), `/stop` 슬래시. 빚진 상대 모두에게 `stopped by user` 회신 — kill 의 조용한 빚 버리기도 고친다 | 바뀜 — 회신 문구 실측 |

## 4. P2 완료 — P1 임시물은 없다

사용자 결정: 지울 때 철저하게 없애 최종 디자인은 **단일 모듈**(루프 종류 하나 = `ResidentLoop`, 결과 = 회신)이다.
P1 이 둔 임시 종류·결과 어댑터·결과 포장·`run:` 주소·축소 스키마·run 전용 수명과 명단 필터·run 채널 읽기 전용·바이트 기준
코퍼스는 전부 지웠다. 무엇을 지웠는지는 `tests/test_agent_engine_transitional.py` 한 곳이 기억하고, 그 이름·글이
`agent_cli/`·`tests/`·`docs/` 어디에도 다시 나타나지 않음을 지킨다(이 문서도 그 이름을 쓰지 않는다).

| 자리 | 지금 |
|------|------|
| run 의 루프 종류 | `ResidentLoop` + `ends_after_task` — 런이 `complete` 로 끝나면 내려온다 |
| run 의 결과 | 회신(`reply`, 없으면 런 끝 폴백) — 도구 결과는 "started agent … on request r-…" 한 줄 |
| run 의 주소 | `agent:<key>` — 받은편지함으로 통지·회신을 받는다 |
| run 의 `wait` | 에이전트와 같다(런 끝). `WAIT_BLOCK` 은 하니스 작업과 기다리는 회신을 둔 main 이 쓴다 |
| `agent` 스키마 | 모든 루프가 같다 — run·spawn·status·resume·kill. 깊이 상한만 도구를 뺀다 |
| run 의 수명 | `AgentRegistry.spawn(ends_after_task=True)` 한 길 · resume 은 못 끝낸 run 을 되살린다 |
| 모델 대면 명단 | run 에이전트도 일하는 동안은 `## Live Agents`·`status`·주소에 있다(`message` 상대). 끝난 run 은 꼬리에서 빠진다 |
| run 채널 | 일하는 동안 사람이 직접 쓴다 · 끝나면 회색 칩 |
| 한 턴의 run 여럿 | 병렬 배치는 없다 — 각 run 이 바로 돌아오고 에이전트가 각자 돈다 |

### 4.1 P2 에서 함께 고친 것

- 에이전트의 훅 설정이 agents.json 저장을 깨뜨리던 것(`TypeError` — 훅 객체를 그대로 넘겼다) → JSON 모양으로 저장·복원.
- 레지스트리 런타임 하나를 매 호출 덮어써 에이전트가 띄운 에이전트의 깊이가 main 의 것과 섞이던 것 → 에이전트마다
  띄운 쪽의 배선(`caller_runtime`), 레지스트리 기본값은 main 의 호출만 갱신.
- 쓰는 곳이 없어진 것: `agent` 의 병렬 배치 기계(디스패치·도구 선언), 결과 파일 포인터(`oversized_source_path`), 루프별 인자
  스키마 교체(`parameter_overrides`), `begin_agent_work` 의 `ctx_dir`, 세션 배치의 `RUN_PREFIX`.
