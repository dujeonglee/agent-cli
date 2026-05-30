# Audit Report — Round 2

**Scope:** critic Round 1 피드백 반영 + critic 지정 영역 깊이 분석
**Auditor:** auditor (audit-loop team)
**Date:** 2026-05-22
**Reviewing critic feedback:** `_workspace/audit_feedback_v1.md`

---

## A. Round 1 정정 및 철회

### A-1. F-002 / F-005 — 명시적 철회

**철회 사유:** 본인이 `react.py:28-36` (모듈 docstring) 과 `prefix_md.py:130-134` (parser section docstring) 를 누락 검토.

**관련 정책 인용 (재독):**
```python
# react.py:28-36
# 3-stage fallback parser plus its stage-2 JSON repair helper. Lives
# entirely in this module (no ``parsing/`` package, no shared
# ``json_repair`` module) so the whole ReAct format — parser,
# repair, format rules, recovery wording, history rendering — is
# folder-deletable as a single boundary. If a future plugin needs the
# same JSON repair algorithm we re-evaluate sharing at that point;
# pre-emptive extraction would impose ReAct's repair policy on
# wire formats that may want a different recovery strategy.
```

```python
# prefix_md.py:130-134
# Plugin-internal helpers. Surrogate sanitisation and thinking-block
# stripping are duplicated from react.py rather than shared via a
# common module — keeping each plugin folder-deletable trumps DRY for
# this short helper. If a third plugin appears with the same policy
# we can lift these into a wire_formats common module.
```

**상태:**
- F-002 surrogate/thinking dup → **WITHDRAWN.** "Third plugin" 트리거 조건 미충족 (`ls agent_cli/wire_formats/*.py` → react.py, prefix_md.py 2개).
- F-005 brace scanner dup → **WITHDRAWN.** 동일 정책 적용. 시그니처/정책도 다름 (first vs last).

**Self-check 결과:** 다음 라운드부터 finding 작성 전 같은 모듈 파일 내 *section docstring* (특히 ── 구분자 헤더 직후 주석) 을 의무 검토. 본 라운드에서 system_prompt.py, tools/symbols.py 의 모듈/section docstring 모두 사전 확인.

---

### A-2. F-001 정정 — HookRunner 사용 범위

**원 finding 의 오류 문장:** "`HookRunner` 자체도 패키지 내에서 인스턴스화되지 않음" (audit_report_v1.md F-001 마지막 줄).

**정정:** `HookRunner` 클래스는 `loop.py:189-191, 1057-1060, 1097-1105, 1131-1138, 1456-1462, 1500-1507` 등 Python hooks 디스패치 경로에서 활성. 단, **인스턴스 생성**(`HookRunner(...)`) 호출은 `agent_cli/` 패키지 내 0건 — 모두 `hook_runner=None` 기본값으로 전달됨 (다음 finding F-011 참고).

**살아있는 부분:** `HookRunner` 클래스, `fire()`, `_run_python_hooks()`, `load_python_hooks()` (loader.py).
**Dead 부분 (재확인 F-001 P0 유지):**
- `_run_shell_hooks` 메서드 (runner.py:89-95) — body 가 docstring 뿐인 빈 함수.
- `shell_hooks_config` 파라미터 (runner.py:26) + 필드 (runner.py:29) + dispatch branch (runner.py:75-76).
- 영향 LOC ≈ 12줄.

### A-3. 우선순위 재분류

| Finding | Round 1 | Round 2 (critic 권고 반영) |
|---|---|---|
| F-003 delegate/skill subdir dup | P0 | **P1** |
| F-006 provider streaming consist | P1 | **P2** |
| F-007 json_repair docstring | P1 | **P2** (react.py:31 오독 제거) |
| F-008 _apply_style 비대칭 | P2 | **DROP** (이미 Protocol 통합 완료) |
| F-004 skill wrapper dup | P1 | **P0** (render_group_start 4 호출처 확장) |

---

## B. F-004 강화 분석 — render_group_scope 일반화

### B-1. 4 호출처 패턴 검증

