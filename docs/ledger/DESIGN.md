# 열린 항목 장부 (Open Items Ledger) — 설계 1판 (P0)

> 상태: **설계** (2026-10-18). 구현 전. 단계별 출시(P1~P4), 각 단계는 따로 측정한다.

## 0. 한 줄 요약

런이 끝나기 전에 닫아야 하는 것 — 사용자 요청, main 앞 메일, 에이전트 질문, 회신 빚 — 을
**장부 하나**에 둔다. 항목은 닫는 법을 스스로 말하고, 정책은 하나(계속형 붙잡기 + 상한),
순서도 하나(행동으로 닫는 것 먼저, `complete` 는 항상 마지막)다.

## 1. 왜

장기 실행·다중 에이전트에서 새는 자리는 "무엇이 아직 열려 있나"를 모델의 기억에 맡긴 곳이다.
사용자가 짚은 비용: **어설프게 끝나면 사람은 그걸 몇 시간, 하루 뒤에야 안다** — 사람이 늘
지켜볼 수 없다. 턴을 한두 번 더 써서 제대로 끝내는 쪽이 싸다.

지금 그 사실이 세 군데로 갈라져 있다(§3 조사).

- `## Open Requests` — 사용자 요청. `complete` 의 `answers` 로 닫는다.
- `## Owed Replies` — 에이전트 질문·회신 기대 메시지. 그 행동(`answer`·`agent request`)으로 닫는다.
- main 앞 메일 7종(회신·답·통지·모니터·작업·감사·사망) — **회계가 없다**. 무시하고 끝내도 아무 표시가 없다.

갈라져 있어서 생긴 빈 곳(§3.6): 메일 무회계, 빚·질문 원문에 압축 핀 없음, 독촉 상한이 셋 다
다름(`answers` 되묻기 런당 1회 · 요청 독촉 요청당 1회 · 빚 런 공유 3회), 결과 배달 여부가 경로마다
엇갈림, id 공간이 섞임.

## 2. 원칙 (사용자와 합의, 2026-10-18)

1. **장부 하나.** 들어온 것 1건 = 항목 1개. 꼬리에는 머리말(≤120자)만, 원문은 history
   레코드에 두고 **열려 있는 동안 압축 핀**으로 보호한다(크면 핀하지 않는다 — 요약·파일 경로).
2. **정책 하나 — 계속형 붙잡기.** `complete` 결과는 **바로 내보낸다**(인질 금지 — agent-ask
   3판에서 틀린 자리). 열린 항목이 남으면 런은 끝나지 않고 되돌린다. 항목당 독촉 상한 N, 넘으면
   하니스가 "미답" 으로 닫고 최종 답에 표시한다. N 은 P3 측정으로 정한다.
3. **닫는 법은 항목마다.** 행동(`answer(id)`, `agent request`, 상주의 `reply`) 또는 `complete`
   의 `answers`. 정책이 아니라 그 항목의 성질이다. 할 일이 없는 항목도 `answers` 에 적고
   "할 일 없음" 으로 닫는다.
4. **순서 하나.** 행동으로 닫는 항목 먼저, `complete` 는 항상 마지막(종결 호출 — 뒤의 호출은
   실행되지 않고 문법도 막는다). 꼬리가 그 순서대로 보여 준다.

장부 **밖**: main 의 블로킹 `ask` — 런 안에서 이미 사람을 기다린다.

## 3. 조사 — 지금 빚이 생기고 닫히는 자리 (2026-10-18, 코드 전수)

의무는 실제로 다섯 종류다: ① 사용자 요청 ② 회신 빚 ③ 질문 답 빚 ④ 사람에게 간 질문 ⑤ 빚 없는 메일.

### 3.1 사용자 요청 — `LoopState.run_requests` (`loop/state.py:118`)

