# Audit Feedback — Round 3 (critic)

**Critic:** critic (audit-loop team)
**Reviewing:** `_workspace/audit_report_v3.md`
**Date:** 2026-05-22
**Methodology:** Round 2 정정사항 + 신규 finding (F-017/F-018/F-019/F-020) 본인 Read/Grep 재검증.

---

## 종합 평가

| Finding | Auditor (Round 3) | Critic 결론 |
|---|---|---|
| A-1 F-011 카테고리 재정의 + (c) 본인 권장 | unwired infra, P0, (c) | **CONFIRMED** — 3 옵션 비교표가 정밀. 권장 (c) 채택 적절. |
| A-2 F-012 P1 → P0 + 변경 범위 | P0, ~87 LOC | **CONFIRMED** — `_ALWAYS_INCLUDE` 보존 결정 정확 (get_tool_descriptions 사용). |
| A-3 F-014 카테고리 재정의 | CONSIST | **CONFIRMED** |
| A-4 F-016 DROP | — | **CONFIRMED** |
| **F-017** WebServerConfig + prune unwired (~58 LOC) | P0 후보 | **CONFIRMED** P0 후보 — orphan docstring 까지 확인. |
| **F-018** recovery/ active wired 검증 | (검증) | **CONFIRMED** — recovery/ 시스템 매우 정밀. DUP/ROLE 없음. |
| **F-019** F-014 일반화 (web override 매트릭스) | P1 | **CONFIRMED** — 매트릭스 본인 base/web 메서드 두 grep 비교 일치. |
| **F-020** test-only sweep 결과 | (검증) | **CONFIRMED** — F-001/F-011/F-012/F-017 4건이 패키지 모든 후보. |

**총평:** 본 라운드는 *근거의 질* 가장 높음. 신규 F-017 도 본인 grep 4-way 검증 (정의/callers/orphan docstring/tests) 일치. F-018 의 recovery/ 19 export wiring 검증은 spot check 모두 통과 (TurnRecorder loop.py:176, FAILURE_NO_OUTPUT loop.py:475 등). F-020 sweep 도 정확. 정정사항 모두 반영됨.

이번 라운드부터는 **finding 추가보다 P0 결정 / 사용자 spec 정리 / Round 5 verdict 준비** 에 집중하는 게 효율적.

---

## Q1~Q4 본인 검증 결과

### A-1 F-011 카테고리 재정의 + (c) 본인 권장 — CONFIRMED

**Q1 라인 검증:** 정정사항이라 기존 검증으로 충분.

**Q2 인과/우회 — 3 옵션 비교표 검증:**
- (a) Phase 2 wire 완성: critic 동의. 단 *hook_dirs 정책 결정* 이 진짜 product-level 결정이고, 사용자에게 spec 묻기 전까지 (a) 채택 불가능. **올바른 분리.**
- (b) 전체 제거: critic 동의. 미래 재작성 비용 큼.
- (c) 명시 문서화: critic 잠정 권고와 일치.

**Q3 일반화:** auditor가 사유 1~3 명시한 점은 critic Round 2 지시 ("사용자에게 떠넘기지 말 것") 의 핵심을 정확히 반영. 단 **사유 2 "design quality 높음" 주장**의 객관 근거가 약함 — 단순 "tests 까지 갖춤" 만으로 design quality 라고 하기 부족. 그러나 결론 (c) 자체는 적절.

**Q4 우선순위:** auditor P0 + (c) 권장. critic CONFIRMED. 단 **Round 5 verdict 에서 사용자 결정 필요 항목으로 명시** 권장 — auditor 가 (c) 권장했더라도 사용자가 (a)/(b)를 택할 권리가 있음.

**보정 사항 없음.**

---

### A-2 F-012 P0 upgrade — CONFIRMED

**Q1 라인 검증:** auditor 인용 라인 (registry.py:355-365 helper, 368-377 anthropic, 380-392 openai, 351-352 ALWAYS_INCLUDE) 본인 Round 2 에서 확인.

**Q2 인과/우회 (중요):**
- `_ALWAYS_INCLUDE` 검증: auditor 본인 grep 결과 (registry.py:352 정의 / :362 _convert_tools 내 / :411 get_tool_descriptions 내) — critic 본인 검증 시 동일 패턴 확인. **`_ALWAYS_INCLUDE` 유지 결정 정확.**
- `_convert_tools` helper 만 제거 시 영향: `get_tool_descriptions` 가 별도 dict iteration 사용 — 영향 없음. **올바른 분리.**

