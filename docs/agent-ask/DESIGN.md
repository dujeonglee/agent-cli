# 에이전트 `ask` — 질문/답 페어링 설계 (3판)

> 상태: **설계 3판 · 재리뷰 대기**
> 1판 → 외부 리뷰("구현 불가") → 2판 → 재리뷰 + 사용자 지적 → 3판
> 각 판이 틀린 것과 그 경위는 §11 에 남긴다.

## 0. 한 줄

**질문에는 이미 주소가 있다** — `to = tm.current_author`(`:1517`). 지금은 그
주소로 **배달도 안 되고**(peer 는 질문을 못 본다) **판정도 안 한다**(도착 순서가
답이다). 이 설계는 배달과 자격을 **그 주소 하나에 맞춘다.**

## 1. 문제

상주 에이전트가 `ask` 를 부르면 자기 **inbox** 에서 답을 기다린다(`:1538`).
그 inbox 는 "새 일감"과 "내 질문의 답"을 구분하지 않는다.

```python
tm.state = "waiting_ask"          # :1535
item = tm.inbox.get()             # :1538  ← 무엇이 오든 답이 된다
```

**소비된 항목은 `_handle_request` 를 거치지 않는다.** 본문은 "User responded:"
로 모델에게 가지만 **라우팅이 통째로 사라진다** — 그 `seq` 의 회신이 main
메일박스로 안 가고, peer 요청이었다면 `_deliver_peer_reply` 가 안 돌아
**요청한 peer 가 영원히 기다린다.**

### 1.1 주소와 배달이 어긋나 있다

질문 페이로드는 `to = tm.current_author` 로 **수신자를 명시**하는데
(`:1511-1522`), 실제 배달은 그걸 안 따른다:

| `current_author` | 질문이 실제로 가는 곳 | 그 주소의 주인이 받나 |
|---|---|---|
| `main` | main 메일박스(`:1523-1534`) + A 의 🤝 창 + ❓ 트레이 | ✅ |
| `agent:B` | **A 의 🤝 창 + ❓ 트레이뿐** | ❌ **B 는 모른다** |
| `user:bob` | A 의 🤝 창 + ❓ 트레이 | ✅ |

**peer 케이스가 구멍이다.** 주소는 B 인데 B 는 못 듣는다. 지금 그게 동작하는
것처럼 보이는 이유는 **사람이 ❓ 트레이로 우연히 메워 주기 때문**이고, 그건
기능이 아니다.

### 1.2 그래서 누구든 A 를 풀어 버린다

| 도착한 것 | 지금 | 3판 |
|---|---|---|
| 주소 주인의 답 | 답 ✓ | 답 ✓ |
| **다른 peer 의 message** | **답으로 먹힘** | 일감으로 큐잉 |
| **main 의 새 일감** (주소가 main 아닐 때) | **답으로 먹힘** | 일감으로 큐잉 |
| **사람의 무관한 메시지** | **답으로 먹힘** | (주소가 사람이면 답, 아니면 일감) |

## 2. 조사 — 코드가 이미 아는 것

**① 하네스는 "답 기다리는 중"을 이미 안다** — `was_waiting`(`:1722`). 다만
**표시 힌트로만** 쓴다. 주석도 *"표시용 힌트 — 도착 순서가 진실"*.

**② peer 왕복 배관이 이미 있고, 그 주소 규약이 우리 규칙과 맞는다.**
`_deliver_peer_reply` 는 회신을 요청자 inbox 로 `author=f"agent:{from_key}"`
로 재주입한다(`:769-775`). 즉 **B 가 답하면 그 답의 author 는 `agent:B`** 다.

**③ 미결 질문은 한 번에 1개 — 단 한 턴에 여러 번 순차로 생긴다.**
`_handle_ask` 의 `"\n".join`(`dispatch.py:1116`)은 **op 하나 안**만 합친다.
`AskTool` 설명이 *"to ask several, emit several `ask` ops"* 라고 가르친다
(`virtual.py:52-54`). 한 턴 = N번의 순차 대기.

**④ 발신자 종류는 넷** — `main`(:703) · `agent:<key>`(:772·:856) ·
`user`(CLI `main.py:339`) · `user:<nick>`(`server.py:1266`).

