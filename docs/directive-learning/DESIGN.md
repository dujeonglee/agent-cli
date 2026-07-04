# DIRECTIVE 학습 (Directive Learning) — DESIGN

> Status: **IMPLEMENTED (v4.24.0)** · **자동생성 제거 + 3축 프리셋/템플릿으로 개편 (v4.25.0)** · 2026-07-04
> 관련: `docs/session-memory/DESIGN.md`, `agent_cli/web/server.py`(learn/template/축별-library),
> `agent_cli/directive_presets.py`(축별 프리셋 스토어), `agent_cli/memory.py`(store),
> `agent_cli/prompts/system_prompt.py`(directive 로드)

## 0.5 v4.25.0 개편 — 🪄 자동생성 전면 제거 (CoT-leak)

- **왜**: 성격/업무 축의 🪄 통짜 자동생성을 27B 실증한 결과 **CoT leak** — omlx/Qwen3 가
  독립 산문 메타-호출에서 chain-of-thought 를 `reasoning_content` 채널이 아닌 **일반 `content`**
  로 흘려서 directive 본문이 오염됨. 억제 시도 전부 사멸: `/no_think` 무시,
  `enable_thinking:false`(chat_template_kwargs)는 모델 파손(`!!!!` garbage), JSON/센티넬 출력은
  CoT 가 요청 구조를 그대로 에코해 누설. 메인 ReAct 루프는 wire-parser 로 관용하나
  `_gen_directive` 산문 경로는 무방비.
- **결정**: 🪄(compose/personas 전체) **제거**. 성격/업무/지침 3축 유지하되 자동생성 대신
  - **📋 기본 템플릿**(`_AXIS_TEMPLATES`, 결정적·무LLM 골격을 `_zone_set` 으로 삽입) — 사용자가 직접 채움.
  - **📥 learn**(지침 축만; JSON 배열 출력이라 leak 충분히 회피 — 유일하게 남은 LLM 경로).
  - **💾 축별 프리셋**(persona/task/learned 각 서브디렉토리).
- **제거된 것**: `/api/directives/compose`·`/presets`·`/personas`, `_DIRECTIVE_ENHANCE_SYSTEM`·
  `_DIRECTIVE_STARTER_SYSTEM`·`_DIRECTIVE_PRESETS`·`_DIRECTIVE_PERSONA_SYSTEM`·`_DIRECTIVE_PERSONAS`·
  `_strip_persona_section`·`_merge_persona`. 내부 디버그 드로어 전용이라 외부 소비자 없음(MINOR).
- **아래 §0~§14 는 v4.24.0 시점 As-built** — 🪄 compose 유지를 전제한 서술은 본 절이 우선한다.

## 0. As-built (구현 시 확정된 편차)

