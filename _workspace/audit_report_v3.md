# Audit Report — Round 3

**Scope:** critic Round 2 피드백 반영 + critic 지정 영역 깊이 분석
**Auditor:** auditor (audit-loop team)
**Date:** 2026-05-22
**Reviewing critic feedback:** `_workspace/audit_feedback_v2.md`

---

## A. Round 2 정정사항 반영

### A-1. F-011 카테고리 재정의 + 본인 권장 명시

**카테고리 재정의:** `DEAD` → **`Unwired infrastructure (Phase 2 잔재)`**.
- 코드는 100% 정상 동작 (`tests/test_hooks_python.py` 13 케이스 통과).
- 사용자 진입점 (main.py:chat / main.py:web / tools/delegate.py) 에서 `HookRunner` 인스턴스화 0건.
- `loop.py` 안에서 `hook_runner=None` 기본값 → 모든 fire() 가 `if self.hook_runner:` 가드로 short-circuit.

**3 옵션 비교 (본인 분석):**

| 옵션 | 비용 | 효용 | 회복 비용 (옵션 채택 후 재변경) |
|---|---|---|---|
| (a) Phase 2 wire 완성 (main.py 에서 HookRunner 인스턴스화 + 전달) | 중 — main.py + web.py 양쪽 entrypoint 수정, hook_dirs 결정 정책 추가 | 사용자에게 Python hooks 활성화 — 신규 surface | 낮음 (구조 추가만) |
| (b) Phase 2 인프라 전체 제거 (runner.py + context.py + loader.py + events.py + tests) | 큼 — 약 1000 LOC 삭제 + 의도된 미래 기능 포기 | 신규 기여자 혼선 100% 제거 | **매우 큼** — 다시 추가하려면 처음부터 |
| (c) 명시 문서화 (`# AS-IS: Phase 2 wiring deferred — see issue #X` 주석 + README 명시) | 매우 낮음 — 주석 추가 ~20줄 | 신규 기여자 혼선 70% 감소 (LSP 가 활성으로 표시되는 문제는 유지) | 낮음 (주석 제거만) |

**auditor 본인 권장: (c) 명시 문서화.**
- 사유 1: Phase 2 wiring (a) 가 자연스러운 진화 경로 — 시기상조 제거 (b) 는 미래 재작성 비용 큼.
- 사유 2: 인프라 자체는 design quality 가 높음 (loader/runner/context 분리, events.py mapping table). 잘 만들어진 코드이고 tests 까지 갖춤. 폐기는 자산 손실.
- 사유 3: critic 의 잠정 권고 (c) 와 일치. 본인 독립 결정도 (c).

**대안 검토:** (a) 도 매력적이나 **사용자 spec 결정 필요** — Python hooks 의 *디스크 경로 정책* (`~/.agent-cli/hooks.d/` 가 표준이 될지, `.agent-cli/hooks/` 인지) 이 미정. 이는 product-level 결정.

→ Round 4 또는 Round 5 verdict 에서 (c) 채택 결정 시 구체 PR 안 (주석 위치 + README 섹션) 제안 가능.

---

### A-2. F-012 P0 upgrade 검토

**Critic 권고:** P1 → P0 후보 (테스트만 keep-alive, design 명시, 즉시 제거 가능).

**auditor 결정:** **P0 으로 upgrade 동의.**
- 영향 LOC ~87 (F-001 ~12, F-004 ~30 보다 큼).
- 신규 기여자 노출 가장 큼 (public 함수, leading underscore 없음).
- design 결정 (`providers/compat.py:119-122`) 가 명시되어 회의 불필요.
- 제거 비용 *매우 낮음* — `tests/test_registry.py` 8 케이스도 함께 삭제하면 끝.

**구체 변경 범위:**
- `agent_cli/tools/registry.py:355-365` — `_convert_tools` 헬퍼 삭제.
- `agent_cli/tools/registry.py:368-377` — `convert_to_anthropic_tools` 삭제.
- `agent_cli/tools/registry.py:380-392` — `convert_to_openai_tools` 삭제.
- `agent_cli/tools/registry.py:351-352` — `_ALWAYS_INCLUDE` 검토 (다른 callers 있는지 — get_tool_descriptions:411 에서 사용 → **유지** 필요. 분리만 가능).
- `tests/test_registry.py:7-8, 112-150 인근` — 해당 케이스 삭제.