**⑤ CLI 에는 사람이 답할 자리가 없다.** CLI 명령은 `run`/`setup`/`sessions`/
`update`/`web` 뿐이고 **대화형 프롬프트 루프가 없다** — `@agt-<key>` 는
`run` 의 **인자**다. 에이전트가 물으면 `has_active_work()` 가 `waiting_ask`
를 제외하므로(`:477`) 펌프/busy-wait 가 빠져나가 세션이 끝난다. 코드에 그
흔적이 있다: *"⚠ 에이전트 … 답변 대기 중인 채 종료"*(`runtime.py:149-152`).
→ **사람은 웹 전용 답변자다.**

**⑥ ❓ 트레이는 `state === "waiting_ask"` 만 보고 뜬다**(`app.js:2093`) —
질문이 누구에게 갔는지는 안 본다.

**⑦ 거부는 이 표면의 기존 어휘다** — `unknown agent`(:673) · `is dead`(:676)
→ `ToolResult(False, "request rejected: …")`.

**⑧ 거부 반복은 액션 루프 탐지기가 못 잡는다.** 실패한 도구 재호출은
`prev_was_error=True` 로 전달되고(`dispatch.py:826-834`), 탐지기는 그걸
*"Legitimate retry after a failure"* 로 보고 **카운터를 리셋한다**
(`recovery/detectors.py:79-86`). 상한은 `max_turns` 뿐이다.

## 3. 설계

### 3.1 슬롯 — 큐를 늘리지 않는다

막혀 있는 worker 는 분류할 수 없다. ask 는 worker 루프 꼭대기가 아니라
`_worker → _handle_request → run_subagent_message → run_loop → dispatch →
_op_ask → handler` 의 **콜스택 아래**에서 막힌다. **대신 생산자가 분류한다**
— `request()` 는 보내는 쪽 스레드에서 돌고 막혀 있지 않다.

미결 질문이 언제나 0 또는 1 이므로(§2-③) 담는 그릇은 **큐가 아니라 슬롯**이다.

```python
# AgentInstance — 필드 4개
self.awaiting: str = ""       # 대기 중인 질문 (빈 문자열 = 안 기다림)
self.awaiting_to: str = ""    # 그 질문의 **주소** (= ask 시점의 current_author)
self.answer: str = ""
self.answered = threading.Event()
```

### 3.2 자격은 주소 하나로 정해진다

```python
def _answer_kind(tm, author: str, *, explicit: bool = False) -> str:
    """'answer' | 'work' | 'reject'"""
    if not tm.awaiting:
        return "reject" if explicit else "work"     # 없는 질문에 답 (§3.5)
    if author == tm.awaiting_to:
        return "answer"                             # 질문이 그에게 갔다
    if tm.awaiting_to.startswith("user") and author.startswith("user"):
        return "answer"                             # 사람은 닉이 달라도 같은 창
    return "reject" if explicit else "work"
```

한 규칙이 세 경우를 다 덮는다:

| `awaiting_to` | 답 | 일감 |
|---|---|---|
| `main` | main | peer · 사람 |
| `agent:B` | **B** | main · 다른 peer · 사람 |
| `user:bob` | 사람(웹) | main · peer |

2판의 `main`/`user*`/`agent:*` 3분기가 **한 줄로 줄었고**, peer 가 답변자에서
빠져 있던 오류가 사라졌다.

### 3.3 peer 질문은 그 peer 에게 **배달**한다

주소가 `agent:B` 면 질문을 B 에게 실제로 보낸다. **새 배관이 없다** — `message`
도구가 쓰는 그 경로 그대로다(`_make_message_handler:856`).

```
① A: ask(...)  → 하네스: request(B, 질문, author=f"agent:{A}", expects_reply=True)
                 A 는 슬롯 대기, awaiting_to = "agent:B"
② B: 자기 큐에서 꺼내 처리 → complete
③ 하네스: _deliver_peer_reply(A, B, 출력, hop)
          → request(A, 출력, author="agent:B", expects_reply=False)   :769-775
④ A: author == awaiting_to → **답**, 작업 재개
```

B 는 하던 일을 멈추지 않는다 — 질문이 B 의 inbox 에 줄 서고 한가해지면 처리
한다. 그동안 A 가 막혀 있는 것은 `ask` 의 정의 그대로다.

**대안으로 검토한 "거부하고 `message`+`complete` 를 쓰게 하라"는 등가가
아니다**(§10-b): A 가 `complete` 하면 그 request 가 끝나 B 는 답이 아니라
**완료 회신**을 받는다 — 끝나지 않은 일을 끝났다고 보고하는 셈이다.

