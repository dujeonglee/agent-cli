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

## 5. 결과 (2026-10-25, Qwen3.6-35B-A3B-8bit · thinking off · tb21 9과제)

| | pass | turns | min | failed obs | python 호출 | shell 안 python (Traceback) | format err | out tokens |
|---|---|---|---|---|---|---|---|---|
| base r1 (10.72.0) | 5/9 | 283 | 86 | 28 | 137 | 30 (2) | 12 | 155k |
| base r2 (10.72.0) | 6/9 | 279 | 85 | 20 | 129 | 16 (0) | 4 | 148k |
| pyguide r1 (49b0629c) | 4/9 | 227 | 79 | 25 | 89 | 6 (0) | 6 | 103k |
| pyguide r2 (4과제만) | 2/4 | 113 | — | — | 46 | 13 (1) | — | — |
| **pyguide r3 (e5855c5b)** | **7/9** | 242 | 75 | 15 | 99 | 16 (2) | 2 | 100k |

- **r1·r2**: shell 안 python 은 설계대로 줄었는데(elf 16/1 → 0·4 였던 cand 와 달리 base 범위로
  복귀) financial·regex-log 가 둘 다 두 번 떨어졌다(0/4; base 4/4). 턴 단위로 열어 보니 regex r2 는
  도구 순서가 base 와 같고 패턴 경계가 틀린 것, financial 은 두 런 모두 같은 문서를 오분류 — 경로
  선택과 이어지는 선은 없었다. 대신 둘이 드러났다. ① `shell_runs_python` 이 경로 붙은 인터프리터
  (`/usr/bin/python3 -c`)를 못 잡아 financial r2 의 10건에 교정 줄이 한 번도 안 떴다(4562af78). ②
  컨테이너 PATH 에 python3 가 없어 도구는 uv 의 agent-cli venv, 모델의 pip 은 다른 Python →
  ModuleNotFoundError → shell 회귀. 빠진 순간 인터프리터 경로와 설치 명령을 관찰에 붙였다
  (`missing_module_hint`, e5855c5b).
- **r3 (두 수정 포함)**: base 가 두 번 다 통과한 5과제 전부 통과, financial·regex 복귀,
  large-scale-text-editing 은 이 모델의 어느 런에서도 처음 통과. 출력 토큰 base 의 2/3, 실패 관찰
  15, 형식 오류 2. 안내 줄은 gcode 에서 두 번 떴고 모델은 설치 대신 메모리의 데이터로 우회해 python
  셀을 이어 갔다(shell 로 떨어지지 않음). 1회 런이라 7/9 자체는 상한일 수 있으나 "퇴행 없음 +
  비용 감소" 는 같은 방향이다. 채택.
- 꼬리 첫 호출 입력 토큰은 base 보다 약 115 적다(블록은 python 을 쓰는 동안 매 턴 뜨므로 절감은
  python 을 안 쓰는 과제에서만).

## 6. 남긴 것

- pyguide r2 financial 에서 드러난 **인터프리터 불일치**(python 도구 = uv 의 agent-cli venv, 모델의 pip = 다른 Python → ModuleNotFoundError → shell 로 회귀)는 꼬리와 무관한 요인이라 같은 브랜치에서 관찰 안내로 고쳤다(`python_tool.missing_module_hint`, docs/python-tool/DESIGN.md §3). 재측정(§4.2 r3)은 이 수정 포함.

- 다른 도구 설명에도 사용 규칙이 섞여 있을 수 있다(shell·edit_file·fetch). 같은 기준으로
  훑어 목록만 만든다 — 이 변형에는 넣지 않았다(효과 귀속).
- 첫 시도의 다른 삭제(작업 줄 뒷부분 → Background jobs 블록, "diagnose first" 삭제,
  머리 Context Discipline 의 "추론 간결" 불릿을 꼬리 한 줄로 합침)는 cand 에서 행동
  영향이 보이지 않았지만 python 줄 삭제와 섞여 있어 귀속이 안 된다. 다음 변형(2단계
  "기대·실패시 행동" 산문)에 함께 실을지는 사용자 결정.
