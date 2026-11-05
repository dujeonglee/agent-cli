# 도구 설명 지연 로딩 — 설계 (v10.109.0~)

작성 2026-11-05. 설계 검토(https://claude.ai/artifact/RTz49h3wRrmBxny9cvYHHn)의 결정과 숫자를 옮긴다. 기준 코드 v10.108.0, 측정은 GLM-5.3-Flash 토크나이저.

## 1. 문제

모든 도구의 설명·인자 설명·인라인 안내가 매 호출의 시스템 프롬프트에 실린다. main 루프(xml_fc)의 시스템 프롬프트는 **8,002** 토큰이고 그중 `## Available Tools` 절이 **6,320** 토큰(79%)이다. 그 절반 이상이 벤치 900 세션·실사용 59 세션에서 거의 불리지 않은 도구의 몫이다(세션의 5% 이하에서만 불린 도구가 20개 중 11개). 약한 모델(35B)은 긴 프롬프트에서 지시 준수가 떨어진다는 것을 앞선 검토에서 봤다. Claude Code 의 ToolSearch 처럼 드문 도구는 이름과 한 줄만 두고 자세한 설명은 요청 시 준다.

## 2. 숫자

### 2.1 도구별 몫과 쓰임

한계 토큰 = 그 도구를 뺐을 때 절이 줄어드는 양. 쓰임 = 그 도구를 한 번 이상 부른 세션 비율.

| 도구 | 한계 토큰 | 설명 | 인자 | 안내 | 벤치 900 | 실사용 59 | 계층 |
|---|---:|---:|---:|---:|---:|---:|---|
| agent | 922 | 231 | 599 | 212 | 2% | 3% | 지연 |
| code_index | 810 | 160 | 832 | — | 1% | 5% | 지연 |
| monitor | 558 | 311 | 339 | — | 2% | 0% | 지연 |
| edit_file | 533 | 134 | 224 | 258 | 11% | 51% | 전문 유지 |
| browser | 438 | 174 | 379 | — | 미광고 | 2% | 지연 |
| ask | 378 | 52 | 43 | 304 | 0% | 3% | 전문 유지 (§5) |
| memory | 285 | 138 | 240 | — | 1% | 12% | 지연 |
| shell | 262 | 115 | 185 | — | 93% | 68% | 전문 유지 |
| read_file | 261 | 120 | 201 | — | 26% | 73% | 전문 유지 |
| schedule | 251 | 91 | 220 | — | 0% | 0% | 지연 |
| history | 236 | 142 | 123 | — | 0.4% | 0% | 지연 |
| message | 233 | 151 | 132 | — | 0% | 0% | 지연 |
| snapshot | 198 | 113 | 128 | — | 5% | 0% | 지연 |
| write_file | 175 | 128 | 75 | — | 49% | 39% | 전문 유지 |
| reply | 163 | 118 | 81 | — | 0.2% | 0% | 지연 |
| fetch | 145 | 89 | 84 | — | 0.2% | 0% | 지연 |
| wait | 104 | 76 | 43 | — | 7% | 0% | 전문 유지 |
| view_image | 64 | 43 | 40 | — | 5% | 2% | 전문 유지 |
| run_skill | 63 | 16 | 74 | — | 0.2% | 2% | 전문 유지 |
| complete | (항상) | 174 | 93 | — | 67% | 75% | 전문 유지 |

벤치는 `agent-cli[image]` 만 설치해 browser 가 광고되지 않았다. native_fc 는 같은 재료가 요청의 `tools[]` JSON 7,915 토큰으로 간다(서버가 템플릿에 펼친다).

### 2.2 절감 추정

지연 집합 D1 = agent, code_index, monitor, browser, schedule, history, fetch, snapshot, memory, message, reply (11개). 한 줄 요약은 첫 문장 길이(10~40 토큰)로 근사했다.

| 변형 | 지연 도구에 남는 것 | 절감 | 도구 절 | 프롬프트 |
|---|---|---:|---:|---:|
| V1 설명만 지연 | 한 줄 요약 + 인자 전문 | −1,710 | 6,320 → 4,610 | 8,002 → 6,290 (−21%) |
| V2 설명 + 인자 설명 지연 | 한 줄 요약 + 인자 뼈대(이름·타입·enum·필수) | −3,790 | 6,320 → 2,530 | 8,002 → 4,210 (−47%) |

`tool_help` 한 줄(구현값: GLM 50 토큰, xml_fc)은 빼지 않은 값이다.

## 3. 제약

- **KV 프리픽스.** 시스템 프롬프트는 런 동안 고정이다(v8.46.0 이래 — 중간 편집은 전체 재프리필). 설명을 요청 시 프롬프트에 펼칠 수 없다 — 전문은 **관찰**(도구 결과)로 온다.
- **스키마는 남는다.** xml_fc·json_fc 의 디코딩 문법과 native_fc 의 서버 검증이 인자 스키마를 쓴다. 지연할 수 있는 것은 설명 글(도구 설명·인자 설명·인라인 안내)뿐이다.
- **꼬리의 길 선택 문장은 그대로.** 모델이 도구를 고르는 근거는 꼬리다(v10.73.0 측정). 지연은 머리만 줄인다.
- **한 함수에서 나온다.** 프롬프트 절과 native 스키마가 같은 재료(설명/스키마 교체·인라인 안내)에서 나오듯, 지연 판정도 한 자리(`registry.tool_view`)에서.
- **표기 가드.** 한 줄 요약·`tool_help` 결과는 inventory 표본으로 가드가 본다.

### 3.1 왜 꼬리가 아니라 머리인가

- native_fc 의 `tools[]` 도 서버 챗 템플릿이 시스템 메시지 앞머리에 펼친다 — 꼬리가 아니라 프리픽스다.
- 꼬리는 매 턴 다시 프리필되는 자리라, 6,000 토큰의 고정 설명을 꼬리로 옮기면 턴마다 6,000 토큰을 새로 계산한다. 고정 글은 머리, 매 턴 바뀌는 상태와 길 선택은 꼬리.
- 프리픽스 캐시가 있어도 지연의 이득은 남는다: 디코딩은 프리픽스 전체에 어텐션하고, 본래 동기는 연산이 아니라 약한 모델의 주의·준수다.
- **ttft 실측 (2026-11-05 라이브 런 로그)**: 한 런 안에서 입력 10.2K→11.1K 로 커지는 동안 ttft 는 6.6 s → 5.1 → 4.0 → 4.2 → 4.5 → 4.6 → 4.8 s — **턴 사이에 떨어지지 않는다**(네 런 모두 같은 모양). 다른 런과 첫 프롬프트가 바이트까지 같았던 한 경우만 847 ms. 이 서버(omlx)에서는 런 안의 매 턴이 사실상 전체 프리필이라 프리픽스 길이 비용이 매 턴 든다 — 원인은 이 설계의 범위 밖.
- `tool_help` 결과는 대화 본문으로 들어가 다음 턴부터 프리픽스의 일부가 된다. 머리를 바꾸지 않으므로 캐시를 깨지 않는다.

## 4. 설계

### 4.1 두 계층

도구 클래스의 정책 속성(`requires_handler`·`mounted_with` 와 같은 결):

- `Tool.deferred: bool = False` — 프롬프트에 전문을 싣지 않는다.
- `Tool.summary: str = ""` — 지연 도구의 한 줄. "무엇인가" 가 아니라 "언제 쓰는가"(모델이 이 한 줄로 길을 고른다). 손으로 쓴다. `deferred` 인데 비면 등록 시점에 실패(`registry.check_deferred`) — 조용히 전문으로 되돌리지 않는다.

렌더는 두 곳이 같은 함수를 탄다: `registry.tool_view(name, …)` → `(설명, 인자 스키마, 안내)`. `get_tool_descriptions`(프롬프트 절, 도구마다 `render_tool_block`)와 `system_prompt.function_schemas_for`(native `tools[]`)가 둘 다 이것을 부른다. 지연 도구는 설명 자리에 `summary`, 안내 없음, 인자는 `DEFER_PARAM_DESCRIPTIONS` 가 True(V2)면 `deferred_schema(parameters)` — JSON 스키마에서 `description` 키만 재귀로 지우고 type·enum·required·items·minLength 는 보존(`properties` 의 키는 인자 이름이라 지우지 않는다). 텍스트 렌더(`render_param_value`)는 설명이 없는 열거값이면 스키마 `enum` 에서 값을 읽어 `"mode": "string, required — "add" | "list" | "delete""` 를 뼈대에서도 낸다(설명이 있으면 종전 그대로 — 지금 모든 enum 인자는 설명이 있어 바이트 불변).

### 4.2 전문을 주는 길 — `tool_help`

- `tool_help` 도구, 인자 `name`. 설명: "Full guide for one tool — what each argument means and how its results look. Call it before the first use of a tool described here in one line."
- 결과: 머리 `[tool_help] <name>` + 그 도구의 **이 루프 프롬프트에 지연 없이 실렸을 블록 그대로**(`render_tool_block(full=True)` — 같은 방언·설명/스키마 교체·인라인 안내). 따로 쓴 글이 아니라 두 벌이 생기지 않는다. 지연이 아닌 도구를 물어도 같은 전문. 모르는 이름이면 `No tool named "x" in this loop. Tools: `a`, `b`, …`(이 루프의 `effective_tool_names`, 백틱 — 표기 가드가 처음에 잡았다).
- 루프가 자기 묶음을 `RunContext.tool_guide`(콜러블, `ToolBridge._tool_guide` → `system_prompt.tool_guide_text`)로 건넨다 — 방언·종류별 교체는 루프마다 다르다.
- 광고: `requires_handler="deferred"` + `force_mount`. 다른 게이트·짝 탑재가 끝난 도구 목록에 지연 도구가 남았을 때만 붙고, 그런 루프면 프로파일 `allowed-tools` 와 무관하게 붙는다(D4). 순서는 `complete` 바로 앞.
- **A5 자동 전문**: 지연 도구의 호출이 스키마 불일치(A5)로 이 런에서 **처음** 거절될 때 거절 관찰 끝에 `Full guide:` + 전문을 한 번 붙인다(`TurnDispatcher._first_rejection_guide`, 도구별 한 번 — 디스패치만 쓰는 상태라 `LoopState` 가 아니라 디스패처가 가진다). 뼈대만 보고 틀리게 부른 순간이 전문이 필요한 순간이고 왕복 하나를 아낀다. 성공한 호출에는 붙이지 않는다.

### 4.3 한 줄 요약의 성질 (초안 — PR 2 에서 영어로)

| 도구 | 한 줄 |
|---|---|
| agent | 다른 에이전트에게 과제를 맡기거나 상주 에이전트를 띄울 때. 프로파일은 `## Agent Profiles`. |
| code_index | 코드베이스에서 정의·호출자·참조를 찾을 때 — grep 보다 먼저. |
| monitor | 파일·로그가 어떤 조건이 될 때 알림을 받고 싶을 때(작업 종료는 이미 통지됨). |
| browser | JavaScript 가 그리거나 클릭·입력이 필요한 페이지. 정적 HTML 은 `fetch`. |
| memory | 압축 뒤에도 남겨야 할 결정·사실을 적을 때. |
| schedule | 사용자가 자동화를 부탁한 정기 작업(cron)을 걸 때. |
| history | 지금 컨텍스트에서 빠진 옛 기록(관찰 전문)을 다시 볼 때. |
| snapshot | 파일 변경을 되돌리거나 변경 전 상태를 볼 때. |
| fetch | URL 하나를 읽을 때(HTML 은 마크다운으로). |
| message / reply | 다른 에이전트에게 보내기 / 받은 요청에 답하기. |

### 4.4 범위와 바뀌지 않는 것

- 속성이 도구에 붙으므로 모든 루프(main·서브·상주)에 같은 규칙. 프로파일 `allowed-tools` 와 직교.
- 꼬리(상태 블록·길 선택 줄·`## Live Agents`·`## Web Sources`), 디코딩 문법·검증·거절 문구는 그대로. 지연 여부는 런 시작에 정해지고 중간에 바뀌지 않는다.

## 5. 열린 점과 위험

- **35B 의 선택 실패** — 한 줄로는 드문 도구를 못 고를 수 있다. 벤치 9과제는 이 도구들을 거의 안 부르니 표적 과제(§6)로 잰다.
- **뼈대만 보고 잘못 부름(V2)** — monitor 의 `file`·`pattern`·`silent` 같은 의미는 인자 설명이 전부다. A5 자동 전문이 한 번 구제하지만 검증을 통과하는 의미상 오류는 못 잡는다. V1 은 이 위험이 없고 절감이 절반.
- **ask 의 안내 304 토큰**, **edit_file 안내 258 토큰** — 지연 대상이 아니라 별도 후속.
- **`tool_help` 결과 크기** — agent 전문 약 1,000 토큰. 한 번 읽히고 압축 때 빠지는 관찰이라 상주보다 싸다.

## 6. 측정 계획 (PR 2 머지 전, D5)

1. 토큰: 세 방언 전후(§2 표를 구현값으로 갱신).
2. 벤치: GLM 9과제 × 2회, 기준·후보 교차. 점수 드리프트와 첫 요청 토큰.
3. 표적 과제(지연 도구가 정답 경로, 각 5회, 기준·후보 교차): ① 두 파일을 두 에이전트에게(agent) ② 함수의 호출자 전부(code_index) ③ 로그에 특정 줄이 나타나면 알리기(monitor) ④ JavaScript 페이지에서 값 읽기(browser) ⑤ 결정을 메모리에 남기고 다음 턴에 쓰기(memory). 지표: 그 도구를 골랐나, `tool_help` 를 불렀나, 첫 호출이 검증을 통과했나, 턴 수.
4. V1 과 V2 를 둘 다 후보로 두고 ③ 에서 가른다. V2 가 ③ 을 통과하면 V2, 아니면 V1.

## 7. 구현 순서

1. **PR 1 (v10.109.0, 행동 불변)** — 이 문서의 §4 기반 전부: `Tool.deferred`·`summary`·`check_deferred`, `deferred_schema`·`tool_view`·`render_tool_block`, 렌더 두 곳의 공용 판정, `render_param_value` 의 enum 대체, `DEFER_PARAM_DESCRIPTIONS`(측정용 상수, V2 기본), `tool_help`(+ `RunContext.tool_guide`, `deferred` 게이트), A5 자동 전문, inventory 표본(`tool_help_result`·`tool_help_unknown`). **어떤 도구도 `deferred=True` 가 아니다** — 지연 경로는 가짜로 켠 도구로 단위 테스트(tests/test_tool_help.py).
   - 불변 검증: `python -m agent_cli.prompts.inventory -o` 를 origin/main(v10.108.0) worktree 와 이 커밋에서 떠서, `tool_help` 항목(도구 목록·`- tool_help:` 줄·native 함수·표본)과 작업 폴더 줄만 걷어 내고 비교 — 다섯 방언 × 네 시나리오의 절 131개·도구 목록·native 함수·실행 중 문구 전부 바이트 동일. 실제 루프는 지연 도구가 없어 `tool_help` 도 광고하지 않으므로(게이트) 모델이 보는 글은 종전과 완전히 같다. inventory 는 원래 게이트를 적용하지 않고 모든 내장 도구를 보이므로 `tool_help` 줄이 거기에만 나온다.
2. **PR 2 (V1 또는 V2 켬)** — D1 열한 개에 `deferred=True` 와 손으로 쓴 `summary`(영어, 표기 규칙), §6 측정 뒤 머지. 승자로 `DEFER_PARAM_DESCRIPTIONS` 를 고정하고 상수를 없앤다.

## 8. 결정 (사용자, 2026-11-05)

- **D1** 지연 집합 = agent, code_index, monitor, browser, schedule, history, fetch, snapshot, memory, message, reply (세션 5% 이하).
- **D2** 전문 경로 = `tool_help` 도구 + 스키마 불일치(A5) 거절에 자동 전문 한 번. (자동만 두는 대안은 모델이 미리 읽을 길이 없어 비권고.)
- **D3** V1/V2 둘 다 후보로 만들어 표적 과제 ③ 으로 정한다.
- **D4** 모든 루프 같은 규칙(도구 속성).
- **D5** PR 2 머지 전에 §6 전부(벤치 2회 + 표적 25런 × 2변형).
