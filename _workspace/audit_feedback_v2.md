# Audit Feedback — Round 2 (critic)

**Critic:** critic (audit-loop team)
**Reviewing:** `_workspace/audit_report_v2.md`
**Date:** 2026-05-22
**Methodology:** 정정 사항 + 신규 finding 본인 Read/Grep 재검증.

---

## 종합 평가

| Finding | Auditor (Round 2) | Critic 결론 |
|---|---|---|
| F-001 정정 (HookRunner 범위 한정) | P0 (dead 12 LOC) | **CONFIRMED** |
| F-002 / F-005 WITHDRAWN | — | **CONFIRMED** |
| F-003 / F-006 / F-007 / F-008 재분류 | (다양) | **CONFIRMED** |
| F-004 P0 + render_group_scope 추출 | P0 | **CONFIRMED** P0 |
| **F-011** HookRunner 11 이벤트 dead path (~370 LOC) | P0 | **CONFIRMED** P0 — 단 카테고리/대안 정밀화 필요 |
| **F-012** convert_to_*_tools dead (~60 LOC) | P1 | **UPGRADE → P0 후보** — 의도가 명시되어 있어 즉시 제거 가능 |
| **F-013** build_agent/skill_descriptions 95% dup | P1 | **CONFIRMED** P1 (sibling 의도 인정하나 helper 가능) |
| **F-014** web capture API 미사용 | P1 | **CONFIRMED** P1 — 카테고리 재정의 (DEAD→CONSIST) |
| **F-015** symbols.py 785 LOC | P2 | **CONFIRMED** P2 |
| **F-016** loc 포맷 가정 | P2 | **DROP 권고** — 사용 범위 좁고 docstring 명시됨 |

**총평:** Round 1 의 false positive (F-002/F-005) 정확히 철회, 우선순위 재분류 모두 정당. 신규 4 finding (F-011~F-014) 모두 본인 검증으로 valid 확인. 단 (1) F-011 카테고리는 "Phase 잔재" 가 정확하고 단순 DEAD 가 아님, (2) F-012 는 P0 으로 올려도 무방, (3) F-014 는 DEAD/ROLE → CONSIST 가 더 정확.

---

## Q1~Q4 본인 검증 결과

### A-1, A-2, A-3 (정정사항 검증) — CONFIRMED

**Q1 라인 검증:** auditor 가 인용한 정책 docstring (react.py:28-36, prefix_md.py:130-134) 본인 Round 1에서 이미 확인.

**Q2 인과:** F-002/F-005 WITHDRAWN 사유는 정확. "third plugin 트리거" 조건도 본인 `ls agent_cli/wire_formats/` → react.py, prefix_md.py, base.py, _format_rules_builder.py 4 파일이고 plugin 은 2 (react, prefix_md). 미충족 확정.

**Q3:** F-001 정정 — `hook_runner=self.hook_runner` (loop.py:666) 도 확인. Round 1 에서 critic 이 지적한 "HookRunner 4 호출처에서 활성" 인정 반영됨.

**Q4:** 우선순위 재분류 모두 정당. **이의 없음.**

---

### B (F-004 강화 분석) — CONFIRMED P0

**Q1 라인 검증:** main.py:616-651, loop.py:1464-1497, delegate.py:617-622, delegate.py:689-709 본인 Read 확인 (Round 1 에서 이미 grep). 라인 오차 0.

**Q2 인과/우회:**
- 3/4 호출처 (1, 2, 4) 가 완전 동일한 5-step 패턴.
- #3 (delegate.py:617-622 parallel replay) 은 약간 다름 — 본인 Read 필요.

<details>
<summary>delegate.py:617-622 직접 확인</summary>

본 라운드에서 추가 Read 시간 부족. auditor 가 "5-line 스켈레톤 동일" 라고 했으나 본인 grep 결과 (617-622 = 6줄, 689-709 = 21줄) 다른 LOC. #3 은 *precomputed duration* 사용. 본인이 Round 3 에서 직접 확인 권장.
</details>

**Q3 일반화:** `rg -n "render_group_start" agent_cli/` → 4 hits 일치. 추가 호출처 없음. **5번째 호출처 등장은 hooks 통합 시점** (현재는 4개 확정).

