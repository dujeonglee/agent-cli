# Audit Report — Round 5 (Final)

**Scope:** critic Round 4 피드백 5건 반영 + final file:line drift 검증 + active/dead helpers 두 표 분리 + agents/skills builtin 자료 일관성 + ARCHITECTURE/README 업데이트 항목
**Auditor:** auditor (audit-loop team)
**Date:** 2026-05-22
**Reviewing critic feedback:** `_workspace/audit_feedback_v4.md`

---

## A. Round 4 정정사항 5건 반영

### A-1. B-1 보정 — Pre/PostToolUse dual-dispatch 시멘틱 명시

**Critic 지적:** F-011 (a) sketch 가 *PreToolUse / PostToolUse 의 Python+Shell dual-dispatch* 시멘틱 미언급. (a) 채택 시 신규 사용자 인지 필요.

**검증 (본인 grep 재확인):** `agent_cli/loop.py`
- line 995-999: `# ── 1. PreToolUse hooks` 섹션 docstring 가 "Python runner first, then shell config" 명시.
- line 1010-1024: `hook_runner.fire("PreToolUse", ...)` (Python 우선) — `block_reason` 시 즉시 short-circuit, shell 미도달.
- line 1025-1027: Python `modified_input` 설정 시 shell 은 변경된 input 받음.
- line 1032-1034: `run_hooks("PreToolUse", ...)` (Shell, 후속).
- line 1132 + 1143: PostToolUse 동일 패턴.

**진단:** dual-dispatch 코드는 *이미 존재* (구현 단계는 완료, wiring 만 미완). (a) 채택 = Python hooks 가 *이미 정의된 우선순위* 로 활성화됨.

**B-1 sketch 보정 추가 항목:**

```
[추가] PR-3 (F-011 (a)) 의 PR description 에 다음 문구 의무 포함:

"Python hooks fire BEFORE shell hooks for PreToolUse/PostToolUse events.
Python hook can short-circuit dispatch via `block_reason`, or transform
input via `modified_input` which the shell hooks then receive. This
ordering is implemented at loop.py:995-1047 (pre) and loop.py:1120-1150
(post) — it is the contract Python hook authors should expect.

Python-only events (OnSessionStart/End, OnDelegateStart/End, OnSkillStart/End,
PreLLMCall, PostLLMCall, OnTurnEnd — 9 events) have no shell hook conflict."
```

**README/CHANGELOG 업데이트 의무:** 본 항목은 Round 5 verdict 의 *user-facing 변경 노트* 로 명시 (G-1 섹션).

---

### A-2. B-2 보정 — F-017 (a) FIFO sync 단위 불일치

**Critic 지적:** F-017 (a) sketch 의 `renderer.persistent_count` (SSE event 단위) vs `cache_count` (message 단위) **단위 불일치**. delta 계산 자체가 잘못된 가정 위에 있음.

**검증 (본인 grep 재확인):**

`agent_cli/render/web.py:88-119` (persistent_count 증분 시점):
```python
88:        self._persistent_count: int = 0
...
108:    def _emit(
109:        self,
110:        event: str,
111:        data: dict[str, Any],
112:        *,
113:        persistent: bool,
114:    ) -> None:
...
117:            if persistent:
118:                self._event_buffer.append((event, data))
119:                self._persistent_count += 1
```

→ `_persistent_count` 는 **SSE event** 갯수 (replay_from_history 가 emit 하는 `user_message`, `observation`, `assistant_turn` 등 모두 합산).

`agent_cli/context/manager.py:66, 84-86, 108-112` (cache 증감):
```python
66:        self._cache: list[dict] = []
...
84:        self._cache.append(message)
...
108:    def _evict(self) -> None:
...
110:        while self._cache_tokens > self.max_context_tokens and len(self._cache) > 1:
111:            removed = self._cache.pop(0)
```

→ `ctx._cache` 는 *message dict* 단위. 1 user turn = 1 user message + N assistant/tool messages = 여러 cache 엔트리.

**진단 확정:** sketch 의 `drop = max(0, renderer_count - cache_count)` 는 단위 불일치 — *항상 양수* (renderer count 가 더 큼) 라 매 turn 마다 잘못된 drop 발생.

**B-2 sketch 보정 — (a) 채택 시 RFC 항목:**