| | |
|---|---|
| 생성 | `core.py:623-640` 스타터(main `query_request_id`, 상주 `user_requests`) · `:774-780` 런 도중 큐 드레인(라우팅 명령·`system` 깨우기 제외) · `:859-867` 상주 `_absorb_inbox` 의 `user_request`. 예약 발화(`server.enqueue_scheduled`)도 일반 큐라 **사람 요청과 똑같이** 여기 오른다 |
| 표시 | 꼬리 `outstanding_requests_block`(`constants.py:143`) — 매 턴, 1건이어도 |
| 강제 | ① `_require_answers`(`dispatch.py:962`) `answers` 없이 `complete` → **런당 1회 되돌림, 결과 미배달** ② `_settle_requests`(`:1009`) 주장 id 삭제, 생략이면 전부 삭제(모름) ③ `_nag_open_requests`(`:1050`) 남은 것 **요청당 1회 독촉, 결과 먼저 배달 후 계속** ④ `_with_unanswered_notice`(`:1150`) 최종답 말미 `⏳`/`📋` |
| 영속 | 메모리(런 수명). 디스크엔 user 레코드 `request_id`, 종결 레코드 `answers`·`open_requests`. main 은 끊긴 런의 요청을 되살리지 않음, 상주는 `in_flight` 재주입 |
| 압축 | 미답 id 의 user 레코드 핀(`manager.py:381-399`), `_SUMMARY_CHAR_CAP//4` 초과는 핀 안 함. **상주의 시작 요청은 `request_id` 가 안 찍혀 핀 안 됨** |
| 루프 | main · 상주(사람이 창에 쓴 것). delegate·skill 없음 |

### 3.2 회신 빚 — `ReplyDebt` (`agents_live.py:194`, `_reply_debts`)

| | |
|---|---|
| 생성 | 상주: 시작 항목 `note_owes(id=seq)`(`:2811`)·흡수(`:1795`). main: `expects_reply` peer_message 수신 시(`core.py:811`) — **id 자동 `m<n>`**(`agents_live.py:1267`) |
| 표시 | `owed_replies_block`(`constants.py:127`) `## Owed Replies … the harness holds the run open while they are unpaid` + `debt_lines`(상주 `reply(id=…)`, main `agent(mode="request", key=…)`) |
| 강제 | `dispatch.py:918-930`, `MAX_DEBT_NAGS = 3` **런 공유**. 사용자 런 아니면 `_refuse_for_debts` **거부(결과 미배달)**, 사용자 런이면 수락 후 `_nag_debts` 계속. 같은 턴에 요청 독촉이 걸리면 빚 검사 건너뜀 |
| 정산 | 상주 `reply(id)`(id 필수)·요청자에게 `message`. main 은 그 에이전트 앞 **아무 `request`나 가장 오래된 빚**을 갚음(id 미사용) |
| 폴백 | `end_run`(`:1322`) 런 요약을 `_NO_REPLY_LABEL` 로 배달; main 은 `pump` → `runtime.main_run_ended` |
| 영속·압축 | 메모리 전용 · **핀 없음** |

### 3.3 비동기 질문 — `Question` (`agents_live.py:135`, `_questions`)

| | |
|---|---|
| 생성 | 상주 `ask` → `QuestionPort.ask`(`:658`). main 은 블로킹 `ask`(장부 밖) |
| 라우팅 | 요청자/user/main/에이전트. main 앞 = 메일 `question`, 에이전트 앞 = inbox, `user*` 앞 = ❓ 트레이(배달 없음) |
| 빚이 되는 때 | `delivered_seq` 가 찍힐 때(상주 `_handle_request`·흡수, main `drain_replies`) → `## Owed Replies` 에 `answer question {id} …` |
| 강제 | 회신 빚과 **같은 `MAX_DEBT_NAGS` 카운터**. `_MAX_QUESTION_NAGS` 는 없다(`agents_live.py:108` 주석만 남음) |
| 종결 | `answer_question`(원자적), 런 끝 `close_unanswered`, 사망 시 `_purge_questions_for`(부활 불가일 때만) |
| 재시작 | `agents.json` `questions` → `_adopt_questions`: 상대 없으면 STALE, 배달됐던 빚엔 `kind:"reminder"` 1회 |
| 사람 질문 | 상한·종결 없음. ❓ 트레이, 상주 결과 말미 `_with_human_notice` |
| 압축 | **핀 없음** |

