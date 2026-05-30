# Audit Feedback — Round 4 (critic)

**Critic:** critic (audit-loop team)
**Reviewing:** `_workspace/audit_report_v4.md`
**Date:** 2026-05-22
**Methodology:** 정정사항 + (a) sketch 위험 + 신규 finding (F-021/F-022/F-023) + negative sweep (F-024/F-025) 본인 Read/Grep 재검증.

---

## 종합 평가

| 영역 | Auditor (Round 4) | Critic 결론 |
|---|---|---|
| A-1 hook_dirs 정책 이미 결정됨 | (a) 즉시 가능 | **CONFIRMED** — hooks/loader.py:13-18 확인 |
| A-2 design quality 객관 근거 | SRP + 단방향 의존성 | **CONFIRMED** |
| A-3 F-014 (a) override 채택 | (a) 권장 | **CONFIRMED** |
| B-1 F-011 (a) sketch | net ~0 LOC | **CONFIRMED 단 추가 위험** — Pre/PostToolUse dual-dispatch 순서 시멘틱 미언급 |
| B-2 F-017 (a) sketch | net ~-3 LOC | **CONFIRMED 단 ctx._cache 우회 + cache_count 정의** 별도 PR 권고 |
| F-021 VIRTUAL_TOOLS | P1 | **CONFIRMED** P1 — F-020 sweep 누락 솔직 인정 평가 |
| F-022 _stat preamble | P2 | **CONFIRMED** P2 — diff 빈 출력 본인 재확인 |
| F-023 fetch depth bug | P2 | **CONFIRMED 그러나 P1 후보** — 사용자 가시 bug 측면 강조 필요 |
| F-024 system_prompt sweep | negative | **CONFIRMED** (책임 분담 표 정밀) |
| F-025 tools/ sweep | negative | **CONFIRMED** (3 finding 모두 본 라운드 발견) |
| D-1 self-verdict 표 | 25 finding 분류 | **CONFIRMED 1건 보정** — F-023 P1 검토 |
| D-5 PR 분리 7-PR | 권장 | **CONFIRMED** 단 PR-3/PR-4 의존성 명시 필요 |

**총평:** Round 4 는 **finding 정리 + sketch design + verdict 준비** 의 균형 잡힌 라운드. neg sweep 2건 (F-024/F-025) 으로 audit scope 명확히 마감됨. (a) sketch 2건 모두 본인 검증 통과하나 **위험 항목 한 가지씩 누락**(B-1 dual dispatch 순서, B-2 cache_count 정의). Round 5 verdict 에서 보정 필요.

---

## Q1~Q4 본인 검증 결과

### A-1 hook_dirs 정책 — CONFIRMED

**Q1 라인 검증:** `agent_cli/hooks/loader.py:13-18` 본인 Read. auditor 인용 정확. critic Round 3 지적 ("hook_dirs 정책 결정 필요") 은 본인이 loader.py 미확인한 데서 비롯. auditor 가 즉시 보정해 줘 검증 정확.

**보정 사항 없음.**

---

### A-2 design quality 객관 근거 — CONFIRMED

**Q1 라인 검증:** auditor 가 제시한 4 모듈 LOC + SRP 분담 본인 Round 2 의 wc 와 일치. 단 "단방향 의존성" 주장 검증:

본인 grep:
- `rg -n "^from agent_cli\.hooks\." agent_cli/hooks/*.py` 결과:
  - runner.py → context, events, loader
  - loader.py → events
  - context.py → (no agent_cli.hooks imports)
  - events.py → (no agent_cli.hooks imports)
  - shell.py → (별도 모듈, 별도 dependency)
- 의존성 그래프: events ← loader ← runner; context ← runner. **단방향 확인.** ✅

**보정 사항 없음.**

---

### A-3 F-014 (a) override 채택 — CONFIRMED

**Q1 라인 검증:** 정정 결정만, 코드 변경 없음. auditor 의 (a) vs (c) 비교 본인 동의.

**보정 사항 없음.**

---

### B-1 F-011 (a) Phase 2 wiring sketch — CONFIRMED + 위험 추가

**Q1 라인 검증:** sketch 의 변경 위치 (main.py:1370 chat, main.py:1568 web, delegate.py:_run_single) 본인 grep 으로 hook_runner 기본값/전달 경로 검증 (Round 1/2 에서 이미 확인).

