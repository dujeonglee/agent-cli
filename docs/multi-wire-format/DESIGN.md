# 멀티 wire-format — Phase 1: 모델별 바인딩 (DESIGN)

> 상태: **Phase 1 구현 완료** (v5.19.0 — D1·D2·D3 승인 2026-07-16, 추천안 그대로)
> 작성: 2026-07-16
> 범위: Phase 1만. 새 포맷 플러그인(Phase 2 xml_fc)·foreign-format 구제(Phase 3)는 §9 미리보기만.
> 구현 노트: §7 감사 결과 — A1 은 `save_model_entry` 병합-보존으로 방어 구현,
> A2(위저드)·A5(타 진입점)는 도달 불가 확인, A3(conftest pin) 충돌 없음,
> A4 는 설계대로 기존 동작 유지. web 의 대화형 최근-세션 resume(부트 후 결정)은
> 기록 포맷으로 재해석 + `dataclasses.replace` 로 처리 (§4.4 의 --resume 경로와 동치).

## 1. 배경과 목표

모델(계열)마다 학습된 tool-call 포맷 프라이어가 다르다 — Qwen 계열은
`<tool_call>` XML, 다른 계열은 JSON/markdown. 현재 wire format은 **세션
전역 1개**(`--response-format`, 기본 md_array)라서, main과 다른 모델을
쓰는 서브에이전트도 main의 포맷을 강제당한다. 프라이어와 어긋난 포맷은
형식 실패율을 올린다.

**목표**: wire format 바인딩 단위를 "세션 전역"에서 "**LLM 대화 스트림당
1개 = 모델당 해석**"으로 내린다. 한 세션 안에서 main은 md_array,
Qwen 서브에이전트는 (Phase 2에서 추가될) xml 포맷 — 각자 자기 대화에서.

Phase 1은 **바인딩 배관만** 만든다. 기존 두 플러그인(react/md_array)으로
전체 체인을 검증할 수 있으므로 새 포맷 없이도 독립적으로 출하 가능하다.

### 비목표 (이번에 안 함)

- 새 wire format 플러그인 (Phase 2 — xml_fc, bakeoff 필수)
- 한 LLM 대화 스트림 안에서 두 포맷 혼용 (§2 불변식 위반 — 영구 비목표)
- foreign-format 구제 파서 (Phase 3)
- capabilities 재해석 (role model 오버라이드 시 provider/capabilities를
  다시 뽑는 문제 — 기존 동작 유지, §7-A4 참고)

## 2. 불변식

| # | 불변식 | 근거 |
|---|--------|------|
| I1 | **한 LLM 대화 스트림(= ContextManager 1개)의 wire format은 항상 1개** | transcript에 두 포맷이 섞이면 mimicry로 키메라 emission 유발 (v3.16.1 실측 교훈) |
| I2 | **loop `cfg.wire_format` ≡ `ctx.wire_format`** (ctx가 있는 한) | 어긋나면 파서/프롬프트는 A, 히스토리/컴팩션 렌더는 B — split-brain (§4-G2가 현재 이 위반) |
| I3 | 플러그인은 self-contained — 바인딩 배관은 플러그인 내부를 모른다 | 기존 wire-format 불변식 (parity 테스트로 고정) |
| I4 | 이전 세션 resume 무해 — meta에 필드가 없으면 오늘과 동일하게 동작 | semver 규칙: resume 깨면 MAJOR |

## 3. 현재 배관 (실측)

```
CLI --response-format (기본 md_array)
  └→ _bootstrap_provider(response_format)            main.py:924
       └→ _get_wire_format(name) fail-fast           main.py:941
       └→ SessionBootstrap.wire_format               main.py:920
            ├→ ContextManager(wire_format=…)         main.py:974 _build_context
            └→ run_loop(wire_format=…)               main.py:1257(run) / 1813(web)

세션 메타: SessionMeta.response_format               context/session.py:35
  - create_session 때 기록됨                          main.py:1105
  - resume 때 **읽지 않음** (§4-G1)

서브에이전트 (delegate oneshot / 상주 agents_live):
  apply_role_overrides → model 교체 가능              subagent/runner.py:51-53
  create_subagent_ctx  → wire_format 무조건 부모 상속  subagent/runner.py:82
  run_subagent_message → run_loop에 wire_format 미전달 subagent/runner.py:160
       └→ AgentLoop: None이면 ctx가 아니라 전역 기본   loop/core.py:100-101
```

### 발견된 갭 3개

