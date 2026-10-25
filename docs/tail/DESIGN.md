# 꼬리 — 사용 가이드의 자리 (v10.73.0)

## 1. 왜

꼬리(매 턴 마지막 메시지 뒤에 붙는 블록)는 모델이 가장 잘 읽는 자리다 — v8.52.0 에
Task Guidelines 를 머리에서 꼬리로 옮긴 근거(35B 가 머리의 원칙은 0/1, 꼬리의 같은
문장은 3/3 준수). 그 뒤 꼬리에는 상태 블록(Open Items·Background jobs·Live
Agents·메모리 색인·Web Sources·압축 안내·마지막 턴 안내)이 하나씩 늘었고, 상시 규칙은
열다섯 줄(3,182자, 턴마다 다시 prefill 되는 비캐시 구간)이 됐다.

2026-10-25 검토에서 "같은 말이 두 곳에 있다" 가 보였다. `python` 도구의 설명(머리,
도구 목록)과 Task Guidelines 의 python 줄(꼬리)은 네 항목(한 번 로드하고 함수만 고쳐라 ·
한 호출 = 한 단계 · 그림은 좌표로 · 산출물은 파일로)이 같은 문장이었다. 첫 시도
(브랜치 `tail-state-advice`, PR #171, 폐기)는 **꼬리 쪽을 지웠다** — "도구 설명에
있으니 중복" 이라는 판단으로. 결과는 §4.

## 2. 원칙 — 역할이 자리를 정한다

| 역할 | 자리 | 내용 |
|---|---|---|
| **설명** — 이 도구가 무엇인가 | 머리(도구 목록, `Tool.description`) | 모델이 **관찰을 해석하는 데** 필요한 것만: 성질(상태가 남는다), 결과의 모양(`state:` 줄·마지막 식 값), 그림이 이미지로 온다, 타임아웃 때 어떻게 되나 |
| **경로 선택** — 이런 작업이 오면 이 도구다, 저 경로는 안 된다 | 꼬리 상시 한 줄(`TASK_GUIDELINES`) | 작업이 주어, 도구가 술어. 도구를 **고르기 전** 판단에 끼어드는 글이라 조건을 만들 수 없다("아직 안 썼다" 는 모든 세션의 처음부터 참) |
| **사용법** — 쓰는 동안 어떻게 | 꼬리 상태 조건부(`## Python session`) | 인터프리터에 상태가 있는 동안만. 들고 있는 변수 이름을 함께 보여 "재읽기 금지" 가 구체적이 된다 |
| **교정** — 잘못된 경로를 탄 직후 | 꼬리 상태 조건부 한 줄(`SHELL_PYTHON_NOTICE`) | 지난 턴 shell 명령이 파이썬을 돌렸다(`python3 -c`·heredoc·`.py`) |

"중복" 의 판단 기준은 **같은 문장이 있나** 가 아니라 **같은 역할을 같은 자리에서
하나** 다. 머리의 설명은 모델이 도구를 고른 뒤에 읽는 글이고, 꼬리의 가이드는 고르기
전에 읽는 글이다 — 문장이 같아도 역할이 다르면 중복이 아니고, 역할이 같으면 더 잘
읽히는 자리 하나에만 둔다(사용자 모델: "도구 설명은 앞, 도구 사용 가이드는 상황에
맞게 꼬리 세션 상태 메시지 및 task guide").

## 3. 무엇 (v10.73.0)

- **머리** `PythonTool.description` — 성질만. 지운 것: "Use it for data work — load or
  OCR once…", "plot coordinates instead of reading numbers", "Keep each call to one
  step; a long loop should accumulate…", "Print only what you need", "Write final
  deliverables to files". 전부 꼬리에 있다.
- **꼬리 상시** `TASK_GUIDELINES` python 줄 — 한 문장:
  "Work you will iterate on — a costly step (loading, decoding, preparing data)
  followed by repeated attempts at the next one — goes through `python`, not Python
  run from `shell` (an inline `-c`, a heredoc or a script file): state persists
  between calls, so the costly step runs once and only the attempt changes."
  이득이 나는 일의 **성질**로 말한다 — 작업 종류(OCR·파싱·추출…)를 나열하면 그 과제에
  묶이고, 실행 파일 이름(`python3`)은 환경마다 다르다(둘 다 사용자 지적). 감지
  (`shell_runs_python`)는 `python`·`python3`·`python3.11` 을 다 본다.
- **꼬리 조건부** `tools/python_tool.tail_block(state)` →
  ```
  ## Python session — holding: texts (dict, 12 keys) · parse (function)
  Refine the function, do not reload — the data above is still in memory. One call = one step; a long loop accumulates into a dict or list so a timeout keeps what is done. Plot coordinates or shapes instead of reading numbers. Print only what you need to see (slices, len(), head()). Deliverables go to files — memory is not an output.
  ```
  입력은 `PyWorkerRegistry.state_of(owner)`(살아 있는 워커의 마지막 상태; 워커를
  만들지 않는다). 상태가 비면 블록도 없다. 자리는 Background jobs 뒤, Live Agents 앞.
- **꼬리 교정** `session_state.SHELL_PYTHON_NOTICE` — 턴 줄 바로 아래, Open Items 앞:
  "⚠ Last turn ran Python through `shell`. Use the `python` tool instead: it keeps state
  between calls — load once, then refine only the function."
  조건은 `LoopState.shell_ran_python_turn == turn - 1`. `ToolBridge._record_tool_history`
  가 shell 명령에 `python_tool.shell_runs_python` 을 대어 찍는다 — `python3 -c …`,
  `python3 - <<EOF`, `python3 x.py`(옵션이 끼어도)는 참, `python3 -m pip/pytest/venv`
  와 `--version` 은 거짓. 명령 문자열이 history 에 남으므로 **발동 여부를 기록으로
  검증할 수 있다**(§4 의 첫 시도는 그게 안 됐다).
- 둘 다 `python` 이 광고된 루프에서만(`"python" in tools_list`).

## 4. 측정

### 4.1 첫 시도 — 꼬리 줄 삭제 + "지난 턴 실패" 권고 (2026-10-25, 폐기)

wheel 10.72.0(base) vs 브랜치 `tail-state-advice`(cand), Qwen3.6-35B-A3B-8bit,
thinking off, tb21 9과제 × 2회(교차). cand 는 python 줄·작업 줄 뒷부분·"diagnose
first" 줄을 지우고(16줄 → 13줄, 턴당 약 215 토큰 감소 확인) `LoopState.last_turn_failed`
→ "⚠ A call failed last turn. Read its error, fix the cause, then retry" 를 넣었다.

| | pass | turns | min | failed obs | 복구 턴 | python 호출 | shell 호출 | format err |
|---|---|---|---|---|---|---|---|---|
| base | 11/18 | 562 | 171 | 48 | 1.02 | 266 | 267 | 16 |
| cand | 10/18 | 638 | 168 | 70 | 1.35 | 177 | 338 | 26 |

- 통과 수 차이는 35B 잡음 안(5·6 vs 4·6). 형식 오류 증가는 regex-log cand-r1 의
  되돌리기 고리 한 건(`ACTION_LOOP` 15회).
- **python 호출 −33% 는 두 과제에 몰렸다**: extract-elf 23·32 → 0·4, financial 23·7 →
  0·0. 대신 shell 안 `python3 -c`·heredoc 이 9 → 32건, .py 작성 후 실행 4건. shell 안
  python 의 Traceback 비율 2/46 → 10/74 — 이 실패는 shell 관찰이 success 라
  하니스에 실패로 잡히지 않는다. financial cand-r1: t10 에 분류·이동·CSV 를 한 번에
  하는 .py 를 써 돌리고, t21 에 그 스크립트를 **다시** 돌려(파일은 이미 옮겨짐) CSV
  를 0 으로 덮고, t24 에 기억으로 손으로 쓴 CSV 가 검증기에 떨어졌다. REPL 셀이었으면
  상태가 남아 없었을 일.
- 질적 분류(탐색/산출물/검증/셸 대체/실패)는 나머지 7과제에서 양쪽 같음. 상태 재사용은
  양쪽 모두 거의 없음(4% vs 1%), 셀 중앙값 50줄 — "한 호출 = 한 단계" 는 어느 쪽도
  지키지 않는다(35B 의 기존 습성, 꼬리와 무관).
- "지난 턴 실패" 권고: 복구 턴 1.02 → 1.35(두 회차 공통, 고리 시도 제외해도 남음) —
  의도와 반대. 꼬리는 저장되지 않아 그 줄이 떴다는 직접 증거가 없다. 폐기.

결론: **머리의 설명은 사용 가이드를 대신하지 못한다.** 설명을 가진 채로도 모델은
shell 경로로 갔다. §2 의 원칙은 여기서 나왔다.

### 4.2 이 판 (v10.73.0)

- 기준: 위 base-r1·r2(wheel 10.72.0, 같은 조건) 그대로. 후보: 이 브랜치, 같은 조건
  1회 먼저("수정사항만 퀵하게"), 방향이 맞으면 1회 더.
- 지표: ① python 호출 수와 shell 안 python 수(elf·financial 이 base 수준으로 돌아오나),
  ② 교정 줄 발동 수(history 의 shell 명령에 `shell_runs_python` 을 대어 센다) 와 그
  다음 턴의 도구 선택, ③ 통과 세트 퇴행 없음·총 턴, ④ 첫 호출 입력 토큰(꼬리는
  python 을 안 쓰는 과제에서만 짧아진다 — 블록이 쓰는 동안 매 턴 뜨므로).
- 결과: §5 에 기록.

## 5. 결과

(측정 뒤 기록)

## 6. 남긴 것

- 다른 도구 설명에도 사용 규칙이 섞여 있을 수 있다(shell·edit_file·fetch). 같은 기준으로
  훑어 목록만 만든다 — 이 변형에는 넣지 않았다(효과 귀속).
- 첫 시도의 다른 삭제(작업 줄 뒷부분 → Background jobs 블록, "diagnose first" 삭제,
  머리 Context Discipline 의 "추론 간결" 불릿을 꼬리 한 줄로 합침)는 cand 에서 행동
  영향이 보이지 않았지만 python 줄 삭제와 섞여 있어 귀속이 안 된다. 다음 변형(2단계
  "기대·실패시 행동" 산문)에 함께 실을지는 사용자 결정.
- 머리 **Reply Discipline** 1·2번과 방언 **Response Format** 7번의 겹침은 상주·방언
  소유라 이번 범위 밖.
