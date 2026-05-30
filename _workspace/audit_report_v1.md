# Audit Report — Round 1

**Scope:** agent_cli/ 전체 surface 스캔 (5개 카테고리: DUP / ROLE / DEAD / PERF / CONSIST)
**Auditor:** auditor (audit-loop team)
**Date:** 2026-05-22

---

## 측정 (Codebase Surface)

| Metric | Value | Source |
|---|---|---|
| Python files in `agent_cli/` | 70 | `find agent_cli -name '*.py' \| wc -l` |
| Total LOC | 16,569 | `find ... -exec wc -l \| awk '{sum+=$1}'` |
| `def`/`class` lines (incl. nested) | 605 | `grep -rn '^def \|^    def \|^class '` |
| Top-level `def`/`class` | 320 | `grep -rn '^def \|^class '` |

**Top hotspots (LOC):**

| File | LOC |
|---|---|
| `agent_cli/main.py` | 1682 |
| `agent_cli/loop.py` | 1655 |
| `agent_cli/tools/symbols.py` | 785 |
| `agent_cli/tools/delegate.py` | 712 |
| `agent_cli/prompts/system_prompt.py` | 658 |
| `agent_cli/wire_formats/react.py` | 655 |
| `agent_cli/render/minimal.py` | 602 |
| `agent_cli/render/web.py` | 601 |
| `agent_cli/tools/context.py` | 574 |
| `agent_cli/tools/registry.py` | 563 |

---

## P0 (즉시 처리 권장)

### F-001 [DEAD] `HookRunner._run_shell_hooks` 와 dispatch branch 가 unreachable

**위치 A:** `agent_cli/hooks/runner.py:89-95`
```python
89:    def _run_shell_hooks(self, event: str, ctx: HookContext) -> None:
90:        """Execute shell hooks via the legacy hooks module.
91:
92:        Will be wired in Phase 2 when we integrate with loop.py.
93:        For now, shell hooks continue to work through the existing
94:        hooks.py run_hooks() call in loop.py.
95:        """
```

**위치 B:** `agent_cli/hooks/runner.py:23-29, 75-76`
```python
23:    def __init__(
24:        self,
25:        hook_dirs: list[Path] | None = None,
26:        shell_hooks_config: dict | None = None,
27:    ):
28:        self._python_hooks: dict[str, list[Callable]] = load_python_hooks(hook_dirs)
29:        self._shell_hooks_config = shell_hooks_config
...
75:        if event in (PRE_TOOL_USE, POST_TOOL_USE) and self._shell_hooks_config:
76:            self._run_shell_hooks(event, ctx)
```

**증거:**
- `grep -rn "shell_hooks_config" agent_cli/` → only `runner.py` 자기 자신 (3 hits)
- `grep -rn "HookRunner(" agent_cli/` → 0 hits in package (only tests/test_hooks_python.py 인스턴스 5건)
- `grep -n "load_python_hooks\|hook_runner=" agent_cli/loop.py` → 모든 호출처가 `hook_runner=None` 기본값
- 실제 shell hooks 는 `loop.py:1029-1034, 1139-1150` 에서 `from agent_cli.hooks import run_hooks` 로 별도 dispatch

→ method body 는 docstring 만 있고 빈 함수. `shell_hooks_config` 파라미터·필드·dispatch branch 가 전부 dead code. `HookRunner` 자체도 패키지 내에서 인스턴스화되지 않음 (Phase 2 미완료 추정).

**권장:** `_run_shell_hooks`, `shell_hooks_config` 파라미터, dispatch branch 제거. Phase 2 인테그레이션이 필요하면 별도 task 로 분리.

---

### F-002 [DUP] `_sanitize_surrogates` + `_strip_thinking_blocks` + `_THINKING_PATTERN` 가 두 wire format 에 중복

