# 에이전트 `ask`/`answer` — 비동기 문답 설계 (2판)

> 상태: **설계 2판 · 리뷰 대기**
> 블록 계열 1~5판은 `DESIGN-blocking.md` 에 기록으로.
> 1판(비동기)은 외부 리뷰에서 *"as written 은 구현 불가"* 판정 — 그 근거와
> 수리는 §10.

## 0. 불변식 하나

```
질문의 주소 = ask 시점의 tm.current_author = 원 요청자
답할 수 있는 주체 = 그 주소, 오직 그것
∴ 답한 주체 = 원 요청자                    (항상)
```

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

### 3.2 질문 목록

```python
@dataclass
class Question:
    id: str          # q-<hex6>
    asker: str       # "agt-x9"
    target: str      # "main" | "agent:agt-b1" | "user:bob"  ← 주소 = 원 요청자
    text: str
    asked_at: float
    nags: int = 0
```

`AgentRegistry._questions: dict[str, Question]`, **모든 접근은 `_cv` 아래**
(`:394`). `submit` 이 이미 `_cv` 아래서 claim 을 원자화하는 그 요구가 그대로
적용된다 — `answer_question` 의 `pop`, `pending()` 의 스냅샷, `nags += 1`.

**목록인 이유**: 비동기라 한 에이전트가 여러 질문을 동시에 걸 수 있다(블록
설계의 "카디널리티 1" 논거는 성립하지 않는다).

**중복 방지**: 같은 asker→target 에 같은 텍스트가 열려 있으면 기존 id 를
돌려준다. `_op_ask` 는 루프 탐지기 앞에서 반환하므로(`dispatch.py:649` vs
`:832`) 반복 질문이 탐지되지 않는다.

### 3.3 배달 — 질문도 답도 **기존 배관**으로

```python
# 질문 (ask 시점)
target == "main"      → _push_reply({"kind":"question", ...})   # MailWaker 가 깨움
target == "agent:B"   → submit(B, f"[question {id} from {asker}]: {text}",
                               author=f"agent:{asker}", expects_reply=False)
target == "user:*"    → 배달 없음. ❓ 트레이가 표면이다 (§3.6)

# 답 (answer 시점)
submit(q.asker, f"[answer to your question: {q.text}]\n{text}",
       author=q.target,        # ← 주소 = 원 요청자. 새 필드가 필요 없다
       expects_reply=True)     # ← 그 런의 결과가 원 요청자에게 간다 (§2-②)
```

**idle 상대도 깨어난다** — inbox 항목이 곧 런이다. 그래서 `_op_complete`
검사(§3.4)는 배달 수단이 아니라 **"읽고도 안 답함" 백스톱**이다.

peer 질문을 `expects_reply=False` 로 보내는 이유: B 가 그 질문 항목을 처리한
산출물이 A 에게 되돌아가면 안 된다. B 는 **`answer` 도구로** 답한다.

### 3.4 강제 — 답할 것이 남으면 런이 안 끝난다 (LLM 대상)

**거부가 아니다.** `complete` 는 파싱되지만 루프가 끝나지 않는다. 모델에게
에러 상태를 주지 않는 것이 핵심 — 작은 모델에 "거부"는 혼란스럽고 "아직
남았다"는 정상 연속이다.

```python
# _op_complete(self, llm_text, turn, op, outcome) — llm_text 를 넘겨받아야 한다
# 검사는 함수 **맨 앞**, :639-645 보다 먼저.
owed = [q for q in port.pending() if q.enforceable]   # target 이 user* 가 아닌 것
if owed:
    _append_observation(llm_text, ... _format_owed(owed) ...)
    return _CONTINUE
```

`:639-645` 뒤에 두면 안 되는 이유: `ctx.add(serialize_terminal_for_history(...))`
가 터미널 assistant 레코드를 쓰고 `_append_observation` 이 또 하나를 써서
**한 emission 에 assistant 레코드 둘**이 되고, `render_step("final")` 이 웹에서
**작업 카드를 닫아 버린다**(`web.py:1425-1445`).

