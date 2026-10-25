# 꼬리 — 중복 없는 상시 규칙 + 상태별 권고 (v10.73.0)

## 1. 왜

꼬리(매 턴 마지막 메시지 뒤에 붙는 블록)는 모델이 가장 잘 읽는 자리다 — v8.52.0 에
Task Guidelines 를 머리에서 꼬리로 옮긴 근거(35B 가 머리의 원칙은 0/1, 꼬리의 같은
문장은 3/3 준수). 그 뒤로 꼬리에는 상태 블록(Open Items·Background jobs·Live
Agents·메모리 색인·Web Sources·압축 안내·마지막 턴 안내)이 하나씩 늘었고, 상시 규칙은
열다섯 줄(3,197자, 턴마다 다시 prefill 되는 비캐시 구간)이 됐다.

2026-10-25 검토에서 둘이 보였다.

- **같은 말이 두세 곳에 있다.** python 도구의 설명과 Task Guidelines 의 python 줄은
  같은 문장. 작업 줄의 "폴링 금지·`wait`·`kill <pid>`" 는 돌고 있을 때만 뜨는
  `## Background jobs` 블록("do not poll"·`stop it: kill`)과 `## Open Items` 의 wait
  줄(v10.67.1)이 이미 말한다. 머리 Context Discipline 의 "Keep your reasoning brief" 는
  꼬리의 "Reason in short, plain sentences" 와 겹친다.
- **상태에 따라 뜻이 있는 규칙이 상시로 실린다.** "실패하면 원인부터 진단" 은 지난
  턴에 실패가 있을 때만 뜻이 있는데, 실패가 없는 턴에도 매번 실렸고 실패 직후엔
  열다섯 줄 가운데 한 줄이었다.

결정(사용자): **중복은 지우고, 세션 상태에 따라 권고되는 행동이 있으면 그것을 조건부
한 줄로 넣는다.** 꼬리의 상시 규칙은 "조건을 만들 수 없는 원칙" 만 남긴다.

## 2. 무엇을 지웠나 (중복)

| 지운 것 | 같은 말이 남아 있는 곳 |
|---|---|
| Task Guidelines 의 python 줄 ("keeps state … one call = one step … deliverables to files") | `python` 도구 설명(`PythonTool.description`) — 같은 문장 |
| 작업 줄의 뒷부분 ("log under `.agent-cli/tmp/jobs/` … call `wait` … do not poll … `kill <pid>`") | `## Background jobs` 블록(돌고 있을 때만: 로그 경로·"do not poll"·`kill`), `## Open Items` 의 `_TAIL_WAIT` 줄, complete 넛지. 상시 규칙에는 **띄우는 법**("`background: true` 로, 끝나면 알려준다, 그동안 계속 일한다")만 남긴다 |
| "If an approach fails, diagnose the cause before switching tactics." | → §3 의 상태별 권고 |
| 머리 Context Discipline 의 불릿 "Keep your reasoning brief. Do not restate what the observation already shows." | 꼬리 "Reason in short, plain sentences — …; never restate what an observation already shows." 로 합침(v8.59.0 실측 문장을 남기는 쪽. "관찰을 되풀이하지 말라" 는 거기 붙였다) |

결과: 상시 규칙 16줄 3,197자 → 13줄 2,335자(−27%, 턴당 약 200 토큰). 머리의 Response
Format(방언 소유, 재시도 넛지와 짝)·도구 안내(참조 자료)·Reply Discipline(상주 루프만,
벤치로 못 잼)은 이번 범위에서 손대지 않았다 — §6.

## 3. 상태별 권고 — 지난 턴 실패

```
── session state (context only — not part of the conversation) ──
turn 7/60 (53 left after this one)
⚠ A call failed last turn. Read its error, fix the cause, then retry — do not switch tactics before you know why it failed.

## Open Items — …
```