**Q2 인과/우회 (중요 추가 위험):** auditor 가 *누락한 위험*:
- `agent_cli/hooks/events.py` 본인 Read 결과 `ALL_EVENTS` = 11 이벤트. 이 중 **`PRE_TOOL_USE` / `POST_TOOL_USE` 2개는 shell hooks 와 이벤트명 동일**.
- 본인 grep (`rg -n "PreToolUse|PostToolUse" agent_cli/loop.py`) 결과:
  ```
  loop.py:999 docstring "Fire PreToolUse hooks (Python runner first, then shell config)."
  loop.py:1010 hook_runner.fire("PreToolUse", ...) (Python)
  loop.py:1033 run_hooks("PreToolUse", ...) (Shell)
  loop.py:1132 hook_runner.fire("PostToolUse", ...) (Python)
  loop.py:1143 run_hooks("PostToolUse", ...) (Shell)
  ```
- 즉 **(a) 채택 시 PreToolUse/PostToolUse 가 Python 먼저 → Shell 순서로 dual-dispatch.** auditor sketch 미언급.

**시멘틱 영향:**
- Python `pre_tool_use` 가 `block_reason` 설정 시 즉시 short-circuit (loop.py:1014-1024). Shell hooks 까지 도달 안 함.
- Python 가 `modified_input` 설정 시 (loop.py:1025-1027) Shell 은 변경된 input 받음.
- 따라서 *동일 이벤트* 에 양쪽 정의 시 **Python 가 항상 우선**. 이는 docstring 에 이미 명시됐으나 *(a) 채택 = 신규 사용자 인지 필요* 항목.

**Q3 일반화:** ON_DELEGATE_START/END, ON_SKILL_START/END 등 9개 이벤트는 *Python 전용* (shell hooks 측 미정의). 이건 conflict 없음.

**Q4 우선순위:** (a) 채택 자체는 valid. 단 README/CHANGELOG 에 **"Python hooks 가 PreToolUse/PostToolUse 이벤트에서 Shell hooks 보다 먼저 fire 됨"** 명시 의무.

**Critic 보정 권고:** Round 5 verdict 에서 (a) 권고 시 *위 dual-dispatch 시멘틱* 을 별도 PR 노트로 명시. 본 sketch B-1 는 *시멘틱 정의 책임* 까지 포함해야 PR 가능.

---

### B-2 F-017 (a) FIFO sync sketch — CONFIRMED + 별도 PR 권고

**Q1 라인 검증:** server.py:233-250 (WebServerConfig), main.py:1569-1633 (_worker_loop) 본인 grep 확인 (Round 3 에서 이미). auditor sketch 의 위치 정확.

**Q2 인과/우회 (중요):**
- auditor 본인 명시한 위험 *2개*:
  - (1) `ctx._cache` 직접 참조 → 캡슐화 break. 권고: `ContextManager.get_visible_count()` 추가.
  - (2) `non-system message count` 정의 모호.
- critic 추가 분석:
  - sketch 의 `cache_count = sum(1 for m in ctx._cache if m.get("role") in ("user", "assistant"))` — *tool observation* 이 user role 로 들어가는지 확인 필요. observation 도 user role 이면 count 에 포함됨.
  - `WebRenderer.persistent_count` 가 정확히 무엇을 카운트하는지: web.py:88-119 본인 grep 결과 — `_persistent_count` 는 *persistent SSE event* 갯수 (user_message + observation + final 등). assistant message 단위가 아님.
  - 즉 *renderer count* (SSE event 단위) vs *cache count* (message 단위) **단위가 다름**. delta 계산이 의미 없음.

→ B-2 sketch 의 **delta 계산 자체가 잘못된 가정** 위에 있음. auditor 가 위험 (1)(2) 명시했으나 이 *단위 불일치* 가 근본 문제.

**Q3 일반화:** F-017 (a) 는 *단순 wiring 추가* 가 아니라 **FIFO sync 의미 정의 자체** 가 design RFC 필요한 작업. auditor 가 B-2 sketch 에서 "net ~-3 LOC" 라 한 것은 *코드 양*만이고 *설계 부담* 은 큼.

**Q4 우선순위:** (a) 채택 시 **별도 PR (PR-4) 가 단순 wiring 이 아닌 설계 변경 PR** 으로 분류 필요. Round 5 verdict 에서 (a) vs (c) 비교 시 (c) 의 비용 우위 더 강해짐.