- 머리 **Reply Discipline** 1·2번과 방언 **Response Format** 7번의 겹침은 상주·방언
  소유라 이번 범위 밖.

## 9. 다른 도구 설명도 같은 기준으로 — A 묶음 (PR #174, **불채택**)

§2 의 기준으로 열아홉 도구의 설명을 훑었다(2026-10-25). 성질과 섞여 있던 사용 규칙·작업 나열:

| 도구 | 섞여 있던 것 | 처리 |
|---|---|---|
| shell | 꼬리 작업 줄과 같은 문장("1분 넘으면 background=true … 로그 … 통지") + "a test suite, a build, an install, a large clone" | 성질만: background=true 가 어떤 모양으로 도는가. 경로 선택은 꼬리 작업 줄 하나. `timeout` 의 "올리지 말고 background" 도 삭제(같은 줄에 있다) |
| monitor | 셸 요리법 7줄(`nohup … > /tmp/x.log 2>&1 &`, `python -u`/PYTHONUNBUFFERED, `EXIT:$?`), "prefer `schedule`" | 요리법 삭제 — v10.47.0 의 background=true 이후 낡았고 꼬리의 `/tmp` 금지와 충돌. schedule 비교는 성질로("monitor 는 세션과 함께 저장되지 않는다") |
| write_file | "NEW 파일이나 SMALL 파일 재작성에 써라, 부분 수정은 edit_file" + 비용 논증 | 성질만(내용이 컨텍스트를 두 번 통과). 경로 선택은 edit_file 설명("the default way to modify")과 재작성 관찰 교정("Use edit_file next time")이 이미 맡는다. "편집이 계속 실패하면 재작성해도 된다" 는 남김(v10.13.0 실측 탈출구) |
| python (`timeout`) | "a model load, a big OCR batch" | "a step you know takes long" |
| browser · memory · history · snapshot · schedule | 가벼운 경로 선택·사용 문장 | **다음 PR**(벤치 과제가 안 쓰므로 라이브로 확인) |
| agent | 위임 비용 안내 | **손대지 않음** — v10.26.0 에 64모듈급 과제로 잰 문구 |
| read_file · edit_file · fetch · code_index · wait · complete · ask · answer · message · reply | 성질·프로토콜 의미 | 그대로 |

측정: 기준 pyguide-r3(10.73.0), 후보 이 브랜치 1회. 지표: `background: true` 사용 수와 폴링·`kill`·
timeout 올리기 사고, `/tmp` 경로 사용 수, 기존 파일 `write_file` 재작성 수(관찰 교정 발동 수),
통과 세트·총 턴. 결과는 §10.

## 10. 결과 (2026-10-25, 35B · thinking off · 1회)

| job | wheel | 통과 | 턴 | 분 | 실패 관찰 | 형식 오류 | 출력 토큰 | python |
|---|---|---|---|---|---|---|---|---|
| pyguide-r3 (기준) | 10.73.0 | **7/9** | 242 | 75 | 15 | 2 | 100k | 99 |
| tooldesc-r1 | PR #174 (79248f5f) | **2/9** | 280 | 93 | 25 | 1 | 153k | 140 |

과제별: db-wal ✅→✅, regex ✅→✅, video ⏱→⏱, gcode ❌→❌; **extract-elf·financial·large-scale·multi-source 가
✅→❌**, log-summary 는 에이전트 설치 360초 초과(환경 실패, 세부 로그 없음).

§9 가 노린 지표는 **판정 불가**: `background: true` 는 0→1(기준도 0 이라 꼬리 줄만으로 유지되는지
가릴 수 없다), 폴링 2→0, `kill`·`nohup`·끝 `&` 0→0, timeout 올리기 0→0, 기존 파일 `write_file` 재작성 1→1,
monitor 0→0. 포그라운드 긴 명령 10→15(apt/pip 설치), 절대 경로 `/tmp` 0→45(43건이 large-scale 한 시도의
vim 실험 파일).

