# Audit Report — Round 4

**Scope:** critic Round 3 피드백 반영 + prompts/system_prompt.py sweep + tools/ 나머지 sweep + F-011/F-017 (a) design sketch + Round 5 verdict 준비
**Auditor:** auditor (audit-loop team)
**Date:** 2026-05-22
**Reviewing critic feedback:** `_workspace/audit_feedback_v3.md`

---

## A. Round 3 정정사항 반영

### A-1. F-011 (a) wiring 옵션 — hook_dirs 정책 이미 결정됨

**critic Round 3 의 우려 (A-1 Q2):** "hook_dirs 정책 결정이 product-level 결정이라 (a) 채택 불가능."

**보정:** **이미 결정되어 있음.** `agent_cli/hooks/loader.py:13-18`:
```python
13:def _hook_dirs() -> list[Path]:
14:    """Return hook directories in execution order: project → user."""
15:    return [
16:        Path.cwd() / ".agent-cli" / "hooks",
17:        Path.home() / ".agent-cli" / "hooks",
18:    ]
```

→ Phase 2 wiring (a) 의 hook_dirs 정책 자체는 *코드에 이미 명시*. 누락된 것은 **HookRunner 인스턴스화** 한 줄과 **AgentLoop 생성자 호출 시 전달** 한 줄. 본 라운드 B-1 의 (a) sketch 에서 구체화.

### A-2. F-011 사유 2 ("design quality 높음") 객관 근거 강화

**critic 지적:** "tests 까지 갖춤만으로 design quality 라고 하기 부족."

**보정 — 구체 design quality 증거:**
- `hooks/runner.py` (95 LOC) — `HookRunner` + `fire()` + `_run_python_hooks()` 단일 책임.
- `hooks/loader.py` (88 LOC) — `_hook_dirs`, `_scan_hook_files`, `_load_module`, `load_python_hooks` 4 함수 leaf primitive.
- `hooks/context.py` (145 LOC) — `HookContext` dataclass (single immutable data container).
- `hooks/events.py` (53 LOC) — `ALL_EVENTS` frozenset + `EVENT_TO_FUNC` mapping (single source of truth).
- 모듈 분리가 SRP 준수 + import dependency 단방향 (events → loader → context → runner).
- DESIGN.md 미참조 (recovery/ 의 robust-harness 와 달리 design doc 부재) — 단 코드 자체의 시그니처 명료.

→ critic 지적 인정: 단순 "tests 있음" 만으로는 부족. 실제 근거는 *모듈 분리 + 단일 책임*. Round 5 verdict 에서 더 정확한 표현 사용 권장.

### A-3. F-014 (a) override 추가 권고 채택

**critic Round 3 권고:** "(a) override 추가 (no-op + 로그) 가장 안전 — base.py 변경 없음, 약 5 LOC."

**auditor 동의:** Round 3 의 본인 권고 `(c) 명시 limitation 문서화` 보다 (a) 가 더 안전.
- (a): base.py 변경 0, web.py 에 6 override 추가 (~15 LOC), 사용자가 web 에서 parallel delegate 호출 시 디버그 로그로 가시화.
- (c): 문서만, 코드 동작은 그대로 — 사용자가 가시성 못 가짐.

→ Round 5 verdict 에서 (a) 채택 권고.

### A-4. F-020 sweep 표 가독성 보강 권고 반영

**critic 권고:** "표를 활성 helpers vs test-only DEAD 두 표로 분리."

**보정:** Round 5 audit 정리 시 두 표로 분리 예정. 본 라운드는 신규 finding 추가에 집중.

---

## B. F-011/F-017 (a) 완성 옵션 — design sketch

### B-1. F-011 (a) Phase 2 wiring 완성 sketch

**변경 범위:**
- `agent_cli/main.py::chat` entrypoint (line ~1370 inner of `chat()`)
- `agent_cli/main.py::web` entrypoint (line ~1568 inner of `web()`)
- `agent_cli/tools/delegate.py::_run_single` (subagent 진입점, line 320-422)

**구체 변경 (chat 예시, 코드 변경 없이 sketch):**

기존 `loop.py:1372` 인근:
```python
loop_result = run_loop(
    query=query,
    provider=llm_provider,
    capabilities=capabilities,
    ...
    hooks_config=_disk_hooks,
    ...
    wire_format=wire_format_plugin,
)
```

