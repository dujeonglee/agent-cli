# 멀티 dialect — 서버 네이티브 함수 호출 방언 `native_fc` (DESIGN)

> 상태: **승인 — 구현 진행 중** (2026-10-02). 결정: N1 가이드 전문을 `description` 에 그대로(문제 보이면 다듬기) · N2 관찰 렌더는 방언 소유(`render_observation_from_history`; 기본 user 텍스트, native 는 `tool` 역할) · N3 기본 json_fc 유지 · N4 v10.2.0, 브랜치 `native-fc`, PR + CI · 자동 감지 없음(명시 바인딩만) 선행: [DESIGN.md](DESIGN.md) 바인딩·해석 체인, [PHASE5.md](PHASE5.md) 스펙+엔진.
> 발단: 회사 게이트웨이(LiteLLM 프록시 + vLLM 류) 실측 — `tools` 가 없는 요청에서 `<tool_call>` 블록을
> 서버 파서가 떼어내 **폐기**(content 비고 `finish_reason: stop`, 토큰은 셈 → v10.1.7 `OUTPUT_SWALLOWED`),
> `tools` 를 주면 표준 채널 `delta.tool_calls` + `finish_reason: tool_calls` 로 돌려줌. json_fc 는 bare 배열이라 무사.

## 1. 동기와 범위

지금 모든 방언은 "LLM 과의 교환은 텍스트, 파싱은 하니스" 다. 서버의 tool-call 파서가 켜진 게이트웨이는
그 텍스트 블록을 가로채므로 태그 래퍼를 쓰는 방언 셋(xml_fc·hermes_json·glm_argkey)이 전부 죽는다.
`native_fc` 는 그 반대 계약이다: **요청에 함수 스키마(`tools`)를 싣고, 호출은 서버가 파싱한
`tool_calls` 로 받는다.** Qwen 류가 학습한 네이티브 포맷을 그대로 쓰므로 서버가 블록을 삼키는
환경에서도 돌고, 모델 프라이어와의 충돌도 없다.

범위 안: OpenAI 호환 프로바이더(스트리밍·비스트리밍), main·상주·일회성·스킬 루프(바인딩을 같이 탄다).
범위 밖(후속): Anthropic 프로바이더(`tool_use` 블록 — 형식이 달라 별도 매핑), 문법 강제(서버가 파싱하므로 없음),
등가성 코퍼스(원문이 없다).

## 2. 선택 방식 (결정 — 사용자 확인 완료: 명시 바인딩만, 자동 감지 없음)

- **바인딩은 명시, 감지도 하지 않는다.** 해석 체인(v10.3.0): `--dialect native_fc`(세션 전체 강제) >
  models.json `dialect`. 바인딩 없는 모델은 에러. 하니스가 서버를 프로브하거나 몰래 바꾸지 않는다(DESIGN D2 fail-fast 원칙;
  방언이 바뀌면 프롬프트·기록 내보내기·투명성이 달라져 사용자가 알아야 한다). `supports_native_tools` 같은
  capability 필드도 두지 않는다 — 서버가 지원하는지는 사용자가 안다.
- **안내만.** `OUTPUT_SWALLOWED` 경고(v10.1.7) 문구에 선택지 하나를 더한다: "json_fc 로 바인딩하거나, 서버가
  `tools` 요청에 `tool_calls` 를 돌려주면 `native_fc` 로 바인딩하라, 또는 서버 파서를 끄라".
- 보드 admin 드롭다운은 `list_names` 로 자동 반영(등록 방언 목록일 뿐 감지가 아니다), 📐 문법 배지는 이 방언에서 항상 꺼짐.

## 3. 계약 — 무엇이 텍스트에서 구조로 바뀌나

