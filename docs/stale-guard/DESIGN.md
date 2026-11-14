# 제자리 고리 감지기 StaleGuard (v10.120.0)

모델이 새 결과 없이 같은 자리를 돈다 — 파일을 되돌리고 `ls | wc -l` 로 17 을 확인하기를
되풀이하거나, `:s` 변형을 매번 바꿔 돌리는데 출력이 같다. 기존 B1(`ActionLoopDetector`)은
**바로 연달은 같은 호출**만 보므로 글자를 조금씩 바꾼 이 고리를 못 잡는다. StaleGuard 는 호출
글자를 보지 않고 **결과**를 본다. 개입은 모델 쪽이 아니라 사람 쪽이다: 표시하고, 웹 main 은
두 번째 발화에 멈추고 묻는다.

근거: 오프라인 대입 보고서 「제자리 고리 오프라인 대입」(2026-11-08, 사용자 승인) — 기록된 벤치
trial 1,153개(통과 593 · 실패 553 · 판정 없음 7)에 모델 호출 없이 규칙을 대입했다.

## 1. 규칙 C (N3W8)

- 턴 하나 = 그 턴에 실행된 호출들의 (도구 이름, 관찰 본문) 쌍 목록.
- **제자리 턴**: 쌍이 하나 이상이고, **모든** 쌍이
  (a) 비어 있지 않은 결과이고, (b) 쓰기 도구(`write_file`·`edit_file`·`snapshot`·`memory`)가
  아니고, (c) 결과가 `(no output)` 이 아니며, (d) 최근 8턴 안에 같은 (도구, 결과) 쌍이 이미
  있었다. "본 것" 으로 치는 쌍도 (a)~(c) 를 만족한 것뿐이다.
- 리셋(제자리 아님, 연속 수 0): `wait`·`monitor` 가 든 턴, 쌍이 없는 턴, **하니스 작업이 도는
  중인 턴**(그 런 소유의 `shell background` 작업이 running 이거나 살아 있는 모니터 —
  `wake.harness_work_running(owner)`).
- 제자리 턴이 **3번 연달아**면 발화. 발화 뒤 연속 수는 0 — 다음 발화는 다시 3턴. 런마다
  발화 수를 센다.
- 상수 `STALE_WINDOW = 8`·`STALE_RUN = 3` 은 `recovery/detectors.py` 의 모듈 상수다(노브 아님).

`StaleGuard` 는 순수 객체다. 입력은 턴별 쌍과 "작업 진행 중" 불리언, 출력은 발화 여부·
`run`(다음 발화까지 센 연속 수)·`turns_without_new`(이어진 제자리 턴 수 — 발화로 리셋되지 않는
화면의 N)·`since_turn`·`fires`. `acknowledge()` 는 사람이 Continue 를 골랐을 때 — 연속 수 0,
발화 수 1(다음 발화에서 다시 묻는다).

### 1.1 조건 비교 (창 8턴)

| 규칙 | N | 멈춘 4 세션 적중 | 통과 trial 발화 | 실패 trial 발화 |
|---|---|---|---|---|
| A 같은 호출 + 같은 결과 | 4 | 1 | 1 | 10 |
| B 결과만 같음 | 3 | 4 | 3 | 48 |
| **C 같은 도구 + 같은 결과, 쓰기·빈 출력 제외** | **3** | **3** | **1** | **37** |

멈춘 4 세션은 2026-10-14 재생에 쓴 financial trial 이다. 창은 6·8·10, N 은 3·4·5 를 모두
돌렸다.

- **A 불채택 — 1/4.** 모델은 같은 명령을 그대로 되풀이하지 않고 글자를 바꾼다(`ls /app/documents/
  | wc -l`, `ls … 2>/dev/null | wc -l`). 결과는 같다.
- **B 불채택.** 도구를 보지 않으면 다른 도구의 같은 결과(빈 확인 메시지 류)까지 엮여 통과
  trial 발화가 3, 실패 발화가 48 로 는다.

### 1.2 발화 38건 분류 (C N3W8)