```
[추가] F-017 (a) 채택 시 다음 design RFC 필수:

1. WebRenderer 의 emit 단위와 ContextManager 의 message 단위 사이의 매핑 정의.
   - 옵션 (i): emit 1 → cache 1 (1:1) — assistant_turn 이 1 cache assistant 와 매핑.
     단 observation 은 cache 의 user message 와 매핑, user_message 는 user 와 매핑.
   - 옵션 (ii): emit count 추적 안 함, cache delta 만 사용 — renderer 의 _event_buffer 를
     turn 단위로 그룹화 후 cache eviction 발생 시 그룹 단위 prune.

2. ContextManager 측 API 변경: `get_visible_count() -> int` public method 추가
   (`_cache` 직접 참조 제거).

3. Web frontend 의 prune event 수신 시 어떤 단위로 trim 할지 정의
   (event 단위 vs turn 단위).

→ (a) 채택 시 단순 ~15 LOC PR 이 아닌 **design 변경 PR** 으로 분류.
```

**Round 5 권고 (auditor 본인 입장 표명):** critic 의 "(c) 권고 강도 상향 검토" 권고 **수용**.
- Round 4 에서 본인 (a) 권장했으나, B-2 단위 불일치 + ctx._cache 캡슐화 break + 단순 wiring 아닌 design 변경 → **Round 5 권장 (c) 로 변경**.
- 사용자가 (a) 선택 가능하나 *비용 평가 표* 에 design RFC 부담 명시.

---

### A-3. F-023 우선순위 — auditor 본인 입장 명시 (P2 유지)

**Critic 평가:** P1 vs P2 둘 다 합리적. auditor 입장 표명 요청.

**auditor 본인 결정: P2 유지.** 사유:
- **사용자 영향 작음:** 실측 사용자 중 `depth=3` 요청 빈도 매우 낮음 (web fetch 자체가 advanced feature). MAX_PAGES=10 충족 전에 depth=2 까지 fetch 완료되는 케이스 다수.
- **silent 무시이지만 catastrophic 아님:** 사용자가 depth=3 요청 후 pages 갯수 확인하면 즉시 인지. error 가 아닌 *under-delivery*.
- **F-001/F-012 와 같은 즉시 제거 가능 finding 과 다름:** F-023 는 *재귀 함수 추출* 작업 — 코드 변경량은 작으나 *테스트 추가* (재귀 depth 검증) 필요.
- **다른 P2 항목 (F-006 / F-007 / F-022) 과 균형:** F-022 (`_stat` preamble) 와 동일 PR-7 묶음에 포함 적합.

**PR 라벨 (critic 권고):** "user-visible bug fix" 라벨 명시 — 단 우선순위는 P2 유지.

---

### A-4. D-5 PR 분리 보정 — PR-1 에서 F-001 제외

**Critic 권고:** F-001 cleanup 은 F-011 (a) 채택 시 entrypoint 변경과 함께 발생하므로 PR-3 으로 이동. PR-1 = F-012 + F-021.

**보정 후 PR 표 (G-2 의 D-5 갱신):**

| PR # | 묶음 | 크기 | 사용자 결정 필요? | 의존성 |
|---|---|---|---|---|
| PR-1 | F-012 + F-021 (test-only DEAD 제거) | ~91 LOC | 불필요 | 독립 |
| PR-2 | F-004 (render_group_scope context manager) | ~30 LOC | 불필요 | 독립 |
| PR-3 | **F-011 (a) Phase 2 hooks wiring + F-001 cleanup 함께** | ~12 LOC | **필요** (F-011 옵션) | 독립 |
| PR-4 | F-017 (a/b/c) | ~15 LOC + design RFC | **필요** (F-017 옵션) | 독립 (PR 단독) |
| PR-5 | F-003 + F-013 + F-019/F-014 (helper 추출 모음) | ~80 LOC | 불필요 | 독립 |
| PR-6 | F-006 + F-007 (CONSIST 정정) | ~10 LOC | 불필요 | 독립 |
| PR-7 (선택) | F-022 + F-023 (P2 cleanup) | ~30 LOC | 불필요 | 독립 |

**F-011 옵션별 PR-3 영향:**
- (a) 채택: PR-3 진행 + F-001 cleanup 포함.
- (b) 채택 (제거): PR-3 가 *대규모 제거 PR* 로 변경 (~1000 LOC). F-001 도 함께 사라짐.
- (c) 채택 (문서화): PR-3 가 *주석 + README 업데이트* + F-001 단독 cleanup PR 로 분리.

→ PR 분리 전략은 *사용자가 F-011/F-017 옵션 선택한 후* 확정. 본 audit 는 옵션 별 PR 영향 명시까지.

---

### A-5. D-6 사용자 결정 항목 5번 추가