### 3.4 main 앞 메일 (`build_reply_record`, `agents_live.py:325`)

| kind | 회계 |
|---|---|
| `question` | 질문 답 빚(3.3) |
| `peer_message` + `expects_reply` | 회신 빚(3.2) |
| `reminder` | 새 빚 아님 — 기존 질문 빚 상기 |
| `reply`·`answer`·`peer_message`(notice/회신)·`monitor`·`job`·`harness`·`died` | **없음** (회신은 `answers` 닉네임만 `run_authors` 로 승계) |

### 3.5 표시·영속 표면

- 꼬리 순서(`session_state.py:213-221`): 경계 → 규칙 → 세션 상태 → ctx 줄 → **Open Requests → Owed Replies → Jobs → Live Agents → Memory** → 마지막 턴 경고 → 압축 안내. 마지막 메시지가 `tool`(서버 파싱 방언)이면 별도 user 메시지.
- 서버 파싱 종결 결과(`engine.py:1005`) `Request[x] removed from the user request queue. Still pending: […]`.
- `complete.answers` 는 `array<string>` 이고 **필수가 아님**(`tools/virtual.py:26-45`), 문법 제약 없음.
- UI: final payload `requests`(주장한 요청 id)·`answers`(닉네임) 별도; 웹 칩 "답한 요청", CLI `↳ answers [id] (who) text`; resume `_replay_requests`.
- status.json·보드: 열린 의무 필드 **없음**.

### 3.6 불일치와 빈 곳

1. **정책과 주석의 어긋남 (버그 아님 — P1 에서 정정):** `answers: []` 도 되돌린다. 조사 단계에선 버그로 봤으나 **의도된 정책**이다 — `test_empty_answers_list_is_bounced`: "합쳐진 런에서 빈 목록은 빈칸이다". 어긋난 것은 `_claimed_ids` 주석("빈 집합 ≠ None")으로, 그 구분은 정산·최종 통지에서만 쓰인다. 되묻기에서도 진술로 받을지는 P3 정책 통일 때 정한다.
2. 독촉 상한 셋이 다르다: 되묻기 런당 1 · 요청 독촉 요청당 1 · 빚 런 공유 3 · 사람 질문 무한.
3. 결과 배달이 엇갈린다: 되묻기·빚 거부(비사용자 런)는 **미배달**, 요청·빚 독촉(사용자 런)은 **배달 후 계속**.
4. 핀 비대칭: 질문·회신 빚·흡수된 agent 요청·상주의 시작 요청은 핀 없음. `answers` 생략 시 전부 삭제되어 진짜 미답도 꼬리·핀에서 사라진다.
5. 정산 비대칭: main 은 id 없이 "가장 오래된 빚", 상주 `reply` 는 id 필수.
6. 영속 제각각: 질문 `agents.json`, 메일 `pending`, 상주 요청 `in_flight`; `run_requests`·`_reply_debts` 는 메모리.
7. `answers` 가 세 뜻: `complete.answers`(요청 id) · 메일/inbox `answers`(닉네임 귀속) · final payload `answers`(닉네임).
8. id 공간이 섞인다: main 큐 seq · 상주 inbox seq · 빚 `m<n>` · 질문 `q-xxxxxx` — 숫자끼리 충돌 가능(`answer(id="1")` 오용의 원인). **계획했던 메일 `[mN]` 은 기존 빚 `m<n>` 과 겹친다.**
9. 추적 없음: died·job·monitor·harness 메일, 예약 발화와 사람 요청의 구별, 위임 후 돌아온 회신의 원 요청 id.
10. 문서 표류: `state.py:129` 의 `MAX_REPLY_NAGS`(실제 `MAX_DEBT_NAGS`), `agents_live.py:108` 고아 주석, `core.py:145` "CLI 는 빈 문자열".

### 3.7 설계에 주는 함의

