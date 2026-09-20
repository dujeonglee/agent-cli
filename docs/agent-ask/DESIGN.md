# 에이전트 `ask` — 질문/답 페어링 설계 (2판)

> 상태: **설계 2판 · 재리뷰 대기**
> 2026-09-20 초안 → 외부 리뷰("현재 상태로 구현 불가") → 2판
> 1판이 틀린 것 셋과 새로 들인 회귀 둘은 §10 에 경위를 남긴다.

## 1. 문제

상주 에이전트가 `ask` 를 부르면 자기 **inbox** 에서 답을 기다린다
(`agents_live.py:1538`). 그 inbox 는 "새 일감"과 "내 질문의 답"을 구분하지
않는다 — **도착 순서가 곧 답**이다.

```python
tm.state = "waiting_ask"          # :1535
item = tm.inbox.get()             # :1538  ← 무엇이 오든 답이 된다
```

| 도착한 것 | 지금 | 2판 |
|---|---|---|
| main 의 `mode:"answer"` | (없음) | **답** |
| 사람의 답 (❓ 트레이) | 답으로 소비 | **답** |
| main 의 새 일감 (`request`) | **답으로 먹힘** | **거부** — "먼저 답하라" |
| peer 의 `message` | **답으로 먹힘** | inbox 에 일감으로 대기 |
| 사람의 새 메시지 (채널 입력) | 답으로 소비 | inbox 에 일감으로 대기 |

**소비된 항목은 `_handle_request` 를 거치지 않는다**(`:1538-1544`). 본문은
"User responded:" 로 모델에게 가지만 **라우팅이 통째로 사라진다** — 그 `seq`
에 대한 회신이 main 메일박스로 안 가고, peer 요청이었다면
`_deliver_peer_reply` 가 안 돌아 **요청한 peer 가 영원히 기다린다.**

peer 가 특히 나쁘다. **peer 는 그 질문을 본 적이 없다** — 질문은 묻는
에이전트 자신의 🤝 창에만 렌더된다(`:1511-1522`). 답할 수 없는 주체의
메시지가 답으로 소비되는 것은 기능이 아니라 사고다.

## 2. 조사 — 코드가 이미 아는 것

**① 하네스는 "지금 답을 기다리는 중"을 이미 계산한다** — `was_waiting`
(`:1722`). 그런데 **표시 힌트로만** 쓴다(`:1726-1737`). 주석도 그렇게
적혀 있다: *"표시용 힌트 — 도착 순서가 진실"*.

**② 거부는 이 표면의 기존 어휘다.** `request()` 는 이미 문자열 에러를
돌려주고(`unknown agent` :673 · `is dead` :676 · `empty message`),
`_agent_request` 가 `ToolResult(False, "request rejected: …")` 로 감싼다.
새 실패 모양을 발명할 필요가 없다.

**③ 미결 질문은 에이전트당 최대 1개 — 단 한 턴에 여러 번 순차로 생긴다.**
`_handle_ask` 의 `"\n".join`(`dispatch.py:1116`)은 **op 하나 안**의 질문들만
합친다. `dispatch.py:584` 는 op 마다 `_op_ask` 를 부르고, `AskTool`
설명(`virtual.py:52-54`)이 *"to ask several, emit several `ask` ops"* 라고
가르친다. 즉 **한 턴 = N번의 순차 대기**다. (1판이 여기서 틀렸다.)

**④ 답을 볼 수 있는 주체는 `tm.current_author` 가 정한다.** 질문이 main
메일박스로 가는 것은 `current_author == "main"` 일 때뿐이다(`:1523`).
사람이 시킨 작업 중의 질문은 **main 이 존재조차 모른다.**

**⑤ 발신자 종류는 넷뿐이다** — `main`(:703) · `agent:<key>`(:772·:856) ·
`user`(CLI `main.py:339`, 웹 닉 없음 `server.py:1265`) · `user:<nick>`
(`server.py:1266`).

**⑥ ❓ 트레이와 채널 입력은 같은 엔드포인트다** — 둘 다
`POST /api/agent/<key>/input` `{content, conn_id}` (`app.js:2120` · `:1377`).
지금은 하네스가 "답변"과 "새 메시지"를 구별할 수 없다.