| # | 위치 | label | icon | 시작 LOC | 종료 LOC | duration source |
|---|---|---|---|---|---|---|
| 1 | `main.py:616-651` | `f"skill:{cmd_name}"` | 🪄 | 616-618 | 645-651 | `time.monotonic()-_t0` |
| 2 | `loop.py:1464-1497` | `f"skill:{name}"` | 🪄 | 1464-1466 | 1491-1497 | `time.monotonic()-t0` |
| 3 | `delegate.py:617-622` (parallel replay) | label per task | 🦀 | 617-618 | 621-622 | `durations[i]` (precomputed) |
| 4 | `delegate.py:689-709` (single delegate) | `f"delegate:{agent}"` | 🦀 | 689-691 | 703-709 | `time.monotonic()-t0` |

**호출 #1 발췌 (main.py:616-651):**
```python
616:    render_group_start(f"skill:{cmd_name}", icon="🪄")
617:    render_push_depth()
618:    _t0 = _time.monotonic()
619:    skill_result = None
...
626:    try:
627:        skill_result = execute_skill(
...
644:        )
645:    finally:
646:        render_pop_depth()
647:        render_group_end(
648:            f"skill:{cmd_name}",
649:            success=bool(skill_result and skill_result.success),
650:            duration_s=_time.monotonic() - _t0,
651:        )
```

**호출 #4 발췌 (delegate.py:689-709):**
```python
689:        render_group_start(label, icon="🦀")
690:        render_push_depth()
691:        t0 = time.monotonic()
692:        result = None
693:        try:
694:            result = _run_single(
...
702:            return result
703:        finally:
704:            render_pop_depth()
705:            render_group_end(
706:                label,
707:                success=result.success if result else False,
708:                duration_s=time.monotonic() - t0,
709:            )
```

→ 3/4 호출처가 `start → push → t0 → try → … → finally → pop → end(success, duration)` 으로 *완전 동일 구조*. #3 (parallel replay) 만 precomputed durations 사용하지만 5-line 스켈레톤 동일.

### F-004 (UPGRADE → P0) [ROLE/DUP]

**위치 A:** `main.py:616-651` — `_dispatch_skill` 의 render-group 4단계
**위치 B:** `loop.py:1464-1497` — `_handle_run_skill` 의 render-group 4단계
**위치 C:** `delegate.py:617-622` — `_run_parallel` 의 per-task replay (4단계 mini)
**위치 D:** `delegate.py:689-709` — `tool_delegate` single 분기의 render-group 4단계

**증거 (`grep -n "render_group_start"` 4 hits):**
- `main.py:616`, `loop.py:1464`, `delegate.py:617`, `delegate.py:689` — 검색 결과 매칭 4건 일치.

**권장 (2단계 분리, critic 권고 반영):**
- **(a) [P0]** `render/__init__.py` 에 `render_group_scope(label, icon="", *, duration_callback=None)` *context manager* 추가. 본문 `__enter__` 에서 `render_group_start + render_push_depth + monotonic()`, `__exit__` 에서 `render_pop_depth + render_group_end(success=..., duration_s=...)`. 4 호출처 모두 `with render_group_scope(label, icon="🪄") as scope: ...` 로 단순화.
- **(b) [P1]** skill 자체 wrapper (`invoke_skill_with_render` 등) — main.py 와 loop.py 의 `_dispatch_skill`/`_handle_run_skill` 중복 (F-004 원). ctx.add 정책 차이를 caller 가 결정하는 외부 시그니처 설계. (a) 도입 후 작업 가능.

**예상 LOC 절감:** (a) 만으로 4 호출처 × 평균 7줄 wrap = 28줄 → context manager 1개 (~15줄) + 4 호출처 각 1줄 사용 = 19줄. 약 9줄 절감 + 향후 신규 모니터링 이벤트 추가 시 1곳만 수정.

---

## C. Round 2 깊이 분석 — critic 지정 영역

### F-011 [DEAD] HookRunner 11 이벤트 전체가 사용자 측 활성화 경로 없음

**위치 A (인스턴스화 부재):** `agent_cli/` 패키지 전체
**증거:** `rg -n "HookRunner\(" agent_cli/` → 0건 (tests/ 에만 13건).