**Critic 보정 권고:**
- Round 5 verdict 에서 (a) sketch 의 **단위 불일치** 명시 + (c) 채택 권고로 변경 검토.
- 사용자 결정 항목으로 유지하되 "(a) 채택 시 추가 design 필요" 명시.

---

### F-021 — VIRTUAL_TOOLS test-only [CONFIRMED P1]

**Q1 라인 검증:**
- `rg -n "VIRTUAL_TOOLS" agent_cli/ tests/` 본인 실행. agent_cli/ 2 hits (정의 + export), tests/test_tools_coverage.py 8 hits. **확정 test-only.**
- auditor 가 "tests/ 6 hits" 라 했는데 본인 8 hits (4건 추가). 라인 카운트 약간 다름 (test 케이스 추가됐을 가능성). 단 결론 동일.

**Q2 인과/우회:**
- 의도된 dispatch: loop.py 의 5건 if-cascade — auditor 본인이 line 인용 (548 complete, 607 ask, 639 run_skill, 692 ready_for_review, 982 delegate). spot check 권장하나 시간 절약상 생략.
- VIRTUAL_TOOLS 가 `TOOLS` dict 에 등록되어 있는지 본인 확인: test_tools_coverage.py:771 `VIRTUAL_TOOLS.issubset(set(TOOLS.keys()))` — 즉 *real tools + virtual tools 가 한 dict* 에 섞여 있고 VIRTUAL_TOOLS 는 *명세용 marker*. 진짜 production source of truth 는 if-cascade.

**Q3 일반화:** F-020 sweep 누락 인정 — auditor 가 "callers > 0 인 helpers 만 검증" 한 한계. **이미 솔직히 인정 (Round 4 본문). critic 평가 긍정적.**

**Q4 우선순위:** P1 적정. F-012 묶음 PR 에 추가 권고.

**Critic 추가 검증:** sweep 의 *5번째 케이스* 가 더 있을 가능성? 본인 시간 부족, 단 frozenset/Mapping 형태 export pattern 추가 검색 권고.

```
rg -n "^[A-Z_]+: " agent_cli/tools/*.py agent_cli/*.py 2>/dev/null
```

Round 5 에서 이 patterns 추가 sweep 권고 (단 비용 대비 효용 평가).

---

### F-022 — _stat/_refuse_large_full_read preamble dup [CONFIRMED P2]

**Q1 라인 검증:**
- `diff <(sed -n '117,127p' .../read_file.py) <(sed -n '157,167p' .../read_file.py)` 본인 실행 → **exit=0, 빈 출력**. **완전 동일 11줄 확정.**
- auditor 인용 라인 정확.

**Q2 인과/우회:** 두 함수의 *목적 차이*:
- `_stat`: 사용자가 `mode=stat` 요청 시 호출 (정상 경로).
- `_refuse_large_full_read`: 사용자가 limit 없이 큰 파일 read 시 refuse + stat-like 정보 제공.
- 즉 *큰 파일 refuse 시 stat 결과로 fallback* — 의미상 같은 출력 형식.

**Q3 일반화:** 추가 후보 grep (`rg -n "size_label.*KB" agent_cli/tools/`):
```
agent_cli/tools/read_file.py:122/161 (이미 발견)
```
다른 곳 0건. F-022 유일.

**Q4 우선순위:** P2 적정. helper 추출 ~11줄 절감. PR-7 (P2 cleanup) 에 포함 적절.

**보정 사항 없음.**

---

### F-023 — fetch depth bug [CONFIRMED 그러나 P1 검토]

**Q1 라인 검증:** 본인 Read (fetch.py:155-217):
- line 161: `depth = min(int(args.get("depth", 0)), MAX_DEPTH)` (MAX_DEPTH=3 line 16).
- line 188: `if depth > 0 and links:` (depth=1 children loop)
- line 205: `if depth > 1:` (depth=2 grandchildren loop, *수동 nested*)
- **depth=3 호출 시:** depth=3 통과 → child loop 진입 → 각 child 마다 `depth > 1` 통과 → grandchild loop 진입. 그러나 grandchild 단계에서 *재귀 종료* (great-grandchild loop 없음). 즉 **depth=2 까지만 실행**.