| 갈래 | trial | 통과 | 판정 |
|---|---|---|---|
| 상태 확인·되돌리기 고리 | 19 | 0 | 진짜 고리 |
| 같은 실패를 내는 디버깅 실험 | 16 | 1 | 사실상 정체(시도는 다르나 결과가 안 바뀜) |
| 형식 오류 반복 | 1 | 0 | 진짜 고리 — 원인은 형식 |
| 작업 대기 폴링 | 1 | 0 | 오탐 — 작업 실행 중 예외로 없앤다(§1 리셋) |
| 기타(삭제된 python 도구) | 1 | 0 | 해당 없음 |

38건 중 35건(92%)이 진척이 멈춘 구간이다. 폴링 오탐은 기록에 작업 정보가 없어 대입하지 못했고
단위 테스트로 확인한다.

## 2. 개입 수위 — 왜 사람 쪽인가

발화 뒤에 무슨 일이 있었나(고리 35건):

- 발화는 늦지 않다 — 첫 발화 턴 중앙값 33~34, 그때 런 예산의 1/3~1/2(19~26턴)이 남아 있었다.
- 그 남은 턴은 거의 다 버려졌다 — **35개 중 31개가 60턴 상한까지 갔고 22개가 다시 발화했다.**
- **기존 개입이 이미 들어가 있었다** — B1 의 "실행 안 함 + 문구" 넛지가 고리 trial 29/35 에
  들어 있었는데도 고리는 풀리지 않았다(10월 재생의 문구 변형 최대 7/40 과 같은 결론).

결정 기록:

- **모델 쪽 문구 개입 불채택** — 위의 29/35. 같은 종류를 하나 더 붙여도 효과를 기대할 수 없다.
- **실행 거부 불채택** — C 는 호출 글자가 달라도 거는 규칙이라 디버깅 실험 갈래의 시도를 막는다.
  유일한 통과 trial 이 바로 그 갈래였다.
- **자동 종료 불채택** — 통과 trial 이 21·24·44턴에 세 번 발화하고 스스로 풀었다. "두 번째
  발화에서 멈춤" 은 그 통과를 잃는다. 그래서 멈춤은 사람이 고르는 웹에서만, 사람이 Continue 를
  고를 수 있게 한다.
- 결과: 모델 입력은 바뀌지 않는다(Hint 를 고른 경우의 사용자 메시지 하나만). 벤치 퇴행 위험이
  없고, 효과는 사람이 보고 있을 때만 난다. 사람 없는 런의 고리를 줄이는 개입은 이번 데이터로는
  찾지 못했다.

## 3. B1 과의 관계

B1 은 그대로다. 둘은 보는 것이 다르다.

| | B1 `ActionLoopDetector` | StaleGuard |
|---|---|---|
| 본다 | 바로 연달은 같은 (도구, 인자) | 최근 8턴의 (도구, 결과) |
| 시점 | 실행 **전** | 턴의 관찰이 다 기록된 **뒤** |
| 개입 | 실행 안 함 + 넛지(모델 입력) | 표시·기록·웹 main 은 묻기(사람) |

B1 이 실행을 막은 호출은 저장되지 않는다(assistant 레코드 없음, 넛지는 `recovery="loop"`).
StaleGuard 는 그 시도를 턴으로 치지 않는다(아래 §4). trajectory 에서도 그 턴은 agent step 이
아니다 — 같은 정의다.

## 4. 라이브 쌍의 출처

디스패처가 `_handle_text_path` 꼬리(턴 디스패치가 `_CONTINUE` 로 끝난 뒤)에서 한 번 부른다.
턴 시작 서수(`ctx.next_ordinal`)부터 그 턴이 `ctx.add` 한 레코드를 `ctx.records_since` 로 읽어
`stale_turn_pairs` 에 넘긴다. 이 레코드가 history.jsonl 의 그 구간과 같은 내용이고, trajectory 는
history.jsonl 에서 `bench/harbor/atif.py` 가 만든다:

- 호출 이름 = assistant 레코드 `ops` 의 `action`(`complete` 제외) — trajectory `function_name`.
- 결과 = 개입이 아닌(`recovery` 없음, `tool` 있음) 관찰 레코드의 `content` 에서
  `"Observation: "` 접두를 뗀 것, op 순서대로 — trajectory `observation.results[].content`.
  모자라면 빈 결과.
