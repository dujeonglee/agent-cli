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
Task Guidelines(작업 줄) · 꼬리의 작업 블록 머리(`jobs.render_tail`; v10.68.0 에서 상태만 남기고 wait 문장은 뺌 — §3.1) · 관찰 넛지
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

**중복 정리 (v10.68.0).** 같은 턴에 wait 안내가 세 번 보였다: Guidelines 의 긴-명령
규칙, `## Background jobs` 머리의 "Nothing else to do? Call `wait`", 그리고 §3.1 의
Open Items 꼬리말. 원칙은 "규칙은 Guidelines 에, 상태 블록은 상태와 그 순간의 행동
포인터 하나" — 작업 블록 머리는 상태만 남겼다("running — you are told when each one
ends; do not poll"). 같은 원칙으로 Guidelines 의 "address every outstanding request"
문장을 뺐다: `## Open Items` 가 항목을 나열하고 런을 열어 두는 강제이지 조언이
아니다(v9.18.0 메모가 이미 그렇게 적었다).

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

### 4.3 첫 실전 (2026-10-23, Qwen3.8-27B 9과제 풀런, v10.67.0)

video 과제에서 `wait` 5회: 접수 4(apt 설치 2분, 프레임 OCR 작업 셋 — 통지가 깨워
이어 감), **도착-거부 1**(ffmpeg 프레임 추출이 1초에 끝나 호출 도중 도착 → "arrived
while you were thinking; delivered right below this" → 모델이 통지를 읽고 바로 다음
작업을 띄움). 거부-없음 0, `complete` 되돌림 0(9과제). §1 의 사고 자리(apt 설치 대기)가
한 턴으로 끝났다.


## 5. 빚이 wait 를 건너간다 · 서로 기다리는 고리 (v10.77.0)

소통 개정(docs/agent-ask/DESIGN.md §11)에서 에이전트가 에이전트에게 묻는 길이 `message`
요청 하나가 됐고, 묻는 쪽은 회신을 `wait` 로 기다린다. 그 흐름에서 드러난 두 구멍과 하나의
위험을 고쳤다.

1. **상주: 다음 런 시작이 빚을 지웠다.** `begin_run` 이 `_reply_debts[me] = []` 였다 — 앞 런이
   `wait` 로 끝나 남긴 빚을 지우면 요청자는 회신도 런 요약도 못 받는다.
2. **main: wait 해도 빚을 정산했다.** 펌프가 `wait` 로 끝난 main 런에도 `main_run_ended` 를
   불러, 상주가 받을 회신 자리에 "⏸ waiting" 런 요약이 갔다.

   두 구멍의 뿌리는 같다 — "wait 면 정산하지 않는다" 판정이 호출부(상주 단건·사람 배치)에
   흩어져 있었고 런의 시작과 main 이 빠졌다. 그래서 **런 끝을 한 곳으로** 모았다:
   `AgentRegistry.run_ended(debtor, output, *, waiting, stopped, **meta)` — `waiting` 이면
   아무것도 하지 않고(빚은 다음 런으로), `stopped`(kill·종료)면 빚을 버리고(끊긴 요청은
   재시작이 새 id 로 다시 적는다 — 남기면 빚이 둘이 되고 죽은 채무자가 요청자의 `wait` 를
   붙잡는다), 그 밖은 `end_run`(라벨 붙은 런 요약). 상주 두 경로와 `runtime.main_run_ended`
   (펌프가 `waiting` 을 넘긴다)가 모두 이 함수를 부른다. 이제 남아 있는 빚은 언제나 `wait`
   가 남긴 것이라 런 시작이 할 일이 없다 — `begin_run` 은 지웠다.
3. **서로 기다리는 고리.** A 가 B 에게 요청(r-1)하고 `wait`, B 가 A 에게 요청(r-2)하고
   `wait` 하면 둘 다 "상대가 나에게 빚졌다" 를 깨울 근거로 보고 잠든다 — 배달할 것이 없어
   아무도 깨지 않는다. `wake_sources` 는 이제 **스스로 잠든 채무자**(유휴·받은편지함 빔·빚
   남음 = 앞 런이 wait)를 직접 근거로 치지 않고, 그 채무자를 깨울 것이 있는지 거슬러 본다
   (방문 집합으로 순환을 자르고, 지금 wait 를 부르는 쪽은 바쁨 상태라 `pending_for` 의
   `exclude` 로 뺀다). A→B→A 면 두 번째 `wait` 가 "그쪽도 당신을 기다린다" 로 거부되고,
   A→B→(일하는 C) 사슬이면 받아들여진다. 단위 테스트: `tests/test_wait_tool.py::TestCircularWait`.

