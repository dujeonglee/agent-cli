# 에이전트 `ask`/`answer` — 비동기 문답 설계

> 상태: **설계 · 실모델 검증 완료 · 구현 대기**
> 2026-09-20 · 사용자 제안. 블록 계열 1~5판은 `DESIGN-blocking.md` 에 기록으로.

## 0. 한 줄

**아무것도 블록하지 않는다.** `ask` 는 질문을 등록하고 **즉시 반환**하고,
`answer` 는 id 로 짝지어 답을 배달한다. 강제는 **답할 것이 남으면 런이
끝나지 않는 것**으로 하되, 그 강제는 **루프를 도는 주체(LLM)에게만** 건다 —
사람에게는 강제가 아니라 **알림**이다(§3.6).

## 1. 왜 방향을 바꿨나

다섯 판을 돌며 고친 것이 전부 **"블록하되 무엇으로 깨우나"** 였고, 외부 리뷰가
찾은 결함도 전부 블록의 부산물이었다:

| 블록 설계가 만든 문제 | 비동기에서 |
|---|---|
| 상대가 dead 면 영원히 대기 (L1) | 대기가 없다 |
| 상대가 도중 kill 되면 아무도 안 깨움 (L2) | 깨울 것이 없다 |
| arm 을 공개보다 먼저 해야 하는 레이스 (C1) | arm 이 없다 |
| 종료가 슬롯도 깨워야 함 | 슬롯이 없다 |
| 막힌 슬롯 뒤의 큐가 펌프를 붙잡음 (A1) | 막힌 슬롯이 없다 |
| A↔B 상호 대기 (L3) | 둘 다 계속 일한다 |
| "도착 순서가 답" — 주소/종류로 판정 | **id 로 짝짓는다** |
| peer 에게 배달할 채널이 없음 | 답이 곧 새 요청 — 기존 경로 |
| 헤드리스에서 답할 주체가 없음 | 상대가 매 턴 독촉받는다 |

### 1.1 앞서 기각한 "비블로킹 ask"와 다르다

블록 설계 §10-(d)는 비블로킹을 기각했다 — *"슬롯이 없어지지 않고
`paused_item` 으로 옮겨갈 뿐"*. 그 기각은 비블로킹을 **"요청을 일시정지했다
재개"** 로 읽은 것이었다. 이 설계는 **일시정지하지 않는다**: A 는 지금 요청을
그대로 끝내고, 답은 **나중에 도착하는 새 요청**이다. 기억할 멈춘 요청이
없으므로 슬롯도 없다.

## 2. 조사 — 기존 구조가 이미 맞춰져 있다

**① 상주 에이전트는 inbox 항목 1개 = 런 1개다**(`agents_live.py:12`,
`_handle_request:1416` → `run_subagent_message`). 답을 "새 요청"으로 만들면
**배달 배관이 0줄**이다 — 지금 peer 회신이 요청자 inbox 로 재주입되는
(`_deliver_peer_reply:769-775`) 그 경로다.

**② ctx 가 영속이라 대화가 이어진다.** A 가 물어두고 다른 일을 하다 답이 와도
같은 ctx 위에서 이어 판단한다.

**③ 런 시작 프롬프트에 동적 섹션 슬롯이 있다** — `peer_agents_section`
(`state.py:76`)이 `_run_message` 에서 매 런 조립된다.

**④ callable seam 선례가 둘** — `ask_handler`·`message_handler`
(`state.py:67-76`). 레지스트리를 서브루프에 넘기면 **상주 모드 잠금이 풀린다**
(`state.py:59-61`: *"teammate 안 teammate 금지의 단일 가드"*). 새 훅도 callable 로.

**⑤ 터미널 op 를 "계속"으로 돌릴 수 있다.** `_op_ask`·`_op_message` 가 관찰을
붙이고 `_CONTINUE` 를 돌려주는 패턴이 있다(`dispatch.py:697·740`).