- assistant 레코드가 없으면 `None` = 턴이 아니다(형식 재시도·B1 이 막은 반복·A4/A5 단독 거절 —
  전부 `store_emission=False`, 턴 미계수 규칙과 같다). 감지기를 부르지 않는다(리셋도 안 한다).

**다른 점 하나 — 묶음 관찰.** 여러 op 의 턴은 관찰 레코드 하나(`[1/N] tool — OK …`)에 op 별
조각 `parts` 를 싣는다(v10.2.2). trajectory 는 합친 본문을 **첫 호출에만** 붙이고 나머지 호출은
결과가 비어, 묶음 턴은 언제나 제자리가 아니고 그 결과는 "본 것" 이 되지 않는다. 라이브는 `parts`
를 op 별로 붙인다 — 모델이 각 호출에서 받은 결과 그대로다. 이 차이가 아래 등가성의 유일한 차이다.

## 5. 개입

1. **모든 발화**: `failures.jsonl` 에 `failure_signal="stale_loop"` 한 줄(`record_failure` 의
   `detail={turns_without_new, since_turn, fires, calls}`, `text` = 반복 호출 줄들). 렌더러 훅
   `Renderer.stale_loop(info)`(기본 no-op), `info = {turns_without_new, since_turn, fires, asking}`.
   발화할 때, 발화 뒤 제자리 턴이 이어질 때(N 갱신), 새 결과가 나오거나 런이 끝날 때(0 = 지움).
   - Web: 스티키 `stuck` `{turns, since_turn, fires, asking}`(`_flips_status`) → status.json·
     `/api/health` 에 `stuck: {turns, since_turn, asking}`(additive, 없으면 생략). 입력창 바로 위
     `#stuck-band` 한 줄 `🔁 No new results for N turns — the model may be going in circles.` +
     `Stop`(`/api/stop` — Stop 버튼과 같은 길). 멈춰 묻는 중에는 워커가 턴 경계에 닿지 못하므로
     띠의 Stop 이 확인에 `s` 로 답한다. 중첩 루프(상주·one-shot·스킬)는 main 의 띠를 그리지
     않는다(그 기록은 failures.jsonl 에 있다).
   - Minimal(CLI): 발화마다 흐린 한 줄 `🔁 no new results for N turns`(rich escape). 묻지 않는다.