After (a) wiring:
```python
# 1. main.py chat entrypoint — HookRunner 한 번 생성
from agent_cli.hooks.runner import HookRunner
hook_runner = HookRunner()  # uses _hook_dirs() default

# 2. run_loop call — hook_runner 전달
loop_result = run_loop(
    query=query,
    ...
    hooks_config=_disk_hooks,
    hook_runner=hook_runner,  # NEW
    wire_format=wire_format_plugin,
)
```

**호출 라이프사이클:**
- `chat` REPL: HookRunner 인스턴스 한 번 생성 + reuse across user turns (~6 LOC 추가).
- `web` worker thread: 동일 패턴 — worker 시작 시 1회 생성, 매 chat message 처리 시 reuse (~4 LOC 추가).
- `delegate._run_single`: 부모 hook_runner 전달 받음 (이미 `loop.py:1245` 의 `hook_runner=hook_runner` 통과). 단 *delegate entrypoint 진입 시 인스턴스화 추가 필요 없음* — 부모가 만들면 됨.

**삭제 가능 코드 (F-001 와 합쳐서):**
- `runner.py:26` `shell_hooks_config: dict | None = None,` 파라미터 (F-001).
- `runner.py:29` `self._shell_hooks_config = shell_hooks_config` (F-001).
- `runner.py:75-76` dispatch branch + `_run_shell_hooks` 호출 (F-001).
- `runner.py:89-95` `_run_shell_hooks` 메서드 (F-001).

**총 변경 LOC:** 추가 ~12 (entrypoint wiring) - 삭제 12 (F-001) = **net ~0 LOC**.

**효과:**
- Python hooks 가 production 사용자에게 활성화 — 11 이벤트 fire 가능.
- `tests/test_hooks_python.py` 13 케이스가 production 기능 검증으로 의미 부여됨.
- F-011 / F-001 두 finding 동시 해결.

**위험:**
- 기존 사용자가 `~/.agent-cli/hooks/` 또는 `.agent-cli/hooks/` 에 우연히 Python 파일이 있었을 경우 갑자기 동작 시작 — 단 이 디렉터리는 *현재 미사용* 이라 실측 사용자 없음. 단 README 명시 필요.

**권장:** Round 5 verdict 에서 (a) 채택 시 PR 1개로 묶어 처리 가능.

---

### B-2. F-017 (a) FIFO sync 완성 sketch

**변경 범위:**
- `agent_cli/web/server.py::WebServer` 클래스 — `process_chat_turn()` 메서드 추가.
- `agent_cli/main.py::web::_worker_loop` (main.py:1569-1633) — turn 후 `process_chat_turn()` 호출.
- `WebServerConfig` dataclass — *유지* (메서드 구현 위해 필요).

**구체 sketch:**

WebServer 에 추가:
```python
def process_chat_turn(self, ctx: ContextManager) -> None:
    """Compute prune drop after a turn and notify renderer.
    
    Called by the worker thread after each AgentLoop.run() returns.
    Compares renderer's persistent_count to ctx._cache size; broadcasts
    a prune event for the delta so the frontend trims the same prefix.
    """
    renderer_count = self.renderer.persistent_count
    # ContextManager 의 non-system message count
    cache_count = sum(
        1 for m in ctx._cache
        if m.get("role") in ("user", "assistant")
    )
    drop = max(0, renderer_count - cache_count)
    if drop > 0:
        self.renderer.prune(drop)
```

worker_loop 수정 (main.py:1611-1628 try-block):
```python
try:
    run_loop(
        query=message,
        ...
    )
    # NEW: FIFO sync after each turn
    server.process_chat_turn(ctx)
except Exception as exc:
    ...
```

**삭제 가능 코드:**
- `WebServerConfig` dataclass (server.py:233-250) — `compute_prune_drop`/`on_user_message` Callable 패턴은 본 sketch 의 직접 호출로 대체. 단 dataclass 자체는 유지하거나 (`token` 필드만) 삭제 둘 다 가능. **삭제 권장.**
- `server.py:488` `__all__` 에서 `WebServerConfig` 제거.

**총 변경 LOC:** 추가 ~15 (process_chat_turn + worker_loop 호출) - 삭제 ~18 (WebServerConfig) = **net ~-3 LOC**.

