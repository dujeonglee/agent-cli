# 에이전트 `ask`/`answer` — 비동기 문답 설계 (2판)

> 상태: **설계 3판 · 구현 착수 가능**
> 블록 계열 1~5판은 `DESIGN-blocking.md` 에 기록으로.
> 1판(비동기)은 *"as written 은 구현 불가"* 판정(§10.2), 2판은 블로커 2건
> (§10.3). 3판은 *"B1·B2·G1~G3 반영되면 구현 착수 가능"* 을 반영한 것.

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

### 3.4 강제 — 빚이 남으면 런이 안 끝난다

**거부가 아니다.** `complete` 는 파싱되지만 루프가 끝나지 않는다. 모델에게
에러 상태를 주지 않는 것이 핵심 — 작은 모델에 "거부"는 혼란스럽고 "아직
남았다"는 정상 연속이다.

```python
def _owed_gate(self) -> str | None:
    """빚이 있으면 nag 문구, 없으면 None."""
    owed = [q for q in port.pending()
            if q.target_is_me and q.delivered_seq is not None]
    ...
```

**호출 지점이 셋이고 서로 배타적이다.** `_op_complete` 안에 두면 안 된다:

| 경로 | 자리 | 전달 방식 |
|---|---|---|
| 다중 op(`[edit, complete]`) | `dispatch.py:352` 터미널 분기, **`_flush_op_results` 앞** | `results.append({"tool_name":"complete","success":False,"observation":nag})` → flush → `_CONTINUE` |
| 단일 op | `_dispatch_op` 의 `complete` 분기 머리(`:568`) | `_append_observation(llm_text, ...)` → `_CONTINUE` |
| echo-as-final | `_try_echo_as_final` 분기(`:576`) | 위와 동일 |

`_op_complete` 맨 앞은 **다중 op 에서 늦다**. 쓰는 두 포맷 다
`multi_op = True`(`json_fc.py:622`·`xml_fc.py:293`)이고, `:352` 가 터미널 op
앞에서 `_flush_op_results` → `_append_observation`(`:1286-1293`)으로 **이미
assistant 레코드를 하나 쓴 뒤** `_dispatch_op` 로 간다. 거기서 nag 가 같은
`llm_text` 로 하나 더 쓰면 **한 emission 에 assistant 레코드 둘** — 2판이
`:639-645` 를 피해 막겠다고 한 바로 그 결함이 다중 op 경로로 되돌아온다.

`run_skill` 도 터미널이지만 빚과 무관하니 **`complete` 만** 대상이다.

**첫 `complete` 의 결과는 버리지 않는다.** `_CONTINUE` 를 돌리면 런의 output 은
마지막 `complete` 것이 되는데, 작은 모델은 답한 뒤 "답했습니다"로 끝내기 쉽다.
→ 첫 결과를 런에 stash 해 두고 **최종 결과와 문자열이 다르면 이어 붙인다**
(같으면 그대로). 임계값 없는 결정적 규칙이라 유실이 없다. 그리고 nag 앞에
`render_step("action", tool_name="complete")` 를 낸다 — `_op_ask`/`_op_message`
가 그러듯(`:655-660` 주석), 없으면 스트리밍 카드가 다음 턴 것과 눌어붙는다.

**턴 계수**: nag 턴은 `_intervene`(`:132`) 관례를 따라 세지 않는다
(`self.state.turn -= 1`). 상주 에이전트의 `max_turns` 가 유한할 수 있어
(`agents_live.py:1689`) nag 가 예산을 태우면 안 된다. 상한은 `nags` 가 맡는다.

**`[complete, answer]` 순서 주의**: 터미널 뒤의 op 는 조용히 버려진다
(`:359` 가 `return`). nag 문구에 *"emit `answer` BEFORE `complete` in the same
turn"* 을 넣는다.

**`_op_complete` 에 도달하지 않는 종료**가 여럿이다 — `max_turns`
(`core.py:486`) · LLM 실패(`:817`) · 중단(`:830`) · 액션 루프 하드페일
(`dispatch.py:862`). 그래서 **`_handle_request` 가 `_run_message` 뒤에서
sweep** 한다: `delivered_seq == 이 런의 seq` 인 빚만 *"(답변 없음 — 상대 런
종료)"* 으로 닫고 asker 에게 배달한다.

**main 에는 sweep 이 없다.** `_handle_request` 가 main 에 없고, `run_one` 뒤에
같은 것을 다는 건 표면을 하나 더 늘린다. 대신 **다음 main 런의 nag 가 백스톱**
이고 최종적으로 `nags` 상한(§3.5)이 닫는다. 그 사이 asker 의 회신이 §3.7 로
억제된 채 머무는 창이 있다 — main 이 다음 턴을 돌면 닫히므로 수용한다.

### 3.5 상한

`nags`(= 빚을 진 채 `complete` 을 시도한 횟수, 기본 6) 초과 → *"(답변 없음)"*
으로 닫고 asker 에게 배달, 런은 정상 종료. **사람 주소에는 적용하지 않는다**
(§3.6).

### 3.6 사람에게 묻는 경우 — 강제가 아니라 알림

**루프를 붙잡는 강제는 루프를 도는 주체에게만 걸 수 있다.** 사람의 루프는 우리
것이 아니고, CLI 엔 답할 자리조차 없다(§2-⑧).

- **강제 목록에서 빠진다** — `complete` 을 막지 않는다. `delivered_seq` 도 안
  찍힌다(배달이 없으므로) — §3.4 의 필터가 자연히 걸러낸다.
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

### 3.7 asker 가 먼저 `complete` 한 경우

