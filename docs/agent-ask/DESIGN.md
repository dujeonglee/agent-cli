# 에이전트 `ask` — 질문/답 페어링 설계 (4판)

> 상태: **설계 4판 · 재리뷰 대기**
> 1판 →("구현 불가")→ 2판 →(재리뷰+사용자 지적)→ 3판 →("교착 셋")→ 4판
> 각 판이 틀린 것과 경위는 §11.
>
> **4판의 방향 전환**: 세 판 연속 뒤집힌 것을 되짚으니 **핵심(슬롯 + 생산자
> 분류 + 주소)은 세 리뷰를 모두 통과했고, 깨진 것은 전부 내가 *추가*한
> 것**이었다(2판 `explicit` 축, 3판 peer 배달·트레이 필터). 그래서 1단계를
> **"오늘 있는 사고 하나를 없애되 아무것도 새로 만들지 않는"** 크기로 되돌린다.
> peer 배달은 그 자체로 가치가 있으나 교착 셋을 데려오므로 2단계로 옮기고,
> 살리는 방법을 리뷰에 따로 묻는다(§12).

## 0. 한 줄

**질문에는 이미 주소가 있고**(`to = tm.current_author`, `:1516`) **종류도 이미
있다**(`expects_reply`, `:774` vs `:856`). 지금은 둘 다 안 보고 **도착 순서**로
답을 정한다. 이 설계는 판정을 그 둘에 맞춘다 — 배달까지 맞추는 것은 2단계다.

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
`user`(CLI `run` 인자 **및 웹 main 채팅**의 `@agt-…`, `main.py:339`) ·
`user:<nick>`(웹 🤝 창·❓ 트레이, `server.py:1266`).

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

**⑨ 주소 말고 두 번째 기존 값이 있다 — `expects_reply`.** `message` 는
`True`(:856), `_deliver_peer_reply` 의 회신은 `False`(:774). `_is_human_direct`
(:1330-1338)가 **이미 이 값을 판정에 쓴다.** 즉 코드는 "누구에게"뿐 아니라
"요청인가 회신인가"도 이미 구분하고 있다.

**⑩ 사람은 peer 가 아니라 운영자다.** 웹 창 입력이 `waiting_ask` 인 에이전트에
닿으면 **"main 과 선착순"** 으로 답이 된다고 문서화돼 있다(`server.py:1249-1251`).
main 이 엉뚱하게 답하거나 답변자가 사라졌을 때의 **유일한 탈출구**다.

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

### 3.2 자격 — 주소와 종류, **그리고 받았는가**

3판은 `author == awaiting_to` 한 줄이었다. 틀렸다. **질문을 받지 않은 주체는
주소가 맞아도 답변자가 아니다** — 1단계에서 peer 는 질문을 못 받으므로(§3.3),
peer 의 메시지가 주소만 맞다고 답이 되면 그건 여전히 도착 순서 판정이다.

```python
def _answer_kind(tm, author, *, expects_reply=True, explicit=False) -> str:
    """'answer' | 'work' | 'reject'"""
    if not tm.awaiting:
        return "reject" if explicit else "work"

    # 사람 = 운영자. 주소와 무관하게 답할 수 있다 (§2-⑩ — 유일한 탈출구).
    if author.startswith("user"):
        return "answer"

    if author == tm.awaiting_to:
        if author.startswith("agent:"):
            # peer 는 **회신**일 때만 답이다. `message`(expects_reply=True)는
            # 새 요청이지 답이 아니다 (§2-⑨). 1단계엔 배달이 없으므로 이
            # 분기는 도달하지 않는다 — 2단계 peer 배달과 짝이다.
            return "answer" if not expects_reply else "work"
        # main — 1단계는 레거시(답), 2단계는 explicit 요구 (§3.6)
        return "answer" if (PHASE1 or explicit) else "reject"

    return "reject" if explicit else "work"
```