## 3. 설계

### 3.1 슬롯 — 큐를 늘리지 않는다

막혀 있는 worker 는 분류할 수 없다. ask 는 worker 루프 꼭대기가 아니라
`_worker → _handle_request → run_subagent_message → run_loop → dispatch →
_op_ask → handler` 의 **콜스택 아래**에서 막힌다.

**대신 생산자가 분류한다.** `request()` 는 보내는 쪽 스레드에서 돌고 막혀
있지 않다. 미결 질문 개수가 언제나 0 또는 1 이므로(§2-③) 담는 그릇은
**큐가 아니라 슬롯**이다.

```python
# AgentInstance — 필드 3개
self.awaiting: str = ""          # 대기 중인 질문 (빈 문자열 = 안 기다림)
self.answer: str = ""
self.answered = threading.Event()
```

### 3.2 답할 자격 — `current_author` 가 정한다

```python
def _answer_kind(tm, author: str, *, explicit: bool) -> str:
    """'answer' | 'work' | 'reject' — 질문을 **본** 주체만 답할 수 있다."""
    if not tm.awaiting:
        return "work"
    if author.startswith("user"):
        # 사람은 ❓ 트레이(= 질문을 본 자리)에서 답할 때만 답이다.
        return "answer" if explicit else "work"
    if author == "main":
        if tm.current_author != "main":
            return "work"      # main 은 이 질문을 본 적이 없다 (§2-④)
        return "answer" if explicit else "reject"
    return "work"              # agent:* — peer 는 질문을 본 적이 없다
```

`explicit` 은 **발신자가 "이건 답이다"라고 말했는가**다:
- main → `agent(mode="answer", key=…, task=…)`
- 사람 → ❓ 트레이가 `{answer: true}` 를 실어 보냄 (§2-⑥)

### 3.3 main 의 오답은 거부한다 (사용자 제안)

`mode:"answer"` 만 두면 모델이 `request` 를 잘못 썼을 때 **조용히 멈춘다**.
그래서 그 경우를 **거부**한다 — 멈추는 대신 그 자리에서 알려준다.

```
request rejected: agt-x 는 답을 기다리는 중입니다 —
  "이 마이그레이션 지금 돌릴까요?"
먼저 {"mode":"answer","key":"agt-x","task":"<답>"} 로 답한 뒤 이 요청을
다시 보내세요. (이 요청은 큐에 넣지 않았습니다.)
```

세 가지가 동시에 해결된다: ① 새 일감이 답으로 먹히지 않고 ② 잃지도 않고
(보내지 않았으므로 모델이 그대로 다시 보낸다) ③ 교착도 아니다(다음 턴에
모델이 스스로 고친다). §2-② 의 기존 거부 어휘를 그대로 쓴다.

**거부는 main 에게만 적용한다.** peer 와 사람의 비-답변 메시지는 거부가
아니라 **일감으로 큐잉**된다 — 그들은 질문을 본 적이 없거나(peer) 답이
아닌 다른 말을 할 자유가 있다(사람).

### 3.4 흐름

```python
# ask 핸들러 — **arm 먼저, 그 다음 공개** (1판의 C1 회귀 수리)
with self._cv:
    tm.answered.clear()          # clear 가 arm 보다 먼저 (이전 set 잔류 방지)
    tm.awaiting = question
    tm.answer = ""
    tm.state = "waiting_ask"
# ↓ 여기서부터 공개 — 이제 답이 와도 슬롯이 받는다
renderer.agent_message(**q_payload); self._log_conversation(...)
if tm.current_author == "main":
    self._push_reply({"kind": "question", ...})
self._notify_roster()

tm.answered.wait()               # 1단계엔 타임아웃 없음 (§7)

if tm.stop_event.is_set():       # 종료 wake 를 **데이터보다 먼저** 판정 (C3)
    return "(no response — agent is being terminated)"
with self._cv:
    tm.awaiting = ""
    answer, tm.answer = tm.answer, ""
    tm.state = "busy"
self._notify_roster()
return answer
```

