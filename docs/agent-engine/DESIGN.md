# 에이전트 엔진 — 엔진 하나, 메시지 한 쌍, 수명 값 하나

모든 에이전트(`agent` `mode` "run" 의 run 에이전트와 `spawn` 의 상주 에이전트)는 레지스트리
하나(`subagent/agents_live.py::AgentRegistry`)에 오르고, 각자 자기 스레드(`agent-<key>`)에서 같은
루프 종류(`ResidentLoop`)로 돈다. run 과 spawn 의 차이는 에이전트 값 `ends_after_task` 하나다 —
참이면 런이 `complete` 로 끝날 때 내려온다(채널은 회색으로 남는다), 거짓이면 다음 메시지를 기다린다.
결과는 요청자에게 가는 **회신**(`reply`)이고, 요청은 누구든 `message` 로 보낸다.

이 문서는 지금 구조(P1·P2·P3)를 적는다.

## 1. 결정 (사용자)

- 엔진은 하나로 합친다 — 주소만 통일하는 안은 접었다.
- 결과는 요청자에게 가는 회신이다. 기다리는 회신이 남으면 `complete` 할 수 없다.
- 정지는 명시적 UI 로, run·spawn 공통이다. 멈추면 빚진 상대 모두에게 알린다 — 조용히 버리지 않는다.
- 상한은 run 도 똑같이 센다. 화면은 모두 채널이다.
- main 은 기다리는 회신이 있는 동안 제자리에서 기다리되, 사용자 입력에 깨어 그 일을 하고 회신은 계속 기다린다.
- 방 ■ 는 main 만 멈춘다. 자식은 채널 ■ 또는 "모두 정지". 정지 버튼은 입력창 옆 하나다 — main·에이전트가 함께
  일하면 Stop ▾ 로 범위를 고르고, main 이 한가하고 에이전트만 일하면 채널 줄 끝의 ■ all.
- ✕ 제거는 그 에이전트가 띄운 run 을 함께 멈추고(회신 받을 곳이 없다), 하위 상주는 남긴다.
- CLI Ctrl+C 한 번은 main 만, 2초 안에 두 번은 전부.
- P1 의 결과 어댑터는 회귀 방지로 두었다가 P2 에서 가드 테스트와 함께 지웠다 — 최종 디자인은 단일 모듈이다.
- 에이전트 사이의 말은 `message` / `reply` 한 쌍이다.

## 2. 구조