| `awaiting_to` | 답 | 일감 | 거부(2단계) |
|---|---|---|---|
| `main` | 사람 · main(`mode:"answer"`) | peer | main 의 `request` |
| `agent:B` | 사람 · **B 의 회신**(2단계) | main · 다른 peer · B 의 `message` | — |
| `user:bob` | 사람 | main · peer | — |

**사람이 모든 행에 있는 것이 의도다.** 운영자는 어느 질문에든 개입할 수 있어야
한다 — 그게 3판에서 제거하려다 리뷰에 지적받은 탈출구다.

### 3.3 peer 질문 배달 — **2단계로 미룬다**

주소가 `agent:B` 인 질문은 지금 **B 에게 배달되지 않는다**(§1.1). 배달하는
것이 옳고 배관도 있다(`_deliver_peer_reply` 의 `author=f"agent:{from_key}"` 가
`awaiting_to` 와 맞는다). 그런데 3판에서 그걸 1단계에 넣었더니 **오늘 없는
영구 교착 셋**이 생겼다:

| | 무엇 |
|---|---|
| L1 | 배달 `submit()` 의 에러를 버려서, B 가 dead 면 A 가 armed 슬롯에서 영원히 대기 |
| L2 | B 가 질문을 꺼내기 전에 kill/crash 되면 **아무도 A 를 안 깨운다** — `kill` 은 inbox 를 안 비우고 큐잉분은 유실된다(코드가 그렇게 적고 있다: `:201`) |
| L3 | 순환 검사가 `_cv` 밖이라 A·B 동시 ask 가 둘 다 통과 |

거기에 L4(B 는 A 가 막힌 걸 모르고, 프롬프트는 peer 에게 `message` 로 답하라고
가르친다 — `system_prompt.py:1123-1130`)까지 있다.

**1단계에서는 배달하지 않는다.** 그러면 peer 주소 질문의 답변자는 **사람뿐**
이고, 그건 **오늘과 같다.** 대신 peer 의 무관한 메시지가 답으로 먹히던 사고는
사라진다 — 그게 원래 제보된 버그다.

| | 오늘 | 1단계 | 2단계 |
|---|---|---|---|
| peer 의 무관한 메시지 | **답으로 먹힘** | 일감 ✅ | 일감 |
| peer 주소 질문의 답변자 | 사람만 | 사람만 (동일) | 사람 + **B** |
| 새 교착 | — | **없음** | L1~L4 처리 필요 |

살리는 방법은 §12 에 따로 묻는다.

### 3.4 순환 검사 — 2단계와 함께

배달이 없으면 A→B 대기 자체가 성립하지 않으므로 1단계엔 불필요하다. 2단계에
넣을 때는 **`_cv` 안에서** 한다(L3).

### 3.5 거부 문구는 **그 자리에서 실행 가능**해야 한다

거부의 자기수정 근거는 **관찰문의 op 를 모델이 복사하는 것뿐**이다(§2-⑧:
루프 탐지기가 못 잡으므로 다른 안전망이 없다). 그렇다면 **복사할 것이 관찰문
안에 있어야 한다** — 질문 본문과 응답 op 를 같이 싣는다. 세 거부가 한 헬퍼를
쓴다(문구가 갈라지면 한쪽만 고쳐지므로).

```
ask rejected: 🐙 code-reviewer agt-b1 가 당신의 답을 기다리는 중입니다.

  질문: "이 마이그레이션을 지금 돌릴까요, 아니면 리뷰 후에 할까요?"

  먼저 답하세요:
    {"action":"message","action_input":{"to":"agt-b1","text":"<답>"}}
  그 다음 당신의 질문을 다시 보내세요.
```

```
request rejected: 🦊 code-writer agt-x9 가 당신의 답을 기다리는 중입니다.

  질문: "어느 브랜치에 커밋할까요?"

  먼저 답하세요:
    {"mode":"answer","key":"agt-x9","task":"<답>"}
  그 다음 이 요청을 다시 보내세요. (이 요청은 큐에 넣지 않았습니다.)
```