### 3.4 순환은 거부한다

A 가 B 에게 묻는데 B 가 이미 A 의 답을 기다리는 중이면 **답할 수 있는 주체가
구조적으로 없다.** 거부한다.

```python
# ask 진입 시 — awaiting_to 체인을 따라간다 (깊이 상한 _MAX_PEER_HOPS)
def _ask_cycle(registry, asker_key, target) -> str | None:
    seen, cur = {asker_key}, target
    for _ in range(_MAX_PEER_HOPS):
        if not cur.startswith("agent:"):
            return None
        k = cur.split(":", 1)[1]
        if k in seen:
            return f"{k} 는 당신의 답을 기다리는 중입니다 — 먼저 답하세요."
        tm = registry.get(k)
        if tm is None or not tm.awaiting:
            return None
        seen.add(k); cur = tm.awaiting_to
    return "질문 사슬이 너무 깊습니다."
```

관찰로 돌아가는 거부라 모델이 판단해 진행하거나 `complete` 할 수 있다.

### 3.5 명시적 답이 빗나가면 거부한다

2단계에서 main 이 `mode:"answer"` 를 쓰는데 그 질문이 (ⅰ)없거나 (ⅱ)자기에게
온 게 아니면, **답 텍스트를 새 일감으로 큐잉해 LLM 턴을 태우면 안 된다.**
`explicit and kind != "answer" → reject`(§3.2). 문구가 사실을 말한다:
*"no pending question"* / *"the question is addressed to user:bob"*.

### 3.6 흐름

```python
# ask 핸들러 — **arm 먼저, 그 다음 공개** (1판 C1 회귀 수리)
if err := _ask_cycle(registry, tm.key, tm.current_author):   # §3.4
    return f"(ask rejected: {err})"
with self._cv:
    tm.answered.clear()          # clear 가 arm 보다 먼저 (이전 set 잔류 방지)
    tm.awaiting = question
    tm.awaiting_to = tm.current_author
    tm.answer = ""
    tm.state = "waiting_ask"
# ↓ 여기서부터 공개 — 이제 답이 와도 슬롯이 받는다
renderer.agent_message(**q_payload); self._log_conversation(...)
if tm.awaiting_to == "main":
    self._push_reply({"kind": "question", ...})
elif tm.awaiting_to.startswith("agent:"):
    self.submit(tm.awaiting_to.split(":",1)[1], question,     # §3.3 배달
                author=f"agent:{tm.key}", expects_reply=True)
self._notify_roster()

tm.answered.wait()               # 1단계엔 타임아웃 없음 (§8)

if tm.stop_event.is_set():       # 종료 wake 를 **데이터보다 먼저** (1판 C3)
    with self._cv: tm.awaiting = tm.awaiting_to = ""
    return "(no response — agent is being terminated)"
with self._cv:
    tm.awaiting = tm.awaiting_to = ""
    answer, tm.answer = tm.answer, ""
    tm.state = "busy"
self._notify_roster()
return answer
```

```python
# submit() — 새 함수. request() 는 시그니처를 **바꾸지 않는다** (재리뷰 C)
def submit(self, key, message, *, author="main", hop=0,
           expects_reply=True, explicit=False) -> tuple[str, str]:
    """(error, verdict) — verdict ∈ answer | work | rejected"""
    ...
    with self._cv:
        kind = _answer_kind(tm, author, explicit=explicit)
        if kind == "reject":
            return _reject_message(tm, author), "rejected"
        tm.queued += 1; seq = tm.queued        # 답도 seq 를 받는다 (재리뷰 D)
        if kind == "answer":
            tm.answer = message if author == "main" else f"[{author}]: {message}"
            tm.awaiting = tm.awaiting_to = ""   # 원자적 claim
            tm.answered.set()
        else:
            tm.inbox.put({...})
    # ↓ 락 밖 — 답이든 일감이든 **항상** 창·로그·로스터에 남긴다 (1판 C2)
    renderer.agent_message(**payload); self._log_conversation(tm, payload)
    self._notify_roster()
    return "", kind

def request(self, key, message, **kw) -> str:   # 기존 8개 호출자 보존
    return self.submit(key, message, **kw)[0]
```

