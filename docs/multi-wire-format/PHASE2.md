# 멀티 wire-format — Phase 2: xml_fc 플러그인 (태그-파라미터) (DESIGN)

> 상태: **구현 완료** (v5.20.0 — D4 확인·D6=ⓐ 현행 유지 승인 2026-07-17)
> 작성: 2026-07-17 · 선행: [DESIGN.md](DESIGN.md) Phase 1 (v5.19.0 출하)
> 문법 결정: **태그-파라미터** (`<function=X><parameter=k>v</parameter></function>`,
> Hermes JSON-inside-tags 아님 — 사용자 확정 2026-07-17)
> 구현 노트: 선행 리팩토링(v5.19.1) = thinking-tag 스트리핑 단일 소스
> `agent_cli/thinking_tags.py` — vocab 4곳 중복 제거, `WireFormat.strip_thinking`
> (파서 stage 0 공용), md_array 의 provider-미경유 경로 ①② 무방비 갭 봉합.
> **bakeoff 미실시** — non-default 플러그인으로 출하, models.json 바인딩
> 권장/기본 전환 전 실측 게이트 (§6.4).

## 1. Wire shape

```
파일을 읽고 나서 빌드를 돌린다.          ← thought = 자유 산문 (선택, §D4)

<tool_call>
<function=read_file>
<parameter=path>src/main.c</parameter>
</function>
</tool_call>
<tool_call>
<function=shell>
<parameter=command>make -j4</parameter>
</function>
</tool_call>
```

- **멀티-op** = `<tool_call>` 블록 반복 (md_array 의 배열 원소와 동형 의미).
  op 하나 = 대상 하나 (per-tool 배치 금지 — md_array 와 같은 가드레일).
- **종료** = 명시적 `complete` op:

```
<tool_call>
<function=complete>
<parameter=result>
최종 답변 — raw 텍스트. JSON escaping 불필요 (마크다운·개행·따옴표 그대로).
</parameter>
</function>
</tool_call>
```

- **파라미터 값 = raw 텍스트** (JSON 아님). write_file content·complete
  result 의 literal-control-char / under-escape 실패 클래스가 이 포맷에선
  **구조적으로 소멸** — md_array 파서 강화의 상당 부분이 필요 없어진다.
  트레이드: 값 내부의 `</parameter>` 유사 텍스트와의 구분자 충돌 (§5.3).

## 2. 결정 포인트 (승인 요청)

### D4 — thought 슬롯 = 첫 `<tool_call>` **앞 자유 산문** (`<think>` 아님)

Phase 1 논의의 예시는 `<think>...</think>` 를 reasoning 슬롯으로 보였으나,
조사 결과 **아키텍처 충돌로 불가**:

- provider 층 `strip_think_blocks`(openai.py, 5.10.0)가 content 의
  `<think>` 블록을 `thinking` 필드로 **이미 격리**하고,
- 파서 stage 0(각 플러그인)도 선두 thinking 블록을 strip 해
  `ParsedTurn.thinking`(verbose 전용, **비재공급**)으로 보낸다.

즉 `<think>` 를 thought 로 요구하면 파서에 도달하기 전에 사라진다. 또한
native thinking 모델(Qwen3 계열)의 `<think>` 는 RL 학습된 **CoT 채널**이라
visible thought(재공급되는 prior)로 쓰면 이중-thinking 을 유발한다.

**설계**: thought = 첫 `<tool_call>` 앞 산문 (`thought_required=False`,
md_array 동형 플래그). 모델이 native `<think>` 를 내면 지금처럼 CoT 채널로
자연 흡수 — 규칙으로 요구하지도 금지하지도 않음. 프롬프트 예시는 산문
한 줄 + tool_call 블록으로 제시.

### D6 — 관찰 되먹임: 현행 `Observation:` 산문 유지 (추천) vs `<tool_response>` 랩

| | ⓐ 현행 유지 (추천) | ⓑ `<tool_response>` 랩 |
|---|---|---|
| 배관 | 없음 — 관찰 문구는 loop 소유(dispatch `"Observation: ..."`)로 format-agnostic | loop→wire 관찰-랩 훅 신설 (전 포맷 표면 변경) |
| 프라이어 정합 | emission 측만 정합 | Qwen 계열 template(`<tool_response>`)과 양방향 정합 — 이득은 **가설** |
| 리스크 | 없음 (검증된 경로) | 관찰 변형은 mimicry-safe 이나 배관 신규 + 검증 표면 확대 |

**추천 ⓐ**: Phase 2 범위 최소화. ⓑ 의 이득은 bakeoff 에서 xml_fc 형식
실패가 관찰-측 부정합으로 나타날 때만 실측 근거가 생김 — 그때 후속으로.

## 3. 플러그인 표면 (ABC 매핑 — 전부 self-contained, md_array 와 코드 공유 0)