- 원칙 2(계속형 붙잡기)는 **결과 배달을 언제나**로 통일해야 성립한다 — 지금 미배달 경로가 둘(3.6-3).
- 원칙 1(장부 하나)은 **id 공간 하나**를 요구한다(3.6-8). 출처 접두로 가른다: 사용자 `1,2,…`(지금 그대로 — 모델이 이미 쓰는 번호) · 메일 `n<k>` · 회신 빚 `r<k>` · 질문 `q-xxxxxx`. 기존 빚 `m<n>` 은 P3 에서 `r<k>` 로.
- 핀은 장부가 쥔다 — "열린 항목의 원문 레코드" 하나의 규칙으로 3.6-4 의 빈 곳이 한꺼번에 닫힌다.
- 3.6-1 은 버그가 아니었다 — P1 은 주석만 정정하고, 빈 `answers` 를 진술로 받을지는 P3 에서 정한다.

## 4. 데이터 모델

```python
@dataclass
class OpenItem:
    id: str            # "1"(사용자) · "n3"(메일) · "q-7a3f"(질문) · "r5"(회신 빚) — §3.7
    source: str        # user | mail | question | debt
    who: str           # 닉네임 · "mail:job" · "agt-x"
    head: str          # 꼬리에 실릴 머리말 (≤120자)
    record_ref: ...    # 원문 레코드(핀 대상) — history 서수 또는 request_id
    closer: str        # "complete" | "answer" | "agent_request" | "reply"
    nags: int = 0
```

## 5. 꼬리 (P3 에서 바뀐다 — P1·P2 는 지금 문구 유지)

```
## Open Items — close them in this order; `complete` is always the last step
  1. by action
     [q-7a3f] (agt-x) "할인 상한은?"          → answer(id="q-7a3f", text="…")
     [r5]     (agt-y) "리뷰 결과 보내줘"       → agent(mode="request", key="agt-y", task="…")
  2. by `complete` (last)
     [1]      (user) "README 정리해 줘"
     [n3]     (mail) "🧵 job #1 finished rc=0"
     → complete(result="…", answers=["1", "n3"])
  The run stays open until every item is closed; your result is delivered either way.
```

측정할 변형: 마지막 줄의 **완성된 호출 모양** 대 지금의 문장 안내 — 호출 모양은 거부 문구 재생에서
반복을 늘린 전례가 있다(메모리 `rejection-wording-replay`).

## 6. 단계

| 단계 | 내용 | 모델 입력 | 검증 |
|---|---|---|---|
| P0 | 이 문서 · 조사 | 없음 | 리뷰 |
| P1 | 장부 데이터 모델 하나 — 꼬리·강제 문구 **바이트 동일**. 주석·문서 표류 정리(§3.6-1·10) | 없음 | 꼬리 golden + 동등성 |
| P2 | 메일 7종 항목화(`[nK]`), 깨우기 문장 컨텍스트 제거, 열린 항목 전부 압축 핀 | 있음 | 깨우기·바쁜 런 재생 |
| P3 | 꼬리 `## Open Items` 하나 + 정책 통일(계속형·상한 N) | 있음 | main+상주 재생, 같은 조건 반복으로 흔들림부터 |
| P4 | 사람에게 보이는 장부(화면·status.json·보드) | 없음 | 브라우저 |

측정 규율(2026-10-18 깨우기 재생의 교훈): 같은 조건을 다시 돌려 흔들림부터 잰다 — 같은 조건이
5/50 ↔ 13/50 으로 움직였고, 그 범위 안의 "차이" 로 결론을 냈다가 철회했다.

## 7. 결정 (사용자, 2026-10-18)

1. **독촉 상한 — 런 공유 예산, 넉넉하게.** 항목마다가 아니라 런 전체가 나눠 쓴다(지금 빚의
   `MAX_DEBT_NAGS = 3` 과 같은 모양을 모든 항목으로). 기본값은 크게 — 후보 8, P3 재생으로
   확정한다. 예산을 다 쓰면 남은 항목을 하니스가 "미답" 으로 닫고 최종 답에 표시한다.
   `answers` 되묻기(지금 런당 1회, 결과 미배달)도 이 예산을 쓰는 독촉의 한 갈래가 되고, 결과는
   늘 배달된다(원칙 2).
