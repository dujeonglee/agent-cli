# 에이전트 `ask`/`answer` — 비동기 문답 설계 (2판)

> 상태: **설계 4판 · 구현 완료 (①②③④)**. 남은 것은 §6 의 수락 기준
> — 로컬 모델 실하네스 왕복 1회 + 바쁜 peer 1회.
> 블록 계열 1~5판은 `DESIGN-blocking.md` 에 기록으로.
> 1판(비동기)은 *"as written 은 구현 불가"* 판정(§10.2), 2판은 블로커 2건
> (§10.3). 3판은 *"B1·B2·G1~G3 반영되면 구현 착수 가능"* 을 반영한 것.

> **v9.20.0 보강 — `to: "user"`.** 주소 = 원 요청자라는 §0 은 main 이 시킨 일에서
> 에이전트가 **사람을 지목할 길이 없다**는 뜻이기도 했다. 실측(프로브 1790070684):
> "사용자한테 질문해 봐" 를 받은 에이전트의 `ask` 가 main 에게 갔고, 트레이는
> 비었고, main 은 사용자의 답을 지어냈다. 상주 `ask` 에 `to` (`requester` 기본 |
> `user`) 를 둔다 — `user` 면 주소가 `user` 가 되어 `to_human` 이다. §0 은 그대로다
> (답할 주체 = 주소 = `user*`, 답은 asker 에게). 스키마는 상주 전용
> (`parameter_overrides`), main 의 QUESTION 안내는 "대신 답하지 말라" 를 명시한다.

## 0. 불변식 하나

```
질문의 주소 = ask 시점의 tm.current_author = 원 요청자
답할 수 있는 주체 = 그 주소, 오직 그것
∴ 답한 주체 = 원 요청자                    (항상)
```

단 **`user*` 는 하나의 주체로 본다** — CLI 는 `author="user"`(`main.py:339`),
웹은 `author="user:{nickname}"`(`server.py:1266`), 뷰어가 여럿이면 닉이 다르다.
주소가 `user*` 인 질문은 `user*` 인 누구나 답할 수 있고, 배달은 `q.target` 으로
한다. 문자열 동치로 답변자를 검사하면 두 번째 뷰어의 트레이 답이 거부된다.

이 줄에서 설계가 거의 다 나온다. **아무것도 블록하지 않는다** — `ask` 는
질문을 등록하고 즉시 반환하고, `answer(id, text)` 가 id 로 짝지어 배달한다.
강제는 *"답할 것이 남으면 런이 끝나지 않는다"* 로 하되, **루프를 도는
주체에게만** 건다(사람의 루프는 우리 것이 아니다).

1판이 복잡했던 이유는 **"사람은 주소와 무관하게 답할 수 있다"** 는 규칙을
블록 설계에서 그대로 들고 왔기 때문이다. 그 규칙은 **교착을 구제할 탈출구**로
만든 것인데 비동기에는 교착이 없다 — 규칙을 걷어내니 라우팅 분기, `origin_*`
필드, 운영자 예외가 전부 사라졌다(§10.1).

## 1. 문제

`ask` 는 지금 상주 에이전트의 답을 **기다린다**. 다섯 판을 돌며 고친 것이
전부 "블록하되 무엇으로 깨우나"였고, 리뷰가 찾은 결함도 전부 블록의
부산물이었다:

| 블록 설계의 문제 | 비동기에서 |
|---|---|
| 상대 dead → 영원히 대기 · 도중 kill → 아무도 안 깨움 | 대기가 없다 |
| arm 을 공개보다 먼저 해야 하는 레이스 · 종료가 슬롯도 깨워야 함 | 슬롯이 없다 |
| 막힌 슬롯 뒤의 큐가 펌프를 붙잡음 · A↔B 상호 대기 | 막힌 것이 없다 |
| "도착 순서가 답" — 주소·종류로 판정해야 함 | **id 로 짝짓는다** |
| peer 에게 배달할 채널이 없음 · 헤드리스에서 답할 주체 없음 | 기존 배관으로 배달 |

## 2. 조사 — 기존 구조가 맞춰져 있다

**① 상주 에이전트는 inbox 항목 1개 = 런 1개다**(`agents_live.py:12`,
`_handle_request:1416` → `run_subagent_message`). ctx 는 영속이라 런이 끝나도
대화가 이어진다.

**② `_handle_request` 가 이미 원 요청자에게 라우팅한다**(`:1475-1505`):
`expects_reply=False` → 아무 데도 · `agent:*` → `_deliver_peer_reply` ·
`main` → `_push_reply` · `user:*` → 창만. **답을 `author=주소`,
`expects_reply=True` 로 보내면 결과가 저절로 제자리로 간다** — 새 필드 0.

**③ 질문 배달 배관이 이미 있다.** peer 는 `_make_message_handler:930` 이 쓰는
`submit(...)`, main 은 `_push_reply(kind:"question")`(`:1628`) → `MailWaker`
(`:1734`)가 idle main 을 깨운다.

**④ 도구 마운트가 선언 하나로 된다** — `handler_resources`(`core.py:150-158`)
가 `Tool.requires_handler` 로 붙이고 뗀다. `MessageTool` 이
`requires_handler="message_handler"` + `force_mount=True`(`virtual.py:85-86`)
로 상주 전용이 되는 그 방식.

**⑤ 터미널 op 를 "계속"으로 돌릴 수 있다** — `_op_ask`·`_op_message` 가 관찰을
붙이고 `_CONTINUE` 를 돌려준다(`dispatch.py:697·740`).

**⑥ per-loop 설명 교체 선례가 있다** — `description_overrides` 로
`AgentTool.SUBLOOP_DESCRIPTION` 을 갈아끼운다(`system_prompt.py:640-648`).

**⑦ 레지스트리는 서브루프에 넘기면 안 된다** — `state.py:59-62` 가
*"teammate 안 teammate 금지의 단일 가드"*. 새 훅은 callable/객체 seam 으로.

**⑧ CLI 에는 사람이 답할 루프가 없다.** 명령이 `run`/`web` 등뿐이고
`@agt-<key>` 는 `run` 의 인자다(`main.py:339`).

## 3. 설계

