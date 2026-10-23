# `wait` 도구 — 닫지 않고 끝낸다 (v10.65.0)

## 1. 왜

`complete` 한 동사에 뜻이 둘이었다.

- 작업 안내(v10.47.1): "할 일이 없으면 `complete` 로 턴을 끝내고 기다려라 — 통지가
  깨운다." 여기서 complete 는 **"지금은 여기까지"**.
- 장부(v10.56.0 P3): "`complete` 는 열린 항목을 닫는 행위다. `answers` 없이 오면
  8번까지 되돌린다." 여기서 complete 는 **"끝냈다"**.

벤치 실측(2026-10-22, extract-moves-from-video, Qwen3.6-35B-A3B): apt 설치를
`background: true` 로 띄운 뒤 모델이 안내대로 "기다리겠다" 는 complete 를 보냈고,
장부가 "`u-1` 을 안 닫았다" 며 되돌렸다. 모델은 과제가 안 끝났으니 `answers` 에
`u-1` 을 넣을 수 없고(거짓), `answers: []` 도 같은 되돌림이라 같은 complete 를
**9번** 보냈다. 예산이 끝나서야 런이 끝났고 그제야 작업 통지가 깨웠다 — 그 사이
설치는 끝나 있었다. 모델 호출 9번 낭비, 모델 잘못 0.

## 2. 무엇

`wait` — "더 할 게 없다, 깨워 달라". 아무것도 닫지 않고 런을 끝낸다.