```
tool_bridge._invoke_agent ─▶ agent_modes.tool_agent(args, registry, caller=Caller.from_loop(…), runtime)
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
| `subagent/agents_live.py` | 레지스트리 — 명단·상한·스레드·agents.json, `message` 라우팅(`message_handler(sender)`), 회신 배달(`pay_debt`), 보낸 쪽 기록(`_sent`)·`awaited_replies` |
| `subagent/agent_modes.py` | `agent` 도구의 실행 — 모드 표·모드별 처리·결과 문구. 레지스트리의 공개 표면만 부르는 잎 |
| `subagent/question_port.py` | 루프의 질문·회신 seam — `reply(id)` 의 거부 규칙, 배달은 `AgentRegistry.pay_debt` |
| `subagent/agent_mail.py` · `agents_state.py` · `mail_waker.py` | 메일의 글(봉투·관찰 레코드) · `agents.json` 직렬화 · 한가한 main 깨우기 — 레지스트리를 import 하지 않는 잎 |

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
  (`wait_input`: 웹은 `runtime.wait_for_human_words`, run 은 터미널 줄을 큐에 넣는 `main._terminal_steer_input` — 둘 다 대기 중
  친 `/`·`@` 명령(`/stop agt-3` 등)은 그 자리에서 라우팅하고 깨우지 않는다)이 올 때까지
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
  남으면 런 끝 폴백이 런 요약으로 회신한다. 정지(■·✕)로 끊긴 run 도 내려온다 — 요청자에게는 stopped 회신(§2.3).
- **갚은 요청의 런은 깨울 근거가 아니다 (v10.143.0, 사용자 결정).** 요청자 X 의 `wait` 가 깨울 근거를 셀 때, 에이전트의
  진행 중 런과 받은편지함은 X 의 요청 중 아직 회신이 가지 않은 것을 들고 있을 때만 근거다(v10.146.0 부터 `wake._holds_unpaid_request_from`
  — 항목 가운데 X 의 기다리는 회신(장부의 열린 r 줄)에 든 것; 빚으로 적히기 전 틈 포함). 이미 회신했거나 통지로 도는 런,
  대기열의 통지는 X 에게 돌아올 것이 없다 — 남은 근거가 없으면 `wait` 는 거절된다("nothing you started is pending …").
  종전엔 회신을 먼저 읽은 main 의 `wait` 가 아직 내려오지 않은 run 을 근거로 런을 끝냈다(CI 실측).
- **실패한 런의 폴백 (v10.143.0).** 턴 상한·provider 오류로 끝난 런은 산출물이 비어 있다 — 폴백 회신의 머리에 엔진의
  사유 한 줄이 선다: `(the run ended with an error: Max turns (40) reached)`(사유가 없으면 — 엔진 예외는 사유가 산출물에
  있다 — `(the run ended with an error)`). 요청자가 main 이면 관찰 레코드(`agent_mail.build_reply_record`)가, 에이전트면
  런 끝 폴백(`AgentRegistry.end_run`)이 단다 — 종전엔 사유가 버려졌고 에이전트 요청자는 실패 표시도 받지 못했다.
  사유는 실패한 런의 메일에만 ``error`` 로 실린다(agents.json 미러의 더해진 키).

### 2.3 정지

정지는 둘이다. **■ 런 정지**(`AgentRegistry.stop_run`)는 지금 런만 끝내고 상주 에이전트는 남는다. **✕ 제거**
(`AgentRegistry.kill` — 모델의 `agent` `kill` 도 같은 장치)는 에이전트를 없앤다. run 에이전트(`ends_after_task`)는 맡은
일 하나가 수명이라 ■ 도 ✕ 와 같다 — 내려온다(■ 는 채널을 비우지 않는다 — 회색 칩, 읽기 가능).

에이전트마다 정지가 둘이다: 런의 정지(`ResidentLoop.start` 가 런마다 세우는 `agent_stop.LinkedStop`)와 평생 정지
(`AgentInstance.stop_event` — ✕·세션 종료). 런의 정지는 평생 정지에 묶여 있어 ✕ 도 런을 멈춘다. 어느 쪽이든 런은
도구 실행 직전 검사(v10.140.0, `dispatch._stopped_before`)에서 멈춘다 — 멈춘 뒤 도구 0회. 부른 런의 정지에는 묶이지
않는다 — 방 ■·Ctrl+C 한 번은 main 만 멈추고, main 이 기다리던 회신은 다음 런에 메일로 온다.

**규칙 한 장** — 그 에이전트가 들고 있던 것마다 무엇이 가는가 (`agent_stop.STOP_RULES`):

| 들고 있던 것 | ■ 런 정지 (상주) | ✕ 제거 | run 에이전트 (■·✕) |
|------|------|------|------|
| 지금 하던 일의 요청자 | stopped 회신 | 같음 | ✕ 와 같다 — 내려온다 |
| 그 밖의 빚(다른 에이전트·main 이 `message` 로 맡긴 것) | 같은 회신 | 같음 | 〃 |
| 아직 시작 안 한 대기 요청 | 남김 — 다음 차례에 처리 | removed 회신 | 〃 |
| 그 에이전트가 기다리던 회신(자기가 맡긴 하위 일) | 그대로 — 하위 에이전트는 계속 | 하위 run 은 함께 멈춤 · 하위 상주는 남음 | 〃 |

사람 주소의 빚(채널에 사람이 직접 쓴 요청)은 회신하지 않는다 — 창이 곧 배달이다. 세션 종료(`shutdown_all`)는 사람의
정지가 아니다: 원인(`stop_cause`)이 비고, 빚은 종전처럼 버려 재시작이 끊긴 요청을 되넣는다.

**회신.** 회신이므로(P2 규칙) 요청자의 기다리는 회신을 닫고 새 장부 항목을 만들지 않는다. 봉투는 회신 모양 그대로에
머리 끝 표지(`mark`)만 더한다:

```
── reply [n-7] from agt-3 re [r-21] · stopped by user ──
The user stopped agt-3 while it was working on your request [r-21] ("find every caller of parse_tokens, incl. tests/").
Last activity: `shell` "rg -n parse_tokens" ✓ · `read_file` "src/lexer.py" ✓
Partial notes: lexer.py:42, cli.py:118
Decide whether to continue another way or report back to the user.