```
answer rejected: agt-x9 의 질문은 user:bob 에게 간 것이라 당신이 답할 수 없습니다.
  질문: "이 설정 파일을 덮어써도 될까요?"
  (사람의 답을 기다리는 중입니다 — 다른 일을 진행하세요.)
```

규칙 셋:

- **질문 본문을 싣는다.** 300자로 자른다 — 모델이 무엇에 답하는지 알아야 하고,
  질문은 로그 한 줄이 아니라 문장이라 길어질 수 있다.
- **응답 op 를 복사 가능한 형태로 싣는다.** 대상 key 가 박혀 있어야 한다
  (`<답>` 만 채우면 되게).
- **다음에 할 일을 말한다.** "다시 보내라" / "다른 일을 하라" — 거부가 막다른
  길이 아님을 밝힌다.

2단계의 `reject_count` 가드(§7-②)는 이 문구를 **강화**하는 형태로 붙는다:
2회째 거부는 같은 내용에 *"직전 거부를 이미 받았습니다 — 답하지 않으면 그
에이전트는 계속 막혀 있습니다"* 를 덧붙인다.

### 3.6 명시적 답이 빗나가면 거부한다

2단계에서 main 이 `mode:"answer"` 를 쓰는데 그 질문이 (ⅰ)없거나 (ⅱ)자기에게
온 게 아니면, **답 텍스트를 새 일감으로 큐잉해 LLM 턴을 태우면 안 된다.**
`explicit and kind != "answer" → reject`(§3.2). 문구는 §3.5 형식을 따른다.

### 3.7 흐름 (1단계)

