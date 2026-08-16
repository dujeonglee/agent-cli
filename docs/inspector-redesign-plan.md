# Prompt Inspector 재설계 — 구현 계획 (승인됨)

상태: **구현 완료 (v8.11.0)** · 대상: agent-cli (web UI + skill 스코프 백엔드) · 미적: 플랫/솔리드

## 목표 (사용자 승인)
1. **역할 분리**: ⚡ 전역 Prompt Inspector 메뉴 제거 → 그 툴바 자리는 **📝 Directive Editor 전용**.
2. **컨텍스트 인스펙션**: 대화 카드(agent/skill)의 **🔍 프롬프트** 버튼 → 그 스코프의 프롬프트 패널(스코프 칩 없음). agent·skill **동일 뷰(전체 스냅샷)**.
3. **Main 인스펙션 유지**: 전역 메뉴가 사라져도 메인 루프 프롬프트를 볼 수 있어야 함 → 대화 푸터에 저키한 **🔍(메인)** 인라인 어포던스.
4. 플랫/솔리드 카드 + 의미색(system/대화) + 그룹 헤더 + 복사 + 전체접기 + 섹션 점유율 바.
5. 정리: `loadAllAxes()` 미정의 호출(ReferenceError) 제거 + 죽은 CSS(axes 행·`#dir-preset-modal`) 제거.

## 백엔드 — skill 스코프 id 통일 (핵심)
문제: skill 카드의 `data-task-id`(caller가 `render_begin_scope`로 만든 `_scope_id`)와, `execute_skill` 안에서 **따로 생성**한 `begin_prompt_scope` id가 달라 `GET /api/debug/prompt?task_id=<카드 id>` 가 `ok:false`.

근거(확인됨): `begin_scope`(caller)가 이미 `_thread_prompt_scopes[tid]` 에 `_scope_id` 를 push(render/web.py:49)하고, `end_scope`가 pop + `_finalize_prompt_scope`(동적 ctx 고정)까지 함(web.py:512-518, end_scope:28). 즉 caller가 스코프를 완전히 관리한다. `execute_skill`의 `begin_prompt_scope`는 **이중 push + id 불일치** 버그.

수정(`skills/executor.py:execute_skill`):
- 파라미터 추가 `scope_id: str = ""`.
- `_owns_scope = not scope_id`.
- `_owns_scope`일 때만 `begin_prompt_scope(minted)`/`end_prompt_scope(minted)` (직접호출·테스트용 폴백).
- `note_scope_ctx(skill_ctx)`는 **항상** 호출 — 현재 스택 top 스코프에 등록됨(caller 스코프면 카드 id에, 폴백이면 minted에).
- 두 caller가 `scope_id=_scope_id` 전달: `loop/skill_invoke.py:124`, `main.py:754`.

결과: skill 스냅샷 + 동적 ctx 가 카드 `data-task-id` 로 조회됨(agent와 동일). LLM 호출 전이면 기존대로 `ok:false`(empty state).

## 프론트엔드
### index.html
- 툴바: `#inspector-btn ⚡ "Prompt Inspector"` → `#directive-btn 📝 "Directive Editor"`.
- 드로어 2개:
  - `#directive-editor` + `#directive-backdrop`: 3탭·편집기·✨생성·저장/취소(기존 `#insp-dir` 내용 이관, `insp-dir-*` id 유지 가능).
  - `#inspector` + `#inspector-backdrop`: 스코프 제목 + 총계 바(turn·tok·섹션) + 미니액션(전체접기/전체복사) + 검색 + 섹션(그룹 헤더 SYSTEM/CONVERSATION). **스코프 칩 제거**.
- 푸터: `#insp-main-btn 🔍 "메인 프롬프트"` (저키).

### app.js
- `ensureTaskGroup`: 헤더에 `<button class="task-inspect" title="프롬프트">🔍</button>` 추가(run·skill 공통). click → `stopPropagation()` + `window.__openInspector(taskId, label, kind)`.
- **Directive Editor IIFE**(신규 분리): `#directive-btn` 토글, 기존 directive 함수 전부 이관(dirBuffers/tabs/generate/save/cancel + `agentcli:directives-changed` 리스너). `loadAllAxes()` 호출 삭제.
- **Prompt Inspector IIFE**(재작성): `window.__openInspector(scope, name, kind)` 로 오픈(스코프 고정), `loadPrompt(scope)` fetch. 렌더: 의미색(kind=system→--system, dynamic→--convo) + 그룹 헤더 + 토큰 pill + 점유율 바 + per-section 복사 + 전체접기/전체복사 + 검색. 스코프 칩/삭제/`loadScopes` 제거. `agentcli:prompt-changed`/`memory-changed`는 현재 열린 스코프 refetch. Main: `#insp-main-btn` → `__openInspector("", "Main", "main")`.
- esc() 인용부호도 이스케이프(속성 주입 안전).

### style.css
- 제거: `.insp-dir *`(에디터는 새 클래스로), 죽은 axes 행 규칙(1089-90)·`#dir-preset-modal`(1092-96), 위치기반 PALETTE 점 의존.
- 플랫/솔리드: 글래스 블러 완화, 솔리드 surface. 신규 컴포넌트: `.de-*`(directive editor), `.insp-*` 재정의(그룹헤더·의미색 좌측선 `.insp-sys`/`.insp-dyn`·점유율 `.insp-share`·복사 `.insp-cp`·미니액션), `.task-inspect`, `#insp-main-btn`.

## 테스트
- 백엔드: skill 스냅샷이 **카드 scope_id로 조회됨** 검증(신규, test_web_renderer 또는 test_web_server). 기존 main/delegate 격리 테스트 유지.
- 프론트: `node --check app.js`. test_app_markdown 하네스(첫 IIFE 추출)가 여전히 동작하는지(첫 IIFE에 `})();` 넣지 않기). 인스펙터/디렉티브 관련 서버 테스트 회귀 확인.
- 전체 `pytest tests/` + `ruff`.

## 문서 / 릴리스
- README(사용법: ⚡→📝, 대화 🔍), docs/ARCHITECTURE(있으면), CHANGELOG. MINOR 버전 bump(8.11.0 예상).
- 사용자 확인 후 커밋·푸시·wheel(사용자가 push 전 리뷰 선호 — 완료 후 결과 보고하고 승인받아 진행).

## 구현 순서 체크리스트
1. [x] 백엔드 skill scope 통일(executor + 2 caller) + 테스트
2. [x] index.html(툴바·드로어 2개·푸터 🔍)
3. [x] app.js(card 🔍 · Directive IIFE 분리 · Inspector IIFE 재작성 · loadAllAxes 제거)
4. [x] style.css(플랫/솔리드·신규 컴포넌트·죽은 CSS 제거)
5. [x] node --check + 테스트 + ruff
6. [x] 문서 + 버전
7. [ ] 결과 보고 → 승인 → 커밋·푸시·릴리스