```python
# request() — 분류는 _cv 아래, 부수효과는 락 밖 (C2 회귀 수리)
def request(...) -> tuple[str, str]:      # (error, verdict) — C6
    ...
    with self._cv:
        kind = _answer_kind(tm, author, explicit=explicit)
        if kind == "reject":
            return _reject_message(tm), "rejected"
        if kind == "answer":
            tm.answer = text if author == "main" else f"[{author}]: {text}"
            tm.awaiting = ""                  # 원자적 claim
            tm.answered.set()
        else:
            tm.queued += 1; seq = tm.queued
            item = {...}; tm.inbox.put(item)
    # ↓ 락 밖 — 답이든 일감이든 **항상** 창·로그·로스터에 남긴다 (C2)
    renderer.agent_message(**payload)
    self._log_conversation(tm, payload)
    self._notify_roster()
    return "", kind
```

**`_cv` 아래서는 렌더러·디스크 I/O·`interactive_lock` 을 부르지 않는다.**
이건 이 파일의 기존 규율이기도 하다(`_cv` 구간은 전부 짧다: :689 · :735 ·
:869 · :908). `answered.set()` 은 Event 자기 락만 잡으므로 `_cv` 아래서 안전
하고, 대기자는 `wait()` 중 아무 락도 쥐지 않는다.

## 4. 동시성

| # | 레이스 | 처리 |
|---|---|---|
| 1 | 답이 `wait()` 보다 먼저 | `Event` 가 흡수. **`clear()` 를 arm 보다 먼저** 해야 이전 질문의 set 이 안 남는다 |
| 2 | 공개 전에 답 도착 | **arm 을 공개보다 먼저**(§3.4) — 1판은 반대라 답이 inbox 로 샜다 |
| 3 | 답변자 둘 동시 | `_cv` 아래 `awaiting` claim — 두 번째는 `work` 로 떨어져 **일감이 된다**(유실 없음, 다만 LLM 턴 하나를 쓴다) |
| 4 | 종료 중 대기 | `kill`/`shutdown_all` 이 `answered.set()` 추가. 깨어나면 **`stop_event` 를 데이터보다 먼저** 본다 |
| 5 | 같은 스레드가 생산자이자 대기자 | 도달 불가 — 자기 메시지 거부(:845), 서브루프에 registry 없음(:1829) |
| 6 | dead 에 답 | 기존 `state == "dead"` 거부가 앞선다(:674) |

`_cv` 는 RLock 기반이라 `_save_state()` 의 재진입(:1077)이 안전하고, 렌더러
`_lock` → `_cv` 순서로 잡는 곳이 없어 역전이 없다.

## 5. 영향 받는 표면

| 곳 | 변경 |
|---|---|
| `AgentInstance.__init__` :293 | 필드 3개 |
| `_make_ask_handler` :1496 | arm→공개 순서, 슬롯 대기, stop_event 우선 판정 |
| `request()` :660 | 분류 분기 · `(error, verdict)` 반환 · `explicit` 인자 |
| `kill` :930 · `shutdown_all` :949 | `answered.set()` 추가 — **없으면 join 이 2/5초 타임아웃** |
| `_agent_request` :1719 | `was_waiting` TOCTOU 제거 → verdict 사용 |
| `agent_tool.py` AGENT_MODES :44 | `answer` 모드 추가 (`key`+`task` 필수) |
| `build_reply_record` :225 | 안내 문구를 `mode:"answer"` 로 |
| `web/server.py` :1247 | `{answer: bool}` 수용 → `explicit` |
| `app.js` :2120 | ❓ 트레이가 `answer: true` 를 실음 (채널 입력 :1377 은 안 실음) |

**표시 표면은 그대로다.** `waiting_ask` state 가 유지되므로 ❓ 트레이 ·
로스터 dot · `waiting_ask_keys()` 종료 경고가 전부 동작한다. 새 SSE 이벤트도
새 카드도 없다.

**영속 변화 없음.** `awaiting` 은 저장하지 않는다 — 블록된 스레드는 프로세스와
함께 죽으므로 되살릴 대상이 없다. 지금도 `_pending` 의 question 은 resume 시
`stale=True` 로 표시만 된다(:1114-1122).

## 6. 알고 두는 것