- **G1 — resume이 세션 메타 포맷을 무시.** `session.jsonl`에
  `response_format`을 기록하지만(필드 docstring: "recoverable for
  debugging / **resume**"), `--resume` 경로는 CLI 플래그(기본 md_array)로
  부트스트랩한다(main.py:1080-1083). react 세션을 플래그 없이 resume하면
  조용히 md_array로 바뀐다. 기록 필드의 의도가 미완성.
- **G2 — 서브 루프 wire_format split-brain (잠복 버그).**
  `run_subagent_message`가 `run_loop`에 `wire_format`을 안 넘기고,
  `AgentLoop.__init__`은 `None`일 때 `ctx.wire_format`이 아니라 전역
  기본(`_get_wire_format()` = md_array)으로 폴백한다(core.py:100-101).
  → react 세션의 delegate는 ctx(히스토리 렌더)는 react, loop(프롬프트·
  파서·복구)는 md_array로 돈다. 기본 포맷이 md_array라 현재는 잠복.
- **G3 — 모델별 바인딩 부재 (Phase 1의 본 목적).** role md가 `model`을
  오버라이드해도 포맷은 부모 상속 고정(runner.py:82).

## 4. 설계

### 4.1 바인딩 저장 — models.json 엔트리 필드

```jsonc
// ~/.agent-cli/models.json (또는 agent_cli/default_models.json)
"models": {
  "qwen3-32b": {
    "provider": "openai",
    "context_window": 32768,
    // ... 기존 capabilities 필드 ...
    "wire_format": "md_array"        // ← 신규, 선택 필드
  }
}
```

- **위치 근거**: 포맷 프라이어는 모델의 성질(학습 결과)이지 역할(agent
  profile)이나 세션의 성질이 아니다. capabilities와 같은 파일에 두되,
  `ModelCapabilities` dataclass에는 **넣지 않는다** — capabilities는
  "모델이 뭘 할 수 있나", 바인딩은 "우리가 어떤 shape로 말할까"로 축이
  다르고, role model 오버라이드 경로는 capabilities를 재해석하지 않으므로
  (§7-A4) dataclass에 태우면 그 경로에서 죽는 필드가 된다.
- 조회는 `config.get_model_entry(model)`(이미 존재, config.py:74)를 그대로
  읽는 모델명-키 단독 함수로 — 어디서든(부트스트랩·서브에이전트) 호출 가능.

### 4.2 해석 체인 — `resolve_wire_format()`

`agent_cli/wire_formats/__init__.py`에 추가 (레지스트리 옆; wire_formats →
config 단방향 import, 순환 없음 — config는 wire_formats를 import하지 않는다):

```python
def wire_format_for_model(model: str) -> str | None:
    """models.json 엔트리의 wire_format 바인딩 (없으면 None)."""

def resolve_wire_format(
    *,
    explicit: str | None,        # 사용자가 명시한 --response-format
    session_format: str | None,  # resume 세션 메타의 response_format
    model: str = "",             # 해석된 모델명 (바인딩 조회용)
) -> WireFormat:                 # 등록 플러그인 인스턴스 (unknown → KeyError)
```

우선순위 (위가 이김):

| 순위 | 소스 | 이유 |
|------|------|------|
| 1 | 명시 `--response-format` | 사용자의 말이 항상 최우선 |
| 2 | resume 세션 메타 `response_format` | 세션이 이미 그 포맷으로 축적한 transcript와의 정합 (G1 수리) |
| 3 | models.json 모델 바인딩 | 모델 프라이어 (G3 — Phase 1의 목적) |
| 4 | `DEFAULT_WIRE_FORMAT` | 현행 유지 |

> **주 2→3 순서 근거**: 바인딩이 세션 생성 후 바뀌어도 기존 세션은
> 기록된 포맷으로 안정 resume. 새 바인딩은 새 세션부터.
>
> **명시 플래그로 resume 시 포맷 전환은 허용** (순위 1 > 2): 히스토리는
> 구조화 레코드로 저장되고 prior는 현재 플러그인의
> `render_assistant_from_history`로 재조립되므로(base.py 라이프사이클
> B→C), 전환해도 transcript 전체가 새 포맷으로 **일관되게** 재렌더된다.
> 혼합-포맷 transcript는 생기지 않는다.

### 4.3 명시성 감지 — 플래그 default 변경

현재 `--response-format`의 default가 `DEFAULT_WIRE_FORMAT`이라 "사용자가
명시했는지"를 구분할 수 없다. **default를 `None`으로** 바꾸고(run:
main.py:1049, web: main.py:1633) help 텍스트에 "(default: md_array,
모델별 바인딩이 있으면 그것)"을 명시한다. `None`이 체인의 `explicit=None`
으로 흐른다. 사용자-가시 동작: 플래그를 안 쓴 경우에만 달라질 수 있고,
그 경우 바인딩이 없으면 오늘과 바이트 동일.

### 4.4 main 부트스트랩 배선