- **깨울 것이 있을 때만** 받는다(`runtime.wake_sources(owner)`): 이 소유자의
  돌고 있는 백그라운드 작업·살아 있는 모니터, 세션의 일하는 중인 상주 에이전트·
  예약. 없으면 거부 관찰("nothing would wake you — keep working or call
  `complete`") 을 남기고 런을 잇는다. 막혔을 때 wait 로 도피하는 길은 없다.
- 열린 요청(`u-…`·`n-…`)은 **다음 런으로 넘어간다** — 장부는 런 수명이라
  `ctx.park_requests` 에 두고 다음 런의 `_setup` 이 `take_parked_requests` 로
  가져가 장부에 올린다. 메모리뿐: 프로세스가 죽으면 resume 런은 빈 장부(원문은
  history 에 있다). 빚(`r-`·`q-`)은 레지스트리 소유라 원래 이월된다.
- 기록은 **보통의 op + 관찰** 모양(`{"action":"wait","reason":…}` + "Observation:
  waiting — will wake on: job #1 (`…`). Open items stay open for the next run:
  u-1."). 새 카드 종류가 없다 — 웹 재생·resume·CLI 가 그대로 그린다. 깨어난 런도
  history 에서 "내가 기다렸다" 를 본다.
- 런 결과는 `ToolResult(waiting=True, output="⏸ waiting on …")`. run 의 결과 파일
  사슬(`main._ConsoleSurface.run_ended`)은 작업을 썼을 때와 같이 다음 깨우기 런의
  답까지 쓴다 — 모니터·에이전트를 기다린 경우도 포함(종전엔 작업만).
- 터미널(`Tool.terminal`) — 문법이 뒤따르는 호출을 막고, 디스패치가 앞선 op 결과를
  flush 한 뒤 처리한다. 자리는 `complete` 바로 앞.

### 2.1 불완전하게 끝나는 길을 막은 것

- **스킬·일회성 루프엔 광고하지 않는다** — `requires_handler="wake"`,
  `LoopPorts.wakeable()`(main 은 입력 큐, 상주는 받은편지함). 거기서 wait 로 끝낸
  결과는 호출자에게 "⏸ waiting" 으로 돌아가 버린다(일회성은 부모의 owner 를
  물려받아 부모의 작업이 판정에 걸렸을 것이다).
- **에이전트 활동은 요청자별** — `AgentRegistry.pending_for(addr)`: 그 주소에게
  빚진 회신, 그 주소의 항목을 들고 있거나 처리 중인 에이전트, 그 주소로 가는 미배달
  회신. 세션 전체 `has_active_work` 로 보면 상주 에이전트 **자신이 busy** 라 늘
  참이었다(아무것도 안 기다리는 wait 가 통과했을 것). run 모드에서도 "동료의
  일로 바쁜 에이전트" 때문에 main 의 wait 가 받아들여져 아무도 깨우지 않는 채
  펌프가 끝나는 길을 막는다.
- **상주 런이 wait 로 끝나면 회신 폴백도 질문 닫기도 없다** —
  `_handle_request`/`_handle_human_batch` 가 `loop_result.waiting` 을 보고
  `owed`·`end_run` 을 건너뛴다. 종전 흐름이면 요청자가 "(no explicit reply — run
  summary) ⏸ waiting on job #1" 을 답으로 받고 빚이 갚힌 것으로 처리돼, 깨어난
  뒤의 진짜 `reply` 가 "no unpaid reply" 로 거부됐을 것이다.
- **이미 도착한 통지가 있으면 거부한다** (v10.66.2) — `runtime.arrived_mail(owner)`:
  main 의 메일함(`AgentRegistry._pending` — 작업·모니터 통지, 에이전트 회신·질문),
  상주의 받은편지함 미러(`inbox_items`). 기다리던 것이 wait 보다 **먼저 끝난**
  경우다(9과제 벤치의 once 모니터). 그 통지는 어차피 다음 턴 경계
  (`_deliver_agent_mail`/`_absorb_inbox`)에 배달되므로, 받아 줘서 런을 끝내면 펌프가
  바로 다시 깨울 뿐이다 — 거부하고 "N notice(s) arrived while you were thinking; delivered right
  below this" 로 잇는 쪽이 싸고 모델의 맥락도 남는다(문구는 §4.2 실측 뒤 v10.67.0). 돌고 있는 것이 함께
  있어도 도착분이 먼저다(읽고 나서 다시 wait). 종전 `pending_for` 의 "a reply to
  you not yet delivered" 는 이 도착분을 **깨울 것** 으로 치려던 가지였는데, 작업·
  모니터 메일엔 `to` 키가 없어 한 건도 안 잡혔다 — 뺐다.
- **깨우는 쪽은 그대로** — 작업 종료 통지·모니터 보고/만료(2h)·에이전트 회신·
  예약 발화는 전부 소유자 주소로 배달되고, run 펌프(`QuietPolicy`)와 web 의
  self-reap 은 같은 항을 "할 일" 로 본다. wait 가 받아 준 것은 그 네 가지뿐이라
  깨울 것 없이 끝나는 런은 없다.

## 3. 문구 교체

"기다릴 땐 `complete`" 가 여섯 군데 있었고 전부 `wait` 로 바꿨다:
Task Guidelines(작업 줄) · 꼬리의 작업 블록 머리(`jobs.render_tail`) · 관찰 넛지
(`context/manager._OBS_COMPLETE_NUDGE`) · `agent status` 의 ⏳ 힌트 · 요청 접수·
resume 안내(`agents_live` 3곳). `complete` 의 설명에 "Not for waiting — call `wait`"
를 더했고, 기다리려고 쓴 `answers` 없는 complete 의 되돌림은 깨울 것이 있으면
"call `wait` instead" 를 붙인다(옛 습관 한 번에 교정). 회귀 가드:
`tests/test_wait_tool.py::TestWording` 이 패키지 전체에서 그 문장을 찾는다.

### 3.1 꼬리의 안내 (v10.67.1)

도구 설명은 프롬프트 **머리**(Available Tools)에 있다. Task Guidelines 와 `## Open
Items` 를 꼬리로 옮긴 근거가 "모델은 `complete` 를 부르는 순간 머리의 문장을 읽지
않는다" 였으므로 wait 도 같은 자리에 둔다 — 종전 꼬리의 wait 언급은 Task
Guidelines 의 긴-명령 항목 안 한 문장과 `## Background jobs` 블록 머리뿐이라,
모니터·에이전트를 기다리는 런은 꼬리에서 wait 를 보지 못했다.

`## Open Items` 의 꼬리말에 한 줄을 붙인다(`Ledger.block(can_wait=)`): "Not finished
because a job, monitor or agent of yours is still working? Call `wait` — the items
stay open and its report wakes you (never `complete` to wait)." 열린 항목을 보며
`complete` 를 고민하는 바로 그 자리이고, **wait 가 광고되는 루프에만**
(`"wait" in cfg.tools_list` — 스킬·일회성 루프는 `requires_handler="wake"` 로
빠진다) 붙는다. 정적인 Task Guidelines 에는 넣지 않았다 — 거긴 루프 종류를 모른다.
효과는 다음 벤치의 `complete` 되돌림·`wait` 거부 수로 본다(직전 두 벤치는 둘 다 0
에 가까워 근거가 약하지만, 사용자 결정으로 넣었다).

## 4. 측정

A/B 가 필요 없는 수정이다(같은 complete 를 9번 보내는 게 옳을 리 없다). 다음
벤치에서 `complete` 되돌림 횟수와 `wait` 호출의 거부율을 센다.

### 4.1 결과 (2026-10-23, 9과제 풀런, v10.66.0)

`complete` 되돌림 **0**(9과제 전부, §1 의 사고가 난 video 포함). `wait` 호출 1회,
거부 1회: video 에서 모델이 파일 감시 모니터(`once`)를 걸고 바로 `wait` 했는데
파일이 이미 있어 모니터가 즉시 발화·은퇴한 뒤였다. 거부 관찰과 그 모니터의
통지가 같은 관찰에 함께 배달돼 런은 그대로 이어졌다 — 결과는 맞고 낭비도 없다.
남은 틈이었던 것: `wake_sources` 는 **이미 도착해 아직 배달 안 된 통지** 를 세지
않았다. 거부 뒤 런이 계속되면 그 통지를 바로 받으므로 결과는 맞았지만 거부 문구
"nothing is pending" 이 사실과 어긋났다 — v10.66.2 에서 `arrived_mail` 로 갈라
"already arrived and follow in the next observation" 으로 거부한다(§2.1). 같은
조사에서 `pending_for` 의 "has your request queued" 가지가 `SimpleQueue` 에 없는
`queue` 속성을 읽어 늘 비어 있던 것도 받은편지함 미러로 고쳤다.

### 4.2 "먼저 끝난" 창의 실측과 문구 (2026-10-23, v10.66.2 → v10.67.0)

run 모드, Qwen3.6-35B-A3B thinking off. 작업 `sleep N; echo FINISHED` 를
`background: true` 로 띄우고 `wait` 하는 과제(N = 1·2·3·5·8)와, 이미 있는 파일에
command 모니터를 걸고 `wait` 하는 과제 × 3, 두 문구로 각각 8런.

- 경로가 갈리는 지점은 **모델 호출 도중** 끝났는가다. N=1 은 wait 를 만드는 호출
  (ttft ≈ 2.4s) 안에 끝나 메일함에 머물렀고 → 거부-도착; N≥2 는 디스패치 때 아직
  돌고 있어 접수 → 깨우기 런. 턴 경계 전에 끝난 것은 이미 관찰로 나가 있으므로 이
  경로를 밟지 않는다. 모니터는 첫 평가가 등록 뒤 1초 안이라 거의 항상 이 창에
  떨어진다(벤치 사례와 동일). CLI 는 그 순간 "⏳ Queued · 🧵 … — delivered to main
  when this turn ends" 를 찍는다.
- 거부 뒤 행동은 두 문구가 같았다: 다음 턴에 통지를 읽고 `complete(answers=[n-1,
  u-1])` 로 3턴 종료, 재-wait 0. 그래서 문구는 정확성으로 골랐다 — 거부 관찰과
  통지는 같은 프롬프트에 연달아 들어가므로 "follow in the next observation" 은
  "다음 턴" 으로 읽힐 수 있어, "arrived while you were thinking; delivered right
  below this" 로 바꿨다.