| 자리 | 텍스트 방언(지금) | `native_fc` |
|---|---|---|
| 도구 설명 | 시스템 프롬프트 `## Available Tools` + 인라인 가이드 | 요청 `tools[]`(JSON Schema). 시스템 프롬프트의 도구 목록·포맷 규칙 섹션은 **뺀다**(서버 템플릿이 넣어 중복 — 실측 90 → 341 토큰). 가이드·규율·환경 섹션은 유지 |
| 모델 → 하니스 | content 텍스트 → `parse_turn` | `tool_calls`(스트리밍은 v10.1.5 가 조각을 모음) → ops. content 는 thought |
| 기록 (history.jsonl) | `{thought, ops}` | **동일** `{thought, ops}` + op 마다 `call_id`(서버가 준 id, 없으면 합성 `call_<turn>_<i>`) |
| 기록 → 요청 메시지 | assistant: 캐노니컬 텍스트 재렌더 / 관찰: user 텍스트 | assistant: `{"role":"assistant","content":thought,"tool_calls":[{id,type:"function",function:{name,arguments(JSON 문자열)}}]}` / 관찰: op 마다 `{"role":"tool","tool_call_id":id,"content":관찰}` |
| 배치(한 턴 여러 op) | 배열 원소 여럿 | `tool_calls` 여럿 — OpenAI 규약이 허용 |
| 종결·가상 도구(`complete`·`ask`·`message`·`reply`·`answer`) | 도구 호출 | 같은 함수로 노출. `complete` 호출 = 런 종료(지금과 같은 의미론) |
| 산문만 온 턴 | NO_ACTION 넛지 | `tool_calls` 없고 content 만 → 같은 NO_ACTION 넛지(문구는 "함수를 호출하라") |
| 문법·구제·foreign 구제 | 있음 | 없음(서버 파싱). 서버 파싱 실패 신호: `finish_reason: length`·빈 `tool_calls`·arguments JSON 불량 → `SCHEMA_MISMATCH` 경로 재사용 |
| 원문 기록(`AGENT_CLI_RECORD_EMISSIONS`) | 모델 원문 | 없음 — 투명성 손실. `AGENT_CLI_DUMP_STREAM` 으로만 본다 |
| 🔍 인스펙터 | 시스템 프롬프트 섹션 + 대화 | 같은 뷰에 **`Function schemas` 그룹**(요청 `tools[]`, 함수마다 한 섹션, 토큰 합계 포함) 이 더해지고, assistant 카드는 `tool_calls` 를 `⚡ 도구 {인자}  (call id)` 줄로, 관찰은 `[tool call_<n>_<i>]` 라벨로 보인다(v10.2.1 — content 만 읽어 빈 카드였던 제보) |

관찰이 `tool` 역할로 가면 지금 user 역할 관찰 뒤에 붙던 꼬리(per-turn tail, `_OBS_COMPLETE_NUDGE`)의
자리가 문제다. OpenAI 규약상 `tool` 메시지 뒤에 바로 assistant 가 와야 하는 서버도 있으므로,
꼬리는 **마지막 `tool` 메시지의 content 끝**에 붙인다(지금도 마지막 메시지 본문 끝에 붙이는 규칙 — 자리만 같다).

## 4. 구현 — 닿는 곳

| 층 | 변경 | 예상 |
|---|---|---|
| `dialects/specs/native_fc.py` | `DialectSpec` 하나: `args=JSON_NATIVE`, 산문은 "도구는 API 함수 호출로 부른다; 텍스트 블록을 쓰지 말라" + 종결 안내. `exposes_complete=True`. 새 플래그 `server_parsed=True` | ~60줄 |
| `dialects/engine.py` | `server_parsed` 분기: `format_rules()` 가 도구 목록 섹션을 생략하라는 신호를 `system_prompt` 에 주고, `render_assistant_from_history` 가 구조화 dict(`tool_calls`)를 돌려주며, `parse_turn` 은 json_fc 와 같은 bare 배열 파서(서버 `tool_calls` → core 가 배열 텍스트로 렌더해 넘기는 v10.1.4 경로 재사용). `grammar()` 는 None | ~80줄 |
| `dialects/spec.py` | `server_parsed: bool = False` | 5줄 |
| `providers/openai.py` | `CallSettings.tools`(함수 스키마 목록) → body `tools`, `tool_choice: "auto"`. `tool_calls` 수집은 v10.1.5 | ~40줄 |
| `providers/base.py` | `CallSettings.tools: list[dict] | None` | 5줄 |
| `tools/registry.py` | `openai_function_schemas(tool_names)` — `TOOL_SCHEMAS[n].parameters` + 설명(가이드 첫 문단) → OpenAI 함수 형식 | ~40줄 |
| `loop/llm.py` | 방언이 `server_parsed` 면 `settings.tools` 채움, 문법 비활성 | ~20줄 |
| `loop/core.py` | `response.tool_calls` 를 배열 텍스트로 렌더(v10.1.4 `_render_server_tool_calls`) — 이미 있음. `call_id` 를 op 에 보존해 레코드로 | ~30줄 |
| `context/render.py` | `_to_natural_language`: 방언이 `server_parsed` 면 관찰을 `tool` 역할 + `tool_call_id` 로, 같은 턴의 op 여러 개 → 관찰 여러 개 매핑(지금 배치 관찰은 `[k/N]` 한 덩어리 — op 별 분할 필요) | ~120줄 |
| `loop/dispatch.py` `_append_observation` | 배치 관찰을 op 별 레코드로 쪼개 `call_id` 를 심는다(텍스트 방언은 종전처럼 한 덩어리로 합쳐 내보냄) | ~60줄 |
| `prompts/system_prompt.py` | `server_parsed` 면 `Available Tools`·포맷 규칙 섹션 생략 | ~20줄 |
| `main.py` / 보드 | 없음(바인딩 체인·드롭다운 자동). 경고 문구 분기 `loop/core.py` | ~10줄 |
| 테스트 | 스펙 표(라운드트립·배치·종결), 프로바이더 `tools` 전송·수집, 기록 왕복(resume 포함, 옛 세션 `call_id` 합성), 압축 요약 호환, 시스템 프롬프트 섹션 생략 | ~350줄 |
| 문서 | README(바인딩·언제 쓰나·투명성 손실), ARCHITECTURE | — |