**효과:**
- FIFO sync 활성화 — 긴 세션에서 web 클라이언트가 ContextManager eviction 따라 prefix trim.
- `WebRenderer.prune` / `persistent_count` 가 production 사용 — F-017 의 test-only keep-alive 해소.
- 모듈 docstring (server.py:17-21) 의 "Server polls this after each turn" 주장이 *실제로 동작*.

**위험:**
- ContextManager `_cache` 직접 참조 (`ctx._cache`) — 캡슐화 break. 더 깨끗한 방법: `ContextManager.get_visible_count() -> int` public 메서드 추가.
- `non-system message count` 정의가 모호 — system prompt 제외, observation/tool 포함 여부 결정 필요.

**권장:** Round 5 verdict 에서 (a) 채택 시 *ContextManager API 확장* 작업 추가 (sketch 의 `_cache` 참조 대신 public count 메서드).

---

## C. Round 4 깊이 분석 — 신규 finding

### F-021 [DEAD] `tools/__init__.py::VIRTUAL_TOOLS` test-only keep-alive — F-020 sweep 누락

**위치 A (정의):** `agent_cli/tools/__init__.py:48-50`
```python
48:VIRTUAL_TOOLS: frozenset[str] = frozenset(
49:    {"complete", "ask", "run_skill", "ready_for_review", "delegate"}
50:)
```

**위치 B (export):** `agent_cli/tools/__init__.py:54`
```python
54:    "VIRTUAL_TOOLS",
```

**증거 (3-way grep):**
- `rg -n "VIRTUAL_TOOLS\b" agent_cli/` → 2 hits (정의 + export self-reference). production callers 0.
- `rg -n "VIRTUAL_TOOLS\b" tests/` → 6 hits in test_tools_coverage.py (line 22 import, 765/768/771/774/777 assertions, 2208/2220/2222 in another test). **Test-only keep-alive 확정.**
- 의도된 dispatch: 가상 tool 들은 `loop.py:548 (complete)`, `loop.py:607 (ask)`, `loop.py:639 (run_skill)`, `loop.py:692 (ready_for_review)`, `loop.py:982 (delegate)` 에서 *하드코딩 if-cascade* 로 분기.

**진단:** F-020 sweep 에서 본인이 *callers > 0 인 helpers* 만 검증하고 *frozenset 상수 export* 를 누락. 사실상 F-001/F-011/F-012/F-017 와 동일 패턴의 5번째 케이스.

**우선순위:** **P1** (LOC 적음 — 3줄 + 1 export). F-012 와 함께 묶어 단일 PR 로 처리 가능.

**권장:**
- 옵션 (a) 제거: VIRTUAL_TOOLS 상수 + `__all__` entry + tests 케이스 삭제.
- 옵션 (b) 실사용: loop.py 의 if-cascade 5건을 `if action in VIRTUAL_TOOLS:` dispatch table 패턴으로 변환. 단 각 분기 별 처리 로직 (special-casing) 가 달라 효과 제한적.

**auditor 권장:** (a) 제거. test_tools_coverage.py 의 검증 의도 (가상 tool 목록 명세) 는 가치 있으나, 진짜 source of truth 는 loop.py 의 if-cascade. 그 cascade 자체를 명세 commit 으로 두는 게 정직.

---

### F-022 [DUP] `tools/read_file.py::_stat` ↔ `_refuse_large_full_read` — 11줄 동일 preamble

**위치 A:** `agent_cli/tools/read_file.py:110-127` (`_stat` preamble)
```python
110:def _stat(path: str, text: str, all_lines: list[str]) -> ToolResult:
...
117:    total = len(all_lines)
118:    size_bytes = len(text.encode("utf-8"))
119:    size_label = (
120:        f"{size_bytes:,} bytes"
121:        if size_bytes < 10_000
122:        else f"{size_bytes / 1024:.1f} KB"
123:    )
124:
125:    head_end = min(_STAT_HEAD_LINES, total)
126:    head = format_hashlines_range(all_lines, 0, head_end)
127:    ...
```