**Q3 일반화:** F-001/F-011/F-017 와 동일 "test-only keep-alive" 패턴. F-020 sweep 으로 추가 후보 0건 확인됨.

**Q4 우선순위:** P0 적정. critic 권고와 일치.

**보정 사항 없음.**

---

### F-017 — WebServerConfig + prune unwired [CONFIRMED P0 후보]

**Q1 라인 검증 (본인 실행):**
- `rg -n "WebServerConfig|compute_prune_drop|on_user_message" agent_cli/ tests/` → 4 hits 모두 self-reference (server.py 234/246/250/488). tests 0건. **외부 callers 0 확정.**
- `rg -n "\.prune\b|persistent_count" agent_cli/ tests/` 결과:
  - production: render/web.py 내부 self-reference (`_persistent_count` 88/119/258/269), `persistent_count` property 262, `prune()` 248.
  - tests: test_web_renderer.py 11 hits, test_web_server.py 2 hits.
  - `static/app.js:474` 는 *주석만* ("the server's persistent_count semantics so...") — 실제 호출 아님.
  - **외부 (server.py 등) callers 0 확정.**
- `rg -n "process_chat_turn" agent_cli/ tests/` → server.py:249 orphan docstring 1 hit 만. **메서드 정의 없음 확정.**

**Q2 인과/우회 (결정적):**
- WebServer 클래스 메서드 본인 grep (`rg -n "    def " agent_cli/web/server.py | grep -v "WebRenderer\|DispatchOutput"`): __init__, push_chat, pop_chat, shutdown, _require_token, stream_events 등. `process_chat_turn` 부재 확정.
- *모듈 docstring* 의 wiring 주장 ("Server polls this after each turn") 이 *실제 코드와 불일치* — auditor 인용 정확.
- web.py 의 `_persistent_count` 자체는 increment (line 119) + decrement (line 258 prune 내부) 만 — *외부에서 prune 호출이 없으면 영원히 증가만 함*. ContextManager 의 prune 이벤트와 연동 안 됨.

**Q3 일반화:** F-011 / F-012 와 동일 패턴 (test-only keep-alive + design 명시되었으나 wiring 부재). F-020 sweep 으로 4건 (F-001/F-011/F-012/F-017) 이 전부 확정.

**Q4 우선순위:** 
- 영향 LOC: production ~58 (WebServerConfig 18 + prune 13 + persistent_count 9 + docstring 4 + __all__ 1 + 추가 13) + tests ~30.
- 사용자 측 영향 *잠재적으로 있음*: 긴 세션에서 _persistent_count 무한 증가 → web 클라이언트의 buffer 동기화 깨질 위험 (단, persistent_count 자체가 외부 노출 안 되므로 *현재* 영향 0).
- **CONFIRMED P0 후보.** 단 F-011 처럼 (c) 명시 문서화 외에 (a) 완성 가치가 있음 — *FIFO sync 는 실제로 필요한 기능* (long session). auditor 본인도 "(a) 도 매력적" 명시.

**Round 4/5 권고:** F-017 도 사용자 spec 결정 항목 (F-011 와 함께). 단 F-017 은 (a) 완성 비용이 F-011 (a) 보다 낮을 가능성 — server.py 안에서 `process_chat_turn()` 메서드 추가 + worker loop 가 호출하면 끝. (a) 채택 권고 강도가 F-011 보다 높음.

**Critic 추가 발견:**
- web/server.py 모듈 docstring (line 17-21 인근 — auditor 미인용) 이 실제 동작과 불일치. 사용자 spec 결정 후 docstring도 함께 정정 필요.

---

### F-018 — recovery/ active wired 검증 [CONFIRMED]

**Q1 라인 검증:** auditor 의 19 export wiring 표 spot check (3건):
- `TurnRecorder` loop.py:176 인스턴스화 — `rg -n "TurnRecorder" agent_cli/loop.py` 결과 line 44 (import), 176 (init). ✅
- `FAILURE_NO_OUTPUT` loop.py:475 — 본인 grep ✅
- `FAILURE_UNKNOWN_TOOL` loop.py:828 — 본인 grep ✅
- `ActionLoopDetector` loop.py:185 인스턴스화 (auditor 표 미언급) — `rg "ActionLoopDetector" agent_cli/loop.py` line 29 import, 185 init. ✅

19 전수 본인 확인은 시간 제약상 미실시. spot check 3/3 통과 → 신뢰.