**위치 A:** `agent_cli/wire_formats/react.py:41-73`
```python
41:# Build regex that matches any of the known thinking tags
42:_THINKING_PATTERN = re.compile(
43:    r"<(" + "|".join(_THINKING_TAGS) + r")>(.*?)</\1>",
44:    re.S | re.I,
45:)
46:
47:
48:def _sanitize_surrogates(text: str) -> str:
49:    """Remove unpaired Unicode surrogates that break JSON parsing."""
50:    return re.sub(r"[\ud800-\udfff]", "", text)
51:
52:
53:def _strip_thinking_blocks(text: str) -> tuple[str, str | None]:
54:    """Strip thinking/reasoning blocks from LLM output.
...
61:    thinking_parts: list[str] = []
62:
63:    def _collect(match):
64:        content = match.group(2).strip()
65:        if content:
66:            thinking_parts.append(content)
67:        return ""
68:
69:    cleaned = _THINKING_PATTERN.sub(_collect, text).strip()
70:
71:    if thinking_parts:
72:        return cleaned, "\n\n".join(thinking_parts)
73:    return text, None
```

**위치 B:** `agent_cli/wire_formats/prefix_md.py:130-175` (요지)
```python
156:def _sanitize_surrogates(text: str) -> str:
157:    """Remove unpaired Unicode surrogates that break JSON parsing."""
158:    return re.sub(r"[\ud800-\udfff]", "", text)
159:
160:
161:def _strip_thinking_blocks(text: str) -> tuple[str, str | None]:
162:    """Strip ``<think>`` / ``<reasoning>`` style blocks, returning the
163:    cleaned text plus the joined thinking content (or ``None``)."""
164:    parts: list[str] = []
165:
166:    def _collect(match: re.Match) -> str:
167:        content = match.group(2).strip()
168:        if content:
169:            parts.append(content)
170:        return ""
171:
172:    cleaned = _THINKING_PATTERN.sub(_collect, text).strip()
173:    if parts:
174:        return cleaned, "\n\n".join(parts)
175:    return text, None
```

**증거:**
- `diff` 결과: 두 함수는 docstring/변수명(`thinking_parts` vs `parts`)·`match` 타입 어노테이션 차이 외 본문 동일.
- `_THINKING_PATTERN` 도 두 모듈에 동일 컴파일.

→ 새로운 wire format plugin 이 추가될 때마다 surrogate sanitize 와 thinking strip 로직을 중복 정의해야 함. 한쪽만 수정될 때 silent drift 위험.

**권장:** `wire_formats/_text_preprocess.py` (또는 `base.py`) 에 공통 모듈로 추출 후 두 plugin 모두 import.

---

### F-003 [DUP] Delegate / Skill 디렉터리 명 + result.md persist 패턴 동일

**위치 A:** `agent_cli/tools/delegate.py:192-200` (`_generate_delegate_dir_name`)
```python
192:def _generate_delegate_dir_name(agent_name: str) -> str:
193:    """Generate a unique delegate directory name: delegate_{name}_{hash}_{ts}"""
194:    import os
195:
196:    name = agent_name or "task"
197:    hash_part = os.urandom(3).hex()  # 6-char hex
198:    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
199:    ms = f"{int(time.time() * 1000) % 1000:03d}"
200:    return f"delegate_{name}_{hash_part}_{ts}{ms}"
```

**위치 B:** `agent_cli/skills/executor.py:153-167`
```python
153:    if ctx:
154:        import os
155:        import time as _time
156:
157:        name = skill.name or "skill"
158:        hash_part = os.urandom(3).hex()
159:        ts = _time.strftime("%Y%m%dT%H%M%S", _time.gmtime())
160:        ms = f"{int(_time.time() * 1000) % 1000:03d}"
161:        skill_dir_name = f"skill_{name}_{hash_part}_{ts}{ms}"
162:        skill_session_dir = ctx.session_dir / skill_dir_name
163:        skill_ctx = ContextManager(
164:            session_dir=skill_session_dir,
165:            max_context_tokens=ctx.max_context_tokens,
166:            wire_format=ctx.wire_format,
167:        )
```

**추가 증거 (`_persist_delegate_result` vs executor.py `result.md` write):**
- `agent_cli/tools/delegate.py:212-222` (try/mkdir/write/except pass)
- `agent_cli/skills/executor.py:198-204` (try/write/except pass) — 동일 패턴

