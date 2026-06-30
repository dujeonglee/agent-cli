# REF-Block Wire Format — 설계 문서

> 목적: `write_file` / `edit_file`처럼 **큰 콘텐츠를 운반하는 도구 입력**에서, 콘텐츠를 JSON
> 문자열로 escape하다 문법이 깨지는 문제를 없앤다. 큰 필드는 JSON 안에 **REF 플레이스홀더**만
> 남기고, 실제 내용은 JSON 봉투 밖 **nonce로 구분된 verbatim 블록**으로 받아 매핑한다.
>
> 이 문서는 **다른 세션에서 컨텍스트 없이 구현**할 수 있도록 자족적으로 작성되었다. 코드
> 경로/라인은 작성 시점 기준이며, 구현 전 실제 코드로 재확인할 것.

---

## 1. 배경과 문제

### 1.1 증상
수정/생성하는 코드 양이 커질수록, LLM(특히 작은 모델)이 콘텐츠를 JSON 문자열 필드 안에
넣을 때 **escape를 자주 틀린다**: literal 개행, 따옴표(`"`), 백슬래시(`\`), 그리고 콘텐츠
자체가 JSON/코드일 때의 중첩 등이 깨진다. 결과적으로 `json.loads`가 실패하고 recovery로
빠지거나 파일이 잘못 써진다.

### 1.2 문제가 집중된 지점 (중요)
조사 결과 JSON 깨짐은 **딱 두 필드**에 집중되어 있다:

- `write_file`의 `content` — 긴 멀티라인 문자열.
- `edit_file`의 `lines` — 긴 문자열 배열(또는 문자열).

`edit_file`은 이미 **hashline ref 기반**이라 원본 코드를 다시 보내지 않는다(아래 입력 스키마
참고). 즉 "원본 재전송" 문제는 없고, 남은 약점은 **새로 써넣는 내용(`content`/`lines`)을
JSON 문자열로 담는 부분**뿐이다. 이 두 필드만 봉투 밖으로 빼면 문제 대부분이 사라진다.

### 1.3 폐기한 대안: Markdown table
콘텐츠를 markdown 표로 받는 방안은 **코드에 부적합**하여 폐기했다:
- 표 셀은 줄 단위 → 멀티라인 코드를 literal 개행으로 못 담아 `\n`/`<br>` 재인코딩 필요(escape 부활).
- 코드 내 `|` 가 컬럼을 깨뜨림(또 escape).
- 다수 파서가 셀 앞뒤 공백을 트리밍 → **Python 들여쓰기 소실**.

### 1.4 채택: REF + nonce 블록
- JSON 봉투(작고 구조적, 큰 콘텐츠 없음)는 유지 → 거의 안 깨짐.
- 큰 필드는 `"@@REF1@@"` 같은 **플레이스홀더 문자열**로 치환.
- 실제 내용은 봉투 뒤에 **`nonce`로 구분된 블록**으로 verbatim(무-escape) 전달.
- 파서가 블록을 추출해 플레이스홀더에 매핑 → 도구는 평소대로 완성된 dict를 받음.

---

## 2. 현재 아키텍처 (구현 전 필독)

이 변경은 **새 wire format 플러그인**으로 격리해 추가한다. 기존 `react`(JSON)·`prefix_md`
(markdown 섹션)와 같은 플러그인 시스템이다.

### 2.1 wire format 플러그인 시스템
- 위치: `agent_cli/wire_formats/`
  - `base.py` — `WireFormat` ABC + `ParsedAction` 데이터클래스. 라이프사이클 훅(파싱/렌더/
    history 라운드트립/recovery/provider) 기본 구현 제공.
  - `react.py` — 기본 JSON 포맷(`ReActFormat`). 3-stage fallback 파서 + JSON repair.
  - `prefix_md.py` — markdown 섹션 포맷(`PrefixMdFormat`). **참고 구현으로 가장 유용** —
    JSON 모드 비활성화(`provider_call_kwargs`), 엄격한 라인-앵커 파싱, recovery 문구 구성을
    이 플러그인이 어떻게 자족적으로 들고 있는지 그대로 본뜬다.
  - `__init__.py` — `register(...)`로 플러그인 등록, 이름으로 조회(`_get_wire_format("react")`).
- 각 플러그인은 **폴더-삭제 가능 경계**다: 그 파일만 지우면 해당 포맷이 통째로 사라지도록
  모든 문자열/파서/recovery 문구/provider 힌트를 자기 안에 둔다. **새 플러그인도 동일 원칙 준수.**

### 2.2 `ParsedAction` (파서 출력 계약)
`agent_cli/wire_formats/base.py` (대략 lines 62-94):
```python
@dataclass
class ParsedAction:
    thought: str | None = None
    action: str | None = None
    action_input: dict | str | None = None
    raw: str = ""
    parse_stage: int = 0  # 0=fail, 1=clean, 2=repaired, 3=regex/last-resort
    thinking: str | None = None  # <think>...</think>에서 추출
    truncated: bool = False
```
REF-block 파서도 **이 객체를 반환**한다. 핵심: 블록을 합쳐 넣은 **완성된 dict**를
`action_input`에 채워서 반환하면, 하류(검증·dispatch·도구)는 한 줄도 안 바뀐다.

### 2.3 입력이 도구까지 흐르는 경로
1. LLM 원문 → wire format `parse()` → `ParsedAction`.
2. `agent_cli/loop.py` (대략 line 1380): `tool_input = op.action_input or {}`
   ← **여기서 이미 완성된 dict여야 한다. REF-block 합치기는 반드시 이 지점 이전, 즉
   `parse()` 내부에서 끝낸다.**
3. 스키마 검증: `recovery/detectors.py:detect_schema_mismatch` →
   `tools/registry.py:validate_tool_input` (필수필드/타입/coercion). 정상화된 dict 반환.
4. dispatch: `loop.py`가 prefix strip(평탄 도구는 no-op) 후 `tools/registry.py:_execute_tool`
   → `TOOLS[name].run(args)` → `_run(args)` → 도구가 `args.get("content")` 등으로 추출.

### 2.4 대상 도구 입력 스키마 (변경 없음)
- `write_file` — `agent_cli/tools/write_file.py` (class lines ~118-161; impl `tool_write_file` ~67-115)
  - `path`(str, required), `content`(str, required). `content = args.get("content","")`.
- `edit_file` — `agent_cli/tools/edit_file.py` (class lines ~315-369; impl `tool_edit_file` ~201-312)
  - `path`(str, req), `op`(replace|append|prepend|delete, req), `pos`(hashline ref, req),
    `end`(hashline ref, opt), `lines`(array[str] | str, opt).
  - **중요**: `lines`가 문자열이면 내부에서 `"\n"`으로 split한다(~lines 240-243). 따라서
    REF 블록 → `lines`에 **문자열 한 덩어리로 주입**하면 자연스럽게 동작한다.

> 시사점: REF 플레이스홀더는 **항상 "문자열"로 resolve**하면 된다. `content`는 그대로,
> `lines`는 문자열로 주면 edit_file이 알아서 split. **특수 케이스 분기 불필요.**

### 2.5 도구 설명/스키마가 프롬프트에 들어가는 곳
- `tools/registry.py:get_tool_descriptions(tool_names, inline_guides, wire_format)` (~221-289)가
  "## Available Tools" 섹션 문자열을 만든다. **`wire_format` 인자를 이미 받는다** → 이 포맷일 때
  큰 필드를 어떻게 REF로 보내야 하는지 도구별 힌트를 여기서 주입할 여지가 있다.
- `prompts/system_prompt.py` (~594-598)에서 호출되어 시스템 프롬프트에 삽입된다.

### 2.6 history 라운드트립 (resume/recovery 안 깨지게 필수 처리)
`base.py`의 기본 구현:
- `serialize_assistant_for_history(...)` — 파싱된 구조 필드를 `history.jsonl`에 저장.
- `render_assistant_from_history(...)` — 저장분을 다시 wire 모양으로 재생성(resume 시 모델에게
  과거 출력 예시로 재투입).
REF-block은 **재생성 시에도 큰 필드를 다시 블록으로 외부화**해야 일관된다(아래 §6).

---

## 3. Wire 포맷 명세 (REF-Block)

플러그인 이름: **`ref_block`**.

### 3.1 전체 형태
```
{"thought":"...","action":"write_file","action_input":{"path":"foo.py","content":"@@REF1@@"}}

@@REF1@@ <<<<7a3f
def main():
    print("따옴표 \\ 개행 ## ``` 다 그냥 써도 됨")
>>>>7a3f
```

- **1줄(논리적): JSON 헤더** — `react`와 동일한 `{"thought","action","action_input"}` 구조.
  단, 큰 필드 값은 플레이스홀더 문자열로 대체.
- **이후: 0개 이상의 REF 블록** — 각 블록이 하나의 verbatim 페이로드.

### 3.2 플레이스홀더
- 형태: 정확히 `@@REF<n>@@` (n = 1,2,3…). 예: `@@REF1@@`.
- JSON `action_input` 안의 **문자열 값**으로만 등장(키 아님). 중첩 dict/array 안에 있어도 됨.
- 파서는 action_input을 재귀적으로 훑어 **값이 정확히 `@@REF<n>@@`인 문자열**을 블록 n의
  페이로드로 치환한다(부분 문자열 치환 아님 — 정확히 일치할 때만).

### 3.3 블록 문법
```
@@REF<n>@@ <<<<<nonce>
<verbatim payload, byte-for-byte, escape 없음>
<nonce>
```
- **시작 줄**: 자체 한 줄. `@@REF<n>@@` + 공백 + `<<<<` + `<nonce>`. 라인-앵커(`^...$`)로 매칭.
- **끝 줄**: 자체 한 줄. 시작 줄과 **동일한 `<nonce>`** 만 단독으로. 라인-앵커 매칭.
- **페이로드**: 시작 줄 다음 줄부터 끝 줄 직전 줄까지. **앞뒤 개행/공백을 trim하지 않는다**
  (들여쓰기·말미 개행 보존). 단, 시작 줄의 개행과 끝 줄의 개행은 경계로만 쓰고 페이로드에 포함하지 않음.
- **`<nonce>`**: 4–8자 영숫자(`[A-Za-z0-9]{4,8}`). 모델이 생성. 역할: 페이로드가 우연히
  `>>>>...`/마커와 충돌하는 것을 사실상 불가능하게 만든다.

> 마커 기호(`<<<<`, nonce 반복)는 코드·markdown·JSON에 거의 나타나지 않는 조합으로 고른 것.
> 구현 시 정규식은 nonce를 시작 줄에서 캡처한 뒤, **그 nonce에 대해서만** 끝 줄을 찾는다.

### 3.4 매핑 규칙
1. 모든 블록을 추출해 `{n: payload}` 맵 구성.
2. `action_input`을 재귀 순회: 문자열 값이 정확히 `@@REF<n>@@`이고 맵에 n이 있으면 payload로 치환.
3. 치환된 값은 항상 **문자열**(§2.4 시사점). edit_file `lines`는 이 문자열을 받아 내부에서 split.

---

## 4. 예시

### 4.1 write_file (단일 큰 콘텐츠)
```
{"thought":"새 모듈 생성","action":"write_file","action_input":{"path":"app/util.py","content":"@@REF1@@"}}

@@REF1@@ <<<<k9z2
import json


def load(p):
    return json.loads(open(p).read())  # 따옴표/백슬래시 "\" 그대로 OK
>>>>k9z2
```
→ `action_input = {"path":"app/util.py","content":"import json\n\n\ndef load(p):\n    return json.loads(open(p).read())  # 따옴표/백슬래시 \"\\\" 그대로 OK\n"}`

### 4.2 edit_file (lines 외부화)
```
{"thought":"함수 본문 교체","action":"edit_file","action_input":{"path":"app/util.py","op":"replace","pos":"4#VR","end":"5#XQ","lines":"@@REF1@@"}}

@@REF1@@ <<<<m3p8
def load(path):
    with open(path) as f:
        return json.loads(f.read())
>>>>m3p8
```
→ `lines` = 위 3줄 문자열. edit_file이 `"\n"`으로 split.

### 4.3 멀티 블록 (여러 큰 필드/멀티 op)
한 액션이 큰 콘텐츠를 2개 이상 나르면 REF1, REF2…로 번호를 늘리고 블록도 그만큼 둔다.
각 블록은 독립 nonce를 가진다.

---

## 5. 파서 알고리즘 (의사코드)

```python
def parse_ref_block(text: str) -> ParsedAction:
    text = sanitize_surrogates(text)
    text, thinking = strip_thinking_blocks(text)   # react/prefix_md와 동일 헬퍼 복제
    result = ParsedAction(raw=text, thinking=thinking)

    # 1) 블록 추출 (라인-앵커). 시작 줄에서 ref 번호 n과 nonce 캡처.
    #    START:  ^@@REF(\d+)@@\s+<<<<([A-Za-z0-9]{4,8})\s*$
    #    END  :  ^<nonce>\s*$   (해당 nonce에 대해서만)
    blocks = {}              # {n: payload}
    header_text = text       # 블록을 들어낸 나머지(= JSON 헤더 후보)
    for each START match in order:
        n, nonce = captures
        find END line matching exactly that nonce after START line
        if not found:
            result.parse_stage = 0; record "unterminated block n"; continue
        payload = text[after START newline : before END line]  # trim 금지
        blocks[n] = payload
        remove the whole START..END span from header_text

    # 2) JSON 헤더 파싱: 남은 header_text에서 첫 균형 {...} 블록을 json.loads.
    data = json_loads_balanced(header_text)   # react의 _extract_json_block 재사용 가능
    if data is None:
        result.parse_stage = 0; return result  # recovery: 헤더 파싱 실패

    # 3) action / thought / action_input 채우기 (+ react의 _normalize_action_input 정책 재사용 여부 결정)
    populate_from_dict(result, data)

    # 4) action_input 재귀 순회하며 @@REF<n>@@ → blocks[n] 치환
    missing = substitute_refs(result.action_input, blocks)  # 매핑 안 된 ref 목록 수집

    # 5) parse_stage 산정
    if action present and all referenced blocks resolved:
        result.parse_stage = 1
    elif action present but some ref missing/unterminated:
        result.parse_stage = 2   # action은 있으나 입력 불완전 → no-input/schema recovery
    else:
        result.parse_stage = 0/3 per 정책
    return result
```

구현 메모:
- `strip_thinking_blocks`, `sanitize_surrogates`는 `react.py`/`prefix_md.py`에 이미 있는 것을
  **복제**한다(플러그인 자족 원칙; DRY보다 폴더-삭제 가능성 우선 — prefix_md 주석 참고).
- JSON 헤더 추출은 `react.py`의 `_extract_json_block`(문자열 리터럴 인식 균형 중괄호)와 동일
  알고리즘을 쓰되, **블록을 먼저 들어낸 뒤** 적용해 본문 코드의 `{}`가 헤더로 오인되지 않게 한다.
- 블록을 먼저 제거하는 이유: 페이로드 안에 `{...}`나 또 다른 `@@REF@@` 유사 문자열이 있어도
  헤더 파싱과 충돌하지 않도록.

---

## 6. history 라운드트립

- `serialize_assistant_for_history`: 기본 동작(구조 필드 저장) 유지 가능. 저장 시 **resolve된
  실제 content**가 들어가도 무방(다음 항목에서 재외부화하므로).
- `render_assistant_from_history` / `render_full_example`: 재생성 시 **큰 필드를 다시 블록으로
  외부화**한다. 정책:
  - 외부화 대상 = 값이 문자열이고 (a) 멀티라인이거나 (b) 길이 > THRESHOLD(예: 200자)인 필드.
    (도구별 화이트리스트 `{"write_file":["content"], "edit_file":["lines"]}`로 명시하는 편이
    안전하고 예측 가능 — 권장.)
  - 각 대상에 REF 번호 부여, 플레이스홀더로 치환, 블록 append. nonce는 history 재생성 시
    **결정적**으로 만들 것(예: `f"h{n}"` 고정) — `Math.random`/시간 의존 금지(resume 재현성).
- `normalize_assistant_for_messages`: 메시지 배열로 넣을 때도 동일 형태 유지.

---

## 7. recovery (재프롬프트)

`prefix_md.py`의 recovery 문구 구성 패턴을 그대로 본뜬다(framing + echo_prior_output + 제약 재진술).
필요한 케이스:
1. **블록 미종결**: 시작 마커는 있는데 nonce 끝 줄이 없음 → "REF 블록을 `<nonce>` 한 줄로
   닫으세요. 시작 줄과 동일한 nonce." + echo.
2. **플레이스홀더 있는데 블록 없음**: `action_input`이 `@@REFn@@`을 참조하나 블록 n 부재 →
   "REFn 블록이 없습니다. 헤더 뒤에 `@@REFn@@ <<<<<nonce>` … `<nonce>` 블록을 추가하세요."
3. **헤더 JSON 파싱 실패**: react의 no-json framing에 준해 "헤더는 한 줄 JSON, 큰 값은 `@@REFn@@`
   플레이스홀더로." 재안내.
4. **블록 있는데 참조 없음**: 경고만 하거나(무해) 재프롬프트. 보수적으로 무시 권장.

`thought_required`는 react/prefix_md와 동일하게 `True`로 두고 `format_no_thought_retry`도 동일
패턴으로 구현(헤더 JSON에 thought가 비어 있을 때).

상수/문구는 전부 플러그인 파일 안에 둔다(자족 경계).

---

## 8. provider / lifecycle

- `provider_call_kwargs` → **`{"skip_json_format": True}` 필수**. 이유: 출력이 순수 JSON이
  아니라 "JSON 헤더 + 뒤따르는 블록"이라, OpenAI 호환 `response_format=json_object`(전체를 단일
  JSON으로 강제)와 충돌한다. `prefix_md`가 동일 이유로 이미 이 힌트를 쓴다.
- `prefill`: 기본값 유지(헤더가 `{`로 시작하므로 prefill `{` 를 줄 수도 있으나, 블록까지 한
  응답에 나와야 하니 prefill로 첫 토큰을 강제하면 모델이 블록을 빠뜨릴 위험. **prefill 비권장**).

---

## 9. 프롬프트 (Format Rules)

`format_rules_anchor` / `format_rules_field_specific` / `render_full_example`을 구현해 다음을
모델에게 가르친다(prefix_md의 빌더 합성 방식 따름):

- 출력 = 한 줄 JSON 헤더 + (필요 시) REF 블록들.
- `content`/`lines`처럼 **여러 줄이거나 긴 값은 JSON에 직접 쓰지 말고** `@@REFn@@`로 두고,
  헤더 뒤에 블록으로 내용을 그대로(escape 없이) 붙일 것.
- 블록 형식: `@@REFn@@ <<<<<무작위 4~8자 nonce>` 시작, 같은 nonce 한 줄로 종료.
- `## Available Tools` 도구 힌트(`get_tool_descriptions`, `wire_format` 인자 활용)에 write_file
  `content` / edit_file `lines`는 REF 블록 사용을 권장한다고 명시.

`render_full_example`는 §4.1 형태의 완결 예시를 1개 제공.

---

## 10. 통합 지점 체크리스트 (구현자가 만질 파일)

- [ ] `agent_cli/wire_formats/ref_block.py` 신규 — 파서 + `RefBlockFormat(WireFormat)` +
      모든 문구/헬퍼(자족).
- [ ] `agent_cli/wire_formats/__init__.py` — `register(RefBlockFormat())` 추가, 이름 조회 가능하게.
- [ ] (선택) `tools/registry.py:get_tool_descriptions` — `wire_format`이 ref_block일 때
      write_file/edit_file 힌트에 REF 사용법 1–2줄 추가.
- [ ] wire format 선택 경로 확인: 사용자가 `ref_block`을 어떻게 고르는지(설정/CLI 옵션).
      `loop.py`의 `_get_wire_format(...)` 호출과 설정 로딩을 따라가 노출. (react/prefix_md가
      노출되는 동일 경로 사용.)
- [ ] **도구(write_file/edit_file/registry/validate)는 수정 금지** — 완성된 dict를 받으므로
      변경 불필요. (만약 변경이 필요해 보이면 설계가 샌 것 → 멈추고 재검토.)

---

## 11. 테스트 (CLAUDE.md: 유닛 테스트 필수, `pytest tests/` 전체 통과)

`tests/`에 `test_wire_format_ref_block.py` 추가. 최소 케이스:
1. write_file 단일 블록 happy path → action_input.content가 byte-exact 복원(개행/들여쓰기/말미개행).
2. edit_file `lines` 외부화 → 문자열 주입, edit_file split과의 호환(통합 테스트로 실제 적용).
3. 멀티 블록(REF1/REF2) 매핑 정확성.
4. 페이로드에 `}` , `"` , ```` ``` ````, `@@REF2@@` 유사 문자열, 그리고 `<<<<` 비슷한 줄 포함 →
   nonce 덕에 오종결 안 됨.
5. 미종결 블록 → parse_stage 강등 + recovery 메시지(케이스 §7-1).
6. 플레이스홀더 있는데 블록 없음 → 케이스 §7-2.
7. 헤더 JSON 깨짐 → 케이스 §7-3.
8. thinking 블록(`<think>`) 스트립 동작.
9. history 라운드트립: serialize → render_assistant_from_history가 블록 형태로 재외부화하고
   다시 parse하면 동일 action_input(결정적 nonce 확인).
10. `provider_call_kwargs() == {"skip_json_format": True}`.

기존 wire format 테스트(react/prefix_md)와 회귀 없음 확인.

---

## 12. 문서 업데이트 (CLAUDE.md 규칙)

- [ ] `README.md` — 사용자 대면 변경(새 wire format 선택지)이면 사용법/옵션 추가.
- [ ] `docs/ARCHITECTURE.md` — wire format 플러그인 목록에 `ref_block` 추가, 데이터 흐름/LOC 갱신.
- [ ] 커밋: 코드 + tests + README + ARCHITECTURE.md를 **한 커밋**으로(ruff check/format 통과 후).

---

## 13. 열린 결정 사항 (구현 전 합의)

1. **nonce 생성 주체**: 모델이 생성(권장, 충돌 회피) vs. 시스템 고정 토큰(단순하나 충돌 시
   recovery 의존). → 모델 생성 + 파서가 nonce를 시작 줄에서 캡처해 그 값으로만 종료 매칭. (채택)
2. **외부화 트리거**: 도구별 화이트리스트(write_file.content, edit_file.lines) vs. 길이/멀티라인
   휴리스틱. → **화이트리스트 권장**(예측 가능, 작은 모델에 일관된 규칙). render 재외부화도 동일.
3. **`_normalize_action_input`(sibling hoisting) 정책 차용 여부**: react의 관용적 정규화를
   ref_block에도 적용할지. → 초기엔 보수적으로 미적용, 필요 시 추가.
4. **wire format 노출 UX**: 설정 키/CLI 플래그 이름. 기존 prefix_md가 노출되는 방식에 맞춤.

---

## 14. 설계 불변식 (어기면 멈추고 재논의 — CLAUDE.md "기술 부채 금지")

- 도구 계약(`args.get("content")`/`lines`)과 검증 계층은 **불변**. REF 합치기는 `parse()` 안에서 끝낸다.
- 플러그인은 **폴더-삭제 가능**해야 한다: 모든 문자열/파서/recovery/provider 힌트를 자기 파일에.
- 페이로드는 **byte-exact** 보존(trim·정규화 금지). 들여쓰기/말미 개행이 코드 정확성을 좌우한다.
- resume 재현성: history 재외부화의 nonce는 **결정적**. `random`/시간 의존 금지.