떨어진 넷의 실패 모양은 지운 문장과 닿지 않는다 — extract-elf 는 주소 97개 값 오류(기준은 python 없이
통과, 이번은 python 24회), financial 은 total 에 Net worth 를 씀(pyguide-r2 와 같은 금액 오류),
large-scale 은 `/app/apply_macros.vim` 을 끝내 안 쓰고 60턴 vim 디버깅, multi-source 는 'John D.'/'John Doe'
정합 오류(이전 4런 전부 통과). ModuleNotFoundError 안내는 1/1 따름(financial t18→t19, 제시한 명령 그대로).

1회 판단: 35B 는 같은 조건에서도 3~5/9 를 흔들지만 7→2 는 그 폭을 넘는다. 지운 문장이 어떤 경로로
이 실패들을 만들었는지 설명이 없는 채로는 머지하지 않고, 같은 wheel 로 1회 더 돌려 분산인지 효과인지 가린다
(사용자 결정, 2026-10-26).

### 10.1 r2 (2026-10-26 02:15–03:42 KST, 같은 wheel · sha256 동일)

| task | pyguide-r3 | r1 | r2 |
|---|---|---|---|
| db-wal-recovery | ✅ | ✅ | ❌ WAL 갱신 미반영(이전 5런 전부 통과) |
| extract-elf | ✅ | ❌ | ❌ r1 과 **같은 97개 값** 으로 실패, 11턴에 대조 없이 종료 |
| extract-moves-from-video | ⏱ | ⏱ | ⏱ |
| financial | ✅ | ❌ Net worth | ❌ 다른 모양(jpg 에서 "$4382 due" 를 잡음) |
| gcode-to-text | ❌ | ❌ | ⏱ 900초 |
| large-scale-text-editing | ✅ | ❌ shell·/tmp 60턴 | ❌ 같은 vim 실험 고리, python 셀 안에서 |
| log-summary | ✅ | ⏱ 환경 | ✅ |
| multi-source | ✅ | ❌ | ✅ |
| regex-log | ✅ | ✅ | ✅ |

| job | 통과 | 턴 | 분 | 실패 관찰 | 형식 오류 | 출력 토큰 | shell | python |
|---|---|---|---|---|---|---|---|---|
| pyguide-r3 | 7/9 | 242 | 75 | 15 | 2 | 100k | 136 | 99 |
| tooldesc-r1 | 2/9 | 280 | 93 | 25 | 1 | 153k | 127 | 140 |
| tooldesc-r2 | 3/9 | 226 | 87 | 10 | 0 | 140k | 78 | 149 |

조건별 합: 10.72.0 (5·6) 11/18 · 10.73.0 (4·7) 11/18 · **10.74.0 tooldesc (2·3) 5/18**. 두 런 모두 이전 네 런의
범위(4~7) 아래다. §9 가 노린 지표는 r2 도 무변(background:true 0, timeout 올림 0, 기존 파일 재작성 0,
monitor 0, /tmp 3). ModuleNotFoundError 안내는 2/2 따름(gcode 는 38턴 우회 뒤).

유일하게 두 런에 걸쳐 보이는 행동 변화는 **shell→python 이동**이다: shell 136→127→78, python 99→140→149.
extract-elf 는 기준이 python 없이 통과했는데 두 런 모두 python 으로 풀어 같은 틀린 값에 닿았다. 지운 문장
(shell 의 작업 나열 "a test suite, a build, an install, a large clone" 과 "1분 넘으면 background")이 shell 의
쓸모를 좁혀 읽히게 했다는 가설은 세울 수 있으나 기록으로 확인되지 않는다.

**결론: 일관된 퇴행으로 보고 이 변형은 채택하지 않는다.** v10.73.0 의 "설명에서 사용 규칙을 지워도 꼬리가
맡는다" 는 python 한 사례였고, shell·write_file·monitor 로 일반화되지 않았다. 설명을 성질로 줄이는 일은 도구
하나씩, 그 도구를 실제로 부르는 과제로만 잰다.

## 11. monitor 설명만 (v10.74.0 후보, 브랜치 `monitor-description-cleanup`)