**위치 B (모든 fire() 사이트에 None 가드):**
```
loop.py:189-191
189:    def _fire_hook(self, event: str, **kwargs):
190:        if not self.hook_runner:
191:            return self.hook_runner.fire(...)  # unreachable
```
실제 코드 위치는 가드 후 fire — `if self.hook_runner:` 체크 (loop.py:1008, 1057, 1097, 1130, 1456, 1500) 모두 있음.

**위치 C (모든 caller 의 기본값):**
```
grep -n "hook_runner=None" agent_cli/loop.py:
100:        hook_runner=None,    # AgentLoop.__init__
1207:    hook_runner=None,        # run_loop (back-compat wrapper)
1417:    hook_runner=None,        # _handle_run_skill
```
호출처: `main.py::chat`, `main.py::web`, `tools/delegate.py::_run_single` 모두 hook_runner 인자를 *전달하지 않음* (기본 None 적용).

**활성화 경로 추적:**
- `agent_cli/hooks/loader.py:55-78` `load_python_hooks(hook_dirs=None)` — Python hooks 디스크 스캔 기능 있음.
- `agent_cli/hooks/runner.py:23-28` HookRunner.__init__ 가 `load_python_hooks(hook_dirs)` 호출.
- 그러나 `HookRunner(...)` 자체를 패키지에서 호출하는 곳 0건.

→ Python hooks 기능 (11 이벤트) 의 *전체 dispatch path* 가 사용자 측 활성화 경로 없이 dead. 단, 코드 자체는 정상 동작 (테스트로 검증됨) — Phase X 잔재 패턴.

**영향:**
- 인프라 LOC ≈ 95 (runner.py) + 145 (context.py) + 76 (loader.py) + 54 (events.py) = **370 LOC** 가 dead path.
- 사용자 측 hooks 는 `.agent-cli/hooks.json` (shell hooks) 만 사용 — `loop.py:1029-1034, 1139-1150` 의 별도 `from agent_cli.hooks import run_hooks` 경로.

**권장 (3 옵션, critic 의논 필요):**
1. **(a)** main.py/web entrypoint 에서 `HookRunner()` 명시 인스턴스화 + `hook_runner=` 전달 — Phase 2 wiring 완성.
2. **(b)** 11 이벤트 전체 dead 처리 (HookRunner 클래스 / loader.py / context.py / events.py 제거).
3. **(c)** 현 상태 유지 + 명시적 docstring (`AS-IS: not wired in v0.X`) 추가.

**우선순위:** **P0**. 사용자 측 영향 없으나 신규 기여자 혼선이 큼 (테스트만 살아있어 LSP/IDE 가 활성으로 표시). Round 3 에서 사용자 요구 spec 확인 후 결정.

---

### F-012 [DEAD] `convert_to_anthropic_tools` / `convert_to_openai_tools` — native tool-calling API 미사용

**위치 A:** `agent_cli/tools/registry.py:368-392`
```python
368:def convert_to_anthropic_tools(tool_names: list[str]) -> list[dict]:
369:    """Convert tool schemas to Anthropic API tool format."""
370:    return _convert_tools(
371:        tool_names,
372:        lambda s: {
373:            "name": s.name,
374:            "description": s.description,
375:            "input_schema": s.parameters,
376:        },
377:    )
378:
379:
380:def convert_to_openai_tools(tool_names: list[str]) -> list[dict]:
381:    """Convert tool schemas to OpenAI API tool format."""
382:    return _convert_tools(
383:        tool_names,
384:        lambda s: {
385:            "type": "function",
386:            "function": {
387:                "name": s.name,
388:                "description": s.description,
389:                "parameters": s.parameters,
390:            },
391:        },
391:    )
```

**증거:**
- `rg -n "convert_to_anthropic_tools|convert_to_openai_tools" agent_cli/` → 0 hits (자기 정의 외).
- `rg -n "convert_to_anthropic_tools|convert_to_openai_tools" tests/` → 4 hits (`tests/test_registry.py:7,8,112,123,131`). 테스트만 keep-alive.
- 모듈 docstring `providers/ollama.py:13-16` 명시: "the ReAct JSON we need is handled robustly by the 3-stage parser ... Keeping the surface consistent with the OpenAI-compat provider, which also uses basic JSON mode."
- `providers/compat.py:118-122` ModelCapabilities: "Legacy field `supports_tool_calling` — silently ignored if present in older models.json entries; the loop uses ReAct text parsing, not the native tool-calling API on any provider."