### 3.1 두 도구

```python
ask(question)        # → {"id": "q-7a3f"} 즉시 반환. 주소는 하네스가 정한다
answer(id, text)     # → 짝지어 배달, 목록에서 제거
```

`ask` 의 반환 관찰:

```
Observation: question q-7a3f sent to <주소>. You are NOT blocked — continue with
whatever does not depend on the answer. The answer will arrive as a new message.
If nothing else can proceed, `complete` — you will be resumed when it arrives.
```

**마지막 문장이 중요하다.** `ask` 는 보통 "막혔다"는 뜻이라 "계속하라"고만 하면
작은 모델이 추측하고 끝낼 수 있다. `complete` 해도 이어진다는 걸 알린다.

**설명은 루프마다 다르다**(§2-⑥). main·delegate 는 블록하는 기존 `ask`
(`renderer.prompt_user`), 상주 에이전트는 비블로킹 —
`AskTool.RESIDENT_DESCRIPTION` 을 `description_overrides` 로 갈아끼운다.
`_ASK_INLINE`(`system_prompt.py:547-575`)도 같은 분기가 필요하다.

### 3.2 질문 목록 — 런 스코프가 핵심

```python
@dataclass
class Question:
    id: str                          # q-<hex6>
    asker: str                       # "agt-x9"
    target: str                      # "main" | "agent:agt-b1" | "user:bob"
    text: str
    asked_at: float
    asked_seq: int                   # asker 가 이 질문을 건 런의 inbox seq
    delivered_seq: int | None = None # target 이 이 질문을 **꺼낸** 런의 seq
    nags: int = 0
```

`AgentRegistry._questions: dict[str, Question]`, **모든 접근은 `_cv` 아래**
(`:394`) — `answer_question` 의 pop, `pending()` 스냅샷, `nags += 1`, 두 seq 의
기록. `submit` 이 이미 `_cv` 아래서 claim 을 원자화하는 그 요구와 같다.

**두 seq 가 왜 필요한가.** 워커는 **inbox 항목 1개 = 런 1개**이고 항목엔 이미
`seq` 가 실려 있다(`:756`). 질문은 `_questions` 에 즉시 등록되지만 상대 inbox
에서는 **줄을 선다**. 런 스코프가 없으면:

- **아직 안 꺼낸 질문에 강제가 걸린다.** B 가 seq 5 를 처리 중일 때 A 의 질문이
  seq 6 으로 쌓이면, seq 5 런의 `complete` 이 본 적도 없는 질문으로 nag 를
  받는다. 곧 seq 6 런이 같은 질문을 다시 돌리고 `answer` 는 "이미 답함" 에러.
- **더 나쁜 건 sweep 이다.** seq 5 런이 `max_turns` 로 끝나면 sweep 이 seq 6 의
  질문을 *"(답변 없음)"* 으로 닫고 A 에게 배달한다 — B 는 읽지도 않았는데
  거짓 무응답이고, seq 6 런의 `answer` 는 unknown id 다. **바쁜 peer 에게
  묻기**는 팀에서 가장 흔한 경우라 이건 상시 오동작이다.
- **§3.7 회신 억제도 번진다.** A 가 run 3 에서 사람에게 물어 둔(상한 없음,
  영영 열림) 질문이 있으면 run 5·7·9 의 회신이 전부 막힌다.

그래서 **강제·sweep 은 `delivered_seq`, 회신 억제는 `asked_seq`** 를 본다
(§3.4 · §3.7). §10.2 가 "새 필드 0" 이라 한 것은 **라우팅**에 관한 것이고
(`origin_*` 4필드가 불필요하다는 뜻) 그건 그대로 맞다 — 런 스코프는 별개의
축이고 필드 둘이 든다.

**중복 방지**: 같은 asker→target 에 같은 텍스트가 열려 있으면 기존 id 를
돌려준다. `_op_ask` 는 루프 탐지기 앞에서 반환하므로(`dispatch.py:649` vs
`:832`) 반복 질문이 탐지되지 않는다.

**목록인 이유**: 비동기라 한 에이전트가 여러 질문을 동시에 걸 수 있다(블록
설계의 "카디널리티 1" 논거는 성립하지 않는다).

### 3.3 배달 — 기존 배관 + `question_id` 한 키

```python
# 질문 (ask 시점)
target == "main"      → _push_reply({"kind":"question", "id": q.id, ...})
target == "agent:B"   → submit(B, f"[question {q.id} from {asker}]: {text}",
                               author=f"agent:{asker}", expects_reply=False,
                               question_id=q.id)          # ← kwarg 하나
target == "user:*"    → 배달 없음. ❓ 트레이가 표면이다 (§3.6)

# 답 (answer 시점)
submit(q.asker, f"[answer to your question: {q.text}]\n{text}",
       author=q.target,        # ← 주소 = 원 요청자. origin_* 가 필요 없다
       expects_reply=True)     # ← 그 런의 결과가 원 요청자에게 간다 (§2-②)
```

`submit()` 에 `question_id` kwarg 를 하나 더한다 — 항목 dict(`:756-765`)에 키
하나. **접두사 `[question q-xxx …]` 를 파싱해 알아내는 것은 금지**(표시 문자열이
계약이 되면 문구를 못 고친다).

**배달 마커는 꺼낼 때 찍는다**: `_handle_request` 가 항목을 꺼내면
`q.delivered_seq = item["seq"]`, main 은 `drain_replies` 가 질문 레코드를 넘길
때. `_push_reply` 의 질문 레코드와 `build_reply_record` 에 **`id` 가 실려야**
main 이 `answer(id)` 를 할 수 있다.

**idle 상대도 깨어난다** — inbox 항목이 곧 런이다. 그래서 §3.4 의 강제는 배달
수단이 아니라 **"꺼내 읽고도 안 답함" 백스톱**이다.

peer 질문을 `expects_reply=False` 로 보내는 이유: B 가 그 질문 항목을 처리한
산출물이 A 에게 되돌아가면 안 된다. B 는 **`answer` 도구로** 답한다.

`port.ask` 의 대상이 dead/unknown 이면 `submit` 이 에러를 돌려준다
(`:727-732`) — 그 경우 **`_questions` 에 등록하지 않고** 에러를 관찰로 준다.