2. **두 번째 발화부터 — 웹의 main 런에서만 멈추고 묻는다.** 조건:
   `Renderer.can_pause_for_human()`(기본 False, `WebRenderer` True — CLI 의 `can_prompt` 를 쓰지
   않는다: `run` 은 묻지 않는 것이 결정이다) 그리고 `kind.tracks_run_authors`(main).
   - 기존 `confirm` 그대로(새 입력 종류 없음): options `c` "Continue" · `s` "Stop",
     `default_key="c"`, 프롬프트에 N·시작 턴·반복 호출 세 줄까지(`도구 인자 → 결과 첫 줄`). 트레이의
     코멘트 칸이 Hint 다.
   - `c`: `acknowledge()`. 코멘트가 있으면 포트 `enqueue_user_message(text, nickname)`(main 전용,
     web = `server.enqueue_named`, run = 같은 `InputQueue`)로 입력 큐에 — 다음 턴 경계의 `_intake`
     가 사람이 친 메시지와 같은 길로 흡수해 `[닉]: …` 사용자 레코드와 `u-N` 장부 항목이 된다.
     작성자는 대답한 뷰어(`Renderer.confirm_author()` — `push_user_input` 이 답의 `conn_id` 에서
     닉네임을 실어 보낸다), 모르면 `Hint`.
   - `s`: `state.stop_note` 를 적고 `state.stop_event.set()`(사용자 Stop 과 같은 길). 다음 경계의
     `_on_interrupt` 가 보통 Stop 과 같은 기록(INTERRUPT_NOTICE)을 남기고, 결과에
     `Interrupted by user. Stopped by the user after N turns without new results.`, 화면엔
     런 종료 사유 줄(`render_run_ended`)로 같은 사실 한 줄.
   - abort/EOF 는 confirm 의 규칙대로 기본값 `c`.
   - **⚡ 자동 승인과 무관하게 묻는다.** 자동 승인의 뜻은 "위험 확인을 묻지 마라" 지 "런을 세우지
     마라" 가 아니다. 종전엔 그 검사가 `confirm` 안에 있어 모든 확인이 첫 옵션으로 답해졌으므로,
     판정을 `Renderer.auto_approved(label)` 로 꺼내 안전 확인의 호출자 둘(`shell._ask_confirmation`·
     `_confine.guard`)이 confirm 전에 부르게 옮겼다. "⚡ Auto-approved (no confirmation): …" 기록
     (웹 관찰 카드·CLI 콘솔 줄)과 라벨(명령, 없으면 프롬프트 첫 줄)은 종전과 같다. `confirm` 에 정책
     인자를 늘리지 않았고, confirm 은 "묻는다" 하나만 하는 뼈대가 된다.
   - 기다리는 동안 status.json 은 `awaiting_input: true` + `stuck.asking: true`.
   - **락 순서**: 이 자리(턴 디스패치가 끝난 뒤의 `_handle_text_path` 꼬리, 엔진의 `_execute_turn`
     안)에서 워커는 락을 하나도 잡지 않는다 — `loop/core.py`·`dispatch.py`·`pump.py` 에 락이 없다.
     `confirm` 이 `interactive_lock`(RLock)을 처음 잡고, 그 안에서 렌더러 `_lock`·허브 락을 잠깐씩
     잡는다(`set_sticky`·`clear_sticky`·`_publish_status`) — `ask`(`prompt_user`)·위험 셸 확인과
     같은 순서다. 테스트가 confirm 진입 시 `interactive_lock._is_owned()` 가 거짓임을 고정한다.
3. `run` 모드·벤치·상주·one-shot·스킬: 1번만(기록 + 표시). 멈추지 않는다 — 벤치 통과율에 영향이
   없다.

## 6. 등가성 (`scratchpad/brs/wt-l-eq/replay_real.py`, 리뷰어 재실행용)

실제 `StaleGuard` 를 import 해 기록 trajectory 1,153개를 턴 단위로 먹이고(작업 진행 중 = False),
`stale/c-fires.json`(C N3W8 첫 발화)과 `replay3.py` 의 `fires()`(소스에서 그대로 꺼내 실행 — 발화
수)에 대조했다. 판정 있는 trial 1,146개.

| 먹이 | 발화 trial | c-fires 집합 | 첫 발화 턴 | 발화 수 | 차이 trial |
|---|---|---|---|---|---|
| A. trajectory 쌍 → StaleGuard | 38 | 일치 | 38/38 | 38/38 | 0 |
| C. history.jsonl → `stale_turn_pairs`(`parts` 무시) → StaleGuard | 38 | 일치 | 38/38 | 38/38 | 0 |
| B. history.jsonl → `stale_turn_pairs`(라이브 그대로) → StaleGuard | 43 | 38 포함 | 37/38 | 37/38 | 6 |

- A 는 클래스 == 스크립트 정의를, C 는 라이브의 레코드→쌍 함수가 trajectory 의 (function_name,
  observation content) 짝짓기와 같음을 보인다. history.jsonl 은 trajectory 와 타임스탬프로 앞부분을
  맞춰 짝지었다(시간 초과 뒤 몇 턴 더 기록된 세션이 있다). 짝이 없는 6개는 history 가 없는 hermes
  trial 이고 38개에 들지 않는다.
- B 의 차이 6건은 전부 묶음 관찰의 `parts` 분할에서 온다(C 는 차이 0):