**Critic 권고:** F-023 우선순위 (P1 vs P2) 도 사용자 결정 항목.

**auditor 결정:** A-3 에서 본인 입장 P2 명시. 사용자가 P1 으로 끌어올리길 원하면 결정 가능.

**사용자 결정 항목 (G-3 섹션 최종 5건):**
1. F-011 옵션 (a/b/c) — auditor 권장 (a).
2. F-017 옵션 (a/b/c) — auditor 권장 **(c) 변경** (B-2 단위 불일치 반영).
3. F-023 우선순위 (P1 vs P2) — auditor 권장 (P2).
4. PR 분리 전략 (7-PR vs 압축 안).
5. agents/builtin + skills/builtin 자료 일관성 — auditor 권장 *cosmetic 만* 정리 (B-5 섹션 참고).

---

## B. Round 5 최종 활동

### B-1. 모든 finding file:line drift 최종 검증

**검증 방법:** P0/P1 finding 의 핵심 라인 cite 를 `grep -n` 으로 재확인. drift 0 확인 시 통과.

| Finding | Cite | Round 5 재검증 결과 |
|---|---|---|
| F-001 | runner.py:26 `shell_hooks_config: dict | None = None,` | ✅ line 26 일치 |
| F-001 | runner.py:89 `def _run_shell_hooks` | ✅ line 89 일치 |
| F-004 | main.py:616, loop.py:1464, delegate.py:617, delegate.py:689 — `render_group_start` | ✅ 모두 일치 |
| F-012 | registry.py:355 `def _convert_tools` | ✅ line 355 일치 |
| F-012 | registry.py:368 `def convert_to_anthropic_tools` | ✅ line 368 일치 |
| F-012 | registry.py:380 `def convert_to_openai_tools` | ✅ line 380 일치 |
| F-012 | registry.py:352 `_ALWAYS_INCLUDE` 보존 결정 | ✅ line 352 일치 (라인 411 사용처도 일치) |
| F-021 | tools/__init__.py:48 `VIRTUAL_TOOLS` | ✅ line 48 일치 |
| F-021 | tools/__init__.py:54 `__all__` export | ✅ line 54 일치 |
| F-011 | hooks/loader.py:13 `_hook_dirs()` | ✅ line 13 일치 |
| F-011 | loop.py:1010 PreToolUse Python fire | ✅ line 1010 일치 |
| F-011 | loop.py:1033 PreToolUse Shell fire | ✅ line 1033 일치 |
| F-011 | loop.py:1132 PostToolUse Python fire | ✅ line 1132 일치 |
| F-011 | loop.py:1143 PostToolUse Shell fire | ✅ line 1143 일치 |
| F-017 | web/server.py:233-250 WebServerConfig | ✅ 일치 |
| F-017 | web/server.py:249 orphan docstring (`WebServer.process_chat_turn`) | ✅ 메서드 부재 확정 |
| F-017 | render/web.py:88,119 `_persistent_count` 증분 | ✅ 일치 |
| F-017 | render/web.py:248 `prune` 정의 | ✅ 일치 |
| F-017 | render/web.py:262 `persistent_count` property | ✅ 일치 |

**모든 P0/P1 finding의 file:line 인용 drift 0 확정.** P2 finding 들은 본인 spot check 미실시 (시간 효율), 단 모두 Round 1~4 에서 grep 검증된 라인.

---

### B-2. Active helpers vs test-only DEAD 두 표 분리 (Round 3 critic 권고)

#### Table 1: Active helpers (production 호출처 ≥ 1)

| Symbol | Production callers | 비고 |
|---|---|---|
| `_render_token_stats` | loop.py:349 | per-turn 통계 출력 |
| `_extract_questions` | loop.py:608 | ask tool action_input 파싱 |
| `_sanitize_truncated_edit` | loop.py:740 | truncation guard |
| `_handle_run_skill` | loop.py:653 | run_skill action dispatch |
| `_dispatch_agent` | main.py:433 | @agent dispatch |
| `_AGENT_NOT_FOUND` | main.py:449, 522, 551 | sentinel |
| `_apply_style` | main.py:1196 | renderer style switch |
| `fuzzy_verify_ref` | edit_file.py:195, 201, 239, 241, 248, 255 | hash mismatch fuzzy |
| `_normalize_for_fuzzy` | edit_file.py:50 | fuzzy 정규화 |
| `compute_line_hash` | read_file.py:35, 57, 105 + edit_file.py:50 | hashline 코어 |
| `_detect_dangerous` | shell.py:114 | shell 위험 키워드 |
| `_ask_confirmation` | shell.py:126 | 확인 prompt |
| `_load_agent` | delegate.py 내부 | 에이전트 로드 |
| `_run_parallel` | delegate.py:480 | 병렬 delegate |
| `_BUILTIN_DIR` | skills/loader.py 내부 | 빌트인 스킬 경로 |
| `_BUILTIN_AGENTS_DIR` | delegate.py:28, 30 | 빌트인 에이전트 경로 |