§10.1 의 결론대로 도구 하나만 바꾼다. 사용자가 고른 것은 monitor 의 셸 요리법이다 — §9 표의 monitor 행과
같되, 지운 요리법 중 **버퍼링 문장은 성질로 남긴다**(로그 파일을 보는 조건 `file+pattern`·`file+idle` 에 지금도
해당: 버퍼링하는 생산자는 끝날 때까지 파일에 아무것도 안 써 건강한 작업이 침묵으로 보인다). A 묶음에서는
요리법과 함께 지웠는데 되돌아보면 남겼어야 할 문장이다.

요리법이 틀린 이유 넷: ① 실행 방식 — 모델이 `nohup … &` 로 직접 띄우라 하나 꼬리 작업 줄은 `background: true`
로 하니스가 소유(v10.47.0 A/B: 직접 띄운 작업은 tail 폴링·pkill 자살·끝 줄 없는 monitor 사고), ② 경로 —
`/tmp/x.log` 는 꼬리의 "never in /tmp" 와 충돌, ③ `EXIT:$?` 를 직접 쓰고 `^EXIT:` 를 match 하라는 것은 종료
코드 통지가 없던 때의 방법, ④ "`2>&1` 없으면 shell 이 timeout 까지 막힌다" 는 모델이 직접 리다이렉션할 때만의
사정. shell·write_file·python 설명은 main 그대로.

바뀐 문장(모델이 읽는 것): 요리법 7줄 → "A producer that buffers its output writes nothing to the file until it
exits, so a healthy job looks silent — run it unbuffered." / "prefer `schedule`" → "A monitor is not saved with the
session and does not survive a restart (`schedule` is and does)."

측정: 벤치 여섯 런 모두 monitor 호출 0 이라 직접 지표는 없다. 보는 것은 통과 세트가 10.73.0 범위(4~7/9)를
지키는지, 그리고 A 묶음에서 보였던 shell→python 이동(shell 136→78)이 이 변형에는 없는지 — 그 이동이 shell
설명 쪽 변화에 귀속되는지 가리는 대조군이 된다. 35B 2회. 결과는 §12.

## 12. 결과 (2026-10-26 05:07–08:13 KST, 35B · thinking off · 2회, wheel 75ae9c78 sha b07da57b…)

| 조건 | 런별 통과 | 합 | python 호출 | shell 호출 |
|---|---|---|---|---|
| 10.72.0 (tail-base r1·r2) | 5, 6 | 11/18 | 89~137 범위 | 115~152 범위 |
| 10.73.0 (pyguide r1·r3) | 4, 7 | 11/18 | (같은 범위) | (같은 범위) |
| PR #174 A 묶음 (tooldesc r1·r2) | 2, 3 | 5/18 | 140, 149 | 127, 78 |
| **PR #175 monitor 만 (mondesc r1·r2)** | **4, 4** | **8/18** | **146, 177** | 159, 73 |

| task | pyguide-r3 | mondesc-r1 | mondesc-r2 |
|---|---|---|---|
| db-wal-recovery | ✅ | ❌ sqlite3 질의 직후 WAL 소실(tooldesc-r2 와 같은 모양) | ✅ |
| extract-elf | ✅ | ❌ 82개 값 오류(tooldesc 의 "97개 전부 0" 과 다른 모양) | ✅ |
| financial | ✅ | ✅ | ❌ Net worth 를 total 로(tooldesc-r1 과 같은 모양) |
| gcode-to-text | ❌ | ❌ | ❌ |
| large-scale | ✅ | ❌ shell 49회 vim exit code 디버깅, /tmp 39건 | ⏱ python 셀 안 vim 실험 → background+monitor+kill |
| log-summary | ✅ | ✅ | ❌ 370 대신 74(이전 런 전부 통과) |
| multi-source · regex-log | ✅ ✅ | ✅ ✅ | ✅ ✅ |
| video | ⏱ | ⏱ | ⏱ |

턴 297·236, 분 77·108, 출력 토큰 131k·154k(기준 100k), 실패 관찰 35·27(기준 15), 환경 실패 0, 연결 오류 0.