### 3.4 독촉 — 런을 붙잡지 않고, 새 런으로 다시 묻는다

**`complete` 을 가로막지 않는다.** 결과는 그대로 나가고, 남은 빚은
**새 inbox 항목 = 새 런**으로 다시 온다.

2판까지는 `_op_complete` 에서 `_CONTINUE` 를 돌려 런을 붙잡으려 했다.
두 가지가 틀렸다:

1. **비동기 전제를 스스로 깬다.** B 가 main 의 질문에 아직 안 답했다는
   이유로 B 의 런을 안 끝내면, B 에게 일을 시킨 쪽은 *자기와 무관한 질문*이
   풀릴 때까지 결과를 못 받는다 — 없애려던 결합이 그대로 돌아온다.
2. **증상을 옆에서 때우게 된다.** 런을 더 돌리면 output 이 마지막
   `complete` 것이 되므로 첫 결과를 stash 해 이어 붙여야 했는데, 그 stash 는
   게이트가 만든 문제를 게이트 옆에서 메우는 코드였다.

독촉을 항목으로 보내면 **답이 오는 경로와 정확히 같은 기계**를 쓴다
(§3.3 — inbox 항목 1개 = 런 1개, main 은 메일박스+MailWaker). 그래서
`dispatch.py` 는 **한 줄도 바뀌지 않는다**. 2판 리뷰의 블로커 B2(다중 op
에서 flush 앞/뒤 순서), echo-as-final 우회, nag 턴 미계수,
`[complete, answer]` 유실 — 전부 "게이트가 턴 루프 안에 있다"에서만 나오던
문제라 통째로 사라진다.

독촉은 **런이 끝났다는 사실**에 붙으므로 핸들러가 아니라 **워커 루프에서
세 갈래가 수렴하는 한 곳**에 건다 — 핸들러마다 두었더니 실제로 배치 경로
(`_handle_human_batch`)에서 빠졌다. main 은 자기 런이 끝나는 자리(펌프)에서
같은 함수를 부른다.

```python
# _worker — 단건/배치/비-사람 세 갈래 뒤
self.remind_owed(f"agent:{tm.key}")

# main.py — run_loop 가 돌아온 뒤 (run·web 두 펌프)
agent_registry.remind_owed("main")
```

`remind_owed(addr)` 는 주소 어휘(`"main"` | `"agent:<key>"`)를 그대로 받고,
**계산·상한·닫기가 완전히 같다.** 다른 것은 배달 한 줄뿐이다 — 에이전트는
inbox 항목, main 은 메일박스 `kind:"reminder"`(→ `source:"agent_reminder"`
관찰). 질문 배달(§3.3)과 같은 비대칭이다.

**스코프 축은 배달 여부**(`delivered_seq is not None`)**이지 seq 동치가
아니다.** 아직 큐에 서 있는 질문은 제외되지만(그 런이 읽지도 않은 것으로
독촉하면 안 된다 — B1), 한 번 읽은 빚은 **답할 때까지 매 런 끝에** 다시
온다. seq 동치로 좁히면 독촉 런의 seq 가 달라 두 번째 독촉이 영영 안 나가고
상한조차 안 걸린다(구현 중 실측).

**main 의 배달 시점은 `drain_replies`** 다 — 에이전트가 inbox 항목을 꺼낼
때 하는 일과 같은 자리. 안 찍으면 `questions_owed_by("main")` 이 영영 비어
main 은 독촉도 상한도 못 받고, §3.7 로 보류된 회신이 영구 정지한다.

**독촉 런 끝에서도 독촉한다.** 한때 `item["reminder"]` 로 연쇄를 막았으나
그 차단이 만든 **정지**가 훨씬 나빴다: 독촉 1회 뒤 그 에이전트에게 일이
안 오면 `nags` 가 1에 멈춰 상한이 영영 안 걸리고 질문이 영원히 열린다.
차단의 근거였던 "수 밀리초에 6회"는 **가짜 러너가 즉시 반환하기 때문**이고,
실제로는 독촉 런 하나가 질문을 컨텍스트에 놓고 도는 진짜 LLM 턴이다.
게다가 inbox 는 FIFO 라 독촉이 큐 뒤에 붙어 실제 일감을 굶기지 않는다.
연쇄는 낭비지 버그가 아니고, 상한이 반드시 끝을 낸다.

**스냅샷과 bump 사이에 답이 들어올 수 있다.** `bump_question_nag` 가 0을
돌려주면(=그런 질문 없음) 건너뛴다 — 살아 있는 질문의 nags 는 언제나 ≥1
이라 0은 모호하지 않다. 안 걸러내면 `0 > 6` 이 거짓이라 **이미 답한 질문
으로 독촉**한다.

독촉 항목은 `expects_reply=False`(산출물이 어디로도 가면 안 된다)이고
발신자는 **기다리는 쪽**(asker)이다 — `target` 은 그 런의 주인 자신이라
창에서 "자기가 자기에게"로 읽힌다.

### 3.5 상한

`nags`(= 받은 독촉 횟수) 초과 → *"(답변 없음 — 반복 독촉에도 무응답)"* 으로
닫고 asker 에게 배달. **main 에도 똑같이 적용된다** — 같은 함수다.
**사람 주소에는 적용하지 않는다**(§3.6) — 배달이 없으니 `delivered_seq` 가
안 찍혀 독촉 대상에서 자연히 빠진다.

상한은 모듈 상수 `_MAX_QUESTION_NAGS`(현재 6) 하나다. 이게 유일한 비용이라
— 끝내 답 않는 질문 하나당 LLM 런 N회 — 실사용을 보고 조정한다.

### 3.6 사람에게 묻는 경우 — 강제가 아니라 알림

**루프를 붙잡는 강제는 루프를 도는 주체에게만 걸 수 있다.** 사람의 루프는 우리
것이 아니고, CLI 엔 답할 자리조차 없다(§2-⑧).

- **독촉 대상이 아니다** — `delivered_seq` 가 안 찍힌다(배달이 없으므로).
- **❓ 트레이가 표면이다** — `_questions` 중 `target.startswith("user")` 인 것.
  런이 끝났든 도는 중이든 뜬다. 지금처럼 `waiting_ask` state 를 보지 않는다.