`grep -rn "os.urandom(3).hex" agent_cli/` → 2 hits (delegate.py:197, executor.py:158).
`grep -rn "time.strftime.*Y.*m.*d" agent_cli/` → 2 hits in identical pattern.

→ "subagent 디렉터리 + result.md persist" 정책이 두 곳에 사실상 복제. dir name 포맷 변경 시 양쪽 동기화 필요.

**권장:** `context/session.py` 또는 신규 helper (`make_subagent_dir(prefix, name, parent_dir)` + `persist_result(dir, text)`) 로 단일화.

---

## P1 (중요, 다음 라운드 검토)

### F-004 [ROLE] `_dispatch_skill` (main.py) 와 `_handle_run_skill` (loop.py) 가 거의 동일한 wrapping 을 수행

**위치 A:** `agent_cli/main.py:570-667` (`_dispatch_skill`)
**위치 B:** `agent_cli/loop.py:1404-1518` (`_handle_run_skill`)

**공통 패턴 (양쪽 모두 수행):**
1. `load_skills()` → 스킬 존재 확인
2. `disable_model_invocation` 체크 (B는, A는 항상 user_invocable 가정)
3. `render_group_start(f"skill:{name}")` + `render_push_depth()` + `time.monotonic()` 시작
4. `execute_skill(...)` 호출 (provider/capabilities/model 전부 전달)
5. `render_pop_depth()` + `render_group_end(...)` (`finally`)
6. result 가 None 이면 fallback 안내
7. `ctx.add({"role": "user", "tool": "run_skill", ...})` 패턴

**위치 B 발췌 (loop.py:1456-1497):**
```python
1456:    if hook_runner:
1457:        hook_runner.fire(
1458:            "OnSkillStart",
...
1464:    render_group_start(f"skill:{name}", icon="🪄")
1465:    render_push_depth()
1466:    t0 = time.monotonic()
1467:
1468:    try:
1469:        from agent_cli.providers import create_provider
...
1487:        )
1488:    except Exception as e:
1489:        _debug_log(f"run_skill({name}) exception: {e}")
1490:        skill_result = ToolResult(False, error=f"run_skill({name}) failed: {e}")
1491:    finally:
1492:        render_pop_depth()
1493:        render_group_end(
1494:            f"skill:{name}",
1495:            success=skill_result.success if skill_result else False,
1496:            duration_s=time.monotonic() - t0,
1497:        )
```

**위치 A 발췌 (main.py:616-651):**
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

→ 두 함수 모두 "스킬 호출 wrapper" 라는 같은 역할 수행. 차이점: A는 user/REPL 진입점(`/<skill>`)·B는 모델 진입점(`run_skill` action). 그러나 render group + ctx.add + skill 호출의 핵심 단계가 모두 중복.

**권장:** `skills/executor.py` 에 `invoke_skill_with_render(...)` 라는 단일 entry 추가, 두 호출처가 import. observation framing(`SKILL: {name}({args})` header)만 호출처에서 결정.

---

### F-005 [DUP] Brace-counting JSON 블록 스캐너가 두 wire format 에 별도 구현

**위치 A:** `agent_cli/wire_formats/react.py:516-553` (`_extract_json_block`)
```python
516:def _extract_json_block(text: str) -> str:
517:    """Find the outermost { ... } block in the text."""
518:    text = strip_markdown_fences(text)
519:
520:    start = text.find("{")
521:    if start == -1:
522:        return text
523:
524:    depth = 0
525:    in_string = False
526:    escape_next = False
527:    last_close = -1
528:
529:    for i in range(start, len(text)):
530:        ch = text[i]
531:        if escape_next:
532:            escape_next = False
533:            continue
534:        if ch == "\\":
535:            if in_string:
536:                escape_next = True
537:            continue
538:        if ch == '"':
539:            in_string = not in_string
...
549:                return text[start : i + 1]
```