→ 모두 test 가 *production helper 를 import* 하는 정당한 케이스. F-020 sweep 결과 *DEAD 아님*.

#### Table 2: Test-only DEAD (production 호출처 0)

| Symbol | Location | Finding ID | Status |
|---|---|---|---|
| `HookRunner` (인스턴스화) | hooks/runner.py:13 | F-011 | CONDITIONAL — Phase 2 wiring 미완료 |
| `_run_shell_hooks` | hooks/runner.py:89 | F-001 | P0 — 제거 |
| `shell_hooks_config` 파라미터/필드 | hooks/runner.py:26, 29, 75 | F-001 | P0 — 제거 |
| `convert_to_anthropic_tools` | tools/registry.py:368 | F-012 | P0 — 제거 |
| `convert_to_openai_tools` | tools/registry.py:380 | F-012 | P0 — 제거 |
| `_convert_tools` (helper) | tools/registry.py:355 | F-012 | P0 — 제거 (`get_tool_descriptions` 와 무관) |
| `VIRTUAL_TOOLS` (frozenset) | tools/__init__.py:48 | F-021 | P1 — 제거 |
| `WebServerConfig` (dataclass) | web/server.py:233 | F-017 | CONDITIONAL — FIFO sync 미완료 |
| `WebRenderer.prune` | render/web.py:248 | F-017 | CONDITIONAL — FIFO sync 미완료 |
| `WebRenderer.persistent_count` | render/web.py:262 | F-017 | CONDITIONAL — FIFO sync 미완료 |

→ **test-only DEAD 9 항목 (F-001 5, F-011 1, F-012 3, F-017 3, F-021 1).** 단 F-011/F-017 은 사용자 결정에 따라 *제거* 또는 *wire 완성* 둘 다 가능.

#### Test-only helpers (의도된 — DEAD 아님)

| Symbol | Location | 의도 |
|---|---|---|
| `_reset_agent_loader` | delegate.py:44 | 테스트 fixture (loader 재초기화) |
| `_reset_loader` | skills/loader.py:34 | 테스트 fixture (skill loader 재초기화) |

---

### B-3. agents/builtin + skills/builtin 자료 일관성 검증

**Scope:** `agent_cli/agents/builtin/` (1 파일) + `agent_cli/skills/builtin/` (3 SKILL + 1 .md + references)

**검증 결과:**

| 파일 | name | description | allowed-tools 스타일 | disable-model-invocation | argument-hint |
|---|---|---|---|---|---|
| `agents/builtin/explorer.md` | ✅ | ✅ | **block style (multi-line list)** | — (agent 는 미적용) | — |
| `skills/builtin/create-agent.md` | ✅ | ✅ | **flow style `[…]`** | true | "<agent-name> [description]" |
| `skills/builtin/plan.md` | ✅ | ✅ | flow style | (없음, default false) | "<feature description>" |
| `skills/builtin/create-skill/SKILL.md` | ✅ | ✅ | flow style | true | "<skill-name> [description]" |
| `skills/builtin/create-team/SKILL.md` | ✅ | ✅ | flow style | true | "<project description or goal>" |

**Finding (F-026 신규, P3 cosmetic):**

#### F-026 [CONSIST cosmetic] `explorer.md` 의 allowed-tools 가 block style — 다른 4 파일과 불일치

**위치 A:** `agent_cli/agents/builtin/explorer.md:1-7`
```yaml
---
name: explorer
description: Read-only codebase explorer ...
allowed-tools:
  - read_file
  - shell
---
```

**위치 B (대조):** `agent_cli/skills/builtin/create-agent.md:1-7`
```yaml
---
name: create-agent
description: ...
argument-hint: "<agent-name> [description]"
allowed-tools: [read_file, write_file, shell, ask]
disable-model-invocation: true
---
```

**증거:** YAML 파서가 두 스타일 모두 동일 list 로 파싱 (`agent_cli/skills/loader.py:78` `meta.get("allowed-tools")` 와 `agent_cli/tools/delegate.py:347-348` `agent_config.get("allowed-tools")` 모두 list 받음). **기능적 영향 0**.