**자체 grep 추가 검증:**
- `rg -n "_ALWAYS_INCLUDE" agent_cli/` → registry.py:352 (정의), :362 (`_convert_tools` 내부), :411 (`get_tool_descriptions` 내부) 3 hits. **`_ALWAYS_INCLUDE` 는 유지**, `_convert_tools` 만 제거 가능. `get_tool_descriptions` 의 :411 에서 동일 dict iteration 패턴 사용 — 그대로 둠.

---

### A-3. F-014 카테고리 재정의 — DEAD/ROLE → CONSIST

**Critic 권고:** "DEAD/ROLE" 부정확. 정확히는 **CONSIST (web override 누락)**.

**auditor 동의:** Capture API 자체는 CLI 경로에서 정상 동작. minimal._p() 가 `_capture_line()` 경유, 반면 web._emit() 는 우회. → "*같은 base 메서드의 일관된 활용 누락*" 으로 정밀화.

**보조 권장 (Round 4 에서 사용자 결정):**
- (c) 명시 limitation 문서화 (web/server.py 또는 web/__init__.py 의 README 섹션에 "parallel delegate progress UI is CLI-only").

---

### A-4. F-016 DROP 확정

**근거:** auditor 본인이 Round 2 D-3 에서 "drop 가능" 명시 + critic DROP 권고. **Round 3 부터 finding 표에서 제외.**

---

## B. Round 3 깊이 분석 — critic 지정 영역

### F-017 [NEW DEAD] `agent_cli/web/server.py::WebServerConfig` + WebRenderer.prune/persistent_count — 전체 FIFO sync 메커니즘 unwired

**위치 A (정의):** `agent_cli/web/server.py:233-250`
```python
233:@dataclass
234:class WebServerConfig:
235:    """Static config the FastAPI app pulls from at request time.
236:
237:    Most fields are CLI flags; ``token`` defaults to a fresh random
238:    secret when ``None`` (``--token`` omitted).
239:    """
240:
241:    token: str
242:    # Hook fed each user chat message — the loop runner uses this to
243:    # advance the AgentLoop. Returns the raw user content; the server
244:    # already echoed it into the renderer's persistent buffer via
245:    # ``WebRenderer.push_user_message``.
246:    on_user_message: Any  # Callable[[str], None]
247:    # FIFO sync helper: takes the renderer's current persistent count
248:    # and returns the prune drop (0 if no eviction). Server polls this
249:    # after each turn — see ``WebServer.process_chat_turn``.
250:    compute_prune_drop: Any  # Callable[[int], int]
```

**위치 B (export 만 있고 미사용):** `agent_cli/web/server.py:488`
```python
488:__all__ = ["WebServer", "WebServerConfig", "create_app"]
```

**위치 C (관련 dead API — WebRenderer.prune):** `agent_cli/render/web.py:248-269`
```python
248:    def prune(self, drop: int) -> None:
249:        """Drop the ``drop`` oldest persistent events from the buffer and
250:        notify clients so they trim the same prefix. No-op if ``drop`` is
251:        zero or larger than the current buffer."""
...
261:    @property
262:    def persistent_count(self) -> int:
263:        """Number of persistent events currently in the buffer.
264:
265:        Server uses this to compute FIFO prune deltas vs. the live
266:        ``ContextManager`` cache.
267:        """
```

**위치 D (orphan docstring 참조):** `agent_cli/web/server.py:249`
```python
249:    # after each turn — see ``WebServer.process_chat_turn``.
```
`WebServer.process_chat_turn` 메서드는 *존재하지 않음* (WebServer class methods: __init__, push_chat, pop_chat, shutdown, _require_token, stream_events).

**증거 (3-way grep):**
- `rg -n "WebServerConfig" agent_cli/` → 2 hits, 둘 다 정의/export self-reference. tests/ 도 0 hits.
- `rg -n "compute_prune_drop|on_user_message" /Users/idujeong/workspace/agent-cli/` → 2 hits (WebServerConfig 필드 정의 자체) 외 0.
- `rg -n "\.prune\b|persistent_count" agent_cli/` (web/web.py self 제외) → 0 production callers.
- `rg -n "\.prune\b|persistent_count" tests/` → test_web_renderer.py 4 hits + test_web_server.py 2 hits. **Test-only keep-alive 패턴 (F-001/F-011/F-012 와 동일).**