**턴 계수**: nag 턴은 `_intervene`(`:132`)의 관례를 따라 **세지 않는다**
(`self.state.turn -= 1`). 상한은 턴이 아니라 `nags` 가 맡는다(§3.5) — 상주
에이전트의 `max_turns` 가 유한할 수 있어(`agents_live.py:1689`) nag 가 예산을
태우면 안 된다.

**`[complete, answer]` 순서 주의**: 터미널 뒤의 op 는 조용히 버려진다
(`dispatch.py:359` 가 `return`). nag 문구에 *"emit `answer` BEFORE `complete`
in the same turn"* 을 넣는다.

**`_op_complete` 에 도달하지 않는 종료**가 여럿이다 — `max_turns`
(`core.py:486`) · LLM 실패(`:817`) · 중단(`:830`) · 액션 루프 하드페일
(`dispatch.py:862`). 그래서 **`_handle_request` 가 `_run_message` 뒤에서
sweep** 한다: 그 런이 진 빚이 남아 있으면 "(답변 없음 — 상대 런 종료)" 으로
닫고 asker 에게 배달한다.

### 3.5 상한

`nags`(= complete 시도 횟수, 기본 6) 초과 → "(답변 없음)" 으로 닫고 asker 에게
배달, 런은 정상 종료. **사람 주소에는 적용하지 않는다**(§3.6).

### 3.6 사람에게 묻는 경우 — 강제가 아니라 알림

**루프를 붙잡는 강제는 루프를 도는 주체에게만 걸 수 있다.** 사람의 루프는 우리
것이 아니고, CLI 엔 답할 자리조차 없다(§2-⑧).

- **강제 목록에서 빠진다** — `complete` 를 막지 않는다.
- **❓ 트레이가 표면이다** — `_questions` 중 `target.startswith("user")` 인 것.
  런이 끝났든 도는 중이든 뜬다. 지금처럼 `waiting_ask` state 를 보지 않는다.
- **`complete` 결과에 미답 질문이 실린다** — 사람이 *"내가 답을 안 해서
  끝났구나"* 를 안다:

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

→ **열린 질문을 진 채 끝난 런은 회신을 밀지 않는다.** main 은 이미 질문을
받았으므로(§3.3) 깜깜하지 않고, 답 런의 회신이 그 요청의 진짜 회신이다.

### 3.8 사망

`_worker` 의 `finally`(`:1385`)가 레지스트리가 사망을 관찰하는 자리다
(kill·crash·ctx 실패가 다 지난다). **양방향으로 정리**한다:

- 그 에이전트 **앞으로 온** 질문 → "(종료됨)" 으로 닫고 asker 에게 배달
- 그 에이전트가 **건** 질문 → 폐기 (남겨 두면 답하려는 쪽이 dead 에러를
  받고 재시도하며 nag 를 태운다)

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
    def pending(self) -> list[Question]: ...
    def ask(self, text) -> str: ...        # id
    def answer(self, id, text) -> str: ... # err