→ Native tool-calling API 채택 거부 (CONFIRMED design decision). 두 변환 함수는 그 결정 이전의 잔재.

**우선순위:** **P1**. F-001 과 동일 패턴 (test-only keep-alive). 영향 LOC ≈ 60 (양 함수 + `_convert_tools` + 테스트 일부). 신규 provider 추가 시 native tool-calling 경로를 잘못 도입할 위험.

**권장:** 두 변환 함수 + `_convert_tools` 헬퍼 + `_ALWAYS_INCLUDE` (registry.py:351) 제거, test_registry.py 의 해당 케이스 제거. `validate_tool_input`, `get_tool_descriptions`, `TOOL_SCHEMAS` 만 유지.

---

### F-013 [DUP] `build_agent_descriptions` vs `build_skill_descriptions` — 95% 동일 패턴

**위치 A:** `agent_cli/prompts/system_prompt.py:560-607` (build_agent_descriptions)
**위치 B:** `agent_cli/prompts/system_prompt.py:610-658` (build_skill_descriptions)

**diff 결과 (sed 추출):**
```
1c1
<     if not agents:
---
>     if not skills:
...
6,7c9,10
<         action="delegate",
<         action_input='{"tasks": ...}',
---
>         action="run_skill",
>         action_input='{"name": ...}',
...
17,19c19,27
<     for name, desc in agents:
<         suffix = f" — {desc}" if desc else ""
<         lines.append(f"- `{name}`{suffix}")
---
>     for skill in skills.values():
>         if skill.disable_model_invocation:
>             continue
>         hint = f" {skill.argument_hint}" if skill.argument_hint else ""
>         lines.append(f"- `{skill.name}{hint}` — {skill.description}")
```