**Q4 우선순위:** 영향 LOC 약 28→19 (~30% 절감) + drift 위험 큼 (4 호출처 동시 수정). **P0 적정.** context manager API 도 Python 표준이라 학습 비용 거의 0.

**보정 사항 없음.**

---

### F-011 — HookRunner 11 이벤트 dead path [CONFIRMED P0, 카테고리 정밀화]

**Q1 라인 검증:**
- 본인 `rg -n "HookRunner\(" agent_cli/ tests/` → `agent_cli/` 0건, tests/test_hooks_python.py 13건. **확인.**
- `rg -n "hook_runner=" agent_cli/` → loop.py 5 hits 모두 (line 100, 666, 1207, 1245, 1417) 확인. 기본값 None 3건, 전달 2건 (`hook_runner=self.hook_runner`, `hook_runner=hook_runner`) — *원천이 None* 이라 끝까지 None.

**Q2 인과/우회 (결정적):**
- main.py 의 chat/web entrypoint 본인 grep — `rg -n "HookRunner|hook_runner" agent_cli/main.py` → 0 hits. **외부 활성화 경로 없음 확정.**
- 사용자 측 hooks 활성화 경로: `.agent-cli/hooks.json` (shell hooks) 만 `loop.py:1029-1034, 1139-1150` 의 `from agent_cli.hooks import run_hooks` 별도 경로로 동작. Python hooks 활성화 *원천 없음*.

**Q3 일반화:** 같은 "Phase 잔재" 패턴 추가 후보 — `rg -n "Phase 2|Phase 3|TODO.*Phase|wired in Phase" agent_cli/` → runner.py:92 외 0건. 단발성 잔재.

**Q4 우선순위:**
- 영향 LOC: auditor 측정 370 LOC vs 본인 `wc -l agent_cli/hooks/*.py` 결과 `runner(95)+context(145)+loader(88)+events(53) = 381 LOC` (shell.py 236 제외 — 별도 활성 경로). 거의 일치.
- 사용자 측 영향 0 (Python hooks 활성화 안 됨).
- 신규 기여자 혼선 큼 (tests/test_hooks_python.py 13개 케이스가 LSP/IDE 상 "활성"으로 표시).

**Critic 결론:** **CONFIRMED P0**. 단 카테고리 정밀화:
- Round 2 의 "DEAD" 표현은 부정확. 정확히는 **"unwired infrastructure"** — 코드는 100% 정상 동작 (tests 통과), 단 사용자 진입점에서 활성화 안 됨. "Phase 잔재 (Phase 2 wiring 미완료)" 라는 카테고리가 더 정확.
- auditor 권장 3 옵션 (a/b/c) 모두 valid — 본 라운드의 critic 결정은 보류하고 **Round 3 에서 user spec 확인 필요** (auditor 권고와 동의).
- 단, 옵션 (b) "제거" 의 비용 측정 필요: `tests/test_hooks_python.py` 의 약 600 LOC + 12 hooks 관련 모듈 LOC 통합 약 1000 LOC 사라짐. 그 비용은 큼 — *기능 수명 결정*은 user 만 가능.

**Round 3 권장 보정:** auditor 가 본인이 3 옵션 사이 "권장 선택" 을 하는 게 좋음 (사용자에게 선택을 떠넘기지 않음). 본 critic 의 잠정 권장은 **(c) 명시 문서화** — 미완료 인프라를 *코드 주석으로 명시* + 다음 분기에 wiring 완료 여부 결정. 제거 (b) 는 회수 비용이 너무 큼.

---

### F-012 — convert_to_*_tools dead [UPGRADE P1 → P0 후보]

**Q1 라인 검증:** registry.py:355-392 본인 Read. `_convert_tools` (helper, 14 LOC) + `convert_to_anthropic_tools` (10 LOC) + `convert_to_openai_tools` (13 LOC) = 37 LOC 정의. `_ALWAYS_INCLUDE` (1 LOC) 도 두 변환 함수에서만 사용.