**① 한 턴에 `ask` op 이 여럿이면 순차로 N번 기다린다**(§2-③). main 이 한
메시지로 두 답을 주면 Q2 는 계속 대기한다. 1단계에서는 **그대로 둔다** —
타임아웃이 없으므로 "Q2 가 15분 멈춘다"는 새 실패가 생기지 않고, 지금도
Q2 는 아무거나로 풀릴 뿐 제대로 답을 받는 게 아니다. 2단계에서 한 턴의
`ask` op 들을 한 번의 핸들러 호출로 접는 것을 검토한다.

**② `ask` 는 액션 루프 탐지기를 우회한다** — `dispatch.py:584` 가
`loop_detector.observe`(:832) 전에 반환한다. 1단계는 타임아웃이 없어 재질문
루프가 생길 수 없지만, 타임아웃을 넣는다면 **이 가드를 같이 넣어야 한다.**

**③ 상주 에이전트 안의 delegate 가 `ask` 하면 핸들러가 없다** —
`tool_bridge` 가 `ask_handler` 를 안 넘겨서 `renderer.prompt_user` 로 가고,
에이전트 worker 스레드에서 `interactive_lock` 을 잡은 채 **main 채팅에**
출처 없이 물어본다. 이 설계와 무관한 **기존 구멍**이고 별건으로 다룬다.

**④ CLI `run` 모드에선 타임아웃이 무의미하다** — `has_active_work()` 가
`waiting_ask` 를 제외하므로(:477) 에이전트가 묻는 순간 펌프가 빠져나가
`shutdown_all` 로 깨운다. 타임아웃은 web/대화형에서만 의미가 있다.

## 7. 실행 계획

**1단계 — 모델 계약 변경 없음 (사고 제거)**
필드 3개 · arm→공개 · 슬롯 대기 · `request()` verdict 반환 · `_answer_kind`
에서 peer/사람-채널입력을 `work` 로 · 종료 두 곳 · ❓ 트레이 `answer:true`.
**이 단계만으로 peer 와 사람 채널입력이 답을 먹는 사고가 사라진다.**

**2단계 — `mode:"answer"` + 거부 (main 경로)**
AGENT_MODES 추가 · `_agent_request` verdict · 거부 문구 · `build_reply_record`
안내 갱신.

**3단계 (조건부)** — 타임아웃. §6-② 의 루프 가드와 **함께**만 넣는다.
web 에서 실제 hang 이 관측되기 전에는 넣지 않는다.

### 테스트 계획

| 층 | 내용 |
|---|---|
| 페어링 | 사람 트레이 답 → 답 / **peer message → 일감으로 남고 이후 정상 처리** / **사람 채널입력 → 일감** / main 새 일감 → 거부(2단계) |
| 손실 없음 | 답이 아닌 것으로 판정된 항목이 inbox 에 그대로 있고 `_handle_request` 를 정상 통과 · peer 요청이면 `_deliver_peer_reply` 가 돈다 |
| 레이스 | **공개 전 도착**(가짜 러너는 지연 0이라 이게 기본 경로다) · 동시 답변자 둘(둘째는 일감) · clear 순서 |
| 종료 | `kill`/`shutdown_all` 이 즉시 깨움, `join` 타임아웃 없음, **기존 TC `test_shutdown_unblocks_pending_ask` 가 "no response" 를 그대로 받는다** |
| 부수효과 | 답도 🤝 창·`conversation.jsonl`·로스터에 남는다(C2) — resume 재생에서 보인다 |
| verdict | `request()` 가 `answer`/`work`/`rejected` 를 돌려주고 `_agent_request` 힌트가 **사실**이다(C6) |
| 비회귀 | ❓ 트레이·로스터 dot·종료 경고 그대로 · `_SHUTDOWN` 재게시 삭제가 배치 경로(:1293)를 안 건드림 |

## 8. 왜 큐를 늘리지 않는가

이미 셋이다(입력 큐 · inbox · 메일박스). 네 번째를 안 만드는 이유는 취향이
아니라 **카디널리티가 1이기 때문**이다. 부수 효과로 `_SHUTDOWN` 재게시
(`:1540`)가 없어진다 — sentinel 이 inbox 에만 있게 되므로.