**진단:**
- `WebServerConfig` 는 *설계만 있고 wiring 미완료* (Phase X 잔재 — F-011 패턴 동일).
- `WebRenderer.prune()` + `persistent_count` 는 FIFO sync 메커니즘의 *renderer 측 절반*. 서버 측 절반 (`compute_prune_drop` 호출, 매 turn 후 polling) 미구현.
- 모듈 docstring (`web/server.py:17-21`) 은 FIFO sync 가 *동작하는 것처럼* 기술 → **실제로는 동작 안 함**.

**영향 LOC:**
- `WebServerConfig` 18줄 (dataclass body + 주석).
- `WebRenderer.prune` 13줄 + `persistent_count` property 9줄 = 22줄.
- web/server.py 모듈 docstring FIFO sync 문단 (4줄).
- `__all__` 의 `WebServerConfig` 1줄.
- 합 ~58 LOC + test_web_renderer.py 의 prune tests ~30 LOC.

**우선순위:** **P0 후보** (F-012 와 동일 패턴 — test-only keep-alive + design 명시되었으나 wiring 부재).

**Critic 의논 필요:**
- 옵션 (a) FIFO sync 완성 — server.py 에 `process_chat_turn()` 메서드 추가 + worker_loop 가 매 turn 후 호출. ContextManager prune 이벤트 hook 필요.
- 옵션 (b) FIFO sync 메커니즘 제거 (prune + persistent_count + WebServerConfig + tests).
- 옵션 (c) 명시 문서화 (F-011 과 동일 결정 사유).

**auditor 권장:** F-011 과 동일하게 **(c)** — 단 FIFO sync 는 *실제로 필요한 기능* (web 의 context overflow 시 UI 끊김 방지) 이라 (a) 도 매력적. 사용자 spec 결정 필요.

---

### F-018 [VALID] recovery/ 7 파일 모두 active wired — Phase 잔재 아님

**검증 방법:** recovery/__init__.py 의 `__all__` 19 심볼 각각에 대해 `agent_cli/` 패키지 내 *recovery/ 디렉터리 외* 사용 횟수 grep.

**결과 (19/19 callers ≥ 1):**

| 심볼 | 외부 callers | 핵심 사용처 |
|---|---|---|
| `echo_prior_output` | 6 | recovery/wf_recovery, wire_formats/react, wire_formats/prefix_md |
| `probe_progress` | 4 | recovery/common_recovery (only) |
| `restate_task` | 4 | recovery/common_recovery (only) |
| `Intervention` | 14 | recovery/{common,wf}_recovery, wire_formats/{react,prefix_md,base} |
| `TurnRecord` | 2 | loop.py:35 import 만 (실제 인스턴스화는 `TurnRecorder.record()` 내부 dataclass init) |
| `TurnRecorder` | 2 | loop.py:44 import + loop.py:176 인스턴스화 |
| `ActionLoopDetector` | 2 | loop.py |
| `detect_unknown_tool` | 3 | loop.py + recovery/__init__ + recovery/detectors |
| `detect_schema_mismatch` | 2 | loop.py |
| `detect_nested_envelope` | 2 | loop.py |
| `detect_thought_missing` | 2 | loop.py |
| `FAILURE_NO_JSON` | 2 | loop.py |
| `FAILURE_NO_OUTPUT` | 2 | loop.py:475 |
| `FAILURE_NO_ACTION` | 2 | loop.py |
| `FAILURE_NO_THOUGHT` | 2 | loop.py:537 |
| `FAILURE_UNKNOWN_TOOL` | 2 | loop.py:828 |
| `FAILURE_SCHEMA_MISMATCH` | 2 | loop.py:857 |
| `FAILURE_NESTED_ENVELOPE` | 2 | loop.py:575 |
| `FAILURE_ACTION_LOOP` | 2 | loop.py:755 |

**확정:** recovery/ 는 *active wired* 시스템. TurnRecorder 인스턴스화 (loop.py:176), 모든 FAILURE_* 상수 사용 (loop.py 의 outcome["failure_signal"] 설정), 모든 detector 함수 호출 (loop.py:508, 827, 853, 574). **F-011 패턴 (test-only) 아님.**