- **`user*` 인 누구나 답한다**(§0) — 답변자 라벨을 `q.target` 과 문자열 비교
  하지 않는다.
- **`complete` 결과에 미답 질문이 실린다**:

```
Repo 정리 완료. src/ 를 3개 모듈로 나눴습니다.

⏳ 답을 받지 못한 질문 1건 — 답하면 이어서 진행합니다:
   [q-4c1b] "이 설정 파일을 덮어써도 될까요?"
```

**이 문구는 `_handle_request` 가 붙인다**(`dispatch.py` 가 아니라). dispatch 에
두면 `serialize_terminal_for_history`(`:640`)를 타고 **모델 자신이 쓴 최종답**
으로 ctx 에 남아, 다음 런에서 모델이 하네스 문구를 모방한다.

사람이 트레이에 답하면 `answer_question` → §3.3 의 답 배달 → **새 런**.
상한도 만료도 없다.

### 3.7 질문을 건 런은 회신을 밀지 않는다

판정은 **"이 런이 질문을 걸었나"**(`tm.asked_this_run`)다. *"런이 끝나는
시점에 질문이 아직 열려 있나"* 가 **아니다** — 비동기라 답은 보통 묻던 런이
끝나기 **전에** 도착하고, 그러면 그 조건은 안 걸려 부분 회신이 그대로 나간다.
막으려던 상황이 오히려 정상 경로다.

**왜 부분 회신이 해로운가.** 그것은 *답이 존재하기 전에* 만들어졌는데
*답을 보낸 뒤에* 도착한다. 요청자는 내용의 시점과 도착 순서가 어긋난 것을
구분할 수 없어 "내 답이 반영된 최신 상태"로 읽는다. peer 요청자면
**inbox 항목 1개 = 런 1개**라 그 오독이 곧 실행이 된다 — orchestrator 가
"작성 완료"로 보고 reviewer 를 부르면, reviewer 는 TODO 가 박힌 옛 정책
코드를 리뷰하고 그 코멘트가 사람에게 남는다.

**보장하는 성질은 "정확히 한 번" 이 아니라 "낡지 않음" 이다.** 한 런이
질문을 둘 걸면 답 런도 둘, 회신도 둘이다 — 다만 각각 자기 답이 반영된
최신 상태다. 한 번으로 줄이려면 마지막 답 런만 보고하게 추적해야 하는데,
그 복잡도가 주는 것은 "신선한 보고 N개 → 1개"뿐이다.

**그래서 중복 접기는 런 안으로 한정한다**(§3.2). 런 경계를 넘어 접으면
요청 둘이 답 런 하나를 공유해 **회신이 하나 사라진다** — 낡은 회신보다 나쁜
누락이다. 접기의 원래 목적(`_op_ask` 가 루프 탐지기 앞에서 반환하는 것의
보완)은 런 내부 현상이므로 한정해도 그대로 달성된다.

**등록에 실패하면 억제하지 않는다.** 답 런이 안 생기므로 억제하면 그 런의
회신이 영영 사라진다. `asked_this_run` 은 배달 성공 뒤에만 세운다.

**억제되는 것은 재주입 한 줄뿐이다.** 하네스는 아무것도 기다리지 않고
`complete` 도 그대로 실행된다. 한 런의 결과가 가는 네 곳 중:

| | | 질문을 건 런 |
|---|---|---|
| `agent_message` | 🤝 대화창 | 그대로 |
| `conversation.jsonl` | resume 재생 소스 | 그대로 |
| `reply-<seq>.md` | 회신 전문 영속 | 그대로 |
| `_deliver_peer_reply` / `_push_reply` | 요청자를 깨운다 | **건너뜀** |

요청자는 깜깜하지 않다 — 이미 질문을 받았고, 그것이 상대가 자기를 기다린다는
신호다. 답이 끝내 안 와도 상한이 질문을 닫고 그 닫힘 역시 `_deliver_answer`
를 지나 답 런을 만든다. 답·상한·상대 사망 **셋 다** 답 런을 만들므로 억제가
영구 보류가 되지 않는다.

### 3.8 사망 — `revivable` 아래에서만

`_worker` 의 `finally`(`:1385`)가 레지스트리가 사망을 관찰하는 자리다. **양방향
정리**:

- 그 에이전트 **앞으로 온** 질문 → *"(종료됨)"* 으로 닫고 asker 에게 배달
- 그 에이전트가 **건** 질문 → 폐기. main 메일박스엔 이미 질문이 있을 수 있어
  main 의 `answer` 가 unknown id 를 받는다 — 에러 문구가 *"asker died"* 를
  말하게 한다.

**단 `not tm.revivable` 아래에서만.** `finally` 는 `shutdown_all`(`:1024`)
에서도 돌고, 거기서 지우면 직후의 `_save_state()`(`:1403`·`:1034`)가 빈 목록을
저장해 **§3.9 의 resume 알림이 항상 0건**이 된다.

`revivable` 이 정확히 맞는 축이다 — `kill`(`:1007`)과 crash(`:1390`)만 False 로
내리고 `shutdown_all` 은 *"revivable 유지 상태로 기록돼 resume 이 되살린다"*
(`:1033` 주석)며 건드리지 않는다. `died` push 의 가드(`crash and not
stop_event.is_set()`)를 쓰면 **kill 이 빠져** 죽은 상대를 향한 질문이 영영
열린다.

### 3.9 영속 — resume 은 **되살린다**

`_questions` 는 `agents.json` 의 `questions` 배열로 나간다
(`_save_state:1153` 옆). resume 은 **되살린다** — `restore` 가 ctx 를 통째로
복원하므로(`tm.revive = True`, 이전 문답 전부 기억) 나중에 도착한 답도
평소처럼 새 런으로 처리된다. 못 할 기술적 이유가 없다.

되살릴 때 손볼 것 넷:

**① 살릴 수 없는 것은 버린다.** asker 나 target 이 안 돌아왔으면(kill
툼스톤·매니페스트 부재) 아무도 답할 수 없거나 답을 받을 데가 없다. 버린
수가 `stale_questions` 이고, 부팅 시 사람에게 알린다 — 정상 경로에서는
`kill`/crash 가 즉시 양방향 정리를 하므로(§3.8) 대개 0이다.