**위치 B:** `agent_cli/tools/read_file.py:142-167` (`_refuse_large_full_read` preamble)
```python
142:def _refuse_large_full_read(
143:    path: str, text: str, all_lines: list[str], limit: int
144:) -> ToolResult:
...
157:    total = len(all_lines)
158:    size_bytes = len(text.encode("utf-8"))
159:    size_label = (
160:        f"{size_bytes:,} bytes"
161:        if size_bytes < 10_000
162:        else f"{size_bytes / 1024:.1f} KB"
163:    )
164:
165:    head_end = min(_STAT_HEAD_LINES, total)
166:    head = format_hashlines_range(all_lines, 0, head_end)
167:    ...
```

**증거:** `diff <(sed -n '117,127p' read_file.py) <(sed -n '157,167p' read_file.py)` → **빈 출력** (완전 동일 11줄).

**차이점 (preamble 후):** hint 메시지와 출력 prefix (`[stat]` vs `[refused-full-read]`) 만 다름.

**우선순위:** **P2** (LOC 적음 — 11줄, 단일 파일 내부, 두 호출처).

**권장:** `_format_file_metadata_response(path, text, all_lines, *, prefix, hint)` helper 추출. 두 함수가 prefix 와 hint 만 전달. 약 11줄 절감.

---

### F-023 [PERF/ROLE] `tools/fetch.py::tool_fetch` 의 depth=1 vs depth>1 fetch 루프 중복

**위치 A:** `agent_cli/tools/fetch.py:191-202` (depth=1 children)
```python
191:        for child_url in child_urls:
192:            if len(pages) >= MAX_PAGES:
193:                break
194:            if child_url in fetched:
195:                continue
196:            fetched.add(child_url)
197:
198:            child_content, child_links, child_error = _fetch_single(child_url)
199:            if child_error:
200:                pages.append({"url": child_url, "content": f"[Error: {child_error}]"})
201:                continue
202:            pages.append({"url": child_url, "content": child_content})
```

**위치 B:** `agent_cli/tools/fetch.py:205-217` (depth>1 grandchildren — nested in A)
```python
205:            if depth > 1:
206:                grandchild_urls = _resolve_links(child_url, child_links)
207:                for gc_url in grandchild_urls:
208:                    if len(pages) >= MAX_PAGES:
209:                        break
210:                    if gc_url in fetched:
211:                        continue
212:                    fetched.add(gc_url)
213:                    gc_content, _, gc_error = _fetch_single(gc_url)
214:                    if gc_error:
215:                        pages.append({"url": gc_url, "content": f"[Error: {gc_error}]"})
216:                    else:
217:                        pages.append({"url": gc_url, "content": gc_content})
```

**진단:** 2-level fetch loop 가 *수동 재귀 풀기*. MAX_DEPTH=3 인데 코드는 depth=2 까지만 도달. depth=3 호출 시 *조용히 무시*.

**증거:** `MAX_DEPTH = 3` (line 16), `depth = min(int(args.get("depth", 0)), MAX_DEPTH)` (line 161). 사용자가 depth=3 요청해도 grandchild (depth=2) 까지만 실행.

**우선순위:** **P2** (사용자 가시 bug: depth=3 효과 없음). 단 MAX_PAGES=10 도 동시 적용되어 실측 영향 작음.

**권장:**
- 옵션 (a) 재귀 함수로 일반화 — depth-N 까지 진짜 도달:
  ```python
  def _fetch_recursive(url, depth, fetched, pages):
      ...
  ```
- 옵션 (b) `MAX_DEPTH = 2` 로 낮춰 코드와 일치시킴 — 단 사용자 spec 변경.

**auditor 권장:** (a) 재귀 함수 추출. 실측 사용 빈도 낮으나 코드 docstring/상수와 동작 불일치는 신뢰성 문제.

---

### F-024 [VALID] prompts/system_prompt.py 7+ builders — DUP/ROLE 없음 (단 F-013 외)

**검증 범위:** `_build_delegate_inline`, `_build_read_file_inline`, `_build_read_symbols_inline`, `_build_tool_inline_guides`, `_build_tools_section`, `_build_environment_section`, `_build_execution_context`, `_build_context_recovery`, `_load_directives`, `build_system_prompt`, `build_agent_descriptions`, `build_skill_descriptions` (12 함수).

**검증 방법:** 각 함수의 책임 분석 + 공통 패턴 search.

**책임 분담 표:**