## 6. 중간 깨우기 (v10.79.0)

### 6.1 왜

받아들여진 `wait` 는 기다리는 것이 끝나거나 발화할 때까지 런을 멈췄다. 상한이 없었다. 보관된
harbor 벤치 전부(trial 483)에서 받아들여진 `wait` 50번 중 **13번(26%)이 깨지 못한 채 과제
시간으로 끝났다**(GLM 7·35B 5·27B 1; 깨어난 기다림의 중앙값은 130~175초, 5분을 넘게 기다린
것이 9번). 깨지 못한 13번 중 10분 시점에 아직 쓸 만한 과제 시간이 남아 있던 것은 5번이고, 오래
기다렸다 결국 깨어난 7번 중 6번도 과제를 끝내지 못했다 — 실패의 원인은 "못 깼다" 보다 처음부터
느린 방식(프레임 3,793장 OCR, 100만 줄 vim)이다. 그래서 이 변경의 목적은 벤치 점수(기대 0~1
과제)가 아니라 **헤드리스(run·예약) 세션이 끝없이 잠들지 않게 하는 안전성**이다: 백그라운드
작업에는 시간 제한이 없고(jobs.py), monitor 만 deadline 이 있다.

### 6.2 동작과 간격

`wait` 가 받아들여져 런이 끝날 때 하니스가 타이머를 하나 건다. 그 전에 어떤 이유로든 런이
시작되면(작업 종료·monitor 발화·메일·사람 입력) 타이머를 푼다. 아무것도 오지 않으면 알림으로
런을 깨운다. 열린 항목은 보통의 깨우기처럼 그대로 넘어간다.

| 같은 대상을 기다린 횟수 | 1 | 2 | 3 | 4 | 5 이후 |
|---|---|---|---|---|---|
| 이번 간격 | 5분 | 10분 | 20분 | 40분 | 60분 |
| 처음 wait 부터 누적 | 5분 | 15분 | 35분 | 75분 | +60분씩 |

"같은 대상" 은 깨울 것의 **id 집합**(`runtime.wake_source_ids`: `job:1`·`mon-…`·`agent:k`)이다 —
사람이 읽는 줄(`wake_sources`)로 비교하면 같은 에이전트라도 "queued↔working" 처럼 글이 바뀌어
헛되이 5분으로 돌아간다. 집합이 **직전에 발화한** 깨우기 때와 같으면 다음 단계, 다르면 5분
(`AgentRegistry._checkin_step`). 진짜 보고로 깨면 집합이 거의 항상 바뀌므로(작업 소멸·빚 정산)
별도 초기화가 없고, 바뀌지 않는 드문 경우(`once` false 모니터의 재발화)에 배증이 이어지는 것은
해롭지 않다. 숫자는 하니스가 고정한다(`constants.CHECKIN_STEPS_S`) — 모델이 정하는 인자는 없다.
예약(schedule)만 기다리는 `wait` 에는 걸지 않는다; 예약과 작업을 함께 기다리면 걸되 알림에는
작업·monitor·에이전트만 적힌다.

왜 5분·배증·60분 상한: 5분이면 13번 중 5번을 더 일찍 한 번 본다; 깨어난 기다림의 중앙값이
130~175초라 대부분은 걸리지도 않는다; 배증은 정말 오래 걸리는 정상 작업이 5분마다 턴을 쓰지
않게 하고, 상한은 하루짜리 작업에서 다시 "끝없이 잠듦" 으로 가지 않게 한다.

### 6.3 알림은 사실 진술이다 — 경합이 사라지는 이유

2판 설계는 알림이 "nothing has reported yet" 이라고 말했다. 그 문장의 참·거짓은 메일함 상태에
달려 있어서, 타이머가 확인하고 추가하는 사이에 진짜 보고가 끼면 거짓이 됐다. 기존 메일(작업
종료·모니터 보고·회신)은 모두 시점과 무관한 사실이라 여러 스레드가 메일함에 써도 순서가 뜻을
바꾸지 않았다 — 중간 깨우기만 부재 진술이었다. 그래서 문장을 사실로 바꿨다: **"Check-in after
5m — still running: …"**. 진짜 보고와 같은 순간에 와도 거짓이 아니고 군더더기 한 줄일 뿐이다.
모델은 둘 다 보고 판단한다. 그러면 "런 진행 중" 상태도, 락 안의 확인+추가도 필요 없다. 남는 것은
"런이 시작되면 타이머를 푼다" 하나이고, 이건 어차피 필요하다.