2. **정산은 id 로.** main 이 회신 빚을 갚을 때도 그 빚의 id 를 쓴다 — 지금은 그 에이전트 앞
   아무 `request` 나 "가장 오래된 빚" 을 갚는다(§3.6-5). 상주의 `reply(id)` 와 대칭이고, 어떤
   빚이 어떤 행동으로 닫혔는지 추적된다. 인자 이름은 `reply`·`answer` 와 같은 `id`
   (메모리 `tool-arg-names-consistent` — 이름이 갈리면 거절이 쌓인다). `agent` 도구의 다른
   `id` 의미와 겹치는지는 P2 에서 확인.
3. **예약 발화는 가상 사용자로 구분.** 지금은 닉네임이 비면 모든 예약이 `⏰ Scheduler` 하나로
   보인다(`schedule/registry.py:35`). 예약마다 `Scheduler-<label|id>` 를 작성자로 — 예약끼리도
   갈리고, 장부 항목은 `[7] (Scheduler-nightly-tests) "…"`. 사람 요청과 같은 규칙(사용자 요청
   항목, `answers` 로 닫음)을 그대로 탄다. 감사 대상 여부는 지금처럼 사용자 런 기준.

## 8. 방언 — 다섯 모두 같은 설계

장부는 **하니스 상태**이고 방언은 그것을 나르는 모양만 정한다. 다섯 방언(`json_fc`·`xml_fc`·
`hermes_json`·`glm_argkey`·`native_fc`)에 같은 규칙이 선다.

| 층 | 방언과 무관한 것 | 방언마다 다른 것 |
|---|---|---|
| 장부·정책·핀 | 전부 — 루프·dispatch·context 가 쥔다 | 없음 |
| 꼬리 블록 | 글(`## Open Items` …)이 같다 | 붙는 자리: 텍스트 방언은 마지막 user 메시지 끝, 서버 파싱 방언(`native_fc`)은 도구 결과 뒤 **별도 user 메시지**(`manager.py:596-615` 기존 분기) |
| 닫는 행동 | `complete.answers`·`answer(id)`·`agent request(id)`·`reply(id)` — 모두 도구 호출 | 호출을 쓰는 문법(각 방언의 파서·문법). 꼬리의 호출 예시는 지금 `debt_lines` 처럼 방언 중립 의사 호출 `answer(id="…")` — P3 에서 방언 자신의 호출 모양으로 렌더할지 재본다 |
| 종결 결과 | — | 서버 파싱 방언의 `complete` 도구 결과 `Request[x] removed … Still pending: […]`(`engine.py:1005`)를 장부에서 쓴다 — 텍스트 방언엔 이 메시지가 없다 |
| 순서 강제 | "`complete` 는 마지막" 은 루프 의미(뒤의 호출 미실행) | 문법이 있는 서버에선 `complete` 뒤 호출 자체가 막힌다(v9.24.6) |

P1 의 바이트 동일 검사와 P3 재생은 **방언별로** 돈다 — 꼬리 golden 은 방언마다 따로, 재생은
녹화가 있는 방언(`json_fc`·`native_fc` 우선) 각각.

## 9. 열린 질문

- 예산 기본값(후보 8)과 소진 뒤 표시 문구 — P3.
- 빈 `answers: []` — 되묻기에서도 "아무것도 안 답함" 진술로 받을지(지금은 빈칸으로 되돌림) — P3.
- 상주 에이전트의 장부 — 같은 모델을 쓰되 닫는 법(`reply`)과 주소(요청자)가 다르다.
- 바쁜 런에 메일이 끼면 사용자 요청과 섞인 `answers` — 누락률이 늘면 정책을 다시 본다.
- 사람에게 보이는 장부(P4)의 자리 — 입력창 위 대기열·❓ 트레이와의 관계.
- `answers` 의 세 뜻(§3.6-7) — 모델이 보는 것은 `complete.answers` 하나지만 내부 필드 이름 정리.