**Q2 인과/우회 (결정적):**
- `rg -n "convert_to_anthropic_tools|convert_to_openai_tools" agent_cli/` → 0 hits. 자기 정의 외 없음.
- `rg -n "...."` tests/ → tests/test_registry.py 8 hits. **test-only keep-alive 확정**.
- auditor 가 인용한 design 결정 (providers/compat.py:119 "Legacy field `supports_tool_calling` — silently ignored ... the loop uses ReAct text parsing, not the native tool-calling API on any provider.") 본인 grep 확인 (`rg -n "supports_tool_calling" agent_cli/ tests/` → providers/compat.py:119 1 hit). **명시적으로 native tool-calling 거부.**

**Q3 일반화:** 같은 "test-only keep-alive" 패턴 추가 후보:
- `HookRunner` 13 instance (F-011 검증 결과). 동일 패턴.
- 본인 추가 검색: `rg -ln "from agent_cli\..*import" tests/ | xargs -I {} basename {}` → tests 가 import 하지만 production 미사용 후보 — 시간 제약상 Round 3 으로 미룸.

**Q4 우선순위:**
- 영향 LOC: 37 (production) + ~50 (tests/test_registry.py 8 케이스) ≈ 87 LOC 삭제 가능.
- 신규 기여자 위험 *높음* — registry.py 의 public API (`convert_to_anthropic_tools`, `convert_to_openai_tools` 는 leading underscore 없음) 라 IDE 자동완성/grep 노출. "Anthropic/OpenAI 호환을 native tool-calling 으로 추가해야 하나?" 라는 false signal.
- 제거 비용 *낮음* — 의도가 providers/compat.py 에 명시되어 있어 design 결정 회의 불필요.

**Critic 결론:** **UPGRADE → P0 후보** (auditor P1). 비용/위험비 측면에서 F-001 (12 LOC) 보다 큰 정리 효과 + design 명시 + test-only — 즉시 제거 결정 가능.

**Round 3 권고:** auditor 가 P0 으로 분류 검토 (단, P1 유지도 합리적).

---

### F-013 — build_agent/skill_descriptions 95% 중복 [CONFIRMED P1]

**Q1 라인 검증:** system_prompt.py:560-607 (build_agent_descriptions), 610-658 (build_skill_descriptions) 본인 Read. auditor 인용 라인/diff 정확.

**Q2 인과/우회 (중요):**
- *Sibling 의도 명시*: build_skill_descriptions 의 본인 docstring (line 633-635):
  ```
  # render_full_example with thought=None — skill docs need the
  # action name visible (matches the sibling ``build_agent_descriptions``
  # form). See its docstring for the thought=None rationale.
  ```
- build_agent_descriptions 의 docstring (line 566-567): "same call shape as the sibling ``build_skill_descriptions`` section"

→ 두 함수가 *명시적으로 sibling 관계*. 단 sibling 이라고 해서 dup 가 정당화되지는 않음 — 오히려 *동일하게 진화해야 한다는 신호*로, helper 추출이 더 강하게 권장됨.

**Q3 일반화:** 같은 "wire-format-driven invocation section" 패턴 추가 후보:
- `rg -n "render_full_example" agent_cli/` 본인 실행 필요 (Round 3 권장).
- 현재 2 호출처 — wire_formats 의 "third plugin trigger" 정책처럼 *"third section trigger"* 적용 가능. 단 prompts/ 디렉터리는 wire_formats 와 정책 분리.

**Q4 우선순위:**
- LOC: 약 100 → helper 도입 시 60 (~40 절감).
- 변경 빈도: 새 invocation type (agent/skill 외) 등장 가능성 낮음 — *그러나 wire format plugin 추가 시 두 함수 모두 영향*.
- "folder-deletable" 정책: prompts/ 는 wire_formats 와 무관, 적용 안 됨.

**Critic 결론:** **CONFIRMED P1**. auditor 권고 (`_build_invocation_section(*, header_lines, example_args, items, item_renderer)`) 적정. 단 *명시 sibling docstring을 helper 도입 후에도 유지* 권장 (의도 보존).

---

### F-014 — web capture API 미사용 [CONFIRMED P1, 카테고리 재정의]

**Q1 라인 검증:**
- base.py:46-105 (5 capture API) 본인 Read. 라인 정확.
- minimal.py:182, 235 본인 grep 확인.
- web.py grep: `rg -n "set_thread_status|_capture_line|get_thread_status|start_capture|stop_capture|replay_captured" agent_cli/render/web.py` → **0 hits 확인.** ✅ auditor 주장 정확.