**⑥ CLI 에는 사람이 답할 루프가 없다.** 명령이 `run`/`setup`/`sessions`/
`update`/`web` 뿐이고 `@agt-<key>` 는 `run` 의 **인자**다. 사람 주소 질문에
블록을 남기면 CLI 에서 확정 행이다 — `runtime.py:149` 의 *"답변 대기 중인 채
종료"* 경고가 그 사실의 흔적이다.

## 3. 설계

### 3.1 두 도구

```python
ask(question)                  # → {"id": "q-7a3f", "status": "asked"} 즉시 반환
answer(id, text)               # → 짝지어 배달, 목록에서 제거
```

`ask` 는 **주소를 스스로 정한다** — 지금 처리 중인 요청의 발신자
(`tm.current_author`). 질문 페이로드가 이미 `to` 로 싣던 값이라(`:1516`) 새
기준이 아니다.

`ask` 의 반환 관찰:

```
Observation: question q-7a3f sent to agt-b1. You are NOT blocked — continue with
whatever does not depend on the answer. The answer will arrive as a new message.
If nothing else can proceed, `complete` and you will be resumed when it arrives.
```

**마지막 문장이 중요하다.** `ask` 는 보통 "막혔다"는 뜻이라 "계속하라"고만 하면
35B 가 추측하고 끝낼 수 있다. `complete` 해도 이어진다는 걸 알려 준다 — 답이
새 요청으로 오므로 사실이다.

### 3.2 질문 목록 — 레지스트리가 소유

```python
@dataclass
class Question:
    id: str          # q-<hex6>
    asker: str       # "agt-x9"
    target: str      # "main" | "agent:agt-b1" | "user:bob"
    text: str
    asked_at: float
    nags: int = 0

    @property
    def enforceable(self) -> bool:
        """루프를 도는 주체에게만 강제할 수 있다 (§3.6)."""
        return not self.target.startswith("user")
```

`AgentRegistry._questions: dict[str, Question]` — **목록 하나**, 강제 여부는
`target` 에서 **파생**한다(두 번째 목록을 만들지 않는다).

**슬롯이 아니라 목록인 이유**: 비동기라 한 에이전트가 여러 질문을 동시에 걸 수
있다. 블록 설계의 "카디널리티 1" 논거는 여기선 성립하지 않는다.

### 3.3 강제 — 답할 것이 남으면 런이 안 끝난다 (LLM 대상)

**거부가 아니다.** `complete` 는 정상 처리되고 루프가 **끝나지 않을 뿐**이다.
모델에게 에러 상태를 주지 않는 것이 핵심 — 작은 모델에 "거부"는 혼란스럽고
"아직 남았다"는 정상 연속이다.

```python
# _op_complete 진입부 (dispatch.py:602)
owed = [q for q in self.cfg.pending_questions() if q.enforceable]
if owed:
    _append_observation(... _format_owed(owed) ...)   # 질문 전문 + 복사 가능한 op
    return _CONTINUE
```

`_op_ask`·`_op_message` 가 쓰는 그 메커니즘이다(`dispatch.py:697·740`).

**매 턴 다시 알린다.** 한 번만 알리면 조사하러 간 사이 잊는다 — 실측에서 모델은
답하기 전에 `git branch -a` 로 **조사**했고(§4), 그 관찰 뒤에도 보여야 답으로
돌아온다.

### 3.4 배달

`answer(id, text)` → 하네스가 `Question` 을 찾아 **asker 의 inbox 에 새 요청**
으로 넣는다:

```python
submit(q.asker, f"[answer to your question: {q.text}]\n{text}",
       author=answerer, expects_reply=False)
```

`expects_reply=False` 라 A 가 처리한 산출물이 되돌아가지 않는다(핑퐁 방지 —
기존 `_deliver_peer_reply` 규율). A 가 idle 이면 그 항목이 곧 새 런이다.

### 3.5 상한 — LLM 이 답 못 하는 경우

무한 독촉은 `max_turns` 를 태우고 런을 실패로 끝낸다 — **답도 못 받고 상대의
작업도 잃는다.**

