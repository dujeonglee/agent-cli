# Audit Feedback — Round 1 (critic)

**Critic:** critic (audit-loop team)
**Reviewing:** `_workspace/audit_report_v1.md`
**Date:** 2026-05-22
**Methodology:** 모든 finding 에 대해 Read/Grep 본인 재실행 (Q1 라인 검증, Q2 인과·우회, Q3 일반화, Q4 우선순위).

---

## 종합 평가

| Finding | Auditor | Critic 결론 |
|---|---|---|
| F-001 HookRunner Phase 2 잔재 | P0 DEAD | **CONFIRMED** P0 (P1 가능) — 단 권장 범위 조정 필요 |
| F-002 surrogate/thinking dup | P0 DUP | **FALSE POSITIVE** — 의도된 중복, docstring 정책 명시됨 |
| F-003 delegate/skill subdir | P0 DUP | **DOWNGRADE → P1** — 진짜 dup이나 LOC 작고 영향 좁음 |
| F-004 skill wrapper dup | P1 ROLE | **UPGRADE → P0 후보** — 일반화 범위 누락 (delegate 까지 4 호출처) |
| F-005 brace scanner dup | P1 DUP | **FALSE POSITIVE** — F-002 와 동일 정책 적용, 의도됨 |
| F-006 provider streaming consist | P1 CONSIST | **DOWNGRADE → P2** — Ollama-specific 정당화 명시됨 |
| F-007 json_repair docstring | P1 stale | **PARTIAL** — react.py:31 은 오독, 다른 두 곳은 valid |
| F-008 _apply_style 비대칭 | P2 | **CONTEXT 누락** — Protocol 으로 이미 통합 완료 |
| F-009 agent discovery 분기 | P2 ROLE | **VALID 그러나 의도된 분리** — 데이터 모양 다름 |
| F-010 _handle_text_path hot-spot | P2 PERF | **CONFIRMED** P2 |

**총평:** 10건 중 *FP* 2건 (F-002, F-005), *과대 평가* 2건 (F-003, F-006), *과소 평가* 1건 (F-004), *주장 부분 오류* 2건 (F-007, F-008). 본인 검증 비율은 7/10 (라인/문법 인용은 정확).

---

## Q1~Q4 본인 검증 결과

### F-001 — HookRunner Phase 2 잔재 [CONFIRMED P0]

**Q1 라인 검증:** `runner.py:23-29, 75-76, 89-95` 모두 정확 (라인 오차 0). Read 로 본인 확인. `_run_shell_hooks` 본문은 docstring 만 있고 빈 함수 — auditor 주장 정확.

**Q2 인과/우회:** 본인 `rg -n "HookRunner\(" agent_cli/ tests/` 재실행 결과:
- `agent_cli/` 인스턴스화 0건 (docstring 예시는 `runner.py:18` 1건뿐).
- `tests/test_hooks_python.py` 에 13건 — *tests 가 dead production code 를 keep-alive* 시키는 패턴.
- `rg -n "shell_hooks_config" agent_cli/ tests/` → self-reference 3건 (runner.py 만), tests 에서도 0건 사용.

**대안 dispatch 경로 확인:**
- `loop.py:1029-1034`: shell hooks 는 `from agent_cli.hooks import run_hooks` 로 *직접* 호출 (HookRunner 우회).
- `loop.py:1057-1060, 1245`: HookRunner 는 `delegate`, `tool_use`, `skill` 관련 4 호출처에서 *Python hooks 전용*으로 사용. `shell_hooks_config` 는 한 번도 전달되지 않음.

→ `_run_shell_hooks` + `shell_hooks_config` 는 명백한 dead. 다만 `HookRunner` 클래스 자체는 *Python hooks 디스패치*로 살아 있음. auditor 가 "`HookRunner` 자체도 패키지 내에서 인스턴스화되지 않음" 이라 말한 것은 부분 오류 — `loop.py:1057, 1245, 1456, 1500` 등에서 `self.hook_runner.fire(...)` 사용함 (단 `hook_runner=None` 기본값이라 conditional 동작).