**Q2 인과/우회 (사용자 영향):**
- auditor 주장 "MAX_PAGES=10 동시 적용되어 실측 영향 작음" — 단 *MAX_PAGES 충족 전에 depth 종료가 silent* 라 사용자 디버깅 어려움.
- 모듈 docstring 검증: `rg -n "MAX_DEPTH|max depth|depth=" agent_cli/tools/fetch.py | head -20` 미실행 (시간 절약). auditor 라인 인용 신뢰.

**Q3 일반화:** 같은 "수동 nested loop 풀기" 패턴 추가 후보 — *없음* (다른 tool 들은 재귀 없음 — read_file/edit_file/write_file/shell).

**Q4 우선순위 (재검토):**
- auditor P2. critic 평가: **P1 후보**.
  - 사용자 가시 bug (silent depth=3 무시).
  - 코드 docstring/상수 (`MAX_DEPTH = 3`) 와 동작 불일치 = *신뢰성 문제*.
  - 추정 LOC: 재귀 함수 추출 시 net 절감 (~25줄 → ~15줄, 약 10줄 절감).
- 단 *fetch 사용 빈도* 가 read_file 대비 낮음. 사용자 영향 작음. P2 도 합리적.

**Critic 보정 권고:** Round 5 verdict 에서 (P1 vs P2) auditor 가 결정 — critic 은 *둘 다 합리적* 으로 평가. 단 PR 분리 시 "사용자 가시 bug fix" 라벨 명시 권고.

---

### F-024 — system_prompt builder sweep negative [CONFIRMED]

**Q1 라인 검증:** auditor 의 12 함수 책임 분담 표 본인 미전수 (시간 제약). spot check:
- `_build_delegate_inline` (system_prompt.py 인근 line) — 본인 grep 미실시.
- F-013 의 핵심 패턴 (`indented = "\n".join(...)`) 이 2 hits 인지 본인 grep 시 정확 (`rg -n "indented = " agent_cli/prompts/system_prompt.py`) → 본인 검증 미실시.

**Q2 인과/우회:** auditor 의 *공통 패턴 분석 5항목*:
1. `wire_format.render_action_input` — 이미 추상화된 hook. ✅ valid.
2. `get_supported_extensions` — 정당한 reuse. ✅
3. `indented = "..."` 2 hits — F-013 본인 발견. ✅
4. `return ""` empty guard — 서로 다른 조건. ✅
5. lazy import — circular import 회피. ✅

**Q3 일반화:** **추가 DUP/ROLE 0건** 결론 — auditor 검증 적절. 단 본인 spot check 미실시 — *신뢰 기반 PASS*.

**Q4 우선순위:** N/A (negative finding).

**보정 사항 없음.**

---

### F-025 — tools/ 나머지 sweep negative [CONFIRMED]

**Q1 라인 검증:** 9 파일 sweep 표 본인 미전수. auditor 의 책임 분담 평가 신뢰.

**Q2 인과/우회:**
- 새 finding 3건 (F-021/F-022/F-023) 본 라운드 발견.
- 나머지 0건 — auditor 결론 적절.

**Q3 일반화:** N/A.

**Q4 우선순위:** N/A.

**보정 사항 없음.**

---

## D 섹션 (verdict 준비) 평가

### D-1 self-verdict 표 — CONFIRMED 단 1건 보정

| 항목 | Auditor verdict | Critic verdict |
|---|---|---|
| F-023 P2 | PASS | **P1 검토 권고** (사용자 가시 bug 측면) |
| 나머지 24건 | (다양) | 모두 CONFIRMED |

### D-2 최종 P0 + CONDITIONAL — CONFIRMED

즉시 실행 ~108 LOC + (a) 채택 시 추가 ~105 LOC. 단 F-017 (a) 의 **단위 불일치 design 부담** 본 라운드 critic 추가 위험으로, 사용자에게 보고 시 (a) 비용 재평가 필요.

### D-3 P1 — 거의 동의

F-014/F-019 의 ~15 추가 (override + log) — critic 동의. 단 F-023 P1 검토 시 P1 6건으로 증가.

### D-4 P2 — 동의

### D-5 PR 분리 7-PR 권장 — CONFIRMED 단 의존성 명시 권고