**`_cv` 아래서는 렌더러·디스크 I/O·`interactive_lock` 을 부르지 않는다.**
이 파일의 기존 규율이다(`_cv` 구간은 전부 짧다: :689 · :735 · :869 · :908).
`answered.set()` 은 Event 자기 락만 잡으므로 안전하고, 대기자는 `wait()` 중
아무 락도 쥐지 않는다.

## 4. ❓ 트레이 — 사람에게 온 질문만

지금 트레이는 `state === "waiting_ask"` 만 보고 뜬다(`app.js:2093`).
**`awaiting_to` 가 사람일 때만** 뜨게 한다 — 트레이는 "답하라"는 어포던스이고,
내게 온 질문이 아니면 그게 있으면 안 된다.

- 로스터 엔트리에 `awaiting_to` 를 실어 보낸다(`snapshot()` :350 additive).
- `main`·`agent:B` 로 간 질문은 **채널 카드로는 그대로 보인다**(투명성) —
  입력 어포던스만 사라진다.

**이 변경이 §1.1 의 구멍을 눈에 보이게 만든다.** 지금은 사람이 트레이로 peer
케이스를 메워 주고 있어서 구멍이 안 보였다. §3.3 의 배달이 그 자리를 제대로
채우므로 가림막을 걷어도 된다 — **둘은 같이 가야 한다.**

## 5. 동시성

| # | 레이스 | 처리 |
|---|---|---|
| 1 | 답이 `wait()` 보다 먼저 | `Event` 가 흡수. **`clear()` 를 arm 보다 먼저** |
| 2 | 공개 전에 답 도착 | **arm 을 공개보다 먼저**(§3.6) — 1판은 반대라 답이 inbox 로 샜다. 가짜 러너는 지연 0이라 테스트의 기본 경로다 |
| 3 | 답변자 둘 동시 | `_cv` 아래 `awaiting` claim — 두 번째는 `work`(비-explicit) 또는 `reject`(explicit) |
| 4 | 종료 중 대기 | `kill`(:935)·`shutdown_all`(:953)에 `answered.set()` 추가 — **없으면 `join` 이 2/5초 타임아웃**. 깨어나면 `stop_event` 를 데이터보다 먼저 본다 |
| 5 | 같은 스레드가 생산자이자 대기자 | 도달 불가 — 자기 메시지 거부(:845), 서브루프에 registry 없음(:1829) |
| 6 | dead 에 답 | 기존 `state == "dead"` 거부가 앞선다(:674) |
| 7 | **A→B 상호 대기** | §3.4 순환 검사 — §3.3 이 새로 만드는 위험 |

`_cv` 는 RLock 기반이라 `_save_state()` 재진입(:1077)이 안전하고, 렌더러
`_lock` → `_cv` 순서로 잡는 곳이 없어 역전이 없다.

## 6. 영향 받는 표면

| 곳 | 변경 |
|---|---|
| `AgentInstance.__init__` :293 | 필드 4개 |
| `_make_ask_handler` :1496 | 순환 검사 · arm→공개 · peer 배달 · 슬롯 대기 · stop_event 우선 |
| **`submit()` 신설** | 분류 + verdict. `request()` 는 `submit()[0]` 로 보존 |
| `kill` :930 · `shutdown_all` :949 | `answered.set()` |
| `_agent_request` :1719 | `was_waiting` TOCTOU 제거 → verdict 사용 |
| `snapshot()` :350 | `awaiting_to` additive (트레이용) |
| `app.js` :2090 | 트레이 필터에 `awaiting_to` 가 사람인지 추가 |
| **2단계** `agent_tool.py:44` | `answer` 모드 |
| **2단계** :226 · :1734 · `system_prompt.py:1136` | `request` 를 답변 op 로 가르치는 곳 **전부** |

**영속 변화 없음.** `awaiting*` 은 저장하지 않는다 — 블록된 스레드는 프로세스와
함께 죽는다. `resume_teammate`(:982)·`restore`(:1129)는 **새 `AgentInstance`**
를 만들므로 낡은 `awaiting` 이 새지 않는다(재리뷰 확인).

## 7. 알고 두는 것

**① 한 턴에 `ask` op 이 여럿이면 순차로 N번 기다린다**(§2-③). 1단계는
타임아웃이 없어 "Q2 가 N분 멈춘다"는 새 실패가 없다. 2단계에서 한 턴의 `ask`
op 들을 한 번의 핸들러 호출로 접는 것을 검토한다.