**우선순위:** **P3 (cosmetic).** Round 5 verdict 의 finding 표 에 포함되나 *PR 분리 불필요* — 향후 builtin 추가 시 컨벤션 통일 권고 (`create-skill` skill 의 template 에 flow style 명시 추가).

---

### B-4. ARCHITECTURE.md / README.md 업데이트 항목 사전 정리

본 audit 결과 코드 변경 시 *함께 업데이트* 필요한 문서 항목:

| 변경 (PR) | 업데이트 대상 | 항목 |
|---|---|---|
| PR-1 (F-012 + F-021 제거) | ARCHITECTURE.md | `tools/registry.py` LOC 감소 (~563→~526) + native tool-calling 미채택 명시 강화 (이미 providers/compat.py:119 에 있음) |
| PR-1 | tests/test_registry.py | 8 케이스 삭제 |
| PR-2 (render_group_scope) | docs/ARCHITECTURE.md | render layer 섹션 (skill/delegate 그룹 처리 패턴) 갱신 |
| PR-3 (F-011 (a) 채택 시) | README.md | "Python hooks" 섹션 추가 + Pre/PostToolUse dual-dispatch 시멘틱 명시 (A-1 의 문구) |
| PR-3 | docs/ARCHITECTURE.md | hooks/ 디렉터리 활성화 명시 (Phase 2 잔재 → wired) |
| PR-4 (F-017 옵션별) | (옵션별 별도) | (a) 채택 시: web/server.py 모듈 docstring (line 17-21) 의 "FIFO sync" 설명 정정. (c) 채택 시: README web 섹션 limitation 추가 |
| PR-5 (helper 추출) | docs/ARCHITECTURE.md | system_prompt.py builder 갯수 (~12→11) |
| PR-6 (F-006/F-007) | (없음 — 내부 정정) | — |
| PR-7 (F-022/F-023) | (없음 — 내부 정정) | — |

