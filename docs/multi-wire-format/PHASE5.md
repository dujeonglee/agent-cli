# 멀티 wire-format — Phase 5: 파서를 코드에서 데이터로 (스펙 + 엔진) · `dialects` 로 개명 (DESIGN)

> 상태: **승인 — 구현 진행 중** (2026-10-01). 결정: D1 이름 `dialects` · D2 기본 `json_fc` 유지 · D3 새 스펙은 파서·문법 테스트만("실모델 미검증" 표시) · D4 태그 구제 유지(`recovery/tagged`) · D5 v10.0.0 한 번에 · D6 PR + CI 후 일반 머지
> 선행: [DESIGN.md](DESIGN.md) P1~P3 바인딩·해석 체인·foreign 구제, [PHASE2.md](PHASE2.md) xml_fc·bakeoff,
> [PHASE4.md](PHASE4.md) md_array → json_fc. 외부 근거: 툴콜 포맷 조사 리포트(2026-10-01,
> vLLM structural-tag · llama.cpp autoparser(PR #18675) · HF `response_template`).
> 브랜치: `dialects`. 머지 조건: §7 합격선 전부 통과 + CI 초록 + 라이브 bakeoff.

## 1. 동기

생태계 조사의 결론 하나가 이 설계의 출발점이다. 추론 서버 셋(vLLM·SGLang·llama.cpp)과
HF transformers 가 2026 년 들어 **툴콜 파서를 코드가 아니라 데이터로** 옮기고 있다 —
llama.cpp 는 모델별 파서를 전부 걷어내고 Jinja 템플릿 차분 분석으로 PEG 파서와 GBNF 를
자동 생성하고(2026-03), vLLM 은 `structural_tag_registry`, HF 는 `response_template`.
공통 추상화는 네 축이다: **시작 트리거 · 호출 단위 구분자 · 이름 위치 · 인자 포맷**.

우리 쪽 현실:

- 와이어 포맷 하나 = 모듈 800~1,000줄(`xml_fc.py` 802, `json_fc.py` 995). self-contained
  불변식 때문에 포맷마다 파서·문법·산문·구제·history 왕복을 전부 다시 쓴다.
- `json_fc.py` 의 **절반(157–597행, ~440줄)은 포맷이 아니라 JSON 수리 기계**다
  (`_extract_first_json`, `_merge_reopened_op_arrays`, `_repair_anonymous_op_objects`,
  `_extract_op_json`, stage 판정). 그 하층(`_json_repair.py` 241, `_json_diag.py` 76)은
  따로 떨어져 있다. Hermes 가족(`<tool_call>{json}</tool_call>`)을 추가하면 같은 수리
  기계가 또 필요하다.
- 조사에서 확인한 평문 태그 가족(Hermes JSON, GLM `<arg_key>`)은 2026 년 가장 많이
  쓰이는 오픈 모델군의 네이티브 프라이어인데 우리에겐 없다. 손 코딩으로 추가하면
  각 600~800줄.
- 파서 버그 수정(인라인 코드 비호출 v9.24.8, 사고 구간 금지, truncation 전파)이 포맷마다
  따로 들어간다.

목표: **스펙(데이터) 하나 + 엔진 하나**. 새 방언 = 스펙 40~60줄 + 산문 조각. 기존 두
포맷은 스펙으로 다시 표현되고 옛 모듈은 등가성 합격 후 삭제된다. 패키지 이름은 실제
역할(모델의 턴을 쓰고·가르치고·읽는 방식 = 방언)에 맞게 `dialects` 로 바꾼다.

## 2. 비목표

- **특수 토큰 가족**(DeepSeek `<｜tool▁calls▁begin｜>`·DSML, Kimi K2/K3, Mistral
  `[TOOL_CALLS]`, gpt-oss Harmony, Gemma 4 `<|tool_call>`): OpenAI 호환 **텍스트** API
  너머에서는 서버가 특수 토큰을 지우거나 모델이 텍스트로 낼 수 없다. 스펙 스키마가
  표현할 수는 있어도 라이브로 검증할 수 없으므로 범위 밖.
- 모델 동봉 Jinja 템플릿에서 스펙을 **추출**하는 가져오기(llama.cpp 방식): v2 후보.
  이번엔 스펙을 손으로 쓴다.
- 관찰(툴 결과)의 모양: 우리는 관찰을 user 메시지로 직접 넣으므로 모델 템플릿의
  "결과 구문" 축은 해당 없음. `Observation:` 산문 유지(PHASE2 D6).
- op 의미론(`complete` 마지막, 빈 값 금지, 같은 파일 edit 배치, 배치 안 실패 처리,
  A4/A5 검증, 확인·훅): 전부 dispatch 층. 스펙과 무관하게 그대로.
- 서버 `tools` API 로 전환(서버 파서에 맡기기): 하니스의 전제(텍스트 한 줄 제어)를
  바꾸는 별도 결정.

## 3. 현재 배관 (실측, 2026-10-01 main)

### 3.1 `WireFormat` ABC 표면 (`base.py` 668줄)

| 묶음 | 멤버 | 스펙에서 유도되나 |
|---|---|---|
| 식별·정책 | `name`, `multi_op`(둘 다 True), `exposes_complete`, `action_required` | 스펙 필드 |
| 모양 | `render_full_example`, `render_action_input`, `prefill`(둘 다 "") | **유도** |
| 파싱 | `parse_turn`(1차), `parse`(첫-op 투영; 둘 다 오버라이드), `strip_thinking`(ABC 공용), `thinking_stop`, `is_degenerate`/`degeneration_trigger`, `sanitize_thought` | 엔진 + 스펙 필드 |
| 구제 문구 | `constraint_reminder_call`, `constraint_reminder_action_required`, `failure_framing_parse_fail`, `no_action_detail`, `static_retry_hint_no_json`, `static_retry_hint_no_action`, `diagnose_syntax_error`, `system_user_prefixes` | 산문 조각 + 공용 틀 |
| 프롬프트 | `format_rules` | 산문 조각 + `render_full_example` |
| 문법 | `grammar(tools, thinking_open)` | **유도**(`grammar.py` 조각 재사용) |
| history | `serialize_assistant_for_history`, `serialize_terminal_for_history`, `render_assistant_from_history` | 기본 구현 + `render` |
| provider | `provider_call_kwargs`(둘 다 `{}`) | 기본 |

### 3.2 두 포맷이 각자 가진 것

- **xml_fc** — `_PARAM_CLOSED`(키-이름 closer `</KEY>` 수용), 후보 검증 ①세그먼트 자격
  ②펜스·인라인 코드 인용 제외(v7.28.1/v9.24.8), lenient 구제(`<X>`·`<k>` 붕괴 변종,
  라인-단독 등록 도구명 앵커, 값 무결성 우선 closer 판정 v7.11.4), `_trim_block`,
  스키마-주도 타입 복원(`_coerce_params`), truncation 표시, 문법(`body`/`body_nb` 규칙,
  사고 구간 `<tool_call>` 금지).
- **json_fc** — 수리 기계(§1), md_array 헤더 관용(`_split_sections`, `_INPUT_RESIDUE`),
  truncation = `close_unbalanced` 증거일 때 **마지막 op 에만**, 문법(`JSON_RULES`,
  `json_value_rule`, prose 가 `[` 를 못 품음 — `after_blank_line`).
- 공용 조각(`grammar.py` 430줄): `not_containing`, `prose_rule`, `think_prefix(forbid)`,
  `grammar_params`(enum/text_nonempty/forced), `tool_params_expr`, `call_sequence_rules`
  (종결 도구 마지막), `json_value_rule`, `JSON_RULES`.

### 3.3 레지스트리·해석·구제 (`__init__.py` 245줄, `dispatch.py`)

- `register(instance)` 이름 충돌 fail-loud, `get(name)`, `list_names()`,
  `DEFAULT_WIRE_FORMAT = "json_fc"`.
- `resolve_wire_format(explicit, session_format, model)`: `--response-format` > resume
  세션 메타 `response_format` > `models.json` 엔트리 `wire_format` > 기본.
- `try_foreign_parse(bound, text)`: 바인딩이 0-op 일 때 등록된 다른 포맷을 **기본 먼저,
  이름순**으로 시도, `parse_stage ∈ {1,2}` + action-보유 op 면 채택 →
  dispatch 가 `corrected_record` 로 바인딩 포맷 캐노니컬 shape 재렌더.
- `all_system_user_prefixes()` = 포맷 무관 접두 + 각 포맷의 `system_user_prefixes()`.

### 3.4 외부 접점

| 접점 | 위치 | 비고 |
|---|---|---|
| CLI `--response-format` | `main.py:1283, 2089` | 해석 체인 최우선 |
| 세션 메타 `response_format` | `context/session.py:55,71`, `main.py:1345,1890` | resume 근거 — **영구 읽기 호환 필요** |
| `models.json` 키 `wire_format` | `config.py:126`(손 추가 키 보존), `wire_format_for_model` | agent-board admin 드롭다운이 씀 |
| agent-board | `admin.py:245 list_wire_format_names` → `from agent_cli.wire_formats import list_names`; `app.py:490 view["wire_formats"]`; README | import 경로가 계약 |
| 테스트 | `xml_fc`/`json_fc` 참조 39파일, `parse_turn` 직접 호출 11파일, 스냅샷 `tests/snapshots/tools_section_json_fc.txt` | 산문 바이트 동일이면 무변경 |

### 3.5 재생 코퍼스

- `data/ws/*/…/history.jsonl` 10개, assistant 레코드 456건(xml_fc·json_fc 혼재, 라이브
  Qwen 출력 — 드리프트·구제 사례 포함).
- `tests/` 의 파서 테스트 입력(두 포맷 합쳐 수백 건: lenient 변종, 펜스 인용, truncation,
  md_array 잔재, 깨진 JSON).
- 2026-07-17 bakeoff 캡처(PHASE2 §8)가 남아 있으면 추가.

## 4. 설계

### 4.1 `DialectSpec` — 네 축 + 우리 축

```python
class ArgStyle(Enum):
    TAGGED = "tagged"            # <parameter=k>v</parameter>            (xml_fc, Nemotron 3, Granite 4.2)
    TAGGED_PAIR = "tagged_pair"  # <arg_key>k</arg_key><arg_value>v</arg_value>   (GLM 4.5+)
    JSON_IN_TAG = "json_in_tag"  # <tool_call>{"name":…,"arguments":{…}}</tool_call> (Hermes, Qwen2.5/3)
    JSON_NATIVE = "json_native"  # [{"action":…, …}]  태그 없음                (json_fc)

class NameSlot(Enum):
    OPEN_TAG = "open_tag"        # 여는 태그 안: <function=NAME>            (xml_fc)
    BODY_HEAD = "body_head"      # 호출 본문 첫 토큰: <tool_call>NAME\n…      (GLM)
    JSON_KEY = "json_key"        # JSON 객체의 키: "name" 또는 "action"       (Hermes, json_fc)

@dataclass(frozen=True)
class DialectSpec:
    name: str                                   # 레지스트리 이름 (= 옛 wire_format 이름)
    # ── 네 축 ──
    section: tuple[str, str] | None = None      # 섹션 래퍼 (MiniMax 류). 없으면 호출이 곧 단위
    call: tuple[str, str] | None                # 호출 단위 (xml/hermes/glm: <tool_call>…</tool_call>; json_native: None)
    name_slot: NameSlot
    name_wrap: tuple[str, str] = ("", "")       # OPEN_TAG 일 때 ("<function=", ">") 와 닫기 "</function>"
    args: ArgStyle
    param: tuple[str, str] = ("", "")           # TAGGED: ("<parameter={k}>", "</parameter>"); TAGGED_PAIR: ("<arg_key>{k}</arg_key><arg_value>", "</arg_value>")
    value_mode: str = "raw"                     # raw(태그 안 원문, 스키마로 타입 복원) | json(JSON 값)
    op_shape: str = "name_arguments"            # JSON 계열: "name_arguments" | "flat_action"
    # ── 우리 축 ──
    thought: str = "prose_before"               # 첫 트리거 앞 산문 = thought (PHASE2 D4). 다른 값 없음(예약)
    terminal_op: str = "complete"
    prose_opener: str                           # 문법 prose 가 품지 못할 여는 시퀀스 ("<tool_call>" / "[")
    prose_after_blank_line: bool = False        # json_fc 의 "\n\n[" 규약
    degeneration_trigger: str                   # "<" / "#"  (P0-4 조기종료 게이트)
    thinking_stop: re.Pattern | None            # 미닫힘 <think> 의 정지점 = 첫 구조 마커
    lenient: Lenient = Lenient()                # 아래
    prose: Prose                                # 산문 조각 (§4.6)

@dataclass(frozen=True)
class Lenient:
    tag_name_variants: bool = False   # <X>/<k> 붕괴 변종 구제 (xml_fc 실측 83%) — 등록 도구명 라인 앵커
    key_named_closer: bool = False    # </KEY> 로 닫는 혼합 스타일 수용
    inline_quotes_exclude: bool = True  # 펜스·인라인 코드 안의 후보는 인용 (v7.28.1 / v9.24.8)
```

네 축으로 부족한 자리 셋(GLM 쌍 태그, DSML 속성, MiniMax M3 키=요소명)은
`TAGGED_PAIR` 와 `param` 템플릿으로 둘을 덮고, 세 번째는 특수 토큰 가족(비목표)이라
넣지 않는다.

### 4.2 네 스펙

| | `json_fc` | `xml_fc` | `hermes_json` (신규) | `glm_argkey` (신규) |
|---|---|---|---|---|
| section | — | — | — | — |
| call | — (bare 배열) | `<tool_call>` `</tool_call>` | `<tool_call>` `</tool_call>` | `<tool_call>` `</tool_call>` |
| name_slot | JSON_KEY `action` | OPEN_TAG `<function=NAME>` … `</function>` | JSON_KEY `name` | BODY_HEAD (`<tool_call>NAME`) |
| args | JSON_NATIVE | TAGGED `<parameter=k>…</parameter>` | JSON_IN_TAG (`arguments` 객체) | TAGGED_PAIR `<arg_key>k</arg_key><arg_value>…</arg_value>` |
| value_mode | json | raw | json | raw |
| op_shape | flat_action | — | name_arguments | — |
| prose_opener | `[` (after_blank_line) | `<tool_call>` | `<tool_call>` | `<tool_call>` |
| degeneration_trigger | `#` → **`[`** (md_array 헤더 관용 삭제로 시그니처가 바뀜 — §4.9) | `<` | `<` | `<` |
| lenient | 없음 | tag_name_variants + key_named_closer | 없음 | key_named_closer(`</arg_key>` 생략 변종은 미실측 — 꺼둠) |
| 네이티브 프라이어 | (우리 고유) | Qwen3-Coder, Qwen3.5/3.6/3.8, Nemotron 3/3.5, Granite 4.2 | Qwen2.5/3/Next, Hermes 2~4, Granite 4.0/4.1, SmolLM3 | GLM-4.5 ~ 5.3 |

호출 한 건의 캐노니컬 렌더(엔진 `render_call(name, params)`):

```
xml_fc        <tool_call>\n<function=read_file>\n<parameter=path>\nsrc/main.c\n</parameter>\n</function>\n</tool_call>
hermes_json   <tool_call>\n{"name": "read_file", "arguments": {"path": "src/main.c"}}\n</tool_call>
glm_argkey    <tool_call>read_file\n<arg_key>path</arg_key>\n<arg_value>src/main.c</arg_value>\n</tool_call>
json_fc       [{"action": "read_file", "path": "src/main.c"}]            (한 턴 = 배열 하나, op 반복은 원소)
```

멀티-op(사고 1개 + 호출 여러 개)는 네 스펙 모두 **기본형**: 호출 단위 반복(태그 셋) 또는
배열 원소 반복(json_fc). 문법은 `call_sequence_rules` 공용(종결 op 마지막).

### 4.3 엔진 `Dialect(WireFormat)` — 파이프라인

```
parse_turn(text)
  0. strip_thinking (ABC 공용)                                   — 변경 없음
  1. 인용 영역 계산: 균형 ``` 쌍 + 인라인 `…` (lenient.inline_quotes_exclude)   — xml_fc 의 _fence_spans 를 엔진으로
  2. 트리거 탐색: call[0] (또는 json_native: 줄머리 '['/'{') — 인용 영역 밖, 산문 뒤
     · 0건 → lenient 층(tag_name_variants 가 켜진 스펙만) → 그래도 0건이면 thought-only / stage 0
  3. 호출 분할: call[0]…call[1] 세그먼트 (section 이 있으면 그 안에서), EOF 까지 열린 세그먼트 = truncated
  4. 이름: name_slot 에 따라 — OPEN_TAG 정규식 / BODY_HEAD 첫 토큰 / JSON_KEY (디코드 뒤)
     · 세그먼트 자격(①): 이름 뒤에 param/closer 가 와야 호출, EOF 세그먼트는 자격 유지 (A5 진단 보존)
  5. 인자 디코드:
     · TAGGED / TAGGED_PAIR → param 템플릿 정규식 (key_named_closer 옵션), _trim_block, raw 값
     · JSON_IN_TAG / JSON_NATIVE → json_recovery.decode(text, expect=…) → (obj, repaired, diag)
  6. 타입 복원: value_mode=raw 면 스키마가 string 이 아닌 param 만 JSON parse 시도 (_coerce_params 그대로)
  7. stage: 캐노니컬 1 / 수리·lenient·drift 2 / 0 (ops 없음) · truncated 는 json: close_unbalanced 증거 시 마지막 op, tagged: EOF 세그먼트
  → ParsedTurn(thought, ops, terminal, raw, parse_stage, thinking)
```

- 2단계 "산문 뒤" 는 `thought = prose_before` 하나뿐이라 조건이 없다. 예약 필드.
- xml_fc 의 후보 검증 ①②, lenient 값-무결성 closer 규칙(v7.11.4), `_trim_block` 은 **엔진 코드로
  승격**하되 스펙 옵션으로 켜고 끈다. 옵션이 꺼진 스펙에서는 코드 경로가 실행되지 않는다.
- `parse()`(첫-op 투영)는 두 포맷이 "역사적 투영 바이트 동일" 을 위해 오버라이드했다.
  엔진은 기본 구현(ABC)으로 통일하고, 등가성 코퍼스에서 두 포맷의 옛 `parse()` 와 비교해
  차이가 있으면 그 차이를 §7 에 기록하고 결정한다(사용처는 history 직렬화 기본과 테스트뿐).

### 4.4 `recovery/` — 구제는 한 패키지, 기계는 둘

JSON 수리와 태그 변종 구제는 **같은 역할**(드리프트한 출력을 2단계로 ops 에 되살림)이지만
코드 공유가 0 인 다른 기계다. 한 파일로 합치지 않고 패키지로 묶는다.

```
dialects/recovery/
  __init__.py   공용 앞단: quote_spans(text) — 균형 ``` 쌍 + 인라인 `…` 인용 영역(v7.28.1/v9.24.8),
                세그먼트 자격(이름 뒤에 param/closer, EOF 세그먼트는 자격 유지 — A5 진단 보존),
                Recovered(value, repaired, truncated_evidence, diag) 결과 타입, stage 정책 상수
  json.py       ← `_json_repair.py` + `_json_diag.py` + `json_fc.py` 157–597 (이동, 무수정)
                decode(text, *, expect="array"|"object") -> Recovered ; describe_error(json_text)
  tagged.py     ← `xml_fc.py` 의 `_PARAM_CLOSED`(키-이름 closer), lenient 오픈/값-끝 폴백(v7.11.4)/
                `_trim_block`, 등록 도구명 라인 앵커(`_lenient_tool_open_re`)
                extract_params(segment, *, param_open, closers, key_named_closer) ; lenient_calls(text, tool_names)
```

- 엔진은 `ArgStyle` 로 고른다: `JSON_*` → `recovery.json`, `TAGGED*` → `recovery.tagged`.
  스펙의 `Lenient` 옵션은 `recovery.tagged` 의 어느 규칙을 켤지 정하는 스위치(D4 = 유지,
  xml_fc 전용 코드가 아니라 태그 가족 공용).
- 인용 영역 제외가 공용 앞단이 되면 json_fc 에도 같은 규칙이 걸린다(지금은 `_op_anchor`/
  `_op_signature` 로 비슷한 일을 따로 함). 등가성 코퍼스에서 차이가 나면 §10 에 올린다.
- `diagnose_syntax_error` 는 `recovery.json.describe_error` 를 쓴다.

### 4.5 문법 생성 (`Dialect.grammar`)

```
root  ::= {think_prefix(forbid=(call_open,))}( calls | prose SEP calls | prose ) ws
prose ::= prose_rule(prose_opener, after_blank_line)
calls ::= call_sequence_rules(names, call=<스타일별 래퍼>, sep=<스타일별>)
tool_X ::= <name_slot 렌더> <tool_params_expr(items, forced)> <닫기>
```

- items 는 ArgStyle 별 한 줄: TAGGED `"<parameter=k>" body "</parameter>"`, TAGGED_PAIR
  `"<arg_key>k</arg_key><arg_value>" body "</arg_value>"`, JSON `"\"k\"" ":" json_value_rule`.
- `body`/`body_nb`(`not_containing(closer)`), enum/`text_nonempty`/forced 는 `grammar_params`
  공용 — v9.24.4/v9.24.6 의 빈 값 금지·종결 마지막은 그대로.
- `think_prefix(forbid=(call_open,))` — xml_fc 만 걸던 사고 구간 금지(v9.24.8)를 **태그가
  있는 스펙 전부**에 건다(json_native 는 opener 가 `[` 라 걸지 않음, 종전과 동일).
- 합격선: 옛 두 포맷의 EBNF 와 **문자열 동일**을 목표로 하되, 규칙 이름·순서 차이는
  허용하고 수용/거부 집합(`tests/test_decoding_grammar_accept.py` 코퍼스)과 실제 토크나이저
  토큰당 비용(gbench)으로 판정한다.

### 4.6 산문 — 생성하지 않고 조각으로 둔다

`format_rules` 와 구제 문구 6종은 벤치로 다듬은 말이다(v6.0.0 bakeoff 140런, 배치 중첩
27B 90% 파손 실측). 스펙에서 **생성하지 않는다**. `Prose` 는 조각 묶음이고 공용 틀이
자리를 잡아 준다:

```python
@dataclass(frozen=True)
class Prose:
    rules: str                     # ## Response Format 본문 (예시 자리는 {example} 로 — render_full_example 이 채움)
    reminder_call: str             # constraint_reminder_call
    reminder_action_required: str
    framing_parse_fail: str
    no_action_detail: str
    retry_no_json: str
    retry_no_action: str
    user_prefixes: tuple[str, ...] # system_user_prefixes (= 위 문구들의 접두)
```

기존 두 포맷은 **옛 텍스트를 바이트 그대로** 조각에 옮긴다 → `format_rules()` 출력이
동일 → 시스템 프롬프트 스냅샷·플러밍 테스트가 무변경으로 통과한다(가장 강한 등가성).
새 스펙의 조각은 xml_fc 의 것을 베껴 쓰고 bakeoff 로 다듬는다. "한 op = 한 대상"
문장은 공용 틀이 모든 스펙에 넣는다.

### 4.7 history 왕복

ABC 기본 구현(`parse` + `render_full_example`)을 그대로 쓴다. 두 포맷의 오버라이드는
`render_assistant_from_history` 가 `render_call` 을 부르도록 엔진에 흡수. 세션 메타의
포맷 이름으로 재렌더되므로 **이름이 유지되는 한** 옛 세션은 그대로 resume 된다.

### 4.8 레지스트리·바인딩·구제

- `specs/*.py` 가 `DialectSpec` 인스턴스를 정의하고 `register(Dialect(spec))` — 자동 등록
  방식은 지금과 같다(`_register_builtin_plugins`).
- `resolve_wire_format` → `resolve_dialect`(동작 동일). `try_foreign_parse` 는 무변경 —
  등록 스펙이 넷이 되면 구제 폭이 저절로 넓어진다(순서: 기본 먼저, 이름순).
- 바인딩 기본값 제안(models.json 자동 저장 대상 아님, 문서로): Qwen3-Coder/3.5+/Nemotron 3/
  Granite 4.2 → `xml_fc`, Qwen2.5/3/Next·Hermes·Granite 4.0/4.1 → `hermes_json`, GLM → `glm_argkey`.

### 4.9 버리는 것 (결정 1 — 확정)

- md_array 헤더 관용(`## Thought`/`## Action` → stage 2): `_split_sections`, `_INPUT_RESIDUE`,
  관련 테스트. v6.0.0 이후 1년, 라이브 구제 전용(재생엔 무영향).
- 그 결과 `json_fc` 의 `degeneration_trigger` 는 `#`(헤더 반복 시그니처)에서 `[` 로 바뀐다 —
  배열 반복 러너웨이가 실제 시그니처.
- `json_fc.parse()` 의 역사적 투영 오버라이드(§4.3 마지막 항 — 코퍼스 결과에 따라).

## 5. 파일 배치 (브랜치 `dialects`, 최종)

```
agent_cli/dialects/                ← 옛 wire_formats/
  __init__.py        register/get/list_names/resolve_dialect/try_foreign_parse/all_system_user_prefixes (+ 호환 별칭)
  base.py            WireFormat ABC → Dialect ABC 로 이름만 (내용 동일)
  spec.py            DialectSpec, ArgStyle, NameSlot, Lenient, Prose
  engine.py          Dialect(spec): render / parse / grammar / prose / history
  recovery/          §4.4 — __init__.py(공용 앞단) · json.py · tagged.py
  grammar.py         그대로
  specs/
    __init__.py      네 스펙 import → register
    json_fc.py  xml_fc.py  hermes_json.py  glm_argkey.py
agent_cli/wire_formats/__init__.py ← 한 주버전 동안 남기는 shim: from agent_cli.dialects import *  (agent-board import 호환)
```

## 6. 개명 `wire_formats` → `dialects` 와 호환층

| 계약 | 신 | 호환 |
|---|---|---|
| 패키지 | `agent_cli.dialects` | `agent_cli.wire_formats` shim(재수출 + DeprecationWarning), v11 에서 제거 |
| CLI | `--dialect NAME` | `--response-format` 숨은 별칭(동작 동일), v11 에서 제거 |
| models.json | `dialect` | 읽기: `dialect` 우선, 없으면 `wire_format`; 자동 저장은 새 키. 보드 admin 드롭다운은 `list_names` 그대로 |
| 세션 메타 | `dialect` | 읽기: 둘 다 **영구**(옛 세션 resume). 쓰기: 새 키 |
| 클래스 | `Dialect`, `DialectSpec` | `WireFormat = Dialect` 별칭(shim) |
| 내부 변수명 `wire_format` (656곳/73파일) | `dialect` | 기계 치환 — 등가성 합격 **뒤** 별도 커밋 |
| 문서 | README·ARCHITECTURE·docs/multi-wire-format → `docs/dialects/` 로 이동, 옛 경로는 포인터 | |

semver: 패키지 이름·CLI 플래그·models.json 키가 바뀌므로 **MAJOR v10.0.0**(별칭을 깔아도
공개 계약의 정식 이름이 바뀐다). agent-board 는 shim 덕에 무수정이지만 `list_names` import
경로를 새 패키지로 옮기는 짝 릴리스를 낸다.

## 7. 합격선 (결정 2 — 확정)

옛 모듈을 지우기 전에 셋 다 통과한다.

1. **파서 등가성** — 재생 코퍼스(§3.5) 전건에 대해 `old.parse_turn(text)` 와
   `new.parse_turn(text)` 의 `(thought, [ (action, action_input, truncated) ], terminal,
   parse_stage)` **바이트 동일**. 허용 차이 목록은 비어 있어야 하며, 차이가 나면 둘 중
   어느 쪽이 맞는지 §10 에 올려 결정한 뒤 테스트로 고정한다. 하네스는
   `tests/equivalence/test_dialect_parity.py`(옵트인 `AGENT_CLI_EQUIV_CORPUS=<dir>`), 옛 모듈은
   삭제 전까지 `agent_cli/dialects/_legacy/` 에 두고 비교한다.
2. **문법 등가성** — `test_decoding_grammar_accept.py` 의 수용/거부 코퍼스 동일 + 실제
   토크나이저 벤치(gbench, Qwen3.8 토크나이저) 토큰당 비용 ±5% 안, xgrammar 컴파일 시간 동등.
3. **산문 등가성** — `format_rules()`·구제 문구 6종·`system_user_prefixes()` 바이트 동일
   (스냅샷 `tools_section_json_fc.txt` 무변경). 프롬프트가 같으면 모델 동작의 상한도 같다.
4. **라이브 bakeoff** — 로컬 omlx(Qwen3.8-Flash-Next, xml_fc 바인딩)로 PHASE2 §6.4 와
   같은 묶음 N≥20런: 완주율·pf·재시도 종전 이상. 새 스펙(hermes_json, glm_argkey)은 §10 D3.

## 8. 전환 시퀀스 (브랜치 `dialects`, 커밋 단위)

| # | 커밋 | 게이트 |
|---|---|---|
| S1 ✅ | `recovery/` 추출(이동만: json.py ← 수리 기계, tagged.py ← xml lenient), `json_fc`/`xml_fc` 가 그것을 import | 전체 테스트 초록, diff = 이동 |
| S2 ✅ | `spec.py` + `engine.py` + `specs/xml_fc.py`; 옛 `xml_fc.py` → `_legacy/` | 합격선 1·2·3 (xml_fc) — 코퍼스 230건 바이트 동일, 문법·산문·history 동일. 발견: ABC `strip_thinking` 이 classmethod 라 인스턴스 정지점을 못 읽음(엔진이 override) |
| S3 | `specs/json_fc.py`(md_array 관용 삭제 포함); 옛 `json_fc.py` → `_legacy/` | 합격선 1·2·3 (json_fc) |
| S4 | `specs/hermes_json.py`, `specs/glm_argkey.py` + 표 기반 파서·문법·foreign 구제 테스트 | 테스트 + D3 |
| S5 | `_legacy/` 삭제, 등가성 하네스는 코퍼스 고정본으로 전환 | 합격선 4 (라이브 bakeoff) |
| S6 | 개명 `dialects` + 호환층(§6) + 변수명 치환 + 문서 이동 | 전체 테스트·스냅샷·브라우저·보드 연동 |
| S7 | README/ARCHITECTURE/CHANGELOG, 버전 10.0.0, PR → CI 초록 → 머지 → 태그 | CI |

S1~S3 각각 사보타주(옛 코드에 새 테스트) 확인. 큰 되돌림 전 커밋(메모리 규칙).

## 9. 테스트 계획

- **표 기반 스펙 테스트** `tests/dialects/test_spec_table.py`: (스펙, 입력, 기대 ops) 행 —
  캐노니컬·멀티-op·종결·인용(펜스/인라인)·truncation·lenient 변종·깨진 JSON·빈 산문. 네 스펙에
  같은 논리 입력을 각자 렌더로 돌린다(PHASE2 "같은 내용 다른 모양" 원칙).
- **문법 수용 테스트** 확장: 스펙마다 캐노니컬 렌더가 자기 문법에 수용되고, 다른 스펙 렌더는
  거부되는지.
- **foreign 구제**: xml_fc 바인딩에서 hermes_json 누출 → 구제 → corrected_record 가 xml 캐노니컬.
- **등가성 하네스**(§7.1) 옵트인.
- **호환층**: `--response-format`/`wire_format` 키/옛 세션 메타 resume, `agent_cli.wire_formats` import.

## 10. 결정 포인트 (승인 요청)

- **D1 이름**: 패키지 `dialects`, 클래스 `Dialect`/`DialectSpec`, CLI `--dialect`, models.json·세션 메타 키 `dialect`. 별칭 유지 기간 = 한 주버전(v10 동안, v11 제거).
- **D2 기본 포맷**: `DEFAULT = json_fc` 유지(bakeoff 검증본, 모델 무관) vs `xml_fc`(지금 주력 Qwen3.x 의 네이티브 프라이어). 추천: **유지** — 바인딩이 모델별로 고르므로 기본값은 보수적으로.
- **D3 새 스펙의 실모델 검증**: 로컬 omlx 는 Qwen3.8(XML 프라이어)뿐. hermes_json 은 Hermes 프라이어 모델(Qwen3-Next·Qwen2.5·Hermes 4 등)이 로컬에 있어야 bakeoff 가 되고, glm_argkey 는 GLM 가중치가 필요하다. 없으면 S4 는 **파서·문법·foreign 구제 테스트만**으로 들어가고 "실모델 미검증" 으로 표시. 어느 쪽?
- **D4 lenient 변종 유지**: xml_fc 의 `<X>`/`<k>` 붕괴 구제(35B 실측 83%)를 스펙 옵션으로 유지. 끌 이유가 없으면 유지.
- **D5 버전**: v10.0.0 MAJOR(개명 포함) 한 번에 vs 엔진(9.x minor) → 개명(10.0.0) 두 릴리스. 추천: **한 번에** — 두 번 흔들면 등가성 검증이 흐려진다(§6 S6 이유).
- **D6 머지 방식**: PR + CI 초록 + 리뷰 후 머지(스쿼시 아님 — 커밋 단위 게이트 보존).