```python
# main.py run/web — 順序 유지: resume 로드 → 부트스트랩
session_resumed = _load_resume_session(resume) if resume else None
boot = _bootstrap_provider(
    provider, model, base_url, api_key,
    response_format,                                  # None 가능해짐
    max_context_tokens,
    session_format=(session_resumed.response_format if session_resumed else None),
)
```

`_bootstrap_provider` 내부: `_setup_provider`로 `resolved_model`을 얻은 뒤
`resolve_wire_format(explicit=…, session_format=…, model=resolved_model)`.
unknown 이름은 지금처럼 세션 생성 전 fail-fast (exit 2). `create_session`
에는 **해석 결과**(플러그인 `.name`)를 기록 — 다음 resume의 순위 2 소스.

`run`의 최근-세션 자동 resume(`_maybe_resume_recent`, main.py:1430)도 같은
규칙: resume 채택 시 그 메타의 포맷이 순위 2로 들어간다.

### 4.5 서브에이전트 배선 (G2·G3 수리)

**(a) `create_subagent_ctx`에 effective model 전달** (runner.py:65):

```python
def create_subagent_ctx(context_mode, parent_ctx, subagent_dir, *, model: str = ""):
    binding = wire_format_for_model(model) if model else None
    wf = _get(binding) if binding else (parent_ctx.wire_format if parent_ctx else None)
```

- 체인: **effective model 바인딩 > 부모 상속** (부모 없으면 ContextManager
  기본). 세션의 명시 플래그는 서브에이전트 모델 바인딩을 **덮지 않는다**
  — 플래그는 main 스트림에 대한 사용자의 선택이고, 서브 모델의 프라이어가
  다른 게 이 기능의 존재 이유다 (결정 D1).
- unknown 바인딩 이름 → spawn/delegate 거부 에러 반환 (`ToolResult(False,
  error=…)` — 조용한 폴백 금지, 결정 D2).
- 호출자 3곳이 `apply_role_overrides` **후의** model을 넘긴다:
  oneshot.py:142(delegate), agents_live.py spawn, agents_live resume 재생성.
- `fork`/`resume` 모드: 기존 히스토리를 이어받으므로 **부모/기존 포맷
  유지가 원칙**이나, 히스토리가 구조화 레코드라 다른 포맷 재렌더도 일관
  (§4.2 주). 단순화를 위해 세 모드 동일 체인 적용.

**(b) `AgentLoop` 폴백을 ctx-우선으로** (core.py:100-101):

```python
if wire_format is None:
    wire_format = ctx.wire_format if ctx is not None else _get_wire_format()
```

I2 불변식의 단일 지점 강제. `run_subagent_message`는 계속 `wire_format`을
안 넘겨도 되고(ctx가 소스), 상주/oneshot/fork 전부 자동으로 정합된다.
ctx 없는 headless 호출은 현행(전역 기본) 유지 — 동작 무변경.

### 4.6 검증·관찰성

- **boot fail-fast**: 명시 플래그·models.json 바인딩 모두 unknown 이름이면
  등록 플러그인 목록과 함께 즉시 에러 (기존 main.py:941 경로 재사용).
- **관찰성**: 서브에이전트가 부모와 다른 포맷으로 뜰 때 verbose/디버그
  로그 1줄 (`wire_format: md_array (model binding: qwen3-32b)`), Prompt
  Inspector 스코프 메타에 포맷명 표기(web.py `insp-meta` — 선택, 소품).
- turns.jsonl에는 이미 parse_stage/failure_signal이 있어 포맷별 형식
  실패율 비교 가능 — Phase 2 bakeoff의 기초 데이터.

## 5. 변경 파일 (예상)

| 파일 | 변경 | 규모 |
|------|------|------|
| `wire_formats/__init__.py` | `wire_format_for_model` + `resolve_wire_format` | +40 |
| `main.py` | 플래그 default None ×2, `_bootstrap_provider` 시그니처+체인, resume 배선 | ~30 |
| `loop/core.py` | ctx-우선 폴백 1줄 (G2) | ~3 |
| `subagent/runner.py` | `create_subagent_ctx(model=…)` + 바인딩 해석 | ~15 |
| `subagent/oneshot.py` | 호출부 model 전달 | ~2 |
| `subagent/agents_live.py` | spawn/resume 호출부 model 전달 | ~4 |
| `config.py` | (변경 없음 — get_model_entry 재사용) | 0 |
| tests | §8 신규 + conftest 영향 확인 | +150~ |
| README / ARCHITECTURE | models.json `wire_format` 필드, 해석 체인, 플래그 default | 문서 |

## 6. 마이그레이션 / semver

- models.json `wire_format` 필드는 **선택** — 없으면 전 경로가 오늘과
  동일(I4). `_build_from_entry`는 unknown 키를 이미 무시하므로 구버전
  agent-cli가 신버전 models.json을 읽어도 무해.