- **조건**: 지난 턴에 실행된 호출(도구 결과 `success=False`, 스킬 실패) 또는 거부된
  호출(모르는 도구·스키마 불일치·인자 JSON 오류 — `_reject_op`)이 하나라도 있었다.
  형식 실패(NO_JSON 등)는 포함하지 않는다 — 그쪽은 재시도 넛지가 관찰 시점에 이미
  말한다.
- **자리**: 상태 블록 머리 바로 아래, 턴 줄 다음, 열린 항목보다 앞 — "지금 무엇을
  해야 하나" 의 가장 가까운 사실. 상태가 이것뿐이어도 블록은 뜬다.
- **기계**: `LoopState.last_turn_failed` — 디스패치가 턴 시작(`_dispatch_turn` 직전)에
  False 로 놓고, `_op_execute_tool`·`_op_run_skill`·`_reject_op` 가 True 로 올린다.
  `LLMCaller._build_session_state` 가 `build_session_state(failed_last_turn=)` 로 넘기고
  `session_state.FAILED_LAST_TURN_NOTICE` 가 글이다. 홀로 거부된 호출의 형식 개입(턴
  미계수 재시도)도 같은 ctx 경로로 꼬리를 받으므로 그 재시도에도 뜬다.
- **넣지 않은 것**: "같은 호출 + 같은 결과 반복" 감지. 2026-10-14 재생에서 문구 변형이
  40회 중 3~7회만 고리를 끊었다([[stale-loop-nudge-replay]]) — 문구가 아닌 다른 종류의
  개입이어야 한다는 합의 그대로.
- **다음 후보(미구현)**: "읽지 않고 편집하려 했다 / 편집 뒤 재읽기 없이 다음 편집" —
  해시라인 거부가 관찰에 이미 있어 우선순위가 낮다.

## 4. 측정

변경은 **측정 조건 변경**이다. 채택 전 A/B:

- 기준 wheel 10.72.0 vs 후보 wheel(이 브랜치), Qwen3.6-35B-A3B-8bit, thinking off,
  tb21 9과제, 각 2회. 효과가 보이면 GLM 2회.
- 지표: ① 턴당 꼬리 토큰(줄어야 함 — `turns.jsonl` 의 input_tokens 추이로 대리),
  ② 실패한 op 뒤 다음 성공까지 턴 수(조건부 줄의 효과), ③ 통과 세트·총 턴·벽시계 퇴행
  없음, ④ `python` 호출 수(설명 중복 삭제가 사용률을 떨어뜨리지 않는지), ⑤ format
  error 수(`failures.jsonl`).
- 2단계(별도 변형): "State the plan, then act." 를 "Before the calls, say in one line
  each what result you expect and what you will do if it fails; then act." 로 — 산문
  지시라 문법·native_fc 에 무해. 1단계와 섞어 재지 않는다(효과 귀속).

## 5. 테스트

`tests/test_tail_advice.py` — 지운 줄이 되살아나면 실패(python·wait/poll/kill·diagnose·
"Keep your reasoning brief"), 줄 수 13·2,500자 미만, 권고 줄의 자리(턴 줄 아래·Open
Items 앞·상태가 이것뿐이어도 렌더), 루프 끝-끝(턴 1 실패 → 턴 2 꼬리에만 → 턴 2 성공 →
턴 3 꼬리엔 없음; 모르는 도구 거부도 실패로 센다). 짝 수정: `tests/test_jobs.py`
(작업 안내의 자리), `tests/test_wait_tool.py`, `tests/test_python_tool.py`,
`tests/test_prompt_language.py`.

## 6. 남긴 것

- 머리 **Reply Discipline** 1·2번(complete 는 답이 아니다·중간 요청도 각각)은 꼬리
  `## Open Items` 와 겹치지만 상주 루프 전용이라 Harbor 로 못 잰다 — 라이브 보드에서
  따로 재서 지운다.
- 방언 **Response Format** 의 7번("If an observation shows an error, fix parameters and
  retry") 은 §3 의 권고와 겹친다. 방언 소유 텍스트이고 골든 테스트가 묶여 있어 다음
  방언 손질 때 함께.