합계 ~800줄(테스트 포함). 큰 되돌림 없음 — 전부 분기 추가.

## 5. 결정 (확정)

- **N1 가이드의 자리.** 함수 `description` = 한 줄 설명 + 인라인 가이드 **전문**(지금 `## Available Tools` 와 같은 정보량).
  토큰이 늘지만 행동 변화가 가장 적다. 문제가 보이면 그때 다듬는다.
- **N2 관찰의 역할.** 루프에 분기를 두지 않고 **방언이 관찰 렌더를 소유**한다: `DialectBase.render_observation_from_history`
  (기본 = 지금의 user 텍스트) 를 `native_fc` 만 `tool` 역할(`tool_call_id`)로 덮어쓴다. "전부 `tool` 로 통일" 은 거부 —
  OpenAI 규약상 `tool` 은 앞 assistant 의 `tool_calls` 에 대한 응답이어야 해 텍스트 방언에서는 400(정식 API·엄격한
  jinja 템플릿), Anthropic 은 `tool_result`/`tool_use` 짝이라 불가.
- **call_id 는 기록에 두지 않는다.** 렌더 시점에 결정적으로 합성(`call_<레코드 index>_<op index>`) — assistant 와 뒤따르는
  관찰이 같은 규칙을 쓰므로 짝이 맞고, 서버가 준 id 를 되돌릴 필요는 없다(대화 안에서 일관되면 된다). 기록 스키마 무변경.
- **배치 관찰은 op 별 조각을 항상 저장(방언 중립, v10.2.2).** 관찰 레코드에 `parts: [{tool, success, content}]` 를 더한다 — 어느 방언으로 기록했든 같다(v10.2.0 은 native 에서만 저장해 json_fc 세션을 native 로 이으면 호출·결과 짝이 안 맞았다; 렌더 추상화는 저장 때 버린 경계를 되살릴 수 없다). 텍스트 방언은 렌더에서 무시,
  native 는 op 마다 `tool` 메시지 하나). 종전 합친 본문 `content` 는 그대로 — 웹 재생·텍스트 방언 호환.
- **N3 기본값.** 없음 (v10.3.0 — 라이브러리 내부 `DEFAULT_DIALECT = json_fc` 는 생성자·테스트용). 모든 방언이 바인딩으로만.
- **N4 릴리스.** MINOR v10.2.0 한 번. 브랜치 `native-fc`, PR + CI. 프로바이더는 OpenAI 호환만 — Anthropic 바인딩이면 부트에서
  fail-fast("native_fc 는 OpenAI 호환 프로바이더에서만").

## 5.1 v10.20.0 — 되먹임 검토에서 고친 것

컨텍스트에 남는 모양을 실제 세션(호출 메시지 520개)으로 다시 렌더해 검토했다.