**Q3 일반화:** 같은 "Phase X 잔재" 패턴 추가 후보 grep:
- `rg -n "Phase 2|Phase 3|TODO.*Phase" agent_cli/` → runner.py:92 외 0건. 단발성 dead.

**Q4 우선순위:** 영향 LOC: `_run_shell_hooks` 7줄 + `shell_hooks_config` 파라미터/필드/branch 5줄 = 약 12 LOC 삭제. test 영향 0건. **P0 유지 가능** (정리 비용 낮음, 신규 기여자 혼선 방지 효과 큼).

**권장 보정:** auditor 권장 "HookRunner 자체도 인스턴스화 안됨" 문구는 fact-check 실패. 다음 라운드에서 정정 + Phase 2 wiring intent를 별도 task로 분리할지 사용자 결정 필요.

---

### F-002 — surrogate/thinking dup [FALSE POSITIVE]

**Q1 라인 검증:** `react.py:48-73`, `prefix_md.py:156-175` 라인 정확. 두 함수 본문 거의 동일 — 사실 부분 정확.

**Q2 인과/우회 (결정적):** auditor 가 *완전히 누락*한 in-file docstring 정책:
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

이는 *명시적 설계 결정* — "folder-deletable" 보장 + "third plugin 등장 시 lift". 현재 wire_formats plugin 은 2개 (`ls agent_cli/wire_formats/` 검증). 정책 트리거 미충족.

**Q3 일반화:** 동일 텍스트 전처리 추가 후보:
- `strip_markdown_fences` — react.py 에만 존재 (1 정의, 2 호출). prefix_md.py 는 fence 처리 다른 방식. dup 아님.
- `_THINKING_PATTERN` — react.py:42, prefix_md.py:137 두 곳. F-002 와 동일 정책.

**Q4 우선순위:** auditor 권장(`wire_formats/_text_preprocess.py` 추출) 은 *명시된 anti-policy* 를 위반함. **DROP 해야 함.**

**Critic 결론:** **FALSE POSITIVE**. auditor 의 권장은 코드 정책을 거스르는 신규 기술 부채. 다음 라운드에서 이 finding 철회 + auditor 가 docstring을 본인이 읽었는지 self-check 필요.

---

### F-003 — delegate/skill subdir + result.md persist [DOWNGRADE P0 → P1]

**Q1 라인 검증:** `delegate.py:192-200` (delegate_dir_name), `executor.py:153-167` (skill_dir_name), `delegate.py:212-222` (persist), `executor.py:198-204` (persist) 모두 정확. 본인 Read 로 확인.

**Q2 인과/우회:** `rg -n "os\.urandom\(3\)\.hex\(\)" agent_cli/` → 2 hits (delegate.py:197, executor.py:158). `rg -n "time\.strftime.*Y.*m.*d" agent_cli/` → 4 hits 중 동일 패턴 2개 (delegate.py:198, executor.py:159). 다른 2개 (context/session.py:47,53) 는 ISO display timestamp 로 용도가 다름. 진짜 dup.

**Q3 일반화:** 추가 후보 없음 (subagent 디렉터리 생성은 이 두 곳뿐). 단 *related* 패턴으로 F-004 의 `render_group_start`/`push_depth`/try-finally wrap이 4 호출처에 동일 — auditor가 일반화 누락 (delegate.py:617, 689 까지 4건).

**Q4 우선순위:** 영향 LOC:
- 추출 후 양쪽 줄어드는 LOC: 각 ~10줄 → 약 20줄 삭감.
- 변경 시 동기화 비용은 *낮음* — 둘 다 같은 시간/해시 포맷, dir prefix 만 차이.
- 호출 빈도: dir name 생성 자체는 delegate/skill 호출 시마다 (사용자별로 frequent).

dir 포맷이 바뀔 가능성 + drift 위험 vs. 추출 비용 (~30줄 helper) → **P1 적정**, P0 까지는 과대 (영향이 사용자 가시 동작 아님, drift 발생해도 catastrophic 아님).