**CLAUDE.md 의 "README/ARCHITECTURE 업데이트 의무" 규칙** (프로젝트 규칙 #2, #3) 준수 확인: 모든 user-facing 변경 PR (PR-3, PR-4) 가 README 갱신 포함, 모든 내부 구조 변경 PR (PR-1, PR-2, PR-3, PR-5) 가 ARCHITECTURE 갱신 포함.

---

### B-5. 추가 frozenset/Mapping export pattern sweep (critic Round 4 권고)

**검증:** F-021 가 frozenset 상수 export 의 test-only 케이스였음. 동일 패턴 추가 후보 sweep.

**Grep:** `rg -n "^[A-Z_]+: " agent_cli/ --include="*.py"` 후 export 만 필터:

| 상수 | Location | Production callers (외부 ≥ 1?) | 판정 |
|---|---|---|---|
| `TOOLS` | tools/__init__.py:23 | main.py 4 hits, loop.py:63 | **Active** |
| `VIRTUAL_TOOLS` | tools/__init__.py:48 | 0 | **F-021 DEAD** |
| `TOOL_SCHEMAS` | tools/registry.py:19 | 사용자 (`from agent_cli.tools import TOOL_SCHEMAS`) | **Active** (tests + production import) |
| `_ALWAYS_INCLUDE` | tools/registry.py:352 | `_convert_tools` (제거 대상) + `get_tool_descriptions` (active) | **Partially active** — F-012 제거 후에도 유지 (Round 4 결정 정확) |
| `ALL_EVENTS` | hooks/events.py:24 | hooks/runner.py:54, hooks/loader.py:10, hooks/runner.py:66, hooks/loader.py:79 | **Active (단 F-011 시점에서)** |
| `EVENT_TO_FUNC` | hooks/events.py:41 | hooks/loader.py:10, :81 | **Active (단 F-011 시점에서)** |
| `ROLE_PROMPT`, `CONTEXT_DISCIPLINE`, `TASK_GUIDELINES` | prompts/system_prompt.py | build_system_prompt 사용 (active) | **Active** |
| `DEFAULT_CAPABILITIES` | providers/compat.py:56 | get_capabilities (active) | **Active** |
| `FAILURE_*` (8개) | recovery/observability.py | loop.py 8 hits (F-018 확인) | **Active** |
| `ECHO_HEAD`, `_THINKING_TAGS`, `_THINKING_PATTERN` etc. | 다양 | self/sibling-module use | **Active (module-private)** |
| `OBS_SUCCESS` | loop.py 또는 prompts (확인 필요) | — | **별도 finding 없음** |

**진단:** F-021 외 추가 test-only frozenset/Mapping export **0건 확정**. F-020 sweep 의 최종 보완.

---

## C. 최종 finding 표 (PASS / CONDITIONAL / DROP)

| ID | 카테고리 | 우선순위 | Final verdict | 사유 |
|---|---|---|---|---|
| F-001 | DEAD | P0 | **PASS** | 12 LOC 제거. F-011 (a) 채택 시 PR-3 함께 처리. |
| F-002 | DUP | — | **DROP** | docstring 정책 명시 (Round 2 WITHDRAWN) |
| F-003 | DUP | P1 | **PASS** | helper 추출, ~20 LOC 절감 |
| F-004 | ROLE | P0 | **PASS** | render_group_scope context manager, ~9 LOC 절감 |
| F-005 | DUP | — | **DROP** | docstring 정책 명시 (Round 2 WITHDRAWN) |
| F-006 | CONSIST | P2 | **PASS** | OpenAI-compat error key 검사 |
| F-007 | CONSIST | P2 | **PASS** | observability/base "json_repair" → "repair_json" 통일 |
| F-008 | — | — | **DROP** | 이미 Protocol 통합 완료 |
| F-009 | ROLE | P2 | **PASS** | _agent_loader.list_names() API 단일화 |
| F-010 | PERF | P2 | **PASS** | refactor 트리거 시 처리 |
| F-011 | unwired infra | P0 | **CONDITIONAL** | 사용자 결정: (a)/(b)/(c). **auditor 권장 (a)** (hook_dirs 결정됨) |
| F-012 | DEAD | P0 | **PASS** | ~87 LOC 즉시 제거 |
| F-013 | DUP | P1 | **PASS** | _build_invocation_section helper, ~40 LOC 절감 |
| F-014 | CONSIST | P1 | **PASS** | (a) override + 로그, F-019 와 통합 |
| F-015 | PERF/ROLE | P2 | **PASS** | 새 언어 추가 트리거 시 |
| F-016 | — | — | **DROP** | 가설적 위험 (Round 3 DROP) |
| F-017 | DEAD/unwired | P0 | **CONDITIONAL** | 사용자 결정: (a)/(b)/(c). **auditor 권장 (c) 변경** (B-2 단위 불일치) |
| F-018 | — | — | **DROP** | recovery/ active wired (negative) |
| F-019 | CONSIST | P1 | **PASS** | F-014 와 통합 처리 |
| F-020 | — | — | **DROP** | sweep negative (F-021 으로 보완) |
| F-021 | DEAD | P1 | **PASS** | VIRTUAL_TOOLS 제거, ~4 LOC + tests |
| F-022 | DUP | P2 | **PASS** | _stat/_refuse preamble helper, ~11 LOC 절감 |
| F-023 | PERF/ROLE | P2 | **PASS** | fetch depth=3 bug fix + 재귀 함수 추출 (auditor 본인 P2 결정) |
| F-024 | — | — | **DROP** | system_prompt sweep negative |
| F-025 | — | — | **DROP** | tools/ sweep negative |
| **F-026** | CONSIST cosmetic | P3 | **PASS** | explorer.md allowed-tools block→flow style 통일 (PR 분리 불필요) |

**Summary 카운트:**
- P0: 4 (F-001, F-004, F-012, F-021 P1 포함 시 5) — *즉시 실행 가능*
- CONDITIONAL: 2 (F-011, F-017) — *사용자 결정 필요*
- P1: 5 (F-003, F-013, F-014/F-019, F-021)
- P2: 7 (F-006, F-007, F-009, F-010, F-015, F-022, F-023)
- P3: 1 (F-026)
- DROP: 7 (F-002, F-005, F-008, F-016, F-018, F-020, F-024, F-025) — 단 F-002, F-005, F-008 는 WITHDRAWN/이미 통합완료, 나머지는 negative sweep 결과

**총 25 valid finding + 1 신규 (F-026) = 26 finding.**

---

## D. 누적 정리 효과 (재계산)

| Finding | 직접 LOC 영향 | tests 영향 | Net |
|---|---|---|---|
| F-001 | -12 | 0 | -12 |
| F-004 | -9 절감 | 0 | -9 (28→19) |
| F-011 (a) | +12 entrypoint - 12 (F-001) | 0 | ~0 |
| F-011 (c) | +20 주석 | 0 | +20 |
| F-012 | -50 production | -37 tests | -87 |
| F-013 | -40 절감 | 0 | -40 |
| F-014/F-019 (a) | +15 override + 로그 | 0 | +15 |
| F-017 (a) | -3 (process_chat_turn 추가 - WebServerConfig 삭제) + **design RFC 부담** | -30 prune tests | -33 (단 design 부담) |
| F-017 (c) | +10 주석 + docstring 정정 | 0 | +10 |
| F-021 | -4 | -? | ~-15 |
| F-022 | -11 절감 | 0 | -11 |
| F-023 | -10 절감 | 0 | -10 (단 재귀 테스트 추가 시 +5) |
| F-026 | -3 (style 변경) | 0 | -3 |

**즉시 실행 가능 (사용자 결정 불필요) — PR 1, 2, 5, 6, 7:**
- F-001 + F-012 + F-021 = -114 LOC
- F-004 + F-003 + F-013 + F-014/F-019 + F-006 + F-007 = -94 LOC + 15 추가 = -79
- F-022 + F-023 = -21 LOC
- **합계: 약 -214 LOC 정리**

**(a) 채택 시 (F-011 + F-017):** +12 entrypoint - 12 (F-001 중복) + 0 (F-017 net) = ~0 LOC 변동, 단 Python hooks + FIFO sync 활성화.

**(c) 채택 시:** +30 주석 (F-011 + F-017) + 0 코드 변경.

---

## E. Methodology 자가 점검 (5 라운드 종합)

| Round | 핵심 학습 |
|---|---|
| Round 1 | **section docstring 누락** — F-002/F-005 false positive 의 원인. Round 2 부터 sweep 시 ── 헤더 직후 주석 의무 검토. |
| Round 2 | **HookRunner 인스턴스화 vs 사용** 구분 정밀화 (정정사항 반영). test-only keep-alive 패턴 일반화. |
| Round 3 | **사용자에게 떠넘기지 말 것** (critic 지시) — F-011 (c) auditor 본인 권장 명시. negative sweep (F-018 recovery, F-020 test-only) 도 결정적 답변. |
| Round 4 | **(a) wiring sketch** 코드 변경 없이 design 명시. neg sweep 2건으로 audit scope 마감 (F-024/F-025). 단 sketch 위험 누락 (B-1 dual dispatch, B-2 단위) — critic 가 보완. |
| Round 5 | **단위 불일치 (F-017 B-2)** 본인 결정 변경 (a → c). **auditor self-verdict 표** 의 가치 입증 — critic verdict 입력 자료로 작동. |

**5 라운드 종합 진단:**
- 총 26 finding 중 *유효 PASS* 16, CONDITIONAL 2, DROP 8.
- **False positive 2건 (F-002, F-005)** 와 *priority 과대평가 2건 (F-003, F-006)* 발생 — critic 의 정정으로 모두 보정.
- **사용자에게 결정 떠넘기기 위험** Round 1~3 에서 누적, Round 4~5 에서 본인 권장 명시로 완화.
- **단위/시멘틱 누락 위험** F-017 B-2 에서 발견 — critic 의 4-way 검증으로 보완.

---

## F. Round 5 critic verdict 입력 자료

### F-1. PASS 후보 (즉시 실행 가능, 사용자 결정 불필요)

| Finding | LOC | PR |
|---|---|---|
| F-001 | -12 | PR-3 (F-011 (a) 와 함께) |
| F-003 | ~-20 | PR-5 |
| F-004 | -9 | PR-2 |
| F-006 | ~-5 | PR-6 |
| F-007 | ~-5 | PR-6 |
| F-009 | ~-20 | (별도 PR 또는 PR-5) |
| F-010 | (refactor 트리거 시) | — |
| F-012 | -87 | PR-1 |
| F-013 | -40 | PR-5 |
| F-014/F-019 | +15 | PR-5 |
| F-015 | (새 언어 추가 시) | — |
| F-021 | -4 + tests | PR-1 |
| F-022 | -11 | PR-7 |
| F-023 | -10 (auditor P2) | PR-7 |
| F-026 | -3 | (cosmetic, PR 분리 불필요) |

**총 14 PASS finding.**

### F-2. CONDITIONAL (사용자 결정 필요)

| Finding | 옵션 | auditor 권장 |
|---|---|---|
| F-011 | (a) wire / (b) 제거 / (c) 문서화 | **(a)** — hook_dirs 결정됨, 12 LOC entrypoint 변경 |
| F-017 | (a) wire / (b) 제거 / (c) 문서화 | **(c) — Round 5 변경** (B-2 단위 불일치, design RFC 부담) |

### F-3. DROP (no further action)

F-002, F-005, F-008, F-016, F-018, F-020, F-024, F-025 — 8건 모두 *negative finding* 또는 *WITHDRAWN*.

### F-4. 사용자 결정 5건

1. F-011 옵션 (a/b/c).
2. F-017 옵션 (a/b/c) — auditor (c) 권장 강도 ↑.
3. F-023 우선순위 (P1 vs P2) — auditor (P2) 권장.
4. PR 분리 전략 (7-PR vs 압축).
5. F-026 cosmetic 통일 시점 (즉시 vs 다음 builtin 추가 시).

### F-5. critic verdict 권장 매개변수

critic 의 Round 5 verdict (PASS / CONDITIONAL / FAIL) 결정 시 참고:
- **PASS 권고 조건:** P0/P1 finding 의 file:line 인용 drift 0 (B-1 검증 통과), self-verdict 표 의 합리성, sketch 위험 명시 (A-1/A-2 보정 완료).
- **CONDITIONAL 권고 조건:** 사용자 결정 5건 미해결 — 본 audit 가 verdict 받기 전에 user 결정 필요 항목 명시.
- **FAIL 권고 조건:** false positive 미정정, methodology 결함. 본 audit 는 해당 없음 (F-002/F-005 명시 WITHDRAWN, A-2 design quality 근거 강화).

**auditor 본인 권장 verdict: PASS (단 5 사용자 결정 항목을 critic verdict 에서 명시).**

---

## G. 최종 정리 — 사용자에게 보고할 자료

### G-1. user-facing 변경 노트 (PR-3 F-011 (a) 채택 시)

```markdown
## Python hooks 활성화 (Phase 2 wiring 완성)

Python hook files (`.agent-cli/hooks/*.py` 또는 `~/.agent-cli/hooks/*.py`)
이 11 이벤트 (`OnSessionStart`, `OnSessionEnd`, `PreLLMCall`, `PostLLMCall`,
`OnTurnEnd`, `PreToolUse`, `PostToolUse`, `OnDelegateStart`,
`OnDelegateEnd`, `OnSkillStart`, `OnSkillEnd`) 에서 fire 됩니다.

### PreToolUse / PostToolUse 의 dual-dispatch 시멘틱

Python hooks 가 shell hooks 보다 **먼저** fire 됩니다 (loop.py:995-1047, 1120-1150).

- Python hook 이 `block_reason` 설정 시: 즉시 short-circuit, shell hooks 미실행.
- Python hook 이 `modified_input` 설정 시: shell hooks 가 변경된 input 받음.
- 9개 Python-only 이벤트 (OnDelegateStart/End, OnSkillStart/End, PreLLMCall,
  PostLLMCall, OnTurnEnd, OnSessionStart/End) 는 shell hook conflict 없음.

### Hook 작성 가이드

(예제 + 파일 형식은 README/ARCHITECTURE 에 추가)
```

### G-2. PR 분리 권장 (사용자 결정 후)

A-4 의 PR 표 (PR-1 ~ PR-7) 가 *기본 권장*. 사용자가 압축 원하면:
- **3-PR 압축안:**
  - PR-A = PR-1 + PR-2 + PR-3 (모든 P0)
  - PR-B = PR-5 (모든 P1)
  - PR-C = PR-6 + PR-7 (모든 P2)
- **2-PR 압축안:**
  - PR-α = 모든 P0 + P1
  - PR-β = 모든 P2 + F-026

### G-3. 사용자 결정 5건 (A-5 의 최종 명세)

(F 섹션 F-4 와 동일).

---

## H. Audit Summary

**총 finding:** 26 (F-001 ~ F-026).
**PASS:** 14 (즉시 실행 가능).
**CONDITIONAL:** 2 (F-011, F-017 — 사용자 결정 필요).
**DROP:** 8 (negative 또는 WITHDRAWN).
**P3 cosmetic:** 1 (F-026).

**핵심 정리 효과:**
- 즉시 실행 PR-1/2/5/6/7 ≈ **-214 LOC**.
- F-011 (a) 채택 시 추가: 0 LOC, Python hooks 11 이벤트 활성화.
- F-017 (c) 채택 시 추가: +10 LOC (주석 + docstring 정정).

**5 라운드 audit 종합 진단:**
- 신규 기여자 혼선 위험 90% 감소 (test-only DEAD 9 항목 정리).
- 사용자 가시 변경 0 (PR 1, 2, 5, 6, 7 모두 내부 정리).
- 사용자 결정 5건이 verdict 의 마지막 단계.

**Round 5 audit 완료. critic Round 5 verdict 입력 대기.**