- **N5 모든 호출에 결과.** OpenAI 규격은 `tool_calls` 마다 같은 id 의 `tool` 메시지를 요구하는데 기록은 그렇지 않은
  턴을 만든다 — 같은 파일 편집 N 개는 한 번에 적용돼 결과가 하나(병렬 배치·중단된 배치의 남은 호출도 같다), `complete` 는
  결과가 없다(실측: 520 중 25). 엄격한 서버는 400, 너그러운 서버에서도 모델은 "불렀는데 결과가 없는 호출" 을 본다.
  `DialectBase.pair_call_results` (기본 통과) 를 `ContextManager.get_messages` 가 마지막에 부르고, native 는 빠진 id 에
  채운다: 합쳐진 호출 → "다른 호출과 함께 처리됨", 종결 호출 → `completed task: <그 턴이 답한 요청 첫 줄 80자>`.
  기록 무변경 — 옛 세션도 읽을 때 맞는다. 종결 문구는 실측(6턴 대화 × 12~20, "호출 없이 산문만" 거부율)으로 골랐다:
  없음 10.0% · "Delivered to the user." 17.5% · 지난 최종답을 산문 assistant 로 71%(모델이 그대로 따라 함 — 탈락) ·
  "completed task" 11.1% · "completed task: <요청>" 9.7%(사용자 제안, 채택).
- **N6 호출은 다시 파싱하지 않는다.** 종전엔 서버의 `tool_calls` 를 flat op 배열 **텍스트**로 바꿔 산문 뒤에 붙이고
  `parse_turn` 에 넣었다 — 산문(content)에 `[{"action": …}]` 이 있으면 "첫 배열이 이긴다" 규칙으로 그쪽이 호출이 되고
  실제 호출은 버려졌다(재현; 실측 1,651턴에서 발생 0). 이제 `Dialect.ops_from_server_calls` 가 호출 목록에서 op 를 바로
  만들고 루프는 `server_ops` 로 받는다(산문은 생각으로만). 기록은 `corrected_record` 로 ops shape 를 쓴다.
- **N7 깨진 인자는 실행하지 않는다.** provider 는 JSON 이 아닌 인자를 빈 dict 로 바꿨다 — 모델은 "필수 인자 없음" 이라는
  엉뚱한 안내를 받고, `complete` 면 **런이 답 없이 끝났다**(재현). 이제 `{"input": None, "arguments": <원문>}` 으로 넘기고
  op 의 `action_input` 이 문자열이면 dispatch 가 "인자가 JSON 이 아님(어디서)" 을 알린다(배치면 그 자리에 메모, 홀로면
  형식 개입; `SCHEMA_MISMATCH`). 텍스트 방언의 서버-삼킴 구제 경로(`_render_server_tool_calls`)는 종전대로.
- **N8 스트림 오류.** omlx 는 오류를 `data: {"error": …}` 한 줄로 보낸다. `incomplete_tool_call` 은 잘림(v10.19.1), 그 밖은
  `server_error` stop_reason + 서버 문구(stop_detail) — 모델은 "응답이 비었다" 대신 서버의 말을 받는다(`NO_OUTPUT` 행에
  stop_reason/stop_detail 로 구분).
- 검토에서 **문제 아님**으로 확인한 것: 배치 안의 거부(깨진 인자·인자 누락·모르는 도구)는 자기 호출 id 에 붙는다(회귀
  테스트); 호출 id 가 캐시 위치 기반이라 압축으로 바뀌어도 그 지점부터는 어차피 내용이 달라 추가 비용 없음. **남겨 둔 것**:
  하니스 꼬리말(세션 상태·complete 안내)이 마지막 `tool` 메시지 본문 끝에 붙는다 — native 는 뒤에 user 메시지를 둘 수
  있으므로 분리가 가능하나 행동 변화라 측정 뒤에.

## 6. 합격선

1. 로컬 omlx 가 `tools` 를 지원하면: 끝말잇기·실제 작업 6태스크를 `native_fc` 로 완주, NO_ACTION 외 신호 0.
2. 회사 게이트웨이: `OUTPUT_SWALLOWED` 0, 같은 작업이 json_fc 와 같은 턴 수 안에서 완주(사용자 실측).
3. 기록 왕복: `native_fc` 세션을 resume 해 이어가고, 같은 세션을 `--dialect json_fc` 로 resume 해도 깨지지 않음(레코드 스키마 동일).
4. 텍스트 방언 무변경: 등가성 코퍼스 849건·유닛 전부 그대로.