**권장 보정:** P1 로 재분류 + auditor 권장 helper(`make_subagent_dir(prefix, name, parent)` + `persist_result(dir, text)`) 는 좋음 — 동일 변경 시 양쪽 동기화 강제.

---

### F-004 — skill wrapper dup [UPGRADE P1 → P0 후보]

**Q1 라인 검증:** `main.py:570-667` (_dispatch_skill), `loop.py:1404-1518` (_handle_run_skill) 라인 정확. 본인 Read 로 7단계 공통 패턴 검증:
1. `load_skills()` → 존재 확인: A는 `parts = query.split` + `cmd_name not in skills` (main.py:589-594), B는 `name not in skills` (loop.py:1442-1447). ✅
2. `disable_model_invocation`: A는 *없음* (user `/<skill>` 은 통과 가정), B는 있음 (loop.py:1450). ❌ A 누락.
3. render_group_start + push_depth + monotonic: A line 616-618, B line 1464-1466. ✅
4. execute_skill: A line 627-644, B line 1472-1487. ✅
5. render_pop_depth + render_group_end (finally): A line 645-651, B line 1491-1497. ✅
6. None 결과 fallback: A line 653-665, B line 1509-1518. ❌ 형식 다름.
7. ctx.add observation: A line 656-665, B는 *없음* (반환 observation을 caller가 add). ❌ A 만 수행.

**Q2 인과/우회:**
- A 는 user REPL 진입점 — chat REPL / web 양쪽이 `try_dispatch_agent_or_skill` 으로 통합되어 `_dispatch_skill` 호출.
- B 는 모델 진입점 (model 이 `run_skill` action 생성 시).
- 두 호출처가 *반드시 분리* 되어야 할 진짜 이유: A 의 ctx.add 는 user 발화로 기록("Used skill: ..."), B 의 observation 은 model history 에 OBS 형식으로 기록. ctx 기록 *의미* 가 다름.

**Q3 일반화 (중요):** `rg -n "render_group_start" agent_cli/` → 4 호출처:
- `main.py:616` skill
- `loop.py:1464` skill
- `delegate.py:617` delegate (sequential)
- `delegate.py:689` delegate (parallel)

→ 모두 동일한 wrapping 구조 (push_depth + monotonic + try/finally + pop_depth + group_end). **auditor 가 누락한 일반화**. helper `render_group_scope(label, icon)` (context manager) 추출하면 4 호출처 모두 단순화.

**Q4 우선순위:**
- 영향 LOC: 4 호출처 × 평균 10줄 wrapper = ~40줄 → context manager 1개 + 4 호출처 각 3줄 = ~20줄 절감.
- 호출 빈도: skill+delegate 호출 시마다 발화 (frequent).
- 향후 모니터링 이벤트 (turn 시간, observation length 등) 추가 시 4곳 동시 수정 필요 → drift 발생 위험.

→ **P0 또는 강한 P1**. auditor 가 본 finding 을 P1 으로 둔 것은 *skill 진입점 2곳만 본 결과*. delegate 까지 일반화 시 P0 적정.

**권장 보정:** 두 단계로 분리
- (a) `render_group_scope(label, icon, *, duration_callback=None)` context manager 추출 (4 호출처) — P0.
- (b) skill 자체 wrapper (`invoke_skill_with_render`) — P1, ctx.add 정책 차이가 caller 결정이라 외부 시그니처 더 복잡.

---

### F-005 — brace scanner dup [FALSE POSITIVE]

**Q1 라인 검증:** `react.py:516-553` (_extract_json_block), `prefix_md.py:178-213` (_find_last_json_block) 본인 Read. 두 함수 알고리즘은 brace counting + string escape 처리로 *유사*하나 결과 의미 다름:
- A `_extract_json_block(text)` — 첫 outermost `{...}` 의 substring 반환. depth=0 도달 시 return.
- B `_find_last_json_block(text)` — **마지막** balanced top-level 의 `(start, end)` 인덱스 반환. 모든 블록을 순회.