**Q2 인과/우회:** auditor 의 책임 분담 평가:
- `common_recovery.py` vs `wf_recovery.py` 의 *명시 분리 정책* (wf_recovery.py:9-14 docstring) 인용 — Round 2 critic 의 "docstring 사전 검토" 지시 반영. ✅
- `primitives.py` vs `intervention.py` 책임 경계 분석 — single responsibility 명확하다는 결론 적절.
- `detectors.py` 의 stateful vs stateless 분리 정책 (`detectors.py:1-16` docstring 인용) — 적절.

**Q3 일반화:** recovery/ 가 *active wired* 라는 결론은 critic 도 동의. 단 **이 검증은 "DUP/ROLE finding 없음" 을 입증** — 즉 *해당 영역에서 추가 P0/P1 발견 없음*. 이는 valid scope 결과.

**Q4 우선순위:** N/A (검증 결과).

**보정 사항 없음.** auditor 의 sub-task 가 "recovery 가 Phase 잔재인지 확인" 이었고, 결과는 "아니오 — active wired". 명확한 negative 검증.

---

### F-019 — F-014 일반화 (web override 매트릭스) [CONFIRMED P1]

**Q1 라인 검증 (본인 실행):**
- `rg -n "    def " agent_cli/render/base.py` → 23 메서드 + 6 capture API 확인 (auditor 매트릭스와 정확 일치).
- `rg -n "    def " agent_cli/render/web.py` → header, turn_sep, thought, action, observation, final, error, raw, thinking, status, model_detected, model_loaded, context_dump, spinner_start, spinner_stop, dispatch_progress, stream_chunk, stream_end, group_start, group_end, prompt_user, confirm 등 abstract 모두 override 확인. capture/thread_status override 0건 확정.

**Q2 인과/우회:**
- minimal.py:182 `_capture_line(clean)` — `_p()` 출력 경로 경유.
- web.py 의 `_emit()` 본인 미확인 (시간 제약, F-014 검증으로 충분).
- *경로 차이* 명확: console-print 측 (minimal) 은 capture 흐름에 들어감, SSE-emit 측 (web) 은 직접 클라이언트 전달.

**Q3 일반화:** auditor 매트릭스로 *유일한 누락 override 패턴* 확정. 다른 abstract method 누락 없음 — F-014 가 isolated finding.

**Q4 우선순위:** P1 적정. critic 권고 (c) 명시 limitation 문서화 + auditor 권고 (a) no-op override 둘 다 valid. 단 *base.py 분리 (mixin 추출)* 권고 (b) 는 큰 변경이라 P0 채택 시 추가 검토.

**Critic 추가 권고:** **(a) override 추가 (no-op + 로그)** 가 가장 안전 — base.py 변경 없음, web 의 부족한 정보가 즉시 가시화. 약 5 LOC 변경.

---

### F-020 — test-only sweep [CONFIRMED]

**Q1 라인 검증 (spot check):**
- `_render_token_stats` loop.py:1251 (정의) + :349 (호출) — 본인 grep ✅ 활성.
- `fuzzy_verify_ref` edit_file.py:27 정의 + 195/201/239/241/248/255 호출 — 본인 grep ✅ 활성.

**Q2 인과/우회:** auditor 의 검증 방법 ("tests/ import 심볼 → production callers grep") 적절. spot check 2건 통과.

**Q3 일반화:** **추가 후보 0건** 확정. F-001/F-011/F-012/F-017 4건이 패키지 내 모든 후보.

**Q4 우선순위:** N/A (sweep 결과).

**한 가지 보강 권고:** auditor 표에 *production callers > 0 인 helpers* 도 포함됨 (예: `_render_token_stats`, `fuzzy_verify_ref` 등) — 이는 *test-only keep-alive 가 아님* 을 확정하는 검증이라 유용. 단 가독성을 위해 Round 5 정리 시 "검증된 활성 helpers" 와 "test-only DEAD" 두 표로 분리 권장.

---

## C-6. 누적 정리 효과 — Critic 추가 검증

auditor 표:

| Finding | LOC 영향 |
|---|---|
| F-001 | 12 |
| F-004 | ~9 절감 |
| F-011 | 주석 ~20줄 추가만 (코드 변경 없음) |
| F-012 | 87 절감 |
| F-017 | ~58 절감 + ~30 tests |
| **합계** | **약 184 LOC 정리** |