**② 미배달 peer 질문은 다시 배달한다** (창·로그에는 다시 그리지 않는다 —
그 질문은 첫 세션에서 이미 그려졌고 `_replay_conversation` 이 방금 재생했다). inbox 는 `SimpleQueue` 라 영속
대상이 **아니다** — 아직 안 꺼낸 질문은 항목으로만 존재했으므로 통째로
증발했고, 목록만 되살리면 영영 안 꺼내지고 독촉도 안 간다. **배달된 것은
재배달하지 않는다**: 상대 ctx 에 이미 남아 있어 두 번 묻는 꼴이 된다.
main 앞 질문은 `pending` 미러로 살아 있으므로 역시 그대로 둔다.

**③ 배달됐던 빚은 한 번 깨운다(kick).** 독촉은 *런이 끝나는 자리*에
걸리는데, resume 직후 그 에이전트에게 새 일감이 안 오면 **끝나는 런이 없어**
독촉도 상한도 영영 안 돈다 — 연쇄 차단이 만들었던 정지가 resume 경로로
다시 들어온다. 복원 직후 `remind_owed` 를 한 번 걸면 그 독촉 항목이 런을
만들고 이후는 평소 흐름이다. main 앞 빚도 대상이다(`agent:` 로 한정하면
main 의 빚은 영영 잠든다 — main 의 독촉은 런 끝에서만 걸리는데, main 이 그
질문을 모르면 끝날 런도 없다).

**②와 ③은 겹치면 안 된다.** kick 대상은 **재배달 전에** 확정한다: ②가 큐에
넣은 질문을 상대 워커가 곧바로 꺼내 `delivered_seq` 를 찍으면(LLM 호출
**전에** 찍힌다) ③의 집합에 섞여, 방금 배달한 질문에 독촉까지 날아간다 —
런 하나 낭비 + 상한 조기 소모. 재현률 5/5 였다.

**④ seq 둘의 취급이 다르다.** `asked_seq` 는 0 으로 리셋한다 — 세션마다
의미가 다른 값이고, 물어본 런은 사라졌으며, seq 는 1부터라 0 은 어떤 런과도
안 겹쳐 §3.7 오탐이 구조적으로 불가능하다. `delivered_seq` 는 **유지**한다 —
판정 축이 `is not None` 이라 "배달됐던 빚"이 이어져야 ②와 ③이 갈린다.

전부 `_notify_roster` **앞**에서 끝나야 트레이 첫 스냅샷이 맞다.
`AGENTS_STATE_VERSION` 은 그대로 — 구버전 파일엔 `questions` 가 없어
되살릴 것이 없고, 구버전 리더는 모르는 키를 무시한다.

**main 에게 가는 질문 레코드는 `answer` 도구를 가리킨다.** `id` 가 실린
레코드(비동기)에 *"BLOCKED … mode:request 로 답하라"* 를 주면, main 이
보내는 것은 답이 아니라 **일감**이라 질문은 열린 채 남고 독촉이 상한까지
돌다 닫힌다 — 런만 태운다. `id` 유무로 문구를 가른다.

**`stale` 마킹은 블로킹 경로에만 남긴다.** `build_reply_record` 의
*"STALE … NO LONGER waiting"*(`:250`)는 재시작으로 대기 슬롯이 사라지는
블로킹 `ask` 에 대해서는 **참**이다. 비동기 질문(레코드에 `id` 가 실린다)은
목록째 되살아나 답이 정상 처리되므로 마킹하면 거짓말이 된다 — `id` 유무로
가른다. ④에서 블로킹 경로가 사라지면 이 분기도 함께 간다.
*"BLOCKED until answered"*(`:256`)는 flip 커밋에서 고친다.

## 4. `QuestionPort` — seam 하나

콜러블 셋을 `LoopConfig`·`AgentLoop.__init__`·`run_loop`·
`run_subagent_message`·`_run_message` 다섯 곳에 각각 꿰면 15군데다. 객체 하나로:

```python
class QuestionPort:            # 레지스트리가 에이전트별로 만든다
    def ask(self, text) -> tuple[str, str]: ...   # (id, err)
    def answer(self, id, text) -> str: ...        # err
```

**표면은 둘뿐이다.** 독촉은 하네스가 런 경계에서 걸므로(§3.4) 루프가
미답 목록을 조회할 일이 없다 — 조회 메서드를 두면 호출자 없는 표면이 된다.

**main 의 `ask` 는 거부한다.** main 이 사람에게 묻는 것은 기존 블로킹
경로(`renderer.prompt_user`)다. 포트로 오면 주소가 `user` 인 질문이
생기는데, 로스터에도 창에도 안 뜨고(둘 다 asker 를 에이전트 키로 찾는다)
`open_human_questions()` 에만 남아 `any_activity()` 를 영구 True 로 만든다
— **보이지도, 답할 수도, 사라지지도 않는 질문**이다. §3.9 가 질문을
되살리면 asker(main)도 target(user)도 "항상 살아있음"이라 매 세션 부활한다.

`LoopConfig.questions` 에 담고 `AnswerTool.requires_handler = "questions"` +
`force_mount = True` — `MessageTool` 과 같은 선언(§2-④). **레지스트리가 아니라
포트**라 `state.py:59-62` 의 단일 가드는 그대로다.

**main 도 같은 포트를 받는다** — `registry.question_port(None)`(주소 라벨
`"main"`). 이게 없으면 `_answer_kind` 제거 후 **main 에 답변 수단이 없다**.
그 결과 §3.4 의 강제가 main 에도 걸린다 — 의도다(main 도 빚을 지면 답해야
한다). sweep 만 없다(§3.4 끝).

### 4.1 블로킹/비블로킹 `ask` 의 판별 신호