**공통 스켈레톤 (양쪽 모두):**
1. `wire_format = _get_wire_format("react")` if None
2. 데이터 소스 load (delegate `_agent_loader.load_all()` vs `load_skills()`)
3. 빈 list 면 `return ""`
4. `wire_format.render_full_example(thought=None, action=..., action_input=...)`
5. `"\n".join(f"  {line}" for line in example.splitlines())` (indent)
6. `lines = [header(s), indented]`
7. for-loop 으로 `f"- \`{name}\`{suffix}" 추가
8. `"\n".join(lines)`

**위치 A 발췌 (lines 590-605, 핵심):**
```python
590:    example = wire_format.render_full_example(
591:        thought=None,
592:        action="delegate",
593:        action_input='{"tasks": [{"task": "...", "agent": "agent-name", "context": "fork"}]}',
594:    )
595:    # Indent every line so multi-line wire shapes (e.g. markdown
596:    # section headers) keep their structure inside the bulleted list.
597:    indented = "\n".join(f"  {line}" for line in example.splitlines())
598:    lines = [
599:        "## Available Agents",
600:        "Consider delegating parallelizable or independent subtasks to agents.",
601:        indented,
602:    ]
603:    for name, desc in agents:
604:        suffix = f" — {desc}" if desc else ""
605:        lines.append(f"- `{name}`{suffix}")
```

**위치 B 발췌 (lines 636-652, 핵심):**
```python
636:    example = wire_format.render_full_example(
637:        thought=None,
638:        action="run_skill",
639:        action_input='{"name": "skill-name", "arguments": "..."}',
640:    )
641:    indented = "\n".join(f"  {line}" for line in example.splitlines())
642:    lines = [
643:        "## Available Skills",
644:        "Consider using skills for multi-step or specialized workflows.",
645:        "Use the run_skill tool to invoke:",
646:        indented,
647:    ]
648:    for skill in skills.values():
649:        if skill.disable_model_invocation:
650:            continue
651:        hint = f" {skill.argument_hint}" if skill.argument_hint else ""
652:        lines.append(f"- `{skill.name}{hint}` — {skill.description}")
```

**차이점:**
- header lines 갯수: agent 는 2줄, skill 은 3줄 ("Use the run_skill tool to invoke:" 추가).
- item rendering 함수가 다름 (description vs hint+description, disable_model_invocation 필터).
- skill 만 `len(lines) <= 2` empty 가드 (654-656).

**우선순위 평가:**
- LOC: 두 함수 합 ~100줄. helper 도입 시 ~60줄 (공통 40줄 절감).
- 변경 빈도: wire format plugin 추가 시 두 함수 모두 영향 (이미 `render_full_example` 추상화 도입됨). 단, 새 *invocation type* (agent/skill 외) 추가 가능성 낮음.
- "folder-deletable" 정책: prompts/ 디렉터리 내부 (wire_formats 와 무관) — 동일 정책 적용 대상 아님.

→ **P1** (P0 까지는 과대 — 변경 빈도 낮음).

**권장:** `_build_invocation_section(*, header_lines, example_args, items, item_renderer)` 헬퍼 추출. 2 호출처가 데이터·라벨만 전달.

---

### F-014 [DEAD] `Renderer._captures` / `_thread_status` API 의 web 미사용

**위치 A (구현 in base):** `agent_cli/render/base.py:46-105` (`start_capture`, `stop_capture`, `_capture_line`, `set_thread_status`, `get_thread_status` 5 API)

**위치 B (활성 호출처):**
- `agent_cli/render/minimal.py:235` — `self.set_thread_status(...)` (thought 시)
- `agent_cli/render/minimal.py:182` — `self._capture_line(clean)` (모든 `_p` 호출)
- `agent_cli/tools/delegate.py:568` — `renderer.get_thread_status(...)` (parallel Live panel)
- `agent_cli/tools/delegate.py:506` — `render_start_capture()` (parallel worker entry)
- `agent_cli/tools/delegate.py:534` — `render_stop_capture()` (parallel worker exit)

**위치 C (web 미사용):**
- `agent_cli/render/web.py` — `set_thread_status` 0 호출, `_capture_line` 0 호출, `get_thread_status` override 없음.
- `agent_cli/render/web.py:444-447` `group_start`, `group_end` 만 override (no-op pass).

**증거:** `grep -n "_capture_line\|set_thread_status\|get_thread_status\|start_capture\|stop_capture" agent_cli/render/web.py` → 0 hits.

→ Capture API 는 parallel delegate (rich `Live` panel + per-thread status) 전용. Web renderer 사용 시 *parallel delegate 만 호출되는 경로* 가 dead 동작 (Live panel 안 보이고 thread_status 미수집).

**Q: 이건 진짜 DEAD 인가, 의도된 ROLE 분리인가?**
- web renderer 가 parallel delegate 를 호출하면 worker 들이 `render_start_capture()` 호출 → `_captures[tid] = []` 가 web.py 미override 시 base 기본 동작 → capture 됨. 그러나 capture 결과는 `render_replay_captured()` 로 *console.print* 됨 (`render/__init__.py:156-162`), web 의 SSE 와 무관.
- → web 에서 parallel delegate 사용 시 표시 *누락* (Live panel + captured replay 모두 console-only).

**우선순위:** **P1**. CLI/web 비대칭이 사용자 측 기능 누락. 의도된 limitation 이면 web renderer 에 명시적 stub override + 사용자 메시지 ("parallel delegate currently CLI-only") 필요.

**권장:** Round 3 에서 web 의 parallel delegate 동작 실측 확인 후 결정. 옵션:
- (a) web 에 `dispatch_progress` 활용한 mini-status emit.
- (b) capture API 자체를 web 도 활용하도록 abstract 화.
- (c) 명시적 limitation 문서화.

---

### F-015 [PERF/ROLE] `tools/symbols.py` 785 LOC — 5 언어 extractor 가 단일 파일에 적재

**위치:** `agent_cli/tools/symbols.py:111-587`

**구조 분석:**
| 영역 | 함수 | LOC 범위 |
|---|---|---|
| Language map + 헬퍼 | `_detect_language`, `get_supported_extensions`, `_unsupported_ext_msg`, `_ts_lines`, `_node_text`, `_get_field` | 25-108 |
| Python extractor | `_extract_python` | 111-168 (58줄) |
| JS/TS extractor | `_extract_js` | 172-261 (90줄) |
| C/C++ extractor | `_cpp_declarator_name` + `_extract_cpp` + `_has_function_declarator` | 265-540 (276줄) |
| Markdown extractor | `_extract_markdown` | 542-587 (46줄) |
| Dispatcher + I/O | `_EXTRACTORS`, `_parse`, `_extract`, `_resolve_path_and_language` | 591-664 |
| Public API | `_do_list`, `_do_fetch`, `tool_read_symbols` | 667-785 |

**관찰:**
- 단일 파일 단일 책임 위배 — 5 언어 extractor 가 각각 *완전 독립* 모듈처럼 동작 (공통 helper `_ts_lines`, `_node_text`, `_get_field` 만 공유).
- C/C++ extractor 가 LOC 276 (전체의 35%) — `_cpp_declarator_name` 의 재귀 분기가 가장 큼.
- `_parse()` 함수 (601-640) 가 `if language == "python": import tree_sitter_python` 식의 분기 — 새 언어 추가 시 *이 함수 한 줄* 과 *_EXTRACTORS dict 한 줄* + extractor 함수 본문이 모두 수정 대상.

**중복 패턴 (extractor 간):**
- 모든 extractor 의 `visit(node, prefix)` 재귀 구조 동일.
- `name_node = _get_field(node, "name"); base = name_node.text.decode(...); full = f"{prefix}.{base}" if prefix else base; start, end = _ts_lines(node); symbols.append(Symbol(...))` 패턴이 python, js, cpp 모두에서 반복 (각 ~10줄).

**우선순위:** **P2**. 단일 파일이지만 *명확한 모듈 책임* — extractor 자체는 언어별 격리됨. Refactor 트리거는 (a) 새 언어 추가 (Rust, Go 등) 시점, (b) tree-sitter 0.x → 1.x 마이그레이션 시점 모두 자연스러운 split 지점.

**권장:** Round 3 에서 더 깊이 보지 않음. 새 언어 추가 시 `tools/symbols/_python.py`, `_js.py`, `_cpp.py`, `_markdown.py` 로 분리하는 PR 권장.

---

### F-016 [CONSIST] tools/context.py `_parse_loc` ↔ search match 의 `loc` 포맷 불일치

**위치 A (생성):** `agent_cli/tools/context.py:146-153` (`_mode_search` 의 match append)
```python
146:                        matches.append(
147:                            {
148:                                "loc": f"{session_id}/{rel_path}:{line_num}",
149:                                "role": msg.get("role", "?"),
...
```
즉 `loc` 포맷은 `{session_id}/{rel_path}:{line_num}` (한 줄에 한 번의 `:` 가정).

**위치 B (파싱):** `agent_cli/tools/context.py:477-502` (`_parse_loc`)
```python
484:    if ":" not in loc:
485:        raise ValueError(f"loc must end with ':<line_num>', got {loc!r}")
486:    main, _, line_part = loc.rpartition(":")
...
495:    if "/" not in main:
496:        raise ValueError(...)
499:    session_id, _, rel_path = main.partition("/")
```
`rpartition(":")` 사용 — POSIX 경로에 `:` 가 들어가도 마지막 `:` 가 line_num 구분자임을 가정. 단 *Windows-mounted shares* 케이스 docstring 에만 있음.

**문제:** `_mode_search` 생성 측은 단순 `f"...:{line_num}"` — `rel_path` 에 콜론 포함되면 fetch 측이 잘못 partition. 실측 케이스는 거의 없으나 `session_id` 자체가 `2026-05-22:xxx` 형식이면 파싱 오작동.

→ POSIX-only 가정. Windows 미지원은 docstring 에 명시되어 있으니 의도된 동작.

**확인 (`grep "session_id" context/session.py`):** session_id 생성은 `create_session()` 의 `time.strftime` + `uuid` 기반 (콜론 미포함). 실제 위험은 사용자가 직접 만든 디렉터리 (커스텀 backup) 정도.

**우선순위:** **P2** (실측 영향 거의 없음). 단 critic 권장 대로 더 robust 한 분리자 (`#L<num>` 등) 검토 가능.

**권장:** 정보성. Round 3 에서 우선순위 낮음 — drop 가능.

---

## D. Round 2 종합

### D-1. 정정 후 finding 표 (재정렬)

| ID | 카테고리 | 우선순위 | 상태 |
|---|---|---|---|
| F-001 | DEAD | P0 | 정정 (범위 한정) |
| F-002 | DUP | — | **WITHDRAWN** |
| F-003 | DUP | P1 | downgrade (Round 1 → Round 2) |
| F-004 | ROLE | P0 | **UPGRADE** + render_group_scope 추출 권장 |
| F-005 | DUP | — | **WITHDRAWN** |
| F-006 | CONSIST | P2 | downgrade |
| F-007 | CONSIST | P2 | downgrade (react.py:31 오독 제거) |
| F-008 | — | — | **DROP** (이미 Protocol 통합 완료) |
| F-009 | ROLE | P2 | 유지 |
| F-010 | PERF | P2 | 유지 |
| **F-011** | DEAD | P0 | **NEW** — HookRunner 11 이벤트 전체 인스턴스화 부재 (~370 LOC dead path) |
| **F-012** | DEAD | P1 | **NEW** — convert_to_*_tools 변환 함수 미사용 (test-only) |
| **F-013** | DUP | P1 | **NEW** — build_agent/skill_descriptions 95% 중복 |
| **F-014** | DEAD/ROLE | P1 | **NEW** — web renderer 의 capture API 미사용 (parallel delegate 표시 누락 가능성) |
| **F-015** | PERF/ROLE | P2 | **NEW** — symbols.py 785 LOC 단일 파일 |
| **F-016** | CONSIST | P2 | **NEW** — context.py loc 포맷 가정 |

### D-2. 최종 P0 (3건)

1. **F-001** — `_run_shell_hooks` + `shell_hooks_config` 제거 (~12 LOC).
2. **F-004** — `render_group_scope` context manager 추출 (~30 LOC 영향, 4 호출처 단순화).
3. **F-011** — HookRunner Phase X 잔재 (~370 LOC) — Round 3 에서 사용자 결정 필요 (wire 완성 vs 제거 vs 명시 문서화).

### D-3. 다음 라운드 (Round 3) 우선순위 제안

critic 권고 영역 + Round 2 신규 발견 영역:

1. **F-011 의 사용자 spec 확인** — Python hooks 의 활성화 의도 (Phase 2 wiring 예정인지, 아예 제거할지) — 사용자 결정 필요.
2. **F-014 의 web parallel delegate 실측** — 실제 동작 확인 (Live panel + captured replay 가 web 에서 어떻게 나오는지).
3. **`web/server.py`** (488 LOC) 의 SSE 이벤트 매핑 — `WebRenderer` 와 `handle_slash_command` 의 책임 경계.
4. **`tools/registry.py`** 의 `_convert_tools` + 변환 함수 dead 처리 후 Tool dispatch 경로 (TOOLS dict) 의 모듈화 검증.
5. **`recovery/`** 디렉터리 — `observability.py`, `intervention.py`, `primitives.py` 의 책임 분담 + 사용 빈도.

---

## E. Methodology 자가 점검

본 라운드부터 finding 작성 전 다음 점검:
- ✅ 같은 파일 내 ── 헤더 직후 *section docstring* 검토 (F-002/F-005 오류 방지).
- ✅ `rg` 로 *call sites + definition + tests* 3-way 교차 검증 (F-001 정정 + F-011/F-012 발견).
- ✅ 비교 finding 은 양쪽 발췌 모두 라인 번호 prefix 로 인용.
- ✅ critic 권고 우선순위 재분류 적용.
- ⚠ Round 1 F-008 의 "다음 라운드에서 검토" 미루기 패턴 본 라운드 제거 — 본 라운드에서 즉시 DROP 결정.