| ABC 멤버 | xml_fc |
|---|---|
| `name` | `"xml_fc"` |
| 플래그 | `multi_op=True`, `thought_required=False`, `action_required=False`, `exposes_complete=True` (md_array 동형) |
| `parse_turn` | §5 파서 — `<tool_call>` 블록들 → ops. 래퍼 없는 bare `<function=...>` 도 관용 수용 |
| `parse` | 1st-op 단수 투영 (md_array 동형) |
| `render_action_input` | prefixed dict → `<parameter=k>v</parameter>` 줄들 (§4 타입 역변환: dict/list 값은 JSON 인라인, 나머지 str()) — 프롬프트 인라인 가이드가 자동으로 태그 shape 로 |
| `render_full_example` | 산문 thought + `<tool_call><function=...>` 블록 |
| `format_rules` | 전면 override (shared builder 미사용 — md_array 전례). positive 규칙, **HTML-태그 금지 조항 없음** (이 포맷의 본질이 태그) |
| `serialize_*_for_history` | md_array 동형 ops 레코드 `{role, thought, ops:[{action, action_input}]}` — `iter_record_ops` 등 shape-공용 reader 호환 |
| `render_assistant_from_history` | 레코드 → 태그 shape 재방출 (B→C 재조립) |
| `sanitize_thought` | 산문 thought 의 고아 센티널 라인(`<tool_call>`·`<function=`·`</think>` 류) strip |
| `is_degenerate` | 빈 `<tool_call>` 골격 반복 구조 검사 (md_array 의 헤더-반복 검사 동형) |
| `provider_call_kwargs` | `{"json_mode": False}` 무조건 (JSON-object 모드는 선두 `{` 강제 → 태그 envelope 불가능 — md_array 와 같은 이유) |
| `prefill` | `""` (bakeoff 후 재고) |
| `diagnose_syntax_error` | `None` (JSON 없음 — 태그 진단은 실측 실패 shape 나오면) |
| 복구 문구 6종 + `system_user_prefixes` | 태그 shape 언어로 자체 작성 |

## 4. 파라미터 타입 — 스키마-주도 강제 (coercion)

태그 값은 전부 문자열로 파싱된다. 도구 스키마
(`schema.parameters.properties[k].type`)를 참조해:

- `string` / 미선언 키 → raw 유지 (트리밍 규칙: 여는 태그 직후 개행 1개,
  닫는 태그 직전 개행 1개만 strip — 블록 스타일 허용, 내부 공백 보존).
- `integer`/`number`/`boolean`/`array`/`object` → JSON parse 시도
  (`true`/`false`/숫자/`[...]`/`{...}`), 실패 시 raw 유지 — 기존
  `validate_tool_input`(A5 SCHEMA_MISMATCH) 경로가 진단·복구 담당.
  파서가 스키마 검증을 중복하지 않는다 (발생 원인 위치 원칙).

역방향(render)은 대칭: str 값은 그대로, dict/list/bool/숫자는 JSON 인라인.
round-trip 테스트로 고정 (serialize→render ≈ 원형, JSON 정규화 한도 내).

## 5. 파서 — 3-단계 (md_array 의 stage 구조 동형, 구현은 독립)

1. **stage 1 (정상)**: `<tool_call>` 블록 스캔 → 각 블록에서
   `<function=NAME>` + `<parameter=KEY>VALUE</parameter>` 반복 추출.
2. **stage 2 (수리)**: EOF-트렁케이션 — 미닫힘 `</parameter>` /
   `</function>` / `</tool_call>` 을 EOF 에서 닫고 재파싱 (md_array
   `close_unbalanced` 와 같은 발상, 태그 스택 기반 자체 구현).
   bare `<function=...>` (래퍼 생략) 수용도 여기 (parse_stage=2 drift 신호).
3. **stage 3 (보존)**: function 이름이 비거나 무효여도 추출된 파라미터를
   `action_input` 에 보존 (ABC parse 불변식 — infer/NO_ACTION echo 재료).

### 5.3 구분자 충돌 (raw 값 안의 `</parameter>` 유사 텍스트)

파라미터 경계는 **lookahead 앵커**: `</parameter>` 뒤에 (개행·공백 지나)
`<parameter=` 또는 `</function>` 이 따라올 때만 경계로 인정. 값 내부의
고아 `</parameter>` (예: 이 플러그인 자신의 테스트 코드를 write_file 할 때)는
lookahead 불일치로 값에 포함된다. 그래도 모호한 최악 케이스(값이 진짜
`</parameter>\n<parameter=` 시퀀스를 포함)는 잘못 잘리고 — 이는 도구
에러/diff 로 표면화되므로 조용히 오염되지 않는다. JSON escaping 지옥과의
트레이드로 수용 (실측 후 필요 시 길이-힌트 등 강화).

## 6. 검증 계획

1. **유닛** (`tests/test_wire_formats_xml_fc.py`, react/md_array 테스트 구조
   미러): 정상 멀티-op / complete / 산문 thought 유무 / bare function 관용 /
   EOF 트렁케이션 수리 / 구분자 충돌(값 속 고아 태그) / 타입 강제(int·bool·
   array·object·string) / round-trip / sanitize / degenerate / 복구 문구 /
   parse 불변식(action 없어도 input 보존).
2. **parity**: 기존 base 계약 테스트(`test_wire_formats_base.py`)에 xml_fc
   편입 — 세 포맷이 같은 논리 입력에서 같은 구조적 결과.
3. **binding e2e**: models.json `"wire_format": "xml_fc"` → Phase 1 체인으로
   해석되는지 (기존 test_wire_format_binding 픽스처에 한 줄).
4. **bakeoff (별도 단계, 채택 게이트)**: 플러그인 추가 자체는 non-default 라
   안전. **models.json 바인딩 권장/기본 전환 전에 bakeoff 필수** (프로젝트
   교훈: sanity≠실전). 대상 모델(Qwen 계열) 라이브 서버 필요 — 이 구현
   세션 범위 밖, 후속 세션에서 bench 인프라로.

## 7. 범위 밖

- 관찰 `<tool_response>` 랩 (D6-ⓑ — bakeoff 실측 후 재고)
- prefill 강제 (bakeoff 후)
- DEFAULT_WIRE_FORMAT 변경·기본 바인딩 배포 (bakeoff 게이트)
- Phase 3 foreign-format 구제