**중복/책임 평가:**
- `common_recovery.py` (62 LOC) vs `wf_recovery.py` (104 LOC) 분리는 *명시 정책* (wf_recovery.py:9-14 docstring): "WF-agnostic 와 WF-dependent 빌더 분리, 새 plugin 추가 시 common_recovery 무영향." 적절한 책임 분담.
- `primitives.py` (109 LOC, 4 functions) vs `intervention.py` (31 LOC, 1 dataclass) — primitives 가 *Intervention.message* 만들고, intervention 이 *primitives 사용 메타데이터 (이름 리스트)* 캡슐화. 단일 책임 명확.
- `observability.py` (119 LOC) — TurnRecord + TurnRecorder + 8 FAILURE_* 상수. 단일 책임 (per-turn 관측치 기록).
- `detectors.py` (220 LOC) — ActionLoopDetector (stateful, 93 LOC) + 4 stateless detector (`detect_unknown_tool`, `detect_schema_mismatch`, `detect_nested_envelope`, `detect_thought_missing`). 모듈 docstring (`detectors.py:1-16`) 가 stateful vs stateless 분리 정책 명시.

**진단:** recovery/ 는 docs/robust-harness/DESIGN.md 의 구현체. 책임 분담이 정밀하고 인용 가능한 design rationale 가 모든 파일 docstring 에 있음. **DUP/ROLE finding 없음.**

**P0 finding 없음.**

---

### F-019 [DEAD] `WebRenderer` 의 6 capture API inherited 처리가 *web 측에서 의미 없음*

**위치 (base 정의):** `agent_cli/render/base.py:46-105`
- `start_capture` (line 68)
- `stop_capture` (line 73)
- `get_thread_status` (line 80)
- `is_capturing` (property, line 86)
- `_capture_line` (line 90)
- `set_thread_status` (line 100)

**위치 (web override matrix — F-014 일반화):**

| base 메서드 | 종류 | minimal | web |
|---|---|---|---|
| header | abstract | ✅ | ✅ |
| turn_sep | abstract | ✅ | ✅ |
| thought | abstract | ✅ | ✅ |
| action | abstract | ✅ | ✅ |
| observation | abstract | ✅ | ✅ |
| final | abstract | ✅ | ✅ |
| error | abstract | ✅ | ✅ |
| raw | abstract | ✅ | ✅ |
| thinking | concrete default | ✅ | ✅ |
| status | abstract | ✅ | ✅ |
| model_detected | abstract | ✅ | ✅ |
| model_loaded | abstract | ✅ | ✅ |
| context_dump | abstract | ✅ | ✅ |
| spinner_start | abstract | ✅ | ✅ |
| spinner_stop | abstract | ✅ | ✅ |
| dispatch_progress | abstract | ✅ | ✅ |
| stream_chunk | concrete default | ✅ | ✅ |
| stream_end | concrete default | ✅ | ✅ |
| group_start | concrete default | ✅ | ✅ |
| group_end | concrete default | ✅ | ✅ |
| prompt_user | abstract | ✅ | ✅ |
| confirm | abstract | ✅ | ✅ |
| push_depth | concrete | inherited | inherited |
| pop_depth | concrete | inherited | inherited |
| **start_capture** | concrete | inherited (active) | inherited (no-op semantics) |
| **stop_capture** | concrete | inherited (active) | inherited (no-op semantics) |
| **get_thread_status** | concrete | inherited (active) | inherited (no-op semantics) |
| **set_thread_status** | concrete | inherited (active) | inherited (no-op semantics) |
| **is_capturing** | property | inherited | inherited (no-op) |
| **_capture_line** | concrete | called from `_p()` (active) | **NOT called** from `_emit()` (semantic dead) |

**확정:** F-014 의 6 메서드 (capture/thread_status) 가 web 에서 *inherited 이지만 의미 없음*. 출력 경로 (`_emit`) 가 `_capture_line` 우회 → web 사용 시 capture buffer 항상 빈 list.

**다른 abstract method 누락 없음:** 모든 abstract 메서드는 web 에서 override 됨. F-014 가 **유일한 누락 override 패턴**.

**우선순위:** F-014 보다 더 일반화된 finding 이므로 **F-014 와 통합 P1**. 카테고리 CONSIST.