- **resume 동작 변화 1건 (G1 수리)**: 비기본 포맷 세션을 플래그 없이
  resume하면 이제 기록된 포맷을 따른다. 필드 도입 시점의 의도 완성이며
  이전 세션 resume을 깨지 않으므로 **minor (v5.19.0)**.
- G2 수리도 비기본 포맷 세션에서만 관찰 가능한 잠복 버그 수정 — minor에 동봉.

## 7. 감사 항목 (구현 중 확인)

- **A1** `save_model_entry`(config.py:95)는 `_auto_detected` 엔트리를
  refresh할 수 있다 — 그 경로가 손으로 추가한 `wire_format` 키를 떨구지
  않는지 확인, 필요 시 기존 엔트리의 extra 키 보존 병합. (현재
  `get_capabilities`는 엔트리가 있으면 probe를 안 타므로 도달 희박 —
  방어적 확인.)
- **A2** setup 위저드(`_prompt_model_capabilities`)의 entry 재작성 경로 동일 확인.
- **A3** conftest 전역 react-pin(단위 스위트) — 플래그 default 변경·ctx-우선
  폴백이 픽스처와 충돌하지 않는지.
- **A4** role model 오버라이드 시 capabilities 미재해석은 기존 동작 그대로
  둔다 — 바인딩만 모델명-키 조회라 영향 없음. (별도 이슈로 인지만.)
- **A5** 웹 `_maybe_resume_recent` 외 세션 진입점(보드 등)에서 meta 포맷이
  무시되는 곳이 더 있는지 grep.

## 8. 테스트 계획 (TDD — red 먼저)

**해석 체인 (신규 `tests/test_wire_format_binding.py`)**
1. `explicit` 지정 → 바인딩·메타 무시하고 explicit.
2. explicit 없음 + resume 메타 있음 → 메타 포맷.
3. explicit·메타 없음 + 모델 바인딩 있음 → 바인딩 포맷.
4. 전부 없음 → `DEFAULT_WIRE_FORMAT` (바이트 동일 경로).
5. unknown 이름(explicit / 바인딩 각각) → KeyError → boot exit 2.
6. `wire_format_for_model`: 엔트리 없음·필드 없음 → None.

**서브에이전트**
7. role model에 바인딩 있음 → 서브 ctx.wire_format = 바인딩 (부모와 달라짐).
8. 바인딩 없음 → 부모 상속 (현행, 회귀 가드).
9. 바인딩이 unknown 이름 → delegate/spawn 거부 에러.
10. **G2 회귀**: 부모 ctx가 react일 때 서브 loop `cfg.wire_format`도 react
    (ctx-우선 폴백) — 현재는 red인 잠복 버그 재현 테스트.

**resume (G1)**
11. react로 만든 세션을 플래그 없이 resume → loop·ctx 모두 react.
12. 같은 세션을 `--response-format md_array` 명시 resume → md_array로 일관 전환.
13. `response_format` 필드 없는 구세션 meta → DEFAULT (I4).

**parity/회귀**
14. 기존 전체 스위트 무회귀 (바인딩·플래그 미사용 시 바이트 동일 경로).

## 9. Phase 2/3 미리보기 (참고만 — 본 문서 범위 외)

- **Phase 2 — xml_fc 플러그인**: `<think>`/`<tool_call>` 계열. 문법 확정
  (Hermes JSON-inside-tags vs `<function=/parameter=` 태그-파라미터)은
  대상 모델 chat template 실측 후. 관찰 되먹임(`<tool_response>`) 포함
  여부 검토. self-contained + parity 테스트 3-포맷 확장 + **bakeoff 필수**.
- **Phase 3 — foreign-format 구제**: 바인딩 포맷 parse 실패 시 타 등록
  포맷 파서 시도 → `FAILURE_FOREIGN_FORMAT` 라벨. B→C 재조립 특성 덕에
  mimicry-safe. 모델별 누출 실측 데이터 확보용.

## 10. 결정 포인트 (승인 요청)

| # | 질문 | 추천 | 대안 |
|---|------|------|------|
| D1 | 서브에이전트: 세션 명시 플래그 vs 모델 바인딩 | **바인딩 우선** — 플래그는 main 스트림에 대한 선택, 서브 모델 프라이어 존중이 기능의 목적 | 플래그가 전 스트림 강제 (단순하지만 기능 무력화) |
| D2 | unknown 바인딩 이름 처리 | **fail-fast** (boot exit 2 / spawn 거부) — 조용한 폴백은 "바인딩 됐다고 믿는" 오진 유발 | warn + DEFAULT 폴백 |
| D3 | `--response-format` default `None` 전환 | **전환** — 명시성 감지의 유일한 방법, 미지정 시 동작은 바인딩 없으면 동일 | env-var 센티널 (복잡도만 증가) |
