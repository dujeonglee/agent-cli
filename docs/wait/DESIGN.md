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

## 4. 측정

A/B 가 필요 없는 수정이다(같은 complete 를 9번 보내는 게 옳을 리 없다). 다음
벤치에서 `complete` 되돌림 횟수와 `wait` 호출의 거부율을 센다.