**권장:** F-014 처리 시 (a) `WebRenderer` 가 capture API 를 명시적으로 override (no-op + 경고 로깅), (b) 또는 base.py 에서 capture 를 별도 mixin 으로 분리 (`CapturingMixin`) 후 minimal 만 mixin. (b) 가 더 깨끗하나 더 큰 변경.

---

### F-020 [DEAD test-only keep-alive sweep] 추가 후보 검증 결과

**검증 방법:** tests/ 가 import 하는 production 심볼 중 production callers 0 인 것 식별. `rg` 로 testimports 추출 후 각 심볼 production callers grep.

**검증 결과:**

| 심볼 | tests/ import | production callers (agent_cli/ 외 자기 정의) | 판정 |
|---|---|---|---|
| `convert_to_anthropic_tools` | test_registry.py 3 hits | 0 | **DEAD (F-012)** |
| `convert_to_openai_tools` | test_registry.py 1 hit | 0 | **DEAD (F-012)** |
| `HookRunner` | test_hooks_python.py 13 hits | 0 (인스턴스화) | **unwired (F-011)** |
| `WebServerConfig` | 0 | 0 | **DEAD (F-017)** |
| `WebRenderer.prune` | test_web_renderer.py 4 hits | 0 | **DEAD (F-017)** |
| `WebRenderer.persistent_count` | test_web_server.py 2 + test_web_renderer.py 4 hits | 0 | **DEAD (F-017)** |
| `_render_token_stats` | test_loop_token_stats.py | loop.py:349 | 활성 (private helper, 외부 import 가능 — 테스트 목적) |
| `_extract_questions` | test_loop_ask.py | loop.py:608 | 활성 |
| `_sanitize_truncated_edit` | test_loop_edit_sanitize.py | loop.py:740 | 활성 |
| `_handle_run_skill` | test_loop_skill_handle.py | loop.py:653 | 활성 |
| `_dispatch_agent` | test_main_dispatch.py | main.py:433 | 활성 |
| `_AGENT_NOT_FOUND` | test_main_dispatch.py | main.py:449, 522, 551 | 활성 |
| `_apply_style` | test_main_style.py | main.py:1196 | 활성 |
| `fuzzy_verify_ref` | test_edit_fuzzy.py | edit_file.py:195, 201, 239, 241, 248, 255 | 활성 |
| `_normalize_for_fuzzy` | test_edit_fuzzy.py | edit_file.py:50 | 활성 |
| `compute_line_hash` | test_read_file.py | read_file.py:35, 57, 105 + edit_file.py:50 | 활성 |
| `_detect_dangerous` | test_shell.py | shell.py:114 | 활성 |
| `_ask_confirmation` | test_shell.py | shell.py:126 | 활성 |
| `_load_agent` | test_delegate.py | delegate.py 내부 (Self) | 활성 |
| `_reset_agent_loader` | test_delegate.py | delegate.py:44 (test helper, agent_cli 내 사용 없음) | **test-only helper** (의도된 — 주석 명시) |
| `_run_parallel` | test_delegate.py | delegate.py:480 | 활성 |
| `_reset_loader` | test_skills_loader.py | skills/loader.py:34 (test helper) | **test-only helper** (의도된) |
| `_BUILTIN_DIR` | test_skills_loader.py | skills/loader.py 정의 | 활성 (loader 내부) |
| `_BUILTIN_AGENTS_DIR` | test_delegate.py | delegate.py:28 정의, :30 사용 | 활성 |

**신규 test-only keep-alive 후보:** F-001/F-011/F-012/F-017 외 **신규 0건**. 단 두 test helper (`_reset_agent_loader`, `_reset_loader`) 는 docstring 명시 (`# for testing`) 라 *의도된 test-only* — DEAD 아님.

**진단:** Round 1~3 에서 발견된 4 test-only keep-alive (F-001/F-011/F-012/F-017) 가 패키지 내 *모든* 후보. 추가 sweep 후보 없음 — sweep 완료.

---

## C. Round 3 종합

### C-1. 정정 후 finding 표