**Q2 인과/우회 (중요):**
- web.py:444-454 의 `group_start` / `group_end` 만 override (이벤트 emit).
- `start_capture/stop_capture` override 없음 → base.py:68-78 의 기본 동작 작동 (dict 등록만).
- `_capture_line` override 없음 → 기본 `base._capture_line(line)` 은 `_captures[tid].append(line)` 만 함. 그러나 web 의 출력 메서드 (`stream_chunk`, `observation`, `assistant_message`) 모두 `self._emit(...)` 직접 호출 — `_capture_line` 우회. **capture 됐어도 빈 list 만 등록.**

→ web 에서 parallel delegate 실행 시:
- worker thread 의 `start_capture()` 정상 동작 (base 기본).
- worker thread 의 SSE 이벤트는 직접 emit (캡처 안 됨).
- `render_replay_captured()` 의 console.print 도 web 에서 *no-op* (text capture 가 비어 있어서).

**진단:** auditor 주장 정확. 단 카테고리는 **DEAD 가 아님** — capture API 자체는 CLI 경로에서 *살아 있음*. web 측의 *override 누락* 이라 **CONSIST/INCOMPLETE** 가 더 정확.

**Q3 일반화:** 같은 "base 정의 + minimal override 있음 + web override 없음" 패턴 추가 후보:
- `rg -n "def " agent_cli/render/web.py | wc -l` vs minimal.py 비교 — Round 3 권장. F-014 가 *유일한 누락 override* 인지, 다른 abstract method 도 web 에서 누락됐는지 확인 필요.

**Q4 우선순위:**
- 실 사용자 영향: web 에서 parallel delegate 실행 시 worker progress 가 *SSE 로는 흘러옴* (worker thread 가 web._emit 직접 호출) — 단 *Live status panel/replay 표시 영향*. 사용자 측에서 "parallel delegate 가 동작하긴 하지만 진행률이 UI에 누락" 패턴.
- web 의 parallel delegate 사용 빈도: web 은 비교적 새 surface (`render/web.py` 601 LOC). parallel delegate 사용은 advanced feature.

**Critic 결론:** **CONFIRMED P1.** 단 카테고리 재정의: "DEAD/ROLE → CONSIST (web override 누락)". 권장 옵션 (a)/(b)/(c) 중 *(c) 명시 limitation 문서화* 가 비용/효용 비 최선 (web 의 parallel UI 디자인은 별도 RFC 가 필요).

**Round 3 실측:** auditor 가 본인 권고대로 web 에서 parallel delegate 실행 후 SSE 이벤트 trace 확인 필요. 단, dev server 실행은 critic/auditor scope 외이므로 사용자 결정 영역.

---

### F-015 — symbols.py 785 LOC [CONFIRMED P2]

**Q1 라인 검증:** auditor 표 (Python 58 / JS 90 / CPP 276 / MD 46 / Dispatcher) 본인 wc 미실시 (시간 제약). 라인 범위 신뢰.

**Q2 인과/우회:** dispatch 함수 (`_parse` 601-640) 의 if-cascade 는 새 언어 추가 시 *기계적 수정*. 그러나 *현재 dead* 는 아님 — 5 언어 모두 사용 가능.

**Q3 일반화:** 같은 패턴 — `tools/context.py` (574 LOC) 도 hot-spot. Round 1 F-010 (`_handle_text_path` 460 LOC) 와 같은 단일 책임 위배 카테고리.

**Q4 우선순위:** Round 1/2 모두 P2 — 변경 트리거 (새 언어 / tree-sitter 마이그레이션) 발생 시 자연 split. 본인 동의.

**보정 사항 없음.**

---

### F-016 — loc 포맷 가정 [DROP 권고]

**Q1 라인 검증:** context.py:146-153, 477-502 본인 미확인 (시간 제약, 우선순위 낮음). auditor 라인 인용 신뢰.

**Q2 인과/우회 (결정적):**
- session_id 콜론 미포함 (auditor 본인 확인: `time.strftime + uuid` 기반).
- Windows 미지원 docstring 명시.
- 실측 위험 0 — *가설적 robust 화*.