**② `ask` 도 거부도 루프 탐지기 밖이다** — `ask` 는 탐지기 앞에서 반환하고
(`dispatch.py:584` vs `:832`), 거부는 `prev_was_error` 리셋에 먹힌다(§2-⑧).
**2단계의 거부에는 `reject_count` 가드를 함께** 넣는다(답 claim 시 리셋,
2회째부터 문구 강화).

**③ 상주 에이전트 안의 delegate 가 `ask` 하면 핸들러가 없다** —
`tool_bridge` 가 `ask_handler` 를 안 넘겨 `renderer.prompt_user` 로 가고,
에이전트 worker 스레드에서 `interactive_lock` 을 잡은 채 **main 채팅에**
출처 없이 물어본다. 이 설계와 무관한 **기존 구멍**, 별건.

**④ CLI 에서는 사람이 답할 수 없다**(§2-⑤). 1단계가 그걸 바꾸지 않는다.
`awaiting_to` 가 `user*` 인데 CLI 면 그 에이전트는 세션 종료까지 막힌다 —
오늘과 같다.

## 8. 실행 계획

**1단계 — 모델 계약 변경 없음**
필드 4개 · `submit()` · arm→공개 · 슬롯 · `_answer_kind` · **peer 배달**(§3.3)
· **순환 검사**(§3.4) · 종료 두 곳 · 트레이 필터(§4).
얻는 것: peer 가 답을 먹는 사고 제거 · **peer 질문이 실제로 배달됨** ·
요청한 peer 가 영원히 기다리던 것 해소.

**2단계 — `mode:"answer"` + main 거부**
`AGENT_MODES` · `_agent_request` verdict · `explicit` 규칙(§3.5) ·
**`reject_count` 가드**(§7-②) · **프롬프트 3곳**(:226 · :1734 ·
`system_prompt.py:1136`) — 하나라도 남으면 모델이 프롬프트를 따르다 거부당한다.

**3단계 (조건부)** — 타임아웃. §7-② 가드와 **함께**만. 웹에서 실제 hang 이
관측되기 전에는 넣지 않는다.

### 테스트 계획

| 층 | 내용 |
|---|---|
| 자격 | 주소 주인의 답 → 답 / **다른 peer → 일감** / 주소가 peer 일 때 main → 일감 / 주소가 main 일 때 사람 → 일감 |
| peer 배달 | A 가 물으면 **B 의 inbox 에 실제로 들어간다** · B 가 complete 하면 그 출력이 A 의 답이 된다(왕복 1회 통합 TC) |
| 순환 | A→B, B→A → 거부 · 깊이 3 사슬 · 상대가 dead/없음이면 통과 |
| 손실 없음 | 일감 판정된 항목이 inbox 에 남아 `_handle_request` 를 정상 통과 · peer 요청이면 `_deliver_peer_reply` 가 돈다 |
| 레이스 | **공개 전 도착**(가짜 러너 지연 0) · 동시 답변자 둘 · clear 순서 |
| 종료 | `kill`/`shutdown_all` 즉시 깨움 · `join` 타임아웃 없음 · **기존 TC `test_shutdown_unblocks_pending_ask` 가 "no response" 그대로** |
| 부수효과 | 답도 🤝 창·`conversation.jsonl`·로스터에 남는다 · 답에도 `seq` 가 있다 |
| 호출자 보존 | `request()` 반환형이 `str` 그대로 — 기존 8개 호출자 무변경 |
| 트레이 | `awaiting_to` 가 사람일 때만 뜬다 · main/peer 질문은 카드로는 보인다 |
| 비회귀 | 로스터 dot · 종료 경고 · `_SHUTDOWN` 재게시 삭제가 배치 경로(:1293) 무영향 |

## 9. 왜 큐를 늘리지 않는가

이미 셋이다(입력 큐 · inbox · 메일박스). 네 번째를 안 만드는 이유는 취향이
아니라 **카디널리티가 1이기 때문**이다. 부수 효과로 `_SHUTDOWN` 재게시
(`:1540`)가 없어진다 — sentinel 이 inbox 에만 있게 되므로.

## 10. 기각한 대안