main 도 포트를 받으므로 **`questions is not None` 으로는 못 가른다.** 지금
상주 전용 seam 은 `cfg.ask_handler`(`dispatch.py:672`, `_run_message:1679`)
하나뿐이다. → **③에서 `ask_handler` 를 `port.ask`(즉시 id 반환)로 바꾸고**
`_op_ask` 는 *"`ask_handler` 가 있으면 비블록"* 으로 둔다.
`_make_ask_handler` 의 블록 본체는 ④에서 삭제.

**같은 신호가 시스템 프롬프트에도 필요하다.** `_build_tools_section` 은
`has_agent_registry` 만 받고(`system_prompt.py:630-648`), `loop/prompt.py:35-47`
→ `build_system_prompt_sections(:777)` 경로로 `ask_handler` 유무가 전달되지
않는다. `nonblocking_ask: bool` 을 그 경로에 꿴다 — `description_overrides`
선택과 `_ASK_INLINE` 선택(`:616-620`)이 같은 인자를 쓴다.

## 5. 영향 받는 표면

| 곳 | 변경 |
|---|---|
| `agents_live.py` | `Question`(+`asked_seq`/`delivered_seq`)·`_questions`(+`_cv`)·`QuestionPort`·질문 배달·`delivered_seq` 마킹·사망 양방향 정리(`revivable` 가드)·`_handle_request` sweep·사람 알림 문구·§3.7 회신 억제 |
| `agents_live.py:745-765` | `submit(..., question_id=None)` — 항목 dict 에 키 하나 |
| `agents_live.py:1153` | `agents.json` 에 열린 질문 |
| `agents_live.py:454` | **`roster_snapshot()`** 에 `open_questions`(`:385` `snapshot` 은 `AgentInstance` 메서드라 `_questions`/`_cv` 에 못 닿는다) |
| `agents_live.py:250,256` | `build_reply_record` 질문 문구 둘 + 레코드에 `id` |
| `tools/virtual.py` | `AnswerTool`(`requires_handler="questions"`, `force_mount`) · `AskTool.RESIDENT_DESCRIPTION` |
| `loop/state.py` | `LoopConfig.questions` |
| `loop/core.py:150` | `handler_resources` 에 `"questions"` · main 포트 조립 |
| `loop/dispatch.py` | `_op_answer`(`_op_message:701` 동형) · `_op_ask` 비블로킹화. **게이트·stash 없음** — 독촉이 항목이라 종료 경로를 안 건드린다(§3.4) |
| `loop/prompt.py:35-47` | `nonblocking_ask` 전달 |
| `prompts/system_prompt.py:630,777` | `build_system_prompt_sections`/`_build_tools_section` 시그니처 · `ask` 설명 override · `_ASK_INLINE` 분기(`:616-620`) |
| `subagent/runner.py:189` | 포트 전달 |
| `web/server.py:1248` | `agent_input` 이 `answer_id` 수용 → `answer_question` |
| `web/static/app.js:2110` | 트레이를 `open_questions` 기반으로(질문별 항목) |
| `web/static/app.js:2284` | 상태 dot 의 `"w"` 분기 제거 — 비동기엔 그 state 가 없다 |
| `runtime.py:144-152` | `waiting_ask_keys` → `open_human_question_keys`, **그리고 "resume 시 STALE 처리됩니다" 문구를 §3.9 의미로** |
| `runtime.py` | `main_run_ended(registry)` — main 의 런 끝 독촉. 펌프가 둘(run/web)이라 호출부는 둘이되 **정의는 하나** |
| `main.py` | `_agent_mail_notice` 가 `reminder`/`answer` 를 구분 — 독촉을 "회신 도착" 으로 적으면 뭔가 끝난 줄 안다 |
| `agents_live.py:482` | `any_activity` 에 열린 사람 질문 합류 — 안 그러면 idle-reap 이 세션을 걷고 §3.9 가 안 살리니 묘비명이 된다 |
| `render/base.py:686`·`render/web.py:2040` | `can_answer_agent` 제거(게이트는 `agents_live.py:1593` 하나) |

**1단계(`203fa94`)에서 제거될 것**: 슬롯 4필드 · `answered` Event ·
`_answer_kind`/verdict · `can_answer_agent` · `has_active_work` 의
`waiting_ask` 분기(`:523`) · `waiting_ask_keys`(`:528`).
`state_is_active`(`:474`)는 **개명 대상이 아니다** — `waiting_ask` 전용이 아니라
*not idle/dead* 라는 일반 술어이고, 그 값이 더 안 나올 뿐이다.
주석 정리 대상: `_answer_kind` docstring(`:95-118`) · `agent_input`
docstring(`server.py:1249-1252`) · `main.py:1616` · `app.js:1934-2010`.
테스트 영향: 슬롯 어휘 grep 기준 **5파일 43군데**(`test_agents_live` 31).

**남는 것**: `submit()` 자체는 유지(답·질문 배달이 쓴다).

## 6. 실행 순서 — flip 은 마지막에서 두 번째, 원자적으로

1판의 1→2→3→4 는 2와 3 사이에 **main 이 깨진다**(ask 는 즉시 반환하는데 독촉도
트레이도 없고 프롬프트는 "BLOCKED"라고 거짓말한다).

| 순서 | 내용 | 끝났을 때 |
|---|---|---|
| **① 코어** | `Question`(두 seq)·`_questions`·포트·`submit` kwarg·사망 정리·영속 | 호출자 0. 동작 불변 |
| **② 받을 준비** | 독촉(`remind_owed`)·사람 알림·§3.7 억제·`roster_snapshot`·`agent_input`(`answer_id`)·트레이·`any_activity`·main 포트 | `_questions` 가 비어 **전부 no-op**. 동작 불변 |
| **③ flip** | `AnswerTool`·`_op_answer`·`_op_ask` 비블로킹(`port.nonblocking`)·`nonblocking_ask` 프롬프트 배선·상주에 `ask_handler` 미전달·**1단계 TC 재작성** — **한 커밋** ✅ | 동작이 바뀌는 유일한 단계 |
| **④ 정리** | 블록 잔재·주석·`can_answer_agent` 제거 · `submit()`→`request()` 되접기 ✅ | 동작 불변 |

③ 뒤에 남는 `_answer_kind`(`:751`)·`kill` 의 `answered.set()`·`has_active_work`
의 `waiting_ask` 분기는 **`awaiting` 이 다시는 세워지지 않아 전부 dead branch**
라 ③과 ④ 사이에서 무해하다 — 그래서 ③이 원자적이다.