| trial | 판정 | 기준 | 라이브 | 이유 |
|---|---|---|---|---|
| snapshot-tool-dbwal-v10.36.0-off/db-wal-recovery__oggRJN4 | 통과 | — | 10 | `ls -la` 결과를 묶음 턴의 조각으로 이미 봤다 — 8~10턴에 같은 목록을 다시 확인(1회, 표시만) |
| tb21-35b-a3b-off-pyguide-r1/extract-moves-from-video__7H2ThAi | 실패 | — | 13 | `Command timed out (30s)` 를 3턴 연달아 — 첫 번째 것이 묶음 조각에서 이미 나왔다 |
| tb21-35b-a3b-off-v10.33.1-native/large-scale-text-editing__VK7pFxm | 실패 | 48, 55 | 15, 18, 48, 55 | 13~18턴 vim 실험 묶음 두 호출이 매 턴 같은 두 결과 — trajectory 에선 묶음 턴이 제자리가 될 수 없다 |
| tb21-glm53flash-oq4-off-plan-r2/financial-document-processor__baKw375 | 실패 | — | 24, 27, 30, 33 | 같은 그림 `read_file` 을 묶음·단일로 반복 |
| tb21-glm53flash-oq4-off-vbase-r1/financial-document-processor__8yvkdr8 | 실패 | — | 18, 21, 24, 27 | 같은 |
| tb21-glm53flash-oq4-off-vbase-r2/financial-document-processor__L2kBXTh | 실패 | — | 15, 18 | 같은 |

**라이브 정의가 맞는 이유.** 묶음 관찰을 첫 호출에만 붙이는 것은 ATIF 변환의 모양이지 모델이 받은
결과가 아니다 — 모델은 op 마다 자기 결과를 받는다(`[i/N] tool — OK` 머리 아래 그 op 의 본문).
trajectory 모양을 따르면 "묶음으로 같은 확인을 되풀이하는 고리" 는 영영 못 잡고, 묶음 안에서 본
결과를 다음 단일 턴이 다시 내도 새것으로 친다. 실패 5건은 실제로 진척 없는 반복이다: 그림 재읽기
셋은 v10.77.0 이 이미지 상한을 지우며 기록한 바로 그 고리("말없이 빠진 그림을 모델이 다시 읽는
고리 — 같은 그림 다시 읽기 25회")이고, VK7pFxm 은 보고서의 디버깅 실험 갈래와 같은 모양이다.
통과 1건(oggRJN4)은 한 번 발화해 표시만 한다 — 묻는 것은 두 번째부터라 런은 그대로 간다.

## 7. 테스트

- `tests/test_stale_guard.py` — 규칙 단위(ABAB 3턴째 발화, 글자만 바꾼 같은 결과, 새 결과·묶음 안
  새 쌍·쓰기 도구가 끼면 0, 쓰기 도구·빈 출력·`(no output)` 은 제자리 아님, `wait`/`monitor`·작업
  진행 중 리셋, 창 밖으로 밀린 쌍은 새것, 발화 뒤 0, 발화 수·acknowledge), 쌍 함수(접두·parts·
  complete·개입·저장 안 된 시도), `records_since`, `harness_work_running`, 렌더러(스티키·status.json·
  health·중첩 거름·능력·답한 뷰어, CLI 한 줄), 통합(가짜 공급자 — 첫 발화 띠·N 갱신·새 결과로 해제,
  두 번째 발화 confirm·워커 멈춤·`c`+코멘트 → 큐 → 다음 경계 흡수 `[dj]: …`·`u-N`, `s` → 런 종료와
  결과 문구·모델 재호출 없음·띠 해제, 락 순서, Minimal 은 한 줄만·failures.jsonl, 서브루프는 기록만), ⚡ 자동 승인(켜져 있어도 두 번째 발화는 input_required 로 멈춤, 위험 셸·경로 확인은 confirm 없이 허용되고 "⚡ Auto-approved" 관찰이 남음, confirm 자체는 ⚡ 에서도 묻는다).
- `tests/browser/test_stale_band.py` — 띠 줄·순서·N 갱신·Stop 이 중단 핸들에 닿음·숨김, 두 번째
  발화의 트레이 확인(옵션 둘·코멘트 칸 → 워커가 `("c", 코멘트)` 와 뷰어 닉네임), 묻는 중 띠 Stop =
  확인에 `s`.
- 모델 입력 불변: 꼬리 golden·`test_prompt_notation`·`test_ui_english` 무변경 통과.