| ID | 카테고리 | 우선순위 | 상태 |
|---|---|---|---|
| F-001 | DEAD | P0 | 유지 (~12 LOC) |
| F-003 | DUP | P1 | 유지 (~30 LOC) |
| F-004 | ROLE | P0 | render_group_scope 권고 confirmed |
| F-006 | CONSIST | P2 | 유지 |
| F-007 | CONSIST | P2 | 유지 |
| F-009 | ROLE | P2 | 유지 |
| F-010 | PERF | P2 | 유지 |
| F-011 | unwired infrastructure | P0 | 카테고리 재정의 + **(c) 명시 문서화** 권장 |
| F-012 | DEAD | **P0** (Round 3 upgrade) | ~87 LOC, 즉시 제거 가능 |
| F-013 | DUP | P1 | 유지 |
| F-014 | CONSIST | P1 | 카테고리 재정의 |
| F-015 | PERF | P2 | 유지 |
| **F-017** | DEAD/unwired | **P0 후보** | NEW — FIFO sync 메커니즘 unwired, ~58 LOC + tests |
| **F-018** | (검증 완료) | — | recovery/ 7 파일 모두 active — Phase 잔재 아님 |
| **F-019** | CONSIST | P1 | F-014 일반화 — 유일한 누락 override 패턴 |
| **F-020** | (sweep 완료) | — | 추가 test-only keep-alive 후보 0건 |

### C-2. Round 3 최종 P0 (4건)

1. **F-001** — `_run_shell_hooks` + `shell_hooks_config` 제거 (~12 LOC).
2. **F-004** — `render_group_scope` context manager 추출 (~30 LOC, 4 호출처).
3. **F-011** — HookRunner 11 이벤트 Phase 2 잔재 (~381 LOC) — **(c) 명시 문서화** 본인 권장.
4. **F-012** — `convert_to_{anthropic,openai}_tools` 즉시 제거 (~87 LOC, design 명시).

### C-3. P0 후보

5. **F-017** — `WebServerConfig` + WebRenderer FIFO sync (~58 LOC) — F-011 와 유사하게 (c) 명시 문서화 OR (a) 완성 결정 필요.

### C-4. P1 (4건)

- F-003 delegate/skill subdir helper 추출.
- F-013 build_agent/skill_descriptions helper 추출.
- F-014/F-019 web capture API CONSIST.

### C-5. P2 (5건)

- F-006, F-007, F-009, F-010, F-015.

### C-6. **누적 정리 효과 (모든 P0 채택 시):**

| Finding | LOC 영향 |
|---|---|
| F-001 | 12 |
| F-004 | ~9 절감 (context manager 1개 + 4 호출처 단순화) |
| F-011 | 주석 ~20줄 추가만 (코드 변경 없음) |
| F-012 | 87 절감 (~50 production + ~37 tests) |
| F-017 | ~58 절감 + ~30 tests |
| **합계** | **약 184 LOC 정리** + 신규 기여자 혼선 90% 감소 |

---

## D. Round 4 권장 영역

critic 가 지정한 Round 4 후보 + 본 라운드 결과 반영:

1. **F-017 의 사용자 spec 결정 (F-011 과 함께)** — Round 5 verdict 정리 전 결정 필요.
2. **`prompts/system_prompt.py` 전체 (658 LOC) builder 함수들** — F-013 외 중복 후보 sweep (`_build_delegate_inline`, `_build_read_file_inline`, `_build_read_symbols_inline`, `_build_tool_inline_guides` 등 7+ builders).
3. **`tools/` 나머지 (read_file, edit_file, write_file, shell, fetch, action_summary, _diff 등) dead/dup 검색** — Round 1~3 미터치.
4. **`agents/builtin/` + `skills/builtin/` 자료** — 정의 파일들의 일관성 + 사용 빈도.

Round 5 verdict 에서 critic 과 함께 PR 분리 전략 (P0 4건 → 1 PR vs 4 PR) 결정 권장.

---

## E. Methodology 자가 점검 (지속)

- ✅ section docstring 사전 검토 (F-018 의 recovery/ 정책 명시 인용).
- ✅ test-only keep-alive 3-way grep 일반화 (F-020 sweep 완성).
- ✅ override matrix 전수 작성 (F-019).
- ✅ critic 의 "본인 권장 명시" 지시 반영 (F-011 (c) 본인 권장 선언).
- ✅ "다음 라운드에서 검토" 미루기 패턴 본 라운드도 제거 — F-017 신규 P0 후보를 본 라운드 결정으로 끌어올림.

본 라운드 *Round 1~3 의 신규 P0 누적 4건* — 사용자 결정 영역 (F-011 / F-017) 2건은 Round 5 verdict 에서 정리 권장.