```python
# ask 핸들러 — **arm 먼저, 그 다음 공개**
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
# (2단계: awaiting_to 가 agent:* 면 여기서 배달 — §3.3)
self._notify_roster()

tm.answered.wait()               # 1단계엔 타임아웃 없음 (§8)

if tm.stop_event.is_set():       # 종료 wake 를 **데이터보다 먼저**
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
# submit() — 신설. request() 는 시그니처를 **바꾸지 않는다**
def submit(self, key, message, *, author="main", hop=0,
           expects_reply=True, explicit=False) -> tuple[str, str]:
    """(error, verdict) — verdict ∈ answer | work | rejected"""
    ...
    with self._cv:
        kind = _answer_kind(tm, author, expects_reply=expects_reply,
                            explicit=explicit)
        if kind == "reject":
            return _reject_message(tm, author), "rejected"
        tm.queued += 1; seq = tm.queued        # 답도 seq 를 받는다
        if kind == "answer":
            tm.answer = message if author == "main" else f"[{author}]: {message}"
            tm.awaiting = tm.awaiting_to = ""   # 원자적 claim
            tm.answered.set()
        else:
            tm.inbox.put({...})
    # ↓ 락 밖 — 답이든 일감이든 **항상** 창·로그·로스터에 남긴다
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

## 4. ❓ 트레이 — 유지하고 **주소 라벨만** 붙인다

3판은 `awaiting_to` 가 사람일 때만 띄우려 했다. **철회한다.** 사람은 peer 가
아니라 **운영자**이고(§2-⑩), 트레이는 main 이 엉뚱하게 답하거나 답변자가
사라졌을 때 **1단계의 유일한 탈출구**다. 게다가 "main 과 선착순"은 명시된
기능이다(`server.py:1249-1251`).

대신 **누구에게 간 질문인지 라벨을 붙인다** — 어포던스는 남기되 맥락을 준다:

```
❓ 🦊 code-writer agt-x9 이(가) 물었습니다        → main 에게
❓ 🐙 code-reviewer agt-b1 이(가) 물었습니다      → 🐬 orchestrator agt-3e
❓ 🦉 explorer agt-7c 이(가) 물었습니다           → 나에게
```

질문 페이로드에 이미 `to` 가 있으므로(`:1516`) `ovAskTray[d.key]`(app.js:2207)
에 같이 담으면 된다 — **`snapshot()` 변경도 불필요하다.**

## 5. 동시성

| # | 레이스 | 처리 |
|---|---|---|
| 1 | 답이 `wait()` 보다 먼저 | `Event` 가 흡수. **`clear()` 를 arm 보다 먼저** |
| 2 | 공개 전에 답 도착 | **arm 을 공개보다 먼저**(§3.7) — 1판은 반대라 답이 inbox 로 샜다. 가짜 러너는 지연 0이라 테스트의 기본 경로다 |
| 3 | 답변자 둘 동시 | `_cv` 아래 `awaiting` claim — 두 번째는 `work`(비-explicit) 또는 `reject`(explicit) |
| 4 | 종료 중 대기 | `kill`(:935)·`shutdown_all`(:953)에 `answered.set()` 추가 — **없으면 `join` 이 2/5초 타임아웃**. 깨어나면 `stop_event` 를 데이터보다 먼저 본다 |
| 5 | 같은 스레드가 생산자이자 대기자 | 도달 불가 — 자기 메시지 거부(:845), 서브루프에 registry 없음(:1829) |
| 6 | dead 에 답 | 기존 `state == "dead"` 거부가 앞선다(:674) |
| 7 | **A→B 상호 대기** | 1단계엔 없음(배달 안 함). 2단계에 §3.4 순환 검사를 **`_cv` 안에서** |

`_cv` 는 RLock 기반이라 `_save_state()` 재진입(:1077)이 안전하고, 렌더러
`_lock` → `_cv` 순서로 잡는 곳이 없어 역전이 없다.

## 6. 영향 받는 표면

**1단계**

| 곳 | 변경 |
|---|---|
| `AgentInstance.__init__` :293 | 필드 4개(`awaiting`·`awaiting_to`·`answer`·`answered`) |
| `_make_ask_handler` :1496 | arm→공개 · 슬롯 대기 · stop_event 우선 |
| **`submit()` 신설** | 분류 + verdict. `request()` 는 `submit()[0]` 로 보존 |
| `kill` :930 · `shutdown_all` :949 | `answered.set()` — **없으면 `join` 이 2/5초 타임아웃** |
| `_agent_request` :1719 | `was_waiting` TOCTOU 제거 → verdict 사용 |
| `app.js` :2207 | 트레이 항목에 `to` 를 담아 라벨 표시 (§4) |

**2단계**

| 곳 | 변경 |
|---|---|
| `_make_ask_handler` | peer 배달 + 순환 검사(`_cv` 안) + 배달 에러 처리(L1) |
| `_worker` finally :1309 | 죽을 때 나를 기다리는 대기자 깨우기(L2) |
| 배달 질문 꼬리표 | "A 가 이 답을 기다린다 — `message` 말고 `complete` 로 답하라"(L4) |
| `agent_tool.py:44` | `answer` 모드 |
| :226 · :1734 · `system_prompt.py:1136` | `request` 를 답변 op 로 가르치는 곳 **전부** |
| `reject_count` | 거부 가드 (§7-②) |

**영속 변화 없음.** `awaiting*` 은 저장하지 않는다. `resume_teammate`(:982)·
`restore`(:1129)는 **새 `AgentInstance`** 를 만든다.

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

**④ CLI 에서는 사람이 답할 수 없다**(§2-⑤). 1단계가 그걸 바꾸지 않는다 —
오늘과 같다. 사람은 **웹 전용 답변자**이고, §4 가 트레이를 지키는 이유이기도
하다(웹이 유일한 운영자 창구다).

**⑤ 1단계에서 peer 주소 질문은 사람만 답할 수 있다.** 즉 **헤드리스에서는
답할 주체가 없다** — 오늘도 그렇다. 2단계의 배달이 그 자리를 채운다(§12).

## 8. 실행 계획

**1단계 — 오늘 있는 사고 하나를 없애고, 아무것도 새로 만들지 않는다**
필드 4개 · `submit()` · arm→공개 · 슬롯 · `_answer_kind`(peer→work) · 종료
두 곳 · 트레이 라벨.
**얻는 것**: peer/main 의 무관한 메시지가 답으로 먹히던 사고 제거 — 그와
함께 그 일감이 `_handle_request` 를 안 거쳐 **라우팅째 유실되던 것**도 해소.
**새로 만드는 것**: 없음. 교착도, 모델 계약 변경도, 답변 경로 제거도 없다.

**2단계 — peer 배달 + main 거부**
L1(배달 에러) · L2(죽은 B 의 대기자 깨우기) · L3(순환 검사 `_cv` 안) ·
L4(`expects_reply=False` 만 답 + 배달 꼬리표) · `mode:"answer"` ·
`reject_count` · 프롬프트 3곳.

**3단계 (조건부)** — 타임아웃. §7-② 가드와 **함께**만.

### 테스트 계획 (1단계)

| 층 | 내용 |
|---|---|
| 자격 | 사람 → 답(주소 무관) / 주소가 main 일 때 main → 답 / **peer → 일감**(주소가 peer 여도) / 주소가 peer 일 때 main → 일감 |
| 손실 없음 | 일감 판정된 항목이 inbox 에 남아 `_handle_request` 를 정상 통과 · **peer 요청이면 `_deliver_peer_reply` 가 돈다**(오늘 유실되던 것) |
| 레이스 | **공개 전 도착**(가짜 러너 지연 0이라 기본 경로) · 동시 답변자 둘 · `clear()` 순서 |
| 종료 | `kill`/`shutdown_all` 즉시 깨움 · `join` 타임아웃 없음 · **기존 TC `test_shutdown_unblocks_pending_ask` 가 "no response" 그대로** |
| 부수효과 | 답도 🤝 창·`conversation.jsonl`·로스터에 남는다 · 답에도 `seq` 가 있다 |
| 호출자 보존 | `request()` 반환형이 `str` 그대로 — 기존 8개 호출자 무변경 |
| 트레이 | 모든 질문에 뜬다 · 주소 라벨이 맞다 |
| 비회귀 | 로스터 dot · 종료 경고 · `_SHUTDOWN` 재게시 삭제가 배치 경로(:1293) 무영향 · **웹 "main 과 선착순" 이 그대로 동작** |

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
| (회귀) arm 이 공개보다 뒤 | 즉답이 inbox 로 샌다 | §3.7 |
| (회귀) `return ""` | 창·로그·로스터를 건너뛴다 | §3.7 |
| (TC 파괴) 종료 wake | 빈 답 반환 | §3.7 |

### 3판 (2판 재리뷰 + 사용자 지적)

| 2판 | 확인 | 결과 |
|---|---|---|
| 사람은 ❓ 트레이에서만 본다 → `explicit` 요구 | **틀렸다.** 사람은 🤝 창에서도 본다. CLI 엔 트레이가 없다 | 사람 `explicit` 축 **삭제** |
| 1단계 = 계약 변경 없음 | `mode:"answer"` 가 없으니 main 이 항상 `explicit=False` → **1단계에서 main 이 답을 못 한다** | 1단계 규칙 명시 |
| 거부는 자기수정된다 | **틀렸다.** `prev_was_error` 가 탐지기를 리셋한다(§2-⑧) | `reject_count` 가드를 2단계에 |
| `request() -> tuple` | 호출자 8곳이 truthiness 로 판정 — 튜플은 항상 참 | `submit()` 신설 |
| **peer 는 답변자가 아니다** | **틀렸다.** 질문을 못 받아서였을 뿐 — 받으면 답할 수 있고, 배관이 이미 있다 | §3.3 배달 · §3.2 `awaiting_to` |
| 트레이는 그대로 | 내게 온 질문이 아닌데 답 어포던스가 있다 | §4 |

### 4판 (3판 재리뷰)

| 3판 | 확인 | 결과 |
|---|---|---|
| peer 배달을 1단계에 | **교착 셋**(L1 배달 에러 무시 · L2 죽은 B · L3 순환 검사가 락 밖) | **2단계로**(§3.3) |
| `author == awaiting_to` 한 줄 | `expects_reply` 를 안 봐서 B 의 `message` 가 답으로 먹힌다(L4) | 종류를 두 번째 축으로(§3.2) |
| §3.2 와 §3.5 | 주소가 main 이면 무조건 답 → **2단계 거부가 도달 불가**. 2판이 푼 P1 재발 | 단계별 분기 명시(§3.2) |
| 트레이는 주소가 사람일 때만 | 사람은 peer 가 아니라 **운영자** — 문서화된 "선착순" 기능이자 유일한 탈출구 | **철회**, 라벨만(§4) |
| `user` 는 CLI | 웹 main 채팅의 `@agt-…` 도 bare `user` | §2-④ |

### 교훈

1·2·3판이 틀린 자리는 전부 **"이 값 하나로 판정할 수 있다"** 고 적은 곳이었다
— `author` · `explicit` · `awaiting_to`. 3판에서 *"`awaiting_to` 는 발명이
아니라 원래 있던 주소"* 라고 변호했는데, **반만 맞았다**: 주소가 기존 값인 건
사실이지만 코드에는 **종류(`expects_reply`)라는 두 번째 기존 값**도 있었고
(`_is_human_direct` 가 이미 쓴다), 그걸 안 본 대가가 L4 였다. 정직한 문장은
"주소와 종류 **둘 다** 이미 있었고 판정은 둘을 따른다"이다.

더 큰 교훈은 따로 있다. 세 판을 되짚으면 **핵심(슬롯 + 생산자 분류)은 세 리뷰를
모두 통과했고, 깨진 것은 전부 내가 *추가*한 것**이었다 — 2판의 `explicit` 축,
3판의 peer 배달과 트레이 필터. 문제를 고치다 인접한 것까지 "이왕이면" 손대는
습관이 리뷰를 세 번 돌게 했다. 4판이 1단계를 **오늘의 사고 하나**로 좁힌 이유다.

## 12. 리뷰에 묻는 것 — peer 배달을 어떻게 살리나

1단계에서 뺐지만 **버리자는 뜻은 아니다.** 주소는 `agent:B` 인데 B 가 못 듣는
것은 그 자체로 결함이고(§1.1), 사람이 없는 헤드리스에서는 peer 주소 질문에
**답할 주체가 아무도 없다.** 2단계에서 살리려면 L1~L4 를 어떻게 닫아야 하나.

특히:

**① L2 가 일반적인 해법을 갖는가.** "죽을 때 나를 기다리는 대기자를 깨운다"는
`_worker` finally 에 넣으면 되지만, **inbox 에 줄 서 있다가 유실되는 질문**은
그것으로 안 잡힌다(B 가 꺼내기 전에 죽으면 B 는 그 질문의 존재를 모른다).
레지스트리가 `awaiting_to` 역인덱스를 들고 있어야 하나, 아니면 배달 자체를
**inbox 를 거치지 않는 경로**(예: B 의 슬롯에 직접)로 바꿔야 하나?

**② 배달을 요청으로 만드는 것이 맞나.** 지금 제안은 `message` 와 같은 경로라
B 에게 **새 일감**으로 보인다. 그래서 L4(프롬프트가 `message` 로 답하라고
가르침)가 생긴다. 질문을 일감이 아닌 **다른 종류**로 배달할 수 있나 —
`expects_reply` 처럼 이미 있는 축으로 표현 가능한가?

**③ 헤드리스에서 peer 주소 질문은 애초에 성립하나.** 사람도 없고 배달도
어렵다면, 그 조합에서는 `ask` 를 **등록 시점에 거부**하는 게 정직한가?
(3판에서 검토했다가 "기능 축소"라고 철회했는데, 배달의 비용을 보고 나니 다시
물을 가치가 있다.)

**④ 순환 검사의 상한.** `_cv` 안에서 체인을 따라가면 O(깊이)만큼 락을 쥔다.
에이전트 수가 많으면 문제인가, 아니면 `_MAX_PEER_HOPS=6` 이라 무시해도 되나?