**변형이 직접 닿는 자리 — monitor 1건** (mondesc-r2 large-scale t19). 모델은 요리법 없이도 같은 모양을
스스로 만들었다: `vim … > .agent-cli/tmp/jobs/vim_transform.log 2>&1; echo "EXIT_CODE=$?" >> …` 를
`background: true` 로 띄우고 같은 턴에 `monitor file=… pattern="EXIT_CODE=|error|Error"` 를 걸고 `wait` 했다.
경로는 꼬리 규칙대로 `.agent-cli/tmp/jobs/`, 종료 코드 줄은 설명에 없는데도 썼다 — 즉 `EXIT:$?` 관용구는
설명이 아니라 모델의 사전 지식에서 온다. vim 이 끝나지 않아 monitor 는 10분 silent 로 은퇴했고, 그 뒤
`kill 5920` 2회·`&` 붙인 재실행·`pkill -f vim` 로 1200초에 걸렸다(kill 3·`&` 1 은 전부 이 시도). 다른 17개
시도에서는 monitor·kill·nohup·`&` 모두 0.

**해석.**
- 통과 4·4 는 10.73.0 의 두 런(4·7)이 그린 범위 안이다. 합 8/18 < 11/18 이지만, 같은 조건의 10.73.0 이
  4 와 7 을 냈으므로 2회로는 이 차이를 변형에 귀속할 수 없다.
- python 호출 증가(146·177, 이전 네 런 89~137)는 **shell·python 설명을 건드리지 않은 이 변형에도 있다.**
  §10.1 에서 A 묶음의 shell→python 이동을 shell 설명 변화에 귀속하려던 가설은 이것으로 기각된다. 두 변형의
  공통점은 wheel 버전 문자열(10.74.0)과 **측정 시각**(10-25 심야~10-26 새벽; 기준 네 런은 10-25 낮)뿐이다.
  python 증가가 환경·시각 드리프트인지는 10.73.0 기준을 같은 시각대에 1회 더 돌려야 가려진다.
- 반복된 실패 모양(vim 고리, sqlite3 CLI 질의 직후 WAL 소실, Net worth 금액)은 세 조건(기준 포함 r1·r2
  어딘가)에 걸쳐 나오는 35B 의 과제별 취약점이고 도구 설명과 무관하다.
- monitor 요리법의 효과는 이 벤치로 **잴 수 없다**(여섯 런 0회, 이번 1회). 이 변형의 근거는 측정이 아니라
  정합성이다: 요리법은 현 하니스(background=true·`.agent-cli/tmp`·종료 코드 통지)와 모순되는 사실 오류다.

판단은 사용자에게: (1) 10.73.0 기준을 같은 시각대에 1회 재측정해 python 증가·4/9 가 드리프트인지 가린 뒤
결정, (2) 정합성 근거로 머지(측정상 귀속 가능한 퇴행 없음), (3) 접기. → 사용자: (1) 뒤 (2).

### 12.1 기준 재측정 pyguide-r4 (2026-10-26 09:25–11:04 KST, v10.73.0 릴리스 wheel, 1회)

| | pyguide-r3 | mondesc-r1 | mondesc-r2 | **pyguide-r4** |
|---|---|---|---|---|
| 통과 | 7/9 | 4/9 | 4/9 | **4/9** |
| 턴 · 분 | 242 · 75 | 297 · 77 | 236 · 108 | 322 · 99 |
| 실패 관찰 · 형식 오류 | 15 · 2 | 35 · 5 | 27 · 2 | 47 · 6 |
| 출력 토큰 | 100k | 131k | 154k | 207k |
| shell · python | 136 · 99 | 159 · 146 | 73 · 177 | 135 · **189** |

r4 는 db-wal(⏱, sqlite3 질의 직후 WAL 소실)·financial(vat 칸에 Net worth)·large-scale(vim 고리, `:wq` 없음)·
video·gcode 가 떨어졐고, 변형 런에서 반복된 세 모양이 **기준에서도 전부** 나왔다. python 호출 189 는 변형 두 런
(146·177)보다도 많다. db-wal·extract-elf 의 python 수는 r4 가 31·38 — pyguide-r3 의 4·0 이 다른 기준 런(17·34·13 /
16·23·32)에 비해 낮은 값이었다.