- **프리셋 이름 검증**: §5.2/§6/§12 초안의 `[a-z0-9-_]` slug 는 **한글 이름을 죽여서** 폐기.
  대신 `_safe_name` — 유니코드 letter·공백은 그대로 보존(라벨=파일명 stem), 경로 traversal
  (`/`·`\`·`.`·`..`·dotfile)만 거부. Korean preset 이름이 round-trip 그대로.
- **빌트인 스타터 블렌드 보류 → v4.26.0 에서 되살림**: 초안의 "빌트인을 라이브러리에 섞기"는
  당시 빌트인이 *생성 시드*(LLM gen)라 *파일 로드*(결정적)와 의미가 갈려 **보류**했었다.
  v4.25.0 에서 🪄 생성 자체가 사라져 **모든 프리셋이 파일 로드로 통일**되자 그 충돌이 없어졌고,
  v4.26.0 에서 빌트인을 **정적 .md 파일**(`directive_presets_builtin/<axis>/*.md`, wheel 포함)로
  넣었다. `list_presets` 가 `source:"builtin"`(읽기전용, 드롭다운 📦)로 병합·`load` 폴백,
  `save`/`delete` 는 홈 스토어만(같은 이름 user 프리셋이 shadow). learned 축은 세션 파생이라
  빌트인 없음. 성격 2·업무 3(업무엔 `memory` 적극 활용 원칙 포함).
- **피드백**: 별도 토스트 시스템 대신 드로어의 기존 `$dirStatus` 라인 재사용(UI 최소 추가).

## 1. 배경 / 문제

현재 🪄 Directives 자동 생성은 **성격(persona)** + **업무(task)** 두 축을 백지에서
LLM으로 통짜 생성한다. 실사용에서 **업무 축의 통짜 자동생성이 위험**하다고 판단됐다 —
근거 없는 일반론(hallucinated boilerplate)이 나오기 때문. (성격 축은 저위험이라 유지.)

대신 원하는 것: **실제로 일어난 세션 경험에서 배운 것을 축적**하는 것.
같은 방에서 같은 작업을 반복하면 경험이 DIRECTIVE에 쌓이고, 이를 프리셋으로 저장해
다른 방·다른 agent-cli 인스턴스에서도 재사용한다.

### 1.1 실증으로 확정된 전제 (중요)

DIRECTIVE 학습의 소스로 **세션 메모리(memory 도구)**를 쓰려 했으나, 실측 결과
**Qwen3.6-27B는 memory 도구를 자율적으로 전혀 안 쓴다**:

| 개입 | memory 자율 기록 |
|---|---|
| Baseline (도구 설명만) | 0 |
| + 항상-노출 Session Memory 섹션 넛지 | 0 |
| + Task Guidelines 명령형 (Primacy) | 0 |
| + DIRECTIVE 명시 사용자 지시 | 0 (발견 순간에도 무시) |
| 실전 451턴 dead-end 세션 | 0 |

515+ 실턴, 4각 검증 전부 0. 태스크-집중 모델은 곁다리 기록 도구에 손을 안 뻗는다.

**결론**: 기록 주체를 "일하던 에이전트(자율)"가 아니라
**"사용자가 트리거하는 전용 학습 호출 + 시스템"**으로 전환한다.
전용 호출은 유일 임무가 "교훈 추출"이라 확실히 수행된다(페르소나·스타터 생성이 잘 되는 것과 동일 원리).

## 2. 목표 / 비목표

**목표**
- 사용자 트리거로 **현재 세션 컨텍스트**에서 재사용 가능한 지침을 추출해 DIRECTIVE에 반영.
- 반영 결과를 **관리되는 섹션에만** 누적(수기 부분 불가침, 비대 방지).
- DIRECTIVE를 **프리셋으로 저장/로드**(유저 홈, 모든 인스턴스 공유).
- 추출한 교훈을 **memory 스토어에 기록**해 이번 세션 내 recall + 구조화 중간표현으로 활용.
- 모든 반영은 **미저장 → 검토 → 저장** (기존 🪄 안전망 유지).

**비목표**
- 에이전트의 **자율 memory 기록**을 되살리는 것(실측상 불가 — 별도 트랙, §9).
- 성격(persona) 축 변경(현행 자동생성 유지).
- 세션 종료 시 **자동** 학습(전부 사용자 트리거).
- config 스키마 변경·세션 resume 포맷 변경.

## 3. 확정된 결정

| 항목 | 결정 |
|---|---|
| 학습 소스 | **대화 컨텍스트**(`ctx.get_messages()`). memory 의존 아님(비어 있으므로). |
| 반영 방식 | **관리 섹션 `## 학습된 지침`에 통합-재생성**(수기 부분 바이트 불가침). |
| 프리셋 저장소 | `~/.agent-cli/directive-presets/*.md` **별도 라이브러리**(전역 DIRECTIVE.md와 분리). |
| 성격 축 | 현행 자동생성 유지. |
| memory 도구 | **무변경, LLM 노출 유지**(자율 사용 기대 안 하되 가능 — "만에 하나"). |
| memory 스토어 | **학습 파이프라인의 substrate로 재목적화**(시스템이 기록). |

## 4. 아키텍처 / 데이터 흐름

```
[Directives 드로어]
  성격 [▼ 없음] [🪄 성격 생성]
  업무 [프리셋 ▼ …] [💾 프리셋 저장]
  [📥 이 세션에서 학습]            [취소] [저장]
  ┌────────────── 에디터 (방의 .agent-cli/DIRECTIVE.md) ──────────────┐
  │ (사용자 수기 지시)                                                 │
  │ ## 학습된 지침   ← 관리 섹션 (학습이 이 부분만 갱신)               │
  │ - …                                                                │
  └────────────────────────────────────────────────────────────────────┘

📥 학습 파이프라인 (POST /api/directives/learn):
  1. ctx.get_messages() = 현재 대화·관찰
  2. 전용 distillation 호출 (유일 임무 = 이식 가능한 교훈 추출)
     └ 입력: 컨텍스트 + 기존 ## 학습된 지침(중복/통합용)
     └ 출력: 구조화 교훈 [{type, summary, detail}, ...] (JSON)
  3. 시스템이 각 교훈을 memory.add() 로 기록  ← 코드, 결정적 (모델이 도구 호출 X)
     └ ## Session Memory 인덱스 갱신 → 이번 세션 즉시 recall
  4. 교훈을 ## 학습된 지침 블록으로 렌더 → _replace_managed_section 으로 에디터에 반영
     └ 에디터 미저장(dirDirty) → 사용자 검토 → 저장 → 방 DIRECTIVE.md
```

핵심: 모델이 `memory(mode=add)`를 자발 호출하는 게 아니라, **전용 호출은 구조화 교훈을
출력만** 하고 **시스템 코드가 memory와 DIRECTIVE 양쪽에 기록**한다(결정적, 신뢰 가능).

## 5. 데이터 모델 / 저장

### 5.1 관리 섹션
- 마커: `## 학습된 지침` (heading ~ 다음 `## ` 직전까지).
- 페르소나의 `_strip_persona_section`/`_merge_persona`를 **일반화**:
  `_replace_managed_section(md, heading, new_body) -> str` (해당 섹션만 교체, 나머지 불가침).
  기존 `_strip_persona_section`/`_merge_persona`도 이 헬퍼로 리팩터(중복 제거).

### 5.2 프리셋 라이브러리
- 위치: `~/.agent-cli/directive-presets/<slug>.md` (라벨 = 파일명 stem).
- 형식: 순수 DIRECTIVE.md 본문(성격/수기/학습 섹션 포함 가능).
- 전역 `~/.agent-cli/DIRECTIVE.md`(항상 적용)와 **분리** — 프리셋은 "골라 쓰는" 라이브러리.
- 빌트인 4개 스타터(`_DIRECTIVE_PRESETS`)도 같은 목록에 `builtin:` 으로 섞어 노출.

### 5.3 memory 재사용
- 교훈 = `memory.add(session_dir, type=…, summary=…, detail=…)` 그대로.
- 타입 매핑: distillation 출력 type ∈ `failure|discovery|decision|note` (기존 VALID_TYPES).

## 6. API (신규/변경)

기존 `POST /api/directives/compose`(성격/업무 통짜생성)는 **성격 전용으로 축소** 또는 유지
(업무 통짜생성 경로는 프리셋으로 대체). 신규:

- **`POST /api/directives/learn`** (토큰 인증)
  - body: `{content}` (현재 에디터 내용 = 기존 수기+학습 섹션).
  - 서버: `ctx.get_messages()` → `_gen_directive`류 distillation 호출 → 교훈 JSON
    → `memory.add()` 반복 → `_replace_managed_section(content, "## 학습된 지침", block)`.
  - 반환: `{content}` (미저장) + `{learned: N}` (기록된 교훈 수).
  - 503: LLM 미배선 / ctx 없음. 컨텍스트가 비면 `{learned:0}` + 안내.
- **`GET /api/directives/presets/library`** — `[{id, label, source: builtin|user}]`.
- **`POST /api/directives/presets/library`** — `{name, content}` → 홈에 저장(slug 검증).
- **`GET /api/directives/presets/library/{id}`** — 프리셋 본문 반환(로드용).
- **`DELETE /api/directives/presets/library/{id}`** — user 프리셋 삭제(builtin 불가).

경로/이름 검증: slug는 `[a-z0-9-_]`만, traversal 차단(`_safe` 헬퍼 재사용).

## 7. UX / UI (정적 프론트)

`agent_cli/web/static/index.html` + `app.js` + `style.css` (렌더는 정적 프론트 집중).
- **업무 드롭다운** → **프리셋 드롭다운**(library + builtin) + **💾 저장** 버튼.
- **📥 이 세션에서 학습** 버튼 → `learn` 호출 → 에디터 미저장 갱신 + 토스트(`N개 학습됨`).
- 성격 드롭다운 + 🪄 는 현행 유지.
- 프리셋 로드/저장 성공 시 토스트(agent-board 패턴과 동일한 가벼운 피드백).

## 8. 열린 디테일 (승인 시 확정) — 추천값 제시

1. **학습 스텝 수** — *추천: 1스텝, 이중출력.* 클릭 한 번에 (a) memory 기록 + (b) 에디터
   `## 학습된 지침` 미저장 갱신. memory = 세션 recall/로그, 에디터 = 승격 후보. 저장은 사용자.
2. **distillation 출력 상세도** — *추천: memory에는 summary+detail 저장, DIRECTIVE 섹션은
   summary 기반 간결 규칙.* (지침은 실행 가능한 한 줄, 상세는 memory에서 참조.)
3. **프리셋 로드 = 교체 vs 머지** — *추천: 에디터 내용 교체(미저장).* 프리셋은 통짜 DIRECTIVE라
   교체가 자연스럽고, 미저장→검토→저장이라 안전(취소 가능). 조합은 로드 후 사용자 편집.

## 9. distillation 프롬프트 (품질 핵심)

시스템 프롬프트 요지(as-built `_LEARN_SYSTEM` — **도메인 중립**):
> 너는 방금 끝난(또는 진행 중인) 세션에서 **다음 같은 작업에 재사용 가능한 교훈만** 추출한다.
> 세션은 코딩·로그/데이터 분석·리서치·운영·작문 등 **무엇이든 될 수 있으니 도메인을 가정하지 마라**.
> 세션 특정 사실(예: 특정 에러 메시지·파일/줄/레코드·일회성 값/이름)은 **제외**하고,
> 다른 유사 작업 인스턴스에도 도움되는 이식 가능한 운영 지침만.
> 기존 `## 학습된 지침`이 주어지면 **중복 제거·통합**해 재생성한다(누적하되 비대 금지).
> 출력: JSON 배열 `[{type: failure|discovery|decision|note, summary: "한 줄", detail: "선택"}]`.
> 교훈이 없으면 `[]`.
>
> (초안은 "코딩 세션"·"test 9 hang"·"line 143"으로 코딩을 전제했으나, 로그 분석 등 다른 세션
> 타입에도 쓰이므로 도메인-중립으로 확정. real 27B 로 코딩/로그분석 두 세션 모두 이식 가능 교훈
> 추출 검증.)

- 입력 컨텍스트가 크면 상한 적용(최근 N 메시지 / 토큰 예산) + 상한 로깅(silent truncation 금지).
- `_strip_code_fences` 재사용, JSON 파싱 실패는 복구 파이프라인(md_array 유틸) 또는 재시도.

## 10. 안전 / 불변식

- **관리 섹션 외 불가침**: `## 학습된 지침` 밖 텍스트(수기 지시)는 바이트 그대로.
- **미저장→검토→저장**: learn/프리셋 로드 모두 에디터 미저장 상태로만 반영.
- **결정적 기록**: memory/DIRECTIVE 쓰기는 시스템 코드(모델 자율 아님).
- **프로젝트 파일 전용**: 방의 `.agent-cli/DIRECTIVE.md`만 편집(전역 `~/.agent-cli/DIRECTIVE.md` 미편집).
- **resume 무영향**: 신규 파일(프리셋)·기존 memory.jsonl 포맷 재사용 → on-disk 포맷 불변.

## 11. 엣지 케이스

- 컨텍스트 비었음(세션 초반) → `{learned:0}` + "아직 배울 내용이 없어요".
- 교훈 0개 추출 → 에디터 무변경 + 안내.
- 기존 `## 학습된 지침` 없음 → 새로 생성(수기 섹션 뒤/앞 배치 규칙 §5.1).
- 프리셋 이름 충돌 → 덮어쓰기 확인 or 자동 넘버링(확정 필요, 추천: 덮어쓰기 확인).
- LLM 미배선 → learn/🪄 버튼 숨김(compose와 동일).

## 12. 구현 단계 (TDD)

1. **관리섹션 헬퍼 일반화** — `_replace_managed_section` + 페르소나 헬퍼 리팩터. (단위테스트)
2. **프리셋 라이브러리 스토어** — 홈 저장/로드/목록/삭제 + slug 검증. (단위테스트)
3. **learn 파이프라인** — distillation 호출 + memory.add + 관리섹션 반영. (`_FakeProvider`로 테스트)
4. **엔드포인트** — learn / presets library CRUD. (`TestClient`)
5. **프론트** — 프리셋 드롭다운·저장·학습 버튼·토스트. (`node --check` + 수동)
6. **문서** — README(사용법) + ARCHITECTURE(엔드포인트·플로우·LOC) 동기.
7. **실증** — real 27B로 learn 호출이 실제 유의미한 교훈을 뽑는지 확인 후 릴리스.

## 13. 테스트 계획 (red 우선)

- 관리섹션: 교체가 수기 부분 보존 / 섹션 없을 때 생성 / 중복 헤딩 안전.
- 프리셋: 저장→목록→로드 라운드트립 / builtin+user 병합 목록 / traversal 거부 / 삭제(builtin 불가).
- learn: 컨텍스트→교훈→memory 기록 수 / 관리섹션 반영 / 빈 컨텍스트 0 / 기존 학습섹션 통합(중복 제거).
- 엔드포인트: 토큰 인증 / LLM 미배선 503 / 미저장 반환.
- 회귀: 기존 compose/persona 테스트 무손상.

## 14. 미해결 / 후속

- **memory 자율 기록(옵션 2 — 시스템 자동 캡처)**: 도구 에러·ACTION_LOOP·compaction 드롭 요약을
  시스템이 memory에 자동 적재. 본 설계와 독립. 수요 생기면 별도 DESIGN.
- distillation 품질(이식 가능 교훈 필터)은 real 세션으로 반복 튜닝 필요(§9).