```
독촉 N회(기본 6) 초과 → "(답변 없음 — 상대가 답하지 못함)" 으로 닫고 asker 에게
                        배달, 런은 정상 종료
```

실측 평균 1.7턴(§4)이라 6은 넉넉하다. **사람 주소 질문에는 적용하지 않는다**
(§3.6) — 사람은 독촉 대상이 아니므로 세는 것 자체가 무의미하다.

### 3.6 사람에게 묻는 경우 — 강제가 아니라 **알림**

**루프를 붙잡는 강제는 루프를 도는 주체에게만 걸 수 있다.** 사람의 루프는 우리
것이 아니다. 사람이 답할 때까지 런을 살려 두면 그게 바로 피하려던 행이고, CLI
에는 답할 자리조차 없다(§2-⑥).

그래서 사람 주소 질문은:

- **강제 목록에서 빠진다** — `complete` 를 막지 않는다. 런은 정상 종료한다.
- **❓ 트레이에는 계속 떠 있다** — 답할 자리는 그대로다.
- **`complete` 결과에 미답 질문이 실린다** — 이게 알림 표면이다:

```
Repo 정리 완료. src/ 를 3개 모듈로 나눴습니다.

⏳ 답을 받지 못한 질문 1건 — 답하면 이어서 진행합니다:
   [q-4c1b] "이 설정 파일을 덮어써도 될까요?"
```

사람은 이걸 보고 *"아, 내가 답을 안 해서 그냥 끝냈구나"* 를 안다. 트레이에
답을 넣으면 **새 런이 열려 이어진다**(§3.4 의 배달 경로 그대로).

**상한도 만료도 없다.** 사람은 내일 답해도 되고, 세션이 끝나면 질문도 함께
사라진다(§3.8).

### 3.7 상대 사망

`_worker` 의 `finally`(`:1309`)가 곧 "레지스트리가 사망을 관찰하는 자리"다
(kill·crash·ctx 실패 세 경로가 다 지난다). 자기 앞 질문을 "(종료됨)" 으로 닫고
asker 에게 배달한다. **A 는 막혀 있지 않으므로 교착 해소가 아니라 정보 전달**이다.

### 3.8 영속

`_questions` 는 저장하지 않는다. 질문은 살아 있는 에이전트 사이의 상태이고,
세션이 끝나면 그 에이전트도 없다. resume 시 "이전 세션의 미답 질문 N건"만
알린다 — monitor(v9.11.0)가 택한 것과 같은 판단이다.

## 4. 실모델 검증 (2026-09-20)

가짜 러너 TC 로는 *"모델이 실제로 답하는가"* 에 답이 안 나온다 — 기존
`test_full_ask_roundtrip` 은 핸들러를 직접 부르는 스텁이다. 로컬
**Qwen3.6-35B-A3B-8bit** 로 쟀다(시나리오: B 가 `complete` 한 직후 하네스가
루프를 끝내지 않고 pending 을 알리며 계속 돈다).

| 회차 | 조건 | 결과 |
|---|---|---|
| 1 | `max_tokens=500`, 단일 턴 | 0/3 — **측정 오류**, 추론 중 잘렸다 |
| 2 | `max_tokens=2500`, 단일 턴 | 0/3 — **오독**. `git branch -a`·`cat README` = **답을 조사 중**이었다 |
| 3 | 멀티턴, 매 턴 재알림 | **3/3, 평균 1.7턴** |

3회차: `id` 를 세 번 다 정확히 복사(`q-7a3f`), 답도 근거 있음 — README 에
*"migrations land on `release` first"* 를 심은 시행은 `release` 라고 답했다.
**조사한 내용을 반영했다.**

여기서 얻은 설계 제약 둘:

- **"한 턴 더" 가 아니라 "빌 때까지"** — 답하기 전에 조사한다.
- **매 턴 다시 알려야 한다** — 조사 관찰 뒤에도 보여야 돌아온다.

## 5. 영향 받는 표면