### 수락 기준

**실하네스 왕복 1회.** 손으로 쓴 프롬프트 프로브는 증명력이 약하다(§7) —
`run_subagent_message` 를 실제로 태워 **A 가 묻고 → A 가 계속하고 → B 가
답하고 → A 의 답 런 결과가 원 요청자에게 도달**하는 것을 로컬 모델로 한 번
확인한다. **바쁜 B** 로도 한 번(§3.2 의 런 스코프가 실제로 작동하는지).

### 테스트 계획

| 층 | 내용 |
|---|---|
| 비블로킹 | `ask` 즉시 반환 · 여러 질문 동시 · 같은 질문 재발 시 id 재사용 · dead 대상이면 미등록+에러 |
| 배달(질문) | peer inbox 에 `question_id` 와 함께 · **idle peer 가 깨어난다** · main 은 메일박스+MailWaker, 레코드에 `id` |
| 런 스코프 | **바쁜 peer**: 큐 뒤의 질문은 앞 런의 nag·sweep 에 안 걸린다 ← B1 · 사람 질문이 열려 있어도 다른 런 회신은 안 막힌다 ← G3 |
| 배달(답) | `author=q.target`·`expects_reply=True` · **답 런의 결과가 원 요청자에게** |
| 짝짓기 | 없는/이미 답한 id 거부 · `_cv` 아래 원자적 claim |
| 독촉 | 결과는 즉시 나가고 독촉이 뒤따른다 · **독촉 런 뒤엔 독촉 없음**(연쇄 방지) · 답하면 그친다 · 상한 초과면 닫고 asker 에게 알림 · 독촉은 회신을 만들지 않는다 |
| 사람 | 트레이가 `target=user*` 만 · `complete` 을 막지 않음 · 결과에 실림 · **두 번째 뷰어(다른 닉)도 답할 수 있다** ← G5 · 상한 미적용 · `any_activity` 가 센다 |
| §3.7 | 그 런에서 건 질문을 진 채 끝나면 회신 억제, 창/로그/persist 는 그대로 |
| 사망 | 양방향 정리 · **`shutdown_all` 은 지우지 않는다**(resume N>0) ← G1 |
| 실모델 | 왕복 1회 + 바쁜 peer 1회 (수락 기준) |
| 비회귀 | main·delegate 의 `ask` 는 블록 그대로 · 잔재 제거 후 기존 TC |

## 7. 1판 프로브가 증명하지 못한 것

로컬 35B 로 3/3(평균 1.7턴)을 얻었지만 **답하는 쪽 한 경우**만 쟀다:

- **asker 쪽 0회** — "답 없이 계속한다"가 실제로 되는지
- **재개 0회** — 답이 왔을 때 하던 일을 잇는지. **설계 전체가 걸린 동작**
- 손으로 쓴 시스템 프롬프트 · 가짜 shell 출력 · 질문 하나 · n=3
  (3/3 의 한쪽 95% 하한은 0.29 — "전혀 안 된다"만 배제한다)
- `answer` 가 나온 뒤(배달·라우팅·트레이)는 전혀 안 탔다

→ §6 의 수락 기준이 이걸 대체한다.

## 8. 알고 두는 것

**①** `ask` 의 의미가 바뀐다 — "막혔으니 기다린다"에서 "물어두고 계속한다"로.
**②** 되묻기(명확화)가 가능해진다 — 블록 설계에선 순환으로 거부됐다.
**③** main 도 강제 대상이다(§4) — 다만 sweep 이 없어 다음 런 nag 가 백스톱.
**④** main·delegate 의 `ask`(사람에게 묻기)는 무변경.
**⑤** CLI 에서 사람 주소 질문의 알림은 **묘비명**이다 — 답할 자리가 없다(§2-⑧).
**⑥** 답은 `submit()` 의 기본 `hop=0` 으로 들어가므로 `_MAX_PEER_HOPS`
가드(`_deliver_peer_reply:830`)가 **질문 왕복마다 초기화**된다. 상한이 어차피
사실상 도달 불가라 실해는 없지만, 핑퐁 억제를 그 가드에 기대지 않는다는 뜻이다.
**⑦** main 의 `answer` 는 `submit(author="main")` 이라 `_current_run_authors` 를
**답 시점** 런에서 스냅샷한다(`:760-765`) — 멀티유저면 원 요청의 `answers` 와
다를 수 있다. 어긋나면 `Question` 에 원 항목 `answers` 를 실어 넘긴다.

## 9. 기각한 대안

**(a) 매 턴 재알림.** §3.3 이 질문을 inbox 항목으로 배달하므로 **질문이 그 런의
user 턴 자체**다 — 이미 매 턴 컨텍스트에 있다. `core.py` 에 턴 경계 seam 을
새로 팔 이유가 없고, 매 턴 관찰 레코드는 긴 조사 중에 컨텍스트만 희석한다.

**(b) `Question` 에 `origin_*` 4필드.** 불변식(§0) 아래서 **주소가 곧 원
요청자**라 라우팅엔 `target` 하나로 족하다(§10.1). 런 스코프의 두 seq 는 다른
축이다.

**(c) 접두사 파싱으로 `question_id` 대체.** 표시 문자열이 계약이 된다(§3.3).

**(d) 사람 주소 질문도 블록.** CLI 에 답할 루프가 없어 확정 행이고(§2-⑧),
블록을 한 경로라도 남기면 그 경로에 대해 다섯 판의 문제가 전부 돌아온다.

**(e) 콜러블 셋.** 15군데 수정 대 객체 하나(§4).

**(f) main sweep.** `run_one` 뒤에 표면을 하나 더 만드는 값보다 다음 런 nag +
`nags` 상한이 싸다(§3.4 끝).

## 10. 판 이력

### 10.1 1판의 근본 원인: 남의 설계에 내 규칙을 얹었다