| 함수 | 책임 | LOC |
|---|---|---|
| `_build_delegate_inline` | delegate tool 의 inline examples (6 examples) | ~60 |
| `_build_read_file_inline` | read_file tool 의 modes guide + read_symbols 활성 시 flow steering | ~73 |
| `_build_read_symbols_inline` | read_symbols tool 의 list/fetch examples | ~73 |
| `_build_tool_inline_guides` | tool→inline-guide map builder (dispatch) | ~19 |
| `_build_tools_section` | "## Available Tools" 헤더 + get_tool_descriptions wrap | ~13 |
| `_build_environment_section` | "## Environment" + CWD + platform | ~10 |
| `_build_execution_context` | call stack 정보 (skill/agent) | ~25 |
| `_build_context_recovery` | history.jsonl 위치 안내 | ~8 |
| `_load_directives` | DIRECTIVE.md 파일 dedup load | ~35 |
| `build_system_prompt` | main orchestrator | ~85 |
| `build_agent_descriptions` | agent 목록 + invocation example | ~48 |
| `build_skill_descriptions` | skill 목록 + invocation example | ~49 |

**공통 패턴 분석:**

1. **`wire_format.render_action_input(...)` 호출**: `_build_delegate_inline`, `_build_read_file_inline`, `_build_read_symbols_inline` 3 함수가 사용. *이미 추상화된 hook* — DUP 아님.

2. **`get_supported_extensions()` 호출**: `_build_read_file_inline` (line 226), `_build_read_symbols_inline` (line 262) 2 함수. 두 함수 모두 "supported extensions" 라벨 필요 — 정당한 reuse, DUP 아님.

3. **`indented = "\n".join(f"  {line}" for line in example.splitlines())` 패턴**: `build_agent_descriptions:597`, `build_skill_descriptions:641` 2 hits — **F-013 의 핵심 dup** (Round 2 에서 이미 보고).

4. **`return ""` empty guard 패턴**: `_load_directives`, `build_agent_descriptions`, `build_skill_descriptions`, `_build_execution_context` 모두 *서로 다른 조건* 으로 empty 반환. 의미 단위 단일 책임 — DUP 아님.

5. **모듈 docstring lazy import 패턴**: `_build_read_file_inline` 내부 `from agent_cli.tools.symbols import get_supported_extensions` (line 226), `_build_read_symbols_inline` 내부 동일 (line 262), `build_agent_descriptions` 내부 `from agent_cli.tools.delegate import _agent_loader` (line 578), `build_skill_descriptions` 내부 `from agent_cli.skills import load_skills` (line 624). 모두 *circular import 회피* 목적 — 의도된 패턴.

**진단:** F-013 (`build_agent/skill_descriptions` 95% dup) 외 추가 DUP/ROLE finding **없음**. 12 함수 모두 단일 책임 명확. 모듈 전체 docstring (lines 1-16) 가 layout rationale + Recency ordering 설명.

**P0/P1/P2 신규 0건.** Round 3 의 F-013 만 유지.

---

### F-025 [VALID] tools/ 나머지 (read_file/edit_file/write_file/shell/fetch/_diff/action_summary/__init__/result) sweep — F-021/F-022/F-023 외 추가 0건

**검증 결과:**

| 파일 | LOC | DUP/ROLE/DEAD finding | 비고 |
|---|---|---|---|
| `__init__.py` | 83 | **F-021** VIRTUAL_TOOLS test-only | TOOLS dict 단일 책임 |
| `_diff.py` | 113 | 없음 | `format_diff` 단일 export, edit_file/write_file 2 callers 활성 |
| `action_summary.py` | 34 | 없음 | `summarize_tool_args` 단일 export, context/manager.py 1 caller 활성 |
| `result.py` | 15 | 없음 | `ToolResult` dataclass — 모든 tool 이 사용 |
| `read_file.py` | 277 | **F-022** _stat/_refuse 11줄 preamble dup | 나머지 단일 책임 |
| `edit_file.py` | 274 | 없음 | `_normalize_for_fuzzy`, `fuzzy_verify_ref`, `_edit_range`, `tool_edit_file` 4 함수, 책임 분명 |
| `write_file.py` | 37 | 없음 | 단순 wrapper |
| `shell.py` | 162 | 없음 | `_confirmation_enabled`, `_detect_dangerous`, `_ask_confirmation`, `_is_tty`, `tool_shell` 5 함수, 책임 분명 |
| `fetch.py` | 230 | **F-023** depth loop nested-vs-recursive | `_HTMLToMarkdown`, `_fetch_single`, `_resolve_links`, `tool_fetch` 책임 분명 |