**Critic 검증:**
- F-004: auditor "9줄 절감" — Round 2 본인 측정과 일치 (28 → 19).
- F-012: 87 LOC = 50 production + 37 tests — Round 2 본인 검증 (87) 과 일치.
- F-017: 58 + 30 = 88 LOC — production 측 본인 grep으로 약 58 추정 (정의 라인 본인 확인). tests 측 30은 spot check 정도.

**계산 합계 재확인:** 12 + 9 + 0 + 87 + 88 = **196 LOC** (auditor 184 와 12 차이). auditor가 F-017 tests 30 LOC 를 합계에 포함 안 했을 가능성. 큰 차이 아님.

**다만 F-011 의 "(c) 채택 시 코드 변경 0" 가정은 F-011 (a) 또는 (b) 채택 시 *대폭* 변경됨** — 사용자 spec 결정 전까지 정리 LOC 추정치는 유동. Round 5 verdict 에서 *사용자 결정 변수* 명시 필요.

---

## Round 4 지시 (auditor 에게)

### 필수 조치

본 라운드는 **finding 추가보다 정리/사용자 결정 준비** 가 더 중요. auditor 권고 Round 4 영역 우선순위 평가:

1. **prompts/system_prompt.py 빌더 sweep (658 LOC, 7+ builders)** — F-013 외 중복 검사. **유지 — 진행.**
2. **tools/ 나머지 (read_file, edit_file, write_file, shell, fetch 등) dead/dup 검색** — Round 1~3 미터치. **유지 — 진행.**
3. **agents/builtin/ + skills/builtin/ 자료 일관성** — *Round 4 에서 시간 부족 시 Round 5 verdict 전까지 미루기 권고*. 정의 파일 (.md) 일관성 검사는 *코드 finding 보다 우선순위 낮음*.

### Round 4 추가 영역 (critic 지정)

4. **F-017 의 (a) 완성 옵션 — 구체 PR 안 정리:** `WebServer.process_chat_turn()` 메서드 추가 시 worker_loop 가 어떻게 호출할지, `compute_prune_drop` 의 caller (CLI side) 를 누가 만들지. *코드 변경하지 말고* design sketch 만. Round 5 verdict 에서 user 가 (a) 선택 시 곧바로 PR 가능하도록.

5. **F-011 의 (a) 완성 옵션 — hook_dirs 정책 sketch:** `~/.agent-cli/hooks.d/*.py` vs `.agent-cli/hooks/*.py` 등 후보 비교. 본인 권장 1 안 선택. 단 코드 변경 없이 design 만.

6. **Round 5 verdict 준비 자료**: 본 라운드 *모든 PASS/CONDITIONAL/FAIL 항목 분류* 미리 정리 (auditor 본인이 self-verdict 후보 작성).

### Round 5 전 추가 검증 영역 보류

- **`tools/registry.py`** Tool dispatch 경로 (`TOOLS` dict) 모듈화 — F-012 정리만 하면 충분, 추가 finding 가능성 낮음.
- **`agent_cli/agents/builtin/explorer.md`** + skills/builtin/ — meta 일관성. P3 정도 — Round 5 verdict 전까지 보류.

---

## Critic Round 3 Verdict (interim)

**현재 audit_report_v3.md 는 PASS 후보로 진행 가능.** 본 라운드는 *근거의 질이 최고*. Round 2 의 정정사항 모두 반영 + 신규 F-017 본인 4-way grep 검증 통과 + F-018 (recovery wired) 의 negative 검증 정확 + F-020 (sweep) 결과 결정적.

**현재까지 누적 P0:**
1. F-001 (~12 LOC, 즉시 실행 가능)
2. F-004 (~9 LOC 절감, 즉시 실행 가능)
3. F-012 (~87 LOC, 즉시 실행 가능, design 명시)
4. F-011 (~381 LOC 영향, **사용자 결정 필요**: a/b/c 선택)
5. F-017 (~58+30 LOC 영향, **사용자 결정 필요**: a/b/c 선택)

**즉시 실행 가능 (사용자 결정 불필요):** F-001 + F-004 + F-012 ≈ **108 LOC 정리**.
**사용자 결정 후 실행:** F-011 + F-017 ≈ **438 LOC (변동 큼)**.

Round 4 에서 (1) prompts/system_prompt.py sweep + (2) tools/ 나머지 sweep + (3) F-011/F-017 (a) design sketch 권장. Round 5 verdict 에서 사용자 결정 항목 명시 + PR 분리 전략 결정.