```

`LoopConfig.questions` 에 담고 `AnswerTool.requires_handler = "questions"` +
`force_mount = True` — `MessageTool` 과 같은 선언(§2-④). **레지스트리가 아니라
포트**라 `state.py:59-62` 의 단일 가드는 그대로다.

**main 도 같은 포트를 받는다** — `registry.question_port(None)`(주소 라벨
`"main"`). 이게 없으면 `_answer_kind` 제거 후 **main 에 답변 수단이 없다**.

## 5. 영향 받는 표면

| 곳 | 변경 |
|---|---|
| `agents_live.py` | `Question`·`_questions`(+`_cv`)·`QuestionPort`·질문 배달·사망 양방향 정리·`_handle_request` sweep·사람 알림 문구·§3.7 회신 억제 |
| `agents_live.py:1153` | `agents.json` 에 열린 질문 |
| `tools/virtual.py` | `AnswerTool`(`requires_handler="questions"`, `force_mount`) · `AskTool.RESIDENT_DESCRIPTION` |
| `loop/state.py` | `LoopConfig.questions` |
| `loop/core.py:150` | `handler_resources` 에 `"questions"` · main 포트 조립 |
| `loop/dispatch.py:602` | `_op_complete` 맨 앞 검사(+`llm_text`) · nag 턴 미계수 |
| `loop/dispatch.py` | `_op_answer`(`_op_message:701` 동형) · `_op_ask` 비블로킹화 |
| `prompts/system_prompt.py:640` | `ask` 설명 override · `_ASK_INLINE` 분기 |
| `subagent/runner.py:189` | 포트 전달 |
| `agents_live.py:385` | `snapshot()` 에 `open_questions: [{id,text,to,ts}]` |
| `web/server.py:1248` | `agent_input` 이 `answer_id` 수용 → `answer_question` |
| `web/static/app.js:2110` | 트레이를 `open_questions` 기반으로(질문별 항목) |
| `build_reply_record:250,256` | 질문 문구 둘 |

**1단계(`203fa94`)에서 제거될 것**: 슬롯 4필드 · `answered` Event ·
`_answer_kind`/verdict · `can_answer_agent` · `has_active_work` 의
`waiting_ask` 분기 · `waiting_ask` 어휘(`state_is_active`·`waiting_ask_keys`
→ `open_human_question_keys`). 테스트 영향 10파일 ~80군데.

**남는 것**: `submit()` 자체는 유지(답·질문 배달이 쓴다).

## 6. 실행 순서 — flip 은 마지막에서 두 번째, 원자적으로

1판의 1→2→3→4 는 2와 3 사이에 **main 이 깨진다**(ask 는 즉시 반환하는데 독촉도
트레이도 없고 프롬프트는 "BLOCKED"라고 거짓말한다).

| 순서 | 내용 | 끝났을 때 |
|---|---|---|
| **① 코어** | `Question`·`_questions`·포트·사망 정리·영속 | 호출자 0. 동작 불변 |
| **② 받을 준비** | `_op_complete` 검사·sweep·사람 알림·`snapshot`·`agent_input`·트레이·main 포트 | `_questions` 가 비어 **전부 no-op**. 동작 불변 |
| **③ flip** | `AnswerTool`·`_op_answer`·`_op_ask` 비블로킹·설명 override·`build_reply_record` 문구 둘·**1단계 TC 재작성** — **한 커밋** | 동작이 바뀌는 유일한 단계 |
| **④ 정리** | 블록 잔재 제거 | |

### 수락 기준

**실하네스 왕복 1회.** 손으로 쓴 프롬프트 프로브는 증명력이 약하다(§7) —
`run_subagent_message` 를 실제로 태워 **A 가 묻고 → A 가 계속하고 → B 가
답하고 → A 의 답 런 결과가 원 요청자에게 도달**하는 것을 로컬 모델로 한 번
확인한다.

### 테스트 계획

| 층 | 내용 |
|---|---|
| 비블로킹 | `ask` 즉시 반환 · 여러 질문 동시 · 같은 질문 재발 시 id 재사용 |
| 배달(질문) | peer inbox 에 들어간다 · **idle peer 가 깨어난다** · main 은 메일박스+MailWaker |
| 배달(답) | `author=q.target`·`expects_reply=True` · **답 런의 결과가 원 요청자에게** ← 1판의 최대 결함 |
| 짝짓기 | 없는/이미 답한 id 거부 · `_cv` 아래 원자적 claim |
| 강제 | 미답이면 `complete` 후에도 계속 · nag 턴이 `max_turns` 를 안 태움 · `[complete, answer]` 안내 |
| sweep | `max_turns`·LLM 실패·중단으로 끝나도 빚이 닫힌다 |
| 사람 | 트레이가 `target=user*` 만 · `complete` 를 막지 않음 · 결과에 실림 · 트레이 답이 질문을 닫고 새 런을 연다 · 상한 미적용 |
| §3.7 | 열린 질문을 진 채 끝난 런은 회신을 안 민다 |
| 사망 | 양방향 정리 |
| 실모델 | 왕복 1회 (수락 기준) |
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
**③** 답이 늦게 와서 asker 가 이미 끝냈으면 §3.7 이 처리한다.
**④** main·delegate 의 `ask`(사람에게 묻기)는 무변경.
**⑤** CLI 에서 사람 주소 질문의 알림은 **묘비명**이다 — 답할 자리가 없다(§2-⑧).

## 9. 기각한 대안

**(a) 매 턴 재알림.** §3.3 이 질문을 inbox 항목으로 배달하므로 **질문이 그 런의
user 턴 자체**다 — 이미 매 턴 컨텍스트에 있다. `core.py` 에 턴 경계 seam 을
새로 팔 이유가 없고, 매 턴 관찰 레코드는 긴 조사 중에 컨텍스트만 희석한다.

**(b) `Question` 에 `origin_*` 4필드.** 불변식(§0) 아래서 **주소가 곧 원
요청자**라 `target` 하나로 족하다. 1판이 "사람은 주소 무관 답변자" 규칙을
블록 설계에서 들고 와서 필요해 보였을 뿐이다(§10.1).

**(c) 사람 주소 질문도 블록.** CLI 에 답할 루프가 없어 확정 행이고(§2-⑧),
블록을 한 경로라도 남기면 그 경로에 대해 다섯 판의 문제가 전부 돌아온다.

**(d) 콜러블 셋.** 15군데 수정 대 객체 하나(§4).

## 10. 1판 → 2판

### 10.1 근본 원인: 남의 설계에 내 규칙을 얹었다

1판의 복잡도 대부분이 **"사람은 주소와 무관하게 답할 수 있다"** 에서 나왔다.
그건 **블록 설계 §3.2** 에서 내가 넣은 규칙이고, **교착을 구제할 탈출구**가
목적이었다. 비동기에는 교착이 없으므로 그 규칙은 존재 이유가 없는데, 그대로
들고 와서 **"사람이 main 앞 질문에 답하면 답한 주체 ≠ 원 요청자"** 라는
엣지를 만들고 그걸 설계의 문제로 보고했다.

규칙을 걷어내니 `origin_*` 4필드도, 라우팅 분기도, 운영자 예외도 사라졌다.

### 10.2 리뷰가 찾은 것 (전부 코드로 확인)

| | 1판 | 2판 |
|---|---|---|
| 1a | 답 런의 결과가 어디로도 안 감(`expects_reply=False`, `:1475-1480`) | `author=q.target`·`expects_reply=True`(§3.3) |
| 1b | **질문 배달을 아예 안 씀** → idle peer 는 영영 모름 | 기존 배관으로 배달(§3.3) |
| 1c | asker 가 건 질문이 안 지워짐 | 양방향 정리(§3.8) |
| 1d | 먼저 `complete` 하면 같은 seq 에 회신 둘 | 빚 진 런은 회신 억제(§3.7) |
| 1e | `_questions` 에 락 없음 | `_cv`(§3.2) |
| 1f | 같은 질문 재발 탐지 없음 | id 재사용(§3.2) |
| §2 | 의사코드 컴파일 불가 · 검사 위치 · nag 가 `max_turns` 태움 · `[complete, answer]` 유실 · 비-complete 종료 | §3.4 |
| §3 | 사람 알림이 dispatch 층(모방 위험) · §3.8 자기모순 · 트레이/`agent_input` 미배선 | §3.6 · §3.9 · §5 |
| §4 | **main 에 답변 경로 없음** | `QuestionPort`(§4) |
| §6 | 순서가 main 을 깬다 | ①②③④(§6) |

### 10.3 교훈

1판이 틀린 자리는 **"무엇을 강제할까"만 쓰고 "누가 어떻게 받고 답하나"를 안
쓴 것**이다. 강제(§3.4)는 상세했는데 배달(§3.3)·마운트(§4)·트레이(§5)가
비어 있었다. 리뷰어의 판정 *"as written 은 구현 불가"* 는 **문서**에 대한
정확한 평가였다.

그리고 §10.1 — 앞선 설계에서 내가 만든 규칙을 새 설계에 관성으로 들고 오면,
그 규칙이 풀던 문제가 이미 사라졌는지를 먼저 물어야 한다.