알림은 **기존 셋째 갈래의 첫 사용자**다 — 회신 빚(`expects_reply`)도 열린 항목도 아닌 장부 밖
메일(`ledger: "none"`, docs/ledger/DESIGN.md §14, v10.78.1 선행 PR). 새 층이 아니다. 장부 항목이
되지 않고(예산 불변) 영속되지 않는다("지금" 에만 참인 메일이 다음 세션 첫 턴에 낡은 채 배달되면
안 된다). main 레코드의 도구 이름은 그 기다림을 연 `wait`, 상주에게는 `── checkin [n-12] from
main ──` 봉투다.

작업 하나, 로그 없음:

```
⏰ Check-in after 5m — still running:
job #1 · 9m42s · `cd /app/.agent-cli/tmp && mkdir -p ocr && ls frames/*.png | xargs -P 4 …`
  log: .agent-cli/tmp/jobs/1.log · nothing after the command line, unchanged since your wait
If it is making progress, call `wait` again; if nothing reports, you are woken again in 10m. If it is not, check its output files, or stop it (`kill -- -9176`) and change the approach.
```

로그가 있는 작업과 monitor:

```
⏰ Check-in after 10m — still running:
job #1 · 15m05s · `cd /app && vim -Nu NONE -n -Es .agent-cli/tmp/in.csv -S apply_macros.vim; echo exit=$?`
  log: .agent-cli/tmp/jobs/1.log · +37 lines since your wait (last 2 lines)
  …
monitor mon-504d33 · `file` + `pattern` on .agent-cli/tmp/ocr.log · 1h44m left
If it is making progress, call `wait` again; if nothing reports, you are woken again in 20m. If it is not, check its output files, or stop it (`kill -- -5788`; `monitor` with `mode` "delete" for a monitor) and change the approach.
```

에이전트를 기다릴 때:

```
⏰ Check-in after 5m — still running:
agent:k (explorer) · working on your request · 12 steps since your wait, last one 40s ago
agent:m (coder) · owes you a reply · itself waiting on job #3 (`pytest -x tests/`)
If they are making progress, call `wait` again; if nothing reports, you are woken again in 10m. An agent whose steps have stopped may be stuck: `agent` with `mode` "kill" ends it.
```

과제 시간은 하니스도 모른다. 알림은 경과 시간·로그라는 사실만 주고 판단은 모델이 한다. "the
next check is in 10m" 은 약한 모델이 "내가 확인해야 한다" 로 읽을 수 있어 주체를 하니스로 썼다.

**진행 신호 — (a) 로그 변화만.** `wait` 때 작업별 로그 크기를 적어 두고(`_Checkin.log_marks`),
알림 때 그 뒤에 쓰인 부분만 읽어 "+N lines since your wait (last 2 lines)" 또는 "unchanged
since your wait" — 명령이 아직 한 줄도 안 썼으면(크기가 하니스의 머리줄 `$ cmd` 그대로) "nothing
after the command line" 을 앞에 붙인다(`JobRegistry.log_growth`, 비용은 새 바이트만큼).
프로세스 신호 둘은 기록만 하고 채택하지 않았다: CPU 시간 합계는 `xargs -P 4 tesseract` 처럼
짧게 살다 죽는 자식의 연속에서 지금 살아 있는 것만 잡혀 늘어난 양이 작거나 음수가 되고(바로 그
신호가 필요했던 OCR 사례에서 안 나온다), 프로세스 교체(pid 집합 비교)는 단일 프로세스가 묵묵히
일하는 vim 매크로에서 "the same" 으로 나와 결국 (a) 와 같이 읽어야 한다.

**에이전트 줄 — 새 필드 없이.** 작업·monitor 와 달리 에이전트는 로그 파일이 없다. 레지스트리가
이미 아는 것만으로 그린다(`AgentRegistry.checkin_agent_lines`): 상태·지금 처리 중인 요청자·
받은편지함 미러·회신 빚과 스스로 잠듦(`parked_debtors_for`)·그 에이전트가 기다리는 것
(`wake_sources(debtor)` 재귀)·열린 사람 질문과 `asked_at`·컨텍스트 기록의 서수(`ctx.last_ordinal`,
`wait` 때 적어 둔 `step_marks` 와 비교)와 마지막 기록 시각(`ctx.last_added_at`, v10.79.0 에 더한
float 하나 — 락 없는 근사값). "step" 은 컨텍스트 기록 하나(모델 턴 또는 관찰) — 모델에게 턴·관찰
이라는 하니스 용어를 쓰지 않는다.

| 상태 | 보이는 줄 |
|---|---|
| 내 요청을 처리 중, 진행함 | `agent:k (explorer) · working on your request · 12 steps since your wait, last one 40s ago` |
| 내 요청을 처리 중, 멈춘 듯 | `agent:k (explorer) · working on your request · no step since your wait, last one 9m ago` |
| 내 요청이 대기열 | `agent:j (writer) · has your request queued behind 2 others · busy on another request, last step 1m ago` |
| 회신을 빚진 채 스스로 잠듦 | `agent:m (coder) · owes you a reply · itself waiting on job #3 (`pytest -x tests/`)` |
| 사람 질문에 막힘 | `agent:n (reviewer) · owes you a reply · itself waiting on job #1 (`…`) · blocked on question q-3 to the user, unanswered for 9m` |

선택지는 다시 기다리기 / kill 둘뿐이다. "메시지로 물어보라" 는 넣지 않는다 — `agent` 요청 결과에
이미 "progress checks or re-sends … queue MORE work" 경고가 있고, 중간 깨우기가 재촉을 유발하면
역효과다. 사람 질문 줄이 있을 때만 "A question to the user is answered by the user, not by you."
한 줄을 더한다. 한계: 걸음 수·시각은 근사값이고 "일하고 있다" 와 "쓸모 있는 일을 하고 있다" 는
구별하지 못한다. 벤치에는 에이전트를 기다린 사례가 거의 없어 실측 근거가 없다 — 상주 에이전트를
쓰는 라이브 세션용이다.

### 6.4 구현 자리

- **거는 곳**: `AgentRegistry.run_ended(waiting=True)` → `arm_checkin(owner)` — 런 끝의 유일한
  정리(§5)라 main·상주·run·web 이 한 곳이다. 대상 id 집합·단계·이번 간격·기준(작업별 로그 크기,
  에이전트별 `last_ordinal`)을 `_Checkin` 하나에 들고 `threading.Timer` 를 띄운다. 상태는
  `{owner: _Checkin}`(걸린 것)과 `{owner: (ids, step)}`(직전에 발화한 것) 둘 — 배증이 치르는
  유일한 값이다.
- **푸는 곳**: `AgentLoop.run()` 이 첫 턴 경계의 배달 **뒤**, 빈 깨우기 런의 조기 반환 뒤에
  `_note_run_started()` → 전역 `main_registry().run_started(owner)` 를 한 번 부른다. 상주 포트는
  `agent_registry=None` 이라 전역으로 가야 하고(`dispatch._op_wait` 와 같은 방식), 네 경로가
  한 지점이다. 배달 뒤인 이유: 다른 런이 메일을 먼저 가져가 입력 없이 끝나는 런(`output=""`)이
  타이머를 풀면 잠든 모델의 기다림에 깨우기가 없어진다. 배달과 이 호출 사이에 타이머가 발화하면
  그 알림은 다음 턴 경계에 배달되거나 `wait` 를 "already arrived" 로 거부시킨다 — 사실 진술이라
  어느 쪽도 거짓이 아니다. `run_ended` 의 `**meta` 에 있는 `run` 키는 그 런을 연 배달 id 이고 이
  "런 시작" 과 무관하다.
- **발화**: `fire_checkin(owner)` 가 `runtime.wait_checkin` 으로 본문을 만들어 main 이면 메일함
  (`_push_reply`, `MailWaker` 가 깨운다), 상주면 받은편지함(`request(..., kind="checkin",
  ledger="none")`)으로. 걸린 것이 없거나(런이 먼저 시작해 풀렸다) 아무것도 안 돌고 있으면 보내지
  않는다. 상주 런을 여는 항목은 도구 레코드가 아니라 시작 user 레코드이므로 "도구 이름 `wait`"
  는 main 메일함 경로에만 해당한다.
- **정리**: `kill`·워커 `finally`·`shutdown_all` 이 그 소유자(세션 종료는 main 포함)의 타이머를
  푼다 — 죽은 에이전트에게 알림이 가지 않게.
- **결과 문장**: `dispatch._op_wait` 가 같은 계산(`next_checkin_s`)으로 이번 간격을 적는다 —
  "waiting — will wake on: job #1 (`…`), or in 5m if nothing has reported." 설명에는 숫자가 없다
  (간격이 바뀐다); 예약만 기다려 타이머가 없으면 종전 문장 그대로. 결과의 "if nothing has
  reported" 는 미래에 대한 조건문이라 사실 진술 규칙과 충돌하지 않는다.
- **테스트 끼움점**: `threading.Timer` 는 가짜 시계로 돌릴 수 없다. 주입 가능한
  `checkin_timer_factory` 와 테스트가 직접 부르는 `fire_checkin(owner)`. `tests/test_wait_checkin.py`.
- **알고 둘 것**: `pending_for("main")` 은 상주 inbox 의 `author=="main"` 항목을 "has your request
  queued" 로 센다. 체크인도 `author="main"` 이라 그 사이 main 의 wait 판정 근거가 된다 — 모니터·
  작업 보고가 이미 같은 방식이라 새 문제는 아니다.

### 6.5 함께 바꾼 것

- `kill -- -<pid>` 네 자리 통일(`Job.stop_hint`): 꼬리 `## Background jobs`, `shell` `background`
  결과, Task Guidelines, 이 알림. 작업은 자기 프로세스 묶음(`start_new_session=True`)이고 pid 는
  `sh -c` 리더라 `kill <pid>` 는 셸만 죽이고 `xargs`·`tesseract` 는 살아남는다 — 하니스의 `kill()`
  은 이미 `killpg` 다. `--` 는 음수를 신호 번호로 읽지 않게 하는 표기이고 macOS·Linux 의 sh 내장
  `kill` 과 `/bin/kill` 모두에서 동작한다. 사람 독자를 위해 꼬리의 `pid 9176` 표기는 그대로 둔다.
- `wait` 설명에 한 문장: "If nothing reports for a while, you are woken with what is still
  running." (279자 → 숫자 없음). "WITHOUT" 대문자 강조는 문장으로.
- monitor 문구·`run` 삭제는 docs/monitor/DESIGN.md §13.

### 6.6 측정 (지시가 있을 때 전체 벤치에서)

깨지 못하고 끝난 wait 수(지금 50번 중 13번) · 중간 깨우기 뒤 모델의 행동(다시 wait / 진행 확인 /
kill 하고 방식 바꿈 / `complete` — run 모드의 결과 파일을 덮어쓰므로 비율을 따로 봄)과 다시 wait
할 때까지의 턴 수 · 깨어난 시점의 남은 과제 시간 · video·large-scale 통과율(기대 0~1과제) ·
작업 종료를 지켜보는 monitor 수 · 도구 설명 길이 변화는 실제 토크나이저(omlx /v1/tokenize)로.

## 7. one-shot 의 `wait` 는 자기 메일함을 기다린다 (v10.84.0)

**사실.** 일회성(one-shot) 루프에서 `wait` 는 v10.65.0 부터 광고되지 않았지만(`ports.wakeable()` 거짓) 디스패치는
`action == "wait"` 를 받아 런을 ``waiting`` 결과로 끝냈다. 부모(`run` 호출자)는 그걸 `STATUS: success / RESULT: ⏸
waiting on job #1` 로 받았다(v10.73 벤치 4건). 원인은 one-shot 에 자기 주소가 없었던 것 — 부모 주소를 빌려 써서 자기
잡의 종료 통지가 부모 메일함으로 갔고, 꼬리엔 부모의 잡이 보였고, 깨울 주체(펌프)도 없었다.

**설계 (사용자 결정, 2026-10-30).** 루프는 `complete` 로만 반환하고, `wait` 는 메시지 큐를 기다린다.

- **소유자 주소 넷** (`constants.RUN_OWNER_PREFIX`·`HARNESS_OWNER_PREFIX`·`is_private_owner`): `main`(메일함, 펌프가 읽음) ·
  `agent:<key>`(상주 받은편지함, 워커가 읽음) · **private** `run:<run_dir>`(one-shot)·`harness:<job>`(하니스 작업) — 프로세스
  수명의 메일함을 그 루프 자신이 턴 경계(`absorb_inbox`)와 `wait` 에서 읽는다. 하니스 작업은 종전에도 자기 주소를 썼으나
  `deliver` 가 "unroutable" 로 떨궜다 — 같은 메일함으로 라우팅된다.
- **메일함** (`AgentRegistry._mailboxes`, `deliver` 의 셋째 가지): `arrived_for`·`take_mail`·`wait_mail(stop_event, still_waiting)`·
  `drop_mailbox`. 같은 `_cv` 로 깨운다. UI(`_pending`)·디스크 밖. 체크인(`fire_checkin`)도 여기로.
- **포트**: `ports_for_oneshot` 이 `absorb_inbox = take_mail(owner)` 를 꽂는다 → `wakeable()` 참 → `wait` 가 광고되고 문법에도
  든다(종전의 "숨김인데 디스패치는 받음" 불일치 해소). 흡수 레코드는 `runtime.mail_record` — **main 이 받는 모양과 같다**(사용자 결정 C, `constants.HARNESS_MAIL_TOOL`
  한 표): 봉투 없이 보고 그대로, ``tool`` 은 `job`·`monitor`·체크인→`wait`. "도구가 만든 보고는 봉투 밖"
  (`build_reply_record`) — 보낸이가 하니스 하나라 `from` 이 정보가 아니고, 봉투는 에이전트끼리의 요청·회신·죽음
  통지의 것. 상주도 같다: `deliver` 의 `agent:` 가지가 종류를 `request(kind=)` 로 넘기고 `_model_text` 는 하니스
  종류면 봉투를 씌우지 않으며 `absorb_pending` 의 레코드가 `tool` = 표·`source` = `harness_mail` (종전엔 kind 가
  떨어져 잡·모니터는 `agent` 레코드, 체크인만 봉투 — 세 루프가 세 모양).
- **`_op_wait`** (`dispatch`): 거부 둘(도착 메일·깨울 것 없음)은 그대로. 수락 뒤 소유자가 private 이면 `_wait_in_place` —
  `arm_checkin` → `wait_mail` 로 **막음** → `run_started`(체크인 해제) → `_CONTINUE`. 장부는 넘기지 않고("stay open for the
  next run" 문구 없음) 같은 런이 이어진다. 기다리던 것이 메일 없이 사라지면(잡을 밖에서 죽임) "nothing you were waiting
  on is running any more" 관찰로 잇는다. 깨울 길이 없는 루프(`cfg.wakeable` 거짓 = 스킬 — 호출자 턴 안의 인라인, 주소를
  **일부러** 물려받아 그 모니터가 호출자에게 보고)는 첫 판정에서 **거부**: "this loop runs inline inside its caller and
  nothing wakes it … finish with `complete` and the caller waits"(사용자 결정 D2).
- **예약은 private 소유자를 깨우지 않는다** (`_wake_items`): 세션 예약은 main 에게 발화한다 — 빼지 않으면 one-shot 이 잡
  없이도 `wait` 를 수락받아 영원히 막힌다.
- **끝날 때** (`runtime.release_private_owner`, 사용자 결정 D1): 돌고 있는 잡은 죽이고(`jobs.drop_owner`), 모니터는 폐기
  (`monitors.drop_owner` — 재부모화하지 않는다, 상주와 같은 규칙), 체크인·메일함을 버린다. 거둔 것이 있으면 결과 끝에 한 줄
  `[1 background job still running was stopped when this agent finished: `…`]` — 호출자가 "끝났다" 를 "다 됐다" 로 읽지 않게.
  종전엔 one-shot 이 남긴 잡·모니터가 부모 소유로 돌아 부모가 우연히 보고를 받았다(행동 변화).

**바뀌지 않는 것.** one-shot 의 수명(함수 호출)·호출자 블록·같은 턴의 결과·중첩 위임(`run` 만)·UI. "wait 는 런을 끝낸다"
(§2)는 main·상주의 말이고, one-shot 은 런 안에서 기다린다 — 뜻("깨어날 때까지 LLM 을 부르지 않는다")은 같다. one-shot 의
메일함에는 하니스만 쓴다(잡·모니터·체크인); 사람·동료의 메시지는 상주의 것.

**검토 기록.** 대안 셋을 재고 (a) 를 택했다: 문구만 바꾸기(부모가 서브에이전트의 후처리를 대신하게 됨), 깨울 수 없는 루프에
`background` 금지(후처리는 남지만 서브에이전트가 긴 명령을 전경으로만), `run` 모드 삭제(중첩 위임 5/735·상주 잔류·한 턴
결과를 잃음 — 변형 측정 뒤 재고). 설계 2판 https://claude.ai/artifact/75VC1MJfqkM2ivFQtZwBBF, 예외 17건·원칙 점검 포함.
테스트 `tests/test_oneshot_wait.py`.