1판의 복잡도 대부분이 **"사람은 주소와 무관하게 답할 수 있다"** 에서 나왔다.
그건 **블록 설계 §3.2** 에서 내가 넣은 규칙이고, **교착을 구제할 탈출구**가
목적이었다. 비동기에는 교착이 없으므로 그 규칙은 존재 이유가 없는데, 그대로
들고 와서 **"사람이 main 앞 질문에 답하면 답한 주체 ≠ 원 요청자"** 라는
엣지를 만들고 그걸 설계의 문제로 보고했다.

규칙을 걷어내니 `origin_*` 4필드도, 라우팅 분기도, 운영자 예외도 사라졌다.

### 10.2 1판 → 2판 (전부 코드로 확인)

| | 1판 | 2판 |
|---|---|---|
| 1a | 답 런의 결과가 어디로도 안 감(`expects_reply=False`, `:1475-1480`) | `author=q.target`·`expects_reply=True` |
| 1b | **질문 배달을 아예 안 씀** → idle peer 는 영영 모름 | 기존 배관으로 배달 |
| 1c | asker 가 건 질문이 안 지워짐 | 양방향 정리 |
| 1d | 먼저 `complete` 하면 같은 seq 에 회신 둘 | 빚 진 런은 회신 억제 |
| 1e | `_questions` 에 락 없음 | `_cv` |
| 1f | 같은 질문 재발 탐지 없음 | id 재사용 |
| §2 | 의사코드 컴파일 불가 · 검사 위치 · nag 가 `max_turns` 태움 · `[complete, answer]` 유실 · 비-complete 종료 | 3판 §3.4, **4판에서 게이트 자체를 폐기** |
| §3 | 사람 알림이 dispatch 층(모방 위험) · §3.8 자기모순 · 트레이/`agent_input` 미배선 | §3.6 · §3.9 · §5 |
| §4 | **main 에 답변 경로 없음** | `QuestionPort` |
| §6 | 순서가 main 을 깬다 | ①②③④ |

### 10.3 2판 → 3판

2판의 아키텍처(§0 불변식·비블로킹·라우팅)는 코드와 맞았다. 깨진 것은 전부
**"런 단위 스코프"가 문서에 없어서** 생긴 것이다 — 질문은 즉시 등록되는데
배달은 줄을 선다는 비대칭을 안 적었다.

| | 2판 | 3판 |
|---|---|---|
| **B1** | `pending()` 에 배달 개념이 없어 강제·sweep 이 **아직 안 읽은** 질문에 건다. 바쁜 peer 에게 묻기 = 상시 오동작 | `delivered_seq`·`asked_seq` 두 필드 + 항목 `question_id`(§3.2·§3.3) |
| **B2** | 검사를 `_op_complete` 맨 앞에 둬도 **다중 op 에선 늦다** — `:352` 가 flush 를 먼저 해 assistant 레코드가 이미 있다. 막겠다던 결함이 그대로 | `_owed_gate` 를 flush **앞** 포함 3곳에서(§3.4) |
| G1 | 사망 정리가 `shutdown_all` 에서도 돌아 §3.9 가 항상 0건 | `revivable` 가드(§3.8) |
| G2 | 블록/비블록 `ask` 판별 신호 없음, 프롬프트 배선 누락 | `ask_handler`→`port.ask`, `nonblocking_ask`(§4.1) |
| G3 | 회신 억제가 다른 런까지 번짐, 억제 범위 미정 | `asked_seq` + 재주입만(§3.7) |
| G4 | main 강제/sweep 미정의 | 강제 O·sweep X 를 명시(§3.4·§4) |
| G5 | 답변자 문자열 동치가 멀티뷰어에서 깨짐 | `user*` = 한 주체(§0) |
| G6 | nag 가 첫 `complete` 결과를 버림 | stash + 다르면 이어 붙임(§3.4) |
| G7 | `snapshot`(`:385`)은 `AgentInstance` · `id` 미탑재 · `runtime.py` 문구 · echo 우회 · `any_activity` | §5 표 · §8-⑥⑦ |

또 2판이 **제 손으로 틀린** 것 둘: `state_is_active` 를 개명 대상으로 적었으나
`waiting_ask` 전용이 아닌 일반 술어였고, 테스트 영향을 "10파일 ~80군데"로
과대 계상했다(실제 5파일 43군데).

### 10.4 3판 → 4판 (구현 중 발견)

구현하며 사용자가 지적했다: *"complete 이 오면 그대로 바로 출력해 주고 남은
질문에 대해서 다른 루프를 시작하는 거 아니야?"* — 맞다. 런을 붙잡는 게이트는
**비동기 전제를 스스로 깨는 것**이었고(§3.4), 그것이 리뷰 블로커 B2 와 결과
stash 를 동시에 만들어 낸 원인이었다. 게이트를 없애니 `dispatch.py` 변경이
0이 됐다.

구현이 드러낸 것 셋(전부 테스트로 고정):

| | 3판 | 4판 |
|---|---|---|
| 게이트 | `_op_complete` 에서 `_CONTINUE` + 결과 stash | 폐기 — 독촉을 inbox 항목으로(§3.4) |
| 독촉 스코프 | `delivered_seq == 이 런의 seq` | `delivered_seq is not None` — seq 동치면 독촉이 **한 번만** 나가고 상한도 안 걸린다 |
| 독촉 연쇄 | (없던 문제) | 독촉 런 뒤엔 독촉 금지 — 없으면 상한 6회가 수 밀리초에 탄다 |
| main 이 asker | `submit(q.asker, …)` | `_agents` 에 main 슬롯이 없다 → 메일박스 `kind:"answer"` |

### 10.5 교훈

1판이 틀린 자리는 *"무엇을 강제할까"만 쓰고 "누가 어떻게 받고 답하나"를 안 쓴
것*이었다. 2판이 틀린 자리는 그 배달을 쓰고도 **"언제 받는가"를 안 쓴 것**이다
— 등록과 배달이 같은 순간이 아니라는 비대칭. 두 번 다 같은 부류다: **상태를
바꾸는 지점만 적고, 그 상태를 읽는 쪽이 어느 시점에 서 있는지를 안 적었다.**

그리고 §10.1 — 앞선 설계에서 내가 만든 규칙을 새 설계에 관성으로 들고 오면,
그 규칙이 풀던 문제가 이미 사라졌는지를 먼저 물어야 한다.