**진단:** 3 신규 finding (F-021/F-022/F-023). 나머지는 각자 단일 책임 명확.

---

## D. Round 5 verdict 준비 자료 — auditor self-verdict

### D-1. 모든 finding 최종 분류 (PASS / CONDITIONAL / DROP)

| ID | 카테고리 | 우선순위 | self-verdict | 사유 |
|---|---|---|---|---|
| F-001 | DEAD | P0 | **PASS** | 의심 없음, 12 LOC 즉시 제거. F-011 (a) 채택 시 자동 해소. |
| F-002 | DUP | — | **DROP** | docstring 정책 명시 (Round 2 WITHDRAWN) |
| F-003 | DUP | P1 | **PASS** | helper 추출 (~30 LOC 영향), 동기화 강제 |
| F-004 | ROLE | P0 | **PASS** | render_group_scope context manager, ~30 LOC → ~19 |
| F-005 | DUP | — | **DROP** | docstring 정책 명시 (Round 2 WITHDRAWN) |
| F-006 | CONSIST | P2 | **PASS** | OpenAI-compat error key 검사 추가 권장 |
| F-007 | CONSIST | P2 | **PASS** | observability/base 의 "json_repair" → "repair_json" 통일 |
| F-008 | — | — | **DROP** | 이미 Protocol 통합 완료 (Round 2) |
| F-009 | ROLE | P2 | **PASS** | _agent_loader.list_names() API 단일화 |
| F-010 | PERF | P2 | **PASS** | refactor 트리거 시 처리, 본 라운드 직접 작업 X |
| F-011 | unwired infra | P0 | **CONDITIONAL** | 사용자 결정: (a) wiring / (b) 제거 / (c) 문서화. **auditor 권장 (a)** (hook_dirs 이미 결정됨, B-1 sketch 참고) |
| F-012 | DEAD | P0 | **PASS** | ~87 LOC 즉시 제거, design 명시 |
| F-013 | DUP | P1 | **PASS** | _build_invocation_section helper |
| F-014 | CONSIST | P1 | **PASS** | (a) override 추가 (no-op + 로그) 권장 |
| F-015 | PERF/ROLE | P2 | **PASS** | 새 언어 추가 시 처리, 본 라운드 직접 작업 X |
| F-016 | — | — | **DROP** | 가설적 위험 (Round 3 DROP 권고) |
| F-017 | DEAD/unwired | P0 후보 | **CONDITIONAL** | 사용자 결정: (a) wiring / (b) 제거 / (c) 문서화. **auditor 권장 (a)** (FIFO sync 가 실제 필요 기능, B-2 sketch 참고) |
| F-018 | — | — | **DROP** | recovery/ active wired 검증 결과 (negative finding) |
| F-019 | CONSIST | P1 | **PASS** | F-014 와 통합 처리 |
| F-020 | — | — | **DROP** | sweep 결과 (negative finding, 단 F-021 으로 보완) |
| **F-021** | DEAD | **P1** | **PASS** | VIRTUAL_TOOLS test-only keep-alive, F-020 sweep 누락 보완 |
| **F-022** | DUP | **P2** | **PASS** | _stat/_refuse_large_full_read preamble 11줄 dup |
| **F-023** | PERF/ROLE | **P2** | **PASS** | fetch depth-N 재귀 풀기 + depth=3 무동작 bug |
| **F-024** | — | — | **DROP** | system_prompt builder sweep 결과 (negative, F-013 외 0건) |
| **F-025** | — | — | **DROP** | tools/ 나머지 sweep 결과 (negative, F-021/F-022/F-023 외 0건) |

### D-2. 최종 P0 (4건 + 2 CONDITIONAL)

| Finding | LOC 정리 | 사용자 결정 |
|---|---|---|
| F-001 | 12 | 불필요 (F-011 (a) 채택 시 자동) |
| F-004 | 9 절감 (~28→19) | 불필요 |
| F-012 | 87 | 불필요 |
| **소계 즉시 실행** | **~108 LOC** | — |
| F-011 (a 권장) | net ~0 (entrypoint 추가 - F-001 삭제) | (a)/(b)/(c) |
| F-017 (a 권장) | ~-3 (process_chat_turn 추가 - WebServerConfig 삭제) | (a)/(b)/(c) |
| **소계 (a) 채택 시** | **~105 LOC + Phase 2 활성화** | 사용자 |