함수 *시그니처도 다름* (str vs tuple[int,int]|None). 정책도 다름 (first vs last).

**Q2 인과/우회:** F-002 와 동일 의도된 dup — react.py:28-36 의 "folder-deletable as a single boundary" 정책 적용. prefix_md.py:130-134 도 같은 정책.

**Q3 일반화:** 추가 brace counter 없음. 정책 정착.

**Q4 우선순위:** auditor 권장 (`find_brace_blocks(text)` 헬퍼 추출 후 first/last/all 정책만 결정) 은 *명시 정책 위반*. **DROP.**

---

### F-006 — provider streaming consist [DOWNGRADE P1 → P2]

**Q1 라인 검증:** `ollama.py:94-148`, `openai_compat.py:80-151`, `anthropic.py:87-171` 본인 Read. auditor 표 모두 정확.

**Q2 인과/우회 (결정적):** Ollama `_handle_stream` line 107-114 의 *본인 docstring*:
```
# Ollama keeps HTTP 200 but can emit {"error": "..."} lines
# mid-stream (e.g., mlx runner failure, cache corruption).
# raise_for_status() is already past; the only signal is the
# top-level `error` key, which normal chunks never carry.
```
→ Ollama-specific 동작 명시. OpenAI-compat 는 SSE 프로토콜로 `data: ...` envelope 이고, error 는 응답 본문이 아닌 HTTP status / SSE event type 으로 전달 (OpenAI spec). vLLM 도 동일 SSE 규약.

`prompt_eval_ns` source 차이도 동일: Ollama 는 서버가 token 시간 보고함, OpenAI-compat 는 보고 안 함 → 클라이언트 측정이 *유일한 신호*.

**Q3 일반화:** 추가 provider 없음 (`ls agent_cli/providers/` → ollama, openai_compat, anthropic, gemini? 확인).

```
agent_cli/providers/ → 본인 확인 필요 (다음 라운드)
```

**Q4 우선순위:** mid-stream error silent corruption 위험은 실재하나 *실제 OpenAI-compat 서버가 mid-stream JSON envelope 에 error 키 넣는 사례 미확인*. 가설적 위험. **P2 적정**, P1 은 과대.

**권장 보정:** P2 재분류 + 다음 라운드에서 providers/ 전체 디렉터리 일관성 매핑.

---

### F-007 — json_repair docstring [PARTIAL]

**Q1 라인 검증:** `recovery/observability.py:62` 정확. `react.py:31` 정확.

**Q2 인과/우회 (중요):**
- `pip show json_repair` → 외부 패키지 없음. `grep json_repair pyproject.toml` → 0건. 외부 의존성 0.
- 그러나 `react.py:31` 의 `(no \`\`json_repair\`\` module)` 는 *명시적으로 "we DON'T have one"* 라는 negation 문맥 — auditor 가 오독. 라인 28-36 컨텍스트 읽으면 `parsing/` 패키지·`json_repair` 모듈 등 *가설로 존재하지 않는* 것들을 거부한 진술임.
- 반면 `recovery/observability.py:62` "2=json_repair" 는 *parse_stage 레이블* 인데 실제 함수명은 `repair_json` (react.py:485). 사소한 명명 불일치. `base.py:79` 도 동일 ("2=json_repair").
- `providers/ollama.py:13` 도 "json_repair" 사용 → 같은 레이블.

**Q3 일반화:** `rg -n "json_repair|repair_json" agent_cli/` → 8 hits. observability/base/ollama docstring 3곳이 "json_repair" 레이블 일관 사용. 실제 함수는 `repair_json` 1정의 + 2호출. 일관성을 위해 *레이블 변경* 1방향만 결정하면 됨.

**Q4 우선순위:** 신규 기여자 혼선 위험 *낮음* — `parse_stage` enum 같은 컨텍스트라 명확. **P2 적정** (P1 과대).