| 곳 | 변경 |
|---|---|
| `tools/virtual.py` | `AnswerTool` 신설(`id`+`text`) · `AskTool` 설명 개정 |
| `loop/state.py` | seam 셋: `pending_questions()` · `ask_question()` · `answer_question()` — **레지스트리를 넘기지 않는다**(§2-④) |
| `loop/dispatch.py:602` | `_op_complete` 에 강제 검사 → `_CONTINUE` · 사람 미답분은 결과에 첨부(§3.6) |
| `loop/dispatch.py` | `_op_answer` 추가(`_op_message:701` 과 동형) · `_op_ask` 비블로킹화 |
| `agents_live.py` | `_questions` · seam 구현 · `_worker` finally 에서 자기 앞 질문 닫기 |
| `agents_live.py:1663` | 미답 목록을 런 시작 프롬프트에 |
| `subagent/runner.py:189` | seam 셋 전달 |
| `web/static/app.js` | ❓ 트레이가 `_questions` 기반(지금은 `waiting_ask` state 기반) |

**1단계(커밋 `203fa94`)에서 남는 것**: `submit()`/verdict · ❓ 트레이 주소
라벨은 그대로. 슬롯 4필드 · `_answer_kind` · `can_answer_agent()` ·
`has_active_work` 의 `waiting_ask` 분기는 **불필요해진다**(블록이 없으므로) —
제거는 구현 시 함께.

## 6. 알고 두는 것

**① `ask` 의 의미가 바뀐다** — "막혔으니 기다린다"에서 "물어두고 계속한다"로.
도구 설명이 그걸 분명히 하고, 정말 막혔으면 `complete` 하라고 안내한다(§3.1).

**② 한 에이전트가 여러 질문을 걸 수 있다.** 필요하면 에이전트당 미결 수를 제한.

**③ 되묻기(명확화)가 가능해진다.** 블록 설계에선 B 가 A 에게 되물으면 순환으로
거부됐다. 비동기에선 둘 다 안 막혀 있어 자연히 풀린다.

**④ 답이 늦게 와서 A 가 이미 끝냈으면** 답이 새 런을 연다. ctx 가 영속이라
맥락은 있지만 A 가 "되돌릴지" 판단해야 한다. §3.1 문구가 그걸 미리 알린다.

**⑤ main 의 `ask`(사람에게 묻기)는 무변경.** `ask_handler` 가 없는 경로라
`renderer.prompt_user` 로 블록한다 — 대화형 프롬프트는 그게 맞다.

## 7. 실행 계획

1. **코어** — `Question`·`_questions`·seam 셋·상한·사망 시 닫기. 단위 TC.
2. **도구** — `AnswerTool` · `_op_answer` · `_op_ask` 비블로킹화 · 설명 개정.
3. **강제와 알림** — `_op_complete` 검사(LLM) · 결과 첨부(사람) · 런 시작 주입.
   **실모델 왕복 TC** 포함.
4. **정리** — 블록 잔재 제거(슬롯 4필드 · `_answer_kind` · `can_answer_agent`).

### 테스트 계획

| 층 | 내용 |
|---|---|
| 비블로킹 | `ask` 즉시 반환 · worker 가 계속 돈다 · 여러 질문 동시 |
| 짝짓기 | `answer(id)` 가 그 질문에만 · 없는/이미 답한 id 는 거부 |
| 배달 | 답이 asker inbox 에 새 요청으로 · `expects_reply=False` |
| 강제(LLM) | 미답 있으면 `complete` 후에도 계속 · 비면 종료 · **매 턴 재알림** |
| 알림(사람) | 사람 주소는 `complete` 를 **막지 않고** 결과에 실린다 · 트레이 유지 · **상한 미적용** |
| 재개 | 사람이 나중에 답하면 새 런이 열린다 |
| 상한 | N회 후 "(답변 없음)" 배달, 런은 정상 종료 |
| 사망 | 상대 kill/crash 시 "(종료됨)" 으로 닫힘 |
| 실모델 | 로컬 서버 왕복 — `answer` op · id 정확 · 유한 턴 (§4 재현) |
| 비회귀 | main 의 `ask` 무변경 · 블록 잔재 제거 후 기존 TC |