**위치 B:** `agent_cli/wire_formats/prefix_md.py:178-213` (`_find_last_json_block`)
```python
178:def _find_last_json_block(text: str) -> tuple[int, int] | None:
179:    """Find the last balanced top-level ``{...}`` block.
...
188:    last: tuple[int, int] | None = None
189:    depth = 0
190:    start = -1
191:    in_str = False
192:    escape = False
193:    for i, ch in enumerate(text):
194:        if in_str:
195:            if escape:
196:                escape = False
197:            elif ch == "\\":
198:                escape = True
199:            elif ch == '"':
200:                in_str = False
201:            continue
202:        if ch == '"':
203:            in_str = True
...
212:                    last = (start, i + 1)
213:    return last
```

→ 두 스캐너 모두 "JSON 문자열 내부의 brace 무시" 알고리즘을 직접 구현. 차이점: A는 첫 outermost(`first complete or last close`), B는 last balanced. 백슬래시 이스케이프 처리 디테일도 미묘하게 다름 (A는 string 외부 `\\`도 escape 마킹, B는 string 내부에서만).

**증거 (`grep -rn "depth = 0" agent_cli/wire_formats/`):** 2 hits at react.py:524, prefix_md.py:189; `in_string`/`in_str` 변수명까지 분리.

**권장:** `wire_formats/_text_preprocess.py` 에 `find_brace_blocks(text)` 헬퍼 추출. 두 호출처가 first/last/all 정책만 결정.

---

### F-006 [CONSIST] Provider 3종의 streaming/parsing 추상 미흡 — TTFT/usage 계산이 미세하게 다름

**위치 A:** `agent_cli/providers/ollama.py:94-148` (`_handle_stream`)
**위치 B:** `agent_cli/providers/openai_compat.py:80-151` (`_handle_stream`)
**위치 C:** `agent_cli/providers/anthropic.py:87-171` (`_handle_stream`)

**일관성 이슈:**
| Aspect | Ollama | OpenAI-compat | Anthropic |
|---|---|---|---|
| TTFT measurement | ✅ `t_first` on first content chunk | ✅ 동일 | ✅ 동일 |
| `prompt_eval_ns` source | `final_data.get("prompt_eval_duration")` (server time) | `ttft_ns` (client time) | `ttft_ns` (client time) |
| `eval_ns` source | `final_data.get("eval_duration")` (server) | `decode_ns` (client) | `decode_ns` (client) |
| thinking accumulation | `msg.get("thinking", "")` | `delta.get("reasoning_content", "")` | `delta_type == "thinking_delta"` |
| cache tokens | none | none | `cache_creation/read_input_tokens` (only Anthropic) |
| error mid-stream | ✅ `RuntimeError(f"Ollama streaming error: ...")` | ❌ raw JSONDecodeError can fall through | ❌ no error key check |

**증거:** Ollama `_handle_stream` 라인 113-114:
```python
113:            if "error" in data:
114:                raise RuntimeError(f"Ollama streaming error: {data['error']}")
```
OpenAI-compat 동일 위치(라인 101-102)에는 `data = json.loads(payload)` 만 있고 error 키 검사 없음.

→ Same shape (`_handle_stream` → `LLMResponse`) 인데 보호 수준이 다름. 미래에 vLLM/LM Studio 도 mid-stream error 노출하면 silent corruption 가능.

**권장:** `providers/base.py` 또는 `providers/http.py` 에 공통 stream 헬퍼 (또는 mid-stream error 검사 규칙) 정의 후 3 provider 모두 적용. 최소한 OpenAI-compat 에 `if "error" in data: raise` 패턴 추가.

---

### F-007 [DEAD] `wire_formats/react.py` parse_stage 주석 — `json_repair` 모듈 표현이 잘못

**위치:** `agent_cli/wire_formats/react.py:31` (모듈 docstring) + `agent_cli/recovery/observability.py:62`
```python
# observability.py:62
parse_stage: int  # 0=fail, 1=json.loads, 2=json_repair, 3=regex
```
```python
# react.py:31 (모듈 docstring)
# ``json_repair`` module) so the whole ReAct format — parser,
```

**증거:** 프로젝트 내부 함수는 `repair_json` (react.py:485) 이며 외부 `json_repair` 모듈을 의존하지 않음 (`grep -rn "import json_repair\|from json_repair" agent_cli/` → 0 hits, `pyproject.toml` 에도 미참조).