**Critic 결론:** auditor 의 `react.py:31` 인용은 오독. 다른 두 곳 (observability.py:62, base.py:79) 의 inconsistency 는 실제 — P2 로 재분류, 함수명을 `repair_json` 으로 통일 권장.

---

### F-008 — _apply_style 비대칭 [CONTEXT 누락]

**Q1 라인 검증:** `main.py:241-273` (`_apply_style`) 본인 Read. 라인 정확.

**Q2 인과/우회 (결정적):** auditor 가 누락한 *바로 이어진 docstring* (main.py:260-272):
```
# Shared ``@<agent>`` / ``/<skill>`` dispatch (chat REPL + web)
# Why a Protocol-based dispatcher instead of two parallel branches:
# the prefix semantics ... are identical across surfaces. Only the
# output format differs ... The Protocol pins the contract so
# adding a new surface ... means writing one ~30-line output
# adapter, not re-implementing the 80-line prefix block.
```
→ chat REPL ↔ web 통합은 *이미 완료*. `try_dispatch_agent_or_skill` + `DispatchOutput` Protocol 사용 (main.py:389, 1347, 1581, 1592; web/server.py:95).

`_apply_style` 만 따로 `chat` 진입점에서 호출되지 않는 이유는 *web 은 renderer 를 명시적으로 set_renderer* 하기 때문 (auditor 가 본인 인용함). 이건 비대칭이 아니라 *web 의 명시적 의도*.

**Q3 일반화:** "공통 setup 추출 가능성" → 이미 Protocol 도입으로 부분 통합. 남은 차이는 surface-specific.

**Q4 우선순위:** 실 영향 없음. **DROP 또는 P3 (참고)**.

**권장 보정:** auditor 가 "다음 라운드 검토" 라고 미루기보다 본 라운드에서 본인 _ConsoleDispatchOutput / WebDispatchOutput 매핑을 읽었어야 함.

---

### F-009 — agent discovery 분기 [VALID 그러나 의도된 분리]

**Q1 라인 검증:** `main.py:366-386` (_collect_agent_names), `prompts/system_prompt.py:577-584` 본인 Read. 라인 정확.

**Q2 인과/우회:** 두 함수의 *반환 데이터 모양 다름*:
- `_collect_agent_names()` → `list[str]` (이름만, REPL `@agents` 리스트용)
- `build_agent_descriptions()` → `list[(name, description)]` 사용 (system prompt 의 agent 섹션용)

`_collect_agent_names` 의 docstring: "Lifted from the chat REPL's inline listing block so both surfaces walk the same paths" — 이미 chat ↔ web 사이는 통합됨. system_prompt.py 는 *prompt build phase* 라 ResourceLoader 의 meta 까지 필요해서 다른 API 사용.

**Q3 일반화:** auditor 권장 (`_agent_loader.list_names()` 단일화) 는 ResourceLoader 가 모든 호출처를 처리하면 가능. 그러나 _collect_agent_names 는 deduped order 정책(첫 hit wins) 명시.

**Q4 우선순위:** 변경 시 영향 LOC 작음 (~20줄). 두 API 모두 `_AGENT_SEARCH_PATHS` 의존 — drift 위험 있으나 실 변경 빈도 낮음. **P2 적정.**

---

### F-010 — _handle_text_path hot-spot [CONFIRMED P2]

**Q1 라인 검증:** `loop.py:450 (_handle_text_path), 494 (_dispatch_text_path)` 본인 Read. `rg -c "^        if " agent_cli/loop.py` = 50 — 메서드 단위 분기 수가 아니라 파일 전체 중첩 if. auditor 측정 부정확하나 fact 자체(긴 메서드)는 valid.

**Q2 인과/우회:** dispatch table 도입 시 special action(`ask`, `run_skill`, `ready_for_review`, `complete`) 의 cross-action 상호작용(예: skill 결과 + observation 처리)을 caller 가 직접 받아야 함. 단순 dispatch 가 아닐 수 있음.