## 9. 기각한 대안

**(a) 질문 id 를 모델/사람에게 요구.** 하네스가 이미 `was_waiting` 을 알고
(§2-①), `agent` 도구엔 이미 `key`(에이전트 key)가 있어 이름이 충돌하며,
사람에게는 물을 수 없다. **`mode:"answer"` 는 id 가 아니라 의도 선언**이라
이 반대가 적용되지 않는다 — `build_reply_record` 가 이미 "Answer via agent
op" 를 안내하고 있어 낯설지도 않다.

**(b) inbox 에서 받아 아닌 것은 되돌려 넣기.** `SimpleQueue` 에 put-front 가
없어 tail 에 다시 넣어야 하고, 답이 안 오면 **스핀**이 된다. worker 루프가
1칸 stash 를 손으로 만든 것도 같은 제약 때문이다(:1270).

**(c) `ask` 를 비블로킹으로.** 1판은 "표시 표면 3종을 잃는다"를 이유로 들었는데
**그건 약한 이유였다.** 강한 이유는 **슬롯이 없어지지 않고 옮겨갈 뿐**이라는
것이다: 답이 새 inbox 항목으로 오면 그 회신을 원래 요청자에게 라우팅하기 위해
멈춘 요청(`seq`·`author`·`expects_reply`·`answers`)을 어딘가 기억해야 하고,
그게 `paused_item` 슬롯이다. 거기에 `_op_ask` 의 terminal 변종, `_handle_request`
의 "보류" 회신 종류, 그리고 **35B 가 `ask` 직후 반드시 `complete` 해야 한다는
모델 계약**이 더 붙는다. 블로킹 설계엔 없는 위험이다.

**(d) 타임아웃으로 교착 방지.** 교착 걱정은 이미 해결돼 있다 —
`has_active_work()` 가 `waiting_ask` 를 제외해 펌프가 빠져나가고
`shutdown_all` 이 슬롯을 깨운다(§6-④). 타임아웃은 **유일하게 새로운 실패
모드**(답 없이 진행 + 루프 탐지 우회한 무한 재질문)를 들여온다. 3단계로 미룬다.

## 10. 1판 개정 경위

외부 리뷰가 **"현재 상태로 구현 불가"** 판정을 냈고, 코드로 확인된 것만
반영했다. 확인 과정에서 1판의 전제 셋이 틀렸다.

| 1판의 주장 | 확인 | 결과 |
|---|---|---|
| `_is_answerer(author)` 로 충분 | main 은 무조건 답변자라 **새 일감이 그대로 먹힌다** — 자기 문제 표 3행을 안 고쳤다 | §3.2 `current_author` 기준 + §3.3 거부 |
| 질문 여럿도 한 번의 대기 | `join` 은 op **하나 안**. 도구 설명이 op 를 여럿 내라고 가르친다 | §2-③ · §6-① |
| "질문을 본 주체만" | 원칙은 맞는데 **반만 적용했다** — 사람이 시킨 작업의 질문은 main 이 못 본다 | §3.2 |
| (새로 들인 회귀) | arm 이 공개보다 **뒤**라 즉답이 inbox 로 샌다 | §3.4 arm→공개 |
| (새로 들인 회귀) | `return ""` 가 창·로그·로스터를 건너뛴다 | §3.4 락 밖 부수효과 |
| (기존 TC 파괴) | 종료 wake 가 빈 답을 반환해 `test_shutdown_unblocks_pending_ask` 실패 | §3.4 stop_event 우선 |

**교훈**: 1판이 틀린 셋은 전부 **"이 값 하나로 판정할 수 있다"고 적은
자리**였다(`author` 하나 · 질문 개수 하나 · 원칙 한 줄). 판정 기준을 좁게
쓰면 그 기준이 못 보는 축이 생긴다 — `current_author` 라는 두 번째 축이
정확히 그것이었다.

거부(§3.3)는 사용자 제안이다. `mode:"answer"` 만으로는 모델의 오용이 조용한
교착이 되는데, 거부가 그것을 **그 자리에서 고쳐지는 실패**로 바꾼다.