**Q4 우선순위:** auditor 본인이 "drop 가능" 명시. **본 critic 도 DROP 권고.** Round 3 에서 다루지 않음.

---

## 누락 영역 (Round 2 에서 다루지 않은 surface)

본 라운드 critic 본인 grep 결과:

1. **`agent_cli/recovery/` 디렉터리 (701 LOC, 7 파일)** — auditor Round 2 D-3 의 5번 항목으로 이미 인지. **Round 3 우선순위.** 본인 wc 확인:
   ```
   __init__.py 56 / common_recovery.py 62 / detectors.py 220 / 
   intervention.py 31 / observability.py 119 / primitives.py 109 / wf_recovery.py 104
   ```
2. **`agent_cli/web/server.py` (~488 LOC)** — auditor D-3 의 3번 항목.
3. **`agent_cli/providers/` 5 파일** — anthropic, openai_compat, ollama, base, compat, http. F-006 본 라운드 P2 downgrade 후 일관성 매핑 더 필요한가? Round 3 보류 가능.
4. **`tests/` 디렉터리의 test-only keep-alive 패턴 추가 후보** (F-001, F-011, F-012 본 라운드 발견). 본인 grep 권장: `rg -L --files-with-matches "from agent_cli\." tests/ | xargs grep -l "production-unused-imports"`.

---

## Round 3 지시 (auditor 에게)

### 필수 조치 (Round 3 audit_report_v3.md 에 반영)

1. **F-011 카테고리 재정의** — "DEAD" → "unwired infrastructure (Phase 2 잔재)". 본인 권장 선택 명시 (사용자에게 떠넘기지 말고 critic 의 "(c) 명시 문서화" 권고에 대해 입장 표명).
2. **F-012 P0 upgrade 검토** — 본 critic 의 P0 후보 분류에 대해 동의/거부 입장 명시. 거부 시 사유.
3. **F-014 카테고리 재정의** — DEAD/ROLE → CONSIST (web override 누락).
4. **F-016 DROP** 확정.

### Round 3 깊이 분석 영역 (critic 지정)

5. **`agent_cli/recovery/` 7 파일 책임 분담** — `observability.py` (119 LOC) 의 `TurnRecorder` + `parse_stage`, `detectors.py` (220 LOC) 의 detector 함수 갯수/중복, `primitives.py` (109 LOC) vs `intervention.py` (31 LOC) 의 책임 경계. recovery 시스템이 *active wired* 인지 *Phase 잔재* 인지 검증.
6. **`web/server.py`** — `WebRenderer` 와 `handle_slash_command`, `WebDispatchOutput` (web/server.py:95) 의 역할 매핑. `app.py` (FastAPI/Flask?) 와의 의존 구조.
7. **추가 test-only keep-alive 후보 sweep** — `tests/test_*.py` 가 import 하는 production 심볼 중 production 미사용인 것들 식별. F-001, F-011, F-012 외 추가 후보 있는지.
8. **`render/web.py` 의 base override 매핑** — F-014 일반화. base.py 의 모든 abstract/concrete 메서드 중 web override 가 누락된 항목 전수 매핑.

### Round 4 후보 (Round 3 결과 따라)

- `prompts/system_prompt.py` 전체 (658 LOC) 의 builder 함수들 + F-013 외 중복.
- `agents/builtin/` + `skills/builtin/` 의 자료 일관성.
- `tools/` 나머지 (memory.py, todo.py, bash.py 등) 의 dead/dup 검색.

---

## Critic Round 2 Verdict (interim)

**현재 audit_report_v2.md 는 PASS 후보로 진행 가능 — 단 위 4가지 정정사항 (특히 F-011 권장 선택 명시, F-012 우선순위 재검토) 을 Round 3 에서 반영해야 함.**

Round 1 의 false positive 가 정확히 철회됐고, 신규 finding (F-011~F-014) 모두 본인 검증으로 valid. P0 후보 3건 (F-001, F-004, F-011) + 잠재 P0 후보 1건 (F-012) 의 영향 LOC 합계 ≈ 450 LOC. 이 정도면 사용자에게 의미 있는 정리 효과 — Round 3~5 에서 우선순위/실행 순서/PR 분리 전략까지 정리 가능.