**Q3 일반화:** main.py 도 1682 LOC. delegate.py 712 LOC. 다 hot-spot. auditor `Top hotspots (LOC)` 표 이미 충분.

**Q4 우선순위:** 리팩토링 비용 큰 항목. **P2 유지** — refactor 트리거 (예: 새 wire format/special action 추가) 발생 시 함께 처리.

---

## 누락 영역 (auditor 가 Round 1에서 다루지 않은 surface)

본인 grep 결과 다음 영역들 Round 2~4 에서 다뤄야 함:

1. **`agent_cli/render/` minimal vs web (각 ~600 LOC)** — auditor 가 본인 다음 라운드 제안 (3번)에서 이미 인지. **OK.**
2. **`agent_cli/tools/symbols.py` (785 LOC)** + `tools/context.py` (574 LOC) — 가장 큰 hot-spot 인데 Round 1 미터치. **Round 2 권장.**
3. **`agent_cli/prompts/system_prompt.py` (658 LOC)** — F-009 에서만 잠깐 언급. 빌더 중복 가능성 (auditor 본인 다음 라운드 제안 미포함).
4. **`agent_cli/tools/registry.py` (563 LOC)** + tool dispatch 경로 — 미터치.
5. **render_group_start wrap 4 호출처 일반화** (F-004 critic 추가) — Round 2 검증 필요.

---

## Round 2 지시 (auditor 에게)

### 필수 조치 (Round 2 audit_report_v2.md 에 반영)

1. **F-002 / F-005 철회**: docstring 정책 (`react.py:28-36`, `prefix_md.py:130-134`) 인용하며 본 finding을 *명시적으로* 철회.
2. **F-001 정정**: `HookRunner` 자체는 4 호출처(`loop.py:1057, 1245, 1456, 1500`)에서 사용됨을 인정. 정정 대상은 `_run_shell_hooks` + `shell_hooks_config` 두 항목으로 한정.
3. **F-003 P0 → P1 재분류**, **F-006 P1 → P2 재분류**, **F-007 P1 → P2 재분류**.
4. **F-004 강화**: `render_group_start` wrap 4 호출처 (main.py:616, loop.py:1464, delegate.py:617, delegate.py:689) 일반화 분석. `render_group_scope(label, icon)` context manager 추출 가능성 검증.

### Round 2 깊이 분석 영역 (critic 지정)

5. **`agent_cli/tools/symbols.py` + `tools/context.py` + `tools/registry.py`** 의 ROLE/DUP 매핑. 특히 symbols.py 785 LOC 가 단일 책임인지 검증.
6. **`agent_cli/prompts/system_prompt.py`** 의 빌더 함수들 (`build_skill_descriptions`, `build_agent_descriptions`, `build_format_rules` 등) 의 중복 패턴 분석.
7. **`agent_cli/render/__init__.py`** 의 dispatcher (각 `render_*` wrapper) 가 minimal/web 양쪽에 동일 시그니처로 전달되는지 — base.py:287 LOC 이 ABC인지 거의 빈 contract 인지 확인.
8. **`HookRunner` 에서 사용되는 4 이벤트** (`OnDelegateStart/End`, `OnSkillStart/End`) 의 hook 사용 빈도 — `hook_runner=None` 기본값 시 사실상 hooks 비활성. 사용자 측에서 실제 활성화 경로가 있는지 확인.

### Round 3 후보 (Round 2 결과에 따라)

- providers/ 전체 디렉터리 (Gemini 등 추가 provider 가 있는지).
- recovery/ 모듈 활용도.
- web/server.py 의 dispatch 와 SSE 이벤트 매핑.

---

## Critic Round 1 Verdict (interim)

**현재 auditor 보고서를 PASS 로 수용할 수 없음.** P0 3건 중 2건 (F-002, F-005) 이 docstring 정책 누락으로 인한 false positive. 명시된 정책 위반 권장이 사용자에게 그대로 전달되면 정상 설계를 망가뜨릴 위험.

**Round 2 에서 위 정정사항 반영 + critic 지정 영역 깊이 분석 후 재평가.**