B 가 물어두고 부분 결과로 `complete` 하면 seq N 의 회신이 main 에게 가고,
나중에 답 런이 **같은 요청에 대한 두 번째 회신**을 만든다.

→ **`asked_seq == 이 런의 seq` 인 열린 질문을 진 채 끝난 런은 회신을 밀지
않는다.** main 은 이미 질문을 받았으므로(§3.3) 깜깜하지 않고, 답 런의 회신이
그 요청의 진짜 회신이다. `asked_seq` 로 좁히지 않으면 다른 런에서 걸어 둔
사람 질문(영영 열림)이 이후 모든 회신을 막는다.

**억제 범위는 재주입뿐이다** — `_push_reply`/`_deliver_peer_reply` 만 건너뛰고
창 렌더·`_log_conversation`·`_persist_reply`(`:1451-1470`)는 그대로 돈다.
사용자는 B 가 한 일을 본다.

§3.6 과 §3.7 은 **런 단위로 배타적**이다: 한 런의 질문은 전부
`target == 그 런의 author` 이므로, 사람 발신 런이면 전부 사람 주소(→ 억제
대상이지만 그 런의 회신은 애초에 창뿐), main/peer 발신 런이면 전부 그쪽이다.

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

### 3.9 영속

`_questions` 는 `agents.json` 의 기존 `pending` 미러(`_save_state:1153`)에
같이 싣는다. resume 시 되살리지 않고 **열린 질문 N건을 알리기만** 한다 —
저장 없이 N 을 알릴 수 없으므로(1판의 자기모순), 알릴 거면 저장한다.

`build_reply_record` 의 질문 문구 둘이 **비동기에선 둘 다 거짓**이 된다:
*"BLOCKED until answered"*(`:256`)와 *"NO LONGER waiting"*(`:250`). flip
커밋에서 함께 고친다.

## 4. `QuestionPort` — seam 하나

콜러블 셋을 `LoopConfig`·`AgentLoop.__init__`·`run_loop`·
`run_subagent_message`·`_run_message` 다섯 곳에 각각 꿰면 15군데다. 객체 하나로:

```python
class QuestionPort:            # 레지스트리가 에이전트별로 만든다
    def pending(self) -> list[Question]: ...   # target_is_me 판정 포함
    def ask(self, text) -> str: ...            # id (또는 에러)
    def answer(self, id, text) -> str: ...     # err
```

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
| `loop/dispatch.py:352` | 터미널 분기, flush **앞**에 `_owed_gate` |
| `loop/dispatch.py:568,576` | `complete` 분기 머리 · echo-as-final 분기에 `_owed_gate` |
| `loop/dispatch.py` | `_op_answer`(`_op_message:701` 동형) · `_op_ask` 비블로킹화 · 첫 결과 stash |
| `loop/prompt.py:35-47` | `nonblocking_ask` 전달 |
| `prompts/system_prompt.py:630,777` | `build_system_prompt_sections`/`_build_tools_section` 시그니처 · `ask` 설명 override · `_ASK_INLINE` 분기(`:616-620`) |
| `subagent/runner.py:189` | 포트 전달 |
| `web/server.py:1248` | `agent_input` 이 `answer_id` 수용 → `answer_question` |
| `web/static/app.js:2110` | 트레이를 `open_questions` 기반으로(질문별 항목) |
| `web/static/app.js:2284` | 상태 dot 의 `"w"` 분기 제거 — 비동기엔 그 state 가 없다 |
| `runtime.py:144-152` | `waiting_ask_keys` → `open_human_question_keys`, **그리고 "resume 시 STALE 처리됩니다" 문구를 §3.9 의미로** |
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
| **② 받을 준비** | `_owed_gate` 3곳·결과 stash·sweep·사람 알림·`roster_snapshot`·`agent_input`·트레이·`any_activity`·main 포트 | `_questions` 가 비어 **전부 no-op**. 동작 불변 |
| **③ flip** | `AnswerTool`·`_op_answer`·`ask_handler`→`port.ask`·`_op_ask` 비블로킹·`nonblocking_ask` 프롬프트 배선·`build_reply_record` 문구 둘·**1단계 TC 재작성** — **한 커밋** | 동작이 바뀌는 유일한 단계 |
| **④ 정리** | 블록 잔재·주석·`can_answer_agent` 제거 | |

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
| 강제 | **`[edit, complete]` 다중 op 에서 assistant 레코드가 하나** ← B2 · 단일 op · echo-as-final · nag 턴이 `max_turns` 를 안 태움 · `[complete, answer]` 안내 · 첫 결과가 안 사라진다 |
| sweep | `max_turns`·LLM 실패·중단으로 끝나도 그 런의 빚만 닫힌다 |
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
| §2 | 의사코드 컴파일 불가 · 검사 위치 · nag 가 `max_turns` 태움 · `[complete, answer]` 유실 · 비-complete 종료 | §3.4 |
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

### 10.4 교훈

1판이 틀린 자리는 *"무엇을 강제할까"만 쓰고 "누가 어떻게 받고 답하나"를 안 쓴
것*이었다. 2판이 틀린 자리는 그 배달을 쓰고도 **"언제 받는가"를 안 쓴 것**이다
— 등록과 배달이 같은 순간이 아니라는 비대칭. 두 번 다 같은 부류다: **상태를
바꾸는 지점만 적고, 그 상태를 읽는 쪽이 어느 시점에 서 있는지를 안 적었다.**

그리고 §10.1 — 앞선 설계에서 내가 만든 규칙을 새 설계에 관성으로 들고 오면,
그 규칙이 풀던 문제가 이미 사라졌는지를 먼저 물어야 한다.
