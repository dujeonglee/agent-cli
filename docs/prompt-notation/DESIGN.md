# 모델이 보는 글의 표기 규칙 (v10.77.0, 2026-10-28 사용자 결정)

## 규칙

| 무엇 | 모양 | 예 |
|---|---|---|
| 도구 이름 | 백틱 | `` `read_file` `` |
| 인자 이름 | 백틱 | `` `line_start` ``, `` `pos` `` |
| enum 값·문자 그대로의 값 | 큰따옴표 | `"replace"`, `"run"`, `"add"` |
| 호출 모양 (`mode=get`, `read_file(path=…)`, `mode:"run"`) | 쓰지 않음 | "`memory` with `mode` "get"" 처럼 문장으로 |
| 대문자 강조 (`ONE`, `NOT`, `VERBATIM`, `FAILURES`) | 쓰지 않음 (v10.81.0, 사용자 결정 2026-10-30) | 강조는 문장 구조로; 머리글자(`JSON`·`URL`)·SQL 예시 줄·`LINE#HASH`·밑줄 자리표시(`TOOL_NAME`)만 예외 |

적용 범위는 모델이 보는 글 전체 — 도구 설명·인자 설명·사용 안내, 시스템 프롬프트 절, 꼬리
문구(압축 예고·Session Memory 등), 넛지·오류 문구(중앙 검증 `Missing required field(s) for
`x`: `p``, `Unknown tool `x``, 형식 오류의 "You sent:" 줄, 반복 호출 넛지).

## 왜

- 인자를 그냥 단어로 쓰면 `text`·`id`·`key`·`file`·`run` 같은 이름이 보통 단어와 구별되지 않는다.
  edit_file 설명이 replace·append·prepend·delete 를 설명하면서 그것이 `op` 의 값이라는 말이 없던
  것이 계기(사용자 지적).
- 도구마다 제각각이었다: 일부만 백틱, 대부분 맨 단어, 여섯 도구는 호출 모양 조각(`mode=get`,
  `background=true`, `query='…'`)까지 — 호출 모양은 v10.75.1 의 "글로 호출을 쓰지 않는다" 규칙에도
  어긋났다.
- 값은 JSON 에서 값이 따옴표로 오는 모양과 같고, 인자(백틱)와 한눈에 구별된다.

## 예외 (규칙 밖)

- 형식이 보여 주는 **호출 예시** — JSON 배열, `<tool_call>` 블록, `` `tool` {…} `` (v10.75.1 방언별 예시).
- 줄 머리 **이름표** — `read_file failed:`, `edit_file apply failed:`, `[shell: output is …` 같은 오류·결과 머리.
- 형식 규칙 본문(`Response Format`)의 문법 설명 — 이미 규칙대로다.
- 단어형 도구 이름(`shell`·`message`·`reply`…)은 영어 단어와 겹쳐 기계 검사가 못 가린다 —
  밑줄 있는 이름만 가드가 잡고, 단어형은 사람이 본다.

## 바뀐 것 (요지)

- 도구 22개 설명·인자 설명, code_index·agent·ask 안내, Task Guidelines(`background` true),
  Agent Profiles·Skills 절, 압축 예고·Session Memory 머리, snapshot 안내, 브라우저 결과·오류,
  중앙 검증 문구, "You sent:" 줄(`` You sent: `edit_file` with `op` "replace", … — missing: `path`. ``),
  반복 호출 넛지(`` You have called `shell` with {…} N times in a row `` — 접기 판정 접두 "You have
  called" 는 그대로), monitor 오류가 모델이 모르는 이름(`match`/`silence`)을 가리키던 것을 조건 쌍으로.
- 골든 말뭉치: edit_golden 139건·ledger_golden 6건이 표기만 바뀌었다(백틱·따옴표를 지우면 바이트
  동일, 성공·결과 파일 불변) — 다시 얼렸다.

## 가드

`tests/test_prompt_notation.py` — inventory 의 글 전부에서 밑줄 있는 도구 이름이 백틱 밖에 있거나
호출 모양이 있으면 실패. **v10.81.0**: 세 글자 이상의 대문자 낱말이 머리글자 허용 목록(`_ACRONYMS`) 밖이면
실패(`test_no_caps_emphasis`). 처음 돌렸을 때 133건(도구 설명 12종·방언 형식 규칙 5종·Context Discipline·
Environment·Agent Profiles 설명·넛지)이 걸려 전부 소문자로 바꿨다 — 뜻은 그대로, 강조만 뺐다. 내장
에이전트·스킬의 **본문**(`agents/builtin/*.md` 의 "do NOT"·"ask ONE focused question")은 inventory 밖이라
가드가 보지 않는다 — 설명(description)만 고쳤다. 측정은 사용자 지시대로 다른 변경과 함께 전체 벤치에서.