── reply [n-9] from agt-5 re [r-32] · removed by user ──
The user removed agt-5 before it started your request [r-32] ("review the parser next"). Nothing was done for it.
Decide whether to continue another way or report back to the user.
```

"Last activity" 는 그 런이 기록한 호출 중 실행된 마지막 셋(`agent_stop.run_activity` — ctx 의 그 런 구간), "Partial notes"
는 그 런의 마지막 생각 앞부분(`partial_notes`) — 없으면 줄이 빠진다. 모델의 `agent` `kill` 이면 주어와 표지가 지운 쪽이다
(`main stopped agt-3 …` · `stopped by main`). 런 도중 정지는 그 런의 정리(`ResidentLoop.finish` →
`answer_debts_as_stopped`)가, 런 없이 남은 빚·대기 요청·하위 run 은 워커가 죽을 때(`_answer_after_stop`) 보낸다.

**스스로 내려간 에이전트 (v10.146.1).** 에이전트가 어떤 이유로든 내려가면(더는 상자를 읽지 않으면) 그 상자에 줄 선
요청을 모두 닫고 요청자마다 사실 한 통을 보낸다 — ✕ 의 removed 와 같은 길(`_answer_queued`)이다. 사람의 정지가 아니라
원인(`stop_cause`)이 비어 있는 두 경우: 과제를 끝낸 run 에이전트(표지 `ended`)와 예외로 죽은 워커(표지 `died`). 표지에
`by` 가 없고, 회신은 요청자를 깨운다. 세션 종료는 여기에 들지 않는다(재시작이 되넣는다). 집어 든(picked) 요청과 그 밖의
빚은 이 규칙 밖이다 — 죽음이면 main 의 `died` 메일만 간다.

```
── reply [n-5] from agt-B re [r-9] · ended ──
agt-B finished its task and is gone before it started your request [r-9] ("please do X"). Nothing was done for it.
Decide whether to continue another way or report back to the user.

── reply [n-6] from agt-B re [r-9] · died ──
agt-B died before it started your request [r-9] ("please do X"). Nothing was done for it.
Decide whether to continue another way or report back to the user.
```

**표면.**

- 웹: 채널 머리(`#ov-chhead`)의 `■ Stop run` · `✕ Remove`(끝난 run 은 머리 없음). 입력창 옆 정지 버튼은 하나 — main 만 바쁘면
  `Stop`, 에이전트도 일하면 `Stop ▾`(본체 = main, ▾ = Stop main · Stop all agents · Stop everything), main 이 한가하고
  에이전트만 일하면 `Send` 그대로 + 채널 줄 끝 `■ all`. **Stop everything 의 정지 회신만은 main 을 깨우지 않는다**
  (사용자 결정 — 메일 단위 표시 `wakes_main: false`, `MailWaker`·`run` 의 정지 판정은 `has_mail_that_wakes_main` 을
  본다): main 메일함에 쌓였다가 사람의 다음 입력 런에 함께 배달되고, `run` 은 조용해지면 끝난다(회신은 agents.json 미러에
  남는다). 살아 남은 상주에게 가는 그 회신도 런을 열지 않는다(받은편지함 항목 `opens_run: false` — 워커가 들고
  있다가 그 상주가 다음에 일을 받는 런의 첫 흡수에 넣는다, `_release_held`; 미러에 남아 재시작에도 산다).
  Stop all agents·채널 ■/✕ 의 회신은 main·상주를 깨운다. API: `POST /api/agent/<key>/stop`, `POST /api/stop` 의 `scope`
  (`main`·`agents`·`all`), ✕ 는 종전 `POST /api/agent/<key>/kill`.
- CLI: `/stop <key|name>`·`/stop all`(main 은 멈추지 않는다), Ctrl+C 한 번 = main 의 런, 2초 안에 두 번 = 전부(`ctrl_c.py`,
  주입 시계). 두 번은 Stop everything 과 같다(회신이 main 을 깨우지 않는다). 멈출 것이 없으면 종전처럼 중단(130),
  `ask`·확인 프롬프트가 입력을 기다리는 중이면 종전대로 그 입력을 끊고(`Renderer.is_awaiting_input`), main 이 멈추는
  중에 창 밖에서 또 누르면 강제 종료(긴 도구의 탈출구).
  정지마다 한 줄: `■ agt-3 │ stopped by user — reply sent to main (r-21)`.
- `@agent` 명령을 기다리다 main 이 멈추면 run 은 계속하고 명령의 답은 "stopped waiting — agent … keeps working; its reply
  [r-…] arrives as mail" 이다.

### 2.4 저장·resume

agents.json 에 run 에이전트는 `ends_after_task: true`·`task_id` 로, 모든 에이전트는 띄운 쪽의 `depth`·`agent_stack`·주소 `parent`(✕ 가 띄운 run 을 함께 멈춘다) 로 남는다
(더해진 키 — 옛 파일도 읽는다; v10.140.0 의 run 에이전트는 `home` 이 `run_…` 이고 dead 라 회색 개체로 선다). 끝나기 전에
프로세스가 죽은 run 은 되살아나 회신을 마친다 — 끊긴 과제를 새 id 로 다시 넣고(`_requeue`, `task_id` 를 잇는다), 요청자가
돌아오지 않았으면 그 회신은 main 메일로 간다. 에이전트의 훅 설정은 hooks.json 모양으로 저장·복원된다(`hooks_to_json`·`hooks_from_json`).