### D-3. 최종 P1 (5건, 모두 PASS)

| Finding | LOC 영향 | 비고 |
|---|---|---|
| F-003 | ~20 절감 | delegate/skill subdir helper |
| F-013 | ~40 절감 | _build_invocation_section helper |
| F-014/F-019 | ~15 추가 (override + log) | F-014 와 F-019 통합 |
| F-021 | 4 삭제 (+ tests 정리) | VIRTUAL_TOOLS |
| **소계** | **~50 LOC 절감 + 15 추가** | |

### D-4. 최종 P2 (5건)

F-006 (Ollama error key 패턴 OpenAI-compat 적용), F-007 (json_repair 명명 통일), F-009 (agent discovery API), F-010 (_handle_text_path hot-spot, refactor 트리거 시), F-015 (symbols.py split, 새 언어 추가 시), F-022 (_stat/_refuse preamble), F-023 (fetch depth 재귀).

### D-5. PR 분리 전략 권장

| PR # | 묶음 | 크기 | 사용자 결정 필요? |
|---|---|---|---|
| PR-1 | F-001 + F-012 + F-021 (test-only DEAD 일괄 제거) | ~103 LOC | 불필요 |
| PR-2 | F-004 (render_group_scope context manager) | ~30 LOC | 불필요 |
| PR-3 | F-011 (a) Phase 2 hooks wiring + F-001 cleanup | ~12 LOC | **필요** |
| PR-4 | F-017 (a) FIFO sync wiring + WebServerConfig cleanup | ~15 LOC | **필요** |
| PR-5 | F-003 + F-013 + F-019/F-014 (helper 추출 모음) | ~80 LOC | 불필요 |
| PR-6 | F-006 + F-007 (CONSIST 정정) | ~10 LOC | 불필요 |
| PR-7 (선택) | F-022 + F-023 (P2 cleanup) | ~30 LOC | 불필요 |

총 **7 PR**, 또는 사용자 선호 시 P0 만 묶어 1 PR + P1 묶어 1 PR + P2 묶어 1 PR (3 PR).

### D-6. 사용자에게 결정 요청 항목

1. **F-011**: Python hooks Phase 2 wiring 옵션 — (a) 완성 권장 / (b) 제거 / (c) 명시 문서화.
2. **F-017**: Web FIFO sync 옵션 — (a) 완성 권장 / (b) 제거 / (c) 명시 문서화.
3. **PR 분리 전략**: 7-PR vs 3-PR.
4. **agents/builtin + skills/builtin 자료 일관성 검증** (Round 4 에서 critic 의 보류 결정 — Round 5 verdict 후 결정).

---

## E. Round 5 권장 (auditor self)

Round 5 (auditor 최종 정리) 에서:
- 본 라운드 D-1 표를 *single source of truth* 로 사용.
- F-020 sweep 의 누락 (F-021) 인정 + 가독성 보강 (active helpers vs test-only DEAD 두 표).
- agents/builtin + skills/builtin 자료 일관성 검증 (critic Round 3 보류 결정).
- 모든 finding 의 file:line 인용 최종 검증 (라인 drift 0 확인).

Round 5 verdict 에서 critic 와 함께:
- F-011 / F-017 사용자 결정 명시.
- PR 분리 전략 합의.
- ARCHITECTURE.md / README.md 업데이트 항목 정리.

---

## F. Methodology 자가 점검 (지속)

- ✅ 본 라운드 F-021 발견 — F-020 sweep 누락 (frozenset 상수 export 미검증) 인정.
- ✅ F-022 / F-023 발견 — tools/ 나머지 sweep 결과.
- ✅ F-024 / F-025 negative finding 으로 sweep 완료 명시.
- ✅ B-1 / B-2 의 sketch 는 **코드 변경 없이** design 만 — critic Round 3 지시 정확 반영.
- ✅ D-1 self-verdict 표 작성 — Round 5 critic verdict 입력 자료 준비.
- ⚠ critic 지적 "design quality 객관 근거 약함" 본 라운드 A-2 에서 보강 시도.