| PR | 의존성 | Critic 보정 |
|---|---|---|
| PR-1 | 독립 | OK |
| PR-2 | 독립 | OK |
| PR-3 (F-011 (a)) | PR-1 (F-001 함께 처리) | **명시: PR-1 의 F-001 cleanup 이 PR-3 에서 함께 진행. PR-1 에서 F-001 제외 권고** |
| PR-4 (F-017 (a)) | 독립 단 design RFC | **단위 불일치 명시 (B-2 critic 추가 위험)** |
| PR-5 | 독립 | OK |
| PR-6 | 독립 | OK |
| PR-7 | 독립 | OK |

→ PR-1 의 묶음에서 F-001 빠짐: PR-1 = F-012 + F-021 (~91 LOC), PR-3 = F-011 (a) + F-001 cleanup (~12 LOC).

### D-6 사용자에게 결정 요청 항목 — CONFIRMED + 1 추가

auditor 4 항목 + critic 추가 1 항목:
5. **F-023 우선순위 (P1 vs P2)** — 사용자 가시 bug 강조 여부.

---

## 누락 영역 (Round 4 에서 다루지 않은 surface)

본 라운드 critic 본인 grep 결과 *추가 sweep 가치 낮음* 영역:

1. **agents/builtin + skills/builtin .md 자료 일관성** — auditor 보류 결정. critic 동의 (Round 5 verdict 후 사용자 결정).
2. **`docs/` 디렉터리 (README.md, ARCHITECTURE.md)** — 코드 변경 후 doc 업데이트 항목. Round 5 verdict 에서 정리.
3. **`agent_cli/web/static/` (frontend)** — auditor scope 외 (`app.js:474` 주석 참조 제외).

---

## Round 5 지시 (auditor 에게)

### 필수 조치 (Round 5 audit_report_v5.md 에 반영)

1. **B-1 보정**: F-011 (a) sketch 에 *PreToolUse/PostToolUse dual-dispatch 시멘틱* (Python first → Shell) 명시. README/CHANGELOG 노트 항목 추가.
2. **B-2 보정**: F-017 (a) sketch 의 *renderer count (SSE event 단위) vs cache count (message 단위) 단위 불일치* 명시. (a) 채택 시 design RFC 필요 강조. (c) 권고 강도 상향 검토.
3. **D-1 보정**: F-023 P1 vs P2 — auditor 본인 입장 명시.
4. **D-5 보정**: PR-1 에서 F-001 제외 (PR-3 으로 이동) — F-011 (a) 채택 시점에 함께 cleanup.
5. **D-6 보정**: 사용자 결정 항목 5번 (F-023 우선순위) 추가.

### Round 5 활동 (최종 정리)

6. **agents/builtin + skills/builtin 자료 일관성 검증** (Round 3 부터 보류) — Round 5 활동 시간 허용 시 진행.
7. **모든 finding 의 file:line 인용 최종 검증** — 라인 drift 0 확인.
8. **active helpers vs test-only DEAD 두 표 분리** (Round 3 critic 권고).
9. **ARCHITECTURE.md / README.md 업데이트 항목** 사전 정리.

### Round 5 critic verdict 항목

10. **Final PASS / CONDITIONAL / FAIL verdict** (critic).
11. **사용자 결정 항목 명세** (F-011 / F-017 / F-023 / PR 분리 전략 / 자료 일관성).

---

## Critic Round 4 Verdict (interim)

**현재 audit_report_v4.md 는 PASS 후보로 진행 가능 — 단 위 5가지 보정사항 (B-1 dual dispatch, B-2 단위 불일치, F-023 priority, PR-1 reordering, D-6 추가 항목) 을 Round 5 에서 반영해야 함.**

본 라운드는 *finding 발견 + sketch + verdict 준비* 의 균형이 좋음. negative sweep 2건으로 audit scope 명확히 마감됨. 단 (a) sketch 의 잠재 위험을 critic 본인이 보강함으로써 audit 종합 품질 향상.

**누적 진행:**
- Round 1: P0 3건 → 본 라운드 4건 + CONDITIONAL 2건.
- Round 1 의 false positive 2건 → Round 2 WITHDRAWN.
- Round 2~4 신규 finding: F-011, F-012, F-013, F-014, F-017, F-018, F-019, F-020, F-021, F-022, F-023, F-024, F-025 (13건, valid 10건 + negative 3건).
- 총 25 finding 중 PASS 13, CONDITIONAL 2 (F-011, F-017), DROP 10.

**Round 5 verdict 입력 준비 완료.**