→ 외부 라이브러리에서 자체 구현으로 마이그레이션된 흔적이지만 주석/docstring 이 외부 모듈 명을 그대로 사용. 신규 기여자에게 혼선.

**권장:** 두 곳 모두 `repair_json` 으로 통일하거나 `our repair_json fallback` 명시.

---

## P2 (참고 — 우선순위 낮음)

### F-008 [DUP] `_apply_style` 호출 중복 — `chat` 진입점 외에는 환경 의존 분기 미흡

**위치:** `agent_cli/main.py:241-273` (`_apply_style`), `chat` 진입점은 호출하지만 `web` 진입점(`main.py:1416-1473`)은 미호출 — 대신 `WebRenderer` 를 명시적으로 `set_renderer` (`main.py:1548-1549`). 두 진입점의 renderer 결정 경로가 비대칭.

**권장:** Round 2 에서 main.py `chat` vs `web` 의 setup 단계를 비교, 공통 setup 부분 (`_setup_provider`, session/ctx init, hooks load) 추출 가능성 검토.

---

### F-009 [ROLE] `agent_cli/main.py::_collect_agent_names` 와 `agent_cli/prompts/system_prompt.py::build_agent_descriptions` 의 디스커버리 경로 분기

**위치 A:** `agent_cli/main.py:366-386`
```python
366:def _collect_agent_names() -> list[str]:
...
377:    for search_dir in _AGENT_SEARCH_PATHS:
378:        if not search_dir.is_dir():
379:            continue
380:        for md_file in sorted(search_dir.glob("*.md")):
```

**위치 B:** `agent_cli/prompts/system_prompt.py:577-584`
```python
577:    try:
578:        from agent_cli.tools.delegate import _agent_loader
579:    except ImportError:
580:        return ""
581:
582:    resources = _agent_loader.load_all()
```

→ Listing 용도(B는 ResourceLoader 사용)와 prompt 빌드 용도(A는 직접 glob)가 동일 search path 를 다르게 순회. dedup 정책도 분리 구현. 미래에 `_AGENT_SEARCH_PATHS` 가 변경되면 두 경로 모두 영향.

**권장:** Round 2 에서 `_agent_loader.list_names()` 등의 단일 API 로 통일 검토.

---

### F-010 [PERF] `loop.py::_handle_text_path` 의 분기 깊이 — 단일 메서드 460 LOC 동안 11+ 분기

**위치:** `agent_cli/loop.py:450-953` (`_handle_text_path` + `_dispatch_text_path`)

`grep -c "^        if " agent_cli/loop.py` (top-level indent in method) → 다수의 if-cascade. parse_stage 분기, ask/run_skill/ready_for_review/complete special-casing 이 모두 한 함수에 적재. P0 는 아니지만 새 wire format/special action 추가 시 핫스팟.

**권장:** Round 3-4 에서 special action dispatcher table 도입 검토.

---

## 종합

- **P0 finding 3건**: 명백한 dead code (HookRunner Phase 2 잔재), 두 wire format 모듈의 텍스트 전처리 중복, delegate/skill subdir 생성 패턴 중복.
- **P1 finding 4건**: skill wrapper 역할 중복, brace scanner 중복, provider streaming 일관성, 잘못된 docstring 참조.
- **P2 finding 3건**: CLI 진입점 비대칭, 에이전트 디스커버리 경로 분기, loop.py 단일 메서드 hot-spot.

**측정 한계:** 본 라운드는 grep + Read 기반 surface 스캔. Round 2 에서 critic 지정 영역 (특히 P0 finding 의 실제 호출 그래프, render/* 모듈, prompts/system_prompt.py 의 빌더 중복) 을 깊이 분석 예정.

**다음 라운드 제안 영역 (auditor 자체 판단):**
1. `agent_cli/loop.py` + `agent_cli/main.py` 의 ROLE 중복 풀 분석 (REPL ↔ web ↔ skill 진입점)
2. `wire_formats/` 의 base ABC vs plugin override 일관성 매핑
3. `render/` minimal vs web 의 method 표 — 누락된 override 와 dead capture/depth API 식별