**(a) 질문 id 를 모델/사람에게 요구.** 하네스가 이미 `was_waiting` 을 알고
(§2-①), `agent` 도구엔 이미 `key`(에이전트 key)가 있어 이름이 충돌하며,
사람에게는 물을 수 없다. **`mode:"answer"` 는 id 가 아니라 의도 선언**이라
이 반대가 적용되지 않는다.

**(b) peer 발신 작업 중 `ask` 를 거부.** 2판→3판 사이에 제안됐다가 **철회**.
`message`+`complete` 가 등가가 아니다 — A 가 `complete` 하면 B 는 답이 아니라
완료 회신을 받고, 끝나지 않은 일이 끝난 것으로 기록된다. 무엇보다 §3.3 의
배달이 **새 배관 없이** 되므로 기능을 닫을 이유가 없다.

**(c) inbox 에서 받아 아닌 것은 되돌려 넣기.** `SimpleQueue` 에 put-front 가
없어 tail 재삽입 → 답이 안 오면 **스핀**. worker 루프가 1칸 stash 를 손으로
만든 것도 같은 제약(:1270).

**(d) `ask` 를 비블로킹으로.** **슬롯이 없어지지 않고 옮겨갈 뿐**이다: 답이 새
inbox 항목으로 오면 그 회신을 원 요청자에게 라우팅하려고 멈춘 요청
(`seq`·`author`·`expects_reply`·`answers`)을 기억해야 하고 그게 `paused_item`
슬롯이다. 거기에 `_op_ask` terminal 변종 · `_handle_request` 의 "보류" 회신
종류 · **35B 가 `ask` 직후 반드시 `complete` 해야 한다는 모델 계약**이 붙는다.

**(e) 타임아웃으로 교착 방지.** 교착은 이미 펌프 종료 + `shutdown_all` 로
해결돼 있다(§2-⑤). 타임아웃은 **루프 탐지 밖의 무한 재질문**이라는 새 실패를
들여온다(§2-⑧). 3단계로.

## 11. 개정 경위

### 2판 (1판 리뷰 반영)

| 1판 | 확인 | 결과 |
|---|---|---|
| `_is_answerer(author)` 로 충분 | main 은 무조건 답변자 → **새 일감이 그대로 먹힌다**(자기 문제 표 3행 미해결) | 판정 축 추가 |
| 질문 여럿도 한 번의 대기 | `join` 은 op **하나 안** | §2-③ |
| (회귀) arm 이 공개보다 뒤 | 즉답이 inbox 로 샌다 | §3.6 |
| (회귀) `return ""` | 창·로그·로스터를 건너뛴다 | §3.6 |
| (TC 파괴) 종료 wake | 빈 답 반환 | §3.6 |

### 3판 (2판 재리뷰 + 사용자 지적)

| 2판 | 확인 | 결과 |
|---|---|---|
| 사람은 ❓ 트레이에서만 본다 → `explicit` 요구 | **틀렸다.** 사람은 🤝 창에서도 본다. CLI 엔 트레이가 없다 | 사람 `explicit` 축 **삭제** |
| 1단계 = 계약 변경 없음 | `mode:"answer"` 가 없으니 main 이 항상 `explicit=False` → **1단계에서 main 이 답을 못 한다** | 1단계 규칙 명시 |
| 거부는 자기수정된다 | **틀렸다.** `prev_was_error` 가 탐지기를 리셋한다(§2-⑧) | `reject_count` 가드를 2단계에 |
| `request() -> tuple` | 호출자 8곳이 truthiness 로 판정 — 튜플은 항상 참 | `submit()` 신설 |
| **peer 는 답변자가 아니다** | **틀렸다.** 질문을 못 받아서였을 뿐 — 받으면 답할 수 있고, 배관이 이미 있다 | §3.3 배달 · §3.2 `awaiting_to` |
| 트레이는 그대로 | 내게 온 질문이 아닌데 답 어포던스가 있다 | §4 |

### 교훈

1·2판이 틀린 자리는 전부 **"이 값 하나로 판정할 수 있다"** 고 적은 곳이었다
— `author` 하나, 질문 개수 하나, `explicit` 하나. 3판이 `awaiting_to`
하나로 돌아온 것은 그 반복처럼 보이지만 다르다: **그 값은 우리가 새로
만든 판정 기준이 아니라 이미 페이로드에 있던 주소**(`to`, `:1517`)다.
설계가 한 일은 기준을 발명한 게 아니라 **있던 주소를 배달과 판정이 따르게
한 것**이다.