### 2.5 훅

`OnAgentStart` 는 run 을 띄울 때(부른 자리의 턴·과제 인자), `OnAgentEnd` 는 그 run 이 과제를 마치고 내려올 때(마지막 런의
결과가 `delegate_result`) 발화한다. spawn 에 붙이는 일은 범위 밖이다.

### 2.6 화면

- 웹: run 은 다른 에이전트와 같은 채널(`agents/<key>`)이다. 과제 런의 `scope_start` 가 `run`·`caller_task`(부른 자리)를 실어
  부른 자리에 한 줄 카드 `→ agt-… <profile> · working/done · open channel` 을 놓는다(resume 재생도 같은 사이드카로).
  일하는 run 칩엔 ✕ 가 있고 채널 입력창이 열려 있다. 끝난 run 은 회색 칩(✕/↻ 없음)이고 입력창이 닫힌다 — 칩 줄에서는
  줄 끝 `Done N ▾` 하나로 접힌다(v10.143.0, 시안 A — 보고 있는 채널만 칩으로 남는다, 목록은 최근 순·항목이 채널을 연다).
  📋 띠는 기다리는 회신이 있으면 "📋 Waiting for N reply · r-21 from agt-3", main 이 제자리에서 기다리는 동안엔 띠 끝에
  "main is waiting for agt-3 — type to steer it or give new work"(스티키 `waiting_in_place`). main 의 `message` 는 발신 카드(→).
- CLI: 모든 에이전트의 진행 줄이 오는 순서대로 `  agt-3 │ shell  rg -n parse_tokens src/  ✓`. main 의 제자리 대기는
  `⏳ waiting for 1 reply (r-21) — type to steer or give new work, Enter to send`, main 의 `message` 는
  `✉ message → agt-3 (notice) …`, 바쁠 때 도착한 회신은 `↩ reply r-21 ← agt-3  …`.

## 3. 단계

| 단계 | 무엇 | 모델 바이트 |
|------|------|-------------|
| P1 | 엔진 하나 — run = 레지스트리 에이전트, 상한·저장·채널·훅·깊이 검사 공통. 화면: run 카드 → 채널 + 한 줄 카드, CLI 에이전트 줄. 결과 어댑터 | 불변 — 코퍼스 바이트 비교 |
| P2 | 결과 = 회신, `message`/`reply` 한 쌍(`agent` 의 요청 모드 → `message`, main 에도 `message`). 장부의 기다리는 회신, main 제자리 `wait`(회신·사용자 입력·체크인), 남으면 `complete` 되돌림. run 에이전트 되살리기. 모든 루프가 같은 `agent` 표면 | 바뀜 — 35B·GLM A/B(짝 기준, 교차 순서)로 판정 |
| P3 (지금) | 에이전트 런 단위 stop, 채널 머리 ■/✕, 입력창 옆 Stop ▾(main · 모든 에이전트 · 전부)·■ all, `/stop`, Ctrl+C 두 번. 빚진 상대 모두에게 `stopped by user` 회신 — kill 의 조용한 빚 버리기도 고쳤다(§2.3) | 바뀜 — 회신 문구 실측 |

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
| run 채널 | 일하는 동안 사람이 직접 쓴다 · 끝나면 `Done N ▾` 로 접힘(보는 동안은 회색 칩) |
| 한 턴의 run 여럿 | 병렬 배치는 없다 — 각 run 이 바로 돌아오고 에이전트가 각자 돈다 |

### 4.1 P2 에서 함께 고친 것

- 에이전트의 훅 설정이 agents.json 저장을 깨뜨리던 것(`TypeError` — 훅 객체를 그대로 넘겼다) → JSON 모양으로 저장·복원.
- 레지스트리 런타임 하나를 매 호출 덮어써 에이전트가 띄운 에이전트의 깊이가 main 의 것과 섞이던 것 → 에이전트마다
  띄운 쪽의 배선(`caller_runtime`), 레지스트리 기본값은 main 의 호출만 갱신.
- 쓰는 곳이 없어진 것: `agent` 의 병렬 배치 기계(디스패치·도구 선언), 결과 파일 포인터(`oversized_source_path`), 루프별 인자
  스키마 교체(`parameter_overrides`), `begin_agent_work` 의 `ctx_dir`, 세션 배치의 `RUN_PREFIX`.