**결론.** 변형(mondesc 4·4)과 같은 시각대의 기준(r4 4)이 같다. §12 의 두 의문 — 통과 8/18 < 11/18, python 증가 —
은 모두 **변형이 아니라 측정 시점**에 귀속된다. 10.73.0 의 35B 범위는 4~7/9 로 넓고, pyguide-r3 의 7/9 는 그 상단
이었다(기준 다섯 런: 5·6·4·7·4). 이 변형에 귀속 가능한 퇴행은 없고, 근거는 정합성이다 → **v10.74.0 으로 출시.**

부수 교훈: 기준 런 하나(r3 7/9)를 비교 기준으로 삼으면 그 뒤 모든 변형이 퇴행으로 보인다. 변형과 기준은 **같은
시각대에 짝으로** 돌려야 하고(10-25 낮 vs 심야 사이 무엇이 달랐는지는 모른다 — omlx 상태·캐시·호스트 부하 후보),
한 변형의 판정에 쓰는 기준은 그 변형과 함께 돌린 것이어야 한다. A 묶음(§10.1, 2·3)은 그래도 같은 시각대 변형·기준
(4·4·4) 아래라 불채택 판단은 유지한다 — 다만 그 차이도 1~2 개 과제 폭이라 확정은 아니다.

### 12.2 GLM 짝 검증 (2026-10-26 13:58–18:25 KST, GLM-5.3-Flash-oQ4-mtp · thinking off · 교차 4런)

순서 base-r1(10.73.0) → mondesc-r1(10.74.0) → base-r2 → mondesc-r2. wheel sha 는 10분마다 확인(고정), 네 런 rc=0,
연결·환경 실패 0, omlx ReadTimeout 1회(재시도 성공). 측정 중 35B 는 omlx 에서 내렸다.

| | v10.67 r1 | v10.67 r2 | base-r1 | mondesc-r1 | base-r2 | mondesc-r2 |
|---|---|---|---|---|---|---|
| 통과 | 6/9 | 6/9 | 8/9 | 7/9 | 6/9 | 5/9 |
| 턴 | 101 | 101 | 89 | 127 | 124 | 98 |
| 실패 관찰 · 형식 오류 | 9 · 0 | 6 · 0 | 5 · 0 | 6 · 0 | 7 · 0 | 4 · 0 |
| shell · python | 51 · 36 | 36 · 47 | 55 · 20 | 59 · 46 | 53 · 50 | 57 · 22 |
| monitor · nohup | | | 0 · 0 | 0 · 0 | 0 · 0 | 0 · 0 |

짝 합 기준 **14/18**, 후보 **12/18**. 짝 안에서 뒤집힌 과제는 두 번 다 financial 하나이고, 두 번 다 같은 PDF
(`dx0AWchV01ZJ.pdf`)를 송장으로 넣은 오분류(송장 10 → 11) — 35B pyguide-r1·r2(10.73.0 이전 판)에서도 나온 모양이다.
나머지 과제는 짝 안에서 결과가 같다(짝 2 의 gcode·log-summary·video 는 둘 다 실패, 실패 모양은 서로 다름).

**판단.** 바뀐 것은 monitor 설명뿐인데 네 런 모두 monitor 호출이 0 이고, 뒤집힌 과제는 PDF 분류다 — 설명 변경이
닿는 경로가 없다. 통과 수는 실행 순서대로 8·7·6·5 로 줄어 짝 안 차이(1)가 시각 추세와 같은 방향으로 겹친다.
python 호출(20·46·50·22)은 버전과 무관하게 흔들린다. **귀속 가능한 퇴행 없음 → v10.74.0 유지.** 다만 financial 이
두 짝 모두 같은 쪽으로 뒤집힌 것은 우연으로 단정하지 않고, 다음 GLM 측정에서 같은 PDF 오분류가 기준 판에서도
나오는지 본다.
