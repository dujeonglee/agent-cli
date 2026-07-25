# Agent-CLI v2 아키텍처 문서

> **이 문서는 코드와 함께 유지보수되어야 합니다.**
> 코드 수정 시 관련 섹션을 반드시 업데이트하세요.
>
> 최종 업데이트: 2026-05-25
> 버전: 2.0.0-dev
> 총 소스: ~23,600 LOC (92 Python 파일) + ~27,700 LOC 테스트 (72 파일)
> 총 테스트: ~2030 유닛

---

## 1. 프로젝트 개요

Agent-CLI는 on-premise LLM을 위한 모듈형 에이전트 CLI입니다. ReAct(Reasoning + Acting) 패턴으로 LLM이 도구를 사용하여 단계적으로 작업을 수행합니다.

### 핵심 특징

- **멀티 프로바이더**: Anthropic, OpenAI 호환(vLLM, LM Studio, mlx-lm/omlx)
- **3단계 파싱 폴백**: json.loads → JSON repair → regex 추출
- **Basic JSON Mode**: OpenAI `response_format={"type":"json_object"}`, Anthropic tool calling (strict JSON Schema는 확장성 위해 미사용)
- **Hashline 편집**: CRC32 해시 기반 정밀 파일 편집 + 퍼지 매칭
- **컨텍스트 관리**: 매 LLM 호출 직전 `(C−S−O)×0.8`(S=system 실측) 초과 시 LLM 요약 compaction (recursive single-call), 실패/재구성 후 미충족 시 FIFO drop으로 belt-and-braces fallback, 호출 직후 서버 실측 `usage`로 reconcile (flow 1 예방); 추정이 빗나가 서버가 400(prompt too long)을 던지면 `force_fit`으로 compact→FIFO 사후 축소 후 bounded 재시도 (flow 2 반응); history.jsonl 영속화 + compaction.json (resume용 dynamic_start_index)
- **모델 적응형**: context window, thinking budget에 따른 자동 조정

### 외부 의존성

| 패키지 | 버전 | 용도 |
|--------|------|------|
| `typer` | >=0.9 | CLI 프레임워크 |
| `rich` | >=13.0 | 터미널 렌더링 (Panel, Table, Rule 등) |
| `requests` | >=2.28 | HTTP 클라이언트 (LLM API 호출) |
| `pyyaml` | >=6.0 | 스킬 frontmatter 파싱 |
| `tree-sitter` | >=0.23 | code_index 파서 코어 |
| `tree-sitter-python` / `-javascript` / `-typescript` / `-cpp` / `-go` / `-rust` / `-java` | >=0.23 | code_index 언어 grammar |
| `tree-sitter-markdown` | >=0.3 | code_index markdown heading 인덱스 |
| `pysqlite3-binary` | >=0.5 | code_index SQLite fallback (Linux only — `--without-sqlite` 빌드된 CPython 대비) |

**Optional**: `agent-cli[web]` → `fastapi` / `uvicorn[standard]` / `sse-starlette`.
**Dev**: `pytest`, `pytest-asyncio`, `httpx`, `hypothesis` (property-based 테스트).

**시스템 패키지**: C/C++ 인덱싱의 `unifdef` 단계는 번들된 pure-Python (`_unifdef.py`) 이 기본 처리 — 설치 불필요. 시스템 `unifdef` 바이너리가 있으면 자동 우선 사용 (battle-tested C).

표준 라이브러리: json, re, dataclasses, pathlib, os, sys, zlib, textwrap, unicodedata, copy, tempfile, threading, sqlite3 (code_index — stdlib 우선, 미존재 시 `_sqlite.py` shim 이 `pysqlite3-binary` 로 폴백)

---

## 2. 디렉토리 구조

```
agent_cli/
├── __init__.py              (3)    패키지 버전 (__version__ = "2.0.0-dev")
├── __main__.py              (5)    python -m agent_cli 진입점
├── main.py                  (2241) CLI 명령어: run, web, setup, sessions, update. **C4 공용 부트스트랩 (v4.46.0)**: `SessionBootstrap`(frozen — provider 6-튜플+wire format+예산) + `_bootstrap_provider`(셋업→fail-fast 포맷 해석(v5.19.0: `resolve_wire_format` 체인 — 명시 `--response-format`(default None=미지정 감지) > resume 세션 메타(`session_format=` kwarg) > models.json 모델 바인딩 > DEFAULT; web 의 대화형 최근-세션 resume 은 부트 후 결정이라 caller 가 기록 포맷으로 재해석+`dataclasses.replace`)→70% 예산 폴백) + `_load_resume_session`(핸드셰이크 전 fail-fast) + `_build_context`(ContextManager 단일 조립) 를 run/web 이 공유 — 세션 획득 UX(run=명시 --resume 만, web=+최근세션 프롬프트)·renderer·worker 만 커맨드별. 예산 폴백은 `compute_token_budget`=(context×7)//10 로 **통일**(이전엔 run/web 상이 — C4 감사 발견). **`run --resume <id>`**(v4.46.0): web 과 같은 on-disk 세션 로드 → 복원 컨텍스트 위에 QUERY 주입(run↔web 상호 이어가기). **web MCP 배선**(v4.46.0): `_setup_mcp`+TOOLS.update+worker `run_loop(mcp_manager=)`+`capture_startup_system_prompt(mcp_manager=)`+shutdown `disconnect_all` — 이전엔 web 만 미배선. --style, --response-format, resume preview. **`update`**: `gh release view`로 최신 릴리스 태그 확인 → `_parse_version` 비교 → 새 버전이면 `gh release download`(wheel)+`pip install --upgrade`(`--check` 확인만, `-y` 확인생략). editable/dev 설치는 `_is_editable_install` 감지해 거부+`git pull` 안내(`--force` 우회). **`_prompt_model_capabilities` wire-format 질문 (바인딩 UX ②, v5.22.0)**: 대화형 모델 등록(감지 실패 폴백)에 "Wire format [auto]" 한 줄 — auto/빈 입력=필드 미기록(해석 체인 위임), `list_names()` 등록명만 수용(오타 재질문 — D2 조용한 폴백 금지), 선택 시 entry 에 `wire_format` 키 추가. **세션 표시 공유 헬퍼**: `_print_session(meta)` 가 id·시각 + `session_summary` 기반 `↳ 마지막 요청` / `→ 마지막 결과`(또는 `(in progress)`)를 한 블록으로 출력 — `sessions` 명령과 resume 프롬프트가 동일 포맷 공유. **`_maybe_resume_recent(workspace, response_format, prompt_fn)`**: `--resume` 없이 `web` 진입 시 가장 최근 세션을 보여주고 `[y/N]` 질의 — `y` 면 `load_session` 으로 이어가고(`is_resume=True`) 그 외엔 `create_session` 으로 새로 시작. `prompt_fn` 은 TTY 면 `input`, 비대화(파이프/cron)면 `None` → 묻지 않고 항상 새 세션 (안전 기본값). **`DispatchOutput` Protocol + `_ConsoleDispatchOutput` + `try_dispatch_agent_or_skill`** — `@<agent>`/`/<skill>` 접두사 처리 (listing, invocation, not-found) 공유 dispatcher. `run` 은 `_ConsoleDispatchOutput`(Rich 색상), `agent-cli web` worker 는 `web.server.WebDispatchOutput`(observation 이벤트) 어댑터 주입. unknown `@`/`/` 명령은 LLM으로 통과하지 않고 error observation 발사 (오타로 인한 사고성 LLM round-trip 방지). **`web` 명령**: `--resume <id>` 지원 — provider 핸드셰이크 전에 `load_session` pre-check로 unknown ID fail-fast, `ContextManager(resume=True)` 로 캐시 복구 후 `renderer.replay_from_history(ctx)` 한 번 호출해 persistent event buffer를 재구성 → 이후 새 SSE 연결의 snapshot replay로 이전 turn이 그대로 UI에 복원. **graceful shutdown**: `uvicorn.Server(config).run()` 직접 호출 + `KeyboardInterrupt` swallow + `finally` 블록에서 `renderer.shutdown_all_connections()` → `server.shutdown()` → `worker.join(timeout=2s)` → `finalize_session(...)` 순서로 정리 (lifespan shutdown 훅이 SSE generator를 먼저 닫아도 idempotent).
├── resource_loader.py       (144)  ResourceLoader — 파일 검색/우선순위 (스킬/에이전트/지시사항)
├── fsio.py                  (82, v4.47.0)  **파일 저장 패턴의 단일 소유자** — ① 원자 교체 atomic_write_text/json(유니크 mkstemp→os.replace; 고정 tmp 는 v4.27.1 status.json 레이스 실측으로 금지, 부모 소실은 실패 시 mkdir+재시도) ② 가드 append append_line(history.jsonl 가드의 일반화) ③ 직접 쓰기는 도구 산출물 의미론이라 의도적 비사용. 소비: compaction.json(고정 tmp 교정)·web.json(비원자 결함 발견-수리 — 보드가 half-write 읽을 수 있었음)·status.json(자체 mkstemp 수렴)·models/config·memory.jsonl rewrite·session meta·DIRECTIVE.md·directive presets·turns.jsonl/raw appends. code_index builder 는 자체 원자 규약 보유(비수렴)
├── memory.py                (242)  세션 메모리 스토어 — LLM 이 큐레이션한 **compaction-immune** salient 노트. `<session_dir>/memory.jsonl`(read_context history 와 같은 세션 디렉토리, resume 복원). **stateless**(매 op JSONL fresh read → resume 정합, 캐시 sync 없음). `add/get/update/delete/list_entries` + `render_index`(상시 `## Session Memory` 섹션 — 요약만, detail 제외 → 토큰 저렴 + recall) + `format_entry`(get 전체). type enum=`failure`⚠/`discovery`💡/`decision`🔀/`note`📝(검증·아이콘 단일 출처). id=max+1 monotonic(삭제해도 재사용 X). 인덱스 `_INDEX_CAP=30` 소프트 캡(초과분 "생략" 꼬리). `_io_lock` 로 read-modify-write 직렬화. `mark_memory_dirty`/`consume_memory_reload`(mutating op → 다음 턴 loop rebuild). **왜 별도 스토어**: 시스템 프롬프트는 `ctx.get_messages()` 밖이라 압축 대상 아님 — 롤링 컨텍스트에 넣으면 정작 필요할 때 요약/드롭됨 + 자기-출력 재공급 mimicry 위험(docs/session-memory/DESIGN.md).
├── config.py                (217)  config.json 3레이어 로딩 + models.json 레지스트리
├── setup.py                 (267)  SetupWizard (Rich TUI, 첫 실행 설정 마법사 — 기존 config 노출 + 프로브 진행 표시). 모델 선택: OpenAI 호환·Anthropic 둘 다 `/v1/models`(`_list_models(provider)` — OpenAI=`Bearer`, Anthropic=`x-api-key`+`anthropic-version` 헤더; 응답 `data[].id` 동형)로 목록 표시 후 선택(`_select_model_from_list`), 실패 시 수동 입력(OpenAI 기본 `gpt-4o`, Anthropic 기본 `claude-sonnet-4-20250514`). omlx 가 두 API 를 같은 모델로 서빙 + 실 Anthropic 도 GET /v1/models 지원이라 양쪽 동작
├── constants.py             (69)  공유 상수 (timeout, observation 템플릿, INTERRUPT_NOTICE). 외부 모듈 의존 없음 — 저층 레이어. wire-format-specific 상수 (FORMAT_RULES, RETRY_HINT_*, SYSTEM_USER_PREFIXES) 는 ``wire_formats/`` 의 plugin이 소유
├── thinking_tags.py         (88, v5.19.1)  **thinking-tag 스트리핑 단일 소스** — 모델이 CoT 를 content 태그로 흘리는 leak(모델-런타임 quirk, wire-shape 속성 아님)의 vocab 4종 + 정규식(①완전 블록 ②미닫힘 opener ③고아/트레일링) + `strip_think_blocks(stop=)`(①② — **v7.11.4**: ②미닫힘 truncation 은 **라인-선두 opener 만**(산문 속 `<thinking>` 언급이 뒤따르는 tool call 을 EOF 까지 삼키던 op-소실 수리) + 포맷별 `WireFormat.thinking_stop` 구조 마커에서 정지: xml_fc=`<tool_call>|<function=`, json_fc=라인-선두 `[` — 진짜 미닫힘 opener 뒤의 액션도 생존). 종전 4곳 중복(providers/base·react·json_fc 정규식 2개·capabilities vocab)을 이주 — 소비자: openai provider(content 정규화, 비-파서 소비자 보호 5.10.0)·`WireFormat.strip_thinking`(파서 stage 0 — provider 미경유 경로 anthropic/http leak·bench 우회의 유일 방어)·capabilities(탐지 vocab)·json_fc(③ 정규식만 — 적용 지점은 포맷 소유). ①②는 blind sub(문자열 값 안 완전 블록도 제거 — openai 경로 5.10.0 부터의 기존 동작과 경로 간 일관), ③은 앵커드(문자열 값 안 고아 태그 보존 — json_fc 계약 테스트). 의존 0 (re 만) — 최저층
├── wire_formats/                   Wire format 플러그인 시스템 — 모델 응답 형식 추상화
│   ├── __init__.py          (245)  Registry (`register` / `get(name=None)` / `list_names`) + `all_system_user_prefixes()` (format-agnostic + plugin prefix 통합 entry point). builtin plugin (json_fc, xml_fc) 자동 등록. **react 는 v7.0.0 제거** — 존치 명분(fallback 사이클·json_mode 유일 소비자) 소진, 실측 우위 없음. json_mode/structured-output 프로브·capabilities 필드도 소비자 0 으로 동반 제거. **`DEFAULT_WIRE_FORMAT = "json_fc"`** — 기본 wire format 의 single source of truth: `get(None)`/unspecified fallback, CLI `--response-format` 기본, 새 세션 default 가 모두 여기로 resolve (변경은 1곳). **모델별 바인딩 (v5.19.0 — docs/multi-wire-format/DESIGN.md Phase 1)**: `wire_format_for_model(model)` = models.json 엔트리의 선택 필드 `wire_format` 조회(모델명-키 — `ModelCapabilities` 에 안 태움: role model 오버라이드 경로가 capabilities 를 재해석하지 않아서) + `resolve_wire_format(explicit, session_format, model)` = 해석 체인 **명시 `--response-format` > resume 세션 메타 > 모델 바인딩 > DEFAULT** (unknown 이름은 어느 소스든 KeyError fail-fast, D2). config lazy import (wire_formats→config 단방향). **`try_foreign_parse(bound, text)` (Phase 3, v5.21.0)**: 0-op emission 을 타 등록 포맷 파서로 구제 — DEFAULT 먼저·이름순, 수용=stage 1·2 + action·dict-input op (stage 3 regex 긁기는 키메라 저신뢰라 배제), bound 제외. 조합은 레지스트리 소유 = 포맷 간 코드 결합 0 (self-contained 유지). 소비자는 dispatch (라벨·corrected_record 재렌더). **2026-06-11 prefix_md→md_array 전환**(현 json_fc 의 전신): Phase-2 풀루프 95.2%(=react) + 실전 150턴 형식실패 0.7%(prefix_md 동급) 검증 후. json_fc 는 prefix_md 의 기능적 상위집합(단일-op + 멀티-op). prefix_md 는 2026-06-13 제거(wire-format 정리 Step 1 — json_fc 가 마크다운 shape 흡수, 남은 등록 포맷은 json_fc·react 둘). 멀티-op 자발 활용률은 아직 낮음(~0.7%) — 다음 거리는 자발 배칭 유도 프롬프트.
│   ├── base.py              (632)  `WireFormat` ABC + `ParsedAction` dataclass. Plugin 베이스 클래스 — abstract method (format-specific 부분만, plugin이 반드시 구현)와 concrete default (lifecycle / 식별 hook, 보통 그대로 상속) 분리. **멀티-op 추상화 (additive, 멀티-op wire format 1단계 — docs/inputs-array-schema/DESIGN.md)**: `Op`(action+action_input+truncated 1개) + `ParsedTurn`(thought + ops 리스트 + terminal + parse_stage) dataclass, 그리고 concrete **`parse_turn(text)->ParsedTurn`** = 기본적으로 기존 `parse()`를 감싸 단수 action을 1-op turn으로 매핑(action 없어도 action_input 있으면 Op 보존 → infer 복구 전제 유지; terminal 항상 False — 단수 포맷은 `complete` op로 종료). **단수 포맷(react)은 무변경**, 멀티-op 포맷만 `parse_turn` override. 루프는 아직 `parse()`를 쓰므로 현재 inert(동작 0 변화); 루프의 `parse_turn` 전환은 후속 단계. **`is_degenerate(text)`** (default False): emission 이 wire shape 을 반복(format runaway)했는지 — 두 용도: (1) loop 이 `provider.call(degeneration_check=...)` 로 넘겨 **streaming 중 조기 break**(토큰 절약), (2) loop 이 최종 emission 을 `FAILURE_DEGENERATE` 로 라벨·raw 캡처. runaway 가능 shape 만 override(prefix_md). **`sanitize_thought(thought)`** (default identity): 모델이 thought 에 흘린 wire sentinel(줄단독 `## 헤더`)을 제거 — raw 가 prior 로 재주입되면 `## Thought … ## Thought` 중복이 self-reinforcement→mimicry→runaway 의 **근본 원인**이라 save-time 에 두 곳에서 정제: `parse`(structured thought) + `serialize_assistant_for_history`(bare content, action 무효 turn). 정제된 record 가 history→prior(render)+화면 일괄로 흐름. react 는 thought 가 JSON string(이스케이프)이라 무관(identity 상속). Abstract: render_full_example / format_rules_anchor / format_rules_field_specific / parse / 6개 recovery wording / system_user_prefixes. Default: format_rules = `build_format_rules(self)`, render_action_input = dict→JSON via json.dumps (wire가 직렬화 — 호출자는 dict만 전달, JSON 가정은 이 hook 한 곳에; render_full_example/history round-trip도 이 hook 경유), provider_call_kwargs = `{}`, prefill = `""`, serialize_assistant_for_history = `self.parse()` + 구조화 필드 추출(+ bare content `sanitize_thought` save-time 정제), render_assistant_from_history = `self.render_full_example()` 호출로 wire shape 재방출 (live + resume prior 둘 다 이 경로 — `normalize_assistant_for_messages` 는 제거됨, 매 턴 prior 가 record 에서 render 됨). **대칭 플래그 `thought_required`/`action_required`** (기본 True): 각각 thought/action 누락 시 **recovery(LLM 재발화) vs 관용·infer** 를 게이트 — loop(복구 측)과 프롬프트(`_gated_rule`)가 같은 플래그를 읽음. **프롬프트 게이트 플래그 `multi_op`/`exposes_complete`** (기본 False/True): multi_op=한 턴 여러 op — 프롬프트 레이어(registry `get_tool_descriptions` + system_prompt 인라인 빌더)가 per-tool 배치 prose 생략·prefix strip; exposes_complete=False — `complete` 를 도구 목록에서 제외(thought-only terminal 로 종료하는 포맷용). **`_gated_rule(required, strong, soft=None)`** = Format-Rules clause 강도를 플래그로 약화/생략하는 hook (현재 soft 미제공이라 inert — 출력 불변, 미래에 soft 채우면 plugin·loop 무변경으로 완화). **parse 계약**: action 슬롯이 무효/없음이어도 식별된 action_input 은 **보존**한다 (infer_action 복구 / NO_ACTION echo 의 전제 — 드롭한 action 을 wire-key prefix 로 복원하려면 파서가 input 을 남겨야 함). 모듈 docstring에 assistant turn lifecycle (A → B → C; render 가 live+resume prior 둘 다 빌드) 표 포함. **`diagnose_syntax_error(prior_content)->str|None`** (concrete default `None`): NO_JSON 회복 시 JSON 이 *어디서* 깨졌는지(line/col + 캐럿) 짚는 opt-in seam — base 는 None(JSON 없는 포맷·미구현 포맷은 generic 힌트 유지), JSON 포맷이 자기 후보 추출 후 `_json_diag.describe_json_error` 에 위임. **`serialize_terminal_for_history(thought, result)`** (concrete default 단수 `{action:complete, action_input}`): loop 의 complete 핸들러가 (nested-envelope 언랩된) result 를 들고 있어 `serialize_assistant_for_history`(raw 입력) 를 못 타므로, terminal turn 을 **이 포맷이 다른 op 와 같은 모양으로** 기록하는 병렬 진입점 — 멀티-op 포맷(json_fc·xml_fc)은 `{ops:[{complete}]}` 로 override 해 history 동질성 유지(과거 complete 핸들러가 직접 단수 dict 를 `ctx.add` 해 73개 op 와 다른 모양으로 새던 불일치 수리; render·summary 는 양쪽 관용이라 무해했으나 shape 읽는 외부 도구를 속임). **`strip_thinking(text)` (concrete static, v5.19.1)**: 파서 stage 0 — thinking 블록(①완전·②미닫힘 opener) 격리해 `(cleaned, thinking|None)` 반환; 구현은 thinking_tags 단일 소스(openai provider 와 같은 함수 — 그 경로에선 no-op, provider 미경유 경로의 유일 방어). react `_strip_thinking_blocks`·json_fc `parse_turn` 진입부가 위임(json_fc 는 종전 ①② 자체 처리가 없어 provider 미경유 경로 무방비였던 갭 봉합). 포맷 간 behavior 공유가 아니라 ABC 기계라 self-contained 불변식 합치. plugin 추가 = WireFormat 상속한 새 파일 1개, main code 0 변경.
│   ├── json_fc.py          (~745) **JsonFcFormat (기본 wire format — 멀티-op; md_array 의 v6.0.0 리네임+리셰이프 후계, docs/multi-wire-format/PHASE4.md)** — **산문 thought + flat `{action, ...params}` op 들의 bare JSON 배열** (D7: 래퍼·헤더 없음 — xml_fc D4 동형). 구 `## Thought`/`## Action` 헤더 emission 은 **legacy 관용(stage 2 drift)** 으로 계속 수용하고 prior 는 캐노니컬로 재렌더(자기 교정); 헤더-runaway 클래스는 shape 차원 소멸. actionless-op 보존은 위치 신호(배열=emission 끝)로, `"action"` 흔적 있는 깨진 JSON 은 stage 0 진단. bakeoff A/B(140run)=구형과 동등 확인 후 교체. 한 턴 여러 INDEPENDENT op(배열 원소), plain 키(no prefix), **op 하나=대상 하나(per-tool 배치 금지 — 배치 중첩이 27B 90% 깨뜨린 실측)**. bare 객체=1-op 관용. **`_repair_anonymous_op_objects`/`_extract_op_json` (DESIGN Exp 8 — 실전 1781208482 5/6 실패)**: 27B 가 대용량 param op 배치 시 `{"action":X, {params}}`(params 를 익명 중첩 객체로 = invalid JSON)를 내면 strict 파싱 실패 후 **익명 `{` 를 제거해 복구** → parse_stage 2(drift). 두 shape: A `{"action":X, {params}}`(균형 `}}`, 27B)·B `{"action":X, {params}`(닫는 `}` 하나, 35B — array 에 N개 `{` 불균형) — `_extract_op_json` 가 양쪽 시도해 valid 채택. 컨텍스트+문자열 인식 단일 패스(키-자리 `{`만 unwrap; array 원소·`:`-값·문자열 내 brace 무시 — C코드 content 안전). **unwrap 후 merge+close 합성 (세션 1783129061, 27B write_file 배치)**: 모델이 anon-wrap 과 함께 배열 `]` 도 빠뜨리고, 심지어 배치를 여러 개의 별도 `[{...}` 배열로 쪼개 내면(3줄 각각 미닫힘) unwrap 만으론 `[{...}`(미닫힘·다중배열) → 여전히 NO_JSON. `_merge_reopened_op_arrays`(문자열-인식: 구조적 `}`↦옵션 `]`↦ws↦`[{` 경계를 `},{` 로 접어 재-오픈된 배열을 하나로 병합)와 `close_unbalanced`(EOF `]` 추가)를 unwrap 결과에 합성해 전 op 복구(parse_stage 2). 병합은 정상 단일-배열 무변경(`changed=False`), content 속 `}[{` 무시(문자열-인식). **happy-path 다중배열 유실도 수정**: 모델이 배치를 여러 개의 **잘 닫힌** 별도 배열(`[{op1}] [{op2}]`)로 내면 각각 유효 JSON 이라 strict 파싱 성공하지만 `_extract_first_json` 이 첫 배열만 취해 나머지를 **신호 없이 유실**(parse_stage 1) → `_extract_op_json` 첫 성공 후에도 merge 로 재-오픈 배열을 접어 **op 수가 늘 때만**(`_op_count`) 병합 채택(drift→parse_stage 2). 보수적: `}…[{` 경계에서만 발화, trailing `</think>`·배열 사이 텍스트는 미병합("첫 배열 우선" 방어 유지). **strict=False 폴백 (실전 1781213377 `complete` 거부)**: 모델이 대용량 `result`/`content` 마크다운을 `\n` 이스케이프 없이 **리터럴 개행/탭(control char)** 으로 내면 strict `json.loads` 가 "Invalid control character" 로 거부(echo/터미널엔 `\n`과 리터럴 개행이 동일 렌더라 안 보임) — `_extract_op_json` 의 **마지막 폴백으로 `_extract_first_json(strict=False)` 재파스**해 구제(parse_stage 2). brace 스캐너는 이미 문자열-인식이라 무관, `json.loads` 의 strict 만 완화 → valid/escaped JSON 은 stage 1 그대로, control char 만 stage 2 로 복구(신호 유지). strict=False 는 control char 만 허용 — 진짜 깨진 JSON(값 누락 등)은 여전히 None(가짜 op 강제 안 함). 헤더-없는 경로·`## Action` 본문 둘 다 적용. **미닫힘 괄호 EOF 닫기 (실전 NO_JSON 지배 shape — 세션 1781336790, 캡처 3/3)**: 모델이 멀티-op 배열을 완결해 놓고 닫는 `]` 만 빠뜨리면 `_extract_first_json` 이 depth 0 복귀 못 해 None → 최후 폴백으로 `_json_repair.close_unbalanced`(문자열-인식 깊이 스택)로 EOF에 닫고 strict→strict=False 재파싱, valid 면 채택(parse_stage 2)·6 op 전부 보존. truncation(미종료 문자열 등 더 깊은 깨짐)은 괄호만 닫아선 파싱 실패 → None 유지(진단+재시도). react `repair_json` 재사용 안 함(그건 첫 `{}`만 잡아 5/6 op 유실 — 배열-인식 닫기가 정답). **따옴표 하나 누락 수리(`repair_value_quotes`)**: `"path": mgt.c"`(앞)·`"path": "mgt.c}`(뒤) 같이 문자열 따옴표 한쪽이 빠진 경우 `_extract_op_json` 최후 폴백에서 에러-위치 가이드로 복구(close_unbalanced 와 합성, bail-if-invalid; bare keyword·EOF-truncation 은 안 건드림). **under-escaped 백슬래시 복구(`fix_invalid_escapes`)**: 모델이 raw-string regex(`[^\s]`)·char class(`[\x00-\x1f]`)·Windows 경로를 한 번만 이스케이프해 strict/strict=False 둘 다 `Invalid \escape` 로 거부하는 **실측 지배 backslash-heavy 실패**(diag_2 7899자·diag_3 3964자 복구)를 stage-2 에서 구제 — 고립 백슬래시를 두 배로 재파싱하고, close_unbalanced 와 합성(under-escape+`]` 누락 동시). 정상 JSON 은 `changed=False`(무변경)라 clean 경로 무영향, bail-if-invalid 로 틀린 추측 채택 불가. 실측: backslash-heavy 3/9→4/4. **여분 닫힘 드롭(`drop_unbalanced_closers`)**: 모델이 op 닫는 중괄호를 중복해 `[{...}}]` 로 내면(세션 1783001191, 27B shell op `}}]`→NO_JSON 실측) close_unbalanced 의 거울로 매칭 안 되는 closer 를 드롭하고 재파싱(close_unbalanced 와 합성해 over-close+미닫힘 동시). 문자열-인식이라 content 속 괄호 무변경, bail-if-invalid. **종료=명시적 `complete` op** (`{"action":"complete","result":...}`, 검증된 prefix_md/react 모델). `multi_op=True`/`exposes_complete=True`/`thought_required=False`/`action_required=False` — prefix_md 패리티 + multi_op. no-op 턴은 canonical 로 NO_ACTION 넛지지만 **`prose_completion` override (v7.14)**: 액션 흔적(`[{`/`{"`/`"action"`/`action=`) 없는 **순수 산문**(자연어 최종답변인데 complete op 누락)은 그 산문을 complete result 로 수용(넛지 왕복 절약); 깨진 액션 잔해·빈 출력은 None → NO_ACTION 넛지(삼키면 조기종료). bakeoff 35B ~460런 실측: no_action 자연어=전부 최종답변(C), 중간서술(B)=0. **`parse_turn` override**가 본 경로(ParsedTurn: N ops, terminal 항상 False); `## Input` 잔재 strip·bare-object=1op·actionless-op 보존(infer)·헤더없는 op JSON=work 유지. **`## Thought` 헤더 누락 보정(`_split_sections`)**: `## Action` 은 있는데 `## Thought` 헤더가 없으면 그 앞 prose 를 thought 로 회수(예전엔 drop→None) — 헤더 빠뜨린 reasoning·오타 헤더 구제. `parse` 는 1st-op 단수 투영(ABC). **history 직렬화 override**: ops 레코드 `{role, thought, ops:[{action, action_input}]}` — render 가 wire 모양 재방출(round-trip). **`serialize_terminal_for_history` override** 로 complete 턴도 `{ops:[{complete}]}` 동질 모양(과거 complete 만 단수 `{action}` 으로 새던 불일치 수리). sanitize_thought 는 prefix_md 동형 + **외톨이 thinking 태그 strip(`_ORPHAN_THINK_TAG`)**: thinking 학습 모델이 visible thought 에 흘린 짝없는 `</thinking>`(여는 태그는 reasoning 채널이 소비; 세션 1782027249 NO_JSON 동반 지배)를 save-time 에 제거 — 파싱은 `## Action` JSON 따로라 무영향, prior 재주입·렌더 cosmetic 청소. json_fc origin 한정(react 미적용 — 발생 origin 에만). is_degenerate 는 prefix_md 동형. **`diagnose_syntax_error` override**: `_split_sections` 로 `## Action` 본문(op 배열) 추출 후 `describe_json_error` 위임(헤더 없으면 전체 텍스트 — 미닫힘 배열도 위치 표시). **2026-06-11 기본 전환** (Phase-2 95.2%=react + 실전 150턴 0.7% 검증) — `DEFAULT_WIRE_FORMAT`. **format_rules 능동 배치 유도(B, DESIGN §6)**: 독립 op 를 한 턴에 묶도록 결정 휴리스틱+3-op read 예시로 steering — 의존-분리·중첩 금지 두 가드레일 동등 비중 유지. read_file 인라인 가이드에도 same-turn 배치 힌트. **종료 모델 변경 (DESIGN Exp 8)**: 원래 thought-only 종료 + ready_for_review 게이트였으나 마무리 버그 class(false-terminate, NO_JSON 종료-전환, 빈 `[]`, 리뷰지시문 불일치로 deliverable 폐기)를 누적 → `complete` 부활로 origin 수리 + lenient-terminal·게이트(`_finish_terminal_turn`/`_terminal_reviewed`) 제거. ready_for_review 도구는 이후 v4.4.0 에서 제거(사용률 0).
│   └── xml_fc.py            (619, v5.20.1)  **XmlFcFormat — 태그-파라미터 function call (멀티-op, docs/multi-wire-format/PHASE2.md)** — `<tool_call><function=X><parameter=k>v</parameter></function></tool_call>` 블록 반복, 종료=명시 `complete` op. **2026-07-17 Qwen bakeoff 실측(PHASE2 §8): 27B=json_fc 동등(바인딩 가능) / 35B-A3B=json_fc 우위(비권장)**. **lenient 구제(v5.20.1, `_parse_lenient`)**: 35B 실측 지배 shape(0-op 의 83%) — `<function=X>`→`<X>`·`<parameter=k>`→`<k>` 로 붕괴한 tool-name 태그 변종(키-이름 closer `<parameter=k>v</k>`·closer 생략 혼합 포함)을 strict 0-op 일 때만 라인-단독 `<TOOLNAME>`(등록 도구명, 매 파스 재조립=MCP 커버) 앵커로 재조립 → stage 2 drift. 라인-앵커가 산문 인라인 언급 오인 차단, 캐노니컬 emission 은 이 경로 미진입(bail-safe), 구제 턴 prior 는 캐노니컬 shape 재렌더(B→C)로 자기 교정. json_fc-회귀 누출(0-op 의 17%, foreign-format 첫 실측)은 범위 밖 — Phase 3 소관. **파라미터 값=raw 텍스트** (JSON 아님): write_file content·complete result 의 literal-control-char/under-escape 실패 클래스가 구조적으로 소멸 — json_fc 가 쌓은 JSON 수리 기계 대부분이 불필요. 트레이드=구분자 충돌: 값 안의 `</parameter>` 는 **lookahead 앵커**(닫는 태그 뒤에 구조 토큰이 따라올 때만 경계, `_PARAM_CLOSED`)로 방어 — 고아 closer 는 값에 보존, 최악 케이스(값이 진짜 `</parameter>\n<parameter=` 시퀀스 포함)만 잘못 잘리고 도구 에러로 표면화(조용한 오염 없음). **타입=스키마-주도 강제(`_coerce_params`)**: 도구 스키마 non-string param 만 JSON parse(strict=False), 실패=raw 유지(진단은 기존 A5 경로 — 파서가 검증 중복 안 함), string/미선언=항상 raw. **thought=첫 구조 토큰 앞 산문(D4)** — `<think>` 는 wire 슬롯이 아님(provider+stage 0 두 층이 격리하는 CoT 채널; 요구하면 파서 도달 전 소실). 파서 3-단계: 정상(stage 1) / 수리(stage 2 — bare `<function=` 래퍼-생략 관용, EOF 미닫힘 태그·closerless 파라미터 회수, drift=래퍼/closer 계수 불일치) / 보존(빈 `<function=>` 이름이어도 파라미터 op 유지 — infer/NO_ACTION echo 재료). 블록 스타일 값=여닫이 개행 1개씩 트림(`_trim_block`) — render 의 멀티라인 블록 스타일과 대칭(round-trip). **lenient/hybrid 값 무결성(v7.11.4, 감사 발견 data-loss 수리)**: `_extract_params_lenient` 의 값 종료가 값 속 임의 `<tag>` 토큰(HTML content·`grep "<pat>"`·sed 식)에서 끊겨 content 빈 값+phantom param 을 만들던 것 → **자기-closer(`</KEY>`/`</parameter>`) 최근접 우선**, 부재 시 폴백(라인-선두 오픈/구조 닫기/라인-끝 임의 closer). 같은 줄 다중 param 상호-삼킴도 자기-closer 로 소멸. **키-이름 closer(v5.22.1)**: strict `_PARAM_CLOSED` 가 `</parameter>` 외에 백레퍼런스 `</KEY>` 수용 — 실전(board 세션) 35B 가 캐노니컬 블록에서 `<parameter=path>…</path>` 로 닫아 미닫힘-복구가 closer 를 값에 포함(오염 경로 ENOENT ×3)했던 수리; lenient 의 아무-이름 closer 처리의 strict 대칭. history 레코드=json_fc 와 동일 `{role,thought,ops:[…]}` shape(cross-format parity 테스트 고정, `iter_record_ops` 호환), legacy 단수 레코드=base 폴백(포맷-전환 resume). `multi_op=True`/`thought_required=False`/`action_required=False`/`exposes_complete=True`(json_fc 동형 플래그), `provider_call_kwargs={"json_mode": False}` 무조건(JSON-object 모드=선두 `{` 강제→태그 envelope 불가). 자체 `_FORMAT_RULES` 전면 override(positive 규칙 — json_fc 의 HTML-태그 금지 조항 같은 negative 나열 없음, 이 포맷의 본질이 태그) + 배치 유도 문구 json_fc 동형. is_degenerate=빈 `<tool_call>` 골격 반복 ≥2. sanitize_thought=고아 think 태그(thinking_tags 공용 정규식)+구조 센티널 라인 strip. D6=관찰 되먹임 현행 유지("Observation:" 산문 — loop 소유, 랩 훅 신설 없음).
├── recovery/                       Robust Harness Recovery Layer (docs/robust-harness/DESIGN.md)
│   ├── __init__.py                 primitive·detector·observability 재export (common_recovery / wf_recovery는 호출처가 import — 패키지 자체 format-agnostic 보존)
│   ├── common_recovery.py   (~65)  WF-agnostic Intervention factory — `format_action_loop_intervention` (B1). 모든 plugin이 같은 텍스트를 봄. 새 wire-format plugin 추가 시 0 변경
│   ├── wf_recovery.py       (~108) WF-aware Intervention factory — `format_no_json_retry` (A1a), `format_no_action_retry` (A3). plugin의 framing/reminder/static fallback 사용. WF 의존이 한 파일에 모여 audit 용이. **`format_no_json_retry(syntax_error=…)`**: 옵셔널 구문 진단(`diagnose_syntax_error` 결과)을 framing 다음 줄에 끼워 *어디서* 깨졌는지 노출 + primitives 에 `diagnose_json_error` 추가. 미지정(기본 None)이면 메시지·primitives bit-for-bit 불변(기존 호출/테스트 보존). ReAct-only NO_THOUGHT recovery는 `ReActFormat.format_no_thought_retry` 메서드 (plugin = boundary)
│   ├── detectors.py         (~250) 감지기 모음. stateful: `ActionLoopDetector` (B1, turn 간 (action, args) 추적). stateless: `detect_unknown_tool` (A4), `detect_schema_mismatch` (A5, `validate_tool_input` wrap), `detect_nested_envelope` (A6, complete 결과의 이중 래핑 감지 — 관찰 전용), `detect_thought_missing` (A7, action 있고 thought 없음 — mimicry-strengthening loop trigger; loop이 `wire_format.thought_required` 가드 후 호출. `complete` 액션은 제외 — 최종 답이라 next-turn 의무 없음, Phase 2 bakeoff 2026-05-18에서 27b prefix_md complete_direct 5/5 recovery loop 해소 측정).
│   ├── intervention.py      (~30)  `Intervention` dataclass — primitive 합성 결과 (message + 적용된 primitive 이름)
│   ├── observability.py     (167)  `TurnRecorder` — 세션별 `turns.jsonl` 추가-only writer; `TurnRecord` 스키마(model, timestamp, parse_stage, failure_signal, primitives_applied — timestamp 가 row 정렬; 구 `seq` 는 run-local 충돌로 제거). FAILURE_* 라벨 9종 (NO_JSON / NO_OUTPUT / NO_ACTION / NO_THOUGHT / UNKNOWN_TOOL / SCHEMA_MISMATCH / NESTED_ENVELOPE / ACTION_LOOP / **DEGENERATE**=wire shape 반복 runaway, 라벨만·dispatch 진행)
│   └── primitives.py        (~109) format-agnostic 회복 primitive (`echo_prior_output`, `probe_progress`, `restate_task`) — provider/모델/채널/wire format 이름 모름. ReAct-shape constraint reminders는 ``ReActFormat`` 가 소유
├── default_models.json             패키지 기본 모델 정의 (6개 모델)
├── hooks/                          Hook 시스템 (Python + Shell 라이프사이클 훅)
│   ├── __init__.py          (24)   shell hook API re-export (하위 호환)
│   ├── shell.py             (236)  Shell hook (PreToolUse/PostToolUse/PostToolUseFailure)
│   ├── events.py            (53)   11개 이벤트 상수 + EVENT_TO_FUNC 매핑
│   ├── context.py           (145)  HookContext (messages 조작, system prompt 주입, MCP 메모리, 도구 제어)
│   ├── loader.py            (88)   Python hook 파일 스캔/로드 (.agent-cli/hooks/*.py)
│   └── runner.py            (95)   HookRunner (이벤트 발화, Python→Shell 순서 실행)
├── input_history.py         (174)  readline/gnureadline 설정 + 채팅 히스토리 영속화 (CJK 지원, paste/IME 디코드 오류 방어)
├── verbose.py               (27)   공용 verbose 플래그 + debug_log (providers가 loop을 역참조하지 않도록 추출)
├── loop/                    (~3149 총, v4.43.0 패키지化 완결) AgentLoop 패키지 — C1(Option 3 협력객체) 파일 물리 분할. 모듈 배치=소유권; `__init__` 이 기존 `from agent_cli.loop import X` 표면 전부 재수출(외부 소비자 무변경). **테스트 monkeypatch 는 사용하는 서브모듈을 patch**(재타게팅 완료: render_step→dispatch/core, _execute_tool→tool_bridge, tool_delegate→subagent/oneshot(함수-내부 import DI seam), render_system_prompt_snapshot→llm, _MAX_OVERFLOW_RETRIES→llm; run_loop 은 소비자가 call-time import 라 __init__ patch 유효 유지).
│   ├── state.py             (95)   LoopConfig(frozen 불변 배선)·LoopState(공유 가변 7필드)·센티널(_CONTINUE/_RETRY/_NOT_HANDLED)
│   ├── prompt.py            (104)  SystemPromptSvc + build_inspector_sections
│   ├── tool_bridge.py       (418)  ToolBridge(hooks·invoke·RunContext·관찰 seam) + _normalize_input(소비자가 bridge 뿐이라 이주)
│   ├── llm.py               (309)  LLMCaller(_call_llm·압축요약콜) + _build_token_stats + _MAX_OVERFLOW_RETRIES. 압축 목표 비율은 상수 대신 `self.ctx.compaction_ratio`(라이브, web 슬라이더 반영)를 매 콜 읽음(flow1 예방 target·flow2 overflow retry 양쪽, 5.14)
│   ├── dispatch.py          (~1325) TurnDispatcher(+_op_* 4헬퍼) + 관찰조립·ask 자유함수군(_append_observation/_handle_ask/_extract_questions/_try_echo_as_final/…). **산문-only 종결 (v7.14, 모든 wire-format 단일 지점)**: `not turn.ops and parse_stage>=1` 이면 thought 렌더 前에 `wire_format.prose_completion(turn)` 호출 — 순수 산문(자연어 최종답변)이면 synthetic `complete` op(result=산문)로 `_op_complete` 종결(넛지 왕복 절약), 깨진 액션 잔해·빈 출력은 None → 기존 NO_ACTION 넛지. history 는 `replace(turn, thought=None)` 로 **thought 없이 complete(result)만** 기록(재공급 시 산문-only-no-op 패턴 재강화 방지 — self-reinforcement 안전). **foreign-format 구제 (Phase 3, v5.21.0)**: parse 직후 0-op 이면 `wire_formats.try_foreign_parse` 1회 — 성공 시 그 turn 으로 정상 진행, 라벨 `FAILURE_FOREIGN_FORMAT`+primitives `foreign_parse:<src>`, 직렬화는 `outcome["corrected_record"]`(ops shape) 경유(단일-op `_dispatch_op` corrected 우선순위: foreign>inference, 멀티-op `_flush_op_results(corrected_record=)` 스레딩) → prior 는 바인딩 포맷 캐노니컬 재렌더 = 누출 raw 재공급 없는 자기 교정
│   ├── skill_invoke.py      (168)  _handle_run_skill(깊이/사이클 가드 포함). **skill=main 워크플로우이므로 agent_registry 상속 (v7.16.0, v7.17.0 배선 통일)**: v7.16.0 은 loop 내부 경로(dispatch→_handle_run_skill→execute_skill)만 명시 배선해 **사용자 `/skill` dispatch 2곳(main.py `_dispatch_skill` — web worker·CLI run)이 누락**, spawn 이 여전히 "main-session only" 거부되던 사고. v7.17.0 이 **main registry 프로세스 슬롯**(agents_live `set_main_registry`/`main_registry`, 프로세스=1세션 전제)으로 통일 — run/web 이 생성 직후 등록(run 은 생성을 dispatch 앞으로 이동), `execute_skill(agent_registry=_INHERIT)` sentinel 이 **미지정→슬롯 자동 상속**(사용자 슬래시 경로+미래 경로 전부 자동), **명시 전달(None 포함)→그대로**(서브에이전트 루프의 dispatch 는 cfg.agent_registry=None 명시 전달 → run-only 경계 보존). skill 서브루프가 `has_agent_registry=True` 라 agent 도구 **full(spawn/request/kill) 노출+실행**. **입구도 통일 (v7.17.0)**: run 초입의 `/skill` fast-path(_dispatch_skill 직접 호출 — registry 미배선·not-found 폴스루로 web 과 갈리던 잔재)를 제거하고 web 의 route_one 과 같은 `try_dispatch_agent_or_skill` 공용 입구로 — `_dispatch_skill` 직접 호출은 이제 그 내부 1곳뿐, not-found 는 양쪽 다 소비+표시(/오타가 LLM 쿼리로 안 샘), run 에서도 `/skills` 리스팅 동작. **depth 와 spawn 권한 분리**: depth 는 skill→skill 재귀만 제한, spawn 은 registry 유무로 결정 — skill(registry 있음)=full, 일반 서브에이전트(registry 없음)=run-only(SUBLOOP). orchestrate skill 이 워커를 spawn·조율 가능(전엔 registry 가 skill 경계에서 드롭돼 spawn 이 no-op). skill 이 spawn 한 워커는 main registry 에 등록돼 skill 종료 후 main 이 인계.
│   ├── core.py              (835) AgentLoop 오케스트레이터(라이프사이클·시그널·큐주입·_execute_turn 지휘 + 위임/property 표면). **wire_format None 폴백은 ctx-우선** (v5.19.0 G2 수리): `ctx.wire_format if ctx else 전역 기본` — 종전엔 무조건 전역 기본이라 비기본 포맷 세션의 서브에이전트가 ctx(히스토리 렌더)/loop(파서·프롬프트) split-brain (I2 불변식: cfg.wire_format ≡ ctx.wire_format)
│   └── run.py               (106)   run_loop 진입점. **C1 분해 3/3 (Option 3 협력객체, v4.40.0~v4.43.0)**: 불변 배선 ~24필드를 **`LoopConfig`(frozen)**, 공유 가변 상태 7필드(query/author·messages·turn·task_log·interrupted·stop_event)를 **`LoopState`** 로 격리 — 실측상 클러스터-간 공유는 이 7종뿐(나머지 가변 필드는 한 클러스터 전유라 PR-2/3 의 소유 객체로 이동 예정). 기존 `self.X` 표면은 **property 브리지**(config=읽기전용, state=RW)가 유지해 메서드 본문·테스트 무변경. **PR-2 승격 완료(교차호출-0 실측 클러스터 2종)**: `SystemPromptSvc`(sections/system 소유 — 단일진실 join 불변식을 클래스 경계로)·`ToolBridge`(hooks·invoke·RunContext·`_tool_observation` seam + 전유 상태 `_oversized_cap`/`_run_ctx_cache`/`recent_tool_history` 소유; cfg/state/ctx/provider 4종 명시 주입, **AgentLoop 없이 단독 생성 가능** — 단위테스트 증명). AgentLoop 는 thin 위임+property 로 기존 표면 유지. **PR-3 승격 완료**: `TurnDispatcher`(턴/op 디스패치·가드 B1/A4/A5·배치·recovery + 전유 `loop_detector`; cfg/state/ctx/tools/recorder 주입) — `_dispatch_op` 389줄은 라우터 + `_op_complete`/`_op_ask`(질문 없으면 `_NOT_HANDLED` 폴스루)/`_op_run_skill`/`_op_execute_tool` 로 분해. `LLMCaller`(스트리밍 콜·오버플로 재시도 카운터·압축 요약 콜 + 전유 `overflow_retries`; cfg/state/ctx/provider/prompt 주입). `_CONTINUE`/`_RETRY` 는 모듈 상수로 승격(인스턴스 별칭 유지). review 헬퍼는 v4.43.0 에 `review.py` 로 이관됐다가 auto-review 제거(v5.3.0)와 함께 모듈째 삭제. AgentLoop=오케스트레이터(라이프사이클·시그널·큐주입·`_execute_turn` 지휘)+위임/property 표면. 패키지化는 v4.43.0 에서 완결(위 트리) (wire_format plugin 통합 — parse_turn / system prompt / recovery builders / NO_THOUGHT 가드 / messages 버퍼·history.jsonl 저장의 assistant 표현, token-budget compaction + FIFO fallback, hook, streaming, nested depth rendering, failure-grounding retry). **과대 출력 캡 (`_tool_observation`, `_oversized_cap=context_window//10`)**: 도구 결과→관찰 seam 에서 `Tool.render_observation`(본문 렌더)+`Tool.apply_oversized_cap`(기본 True)을 consult, cap 초과면 **`Tool.render_oversized(result, args, *, body, tokens, ctx)`** 로 치환(도구가 over-cap 응답을 소유; 기본=`base.default_oversized_nudge` 제네릭; 도구 없으면 fallback 도 이 함수). per-call 컨텍스트는 `_run_ctx()` 가 만든 **`RunContext`**(`session_dir`·`oversized_cap`·`tools_available=frozenset(self.tools_list)`; 세 입력 모두 init 후 불변이라 **lazy 1회 생성 후 캐시**, v4.39.0 — per-call 가변 필드가 생기면 캐시 제거) 로 전달 — depth-stripped 도구는 안내에서 빠짐(read_file 이 run 팬아웃을 depth 한계 서브에이전트에선 생략). 같은 `RunContext` 가 실행 seam(`_invoke_regular`→`_execute_tool`)에도 흘러 두 표면이 동형. messages·ctx 양쪽 전에 1회 — 일관. 1508/1124 두 디스패치 지점이 이 헬퍼 경유. **시스템 프롬프트 단일 소스**: `_system_sections`(이름 붙은 섹션 리스트)가 진실이고 `self.system`은 항상 join 파생 — hook 섹션 적용(`_apply_system_sections`, `Hook: <title>` 항목으로 교체-적용) 후에도 Inspector 뷰와 LLM 수신 문자열이 구조적으로 일치. `_call_llm`이 매 턴 `render_system_prompt_snapshot(build_inspector_sections(self._system_sections, self.ctx), turn)` 으로 renderer 에 전달(CLI no-op, web 은 저장만). **`build_inspector_sections`** 는 system 섹션 뒤에 compaction 주입 컨텍스트(`ctx.summary`·`ctx.file_list`)를 "⊙ Compaction summary / Files touched (user-injected)" 라벨 섹션으로 덧붙임 — 이들은 `get_messages()`가 시스템 프롬프트 직후 `role=user` 로 주입하는 내용이라 `self.system`(=`_system_sections` join) 에는 없지만 컨텍스트 윈도우를 점유하므로 Inspector 가 가시화. **새 list 반환 — `_system_sections` 불변**(self.system 파생원 보호). **통일 turn 디스패치 (멀티-op 3a — docs/inputs-array-schema/DESIGN.md §6)**: `_handle_text_path` 가 `wire_format.parse_turn()`(ParsedTurn) 기반 — per-op `infer_action`, turn-level 라벨링(terminal 은 실패 아님). **턴경계 메시지 주입(web 멀티유저 — 단일 라우팅)**: `run()` while 상단에서 `_inject_queued_messages()`가 `dequeue_user_message` 로 큐된 user 메시지 1개를 꺼내 user 카드 echo 후 — **run-starter 와 동일하게** `route_message(text)` 콜백으로 라우팅: 명령(`/sh`·`/compact`·`@<profile>`·`/skill`)이면 실행(결과는 공유 ctx 반영; `@<profile>` 은 모델 agent run 과 동일한 `tool_delegate` 기계장치에 수렴), 명령이 아니면 `_add_user_message(text, author)` 로 스티어링 주입. `_add_user_message` 는 setup(run-starter)과 **공유하는 단일 헬퍼** — `[author]: text` 라벨 + `task_log` 누적 + ctx.add. CLI 는 두 콜백 None=무동작. (`query_author`=run-starter 닉네임; 과거 `query_label` 별도 인자·라벨링 중복·injected-무라우팅 비대칭은 제거 — 설계 `docs/intake-unification/DESIGN.md`. 이전엔 중간 주입 `/sh`·`@agent` 가 라우팅 없이 리터럴 chat 으로 샜음.) `restate_task`(B1)는 `self.query` 대신 **`_task_text()`(첫 쿼리+주입 전체)** 인용. 디스패치는 `_dispatch_turn`(turn 가드: NO_THOUGHT → thought 렌더 → no-ops → ops 배열 순회) → `_dispatch_op`(per-op: complete/ask/run_skill/B1/A4/A5/tool 실행 — 기존 단수 본문 그대로, 모든 분기가 return 이라 1-op 에서 종전과 동일 동작) → `_recover_unparsed`(NO_ACTION/NO_JSON 공용 recovery, no-ops 와 action-없는-op fall-through 두 곳에서 호출). **종료는 명시적 `complete` op**(`_dispatch_op` 의 complete 분기, result=최종 답변) — json_fc 도 동일(prefix_md/react 모델). thought-only/0-op 턴은 `not turn.ops` → `_recover_unparsed`(NO_ACTION 넌지), 완료 아님. (DESIGN Exp 8: 원래 thought-only 종료 + `_finish_terminal_turn`/`_terminal_reviewed` ready_for_review 게이트였으나 마무리 버그 class 누적으로 제거 — complete 부활로 origin 수리; ready_for_review 도구는 v4.4.0 에서 제거.) B1 loop detector 는 N-op 에서도 per-op observe — 같은 (action,args) 3연속(턴 경계 무관)이면 발화, 한 턴 내 2중복은 무발화(threshold 3 의미론 그대로). **N-op 실행 (3b)**: 1-op 은 legacy 경로(자체 observation append — 종전과 동일), N-op 은 순차 실행+축적 → `_flush_op_results` 가 per-op `[i/N] tool — OK/FAILED` 헤더의 **합성 observation 1개** append (any-fail ⇒ success=False, 모델이 실패 op 재시도; 합본 `tool` 라벨은 `_combined_tool_label` 가 연속 동-도구를 `tool×N` 으로 run-length 압축 — `shell+write_file×12`, 줄넘침 방지·순서 보존). 턴-종료성 op(complete/run_skill)는 분기 전에 축적분 flush(시간순 보존); 가드(B1/A4/A5) 발화 시엔 intervention 후 flush. **`ask` 는 턴-종료가 아님 — 사용자 응답을 observation 으로 내고 일반 도구처럼 accumulate** 하므로 ask op 여러 개가 read/shell 배치처럼 묶임(각자 순차 프롬프트 → 합성 observation 1개). 단일-op ask 는 자기 observation 직접 append(종전 동일). **병렬 batch (`_dispatch_parallel_batch` — 5.0.0 mode-aware)**: 연속된 같은 `Tool.parallel_safe=True` 도구 op 런(≥2)은 순차 대신 **동시 실행**으로 묶임 — 수집 단계가 op 마다 `Tool.parallel_batchable(action_input)` 을 추가 확인해 **agent 는 `mode:"run"` op 만** 배칭(상주 모드가 섞인 턴은 순차 — spawn/request 는 즉시 반환이라 병렬 이득도 없음). run 런은 각 flat op 을 `ToolBridge._run_spec` 으로 task 스펙화해 `{tasks:[...]}` 조립→run 엔진(`tool_delegate`) 한 번 호출→`_run_parallel`(스레딩) — 프롬프트의 "여러 run op = 병렬" 약속을 실제로 참으로 만듦(N-op 루프는 본래 순차이므로). lone parallel_safe op(런 길이 1)은 normal per-op 경로(B1/A4/A5 가드 유지). mutating 도구(parallel_safe=False: write/edit/shell)는 항상 순차 — 순서가 정확성 보장(write→edit 같은 파일, mkdir→touch). 내부엔진 없는 미래 read-only parallel_safe 도구용 generic thread-pool 슬롯은 `NotImplementedError`(미배선 — agent 만 opt-in). **같은 파일 edit 배치 (`_dispatch_edit_batch`, parallel batch 의 형제 경로)**: 연속된 같은-path `edit_file` op 런(≥2)은 `apply_edits_batch(path, edits)`(순수함수) 로 묶여 **원본 1회 read 기준 bottom-up 적용·overlap 사전거부·all-or-nothing** — 뒤 op 의 ref 가 앞 op 의 줄 이동으로 stale 되는 것을 제거(두정 보고 "Hash mismatch at line N"). parallel batch(동시) 와 달리 **순차 의미를 유지하되 단일 base** 라 mutating 이어도 안전. 결과는 누적의 한 unit(`{tool_name, observation, success}`) → `_flush_op_results` 합본. 단방향 호출(loop→edit_file)이라 강결합 없음. 런 길이 1·다른 path·비연속은 per-op. tool-exec 직전 `wire_format.multi_op` 면 `Tool.wrap_single_op` 호출 — **모든 builtin 도구가 flat-native(Step 3)라 wrap=identity**(과거 batch 도구의 flat→캐노니컬 변환은 소멸). **action 카드 렌더는 `op.action_input`(모델 실제 emission)을 표시** — 모든 wrap 이 identity 라 dispatch 입력과 동일하지만, 렌더는 명시적으로 pre-wrap 값을 써서 미래 prefixed/batch 도구가 다시 생겨도 history.jsonl/resume-replay(raw 저장)와 일치 유지. dispatch 는 `tool_input`, 카드는 pre-wrap. 생성 시 `ctx.set_compactor(self._llm_compact_summarize)` + `ctx.set_recorder(self.recorder)`로 compaction 진입점을 ContextManager에 주입; `compaction_enabled=False`면 미주입 → FIFO만 동작. **Tool dispatch safety net**: `_dispatch_tool_with_hooks` 가 invoke 단계 (`_invoke_regular` / `_invoke_agent`) 를 try/except Exception 으로 감싸 unhandled exception 을 `ToolResult(False, error="Tool 'X' raised … retry or different approach")` 로 변환 → post-hooks + observation 정상 흐름, LLM 이 다음 turn 에서 retry 결정 가능. `KeyboardInterrupt` / `SystemExit` 는 의도적으로 통과시켜 Ctrl+C 종료 보장. 전체 traceback 은 `_debug_log` 로 보존, LLM observation 은 짧게 유지. **Output-truncation guard**: `_execute_turn` 가 `response.stop_reason == "length"`(모델 출력 한도 도달) 면 그 응답의 action 을 **dispatch 하지 않고** `_on_output_truncated` 로 `OUTPUT_TRUNCATED_NOTICE` observation 기록 → 잘린 content(write_file)·명령(shell)·답(complete)이 불완전 실행되는 것 방지, 모델이 다음 turn 에 더 작은 단위로 재시도. (이어쓰기 continuation 은 후속.) **Mid-generation interrupt**: `_call_llm` 이 provider 에 `interrupt_check=self._interrupt_check`(= `stop_event.is_set()`) 를 넘겨, Ctrl+C/web stop 이 **생성 스트리밍 도중**이면 provider 가 즉시 stream 을 끊고 `stop_reason="interrupted"` 반환 → `_execute_turn` 가 parse/dispatch **전에** 이를 감지해 `_on_interrupt()` 로 직행(미완성 partial 은 ctx 에 안 들어갔으므로 폐기, interrupt notice 만 기록). 생성이 이미 끝난 뒤(스트림 완료)면 `"interrupted"` 가 아니라 정상 흐름 → 도구는 부작용 보호를 위해 끝까지 실행되고 turn 경계에서 멈춤(graceful "finish current step"). `stop_event` 는 skill(`_handle_run_skill`)·agent run(`tool_delegate`, 병렬 worker 스레드 포함)로 그대로 전파되고 interrupt 로직은 공유 `AgentLoop` 에 있어, 한 번의 interrupt 가 모든 중첩 loop 의 in-flight 생성을 끊음(각 병렬 worker 는 자기 스레드에서 자기 스트림을 닫음). **Unified call-depth ceiling**: `__init__` 가 `depth >= max_depth` 시 `agent` AND `run_skill` 둘 다 tools_list 에서 제거 (대칭). `execute_skill` 이 `parent_depth + 1` 전달 → skill 체인도 depth 카운트. cycle (`skill_stack` / `agent_stack` 검사) + depth 한계 위반 시 `recovery/recursion.py` 의 actionable helper (3가지 recovery option) 로 응답. dispatch 단계 belt-and-suspenders check 가 직접 caller 도 보호. 시스템 프롬프트 `## Execution Context` 가 `depth N/M` 표시 + 한계 도달 시 명시 (KV cache: section 위치 그대로 — 한 loop 내 depth 불변이라 영향 0).
├── render/                         플러그인 가능 렌더링 + 사용자 입력 시스템
│   ├── __init__.py          (~270) 렌더러 디스패치 + load_renderer_by_name + render crash 방어 + observation success 전달
│   ├── base.py              (~625) Renderer ABC + `ConfirmOption` dataclass. **C8(v4.50.0): abstract 17→9** — 출력 코어 7(header/thought/action/observation/final/error/status)+입력 계약 2(prompt_user/confirm — 위험 셸 confirm 게이트가 걸려 있어 조용한 기본값 대신 명시 구현 강제)만 필수; 디버그/장식 8종(turn_sep·raw·context_dump·spinner×2·dispatch_progress=no-op 기본, model_detected/loaded·**agent_mail_hint**=`status` 위임 기본 — `agent_mail_hint`(5.18.2)는 상주 에이전트 회신/질문 도착 힌트로, web 만 전용 `agent_mail` 이벤트로 override)은 concrete 강등 — 신규 렌더러 구현 의무 절반, 기존 minimal/web 은 전부 override 라 바이트 동일. 출력 메서드 19개 (depth, capture, group, thread_status, thinking 등) + 입력 메서드 2개 (`prompt_user` 자유 입력 — optional `context` kwarg로 pre-input 안내(예: ask 도구의 질문 블록)을 전달, `confirm` 선택지+코멘트 + optional `command`/`danger_spans` kwarg로 검토 대상 명령·강조 span 을 전달 → 렌더러가 매체별로 위험 키워드 강조) + **`can_prompt()` (기본 True)** — "지금 사용자에게 프롬프트(confirm y/n/a 또는 ask 자유입력)를 띄울 수 있나"를 렌더러가 선언; 호출자가 블로킹 전에 확인해 못 띄우면 hang 대신 refuse/기본값. **인터랙티브 읽기 추상화**: 모듈 레벨 `interactive_lock`(RLock, 모든 사용자 읽기 직렬화) + `_prompt_display_guard()`(읽기 중 출력 정리 — 기본 no-op) + `_guarded_read(read)`(락+가드로 감싼 블로킹 읽기). `confirm`·`prompt_user` 둘 다 `_guarded_read`로 읽어 서로 직렬화(한 번에 하나 → 응답 오라우팅 방지) + 표시정책 공유. **프롬프트 출처(provenance)**: thread별 `set_thread_agent`(delegate 라벨)·`note_thought`·`note_action` 기록 → `prompt_meta()`가 `{agent, reasoning, action}` 반환. confirm/ask가 "어느 에이전트가 왜(confirm은 무엇을)" 묻는지 표시. `_format_prompt_meta`는 agent 라벨이 있을 때(=delegate)만 CLI 헤더 생성(메인 에이전트는 thought/action이 인라인이라 중복 회피). 입력도 추상화에 포함해 web UI 같은 비-CLI renderer가 SSE+POST로 같은 인터페이스 만족할 수 있게. **`begin_delegate_task` / `end_delegate_task`** concrete no-op lifecycle 메서드 — CLI 렌더러는 그대로 무시(rich.Live가 자체 처리), WebRenderer만 override해서 thread→task_id 매핑 + SSE 마커 발사. `delegate.py::_run_parallel` 워커는 둘을 무조건 호출 → 렌더러 타입 분기 없음.
│   ├── minimal.py           (1043) MinimalRenderer — 유일한 번들 렌더러. **`token_usage(stats, turn, verbose)`**: 매 turn `in/out(+speed) · ctx: used/window(%) · Σout(누적) · cache` 한 줄 (`_format_token_stats`, K 단위 축약; non-verbose면 `--verbose` 힌트). **출력**: nested depth, markdown, ASCII-art talking-face streaming progress with token counter + 시간 기반 프레임 throttle + 폭 통일 패딩 + 좁은 터미널 안전망 + resize-recovery, ASCII-art thinking spinner, `FrameClock` 공유 (delegate 병렬 패널이 동일 cadence로 reuse), write_file/edit_file unified-diff 렌더링 (plain diff 를 `_colorize_diff_line` 으로 라인 첫 char 별 색상 — diff 데이터 자체는 plain), ToolResult.success 직접 전달로 정확한 ✓/✗ 표시, capture, group blocks, CJK+Ambiguous width, verbose에서 provider thinking 블록 표시. **입력**: `prompt_user`는 multiline 시 `input_history.read_rich_input` (paste + `"""..."""` 블록 지원), 단일 줄은 stdin `input()`; EOF/Ctrl+C는 호출자 정책 분기를 위해 전파. `confirm`은 첫 토큰 매칭 (key + aliases, case-insensitive), EOF/empty/unrecognized는 `default_key` 반환. **`confirm`·`prompt_user` 둘 다 `_guarded_read`** 경유 → `_prompt_display_guard` override가 활성 Live(spinner/parallel-delegate 패널)를 정지 후 읽고 재개 (워커 스레드에서 호출돼도 Rich 리페인트가 프롬프트를 안 덮음; 공유 락이 동시 stdin 읽기를 막아 워커 스레드 읽기도 안전). delegate 프롬프트면 읽기 직전 `_emit_prompt_meta_header`로 `↳ from [agent] · 💭 reasoning · ⚡ action`(ask는 action 생략) 출력. begin/end_delegate_task가 `set_thread_agent`로 라벨 설정/해제. **`can_prompt()` = stdin·stdout 둘 다 TTY** (Live/스레드 상태는 가드가 처리하므로 게이트엔 미반영). 커스텀은 `render/{name}.py`에 Renderer 서브클래스를 두면 `--style {name}`으로 로드됨
│   └── web.py               (1703) WebRenderer — `agent-cli web` 전용. **input 게이트 표면 (v7.2.0 ⓓ)**: `awaiting_input_kind()` = 대기 중 프롬프트의 kind(`"prompt"|"confirm"`, 없으면 None — `_sticky["input_required"].payload.kind` 락 하 판독; POST /api/input 게이트가 소비) + `_drain_stale_input()` = prompt_user/confirm 의 `_do()` 가 **announce(set_sticky) 직전** 큐를 비움 — announce 전에 큐에 있던 답변/abort 는 정의상 이전 프롬프트 대상(stale)이라 새 프롬프트를 자동응답하면 안 됨(연결 고갈로 적체됐다 flush 된 confirm 클릭 burst·다중 뷰어 레이스 패자가 다음 confirm 을 오염시키던 poisoning 의 2차 방어; 1차는 서버 409 게이트). **재생 버퍼 바운드 (`_EVENT_BUFFER_MAX=5000`)**: `_event_buffer` 는 `deque(maxlen)` — 장수 세션이 메모리·재접속 재생 비용을 무한 성장시키지 않음. 라이브 전달은 무영향(연결된 클라이언트는 실시간 수신), **재접속 재생만 최신 윈도우로 제한** — 넘친 경우 snapshot 맨 앞(identity 다음)에 `transcript_truncated {omitted:N}` 이벤트를 끼워 프런트가 '이전 N개 생략' 노티스 카드 표시(전체 기록은 history.jsonl 에 보존, `_persistent_count` 는 총 발행 수 유지). **1회 직렬화 (`_JsonReady`)**: `_emit` 이 단일 fan-out 지점에서 payload 를 json.dumps **한 번** 하고 dict 서브클래스(`json_str` 슬롯)에 캐시 — SSE generator(뷰어당 1개)와 재접속 replay 가 캐시 문자열 재사용(이전엔 뷰어수×재접속수만큼 같은 dict 재직렬화). 여전히 진짜 dict 라 테스트·in-process 소비자는 필드 그대로 읽음; per-connection 합성 항목(identity/sticky/viewers)은 plain dict → 서버가 접속 시점에 fallback dumps(접속당 소수). **스코프 스택+서브-스코프 동적 컨텍스트 (v4.52.0)**: `_thread_prompt_scopes`(스레드당 스코프 스택 — delegate 는 begin/end_delegate_task 가, skill 은 executor 의 `begin/end_prompt_scope` 가 push/pop; 중첩 delegate→skill 도 top 해소)로 `note_system_prompt` 스코프 결정 — **skill 중첩 루프가 main 스냅샷을 덮던 동작 소멸**(skill 이 `skill:<name>` 독립 칩). `note_scope_ctx(ctx)` 로 서브 루프의 ContextManager 를 스코프에 등록 → `scope_dynamic_sections(scope)` 가 실행 중=live on-demand, 종료 후=`_finalize_prompt_scope` 가 고정한 텍스트 스냅샷 반환(ctx 장기 홀드 방지, 시스템 스냅샷 사후-검사와 대칭) — debug 엔드포인트의 task_id 분기가 소비(main 은 server.ctx 경로 그대로). `_thread_to_task`(SSE delegate 그룹 라우팅)와 분리 유지. **`note_system_prompt(sections, turn)` override + `prompt_snapshot(scope)` + `prompt_scopes()` + `delete_prompt_scope(scope)`**: 매 LLM 콜의 시스템 프롬프트(이름 붙은 섹션)를 **스코프별 슬롯**(`_prompt_snapshots: dict[scope, snapshot]`)에 저장만(SSE 미발사 — ~16KB는 on-demand) — 섹션별 chars/est_tokens(estimate_tokens 단일 출처) 계산 포함. **스코프는 호출 스레드에서 해소** — `note_system_prompt` 가 `_thread_to_task.get(get_ident())`(`_emit` 과 동일 맵)로 자기가 main(`_MAIN_SCOPE=""`)인지 delegate 서브에이전트(task_id)인지 스스로 판별 → loop 이 identity 를 안 내려줘도 에이전트별 프롬프트가 분리 저장(loop 변경 0). `begin_delegate_task` 가 `_prompt_scope_labels[task_id]={agent,index}` 도 기록(칩 라벨 "code-analyst·1"). 서브에이전트 스냅샷은 task 종료 후에도 잔존(사후 검사) — 프런트가 `delete_prompt_scope`(✕)로만 제거, main 은 삭제 불가. `prompt_scopes()` 는 스냅샷 있는 스코프만 main-우선 나열(`GET /api/debug/prompt/scopes`); `prompt_snapshot(scope)`/`delete_prompt_scope` 가 `GET`/`DELETE /api/debug/prompt?task_id=` 의 공개 표면. 모든 Renderer emit이 (1) `_event_buffer`에 (persistent만) 누적 + (2) 활성 SSE connection의 queue에 push. **턴/툴 에러 이벤트명은 `error` 가 아니라 `turn_error`** (v5.10.3): 브라우저 `EventSource` 는 서버가 보낸 `event: error` 를 전송-실패와 **같은 "error" 타입**으로 디스패치 → `error` 로 명명하면 클라이언트 `es.onerror`(연결 점 빨강)가 발화해 정상 스트림에서도 점이 빨강 고착. `turn_error` 는 live-only(history 미기록·resume 재생 없음)라 개명이 옛 세션에 무영향. 계약 테스트=`test_web_renderer.py::TestTurnErrorEventName`. `thought()` 는 즉시 emit 안 하고 다음 `action()` / `final()` 에서 `assistant_turn` 한 이벤트로 묶음 (LLM 한 emission = 프런트 카드 한 개). `prompt_user` / `confirm` 은 `input_required` 이벤트 push 후 worker thread에서 `_input_queue.get()` blocking, POST /api/input 이 도착하면 깨움 (emit+wait를 `_guarded_read`의 공유 락 안에서 실행 → 동시 prompt/confirm이 단일 큐에서 섞이지 않음). `input_required` 이벤트에 `agent`/`reasoning`(+confirm은 `action`) 필드 첨부 → 프런트가 어느 delegate 에이전트가 묻는지 표시. begin/end_delegate_task가 `set_thread_agent` 호출. **`can_prompt()` = 항상 True (v7.8.0)** — "답이 도착할 수 있는 채널인가"이지 "지금 보는 사람이 있나"(그건 `has_live_connections`)가 아님. 뷰어 0명이어도 ask/위험셸 confirm/confine 승인이 **대기**: input_required sticky 가 status.json `awaiting_input` 으로 흘러 board 가 "답변 필요" 표시, 늦게 접속한 클라이언트는 snapshot replay 로 pending 질문을 받아 답함(다중 방 운용에서 타 방 시청 중 도착한 질문이 같은 ms 에 "(no response)" 로 붕괴하던 것 소멸). CLI 렌더러는 TTY 기반 게이트 유지(터미널 없음=답이 영원히 불가). `prompt_user(context=...)` 는 ask 도구의 질문 텍스트를 `input_required.context` 필드로 그대로 전달 → 프런트가 ANSWERING 칩 옆 패널로 렌더 (스크롤 없이 질문 즉시 노출). **Sticky state registry (`_sticky` + `set_sticky(name, event, payload, position=)`)**: "단일 서버 값을 라이브 브로드캐스트 + 새 connection snapshot 재생"을 한 표면으로 통합 (옛 `_latest_ready`/`_latest_worker_state`/`_latest_token_usage`/`_latest_queue` 4슬롯 + 반복 if 를 흡수). 멤버: `ready`(세션정보 top-bar, position=**prepend** — buffer 분리로 AgentLoop 재진입 시 누적 방지, 첫 turn 전 새로고침에도 top-bar 즉시) / `worker_state`(`worker_busy`/`worker_idle` send 버튼 게이팅, `_worker_loop` 가 pop 전 idle·후 busy, SHUTDOWN 제외) / `token_usage`(raw 카운트, 프런트 포맷) / `queue`(대기 메시지). `register_connection` 이 `_sticky` 순회로 position 별 prepend/append 재조립(전부 non-persistent — buffer=history, slot=latest). NOT sticky: `viewers`(연결집합 파생)·prompt_snapshots(스코프별 on-demand). **agents 요약 (v7.10.0, v7.11.1 어휘 수리)**: `agent_roster` 도 `_STATUS_STICKY_KEYS` 멤버 — 에이전트 상태 전이가 status.json 재발행을 트리거하고, `_agents_summary_from(roster)`(dead 제외 `{alive, working, list}`)가 `agents` additive 필드로 실림. `working` 카운트 = **state ∉ {idle, dead}**(busy/waiting_ask/starting) — ★worker 실어휘에 "working" 은 없다: 문자열 비교하던 v7.10.0 코드가 프로덕션 무동작이었고(어휘 계약 테스트로 고정), "작업 중" 판정 단일 소유는 `AgentRegistry.state_is_active`. `agents_summary()` 공개 표면을 `/api/health` 도 공유(파리티). main.py idle-reap `is_active` 에 `agent_registry.any_activity()`(working ∨ inbox>0; nonlocal 지연 채움이라 None 가드) 합류 — 에이전트 작업 중 자가 종료로 백그라운드 작업이 소실되던 버그 수리(미배달 회신은 pending 미러 resume 복원이라 게이트 안 함). `_build_token_stats` 가 `in=usage.total_input_tokens`(전체 점유량 → Anthropic 캐시 적중 시에도 ctx% 정확)·context_window·누적 out 을 render-agnostic dict 로 전달; CLI/web 공통. `in_speed` 만 bare input_tokens(prefill 비캐시분), cache_read/write 별도 내역. 중첩 AgentLoop(`skill_name`/`skill_args` 세팅)에서의 header()는 무시 (sub-flow가 top-bar를 클로버하지 않도록). **다중 뷰어 (모두 동등)**: `register_connection` 은 연결을 `_connections` 에 append + 스냅샷 맨 앞에 `identity` 이벤트(conn_id) prepend — controller/observer 구분 없이 모두 입력·큐 가능. `unregister_connection` 은 `__close__` sentinel push(SSE generator 즉시 깸). **접속자 수(`viewers`)**: join/leave 시 열린 연결 수를 브로드캐스트 — 참여 conn 은 자기 snapshot 으로(큐 오염 방지), 기존 conn 들은 큐로 받음. 프런트 헤더 `#viewers` 에 `👁 N` + 닉네임 로스터. **`queue_state(pending)`**: 대기 메시지 큐를 `queue` 이벤트로 브로드캐스트(+`_latest_queue` slot 으로 재접속 복원). **`nickname_for(conn_id)`**: 큐 메시지 닉네임 attribution. **`set_nickname(conn_id, name)`**: 사용자 지정 닉네임(trim·24자, 빈값 거부) → 로스터 재브로드캐스트. **`shutdown_all_connections()`** — 모든 active connection에 `__close__` sentinel을 일괄 push하고 리스트를 비움; FastAPI lifespan shutdown 훅과 main.py `finally` 양쪽에서 호출되며 idempotent (두 번째 호출은 빈 리스트 위에서 no-op). **`replay_from_history(ctx)`** — `--resume` 시 worker 시작 + SSE 연결 이전에 한 번 호출, `ctx.get_raw_messages()`를 walk해 user/tool 메시지는 `push_user_message` / `observation` 으로, assistant 메시지는 **`ops` 모양**(두 wire format 이 `complete` 포함 모든 턴을 저장하는 형태)을 walk 해 op마다 `thought+action`(complete면 `thought+final`)으로 재방출(`_replay_assistant_op` 헬퍼; thought 는 1회 held → 첫 op 카드에 실림). 레거시 단수 `{action,action_input}` 모양 + raw content-only(final 카드)도 호환. → 새 클라이언트의 snapshot replay가 자연스럽게 이전 turn을 복원 (transient stream_chunk/status/spinner는 on-disk 기록 없음 = 재생 안 함). **(버그픽스)** 과거 단수 모양만 처리해 `ops`-모양 assistant 턴(=실제 저장 형태)을 전부 누락 → resume 시 complete 최종답 포함 assistant 카드 전체가 안 보이던 문제 수정. `__init__(workspace=...)` 로 workspace 경로 받아 ready 이벤트에 포함. **Card timestamps**: `_emit` 가 단일 fan-out 지점에서 모든 이벤트에 server-stamp `ts`(epoch 초, emit 시각) 부착 → 프런트(`stampCard`)가 카드 모서리에 로컬시각(YYMMDD HH:MM:SS, hover=전체 날짜+ms) 표시. delegate/skill 내부 카드도 같은 `_emit` 경유라 자동 커버. **resume 시각 보존**: `replay_from_history` 가 각 history record 의 원본 `ts` 를 `_replay_ts` 에 실어 `_emit` 의 `if 'ts' not in data` 가드가 그대로 통과 → 재생 카드가 resume 시점이 아닌 실제 발생 시각 표시. **Team 스윔레인 resume 복구 (`scopes.jsonl` + `replay_scopes`)**: `scope_start`/`scope_end` 는 `ctx` 메시지 히스토리에 없어 `replay_from_history` 만으로는 스윔레인 막대가 비므로, `_emit` 이 라이브 scope 이벤트를 `<session_dir>/scopes.jsonl` 사이드카에 1줄씩 기록(agent_msg↔conversation.jsonl 과 같은 "kill=유지/resume=재생" 패턴, `_replay_ts is None` 가드로 재생분 재기록 방지). `replay_scopes()`(resume 시 `replay_from_history` 직후 호출)가 이를 원본 `ts` 로 재방출하되 **`replay:true` 태그** — 프런트는 스윔레인 막대만 복구하고 timeline 접이 카드(`ensureTaskGroup`)는 건너뜀(내부 턴이 flat 재생이라 재생성하면 빈 껍데기). 미완 scope(프로세스가 end 전 사망)는 재생 시 `scope_end` 합성해 막대가 영구 진행중으로 안 남게. 사이드카 부재(구 세션·신규 세션)=no-op → resume 무변경(하위호환). history `ts` 는 ISO 문자열(`_now_iso`), live 는 epoch — 프런트 `tsToDate` 가 둘 다 수용(레거시 pre-ts record 는 None→wall-clock fallback). **Parallel delegate visibility**: `_thread_to_task` dict + `_emit` 자동 task_id 첨부 + `begin_delegate_task` / `end_delegate_task` / `set_thread_status` override로 worker thread별 SSE 이벤트 라우팅. 프런트는 task_id 보고 collapsible group 카드로 격리 표시 → 두 parallel worker 출력이 인터리브하지 않음. **접기 UX (v7.13.0, v7.13.2 먹통 수리)**: `toggleTaskGroup` 를 헤더 클릭 + 본문 여백 클릭(`e.target===body` 만 — 중첩 카드/텍스트 선택 미간섭) 양쪽이 공유, `.task-header` 는 `position:sticky;top:0`(불투명 bg)로 긴 본문 스크롤 중에도 상단 고정 = 어느 스크롤 위치에서든 접기. ★v7.13.0 이 헤더를 sticky 로 만들면서, 확장 시 남아 있던 `scrollIntoView({behavior:smooth})` 가 sticky 요소 대상 smooth 스크롤=이동 타깃 재계산으로 스트리밍 `scrollToBottom` 과 진동→라이브 delegate(run) 카드 확장 시 UI 먹통. sticky 가 이미 헤더를 상단 고정하므로 scrollIntoView 는 불필요·유해라 제거(v7.13.2). 정적 가드=`test_web_server.py` toggle 에 header.scrollIntoView 부재. 계약=static `test_task_group_collapse_from_any_position_wired`+동작 `tests/browser/test_task_group_collapse.py`. **Recovery lifecycle (`recovery(raw, intervention, reason, turn)`)**: parse/validate 실패 경로가 base default(status+observation)를 override해 `failed_turn`(live streaming 카드를 실패 카드로 finalize — 안 하면 다음 턴 stream이 같은 카드에 누적되던 버그)+`observation`(LLM에 되먹인 intervention) 두 persistent 이벤트 emit.
├── web/                            agent-cli web 서버 + 정적 UI (optional dep, `pip install agent-cli[web]`)
│   ├── __init__.py
│   ├── directives.py        (~126, 5.4.0 전면 개편) directive 스코프 에디터 도메인 — ✨ 생성 하나만 소유: `generate_directive_section(audience, brief, current, runtime=)` = **산문 직접 호출**(5.7.0 — `provider.call` 1회 + `sanitize_generated`(<think> 블록·코드 펜스만 벗기는 포장 제거 전용 — 문구 필터는 정당한 본문 오탐 실측으로 금지)). 경로 변천은 모듈 독스트링이 기록(구 Qwen3 CoT-leak → 5.4 run 엔진 → 5.6 서브프로세스 → Qwen3.6 재실측 16/16 무누출로 5.7 복귀 — 사용자 결정). runtime={provider 객체, model, capabilities}. 동시 생성=executor+백엔드 병렬(실측 2건 병렬 ~2s). 입력오류=ValueError→400/호출실패=RuntimeError→502. `build_generation_task`(청중 프레이밍+brief+기존 내용 병합 지시)·`_WRITER_INSTRUCTIONS`(instant-agent 역할). 구 3축 zone 외과수술(_zone_*·관리 섹션·_append_before_scope_markers)·프리셋 라이브러리(directive_presets.py+내장)는 폐지 — 분해/조립은 prompts.system_prompt 의 split/join_directive_scopes 단일 출처. FastAPI import 0(테스트 가드)
│   ├── inspector.py         (91, v4.45.0 C3)  Prompt Inspector 지원 — _dynamic_context_sections(ctx→섹션 shape)·capture_startup_system_prompt
│   ├── slash.py             (222, v4.45.0 C3) 웹 slash 명령(/help·/sh·/compact)+handle_slash_command·WebDispatchOutput(디스패치 어댑터) — main web worker 와 공유
│   ├── server.py            (1172, C3 후 전송 전용: WebServer+create_app 27라우트+포트/미들웨어/SSE 헬퍼) FastAPI app. **이벤트루프 보호(블로킹 오프로드)**: `workspace_tree`(재귀 rglob 디렉토리 사이징)·`workspace_download`(zip deflate)·`workspace_delete`(rmtree; 경로 검증은 사전 수행 후 삭제만 오프로드)·`workspace_upload`(파일 쓰기)·`export_html`(HTML 렌더)의 블로킹 본문을 `run_in_executor` 로 워커스레드에 — 이전엔 async 핸들러 안에서 직접 실행돼 **모든 뷰어의 SSE 전달이 요청 동안 정지**(`_gen_directive` 만 올바랐음). `stream_events` 는 `_emit` 이 캐시한 `json_str` 재사용(payload 에 없으면 fallback dumps — identity/sticky/viewers 합성 항목). **`WebServer(renderer, token, ctx=None)`** — `ctx`(live ContextManager, worker 공유)를 받아 Prompt Inspector 가 **동적 컨텍스트**(대화+관찰)도 보여줌. **`_dynamic_context_sections(ctx)`** = `ctx.get_messages()`(system 제외)를 시스템 섹션과 **같은 shape** 의 섹션 리스트로 변환(`kind="dynamic"`, 메시지당 1섹션) — 프론트가 동일 아코디언으로 렌더(새 렌더 경로 0). `list(...)` 복사로 worker append 레이스 방어(읽기 전용 디버그 뷰, 락 없음). **첫 LLM 콜 전에도 채움**: 엔드포인트가 시스템 스냅샷 없어도(메인 스코프) ctx 메시지 있으면 동적 섹션만이라도 반환(`ok=False` 게이트 완화) → resume 즉시 대화 표시. **`capture_startup_system_prompt(renderer, capabilities, wire_format, session_dir, max_depth)`** — web 시작 시 `build_system_prompt_sections(active_tools=list(TOOLS.keys()), mcp_manager=None, depth=0)` 로 정적 시스템 프롬프트를 미리 빌드·캡처(첫 메시지 전 인스펙터 채움; `Hook:` 섹션은 PreLLMCall 후라 미포함, 첫 콜이 덮어씀). best-effort. `pick_port(host, preferred)` — `--port` 생략 시 main.py가 호출. preferred(8080) 에 **라이브 리스너 없으면**(`_port_has_live_listener` connect 프로브) bind 후 그대로, 있으면 `bind((host, 0))` 으로 OS ephemeral 할당. **connect 프로브가 핵심**: bind 프로브의 `SO_REUSEADDR`(TIME_WAIT 재시작 친화) 만으로는 macOS/BSD 에서 특정-IP bind 가 다른 프로세스의 `0.0.0.0:port` 리스너와 **조용히 공존**해 false-positive(두 서버가 같은 포트 경합) — `--host <ip>` 새 인스턴스가 이미 도는 `0.0.0.0:8080` 위에 또 8080 을 잡던 버그. connect 는 실제 클라이언트처럼 라이브 리스너(점유)와 TIME_WAIT 잔재(재사용 가능)를 구분. 명시한 `--port N` 은 probe 없이 그대로 uvicorn에 전달 (충돌 시 uvicorn이 에러). `_NoCacheStaticFiles` + `_NO_CACHE_HEADERS` — `/static/*` 와 `/` 응답에 `Cache-Control: no-cache, must-revalidate` 자동 stamp. editable install로 CSS/JS 수정해도 사용자가 hard-refresh(Cmd+Shift+R) 안 해도 서버 재기동만으로 반영됨 — `no-store` 가 아닌 `no-cache` 라 변경 없으면 304 fast path 유지. 엔드포인트: `GET /` (정적 index.html), `GET /static/*` (앱 JS/CSS), `GET /api/health` (auth 없음 — `{status, busy, awaiting_input, viewers}`; 프론트 컨트롤러[board]가 working/needs-answer/idle 판별 + `viewers`=라이브 브라우저 구독자 수[`renderer.viewer_count()`, not-closed]로 '누가 보고 있나' 판별→세션 중단 전 게이트), `GET /api/debug/prompt?task_id=` (토큰 인증 — Prompt Inspector: `task_id` 로 서브에이전트(run) 스코프 선택, 생략 시 main loop. 해당 스코프 최신 시스템 프롬프트 스냅샷(`kind="system"`)을 섹션·사이즈와 함께 반환; **메인 스코프면 `_dynamic_context_sections(server.ctx)`(`kind="dynamic"`)를 덧붙여 동적 컨텍스트도 포함**(서브에이전트 ctx 는 미도달이라 system-only). total_chars/est_tokens 는 합쳐서 재계산. 시스템 스냅샷이 없어도 메인 스코프 ctx 에 메시지가 있으면 동적만이라도 반환(resume); 시스템·동적 둘 다 비면 ok=False)·`GET /api/debug/prompt/scopes` (스냅샷 있는 스코프 목록 — main + 서브에이전트, 칩 라벨용)·`DELETE /api/debug/prompt?task_id=` (서브에이전트 스냅샷 제거 ✕; main 불가)·**`GET`/`POST /api/directives`** (토큰 인증 — Prompt Inspector 📝 Directives 에디터. GET=프로젝트 `.agent-cli/DIRECTIVE.md` 내용+경로(없으면 `""` → 에디터 항상 표시). POST `{content}`=파일 쓰기 + `renderer.mark_directives_dirty()`(루프가 다음 LLM 콜 시작에서 `consume_directives_reload()`→`_rebuild_system_prompt()` 픽업·KV prefix 리셋) + `broadcast_directives_changed()`. **update-when-applied**: 저장 시 broadcast 는 에디터 동기화용이고, 프롬프트 뷰의 `Directives` 섹션은 루프가 *실제로 rebuild 하는 순간* 새 스냅샷 push + `notify_directives_applied()` broadcast 로 갱신 → 인스펙터가 `directives_changed` 에서 editor+prompt 둘 다 재fetch(항상 LLM 이 실제 받는 내용 표시). **프로젝트 파일 전용**(사용자 전역 `~/.agent-cli` 미편집))·**`POST /api/directives/generate`**(✨ 생성 — `{audience, brief, current?}` → directives.generate_directive_section 을 executor 오프로드, 미저장 초안 반환. `WebServer.runtime`(LLM 배선, main 부트스트랩 주입) 없으면 503). GET 은 `scopes`(split 분해) 동봉, POST 는 `{scopes}` 를 `join_directive_scopes` 로 조립(또는 `{content}` 원문) — 5.4.0 스코프 에디터 계약. **v4.25.0 제거**: `POST /api/directives/compose`·`GET /api/directives/{personas,presets}` 라벨·빌트인 캐릭터/스타터·프로세 생성 프롬프트(자동생성 폐기, 📋/프리셋으로 대체). `_gen_directive`·📥 learn 은 v5.3.0 에서 제거(auto-review 와 함께 정리). **`GET /api/export/jira/targets`** (토큰 인증 — 설정된 Jira 인스턴스 name+base_url+deployment; 자격증명 없음. config 에 deployment 미지정 시 `detect_deployment` 프로브로 채움; export 드롭다운·필드 선택용)·**`POST /api/export/html`** (토큰 인증 — 선택 entry들을 self-contained HTML attachment 로; 읽기전용이라 controller 게이트 없음)·**`POST /api/export/jira`** (토큰 인증 — `{target?, base_url?, issue_key, deployment?, entries, auth:{user,secret}}` → `jira.resolve_target`[body base_url 우선, config 미일치 URL 은 `http`/`https` 허용(그 외 scheme 거부), base_url 없으면 config 해석] + deployment 결정[body→config→probe→cloud] → cloud=ADF/server=wiki 변환 → 사용자 자격증명으로 `post_comment`. config 없이도 동작(zero-config). 자격증명 누락·잘못된 scheme URL·config/issue 오류 400. 자격증명은 로그·세션에 안 남김), **`GET /api/workspace/tree?path=`** (토큰 인증 — 워크스페이스(서버 cwd) 한 레벨 디렉토리 목록: dirs-first, `{name, rel, type, size}`; 디렉토리도 rglob 합산 size; lazy 트리 펼침용)·**`POST /api/workspace/download`** (토큰 인증 — `{paths:[rel...], all?}` → 선택 경로를 임시 zip 압축[dir 재귀·file 단일·중복 dedup]→`FileResponse` + `BackgroundTask(os.unlink)` 로 전송 후 삭제)·**`POST /api/workspace/upload?name=&path=`** (토큰 인증 — body=raw 파일 바이트, multipart 의존성 0. `name`=대상 `path` 기준 상대경로[단일파일 `a.txt` 또는 디렉토리 업로드 `mydir/sub/a.c`]; 세그먼트별 `..`/빈/절대/백슬래시 거부+`_safe_workspace_path` 최종 재검증, 중간 디렉토리 자동 mkdir. 대상 `path` 는 기존 dir, `_MAX_UPLOAD_BYTES`=50MB 초과 413, 덮어쓰기 허용+`{name,rel,size,overwritten}` 보고. WRITE 라 download 보다 가드 강함. 프런트 📁 통합 드로어=파일/폴더 드래그-드롭[`webkitGetAsEntry` 재귀 walk] 또는 파일/폴더 선택[`webkitdirectory`/`webkitRelativePath`]→파일별 1요청, 트리에서 클릭한 폴더로 업로드)·**`POST /api/workspace/delete`** (토큰 인증 — `{paths:[rel...]}` → 파일 unlink·디렉토리 `shutil.rmtree` 재귀 삭제. **WRITE+DESTRUCTIVE 라 가드 최강**: under-workspace+traversal 거부 + **워크스페이스 루트 자체 삭제 거부**, per-path 오류는 `{deleted, errors}` 로 보고(한 경로 실패가 나머지 중단 X), 빈 목록 400. 프런트 dl-foot 의 `🗑 Delete` 가 체크 선택분을 `confirm()` 후 삭제→트리 갱신). 트리 `size`=파일 stat·디렉토리 rglob 재귀합산, 프런트 루트 행은 최상위 entries 합으로 **워크스페이스 총 크기** 표시. 세 엔드포인트 모두 `_safe_workspace_path(rel)` 로 workspace(=`Path.cwd()` 시작 시 resolve, `self.workspace`) 하위만 허용 — traversal/심볼릭 escape 차단. `GET /api/stream` (SSE, 토큰 인증, 다중 뷰어), `POST /api/input` (chat/prompt/confirm 통합 — 모든 연결 입력 가능; **chat 은 즉시 echo 없이 큐에 enqueue** → 디큐 시점에 카드 렌더. **prompt/confirm 게이트 (v7.2.0 ⓓ)**: `renderer.awaiting_input_kind()` 와 kind 불일치면 **409** — 받아줄 wait 없는 stale 답변(고갈-flush 클릭 burst·뷰어 레이스 패자)이 `_input_queue` 에 적체돼 다음 프롬프트를 자동응답하는 poisoning 차단 + confirm tuple 이 prompt(str) 대기에 흘러드는 malformed 경로 차단; 프런트는 409 수신 시 stale 다이얼로그를 chat 모드로 접음), `POST /api/queue/cancel`(`{conn_id, id}` — 소유자·미디큐만 취소), `POST /api/nickname`(`{conn_id, name}` — 사용자 닉네임 설정, trim+24자), `POST /api/abort` (`prompt_user`/`confirm` 인터럽트), `GET`/`POST /api/compaction` (토큰 인증 — 5.14 세션 한정 압축 목표 비율. GET=`{ratio,min,max,step}`, POST `{ratio}`=`ctx.set_compaction_ratio`(clamp)+`renderer.broadcast_compaction_ratio` sticky. ctx 없으면 GET 기본값·POST `ok:false`), `POST /api/stop` (진행 중 chat/skill turn 중단 — `trigger_stop` → worker `stop_event`). **`set_stop_handle(event)` / `trigger_stop()`** — worker 가 turn 마다 등록한 `stop_event` 를 `/api/stop` 이 set; lock 으로 worker·request thread 간 보호, 미등록이면 `trigger_stop` 이 False 반환. **다중 뷰어 (모두 동등)**: 모든 인증 연결이 스트림을 받고 모두 입력 가능(controller/observer 구분 삭제). 각 연결은 `identity` 이벤트로 conn_id 취득(로스터 "(you)"·큐 소유). takeover 없음. **메시지 큐**: `_pending`(deque+Condition) — `enqueue`(누구나)·`dequeue_blocking`(worker idle, pending 우선 후 SHUTDOWN)·`dequeue_nowait`(loop 턴경계 주입)·`cancel_pending`(소유자·미디큐만)·`queue_snapshot`. 변경마다 `renderer.queue_state`로 `queue` 이벤트 브로드캐스트. 토큰은 `secrets.compare_digest` 상수시간 비교. `stream_events` async generator가 snapshot replay → live loop 순서로 yield. **`handle_slash_command(message, renderer, ctx=None)`** — 웹 명령어: `/help`, `/sh <cmd>`, `/compact`(`ctx.compact_now()` → before/after `observation` 카드; ctx 없으면 unavailable). **`WebDispatchOutput`** — `main.try_dispatch_agent_or_skill` 에 넘기는 `DispatchOutput` 어댑터: `/skills`/`@agents` 리스트, `@<name> <task>`/`/<skill> <args>` invocation, not-found 에러를 전부 `observation` 이벤트로 변환. `run` 과 dispatcher 공유. **`SHUTDOWN` sentinel + `shutdown()` 메서드** — `_pending` 큐의 shutdown 플래그를 set + condition notify 해 worker thread의 blocking `dequeue_blocking()`을 깨움(pending 은 먼저 drain); worker는 `item is server.SHUTDOWN` 분기로 루프를 빠져나간다. **lifespan shutdown 훅** (`@asynccontextmanager async def _lifespan`) — uvicorn SIGINT 경로에서 `server.renderer.shutdown_all_connections()` 호출 → sse-starlette ping coroutine이 CancelledError 트레이스 없이 조용히 종료; main.py finally 블록과 idempotent하게 페어링. **`suppress_incomplete_response_log()`** — Ctrl+C 시 SSE 클라이언트가 연결돼 있으면 sse-starlette가 `_stream_response` task를 final body chunk 전에 cancel해 uvicorn이 "ASGI callable returned without completing response"를 logger.error로 남긴다. 세션은 정상 finalize되는 cosmetic noise이고 shutdown 시에만 발생(정상 운영 중엔 없음)하므로, `uvicorn.error` 로거에 그 메시지만 거르는 `_IncompleteResponseLogFilter`를 main.py `web()`가 idempotent하게 부착.
│   └── static/                     Vanilla JS 프런트엔드 (의존성 0)
│       ├── fonts/                   **Jetendard 웹폰트 번들** (JetBrains Mono + Pretendard 한글, 모노스페이스, SIL OFL 1.1 — `OFL.txt` 동봉). self-hosted woff2 4종(400/500/600/700, ~6.4MB)만 — UI 가 실제 쓰는 weight. 나머지 weight/italic 은 최근접 스냅·합성. CDN/npm 0(온프렘). style.css `@font-face` 의 `url()` 은 스타일시트 상대라 `--base-path` 무관. 회귀가드 `test_web_fonts.py`.
│       ├── index.html       (200)  단일 HTML 셸 — `<head>` 의 인라인 테마 스크립트(첫 페인트 전 `data-theme` 설정 = localStorage `agentcli_theme` 의 테마 id, 미지정 시 amber[시스템 light 선호면 light] — FOUC 방지), **칩 헤더(v7.1.0)**: 좌측 의미 칩 3개 — 모델 `#info`(max 28ch ellipsis, hover=provider·전체명) / ctx 게이지 칩 `#chip-ctx`(`#ctx-gauge`+`#ctx-pct`; 클릭 → `#ctx-popover` 팝오버에 기존 `#token-usage`·`#compaction-wrap`·`#maxagents-wrap` 이 **id 보존 이동** — IIFE·테스트 계약 무변경) / 워크스페이스 칩 `#chip-ws`(`📋 …/마지막 2세그먼트`, 내부 `.hd-chip-text` 36ch ellipsis[flex 컨테이너엔 ellipsis 미적용이라 텍스트 span 이 담당], **칩 자체가 클릭=경로 복사** — 별도 버튼은 칩 높이보다 커 세로 클리핑됐던 실피드백 수리), 우측 아이콘(🎨 테마 피커[`#theme-wrap` > 버튼 + `#theme-menu` 드롭다운] / ⚡ Inspector / 📤 Export / 📁 Files 버튼 — 공통 borderless-emoji 스타일 + 접속자 로스터 옆 ✎ `#rename-btn` 닉네임 재설정 버튼[기본 hidden, 로스터 합류 시 노출]) / name-bar(첫 접속·✎ 공용 닉네임 입력바) / messages / export-bar(선택 모드 액션바 — Jira 폼에 `#export-jira-http-warn` 평문 경고 span 포함) / download-drawer(우측: 파일트리·count·⬇ zip + 업로드 드롭존) / footer + textarea. JS가 URL ``?token=…``에서 토큰 추출, SSE 연결.
│       ├── app.js           (3346) SSE 이벤트 디스패치 + DOM 렌더링. **tab-presence 비콘 (v7.3.0, v7.7.0 에서 유일한 연결-가드 표면으로 단순화)**: EOF IIFE 가 BroadcastChannel `agentcli_tab_presence` 의 ping 에 `{pong, nonce, path}` 응답 — board 열기 게이트(항상 board 경유 운용 전제)의 카운트·재사용 판정 데이터 소스. 페이지 자체 입장 게이트(v7.5 파킹/v7.6 Web Locks)는 제거 — Web Locks 는 secure context 전용이라 LAN http 에서 무동작이었고, URL 직접 진입은 운용 정책상 범위 밖(세션 복원 잔여 리스크는 문서화 수용). **confirm 무응답 가시화 (v7.2.0 ⓔ)**: `submitConfirm` 이 `CONFIRM_STALL_MS`(3s) 타이머를 걸고 미해결이면 버튼 줄에 `#confirm-stall` 경고(연결 정체 안내) 표시 — 브라우저 origin 당 6연결 고갈 시 클릭이 조용히 브라우저 큐에 갇히던 실사고의 가시화; `setInputMode` 전환 시 타이머·경고 정리. postInput 409(이미 해결된 프롬프트) 수신 시 confirm/ANSWERING stale UI 를 chat 모드로 접음. **ctx 칩/팝오버(v7.1.0)**: `token_usage` 핸들러가 `#ctx-pct`(%)·`#ctx-gauge-fill`(width) 갱신+칩 노출, EOF 의 팝오버 IIFE 가 theme-menu 패턴(토글·외부클릭/Esc 닫기·내부클릭 stopPropagation=슬라이더 조작 보호·aria-expanded)으로 `#ctx-popover` 개폐; ready 핸들러가 ws 칩(마지막 2세그먼트+클릭 복사 📋→✓ 플래시) 배선. event_buffer (snapshot) replay → live. 카드 종류: user_message (우측 파란 bubble), assistant_turn (thought + final OR action), observation (✓/✗ + tool_name), error, streaming (점선, 토큰 누적). prune 이벤트 시 가장 오래된 N개 카드 DOM에서 제거. input mode 3개 (chat / prompt / confirm). confirm 모드는 ConfirmOption.label 버튼 + 코멘트 텍스트. confirm/ask에 `input_required.agent/reasoning/action` 가 있으면 `buildPromptMetaEl`이 `↳ from <agent> · 💭 reasoning · ⚡ action`(.prompt-meta) 블록을 버튼/답변영역 위에 렌더 — 서브에이전트 출처 표시. **identity/roster**: `identity` 이벤트로 자기 conn_id 수신(접속자 로스터 "(you)"·큐 소유). 모든 연결 동등하게 입력 가능. 입력 POST 에 `conn_id` 동봉(큐 소유 식별). **닉네임 입력**: 접속 시 `#name-bar`(기본값 채워진 입력)로 이름 설정(localStorage 기억; 저장값 있으면 자동 적용·바 미표시) → POST /api/nickname. **닉네임 중간 변경(✎)**: `openNameBar(current)` 공유 헬퍼가 first-connect 프롬프트와 ✎ 진입점 양쪽에서 name-bar 를 prefill·포커스 재노출 — 헤더 `#rename-btn`(✎)은 `viewers` 이벤트에서 내가 로스터에 있을 때만 노출(`myNickname` 도 갱신해 prefill), 클릭 시 현재 닉네임으로 바 재오픈 → 기존 `applyNickname` 경로 재사용(POST + localStorage 갱신 + 바 숨김). 백엔드 무변경(`set_nickname` 이 로스터 재브로드캐스트, ephemeral·미영속). **메시지 큐 UI**: `queue` 이벤트 → `#queue-list` 에 대기 메시지(닉네임)·자기 항목 ✕(POST /api/queue/cancel). send 는 항상 큐잉(busy 면 대기); Stop 은 별도 `#chat-stop` 버튼(busy 시 노출, POST /api/stop). **markdown 헬퍼 (`escapeAndFormat` → `extractCodeFences` → `markdownInline` → `restoreCodeFences`)** — 의존성 0의 자체 미니 파서: 헤더(`#`/`##`/`###` → `<h1>`/`<h2>`/`<h3>`), GFM 파이프 표(헤더 행 + `---` separator + body), 순서/비순서 리스트 (`-`/`*`/`1.` 연속 라인 ↔ `<ul>`/`<ol>`), `**bold**`/`*italic*`, 인라인 코드, 펜스 코드(```` ``` ````). **XSS 안전(NFR-MD-2)**: `escapeHtml`이 가장 먼저 실행되어 `<`를 `&lt;`로 치환, 펜스를 placeholder로 빼낸 후 markdown 패스를 stripped body에 적용, 마지막에 pre-rendered `<pre><code>`로 복원 — markdown 패스가 사용자 입력 HTML을 실행 가능 토큰으로 되돌릴 경로가 없음. write_file/edit_file 의 plain diff 는 `colorizeDiffBody` 가 observation 본문에서 `--- a/` 블록 이후 라인을 첫 char 별 `rich-*` span 으로 색상 (diff 데이터는 plain — 색상은 렌더 시점). **`failed_turn` 핸들러**는 `finalizeStreamingAsFailed`로 live streaming 카드를 `.card-failed`로 마감(제거 X)+streamingText 리셋 → 잘못된 응답 / intervention(observation) / 재발화가 **3개 카드로 분리**(이전엔 하나의 카드에 재발화까지 누적되다 정상 응답에서야 교체). **Export 기능(별도 IIFE — main 렌더 루프 무수정)**: 📤 버튼이 선택 모드 토글 → `#messages > .card` 를 class 로 분류(user/assistant/observation/error/agent; streaming·failed 제외)해 per-card 체크박스 부착(MutationObserver 로 실행 중 도착 카드도). 선택 entry(`{kind,label,body=innerText,mono}`)를 `POST /api/export/html`(Blob 다운로드) 또는 `POST /api/export/jira`(인스턴스 드롭다운은 `GET …/targets`로 채움[deployment 포함, 0개여도 폼 표시=zero-config] + **편집 가능한 base_url 필드**[config target 선택 시 prefill, 직접 타이핑 가능, localStorage `agentcli_jira_url` 마지막 URL 기억; `updateJiraHttpWarn` 가 `input`/`change` 마다 URL 이 `http://` 면 `#export-jira-http-warn` 평문 경고를 인라인 표시(차단 아님, https/빈값이면 숨김)] + Cloud/Server 토글 + 본인 계정·토큰[localStorage `agentcli_jira_cred_{base_url}` — URL 별 prefill] + issue key 폼, body 에 `base_url`+`auth:{user,secret}`+deployment 동봉)로 전송. Inspector 와 동일한 "헤더 버튼 → 별도 IIFE" 패턴. **Download 기능(별도 IIFE)**: 📥 버튼이 우측 드로어 토글 → `GET /api/workspace/tree`로 루트 목록을 받아 lazy 트리 렌더(디렉토리 ▶ 클릭 시 하위 fetch·펼침, 노드별 체크박스, 파일·디렉토리 size 표시). 선택 경로(또는 All) → `POST /api/workspace/download` → 응답 zip Blob 을 anchor click 으로 저장. **압축 슬라이더(별도 IIFE, 5.14)**: `#compaction-range` — 시작 시 `GET /api/compaction` 으로 현재값+범위(%로 표시) 로드 후 노출, `change` 에 `POST /api/compaction`(clamp 결과 되반영), `agentcli:compaction`(sticky 중계)로 타 뷰어 동기화. 메인 렌더 루프 무수정. **테마 피커(별도 IIFE)**: 🎨 `#theme-btn` 이 `#theme-menu` 드롭다운을 토글 — 5개 테마 목록(`THEMES` = id/name/swatch[bg+accent] 단일 출처)에서 항목별 스와치+이름+현재 ✓ 를 렌더, 클릭 시 `<html data-theme>` 설정 + localStorage `agentcli_theme` 저장 + 메뉴 닫기(외부 클릭/Esc 도 닫음). 기본 amber. 초기 테마는 `<head>` 인라인 스크립트가 이미 설정(FOUC 0); CSS 토큰만 갈리고 렌더 로직 무관 — 메인 IIFE 와 격리. **Directives 드로어(Inspector IIFE, 5.4.0 스코프 탭)**: 📝 에디터 = 청중 탭 3개(공통/Main/서브에이전트 — 파일의 U-C 스코프와 1:1, 내용 있는 탭 ● 뱃지) + 탭별 버퍼(`dirBuffers`, 전환 시 sync) + ✨ 생성 입력줄(`generateDirective`→`POST …/generate`, 진행 중 버튼 잠금, 결과는 활성 탭에 미저장 반영) + 저장(`{scopes}` POST — 서버 조립)/취소(`dirDirty` 가드). 구 3축 행·프리셋 드롭다운·💾 저장 모달은 폐지.
│       ├── team_model.js    (263)  **Team 스윔레인 도출**: 순수 DOM-free — SSE 를 {lanes,agents,messages,oneshots,skillBands,mainSpans} 로 도출. **작업 스팬=scope 이벤트(권위 소스)**: `scope_start` task_id "{key}#{seq}"(begin_agent_work)=그 에이전트 레인의 work span, kind="skill"=호출자 밴드, 그 외(delegate/agent run)=main one-shot. **메시지=왕복**: `out`(정본)+**main/user 발 `in`**(요청 leg — main/user 는 out 을 안 내므로 이것만이 그 화살표의 유일 기록; peer `in` 은 sender out 이 이미 그려 skip=중복 방지). `laneKey` 가 `agent:<key>` 접두어 정규화(peer 회신 to=`agent:orch` → 레인 `orch` 매칭, 안 하면 회신 화살표 드롭). `build(events, now)` — 진행 중(열린 scope)이면 도메인·막대를 now 까지 확장, now 없으면 결정적(tMax). dual Node/browser export → Node 유닛(test_team_model)으로 뮤테이션 검증.
│       ├── team_view.js     (426)  **Team 스윔레인 렌더 — 세로(시퀀스 다이어그램형)**: 시간이 **아래로** 흐르고 에이전트=**세로 컬럼**. 상단 **sticky 헤더**(컬럼 칩, position:sticky)가 고정되고 tall plot SVG 가 세로 스크롤. 작업=세로 막대, peer 메시지=컬럼 사이 **가로 화살표**(요청+회신 왕복). **시간 스케일**: `CAP`(3600s)까지 가시 높이에 fit 압축 → 초과 시 px/초 고정+아래로 성장(세로 스크롤), `_stick`=바닥(now) 고정(사용자가 위로 스크롤하면 해제). 전 색상 var(--…) 테마 반응, 라벨=커스텀 100ms 툴팁, 좌측 gutter=시간축(MM:SS)+adaptive tick. app.js 가 TeamView.ingest(dedup→멱등) 로 급전, Timeline/Team 토글 상호배타, 이벤트 rAF+5s 라이브 틱. **resume 복구**: scope 이벤트는 `scopes.jsonl`→`replay_scopes` 로 재방출(`replay:true`=막대만, timeline 카드 스킵). 브라우저 e2e=test_team_swimlane(세로·왕복·resume 포함).
│       └── style.css        (1321) chat UI 스타일 — **칩 헤더 스타일(v7.1.0)**: `.hd-chip` pill(999px radius·mono 12px)+`.hd-chip-btn` hover/expanded accent 테두리, `.hd-chip-text`(내부 ellipsis 담당)·`#info` 28ch inline-block ellipsis, `#ctx-gauge` 40×5px 미니 게이지, `#ctx-popover` 는 `#theme-menu` 글래스 팝오버 미러(세로 flex, 이동 컨트롤 수납). — 가독성 우선, 모바일 폴백 단일 컬럼. **멀티-테마 디자인 토큰**: 파일 상단 `:root`(공유 다크 베이스 = "slate" ~55 토큰: surface/text/accent/status/glass/shadow) + 큐레이션 테마별 `[data-theme="midnight|terminal|amber|light"]` 오버라이드 블록(다크 변형은 surface+accent 만, light 는 전체 오버라이드). **다크 테두리=반투명 헤어라인**(`--border: rgba(255,255,255,.07)`) — 단단한 회색 선 대신 부드러운 경계(프리미엄 다크 룩의 핵심). 본문 전체가 raw hex 없이 `var(--…)` 로 파생돼 테마 추가=토큰 블록 하나(회귀가드 `test_theme_tokens_and_picker_wired`: body raw-hex 0·토큰 self-ref 0·테마 블록 존재). 테마 피커 드롭다운(`#theme-menu` + `.theme-item`/`.theme-swatch`). **폼/버튼 color 토큰화**: input/textarea/select/button 은 `color` 미상속이라 다크에서 검은 글씨가 되던 것 → 기본 `var(--text)` + placeholder `var(--muted)`. 메시지/카드 색상, 입력창 sticky, 닉네임 바·✎ `#rename-btn`, Prompt Inspector 드로어(스코프 칩 row), Export 선택 모드, Download 드로어, 헤더 아이콘 버튼(`#theme-btn`/`#inspector-btn`/`#export-btn`/`#files-btn` 공통 borderless-emoji) 등.
├── integrations/                   외부 서비스 연동 (export 타깃 등) — web Export 기능이 사용
│   ├── export.py            (149)  대화 export 렌더링 — 선택 transcript entry(`{kind,label,body,mono}`) → **`entries_to_html`**(self-contained HTML, inline CSS, escape + pre-wrap; mono body는 `<pre>`) / **`entries_to_adf`**(Jira **Cloud** 코멘트용 ADF doc — label은 strong paragraph, body는 mono면 codeBlock 아니면 paragraph; 빈 body는 skip해 ADF 빈-텍스트노드 거부 회피) / **`entries_to_wiki`**(Jira **Server·DC** 코멘트용 wiki 마크업 STRING — `*label*` 굵게, mono body는 `{code}…{code}`, 빈 body skip; v2 코멘트 body는 ADF 가 아닌 문자열). 셋 다 순수함수 → 브라우저·라이브 Jira 없이 단위테스트
│   └── jira.py              (236)  Jira 코멘트 POST — **프론트엔드 사용자 본인 명의**. config 는 선택(zero-config 가능); 자격증명은 서버 미저장(`jira.instances` 는 `base_url` + 선택 `deployment` 만 + `default`). `list_targets`(name+base_url+config-pinned deployment, 순수·네트워크 없음)·`detect_deployment(base_url)`(`{base_url}/rest/api/2/serverInfo` 의 `deploymentType` 무인증 GET → `"cloud"|"server"|None`, 성공만 프로세스 캐시)·`resolve_instance`(target/default/단일 해석, `base_url` 만 필수)·**`resolve_target(config, target, base_url)`**(어디로 POST 할지 결정 — body base_url 우선. config 인스턴스와 일치하면 신뢰(내부 http 허용), 미일치=사용자 입력이면 **`http://`·`https://` 둘 다 허용**(그 외 scheme/scheme 없는 값은 `JiraError`) — `http` 평문 위험은 여기서 차단하지 않고 UI 경고로 surface; base_url 없으면 `resolve_instance` 폴백 → 트러스트 정책의 단일 origin)·`post_comment(base_url, deployment, auth_user, auth_secret, key, body)` → `deployment=="server"`면 `/rest/api/2`+문자열 body, 아니면 `/rest/api/3`+ADF dict, 둘 다 `requests.post(auth=(user,secret))` Basic — 자격증명은 그 요청에만 쓰고 저장 안 함. base_url 이 인자라 테스트는 로컬 mock 으로(유료 Jira 불요). 실패는 `JiraError`. `requests` 재사용(새 의존성 0)
├── providers/                      LLM 프로바이더 어댑터
│   ├── __init__.py          (33)   create_provider() 팩토리
│   ├── base.py              (~75)  LLMProvider 프로토콜, LLMResponse(+thinking), TokenUsage(+cache_creation/cache_read tokens). `strip_think_blocks` 는 v5.19.1 부터 thinking_tags 재-export (openai.py·기존 테스트의 import 경로 보존)
│   ├── capabilities.py      (505)  ModelCapabilities + 프로브 감지 + 진행 콜백 + 자동 저장. **공유 오케스트레이터 `_detect_capabilities(model, transport)`** — context_window/thinking-태그/structured/reject/`max_output` 로직은 provider 무관 1곳, **transport 만 provider별**(`_OpenAITransport`=`/chat/completions`·Bearer, `_AnthropicTransport`=`/messages`·`x-api-key`+`anthropic-version`·`content[].text`). OpenAI 는 기존 helper 위임(parity), Anthropic 은 `_detect_anthropic_context_window`(/models 메타→/messages overflow→128K) + 프롬프트-only JSON structured probe(strict 항상 False) + `<think>` 태그 thinking 탐지. (omlx 가 두 API 동일모델 서빙·실 Anthropic 도 GET /v1/models 지원이라 양쪽 동작; 실 Anthropic 은 /v1/models 에 window 메타 없어 overflow/fallback/registry 로.) OpenAI 호환 context window는 `/v1/models` 메타 → overflow probe → 128K fallback 3-tier. **auto-detect 시 `max_output_tokens = context_window // 4`** (예: 256K→64K, 16K→4K; 기존 4096 cap 제거). context window가 `MIN_CONTEXT_WINDOW`(16K) 미만이면 `UnsupportedModelError` raise → CLI(`_setup_provider`)가 잡아 fail-fast (registry/models.json 저장값은 이 규칙 미적용 — 저장값 그대로). **structured-output 감지**(`_probe_structured_output`): context window 수용 후 `response_format={"type":"json_object"}` → `supports_structured_output`, 이어 strict `json_schema` → `supports_strict_schema` 프로브. 산문 자연스러운 프롬프트의 반환값이 유효 JSON(스키마 준수)일 때만 인정(서버가 `response_format` 무시 시 오탐 방지), 실패 시 보수적 False
│   ├── http.py              (466, v4.48.0 C6) post_with_retry(공유 재시도 — 네트워크 예외 10회 + 게이트웨이 5xx[502/503/504] 3회, 독립 예산, 5.14.1)·interruptible_lines(TTFT-interrupt/idle 폴링)·make_stream_patient + **공용 SSE 골격 `run_sse_stream`**: 라인 순회(idle notice+StreamIdleTimeout **양 provider 동일** — 이전 openai 만)·`data:` 파싱·`[DONE]` 관용·**JSONDecodeError 관용(양쪽)** — 이전 anthropic 만·누산/ttft 타이밍·degeneration '#' 게이트 조기종료·interrupt 라벨링. provider 는 `map_payload(dict)->StreamEvent`(이벤트 shape 해석)만 소유 — wire-format self-contained 와 같은 정신(독립 진화 필요 부분만 provider 에)
│   ├── anthropic.py         (240, C6 후 매퍼+usage 조립+idle 재연결 래퍼 — 이전엔 재연결/patient-socket 미배선으로 침묵 시 무한 대기)  Anthropic Messages API (tool_use + thinking blocks + streaming + TTFT + prompt cache via cache_control). **`degeneration_check`**(= `wire.is_degenerate`, provider-독립)·**`interrupt_check`**(zero-arg) 두 predicate 를 `_handle_stream` 가 처리. line read 는 `interruptible_lines` 경유라 interrupt 는 TTFT 포함 no-data gap 에서도 깨지고, 루프 뒤 `interrupt_check()` 재확인으로 `stop_reason="interrupted"`(loop 이 partial 폐기). degeneration 은 content chunk 별 `'#'` 게이팅 후 True 면 `stop_reason="degenerate_runaway"`(loop 이 라벨·복구). openai 와 동작 동일 — loop 이 두 provider 에 같은 predicate 를 넘기므로 대칭(이전엔 anthropic 이 degeneration_check 를 받고도 버리는 비대칭 부채였음).
│   └── openai.py            (218) OpenAI 호환 API (function calling + reasoning_content + streaming + TTFT). **인라인 <think> 격리 (5.10.0)**: 두 응답 조립 경로(스트리밍/비스트리밍) 모두 `base.strip_think_blocks` 적용 — MiMo 류가 content 에 태그로 흘리는 긴 추론을 제거(안 닫힌 열림 태그=EOF 까지)하고 `thinking` 필드로 이동(reasoning_content 와 합류, 정보 무손실). 태그 vocab 은 capabilities 탐지와 동일 4종; capability 프로브는 자체 transport 라 비영향. **스트리밍 콜은 재연결 루프**: post `(30,30)` timeout(헤더 바운드) → `make_stream_patient` 로 소켓 patient 리셋 → `_handle_stream`(idle 파라미터 + `on_idle`=render_status 대기 알림 전달). `StreamIdleTimeout`(10분 침묵) 잡으면 재연결 알림 렌더 후 재전송, `STREAM_MAX_RECONNECTS=3` 회 후 raise. 비스트리밍은 `(30,1200)`. **`degeneration_check`** kwarg(= `wire.is_degenerate`)가 있으면 `_handle_stream` 이 누적 텍스트에 적용 → True 면 stream 을 닫고 break(format-runaway 조기 중단, `'#'` 포함 chunk 에서만 검사해 O(headers)). truncated content 는 `stop_reason="degenerate_runaway"` 로 반환돼 downstream 에서 parse·라벨. **`interrupt_check`** kwarg(zero-arg, = loop `_interrupt_check` → `stop_event.is_set()`): line read 가 `interruptible_lines` 경유라 TTFT 포함 no-data gap 에서도 interrupt 가 깨지고(블로킹 read 를 시그널핸들러/타스레드에서 직접 닫는 reentrant/race 회피 — loop 은 flag 만, 닫기는 reader 소유 측), 루프 뒤 `interrupt_check()` 재확인으로 `stop_reason="interrupted"`. degeneration partial 과 달리 loop 이 이 partial 을 **파싱·기록 없이 폐기**(사용자가 방향 전환). **`response_format={"type":"json_object"}` 는 `kwargs["json_mode"]` 가 True 일 때만 전송 — provider 는 capability 를 직접 안 본다.** `json_mode` 는 **`WireFormat.provider_call_kwargs(capabilities)`** 가 결정(wire ⨯ capability 단일 결정점): JSON-shaped wire(react)는 `capabilities.supports_structured_output`, json_fc(markdown)는 capability 무관 항상 `False`. 이전엔 provider 가 capability 와 wire 의 `skip_json_format` 을 직접 조합하다 prefix_md 에 JSON 강제 → omlx/mlx degenerate(`[2025]`/`[1000,1000]`)하는 버그가 있었음 (prefix_md 기본 전환이 노출 — bakeoff 는 provider 우회라 못 잡음). 이제 새 wire plugin 은 `provider_call_kwargs` 만 정의하면 provider 가 잘못 조합할 여지가 없음. **`response_format={"type":"json_object"}` 는 `kwargs["json_mode"]` 가 True 일 때만 전송 — provider 는 capability 를 직접 안 본다.** `json_mode` 는 **`WireFormat.provider_call_kwargs(capabilities)`** 가 결정(wire ⨯ capability 단일 결정점): JSON-shaped wire(react)는 `capabilities.supports_structured_output`, json_fc(markdown)는 capability 무관 항상 `False`. 이전엔 provider 가 capability 와 wire 의 `skip_json_format` 을 직접 조합하다 prefix_md 에 JSON 강제 → omlx/mlx degenerate(`[2025]`/`[1000,1000]`)하는 버그가 있었음 (prefix_md 기본 전환이 노출 — bakeoff 는 provider 우회라 못 잡음). 이제 새 wire plugin 은 `provider_call_kwargs` 만 정의하면 provider 가 잘못 조합할 여지가 없음.
│
├── tools/                          도구 시스템
│   ├── __init__.py          (30)   registry re-export (TOOLS / TOOL_SCHEMAS / _execute_tool / infer_action / validate / get_descriptions) — 기존 `from agent_cli.tools import ...` 호환
│   ├── base.py              (361)  `Tool` ABC — schema(name/description/parameters) + dispatch(`_run`) + wire-key prefix(`key_prefix`/`strip_prefix`/`add_prefix`) + `claims`(prefix 매칭) + **`touched_paths`/`summary_arg`**(compaction 시 file-list 기여 + action 라벨 — 각 도구가 `strip_prefix`로 표준 키 읽음; base 기본=빈 list / 첫 string fallback, path·command·agent 도구가 override). **의미론 검증 훅 `validate(args)->str|None` (C7, v4.49.0)**: shape(존재/required/타입/coercion)는 중앙 `validate_tool_input` 1~5단계, 의미론(mode별 조건부 필수·enum·필드 형식)은 이 훅 — 중앙 6단계(A5 경로: SCHEMA_MISMATCH 기록+format-error 렌더, 관찰은 도구의 **짧은 문구 그대로**[스키마 전문은 shape 실패만 — 정밀화 결정])와 `run()` 초입(직접 호출자 방어) 두 곳에서 호출하되 **로직은 1곳**. override: code_index(mode→필수키 선언 테이블 `_MODE_REQUIRED` — 이전 ~13개 분산 `_require`/`_validate_kind` 흡수)·edit_file(op enum+pos/end 형식 — TypeError 스레드-사망 가드 승계)·memory(mode enum+id 필수). 실행이 필요한 검사(hashline ref 실검증)는 실행 소관 잔류. **과대 출력 표면 3개**: `render_observation(result, args)`(결과→관찰 본문 렌더, 기본=성공 `output`·실패 `error` — write/edit 가 echo 트림 등으로 override 할 seam) + `apply_oversized_cap: bool = True`(이 도구 관찰에 `context_window//10` 캡 적용 여부 — 도구별 opt-out) + **`render_oversized(result, args, *, body, tokens, ctx)`**(캡 초과 시 무엇을 낼지 도구가 소유 — 기본=모듈 `default_oversized_nudge` 제네릭; override 로 도구별 복구 안내·또는 `body` 의 유계 slice+포인터 반환 가능; per-result `body`/`tokens` 는 명시 인자, per-call 컨텍스트 `ctx`(=`RunContext`: `oversized_cap`·`tools_available`·`session_dir`)로 캡·호출가능도구·세션dir 전달 → 안내가 부를 수 있는 도구만 지목). loop `_tool_observation` 이 결과→관찰 seam 에서 셋 다 consult. **공유 헬퍼 `on_disk_oversized_nudge(tool, subject, location, path, tokens, cap, tools_available, *, nlines, part_extra, tail_bullets)`**: "큰 내용이 디스크 `<path>` 에 있음 → (a) read range/search, (b) **N-way 병렬 섹션 팬아웃**"의 invariant 를 한 곳에 — read_file/shell/delegate/fetch 네 override 가 소비(각자 path·문구·extra 공급). (b) 는 **한 턴에 delegate op 여러 개**(agent-cli 가 동시 실행)를 내 각자 한 line-range 를 읽고 짧은 요약만 반환하도록 유도 — `nlines`(호출자 body 라인수)로 `k≈ceil(tokens/cap)+1`(2~8) 섹션·`step` 을 계산해 **구체 범위가 든 복사가능 delegate 예시**를 제시(단일 offload 가 아니라 병렬 분담 유도). nlines 없으면 제네릭 병렬 문구로 폴백. **read_file** override(원본 파일, +read_symbols), **shell** override(over-cap 때만 출력을 `session_dir/shell-output-<hash>.txt` lazy 저장→가리킴, headless=tee 폴백), **delegate** override(기존 `result.md` 가리킴 + re-delegate-narrower), **fetch** override(over-cap 때만 내용을 `session_dir/fetch-output-<hash>.txt` lazy 저장→가리킴 + 더 좁은 URL/얕은 depth). session_dir·cap·tools_available 은 `ctx`(RunContext, loop `_run_ctx()` 조립) 로 전달. **형제 헬퍼 `narrow_oversized_nudge(tool, subject, tokens, cap, *, bullets)`**: 출력이 파일이 아닌(제자리 재-narrow) 도구용 — **read_context**(SQL LIMIT/projection/`substr(text,1,200)`) · **code_index**(`mode=fetch` 단일 심볼·`search` 필터·`max_bytes`) override 가 소비. on_disk 계열과 달리 파일/팬아웃 얘기 없이 그 도구 파라미터로만 유도. **`render_action_input_for_context(action_input)->dict`** (관찰의 대칭 — action 측): 어시스턴트 turn 재공급 시 이 도구의 action_input 표현(**기본 identity**). manager `_context_view` 가 render+estimate 양쪽에서 consult. **현재 어떤 도구도 override 안 함 → 무영향**(seam 은 미래용 latent). write_file(`content`)·edit_file(`lines`) 본문 elide 를 켰었으나(v3.16.0) **모델이 재공급된 `<…elided…>` 마커를 본문으로 모방(mimicry)해 파일을 실제 손상**(모델은 `shell` heredoc 으로 우회 복구) → **v3.16.1 revert**. 교훈: **모델 자신의 출력(action)을 가짜로 재공급하면 모방 위험** — 관찰(=도구 결과)은 안전, action(=자기 출력)은 위험. 본문 bloat 는 미해결로 둠. **`parallel_safe: bool = False`** (Step 3): 한 턴의 연속 동-도구 op 들을 loop 이 동시 실행해도 안전한가 — 부작용/순서 의존 도구(write/edit/shell)는 False(순차가 정확성 보장), 독립 도구만 True. 현재 delegate 만 opt-in(독립 서브에이전트 = 병렬이 안전+가치). loop `_dispatch_parallel_batch` 가 읽음. **`wrap_single_op(flat)`** (멀티-op 3b): 멀티-op 포맷의 flat 단일-대상 op 을 자기 캐노니컬 입력으로 재포장 — 기존 validate→strip→run 파이프라인을 무변경으로 재사용하는 전제. 기본=add_prefix(미래 prefixed 도구용 — 현재 어떤 도구도 base 기본을 안 씀); **모든 builtin 도구가 flat-native(write_file/read_file/edit_file/code_index/delegate, consolidation Step 3)라 identity override**(스키마 자체가 flat). **`McpTool` 도 identity override**(MCP 는 prefix-less — base add_prefix 면 bare 키 `{query}`→`{srv.tool_query}` 로 손상돼 validate 실패; Step 4 발견·수정). 멀티-op 디스패치 경로에서만 호출(단수 포맷 우회). `run()`이 strip_prefix 후 `_run` 호출. 각 도구는 `name`만 정하면 prefix/strip/claims 자동(현재 latent — flat 키엔 무작동)
│   ├── virtual.py           (133)  가상 도구 Tool 서브클래스 (complete/ask/run_skill) — loop이 인터셉트, **표준 키 유지** (prefix/추론 대상 아님). **`ask` 는 flat 단수 `{question}`**(질문 하나=op 하나; 여러 질문은 ask op 여러 개=read_file 식 배치) — 비-terminal 이라 응답이 observation 으로 accumulate. (legacy `questions[]` 도 `_extract_questions` 가 관용.)
│   ├── result.py            (15)   ToolResult 데이터클래스 (success, output, error, artifact)
│   ├── registry.py          (432)  12개 Tool 인스턴스 수집 → `TOOLS`(= `TOOL_SCHEMAS` alias), `_execute_tool`(tool.run, `ctx=RunContext` 전달), **`infer_action`**(action_input 키 prefix → 정확히 1개 도구가 claims 하면 복원, 0/2+는 None), `validate_tool_input`(3-tuple), `get_tool_descriptions(..., wire_format=None)` — **2단 레이아웃(attention)**: 전 도구 기본 소개(`- name:`+Input JSON) ROSTER 먼저, 그 다음 상세 GUIDES(prose+예시) — 모든 도구 소개가 어떤 참조 예시보다 앞섬(cross-tool 참조: code_index fetch 가이드가 edit_file 을 가리키나 옛 단일-tier 는 edit_file 을 그 뒤에 소개). 텍스트 동일(재그룹만; 가이드 첫 문장이 자기 도구 명시). **format-aware**: wire 가 `multi_op` 면 각 도구 description·param 키에서 자기 prefix(`{tool}_`)를 strip (flat `{action, params}` 컨벤션). **`_multi_op_flat_params`**: multi_op 일 때 배치 배열 param 을 **item-object 필드로 unwrap** 해 Input JSON 을 flat 단일-op 모양으로 노출 — `Tool.wrap_single_op` 의 flat→batch 매핑과 정확히 대칭. **모든 builtin 도구가 flat-native(Step 3)라 현재 전부 else-분기(배열 없음)로 그대로 통과** — unwrap 메커니즘은 MCP/미래 배치 도구용으로 유지. item 스키마는 schema 의 `items.properties` 에 이미 존재(per-tool 선언 불필요). **이걸 안 했더니 27B 가 광고된 배열을 그대로 베껴 옛 wrapper 를 json_fc 에서 emit 했음(DESIGN Exp 8 — root cause; inline 가이드만 flat 로 고치고 스키마 렌더를 안 고친 누락)**. **`_MULTI_OP_DESC_REWRITES`**: 배치 문장("Provide … as a list" 등)을 multi_op 에서 중립화하는 메커니즘 — 현재 **빈 dict**(모든 builtin 도구가 flat-native 라 description 이 native 단일-op). 추상화 표면으로 유지(미래 배치 도구용). 테스트가 잔존 배치 표현 0 단언. `exposes_complete=False` 면 `_ALWAYS_INCLUDE` 의 `complete` 를 생략. 기본(None/단수 포맷)은 바이트-동일 (스냅샷 테스트 가드). **`render_param_value`**(JSON-Schema property → `Input JSON` 값: type + required 마커 + 중첩 `array<object{k1, k2?, ...}>` 항목 키. MCP adapter 와 공유 — 두 도구 표면 렌더 일관). tool 모듈을 import하므로 `detectors`는 `validate_tool_input`을 lazy import (순환 회피)
│   ├── _diff.py             (68)   write_file/edit_file 공용 unified-diff 포매터 — **plain 표준 unified diff** (git diff 텍스트 형태, colour markup·gutter 없음). LLM observation 에 깨끗한 diff 가 들어가도록(=`[green]` 태그로 토큰 낭비/노이즈 없음); 색상은 렌더러가 라인 첫 char 보고 입힘 (CLI `_colorize_diff_line`, web `colorizeDiffBody`). 100줄 cap (`MAX_DIFF_LINES`) + `DIFF_TRUNCATION_PREFIX` summary
│   ├── _change_echo.py      (106)  write_file/edit_file 공용 **변이 후 echo** 조립기. `render_change_echo(old, new, path)` = `format_diff`(무엇이 바뀌었나) + `_updated_region_echo`(바뀐 영역 fresh `LINE#HASH` refs — 후속 edit 을 read_file 없이 체이닝) 를 한 블록으로. edit_file 은 항상, write_file 은 소량-덮어쓰기 갈래에서 호출 → **diff 를 내는 모든 관찰이 region refs 를 일관되게 동반**(이전엔 write_file 소량-덮어쓰기가 diff-only 라 비대칭). `_updated_region_echo` 는 `SequenceMatcher` opcodes 로 result-side 변경 span 수집→인접/겹침 window 병합→절대 줄번호 emit, `_MAX_REGION_LINES`(=100, `_diff.MAX_DIFF_LINES` 대응) 상한+`_REGION_TRUNCATION_PREFIX`. no-op(변경 0)은 빈 문자열
│   ├── read_file.py         (370)  파일 읽기 + hashline 포맷팅. `_read_one`(단일 파일: 부분/검색/stat 모드 dispatch) → ToolResult. **stat 힌트 cap-aware**: `_run` 이 `ctx.oversized_cap`/`ctx.tools_available` 를 `_read_one`→`_stat` 로 흘려, 전체 읽기 추정치(`_full_read_est_tokens`, 실제 hashline body 의 상한 근사)가 캡 초과면 stat 후속 안내에서 "full read" 미끼를 빼고 range/search/run 팬아웃으로 유도(작은 파일·headless cap=0 은 기존 full-read 힌트 무변경). 추정 상한성이 loop `_tool_observation` 의 실제 캡 판정과 일치(테스트가 seam 대조로 고정). **flat-native (consolidation Step 3)**: `ReadFileTool` 스키마 = flat 단일파일 `{path, line_start?, line_end?, search?, context?, stat?}` (required `path`) — `read_file_reads` 배치 배열·`read_file_` prefix 제거. `wrap_single_op`=identity, `_run`→`_read_one` 직결(full read 는 이미 split 한 라인 리스트를 `format_hashlines_range` 로 재사용 — 같은 텍스트 2회 split 제거, 출력 바이트 동일 테스트 고정, v4.39.0). 한 op=한 파일; 여러 파일은 멀티-op 포맷이 read_file op 을 여러 개 emit (op 배열이 곧 배치). (이전 batch `tool_read_file`/`_format_batch` 는 제거 — op-배열이 배치를 대신.) `key_prefix` 는 유지(latent: flat 키엔 strip no-op, `claims`=False)
│   ├── write_file.py        (172)  파일 생성/덮어쓰기. **새 파일/전면 재작성(≥30% 변경)**: 작성 content 를 hashline(LINE#HASH:content) 포맷으로 반환 → LLM 이 read_file 없이 방금 쓴 파일을 바로 edit_file 가능(write→edit 마찰 제거). **소량 덮어쓰기(<30% 변경, `_small_overwrite_analysis`)**: 전체 hashline 덤프 대신 `_change_echo.render_change_echo`(diff + 변경영역 hashline refs, edit_file 과 공용) + "다음엔 edit_file 써라" 넛지 → churn 케이스 echo 축소하면서도 후속 edit refs 제공 → ToolResult (+ WriteFileTool)
│   ├── edit_file.py         (389)  파일 편집 (hashline + 퍼지 매칭 + colored diff). ops: replace / append / prepend / delete (delete = pos..end 범위 제거, lines 없음 = replace+lines=[] 의 명시 형태) → ToolResult. **flat-native (consolidation Step 3)**: `EditFileTool` 스키마 = flat 단일편집 `{path, op, pos, end?, lines?}` (required `path,op,pos`) — `edit_file_edits` 배치 배열·`edit_file_` prefix 제거, `wrap_single_op`=identity. 한 op=한 편집. **같은 파일 다중편집 = 루프 레벨 배치 (`apply_edits_batch`)**: 연속된 같은-path edit_file op 들을 loop 이 묶어 이 순수함수로 라우팅 — 원본 1회 read → 모든 ref 를 그 원본 기준 해석(`_op_to_span`: op→half-open `(lo,hi,repl)` span) → overlap 사전거부(`_find_overlap`: 범위끼리 진짜 겹침·insert 가 범위 내부일 때만 거부, 인접 OK) → 줄번호 내림차순 **bottom-up** 적용(낮은 인덱스 안 밀림) → **1회 쓰기**. **all-or-nothing**: 한 op 라도 ref 실패/overlap 이면 무변경(부분쓰기 시 드리프트 재발 방지). 이로써 앞 편집이 줄을 밀어도 뒤 편집 ref 가 stale 안 됨 — 모델은 ref 를 **마지막 read 기준 그대로**(줄번호 미보정) emit. `fuzzy_verify_ref`/`render_change_echo`/`post_hook` 재사용, 단일 `tool_edit_file` 경로는 무변경(자기 메시지 보존). 다른 파일·비연속 편집은 per-op. `key_prefix` 유지(latent). (옛 `edits` 배열 머신러리는 read_file `_format_batch` 와 함께 제거됐고, 이 배치는 **중첩 배열이 아니라 flat op 의 루프 그룹핑** — 27B 깨뜨린 nested-array 함정 회피.) **편집 결과 출력 = diff + `Updated region` hashline echo**: 단일·배치 두 경로 모두 성공 시 `_change_echo.render_change_echo` 로 diff(무엇이 바뀌었나) + 바뀐 영역 fresh hashline refs 를 한 블록으로 붙임(구현·상한은 위 `_change_echo.py` 참조). no-op 편집(변경 0)은 diff·region 둘 다 무출력.
│   ├── _confine.py          (198)  **워크스페이스 경로 봉쇄** (default-on, `AGENT_CLI_WORKSPACE_CONFINE=0` 로 off). write_file/edit_file/shell 이 워크스페이스 루트(`os.getcwd()` 또는 `AGENT_CLI_WORKSPACE_ROOT`) *밖* 경로를 건드리면 `guard(paths, action)` 이 y/n/a 확인. **read_file 은 봉쇄 안 함**(커널/드라이버가 밖 헤더·툴체인을 대량 read → 프롬프트 폭풍 방지). `resolve_within` 은 `Path.resolve()` 로 `..`·심볼릭링크 탈출까지 canonical 비교. `a` 는 해당 디렉토리 서브트리를 `_session_root_allowlist`(shell `_session_allowlist` 의 path 판)에 추가 → 이후 무프롬프트. shell 은 `extract_shell_paths` 로 절대경로·`../` 토큰을 **best-effort** 추출(shlex; `-I/usr/include`·`--dir=/p`·redirect `> /p` 커버, `$(...)`·`python -c "…"`·변수 `$FILE` 는 **의도적 blind spot** — 사고 방지 speed bump 이지 샌드박스 아님). confirm/`can_prompt`/`interactive_lock` 은 shell 위험가드와 동일 인프라 재사용; `can_prompt()` False → hang 대신 refuse. 게이트 disabled·전부 내부 경로면 renderer import 없이 즉시 통과(zero-overhead).
│   ├── shell.py             (309)  셸 명령 실행 (**flat-native, Step 3**: 스키마 `{command, timeout?}` — `shell_` prefix 제거, `wrap_single_op`=identity; shell 이 마지막 flat 전환이라 이로써 **모든 builtin 도구 flat**) + 위험 명령 (rm/rmdir/mv) y/n/a 확인 (decision + 선택적 코멘트, env `AGENT_CLI_DANGEROUS_SHELL_CONFIRM=0`로 비활성) → ToolResult. **위험 키워드 강조**: `_detect_dangerous`(shlex 토큰 매칭)가 확인을 유발하면 `_danger_spans`(토큰-경계 정규식)가 강조할 `(start,end)` 문자 span 을 **origin 에서 단일 계산**해, 명령 원문·span 을 `confirm(command=, danger_spans=)` 구조화 필드로 전달(프롬프트 문자열에서 `$ cmd` 제거 → CLI 이중 표시 방지). 렌더러는 span 만 칠함 — CLI 볼드-레드, web `.danger` 스팬. 유발 토큰만 표시(따옴표 안·유사문자열은 span 비어 무강조). **위험가드 통과 후 `_confine.guard(extract_shell_paths(cmd), …)` 로 워크스페이스-밖 경로 게이트**(별개 축 — 파괴적이지 않아도 밖이면 물음). **프롬프트 가능 여부는 `get_renderer().can_prompt()` 로 판정** (구 `_is_tty()` 대체) — CLI는 TTY, web은 연결된 클라이언트(SSE+/api/input, TTY 불필요). 못 물어보면 hang 대신 명확한 refuse 에러. **확인 직렬화는 렌더 레이어의 공유 `interactive_lock`(RLock)** 사용 (confirm·ask 공통) — parallel delegate가 task별 워커 스레드로 돌기에 "한 번에 하나의 outstanding 프롬프트"를 보장해 응답이 물어본 워커로만 라우팅. shell은 락을 잡고 `_session_allowlist` 재확인 후 `renderer.confirm`을 호출(같은 스레드 RLock 재진입). `ask`(`_handle_ask`)도 동일 `can_prompt` 게이트 — 못 띄우면 `"(no response)"` 치환. 위험 확인 별칭: `y`(+yes/ok/okay/yep/yeah/sure), `a`(+always/**allow**), `n`(+no/nope) — 긍정 의도가 안전 기본값 deny로 오인되지 않게 확장(특히 프롬프트 라벨이 "always allow"라 `allow`→a). 출력은 잘리지 않고 그대로 LLM observation으로 전달 — 단 마스터 캡(`context_window//10`) 초과 시 `ShellTool.render_oversized` 가 **전체 출력을 `session_dir/shell-output-<hash>.txt` 에 lazy 저장(over-cap 때만)** 하고 그 파일을 가리키는 on-disk nudge(read/search·run 섹션 팬아웃)로 치환(headless=tee 폴백). **전체는 디스크에 보존·경로 announce** 라 옛 shell_artifact head/tail 의 silent 누락 문제(2026-05-19 제거 사유)와 무관 — 정보 유실 없이 컨텍스트만 보호. (이전 shell_artifact 가드는 head/tail 미리보기가 중간 디버깅 정보를 silent하게 누락시키는 사례로 제거.)
│   ├── fetch.py             (313)  웹 페이지 fetch → 마크다운 변환 → ToolResult (+ FetchTool). **flat-native (Step 3 완결, v4.38.0)**: 스키마 = flat `{url, depth?}` (required `url`) — 마지막까지 `fetch_` prefix 를 쓰던 builtin 이라 `claims`/`infer_action` 이 fetch 에만 live 였고 "모든 builtin flat-native" 불변식을 거짓으로 만들던 것을 flatten 으로 완결. `wrap_single_op`=identity; `key_prefix` 기본 유지 → 레거시 `fetch_url` emission(구 세션 재공급 prior)은 `strip_prefix` 가 계속 관용. **`render_oversized` override**: over-cap 내용을 `session_dir/fetch-output-<hash>.txt` 로 lazy 저장→가리키는 on-disk nudge(read range/search·run 섹션 팬아웃·더 좁은 URL/얕은 depth); headless/저장실패=제네릭 폴백
│   ├── agent_tool.py        (196)  **agent 도구 스키마 (5.0.0 통합 — 구 DelegateTool+TeammateTool)**: mode enum(run/spawn/request/status/resume/kill), C7 `validate`(mode 조건부 필수 `_MODE_REQUIRED` — run:task, request:key+message, resume/kill:key), 파라미터 profile(구 role/agent)·instructions(instant-agent — `compose_role_prompt` 합성, manifest 영속)·name·task·key·message·tools·context. **`parallel_safe=True` + `parallel_batchable(args)`=mode=="run"** — mode-aware 배칭: 한 턴의 연속 agent op 이 전부 run 일 때만 병렬(`{tasks:[...]}` 조립→`_run_parallel`), 상주 모드 섞이면 순차. **`SUBLOOP_DESCRIPTION`** — 레지스트리 없는 루프(서브에이전트·headless)용 축소 설명(run 만 문서화; §3.2 모드 축소: 도구 인스턴스는 하나, 프롬프트 렌더만 분기 — 상주 모드는 tool_agent 가 "main 전용" 에러로 거부). **`render_oversized`**: run 결과 over-cap 시 `<run_dir>/result.md`(=artifact) 가리키는 on-disk nudge + re-run-narrower(구 DelegateTool 정책 이식). `touched_paths`=`<agent:key|profile>` 마커. 실행은 루프 인터셉트(tool_bridge `_invoke_agent` — run→oneshot 엔진 / 상주→agents_live.tool_agent)
│   ├── context.py           (426)  read_context 도구 — **history 를 SQL 로 질의**. 필터 파라미터 더미 대신 **단일 `query`(SQL SELECT)** 프리미티브: history.jsonl 을 인메모리 sqlite `history` 테이블로 온-디맨드 적재(컬럼 `session/loc/seq/kind/turn/ts/tools/files/author/text`)하고 LLM 이 SELECT 작성. `text` 컬럼이 각 레코드의 **전체 내용**(검색·읽기 표면). 컬럼은 **읽기 시점에 `manager._classify_record`(kind/tools/text) + `extract_file_paths`(files) 로 유도** → 어떤 레코드 shape 든 동작, prefix-관습 재추측 없음. `turn`/`ts`/`author` 는 레코드에서. SQLite 는 `code_index._sqlite` shim 경유 **lazy 로드**(stdlib→pysqlite3 폴백; **코어 도구라 sqlite 부재여도 모듈 import 안 깨짐** — 쿼리 시 친절 에러). **읽기전용**: 비-SELECT prefix 거부 + 인메모리 DB 콜마다 재빌드·폐기(쓰기 무해)가 1차 가드, sqlite authorizer(SELECT/READ 외 거부)는 belt-and-suspenders(상수 없는 pysqlite3 빌드면 skip). **결과는 행/셀 캡 없이 VERBATIM 반환**(이전 50행 cap + 200자 셀 절단·공백 collapse 제거 — read_context 가 청크 회수 시 내용을 망가뜨리던 버그 수정) — 결과 크기는 loop 의 과대 출력 캡이 관장하되 **`ReadContextTool.render_oversized`** 가 SQL-native nudge(LIMIT/projection/`substr(text,1,200)` 재쿼리 — `narrow_oversized_nudge`)로 치환, 모델은 그 안내대로 작게 유지. `query` 생략 시 스키마+예시+세션목록(discovery). `sessions`(current/all/<id>) = 테이블에 적재할 데이터 범위. `files` 컬럼은 다음 BM25(FTS5)와 같은 sqlite 기반.
│   ├── memory_tool.py       (155)  memory 도구 — `agent_cli.memory` 스토어의 LLM 인터페이스. flat-native `{mode,...}`(add/get/update/delete/list), `_run(args, session_dir=)` → `_dispatch`. `_as_id` 가 int·bare-int 문자열 수용(bool 거부). `MemoryError` → 친절 실패 ToolResult(크래시 아님). description 이 "실패=반복회피/발견=재사용, dead-end·비자명 학습 시 proactive 기록" 유도. session_dir=None → 명확 에러.
│   └── code_index.py        (753)  code_index 도구 — `agent_cli.code_index` 패키지의 native-tool wrapper. **flat-native (consolidation Step 3)**: `CodeIndexTool` 스키마 = flat 단일쿼리 `{mode, path?, name?, symbol_kind?, ref_kind?, search?, with_*?, depth?, max_bytes?}` (required `mode`) — `code_index_queries` 배치 배열·`code_index_` prefix 제거, `wrap_single_op`=identity. 한 op=한 쿼리; 여러 쿼리는 멀티-op 으로 code_index op 을 여러 개 emit(읽기전용이라 순서/상태 의존 없음 — read_file `reads[]` 와 동형). `_run(args)→_dispatch_one(args)` 직결. (옛 batch `tool_code_index`/`_format_batch` 는 read_file `_format_batch` 와 함께 제거.) `key_prefix` 유지(latent). `_dispatch_one`(per-query mode dispatch)는 유지: 10 mode dispatch (list/fetch/lookup/kind/file/refs/callers/callees/slice/build). 인덱스 root 자동 해석 (cwd 또는 가장 가까운 조상 `.agent-cli/`), lazy build + per-query incremental refresh. list/fetch는 root 바깥 path에 대해 on-demand parse fallback (DB 갱신 없음); 나머지 모드는 index-scoped (out-of-root 명시적 거부). fetch 결과는 hashline 포맷 → edit_file 직결. `post_hook(path)`는 edit_file/write_file 성공 직후 호출되어 자동 incremental refresh — 모든 예외 swallow (인덱싱 hiccup이 user-facing op 막지 않음). `_resolve_defs_path(root)`가 `<root>/.agent-cli/defconfig` 존재 시 `build(defs_path=...)`로 전달 — kernel/driver처럼 `#ifdef CONFIG_*` 가 함수 시그니처를 분기하는 코드에서 tree-sitter 파싱이 ERROR로 떨어져 정의가 누락되는 케이스를 unifdef 사전 분기 제거로 살림. 파일 부재 시 `None`이 그대로 통과해 기존 무전처리 동작 유지. 모듈 레벨 `_BUILD_LOCK` (threading.Lock) 이 `_ensure_index` / `post_hook` / `_do_build` 의 `build()` 호출을 직렬화 — 병렬 delegate worker 가 동시 진입해도 중복 빌드 없음. (atomic write 가 correctness 를 책임지고, 락은 효율 + SQLite 락 경합 회피 책임). **`CodeIndexTool.render_oversized`**: over-cap 결과는 index-native nudge(`mode=fetch` 단일 심볼·`search` 필터·`max_bytes`·특정 path/name 스코프 — `narrow_oversized_nudge`)로 치환(파일/팬아웃 아님 — 재쿼리가 정답).
│
├── code_index/                     code_index 패키지 — tree-sitter SQLite 코드 인덱서 (`minish.ai/Agent-tools tsindex.py` Apache 2.0 port — NOTICE 참조). 총 ~5,000 LOC. `_sqlite.py` shim 이 stdlib `sqlite3` 우선 / 미존재(`--without-sqlite` CPython) 시 `pysqlite3-binary` 폴백 — Linux 잠금 서버에서도 무설정 동작.
│   ├── __init__.py          (56)   public API: build / load_index / build_callgraph / cmd_slice / IndexStore / Symbol / Ref / NAME_KINDS / CODE_NAME_KINDS / REF_KINDS / SCHEMA_VERSION
│   ├── schema.py            (~140) SCHEMA_VERSION=2 (v2: `qualified_name` 컬럼 추가, walker가 emit 시 full display form 산출 — Python/JS/TS/Java/Go/Rust/Markdown은 `.`, C++는 `::`, C는 flat=name; tool handler가 qualified_name 우선 lookup + bare-leaf fallback). Symbol/Ref dataclass, NAME_KINDS(5-vocab: function/type/variable/constant/section), CODE_NAME_KINDS(=NAME_KINDS-{section}, cross-file ref name resolution 전용 4-vocab), REF_KINDS(call/name/type). `section`은 markdown heading 5번째 vocab으로 추가됨 (upstream 4-vocab → 5-vocab).
│   ├── preproc.py           (473)  C/C++ 전처리: unifdef 드라이버 + rewriter chain (foreach/decl_macro/bare_attribute/variadic/ifdef_zero/define_comments/pp_trailing_ws/consecutive_attr/pp_continuation/type_arg). `_apply_unifdef` 헬퍼가 백엔드 선택 (시스템 `UNIFDEF_BIN` 우선, 없으면 `_unifdef.run_unifdef` 폴백). `compute_preproc`이 fingerprint 산출 — defs file 내용 변경 시 인덱스 자동 invalidate. 백엔드 선택 정보 `preproc_info["backend"]` 에 노출.
│   ├── _unifdef.py          (653)  Pure-Python `unifdef -b` 구현 — Pratt-style 표현식 parser (defined/논리/비교/산술/비트), UNKNOWN 전파 + short-circuit 평가, directive walker (TAKEN/NOT_TAKEN/PASS_THROUGH 상태 스택). `-b` 라인 보존 contract 준수. 시스템 unifdef 와 byte-identical parity (parity 테스트로 8 케이스 보장). preproc.py 가 백엔드 fallback 으로 사용 — 시스템 binary 없는 잠금 서버에서도 무설정 동작.
│   ├── store.py             (257)  IndexStore (SQLite reader). find_symbols/find_refs/find_refs_in_range, normalize_file_path (exact/absolute/basename/suffix), kind_counts/ref_kind_counts/top_ref_names. dict-style 접근(`idx['symbols']`)도 호환 유지.
│   ├── builder.py           (492)  build() — Pass-1(definitions) + Pass-2(refs) + sha1 incremental + Option-B re-Pass2 (변경 파일의 새 이름을 mention하는 unchanged 파일 자동 re-walk). `iter_source_files`가 `_SKIP_DIRS` (.git/.agent-cli/.claude/.venv/node_modules/build/dist 등) prune → 인덱스 폭주 방지. 무효화 트리거 3개: schema_version mismatch / meta.root 변경 / preproc_fingerprint 변경. `write_sqlite_index` 는 atomic tmp + `os.replace` 패턴 — 활성 DB 파일을 절대 unlink/truncate 하지 않고 옆에 새 tmp 파일을 만든 뒤 한 번의 rename 으로 swap. 병렬 run worker 가 같은 인덱스에 동시 접근해도 `sqlite3.OperationalError: disk I/O error` race 안 남.
│   ├── callgraph.py         (115)  build_callgraph → (calls_of, callers_of, sites_of). 호출 사이트 (caller, callee, file, line) dedup으로 walker의 call+name 더블 emit을 1 edge로 정리. callback-only(kind='name' 단독) 사이트는 1× 그대로 유지.
│   ├── slice.py             (194)  cmd_slice → LLM-context markdown blob (definition + 선택적 callees/callers/types/macros, depth/max_bytes 캡). stdout 출력 대신 str 반환 (tool 통합).
│   └── languages/                  per-language walker 모듈 (lazy import — Python-only 프로젝트가 Rust grammar wheel 비용 안 냄)
│       ├── __init__.py      (~160) LangSpec dataclass + LANGUAGES dict + lazy `_ensure_loaded()` + `language_of(path)` / `get_supported_extensions()` helpers (prompt inline guide + error 메시지 single source)
│       ├── _shared.py       (36)   `text(node, src)` 공통 helper
│       ├── python.py        (~330) 함수/클래스/decorated/UPPER_SNAKE → constant. async/decorator modifiers. nested def/class도 emit (parent dotted chain).
│       ├── go.py            (~290) func/method (receiver type → parent), type/const/var, exported(uppercase) modifier. selector_expression call site.
│       ├── rust.py          (~420) function_item / function_signature_item (trait body sig은 is_definition=False with parent=trait_name), struct/enum/trait/type_item, impl block methods. macro_rules! → kind=function.
│       ├── java.py          (~340) class/interface/enum/abstract method/field. interface method = is_definition=False. generics, variadic args.
│       ├── javascript.py    (~490) function/class/method/field/lexical. const/let/var → kind 결정. arrow fn / generator (`function*`) → modifiers ['generator']. JS 헬퍼는 typescript.py가 import해 재사용.
│       ├── typescript.py    (~270) interface/type_alias/enum + js의 헬퍼 재사용. walk_refs는 type_identifier 추가 처리 → kind='type' ref emit (정의 사이트 제외).
│       ├── c.py             (~550) self-contained C walker (add_function_def/declaration/record/typedef/macro/c_walk_definitions/c_walk_refs). preprocess slot은 preproc.preprocess_source.
│       ├── cpp.py           (~725) self-contained C++ walker (template/namespace/class). C helper를 복제 보유 — upstream의 'language="c" inside .cpp' oddity 회피, .cpp 파일은 일관되게 language="cpp".
│       └── markdown.py      (~225) ATX (`## heading`) + setext heading walker. kind='section', kind_raw='atx_heading_N'/'setext_heading_N', parent stack chain, end_line은 다음 same-or-higher level heading 직전. refs 없음.
│
├── context/                        컨텍스트 관리
│   ├── __init__.py          (14)   re-export
│   ├── token_estimator.py   (23)   토큰 추정 (chars/4)
│   ├── overflow.py          (105)  프로바이더별 오버플로 감지 (`is_context_overflow` 패턴 — Anthropic/OpenAI/omlx 등 OpenAI 호환 서버 커버) + `parse_overflow_amounts`로 400 메시지에서 실제 prompt 토큰·상한 추출. omlx/Anthropic은 actual·limit이 한 구문에 묶여 결합 regex로 함께 캡처("N tokens exceeds max context window of M" / "N tokens > M maximum"); OpenAI/vLLM은 limit과 actual을 **독립 추출**해 actual 표현(버전별 "resulted in" / "you requested" / "contains at least")이 달라도 limit 추출이 깨지지 않게 함 — probe는 limit만 쓰고 recovery는 actual을 best-effort(None시 로컬 추정 fallback)로 사용. omlx 패턴은 실서버 검증 (2026-05-30)
│   ├── records.py           (163, v4.47.0 C5) on-disk record shape 의 계약 — iter_record_ops(양쪽 assistant shape 단일 리더)·_classify_record(kind/tools/text 분류)·_op_summary. subagent/report·review·tools/read_context 가 공개 소비(예전 manager private 침범의 정당화)
│   ├── render.py            (219, v4.47.0 C5) record → LLM 표현 — _to_natural_language(재공급)+_convert_observation+_context_view·_estimate_message_tokens(예산 — 재공급과 같은 view 를 세는 쌍둥이)·_to_summary_text(압축 요약 입력). 전부 무상태(인자만)
│   ├── store.py             (95, v4.47.0 C5)  영속화 I/O primitive(정책 0) — append_record/load_records(history.jsonl)·save/load_compaction(원자·버전 스탬프)·fork_history. 경계는 A/B 비교 후 함수형 확정(_dynamic_start_index 소유권 역전으로 상태-소유 클래스 성립 불가; 승격 트리거=두 번째 백엔드/fake-store 수요)
│   ├── manager.py           (888, C5 후 캐시+압축 정책 본연 — record 계약은 records·LLM 표현은 render·디스크 I/O 는 store 소비) ContextManager (토큰 budget 압축 + FIFO fallback). **`compaction_ratio` (5.14)**: 라이브 압축 목표 비율 필드(기본 `DEFAULT_COMPACTION_RATIO`=0.8, `set_compaction_ratio` 가 [0.5,0.95] clamp). loop 이 상수 대신 `self.ctx.compaction_ratio` 를 매 콜 읽으므로(llm.py), web·loop 공유 ctx 인 웹 슬라이더가 dirty-flag 없이 즉시 반영. 서브에이전트는 `create_subagent_ctx` 가 parent 값 상속(spawn 시점 스냅샷). **`get_messages` 증분 렌더 캐시 (`_nl_cache`, v4.37.0)**: 동적 슬라이스(선두 system 제외)의 자연어 변환을 record 당 **1회**만 수행 — `add` 가 렌더 결과를 미러 리스트에 append, `get_messages` 는 포인터 복사로 서빙(이전엔 턴당 3-4회 호출 × 전체 재변환 = 세션 O(n²), 유일한 초선형 경로). 벌크 변형 4곳(`_compact` 재할당·`_evict_fifo` pop·`force_fit` pop·`_restore_cache`)이 `None` 무효화 → 다음 get 에서 1회 전체 재렌더; 길이 불일치 backstop 이 누락 변형 방어. **전제**: 렌더가 record 의 순수 함수(현재 `_context_view`=identity) — 미래 턴-의존 context view 는 캐시 무효화 필요(코드 주석 명시). **관찰 complete-nudge (5.15.0)**: `get_messages` 가 피드 시점에 **현재 관찰(마지막 cache 레코드가 도구 결과일 때)**에만 complete-리마인더 한 줄(`_OBS_COMPLETE_NUDGE`)을 붙임 — 복사본에 append 라 `_nl_cache`·`_cache`·history.jsonl 무변경(**미저장·비누적**, 매 콜 재파생, resume 무영향). 모델이 작업 후 `complete` 를 안 내고 멈추거나 서브에이전트를 재확인하며 헛도는 것을 종결 결정 지점(관찰 직후)에서 유도 — complete 정당 사유 2종(할 일 없음 / 다음 진행이 서브에이전트 회신에 막힘) 명시. 무조건(플래그 없음): Qwen3.6-27B 실측 무해(조기완료 0; 그 모델은 항상 clean 종결이라 혜택은 mis-terminate 하는 모델에서만) + 비저장이라 게이트 불필요, 관찰-측 텍스트 변형이라 mimicry-safe. **개입 fold (v4.51.0)**: `fold_resolved_interventions()` — 형식-복구 개입(NO_THOUGHT/NO_JSON/NO_ACTION/A4/A5, additive `recovery:"format"` 마킹+레거시 `tool==""` 백스톱 — `records.is_format_intervention` 계약)은 교정의 일회성 재료라, 다음 파싱 성공 시 [실패 prior, 개입] 쌍을 **캐시 뷰에서 전량 접음**(dynamic context=성공 궤적만; 연속 실패도 성공 순간 일괄). live=dispatcher 파싱-성공 지점 호출(`assume_tail_resolved`), resume=`_restore_cache` 가 레코드-기반 재판정(개입 뒤 ops-보유 assistant 존재)으로 동일 뷰 재현 — 사이드카 무상태. **history.jsonl 불변**(관측·디버그 보존, turns.jsonl 통계 무손실). B1(행동 루프) 개입·도구 실행 실패는 과제 정보라 비대상. v3.16.1 mimicry 교훈의 관찰-측 확장 — 단 변형이 아닌 **제거**라 모방 표면 없음. **`iter_record_ops(record)`** (public): assistant 레코드 한 건에서 `(action, action_input)` 쌍 추출 — 멀티-op `ops` 레코드와 단수 legacy `action` 레코드 **양쪽 shape 의 단일 reader**. delegate activity-log 추출기·loop review tool-calls 빌더가 소비(각자 shape 을 재추측하다 423608e 이후 silent 하게 깨졌던 회귀의 수리, v4.35.1). **`_context_view(message)`**: 어시스턴트 turn 의 재공급 표현 — op별 `Tool.render_action_input_for_context`(기본 identity) 적용한 **복사본**(원본 record 불변). render(`_to_natural_language`)+estimate(`_estimate_message_tokens`) 양쪽에서 호출 → 재공급=카운트 일관(큰 write/edit 본문 elide 대비 seam; 현재 identity 라 무영향). **history.jsonl retrieval enrich**: `_append_to_history` 가 round-trip 메시지에 **검색 키를 가산**해 파일에 기록(`_enrich_record`; 세션dir 소실 가드 `mkdir` 는 v4.39.0 부터 **실패 시에만**(FileNotFoundError→mkdir+재시도) — add 당 stat 1회 절약, 외부 wipe 복구 의미 보존. 파일핸들 유지는 의도적 비채택: fd 가 unlink 된 inode 에 계속 쓰면 소리 없는 유실) — `kind`/`turn`/`ts`/`tools`/`files`(`extract_file_paths` 재사용 — 조작 파일 경로)/`text`(+`author` passthrough). `kind`/`tools`/`text` 는 `_classify_record`(레코드 shape → query/action/observation/final/raw/system, `[author]:`·`Observation:` prefix 벗긴 평탄 text)로 유도, `turn` 은 loop 이 매 턴 경계 `set_turn`. **파일만 enrich** — `_cache`/`get_messages`(LLM 경로)는 무변경(round-trip 필드 그대로, extra 키 무시). read_context(JSON 쿼리)와 외부 jq 가 이 키들을 쓴다. (하위호환 무시 — 구 세션은 키 없어 쿼리에서 자연 제외.) **`ensure_within(target)`** (flow 1 예방형): loop이 매 호출 직전 `target=(C−S−O)×0.8`(S=system 실측)로 호출 — `_cache_tokens > target`면 LLM 요약 compaction 시도 (system anchor만 보존 → oldest 절반 evict → 단일 호출로 요약, 이전 summary가 있으면 같은 호출에 prepend하여 recursive 갱신 → `_file_extract`로 touched paths 누적 dedup → `[system][summary][file_list][retained]`로 캐시 재구성 → `compaction.json` atomic write). **요약 입력은 `_to_summary_text`로 만든 자연어 transcript를 user 메시지 하나로 감싼 형태** — `get_messages`의 `_to_natural_language`(assistant를 ReAct JSON으로 round-trip)와 달리, 요약 경로에선 assistant를 산문으로 풀고 action_input을 owning `Tool`의 **`Tool.summary_arg`**(`touched_paths`의 sibling — `strip_prefix`로 표준 키를 읽어 prefix/배열 셰이프 흡수; registry lazy import로 순환 회피)로 축약(파일 본문 제거)해 모델이 "transcript를 요약"하게 함(이전엔 ReAct JSON 대화로 보여 소형 모델이 요약 대신 다음 `write_file` 액션을 생성하던 버그). **(이전 버그: 구 `summarize_tool_args`가 bare `args.get("path")`를 읽어 wire-key prefix 도입 후 모든 실 레코드에서 빈 라벨 — `write_file()`처럼 인자 누락. tool-result 레코드는 args가 없으므로(=`{role,tool,success,content}`) 라벨은 assistant 액션 레코드에서만 나옴. 테스트가 가짜 `args:{path}`·bare `action_input:{path}` shape을 써서 미검출 — `_file_extract`와 동일하게 `serialize_assistant_for_history` 실제 출력 기반 회귀가드 추가) **멀티-op record 처리(`_file_extract` 동형)**: 멀티-op 포맷은 `{ops:[...]}` 저장 → op 순회로 각 op 라벨, flat op 은 `wrap_single_op` 으로 캐노니컬 정규화 후 `summary_arg`. (이전엔 top-level `action` 만 읽어 json_fc 기본값 요약이 thought-only 였음 — 도구 호출 기록 증발; json_fc 회귀가드 추가) dangling assistant 턴 없음 → 연속 유인 제거. 요약 실패하거나 재구성된 캐시가 여전히 target 초과면 belt-and-braces로 `_evict_fifo(target)` 발동 — 무한 트리거 루프 방지. `add()`는 compaction 트리거 안 함(append만). **`compact_now()`** (수동 `/compact`): `_compact` 1회 실행 후 `(before, after)` 토큰 반환 — disabled/compactor 없음/evict 대상 없으면 no-op(equal), 실패 시 warning만(강제 FIFO 안 함). **`reconcile_actual_tokens(actual, system_tokens)`**: 호출 직후 서버 실측(`usage.input_tokens`+cache)으로 `_cache_tokens = actual − system`으로 re-anchor → chars/4의 CJK 과소평가가 턴 간 누적되지 않음(drift 1턴치). **`force_fit(target, actual_tokens)`** (flow 2 반응형): 서버가 400(prompt too long)으로 거부하면 loop이 호출 — 로컬 추정(chars/4, CJK 과소)을 못 믿으므로 서버가 알려준 `actual_tokens`로 reconcile 후 compact→FIFO로 비율 축소. keep_ratio=target/actual로 줄여 추정 과소배율이 분자분모에서 상쇄(추정 절대정확도 불필요); progress 보장(매 호출 최소 1개 evict, anchor=최신 1개 보존). `actual_tokens` 없으면 ~25% trim fallback. `compaction_enabled=False`로 끄면 기존 FIFO만 동작. Resume: `compaction.json`의 `dynamic_start_index`로 history.jsonl 후방 슬라이스만 cache 복원해 summarised tail과 중복 방지. 인스턴스마다 wire_format plugin attach (`__init__(wire_format=...)`, default fallback="react"). `get_messages()`는 system은 verbatim, user/tool branch만 자체 처리하고 assistant branch는 `wire_format.render_assistant_from_history`에 위임 — 한 세션 = 한 wire_format으로 격리. Compactor 콜백(`set_compactor`)과 `TurnRecorder`(`set_recorder`)는 `AgentLoop`가 후입식으로 주입 — unit-test 경로는 미주입 상태로 즉시 사용 가능.
│   ├── _file_extract.py     (74)   `extract_file_paths(messages)` — evict 된 assistant record 의 `action`→owning `Tool` 의 **`Tool.touched_paths(action_input)`** 에 위임. path/prefix 키 지식을 각 도구에 둠(=`strip_prefix` 재사용; write/edit/read/code_index=flat `{path}`, delegate=flat `{agent}` placeholder — 전부 flat-native Step 3) → 도구가 입력 셰이프를 바꿔도(예: read_file 의 flat-native 전환) extract 가 자동 추적. **멀티-op record 처리**: 멀티-op 포맷(json_fc·xml_fc)은 `{ops:[...]}` 로 저장하므로 op 리스트 순회(single-op `{action,action_input}` 은 `[msg]` 로 정규화); 저장 op 은 flat(모델 emission)이라 `touched_paths` 전에 **`Tool.wrap_single_op` 으로 정규화**(현재 모든 builtin 도구 identity 라 사실상 no-op — MCP/미래 도구용 단계). registry 는 함수 내 lazy import 로 module-load 순환(registry→context-tool→manager→_file_extract) 회피. 입력 순서 dedup. compaction 시 file_list 단일 진입점. **(이전 버그: ① bare `path`/`tool-result args` 가정으로 wire-key prefix 도입 후 file_list 빈 채 — 회귀가드 추가. ② top-level `action` 만 읽어 멀티-op `{ops}` record 의 경로를 전부 놓침 — json_fc 기본값(2026-06-11~) file_list 가 줄곧 비었음; ops 순회+wrap 정규화로 수정, json_fc 회귀가드 추가)**
│   └── session.py           (211)  세션 메타데이터 (session.jsonl — id/workspace/updated_at/`response_format`. response_format 은 세션이 돈 wire format 을 기록 — **v5.19.0 부터 resume 이 실제로 읽음**: 해석 체인 순위 2(명시 플래그 다음), 명시 플래그로 전환 resume 하면 활성 포맷으로 메타 갱신(meta=마지막 실행의 truth). default 는 `DEFAULT_WIRE_FORMAT`(json_fc); response_format 키가 없는 옛 세션도 현재 default(json_fc) 로 로드 — backward-compat to 이전 default(react→prefix_md) 는 의도적으로 미보존) + resume용 user↔assistant 페어 추출 (recent_exchanges) + `session_summary(meta)` = 마지막 (user 요청, 결과) 한 쌍을 history 에서 읽어 반환 (제거된 `query` 메타 필드의 대체 — sessions 목록/resume 프롬프트가 공유). System-injected user 메시지 필터는 `wire_formats.all_system_user_prefixes()` (format-agnostic 프리픽스 + 등록된 모든 plugin의 framing prefix) 단일 진입점 사용 — 새 wire format plugin 추가가 자동 반영
│
├── prompts/                        프롬프트 템플릿
│   ├── __init__.py          (1)
│   └── system_prompt.py     (1160) Attention 최적화 시스템 프롬프트 빌더. **DIRECTIVE 스코프 (U-C 5.1.0, DESIGN §3.7)**: `split_directive_scopes(text)` 가 `## @main`/`## @agents` 라인 마커로 본문을 {common, main, agents} 로 분할(블록=다음 `## @` 마커/EOF 까지 — 일반 `##` 헤딩은 안 끊음; 무마커 파일=본문 그대로 common=5.0 바이트 동일; 마커 라인은 렌더 제거; `DIRECTIVE_SCOPE_MARKER` 정규식은 web/directives.py 의 learned-append 위치 결정과 공유), `_load_directives(audience)` 가 common+해당 스코프만 조립, `join_directive_scopes`(split 의 역 — 스코프 에디터 저장 직렬화) 동거, 조립 지점은 `depth==0 → "main" / else "agents"` (run·spawn·skill 전 서브루프=agents). **`build_system_prompt_sections()`** = 단일 조립 지점 — (이름, 텍스트) 섹션 리스트 반환(Role/Context Discipline/Task Guidelines/Response Format/Available Tools/[MCP Tools]/[Skills]/[Agents]/Environment/[Context Recovery]/[Directives]/[Session Memory]/[Execution Context]); `build_system_prompt()`는 그 join wrapper(바이트-동일). **[Session Memory]** = `session_dir` 있고 `memory.jsonl` 비어있지 않을 때 `agent_cli.memory.render_index(session_dir)` 로 상시 인덱스(요약만) 주입 — 매 빌드 fresh read 라 `memory add` 후 rebuild 시 반영·resume 복원. loop 이 `consume_memory_reload()`(directives 와 같은 dirty-flag 패턴)로 mutating op 후 rebuild + `notify_memory_applied()`(→ SSE `memory_changed`)로 **열린 Prompt Inspector 의 prompt 뷰 live 재fetch**(메모리는 에디터 없어 prompt-only). 인덱스 스캐폴딩(헤더·hidden 꼬리)은 영어(시스템 프롬프트 일관, 요약 본문은 LLM 작성 언어). 섹션 이름은 조립 시점에 부여 — 합쳐진 문자열의 `##` 재파싱은 본문 헤딩(도구 가이드·format_rules 예시) 때문에 불가하므로 구조를 원천에서 노출(Prompt Inspector 소비) (Primacy/Middle/Recency, Role 상속, Context Recovery Guide). **format-aware 툴 가이드 (멀티-op 2단계 — DESIGN §5)**: 4개 인라인 빌더(read_file/edit_file/code_index/delegate)가 `wire_format.multi_op` 분기 — multi_op 면 per-tool 배치 prose·예시를 생략하고 단일-대상(op 하나=파일/edit/query/task 하나) 예시를 `render_action_input` 경유 flat 렌더 (op 배열이 곧 배치 — 배치 중첩이 27B 90% 깨뜨린 실측 근거). **read_file 빌더는 flat-native(Step 3) 이후 예시가 항상 flat 단일파일** — `multi_op` 분기는 intro 문구(멀티-op: "한 턴에 read_file op 여러 개" / 단수: "한 호출=한 파일")만 가르고, `{reads:[...]}`·"5. Batch" prose 는 제거(read_file 에 batch 셰이프 자체가 없어짐). `_ASK_INLINE_NO_COMPLETE` = `exposes_complete=False` 포맷용 ask 가이드 변형 (`complete` 호출 대신 thought-only finish 로 표현). **react 도 Step 2(2026-06-13)부터 multi_op=True** 라 multi_op 분기를 탐 — 출하 포맷 둘 다(react/json_fc) multi-op 이고, else(단수) 경로는 synthetic 포맷만 도달(정리는 roadmap Step 3/4). 스냅샷 가드(tests/snapshots/tools_section_react.txt)는 이제 react 의 **multi-op flat** 렌더를 고정. `build_system_prompt(wire_format=…)` — Response Format 섹션은 `wire_format.format_rules()`, 스킬·에이전트 호출 예시는 `wire_format.render_full_example(thought=None, ...)`, 도구 inline 가이드의 action_input 단편은 표준 키 dict로 작성되어 `wire_format.render_action_input(dict)`이 wire별 직렬화 (react·json_fc 둘 다 prefixed dict→flat op `{action, ...plain params}` 로 변환; 비-JSON plugin이 swap), 도구별 `{tool}_` prefix는 `Tool.add_prefix`로 적용 — 가이드는 표준 키로 쓰고 prefix·직렬화는 단일 출처(`_rai_prefixed`)에서. 인라인 예시는 wire 셰이프로 감싸지 않음 — 와이어 셰이프 학습은 Format Rules + skill/agent 예시(각 1번)에서 일어나고, 인라인은 mode 분기 / 의미론 학습. Recency 순서: Environment → Recovery → Directives → Execution Context (passive→active, persistent→immediate; Execution Context만 동적이라 끝에 배치 → 앞 3개 KV cache 안정). Tool inline 가이드는 `_build_tool_inline_guides(active_tools, wire_format)` 가 매 호출마다 빌드 — `read_file` 가이드의 Flow 문장이 `code_index` 활성 여부에 따라 분기 (활성 시 supported 확장자 파일은 `code_index mode='list'`로 우회 — 확장자 목록은 `code_index.languages.get_supported_extensions()` 단일 출처에서 가져와 walker 추가가 자동 전파). code_index 가이드는 per-file (list/fetch) vs index-wide (lookup/kind/file/refs/callers/callees/slice) scope 경계를 명시, on-demand parse fallback 위치도 안내. **code_index 도 flat-native(Step 3)**: `rai` 헬퍼가 항상 flat 단일쿼리 렌더, `{queries:[...]}` batch 예시·"LIST" prose 제거(read_file 와 동형). edit_file 가이드(`_build_edit_file_inline` — op 시맨틱·hashline·constraints 는 wire 공통 텍스트, flat 단일편집 예시만 `render_action_input`으로 wire별 렌더)는 (1) 편집 직전에 CURRENT turn에서 read 하도록 요구(code_index mode='fetch'도 fresh read로 카운트) (2) hash mismatch를 failure가 아닌 guardrail로 reframe해 모델이 panic 없이 re-read/retry 하도록 톤 조정 (3) **파일 CONTENT 작성은 write_file/edit_file — shell heredoc(`cat <<EOF`) 금지** nudge: 코드가 shell+JSON 이중 escape 돼 NO_JSON 빈발(세션 1782027249 실측 지배)이라, write_file 는 본문을 전용 필드에 담아 escape 한 겹. **edit_file flat-native(Step 3)**: 한 op=한 편집(`edits` 배열 제거), `multi_op` 분기는 framing 문구만 가름. **same-file 배치 안내(multi_op 분기)**: "같은 파일 다중편집은 한 턴에 연속 edit_file op 으로 — ref 는 마지막 read 기준 그대로(줄번호 미보정), 함께 bottom-up 적용되어 stale 안 됨, overlap 은 배치 거부, 같은-파일 op 은 인접 유지"로 **허용**을 안내(루프 `_dispatch_edit_batch` 가 실제 처리). 옛 "턴을 나눠 re-read" 문구를 대체. 단수(react 아닌 synthetic) 분기는 한 턴 1-op 이라 여전히 턴 분리 안내.
│
├── input_queue.py           (109)  공용 입력 큐 (P5) — web WebServer 와 CLI run 큐 펌프가 공유하는 골격 (deque+Condition, SHUTDOWN sentinel identity 계약, timeout dequeue, on_change 콜백). 아이템 {id, conn_id, nickname, text} 는 web 큐 표시·cancel 소유권 계약
├── subagent/                       서브에이전트 실행 계층 (5.0.0 agent 통합 — docs/agent-unification/DESIGN.md; 상주 설계 원전=docs/teammate/DESIGN.md)
│   ├── __init__.py          (6)    docstring only — 소비자는 서브모듈 직접 import (가변 전역 재수출 금지, C2 교훈)
│   ├── runner.py            (209)  run(일회성)과 상주 spawn 이 공유하는 3단계: `apply_role_overrides`(프로파일 md config 오버레이 — 파싱된 config dict 만 소화, hooks 는 caller 위에 병합) · `create_subagent_ctx`(none/fork/resume ctx 생성 + **wire format 해석: effective model(role 오버라이드 후, `model=` kwarg)의 models.json 바인딩 > 부모 상속** (v5.19.0 — main 과 다른 포맷으로 도는 서브에이전트; unknown 바인딩 이름은 spawn 거부 `(None, error)`, 부모와 다르면 debug_log 1줄) + 예산 상속 + **현재 스레드의** 인스펙터 스코프에 ctx 등록 v4.52.0) · `run_subagent_message`(ctx 위에서 run_loop 1회, depth+1 — run 은 1회 후 ctx 폐기, 상주는 같은 ctx 로 반복: 이 차이가 두 모드의 전부). 무거운 의존은 함수-내부 import (순환 회피, 테스트로 고정)
│   ├── profiles.py          (70) **통합 프로파일 로더 (5.0.0 PR-1 — 구 delegate/agents.py + roles.py 병합)**: 검색 경로 `.agent-cli/agents/` → `~/.agent-cli/agents/` → 패키지 내장 `agents/builtin/` **단일**(구 teammates/ 경로 폴백 없음 — 하위호환 포기 결정; 내장=범용 워커 5종 code-writer/code-reviewer/code-analyst/unittest-writer/log-analyst + **orchestrator**(v7.18.0 peer 설계: /orchestrate 가 워커를 idle 소환 후 로스터를 인계, orchestrator 가 peer `message` 로 조율 — author 기반 회신 라우팅(agent: 요청의 회신→요청자 inbox, expects_reply=False 는 terminal)이라 조율 트래픽이 main 을 깨우지 않음; 최종 보고만 message_to_main. spawn 은 여전히 main/skill 전용 — orchestrator 는 spawn 불가, 부족하면 main 에 요청) — 모두 격리된 private `memory` 보유). **상주 전원 고정 각인 `## Reply Discipline` (v7.18.1)**: peer_agents_section(상주 신호) 있을 때 system_prompt 가 주입 — 모든 요청자(main·창 사용자·peer)에게 반드시 회신(complete=현재 요청자 자동 회신·소비된 peer 회신은 terminal 이라 대기자에겐 message 명시 보고·실패도 회신). 프로필 md 아닌 고정 섹션이라 커스텀/instant-agent 포함 전 상주 적용. `load_profile(name)` → `(body, config, error)`, `available_profiles(include_meta=)` — 광고용(name+description, `disable-model-invocation` 제외)/auto-spawn 스캔용 두 뷰. `_profile_loader`(ResourceLoader) 는 테스트 직접 교체 + conftest autouse 스냅샷/복원
│   ├── oneshot.py           (452) **run 모드 실행 엔진 (구 tools/delegate/exec.py 이동 — 함수명 tool_delegate/_run_single/_run_parallel 보존)**: `tool_delegate({tasks:[...]})` 진입 → 단건 `_run_single`(빈 task/provider 가드·사이클/깊이 체크·`run_{name}_{hash}_{ts}/` dir 명명·리포트 조립·instructions 합성 — 실행 몸통은 runner) / 복수 `_run_parallel`(worker 스레드·renderer begin/end_delegate_task lifecycle). 워커 라벨 `agent:{profile}`
│   ├── report.py            (272) run 결과의 표현 (구 tools/delegate/report.py): DelegateResult·활동로그 추출(iter_record_ops — 멀티-op+legacy 양쪽 shape)·`_generate_run_dir_name`·result.md 영속(`_persist_run_result`)·출력 포맷팅(STATUS/RESULT/[Subagent activity]/[Files touched]/[Duration])
│   └── agents_live.py       (1531) **상주 에이전트 코어 (구 teammate.py — P1~P5+U 확장)**: `AgentInstance`(key·profile_name·합성 role_prompt·영속 ctx·inbox(SimpleQueue)·자기 stop_event·데몬 worker — 상태 전이 starting→idle→busy→…→dead 는 worker 루프 한 곳) + `AgentRegistry`(main 루프 수명 소유, spawn(profile=)/request/drain_replies/resume_agent/kill/shutdown_all + 회신 mailbox=Condition+list, `runner` DI seam, 동시 생존 상한 `max_agents`(인스턴스 필드, 기본 10, `clamp_max_agents`로 정규화 — 0=무제한; `set_max_agents`로 세션 한정 변경, `_at_agent_limit`가 spawn/resume 게이트, 낮춰도 기존 미살상; 웹 `GET/POST /api/max-agents`+sticky `broadcast_max_agents`, 5.16 — 구 env `AGENT_CLI_MAX_AGENTS` 제거) + `compose_role_prompt`(파일 본문→"## Additional instructions"→인라인 — instant-agent U4, manifest 영속로 resume/부활 동일 정체성) + `build_reply_record`(배달 레코드 — **`tool:"agent"`+additive `source:"agent_reply"`; `tool:""` 금지**(형식-개입 마커 오인 방지); over-cap 회신은 디스크 포인터 치환 — 전문은 `agents/<key>/replies/reply-<seq>.md` 영속. **v7.11.0 `registry=` 동봉**: 배달부(_deliver_agent_mail)가 넘기면 회신 말미에 배달-시점 잔여 상태 한 줄 — `working · N queued`+자동배달 안내(폴링 넛지 정합) 또는 `idle — ready`; question/died/peer·registry 미전달 경로는 무변경) + `tool_agent`(상주 mode 디스패치 — tool_bridge 인터셉트; 레지스트리 없으면 run 안내와 함께 거부=모드 축소) + `format_agent_label`("agt-x (coder · ui)") + **발신자 창 out 기록 (`_log_outbound`, v7.11.1 실사고 수리)**: `message_to_main`/peer request 발신이 mailbox·수신 inbox 만 채우고 발신 에이전트의 🤝 창(agent_message)·conversation.jsonl 을 건너뛰어 — main 챗 관찰로는 보이는데 대화창 무기록·resume 소실. 발신 시 out 방향 payload 로 표면+로그 동시 기록(수신측 in 은 request() 기존 담당 — 각 창=그 에이전트 관점 완결 대화). **에이전트↔에이전트 메시징 (5.11.0, 5.12.0 유일화)**: `message` 가상 도구(virtual.py `MessageTool`, 커널 기본 — message_handler 주입 루프에만 `tools_list` 강제 탑재/그 외 strip) → `_make_message_handler(tm)` → `request(to, text, author=f"agent:{tm.key}", expects_reply=True)`(대상 inbox) 또는 `message_to_main`(→`_push_reply` kind=`peer_message`→build_reply_record 관찰). `request` 에 `hop`·`expects_reply` 추가 — worker 회신 라우팅(5.12 단순화 3분기): `author="agent:X"` & expects_reply → `_deliver_peer_reply`(요청자 inbox 로 `expects_reply=False` **terminal** 재주입 + "계속 + 조건부 보고" 가이던스 꼬리표, `_MAX_PEER_HOPS` 안전망); expects_reply=False 는 소비만(핑퐁 방지, 데드락은 비동기라 불가); `author="main"` → `_push_reply`. registry 는 서브루프에 안 넘김(agent 상주 모드 차단 유지) — 대신 `message_handler`+미리 만든 `peer_agents_section`(`build_live_agents_section(exclude_key, via_message_tool=True)`)만 `_run_message`→run_loop→LoopConfig 로 배선. 메시징=유일 채널(명시 요청, 같은 inbox 배관 — 5.8.0 도구 이벤트 구독은 5.12.0 에서 완전 제거). 회신 라우팅: main 관찰 배달·질문은 main mailbox (D8 확장) + `MailWaker`(idle 자동 재기동 — mark_idle/on_mail/on_run_end/handle_dequeued, web 무의존; **`mark_idle`(5.18.1)**: 펌프가 큐 블록 직전에 호출 — idle set 후 미배달 회신이 이미 있으면 즉시 재무장해 `on_run_end()→idle.set()` 창에서 도착해 `on_mail` 이 드롭한 lost-wakeup 을 봉합. 정합성: idle 를 먼저 set 후 pending 확인 → 동시 도착분은 여기 has_pending 또는 idle 을 본 on_mail 중 최소 한쪽이 무장, append/has_pending 은 registry cv 로 직렬화. web 은 timeout 없는 무한 블록이라 이 봉합 없이는 회신이 영구 park, CLI 는 poll timeout 스핀으로 미배달) + `notify/consume_agents_changed`(멤버십 변화만 재조립 — KV 프리픽스 보호, 인스펙터 즉시 반영 `update_prompt_section`). worker 는 시작 시 `begin_prompt_scope(key)` 상시 스코프. **ask→main 라우팅**: worker 가 `_make_ask_handler` 주입 — 상주 에이전트의 ask 는 질문을 mailbox `kind:"question"` 으로, inbox 다음 도착 메시지가 답(도착 순서 — main request 든 인간 개입이든). **🤝 대화창 resume 복원 (5.13)**: `_log_conversation`/`_replay_conversation` — in/out/question 대화 메시지를 `agents/<key>/conversation.jsonl` 에 append(대화창의 **진짜 소스** — ctx=history 는 에이전트 내부 tool 작업까지 담아 대화창과 추상화 레벨이 달라 소스 부적합). `restore`(세션 resume) + `resume_teammate`(mode:"resume") 가 부활 후 재생 — 라이브와 같은 `agent_message` 표면 통과라 web replay 버퍼에 다시 쌓여 재접속 뷰어까지 복원되고 저장한 `ts` 로 원래 시각 유지. 재생 전 `clear_agent_conversation` 선행으로 멱등(비정상 사망 잔존 중복 방지). `kill` 은 `clear_agent_conversation`(replay 버퍼에서 그 key `agent_msg` 제거+count 보정, 라이브 `agent_cleared`)로 표면만 정리하고 jsonl 은 남김 — "kill=정리 / resume=재생" 대칭. **resume 재생성 (D7)**: `agents.json`(fsio 원자 교체, **구 teammates.json 레거시 읽기 없음**) = manifest(합성 role_prompt+profile+instructions 통째 — 프로파일 md 소실 무관) + 미배달 pending 미러. 부트스트랩이 `restore(parent_ctx)` 무조건 호출(fresh=no-op) + `auto_spawn()`(frontmatter `auto-spawn: true`, 동일 프로파일 dedup). **mode:"resume"**: dead 에이전트를 같은 key·fresh worker·ctx resume 모드로 부활(전 문답 기억·seq 이어가기·revivable 복귀). **대기 지시 일원화 (v7.9.0 status 넛지 → v7.11.2 ACK 전면)**: spawn(초기 task)/request/resume ACK 가 "폴링·재전송 금지, `complete` 로 턴 종료, 도착 시 기상"을 명시 — 실사고(spawn 후 main 이 status 폴링 4회+동일 요청 재전송 4회로 에이전트 방해) 대응. request ACK 는 적체 ≥2 면 큐 수와 간섭 경고 동봉. ★U-A "wait 유도 문구 소멸" 의 의도적 뒤집기(당시엔 MailWaker 부재로 wait 유도가 위험, 지금은 자동 기상이라 wait 가 정답 — 테스트에 기록). **status 대기 넛지 (v7.9.0)**: `format_status` 가 working/미처리 inbox 존재 시 말미에 "회신은 자동 배달·도착 시 깨움 — status 폴링 말고 complete 로 턴 종료" 힌트 — 모델이 status 폴링으로 회신을 기다리는 패턴 차단(전부 idle 로스터 조회엔 미부착). **worker 사망 통지 (Q4)**: 비정상 종료는 `kind:"died"`→`source:"agent_died"` 관찰 능동 통지·revivable=False; kill/세션종료는 통지 없음. **인간 개입 비배달 (D8)**: `tm.current_author`≠"main" 이면 회신·질문을 main mailbox 에 안 넣음 — 🤝 창(SSE)에만. 엔드포인트 `POST /api/agent/{key}/input`·`/resume`·`/kill`. 렌더러 표면 `agent_roster`(sticky)+`agent_message`(persistent, `to`·resume 재생용 `ts` 필드)+`clear_agent_conversation`(5.13 kill 정리 — 버퍼 selective 제거+`agent_cleared`)+`begin/end_agent_work(profile=)`
├── skills/                         프롬프트 스킬 시스템
│   ├── __init__.py          (7)    re-export
│   ├── models.py            (21)   Skill 데이터 모델 (model/context/hooks/invocation)
│   ├── loader.py            (95)   스킬 파일 검색/파싱 (ResourceLoader 기반, 캐싱)
│   ├── executor.py          (229)  인자 치환 + 도구 교집합 + Role 상속 + skill subdir + stop_event
│   └── builtin/                    패키지 내장 스킬
│       ├── create-skill/            스킬 생성 메타 스킬 (references/format.md — ${SKILL_DIR} placeholder 가 render 치환 피하도록 분리)
│       ├── create-agent.md         에이전트 생성 메타 스킬
│       ├── plan.md                 구현 계획 생성 (plan/ 디렉토리에 저장)
│       └── orchestrate.md         멀티스텝 작업을 계획+워커 5종 조율 (main 오케스트레이션)
│           ├── SKILL.md            6단계 워크플로 (분석→설계→에이전트→스킬→오케스트레이터→검증)
│           └── references/         단계별 가이드 (design-patterns, agent-writing, skill-writing)
│
├── agents/                         에이전트 정의 패키지
│   ├── __init__.py          (1)
│   └── builtin/                    패키지 내장 에이전트
│       ├── code-writer.md          구현 (파일 스코프·검증·에러 경로)
│       ├── code-reviewer.md        읽기 전용 리뷰 (실패 시나리오·severity)
│       ├── code-analyst.md         읽기 전용 분석 (콜패스·수명 추적)
│       ├── unittest-writer.md      테스트 (뮤테이션으로 무는지 검증)
│       ├── log-analyst.md          읽기 전용 로그·크래시 근본원인
│       └── orchestrator.md         spawn 전용 peer 조율자 (/orchestrate 인계)
│
├── mcp/                            MCP (Model Context Protocol) 통합
│   ├── __init__.py          (1)
│   ├── config.py            (108)  mcp.json 로드/병합 (프로젝트 > 유저)
│   ├── client.py            (258)  McpClientManager (stdio/SSE 연결, 도구 호출, stderr 격리)
│   └── adapter.py           (149)  MCP 도구 → **`McpTool(Tool)`** 서브클래스로 래핑(`.run`/`.parameters` 보유 → registry validate/dispatch 를 native 와 동일 통과; bare 키라 prefix 無 — virtual tool 과 동일 메커니즘. **`wrap_single_op`=identity** override (Step 4): MCP 는 prefix-less 라 multi-op dispatch 에서 base 기본 add_prefix 가 bare 키를 손상시켜 validate 실패하던 선재 버그 수정), `register_mcp_tools` → TOOLS dict 등록. `build_mcp_tool_descriptions`는 `registry.render_param_value` 재사용 (native 와 동일 스키마 렌더)

pyproject.toml                      패키지 설정
agent-cli.py                        하위 호환 래퍼 (4줄)
```

괄호 안 숫자는 LOC(Lines of Code)입니다.

---

## 3. 모듈 의존성 그래프

### 3.1 전체 의존성 플로우

```
┌─────────────┐
│  main.py    │ ← __main__.py, agent-cli.py
│ (CLI 진입)  │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  loop.py    │
│ (에이전트   │
│  루프)      │
└──────┬──────┘
       ├────────┬────────┬────────┬────────┐
       ▼        ▼        ▼        ▼        ▼
┌──────────┐┌───────┐┌────────┐┌────────┐┌──────────┐
│providers/││tools/ ││context/││prompts/││wire_     │
│          ││       ││        ││        ││formats/  │
│anthropic ││regis- ││manager ││system_ ││base      │
│openai_   ││try    ││overflow││prompt  ││json_fc   │
│compat    ││read_  ││token_  ││        ││  (parser │
│http      ││write_ ││estima- ││        ││  + repair│
│capab.    ││edit_  ││tor     ││        ││  + rules)│
│base      ││shell  ││session ││        ││registry  │
│          ││fetch  ││        ││        ││+ all_    │
│          ││dele-  ││        ││        ││system_   │
│          ││gate   ││        ││        ││user_     │
│          ││action_││        ││        ││prefixes()│
│          ││summary││        ││        ││          │
└──────────┘└───────┘└────────┘└────────┘└──────────┘
       │                  │         │
       ▼                  ▼         ▼
┌──────────┐       ┌──────────┐┌──────────┐
│config.py │       │render.py ││models.   │
│          │       │          ││json      │
└──────────┘       └──────────┘└──────────┘
```

### 3.2 모듈별 import 관계

**순환 의존 없음.** 단방향 흐름: config → capabilities → base → adapters → loop → main

```
config.py           → (외부만: json, pathlib)
constants.py        → (외부만: 없음, 순수 상수)
verbose.py          → (외부만: sys, time) — providers/http, loop가 공유
providers/capabilities.py → config
providers/base.py   → providers/capabilities
providers/http.py   → verbose, render (lazy)
providers/*.py      → providers/base, providers/capabilities, providers/http
wire_formats/base   → (외부만: dataclasses, typing)
wire_formats/_json_diag → (외부만: json) — 순수 JSON 진단 유틸, 저층
wire_formats/_json_repair → (외부만: 없음) — 순수 JSON 구조수리 유틸, 저층
wire_formats/json_fc → recovery/intervention, recovery/primitives,
                      wire_formats/base, wire_formats/_json_diag,
                      wire_formats/_json_repair
wire_formats/xml_fc → wire_formats/base, thinking_tags
wire_formats/__init.→ wire_formats/base, wire_formats/json_fc, wire_formats/xml_fc
                      (builtin 등록)
tools/result.py     → (외부만: dataclasses, 순수 데이터 타입)
tools/read_file.py  → tools/result, (외부만: re, zlib, pathlib)
tools/_confine.py   → render (lazy: guard 시 confirm), (외부만: os, re, shlex, pathlib)
tools/edit_file.py  → tools/read_file, tools/result, tools/_confine (lazy)
tools/shell.py      → tools/result, tools/_confine (lazy)
tools/write_file.py → tools/result, tools/read_file (format_hashlines), tools/_confine (lazy)
tools/context.py    → tools/result, context/session
subagent/oneshot.py → tools/result, subagent/profiles·report·runner (run 엔진)
subagent/runner.py  → (전부 lazy: context/manager, loop, render, hooks — 순환 회피)
subagent/profiles.py→ resource_loader
subagent/agents_live.py → tools/result (+ lazy: subagent/runner·profiles, render, context/token_estimator)
tools/agent_tool.py → tools/base, tools/result
tools/registry.py   → tools/base + 12개 tool 모듈 (인스턴스 수집). detectors는 validate_tool_input을 lazy import (registry→tool→recovery 순환 회피)
context/token_est.  → (외부만: 없음)
context/overflow.py → (no internal deps — pure error-string patterns)
context/manager.py  → context/token_estimator, tools/registry (lazy: summary_arg), wire_formats
prompts/system_pr.  → providers/capabilities, tools/registry, wire_formats
context/session.py  → wire_formats (recent_exchanges가 all_system_user_prefixes 호출)
recovery/common_recovery → recovery/intervention, recovery/primitives
                      (WF 의존 없음 — 모든 plugin이 같은 텍스트를 봄)
recovery/wf_recovery   → recovery/intervention, recovery/primitives, wire_formats
                      (recovery/__init__.py 는 wf_recovery 를 re-export 안 함 —
                       패키지 자체는 format-agnostic, 직접 import 만이 wire_formats 끌어옴)
loop.py             → constants, context/manager, context/overflow,
                      prompts/system_prompt, providers/base, providers/capabilities,
                      render, tools, subagent/oneshot·agents_live, tools/registry,
                      verbose, wire_formats
skills/loader.py    → skills/models, resource_loader
resource_loader.py  → yaml (optional)
skills/executor.py  → loop, skills/models, providers/base, providers/capabilities
main.py             → config, context/manager, loop, providers, render, skills
```

---

## 4. 핵심 데이터 구조

### 4.1 LLM 응답 (`providers/base.py`)

```python
@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int

@dataclass
class LLMResponse:
    content: str                          # 텍스트 응답
    tool_calls: list[dict] | None = None  # 네이티브 tool calling 결과
    usage: TokenUsage | None = None
    stop_reason: str | None = None
    thinking: str = ""                    # provider-side reasoning 채널

# tool_calls 항목 형식:
# {"id": "tu_1", "name": "read_file", "input": {"path": "a.py"}}
```

`thinking`은 모델이 별도 reasoning 채널로 노출한 텍스트를 운반합니다. 채널 매핑:
- **Anthropic**: `content[].type == "thinking"` 블록 + 스트리밍 `thinking_delta`
- **OpenAI 호환**: `choice.message.reasoning_content` (vLLM 컨벤션)
- 위 채널이 없으면 `""` (plain OpenAI Chat Completions 등 — graceful)
- `<think>...</think>` 태그가 content 안에 있는 경우는 별도 — 각 플러그인의 stage 0(`strip_thinking` — thinking_tags 단일 소스)가 `ParsedTurn.thinking`으로 분리 추출

**소비처 (v1):** verbose 모드의 `render_thinking` 디버그 출력 *전용*. recovery 레이어(`format_no_*_retry`, `recovery/primitives.py`)는 thinking을 *읽지 않음* — primitive contract가 channel-agnostic이어야 누더기를 막기 때문 (`docs/robust-harness/DESIGN.md` §2.2).

### 4.2 모델 능력치 (`providers/capabilities.py`)

```python
@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int               # 컨텍스트 윈도우 크기 (토큰)
    max_output_tokens: int            # 최대 출력 토큰
    supports_structured_output: bool  # basic JSON mode 가능 (OpenAI response_format / Anthropic tool calling)
    supports_thinking: bool           # thinking/reasoning 지원
    thinking_budget: int              # thinking 토큰 예산 (0=비활성)
    supports_strict_schema: bool      # (dormant) strict JSON Schema 표식 — 현재 어떤 provider도 이 플래그로 동작 분기 안 함. 향후 opt-in strict schema 재도입 시 사용 예정.
    thinking_format: str = ""         # thinking 블록 태그 ("think", "reasoning", "")
```

`thinking_format` 값:
- `"think"` — `<think>...</think>` 형식 (DeepSeek-R1 등)
- `"reasoning"` — `<reasoning>...</reasoning>` 형식
- `""` — thinking 블록 미사용 (Anthropic API 레벨 처리, GPT 등)

능력치 조회 우선순위:
1. `models.json` 정적 설정 (최우선)
2. 런타임 API 감지 (공유 `_detect_capabilities(model, transport)` — OpenAI 는 `/chat/completions`, Anthropic 은 `/messages` transport; 둘 다 메타(`max_model_len`)+overflow probe+thinking+structured 감지). 감지 시 `max_output = context // 4`; context < 16K(`MIN_CONTEXT_WINDOW`)면 `UnsupportedModelError`로 reject
3. 보수적 기본값 (4096 context, 모든 기능 비활성 — `DEFAULT_CAPABILITIES`, provider/base_url 없을 때만)

**런타임 감지 세부 (OpenAI 호환 / omlx · vLLM · mlx-lm):** `_detect_openai_context_window` 가 3-tier로 context window 결정 —
1. `/v1/models` 메타데이터 `max_model_len`(vLLM) / `context_length` — 있으면 그대로 (가장 쌈·정확).
2. **overflow probe** (`_probe_context_window_via_overflow`) — 메타데이터에 없는 서버(omlx 등)는 의도적으로 상한 초과 prompt(`"word "×2M` ≈ 1.5M 토큰)를 보내 400을 유발하고 `parse_overflow_amounts`로 응답의 상한 숫자를 추출 (omlx: `exceeds max context window of 262144 tokens`). 상한 초과 prompt는 **토크나이즈 직후 즉시 거부**되어 eval/생성이 없으므로 서버 점유 없음(실서버 검증 2026-05-30) — 그래서 경계로 수렴하는 binary search는 **안 함**(상한 *이하* probe는 full prompt-eval을 유발해 서버를 점유시킴).
3. `_DEFAULT_CONTEXT_FALLBACK` = **128K**(`131072`) — 메타데이터·probe 모두 숫자를 못 주면. 보수적/under-set이라 자체적으로 400을 유발하지 않고, 실제가 더 작으면 flow 2 런타임 복구가 교정. (이전 4096 기본값을 대체 — 4096은 256K 서버에서 컨텍스트의 1.5%만 쓰는 심각한 낭비였음.)

모든 첫-실행 probe(thinking / context overflow)는 `constants.DETECTION_PROBE_TIMEOUT`(60s) 공유 — cold-load를 감내하는 여유값이며 사용자 셸 명령용 `SHELL_COMMAND_TIMEOUT`(30s)과 구분.

### 4.3 파서 결과 — `ParsedAction` (`wire_formats/base.py`)

모든 wire-format plugin이 반환하는 boundary 데이터타입 — loop 은 plugin 과 무관하게 이 타입만 소비.

```python
@dataclass
class ParsedAction:
    thought: str | None = None
    action: str | None = None     # "complete" = 작업 완료
    action_input: dict | str | None = None
    raw: str = ""                # 원본 LLM 텍스트 (thinking 제거 후)
    parse_stage: int = 0         # 0=실패, 1=json.loads, 2=json_repair, 3=regex (plugin이 정의)
    thinking: str | None = None  # 추출된 thinking 블록 내용
    truncated: bool = False      # JSON 복구가 닫지 못한 브래킷/문자열을 보충했을 때 True
```

### 4.4 도구 추상화 표면 (`tools/base.py` + `tools/registry.py`)

각 도구는 `Tool` ABC 서브클래스로, 스키마(`name`/`description`/`parameters`)·dispatch(`_run`)·wire-key prefix 를 한 곳에 응집한다 (이전의 중앙 `ToolSchema` dataclass + `__init__.TOOLS` 함수 dict 를 대체).

```python
class Tool(ABC):
    name: str
    description: str
    parameters: dict                      # JSON Schema
    @property
    def key_prefix(self) -> str: ...       # "{name}_"
    def strip_prefix(self, args): ...      # wire → 표준 키 (run()이 dispatch 직전 적용)
    def add_prefix(self, args): ...        # 표준 → wire (inline 가이드 예시용, strip의 역, 멱등)
    def claims(self, action_input): ...    # 키 prefix 로 소유 판정 (action 누락 추론)
    def run(self, args, *, ctx=None):      # strip_prefix → _run (ctx=RunContext|None)
    @abstractmethod
    def _run(self, args, *, ctx=None) -> ToolResult: ...
```

**per-call 루프 컨텍스트 `RunContext`** (frozen dataclass, `tools/base.py`): 루프가 도구 호출마다 아는 값들(`session_dir`·`oversized_cap`·`tools_available`)을 **하나의 객체**로 묶어 두 도구 표면(`run`/`_run` 실행 + `render_oversized` 렌더)에 넘긴다 — 새 per-call 값이 생겨도 필드 하나 추가일 뿐 13+7 개 시그니처를 다시 안 고친다(이전엔 `session_dir` 만 관통·render 는 `cap`/`tools_available`/`session_dir` 3개 loose arg 로 비대칭). frozen = 병렬 delegate 루프가 같은 인스턴스를 공유해도 안전(그래서 공유 `TOOLS` 싱글턴에 저장 금지·per-call 전달). **범위 규율**(god-object 방지): per-call 루프 컨텍스트만 — per-result 데이터(특정 결과의 `body`/`tokens`)는 `render_oversized` 의 명시 인자로 유지, 무관 기계류(parser/provider/history)는 넣지 않음. loop `_run_ctx()` 가 단일 생성점(두 seam 이 "이 호출의 컨텍스트"에서 어긋나지 않게).

**Wire-key prefix** (★ Step 3 완료로 **모든 builtin 도구가 flat-native** — 어떤 builtin 도 prefix 안 씀; MCP 도 prefix-less. 아래 dropped-action 복구 메커니즘은 **미래 prefixed 도구/포맷용 latent seam** — Step 4 에서 "삭제 대신 seam 보존" 결정): action_input 의 최상위 키를 `{tool}_{param}` 으로 네임스페이스한다 (예시는 가상의 prefixed 도구 `xtool_param`; 중첩 키 등은 그대로). 모델이 `## Action` 의 tool 이름을 누락해도(parse_stage 3) 키 모양으로 도구를 복원할 수 있다 — loop 이 parse 직후 (`wire_format.action_required=False` 일 때만) `registry.infer_action(action_input)` 을 호출, 각 `Tool.claims`(prefix 매칭)가 투표해 **정확히 1개**가 소유하면 그 도구로 보정(0/2+는 NO_ACTION recovery로; `action_required=True` plugin 은 infer 를 건너뛰고 바로 NO_ACTION). 이 복구의 전제는 파서가 action 무효 시에도 action_input 을 보존하는 것(WireFormat.parse 계약). 보정에 성공하면 `_append_observation` 이 next-turn prior(messages)와 history record 를 **보정된 wire shape** 으로 재기록한다 — raw drift 를 prior 로 다시 먹이면 mimicry 가 강화되기 때문(다음 턴이, 또는 resume 시 복원된 prior 가 "action 이름을 빠뜨려도 된다"를 학습; NO_THOUGHT retry 가 피하는 것과 같은 실패). 보정 자체는 `TurnRecorder`(parse_stage=3 + `action_inferred` primitive)로 추적되므로 형식 실패 분석 신호는 보존된다. prefix 는 **wire 표면에만** 존재: `Tool.run()` 이 dispatch 직전 `strip_prefix` 로 표준 키로 되돌려 `tool_*` 함수·virtual 처리·validate·기존 dispatch 가 전부 표준 키를 받는다(prefix 없는 키는 no-op → 모델이 표준 키를 보내도 동작). 키 prefix 가 변별을 구조적으로 보장하므로 claims 충돌(`{content, edits}` 류)이 원천 소멸 — 각 도구는 `name` 만 정하면 prefix/strip/claims 가 자동(override 0). **실측 근거**: omlx 27B/35B 에서 prefix 키 compliance 60/60(std-leak 0) — 표준 키와 동일하게 따름.

```text
# 실제 도구 (각 모듈에 Tool 서브클래스): read_file, write_file, edit_file, shell,
#   code_index, read_context, fetch, memory, agent (5.0.0 — 구 delegate+teammate 통합)
# 가상 도구 (tools/virtual.py — loop이 인터셉트, 표준 키 유지, prefix/추론 대상 아님):
#   complete, ask, run_skill
# registry.py가 12개 Tool 인스턴스를 수집 → TOOLS(= TOOL_SCHEMAS alias).
#   인스턴스가 옛 ToolSchema와 같은 .name/.description/.parameters를 노출하므로
#   schema 소비처(system prompt, input validation, MCP adapter)는 무변경.
# _ALWAYS_INCLUDE = ("complete",)
```

가상 도구 인터셉트 분기는 일반 dispatch 경로(`§5.x render_step("action", ...)`)를
거치지 않으므로 분기 진입 시 명시적으로 `render_step("action", ...)` 을 호출해
`assistant_turn` 이벤트를 발사한다. 이게 없으면 WebRenderer의 streaming-text 카드가
교체되지 않아 다음 턴의 stream_chunks가 동일 카드에 누적되어 "이전 메시지에 답변이
붙어 보이는" UX 버그가 발생한다 (`complete` / echo-as-final 은 `render_step("final", ...)` 을
호출하므로 동일 경로로 해결됨).

---

## 5. 핵심 플로우

### 5.1 ReAct 에이전트 루프 (`loop.py` — `AgentLoop` 클래스)

#### 컨텍스트 윈도우 레이아웃

`ctx.get_messages()` 반환: history.jsonl의 마지막 N개를 자연어 변환 (assistant turn은 ctx의 wire_format plugin이 변환 — §5.4 참조)

```
[system]   Role (main/서브에이전트/skill별 상이)
           Task Guidelines + Format Rules (thought에 목적+이유 필수)
           Available Tools / Skills / Agents
           DIRECTIVE.md / Environment
           Context Recovery Guide ("read_file({session_dir}/history.jsonl)")

[messages] user: "hooks.py 분석해줘"
           assistant: hooks.py를 분석하기 위해 파일을 읽겠다. → read_file(hooks.py)
           user: [read_file] hooks.py\n(전문)
           assistant: 분석이 완료되었다. hooks.py는 3개의 hook 타입을 지원...
```

- Scratchpad 별도 inject 없음. messages만 (토큰 budget 자동 계산, 90% 초과 시 compaction → 그 외 FIFO drop)
- 저장: history.jsonl (JSON Lines, 구조화)
- 표현: 자연어 변환 (thought → "목적. → action(인자)")

#### ctx.add 저장 포맷

| 메시지 타입 | history.jsonl 저장 형태 |
|------------|----------------------|
| 사용자 입력 | `{"role":"user", "content":"..."}` |
| LLM action | `{"role":"assistant", "thought":"...", "action":"...", "action_input":{...}}` |
| 도구 결과 | `{"role":"user", "content":"Observation: ..."}` |
| complete | `{"role":"assistant", "thought":"...", "action":"complete", "action_input":{"result":"..."}}` |

#### 루프 플로우

```
AgentLoop.run()
    │
    ├─ _install_signal_handler()   ← Ctrl+C를 flag로 변환
    ├─ _setup()
    │   ├─ 시스템 프롬프트 빌드 (capabilities, tools, session_dir, agent_role)
    │   └─ ctx.add(user query) → ctx.get_messages() (자연어 변환)
    │
    ├─ while _should_continue():
    │    │
    │    ├─ ★ CHECK: _interrupted? → _on_interrupt() → return None
    │    │
    │    ├─ _begin_iteration() → turn separator 렌더링
    │    │
    │    ├─ _call_llm() → LLMResponse (overflow 400 시 force_fit으로 compact→FIFO 축소 후 bounded 재시도 — flow 2)
    │    │
    │    └─ _handle_text_path()  ← text parsing only (native tool calling 제거)
    │         │
    │         │
    │         ├─ [complete] → ctx.add(structured dict) → return answer
    │         │
    │         ├─ [run_skill] → 내부 AgentLoop (별도 skill subdir)
    │         │
    │         └─ [도구] → execute → ctx.add(assistant + observation)
    │
    └─ _restore_signal_handler()
```

**Graceful Interrupt — turn 경계에서 멈춤. 두 surface 공통:**
- 공통 신호 = `stop_event` (threading.Event). `_should_continue()` 가 매 turn
  경계에서 `stop_event.is_set()` 을 평가 → set 이면 `_interrupted=True` →
  `_on_interrupt()`. stop 이 `run_loop` 진입 *전*에 와도 첫 체크에서 0-turn 으로 멈춤.
- **CLI (Ctrl+C, main thread)**: `_install_signal_handler` → 1st `_interrupted`
  flag + `stop_event.set()` (현재 스텝 완료 후 탈출), 2nd `KeyboardInterrupt`
  즉시 (기본 핸들러 복원). chat/skill/agent 모두.
- **web (Stop 버튼, worker thread)**: worker 가 turn 마다 `stop_event` 생성 →
  `run_loop(stop_event=…)` 전달 + `server.set_stop_handle()` 등록. `POST /api/stop`
  → `server.trigger_stop()` → `stop_event.set()`. worker thread 라 signal handler 는
  skip 되고 `stop_event` 경로만 작동. chat(`run_loop`)·`/skill`(→`execute_skill`)·
  `@agent`(→`tool_delegate`→delegate worker `run_loop`, 병렬 worker 가 같은 Event
  공유) 모두 같은 `stop_event` 가 전파되어 turn 경계에서 멈춤.
- **인터럽트 기록 + 렌더**: `_on_interrupt` 이 `{role:user, tool:"interrupt",
  success:False, content:INTERRUPT_NOTICE}` 를 ctx 에 add → `[interrupt] …`
  observation 으로 렌더. user-role bare 메시지가 아니라 observation 이므로 다음 실제
  user 입력과 role 이 겹치지 않고, `recent_exchanges` 가 `tool` 필드로 자동 제외.
  history.jsonl 영속화. (레거시 prefix `"⚡ User interrupted."` 는 옛 세션 하위호환)
  사용자 표시는 **`console.print` 직접 호출이 아니라 `render_step("observation", …)`**
  로 — CLI 는 console, web 은 SSE 로 가서 web 서버 터미널에 노이즈가 새지 않음
  (top-level 만 렌더, nested skill 인터럽트는 부모가 표시).

**run 모드 Ctrl+C:** signal handler 미설치, `KeyboardInterrupt` 즉시 발생 → `try/except`로 세션 저장 후 종료

#### 중첩 렌더링: `push_depth` / `pop_depth` + 그룹 블록

스킬/agent run 실행 시 출력을 시각적으로 감싸기 위해 `group_start`/`group_end`와
depth 기반 prefix(`│ `)를 사용. 병렬 run 은 worker별 capture 후 Live 패널로
실시간 상태 표시, 완료 후 block replay.

| 시점 | 호출 | 출력 |
|------|------|------|
| 스킬/run 시작 | `render_group_start(label, icon)` | `┌─ 🪄 skill:plan` |
| 내부 턴 | `push_depth` 상태에서 `_p()` | `│ 💭 thought...` |
| 스킬/run 종료 | `render_group_end(label, success, dur)` | `└─ ✓ skill:plan (5.2s)` |

### 5.2 프로바이더별 도구 호출 방식

모든 프로바이더가 **ReAct 텍스트 파싱**만 씁니다. 네이티브 tool calling API (Anthropic `tool_use`, OpenAI `function calling`)는 **사용하지 않습니다** — 프로바이더 편차 제거와 구현 단순성을 위한 선택. 따라서 `supports_tool_calling` 같은 플래그는 존재하지 않고, 모든 분기는 JSON 출력 여부 (`supports_structured_output`) 하나로 수렴합니다.

```
              ┌─ supports_structured_output=True ─┐
              │                                    │
        ┌─────┴──────┐                     ┌──────┴──────┐
        │ Anthropic  │                     │ OpenAI      │
        │ tool       │                     │ response_   │
        │ calling    │                     │ format      │
        │ (basic)    │                     │ json_object │
        └────────────┘                     └─────────────┘
              파싱 필요                             파싱 필요
              (JSON 출력)                          (JSON 출력)

              ┌─ False ─────────────────────────────┐
              │                                      │
        ┌─────┴──────┐                              │
        │ 텍스트 자유  │                              │
        │ 형식        │                              │
        └────────────┘                              │
              파싱 필요                               │
              (비구조화 텍스트)                         │

  모든 경우: 3단계 폴백 파서 (json.loads → json_repair → regex)가 도구 호출 추출
```

### 5.3 단계적 파싱 폴백 (`wire_formats/json_fc.py`)

```
LLM 텍스트 응답
    │
    ▼
Stage 0: thinking 격리 (WireFormat.strip_thinking — thinking_tags 단일 소스)
    │
    ▼
legacy 헤더(`## Action`) 관용 경로 (stage 2 drift) ── 구 md_array emission
    │
    ▼ 헤더 없음
캐노니컬: 첫 [/{ 앞 산문 = thought, 이후 = op JSON
    ├─ strict 파스 성공 → ParsedTurn (parse_stage=1)
    ▼ 실패
_extract_op_json 수리 아스널 (전부 bail-if-invalid, 성공 시 parse_stage=2):
    anon-객체 unwrap · 재-오픈 배열 병합 · 미닫힘 괄호 EOF 닫기 ·
    여분 closer 드롭 · 따옴표 수리 · under-escape 배증 · strict=False 재파스
    ▼ 전부 실패
"action" 흔적 있으면 parse_stage=0 (NO_JSON 진단), 아니면 thought-only (NO_ACTION)
```

수리 helper 는 plugin 모듈 안에 산다 (공용 구조수리 유틸 `_json_repair`
제외) — plugin 이 *폴더째 삭제 가능*한 boundary 유지. xml_fc 는 자기만의
lenient/하이브리드 수리를 소유 (self-contained).


(react 의 형제-키 hoist/`_normalize_action_input` 기계는 v7.0.0 에서 react 와 함께 제거 — json_fc 는 flat op 이라 해당 드리프트 class 자체가 없음.)

### 5.4 컨텍스트 관리 (`context/manager.py`)

> 상세 설계: `docs/context-redesign/DESIGN.md`, `docs/context-compaction/DESIGN.md`

#### 2-Tier: Compaction (LLM 요약) → FIFO Fallback

> 두 흐름이 있다. **flow 1 (예방)** — 매 LLM 호출 *직전* `ensure_within((C−S−O)×0.8)`, 호출 *직후* 서버 실측으로 reconcile.
> **flow 2 (반응)** — 예방이 빗나가 서버가 400을 던지면 `force_fit`으로 사후 축소+재시도.
> 아래는 flow 1; flow 2는 이어지는 박스 참조.

```
add(msg): 캐시 append + 토큰 누적 + history.jsonl 한 줄 append  (compaction 트리거 안 함)

매 LLM 호출 (_call_llm):
    │
    ├─ [호출 직전] flow 1 예방: ctx.ensure_within(target)
    │     S = estimate_tokens(self.system)        ← 매 호출 실측 (가변 system 반영)
    │     target = (C − S − O) × 0.8              ← C=context_window, O=max_output
    │     _cache_tokens > target 면:
    │        1. compaction_enabled=False/콜백 미주입 → _evict_fifo(target)
    │        2. 그 외 → _compact() (Split→oldest 절반 evict→단일호출 요약(recursive)
    │           →_file_extract path dedup→[system][summary≤8K][file_list][retained]
    │           →compaction.json atomic write)
    │        3. Belt-and-braces: 여전히 > target 이면 _evict_fifo(target)
    │     self.messages = ctx.get_messages()       ← 축소 반영
    │
    ├─ provider.call(...)
    │
    └─ [호출 직후·성공] flow 1 reconcile: ctx.reconcile_actual_tokens(usage.total_input_tokens, S)
          usage.total_input_tokens = input + cache_creation + cache_read  ← 서버 ground truth (TokenUsage property)
          _cache_tokens = actual − S              ← messages 실측으로 re-anchor
          (usage 없으면 no-op → 추정 유지)

Threshold 계산:
    target = (context_window − system(실측) − max_output) × 0.8
    예: 262K − ~4K system − 4K out ≈ 254K × 0.8 ≈ 203K 임계
    기존(add 시 0.9 × (C−O−4000), system 고정 4000)을 대체 — system 실측 + 매
    호출 reconcile 로 chars/4 의 CJK 과소평가가 누적되지 않음(drift 1턴치로 제한).

LLM 호출 시 messages:
    [system verbatim][summary (있으면)][file_list (있으면)][자연어 변환된 dynamic]

세션 재개 시:
    compaction.json 로드 → dynamic_start_index 유효하면 history[index:]만 forward 파싱,
    아니면 history.jsonl 뒤에서부터 budget 내 메시지 파싱 (legacy 경로)
```

**flow 2 — Reactive overflow recovery (`force_fit`)**

> **전제 — 에러에 본문이 실려야 함**: omlx 400 의 상한 정보는 **응답 BODY**(`...exceeds max context window of N tokens`)에 있는데, 표준 `r.raise_for_status()` 는 본문 없는 메시지(`400 Client Error: Bad Request for url: ...`)만 던져 `is_context_overflow(str(err))` 가 항상 False → flow 2 가 발화를 못 하고 recoverable 400 이 hard-fail(실측 iter=37 증상). provider 는 **`http.raise_for_status_with_body(r)`** 로 본문을 메시지에 포함시켜 이 인식을 복원한다(success 경로 무손상 — 스트리밍 200 에선 `r.text` 안 읽음, 에러 분기에서만 읽음).

```
provider.call() → 예외
    │
    └─ is_context_overflow(err)? (overflow.py 패턴: Anthropic/OpenAI/omlx)
         │ yes & ctx 있음 & overflow_retries < _MAX_OVERFLOW_RETRIES(5)
         ↓
         parse_overflow_amounts(err) → (actual, limit)
             omlx "N tokens exceeds max context window of M tokens" → (N, M)
         target = (limit or budget) × 0.8
         ctx.force_fit(target, actual_tokens=actual)
             1. compact 시도 (enabled면)
             2. 부족하면 _evict_fifo(floor)  ; floor = _cache_tokens × (target/actual)
             3. progress 보장: 아무것도 안 줄면 oldest 1개 강제 pop
         → shrank? messages 갱신 + turn-=1 + _RETRY (재요청)
         → anchor만 남아 force_fit=False → 깔끔히 실패
    │
    └─ 성공 시 overflow_retries=0 리셋 (다음 turn은 fresh 예산)

배경: 로컬 추정(chars/4)이 CJK를 4~8배 과소평가 → flow 1 임계 미달 → 서버 400.
flow 2는 서버 신호(400 + 실제 토큰 수)를 ground truth로 삼아 사후 복구.
비율 축소라 추정 절대정확도 불필요; bounded(5회)라 무한 루프 없음.
```

- **압축 비활성화**: 내부 `compaction_enabled=False` → 플레인 FIFO만 동작. **서브에이전트 전파**: `compaction_enabled` 플래그가 부모 `AgentLoop`→`tool_delegate`/`_run_single`/`_run_parallel`(delegate)과 `_handle_run_skill`→`execute_skill`(skill)의 `run_loop` 호출까지 스레딩됨 → 부모에서 끄면 delegate/skill 서브에이전트에도 적용. (CLI 플래그·env 스위치는 v5.17.0 에서 제거 — 압축은 항상 켜지고 실패 시 belt-and-braces FIFO 로 폴백. 프로그램matic 비활성 경로만 유지.)
- **과대 도구 출력 캡 + nudge (loop `_tool_observation`, 결과→관찰 seam)**: 도구 관찰 토큰이 **`context_window // 10`**(loop `_oversized_cap`) 초과면 전체 출력을 컨텍스트·history 어디에도 안 넣고 **도구별 over-cap 응답**으로 치환 — 호출 자체는 성공. 거대 출력은 추론 공간을 잠식해 품질↓ 이라 모델을 surgical 회수로 유도. 세 **도구별 추상화 표면**(`tools/base.py`)이 여기서 만남: `Tool.render_observation(result, args)`(결과→관찰 본문, 기본=성공 output·실패 error), `Tool.apply_oversized_cap`(기본 True), **`Tool.render_oversized(result, args, *, body, tokens, ctx)`**(캡 초과 시 낼 관찰을 도구가 소유 — 기본=`default_oversized_nudge`(라인범위/심볼/`LIMIT`/`grep`/`tee→read_file` 제네릭 유도); 도구 미등록 fallback 도 이 함수). per-result `body`/`tokens` 는 명시 인자, per-call 컨텍스트는 **`ctx`(`RunContext`)** 로 묶어 전달: **`ctx.tools_available`=현재 루프 호출가능 도구 집합**이라 안내가 부를 수 있는 도구만 지목(depth-stripped 도구 회피), `ctx.oversized_cap`=캡, `ctx.session_dir`(=`self.ctx.session_dir`, 헤드리스면 None)로 출력이 아직 파일이 아닌 도구가 body 를 저장·포인터화 가능. 실행 seam(`run`/`_run`)도 같은 `RunContext` 를 받아 두 표면이 동형(loop `_run_ctx()` 단일 조립점). **디스크-기반 4도구 공유 (`on_disk_oversized_nudge`)**: read_file/shell/delegate/fetch 는 "큰 내용이 `<path>` 에 있음 → (a) read range/search, (b) **N-way 병렬 섹션 팬아웃**(한 턴 여러 delegate op·구체 라인범위 제시)"의 invariant 를 공유 헬퍼로 소비 — **read_file**(원본 파일, +`read_symbols`), **shell**(over-cap 때만 출력을 `session_dir/shell-output-<hash>.txt` lazy 저장→가리킴; 일반 shell 호출 디스크 쓰기 0; headless=tee 폴백), **delegate**(기존 `result.md` 가리킴 + re-delegate-narrower), **fetch**(over-cap 때만 내용을 `session_dir/fetch-output-<hash>.txt` lazy 저장→가리킴 + 더 좁은 URL/얕은 depth). (b) 팬아웃 줄은 `delegate ∈ tools_available` 일 때만(depth 한계 서브에이전트 자동 생략). loop 가 `messages.append`·`ctx.add` **양쪽 전에** 최종 본문을 만들므로 일관 → **`add` 는 순수 저장**(spill 변환 없음). 비-도구 관찰(개입·unknown-tool)·사용자/어시스턴트 메시지는 캡 대상 아님. 관찰 렌더는 `_append_observation` 단일 지점(`render` 플래그; recovery 는 `render_recovery` 가 이미 렌더해 False), multi-op 은 flush 합본 1카드. **비-파일 도구 (`narrow_oversized_nudge`)**: read_context(SQL LIMIT/projection/`substr`)·code_index(`mode=fetch`/`search`/`max_bytes`)는 출력이 파일이 아니라 **제자리 재-narrow** 가 정답이라 별도 헬퍼로 도구 파라미터만 안내(파일/팬아웃 없음). (이전의 청크-spill 레코드 `{spill,output:[guide,chunk...]}` + read_context `json_extract` 회수 + read_file 3% 절단은 모두 제거. 남은 도구는 제네릭 `default_oversized_nudge`.)
- **요약 프롬프트 (agentic resume 지향)**: `_llm_compact_summarize` 의 system 프롬프트가 단순 4-clause 가 아니라 **구조화 섹션**(TASK/STATE/DONE/PENDING/DECISIONS/FAILURES/FACTS, 빈 섹션 생략)을 요청 — 에이전트가 요약만으로 작업을 이어가야 하므로 남은 작업(PENDING)·실패한 시도(FAILURES)·verbatim 식별자(FACTS: 경로/명령/에러문자열) 보존 + "transcript 에 있는 것만, 지어내지 말 것" 규칙 포함. 재귀 병합(이전 요약 + 신규 transcript)은 "same section headings" 로 구조 유지. (실세션 검증: Qwen3.6-27B 가 구조 준수 + 실제 `AttributeError` 실패를 verbatim 포착, 6.4K→2.3K자.)
- **Belt-and-braces**: LLM 요약 실패(`CompactionError`)나 재구성 후 캐시 미충족 모두 같은 FIFO 경로로 수렴 → 무한 트리거 루프 없음
- **Observability**: `TurnRecorder.record_compaction(tokens_before/after, evicted_count, fallback_used, failure_signal, duration_ms)` → `turns.jsonl`에 `event: "compaction"` 기록
- **UI**: `render_compaction_progress(phase, ...)` 단일 helper가 `_renderer.compaction(phase, ...)` 으로 위임 — CLI-vs-web 라우팅은 renderer 가 담당. **base `Renderer.compaction` 기본 구현**은 `status` 한 줄(start/done/warning)로 출력(CLI/minimal 그대로). **`WebRenderer.compaction` 은 override** 하여 전용 `compaction` SSE 이벤트(구조화 payload: phase/old_tokens/new_tokens/evicted_count/reason)를 emit → 프론트(app.js `compaction` 리스너)가 **대화창 인라인 시스템 라인**(`.card-sys`: start "압축 중…" → done "압축됨 X→Y tok" 갱신, warning) 으로 렌더. transient(재접속 시 미재생). (이전엔 helper 가 generic `status` SSE 를 쐈으나 프론트 리스너가 없어 웹에선 안 보였음 — 전용 이벤트로 수리.)
- **상주 에이전트 회신 도착 힌트 (`agent_mail_hint`, 5.18.2 — compaction 과 동형 패턴)**: `main.py _agent_mail_notice(reply)` 가 회신/질문 도착 시 `renderer.agent_mail_hint(key, kind, text)` 호출. **base 기본 구현**은 `status` 한 줄 위임(CLI/minimal/커스텀 그대로). **`WebRenderer.agent_mail_hint` override** 는 전용 transient `agent_mail` SSE 이벤트(`{key, kind, text}`) emit → 프론트(app.js `agent_mail` 리스너 → `renderAgentMail`)가 `.card-sys` 인라인 라인("📨 Agent … replied" / 질문은 "❓ Agent … asked a question")으로 렌더. **웹 라벨은 프론트가 `key`/`kind` 로 자체 영문 조립** — 백엔드 `text` 는 CLI 의 한글 status 라인이라 웹에선 표시 안 함(WebUI 영문화, 5.18.3). persistent=False(도착 순간에만 의미 — 재접속 replay 시 지난 힌트 부활 방지). **배달(회신을 main 관찰로 주입)은 별개 경로**, 이건 도착 힌트만. (이전엔 `_agent_mail_notice` 가 generic `status` 를 직접 호출했으나 app.js 에 `status` 리스너가 없어 웹에선 힌트가 드롭됐던 갭의 수리 — compaction 과 같은 전용-이벤트 패턴으로 봉합. 계약 테스트 `test_web_server.py::test_agent_mail_hint_wired`.)
- **Scratchpad 없음.** history.jsonl이 대화 기록이자 artifact 인덱스
- **Context inject 없음.** LLM이 필요할 때 read_file로 pull
- System prompt에 Context Recovery Guide 포함
- 스킬/agent 서브에이전트는 부모 budget 상속

#### 저장과 표현의 분리

- **저장**: history.jsonl (JSON Lines) — 구조화된 메시지
- **표현**: 자연어 변환 — LLM에 전달되는 user/assistant 메시지

```
저장: {"role":"assistant","thought":"auth.py를 읽겠다","action":"read_file","action_input":{"path":"src/auth.py"}}
표현: auth.py를 읽어 구조를 파악해야 한다. → read_file(src/auth.py)
```

#### Assistant turn lifecycle — 4 forms, 3 plugin-owned transitions

assistant turn 한 번은 3가지 형태(A/B/C)로 conversation pipeline을 통과한다. 각 형태는 **소비자가 다르고** 따라서 **요구하는 셰이프도 다르다**. 형태 간 변환은 모두 wire_format plugin이 소유 — 새 wire format 추가 시 lifecycle 전체가 자동으로 그 plugin의 wire shape을 따른다.

| 형태 | 소비자 | 요구 셰이프 | 어디서 |
|---|---|---|---|
| (A) Emit | model이 produces | plugin wire shape, raw string | provider response |
| (B) Store | history.jsonl reader / 분석 스크립트 | 구조화 dict `{thought, action, action_input}` (save-time sanitize) | `history.jsonl` |
| (C) Feed | LLM — 같은 세션 다음 turn(live prior) **AND** overflow/resume 복원 | plugin wire shape ≈ (A) | in-memory `messages` |

두 plugin 메서드가 형태 간 다리를 놓는다. **live prior 와 resume prior 는 같은 `(B) → render → (C)` transition** — 매 턴 prior 가 raw 가 아닌 저장된 record 에서 재구성된다:

| 전이 | Plugin 메서드 | 입력 → 출력 | 호출 사이트 |
|---|---|---|---|
| (A) → (B) | `serialize_assistant_for_history(raw)` | LLM raw → 디스크 dict (thought·bare 를 `sanitize_thought` 로 save-time 정제) | `loop._append_observation` (`ctx.add` 직전) |
| (A) → (B), terminal | `serialize_terminal_for_history(thought, result)` | (언랩된) complete 결과 → 디스크 dict (포맷 동질 모양) | `loop._dispatch_op` complete/echo-final 분기 (`ctx.add` 직전) |
| (B) → (C) | `render_assistant_from_history(record)` | record → chat 메시지 dict | `loop._append_observation` (live prior) + `manager._to_natural_language` (resume) |

`serialize ↔ render`는 **서로의 역연산**: round-trip이 닫혀 있어 (A) ≈ (C). 모델이 흘린 wire sentinel 은 save-time(B)에서 한 번 정제되므로 prior 로 다시 새지 않는다 (format-runaway mimicry 의 근본 차단). byte-level 차이는 JSON 정규화뿐, semantic 동일. **(이전엔 (A)→(C) 가 `normalize_assistant_for_messages`=identity 로 raw 를 그대로 live prior 에 먹여 mimicry 의 근본 고리였음 — render 통합으로 제거.)**

**WireFormat ABC가 lifecycle 디폴트 제공**: `serialize_assistant_for_history` 디폴트 = `self.parse()` + 구조화 필드 추출 (+ bare content `sanitize_thought`), `render_assistant_from_history` 디폴트 = `self.render_full_example()` 호출 (live + resume prior 양쪽). `sanitize_thought` = identity, `render_action_input` = identity, `prefill` = `""`, `provider_call_kwargs` = `{}`, `format_rules` = `build_format_rules(self)`. 새 plugin은 **format-specific 메서드만 구현**하면 lifecycle 전체가 자동으로 작동:
- `parse(llm_text)` — wire shape 파싱
- `render_full_example(thought, action, action_input)` — wire shape 출력 (Format Rules section + history round-trip 양쪽 이용)
- `format_rules_anchor()`, `format_rules_field_specific()` — 안내 문구
- 6개 recovery wording (framing × 2, reminder × 2, static hint × 2)
- `system_user_prefixes()` — recent_exchanges 필터링

`manager._to_natural_language`는 user / tool branch만 직접 처리하고 assistant branch는 plugin에 위임 — `context/`는 format-agnostic, plugin이 format-aware. 의존 방향: `context → wire_formats` (downward, lazy import 없음).

#### 세션 파일 구조

```
.agent-cli/sessions/{session_id}/
├── history.jsonl                              ← main 대화 기록
├── main_plan_e8d4_20260405T143112890.md       ← main artifact (flat)
│
├── run_coder_f1a9_20260405T143230456/         ← agent run subdir
│   ├── history.jsonl                          ← run 내부 대화
│   └── result.md                              ← run 최종 결과
│
└── skill_summarize_d4e1_20260405T143200100/   ← skill subdir
    ├── history.jsonl                          ← skill 내부 대화
    └── result.md                              ← skill 최종 결과
```

- main: root에 flat artifact
- run/skill: subdir에 history.jsonl + result.md (재귀 중첩 가능)
- fork 모드: parent history.jsonl 복사 → 서브에이전트가 이어서 append

---

## 6. 도구 시스템

### 6.1 등록된 도구

**실제 도구** — 파일/셸/네트워크 작업 수행:

| 도구 | 설명 | 필수 입력 | 출력 |
|------|------|----------|------|
| `read_file` | 파일 읽기 (hashline 포맷, flat-native — 한 op=한 파일). 모드: `stat` (메타데이터 + 앞 20줄), `search` (정규식 grep), `line_start/line_end` (부분 범위), 또는 mode 없이 full read (크기 제한 없음). 여러 파일은 멀티-op 으로 read_file op 을 여러 개 emit. | `path` 필수, `line_start?`/`line_end?`/`search?`/`context?`/`stat?` | `LINE#HASH:content` 형식 |
| `write_file` | 파일 생성/덮어쓰기. 작성 content 를 hashline 으로 반환 (read_file 없이 edit_file 직결) | `path`, `content` | 저장 확인 + hashline(edit refs) |
| `edit_file` | hashline 기반 파일 편집 (flat-native — 한 op=한 편집) | `path`, `op`, `pos`, `end?`, `lines?` | 편집 확인 메시지 + diff |
| `shell` | 셸 명령 실행 | `command` | stdout + stderr + exit code |
| `agent` | 서브에이전트 통합 도구 (5.0.0). `mode:"run"`=일회성 in-process 위임(flat-native — 한 op=한 task; 연속 run op = 병렬, mode-aware 배칭) / `spawn`/`request`/`status`/`resume`/`kill`=상주(main 전용, 회신 자동 배달) | `mode` 필수; run: `task`+`context?`/`tools?`/`profile?`/`instructions?`; 상주: `key`/`message` 등 | run: 구조화된 결과 (output + activity log + duration) + run subdir 경로. 상주: 즉시 반환 + 턴 경계 관찰 배달 |
| `memory` | 세션 메모리 (failure/discovery/decision/note 기록·조회, compaction 무관, resume 복원, 상시 `## Session Memory` 인덱스) | `mode`(add/get/update/delete/list), `type?`, `summary?`, `detail?`, `tags?`, `id?`, `tag?` | **add**: type+summary(+detail/tags) → id. **get**: id → 전체(detail 포함). **update**: id + 바꿀 필드. **delete**: id. **list**: type/tag 필터. detail 은 인덱스 미노출(요약만) — get 으로만 회수. |
| `read_context` | 세션 이력 조회 | `mode`, `keyword`, `scope?`, `sessions?`, `loc?`, `range?` | **list**: 전체 세션 목록. **search**: 기본 현재 세션, `sessions="all"` 또는 ID로 확장; `scope`로 필드 필터 (reasoning/tool/observation/query); 결과 턴 블록 + preview 200자 cap + 50건 truncation + fetch hint footer. **fetch**: `loc='{session}/{path}:{line}'` (search 결과 그대로) 로 전체 턴 회상; `loc` 단일/배열 (max 10), `range` 0-5 (앞뒤 N턴). multi-line 보존, action_input compact JSON, all-or-nothing 시멘틱. |
| `fetch` | 웹 페이지 fetch → 마크다운 변환 | `url` | 재귀 링크 추출, 에러 힌트 |

**가상 도구** — loop.py if-cascade가 인터셉트해 직접 처리 (실제 tool dispatch 우회). LLM에게는 일반 도구처럼 노출 (시스템 프롬프트의 ``## Available Tools`` 섹션 포함):

| 도구 | 설명 | 필수 입력 | 비고 |
|------|------|----------|------|
| `complete` | 작업 완료 신호 | `result` | 루프 종료 |
| `ask` | 사용자에게 질문 | `questions` | 대화형 전용 (ctx 없으면 제거) |
| `run_skill` | 스킬 실행 | `name` | loop 레벨 인터셉트, skill subdir 생성 |

### 6.2 agent 프로파일 로딩

`agent` 도구의 `profile` 파라미터로 사전 정의된 프로파일(역할)을 로드할 수 있습니다 (run/spawn 공용, `subagent/profiles.py`):

```
검색 경로 (우선순위 순):
  1. .agent-cli/agents/{name}.md  (프로젝트 로컬)
  2. ~/.agent-cli/agents/{name}.md (유저 전역)

에이전트 파일 형식:
  ---
  allowed-tools: [read_file, shell]   # 선택: 허용 도구 제한
  model: claude-sonnet-4-6            # 선택: 모델 오버라이드
  ---
  에이전트 역할/원칙 본문 (시스템 프롬프트의 Agent Role 섹션에 주입)
```

**핵심 함수** (`subagent/oneshot.py` + `subagent/profiles.py`):
- `profiles._PROFILE_NAME_PATTERN` — 이름 검증 (`[a-zA-Z0-9_-]`만 허용)
- `profiles.load_profile(name)` — 파일 탐색 + YAML frontmatter 파싱 → `(body, config, error)`
- `_extract_activity_log(messages)` — raw history 레코드에서 per-turn 액션 요약 추출. `manager.iter_record_ops` 로 **두 저장 shape 모두**(멀티-op `ops` + 단수 legacy `action`) 읽음 — 멀티-op 턴은 op 요약을 `"; "` 조인해 1 iter. (이전엔 `json.loads(content)` — wire-format 리팩터(423608e) 후 레코드가 구조화 필드로 바뀌어 **모든 실 세션에서 빈 로그를 내던 silent 회귀**를 v4.35.1 에서 수리; 테스트 픽스처도 실제 저장 shape 으로 교체)
- `_summarize_action(action, action_input)` — 단일 액션을 한 줄 요약으로 포맷
- `_extract_last_actions(messages, n)` — 마지막 N개 액션(턴) + 에러 observation 추출 (같은 `iter_record_ops` 경로)
- `_persist_run_result(formatted, run_dir)` — result.md를 run subdir에 저장
- `_format_delegate_output(result)` — DelegateResult를 구조화된 observation 문자열로 포맷
- `_AGENT_SEARCH_PATHS` — 검색 경로 리스트
- `_FRONTMATTER_PATTERN` — `---` frontmatter 정규식

**DelegateResult 필드**: `output`, `duration_secs`, `activity_log`, `last_actions`, `iterations`

**산출물 구조**: run 실행 결과는 다음 섹션을 포함:
1. 서브에이전트 출력 (output 또는 "(subagent returned no result)")
2. `[Subagent activity]` — per-turn 액션 로그 (최대 20개)
3. `[Last actions before failure]` — 실패 시 마지막 5개 액션 + 에러 힌트
4. `[Duration: Ns]` + `[Subagent used N turns]` — 실행 메타데이터
5. `→ run_{name}_{hash}_{ts}/` — run subdir 경로 (history.jsonl + result.md)

**적용 우선순위**: op 에 명시된 `tools`/`model`이 프로파일 파일 설정보다 우선합니다.

**병렬 run lifecycle 통합 인터페이스** (`_run_parallel`):
- 각 worker thread 는 `renderer.begin_delegate_task(task_id, ...)` → `_run_single` → `renderer.end_delegate_task(task_id, ...)` 만 호출. 그 외 panel/capture 오케스트레이션 전부 renderer 책임.
- `task_id` 는 `delegate-{index}-{uuid4().hex}` (single 경로는 `delegate-single-{uuid4().hex}`). **thread id 가 아니라 uuid4** — `threading.get_ident()` 는 worker thread 종료 후 재사용되므로, 나중 delegate 호출의 worker 가 이전 호출과 동일한 id 를 받아 web 프론트 `ensureTaskGroup`(v7.13.0 접기 UX: 헤더 전체 토글 + **sticky 헤더**[긴 본문 스크롤 시 상단 고정=어느 위치서든 접기]+**본문 여백 클릭**[`e.target===body` 만, 중첩 카드/텍스트 미간섭]) 이 stale 항목에 short-circuit → 새 카드 미생성 버그가 있었음. uuid4 로 호출-간 유일성 보장. 프론트는 `delegate_task_end` 수신 시 `taskGroups[taskId]` 항목을 삭제(DOM 카드는 유지) → 전역 누적 방지 + stale 충돌 원천 차단.
- MinimalRenderer 가 첫 begin 에서 Live 영역 띄움 (`is_terminal` 체크 — non-tty 면 skip), 자체적으로 thread → task slot → capture buffer 매핑. emit (`thought`/`action`/`observation`) 마다 `set_thread_status` 로 카드 상태 업데이트, `_capture_line` 으로 버퍼 누적.
- 마지막 end 에서 Live 종료 + 각 task 의 captured 출력을 `┌─ 🦀 [N] agent: task` group 으로 wrapping 해서 replay (등록 순서).
- **단일 scope 추상화 (v-Phase2, CLI·web 동일 표면)**: skill 서브루프와 delegate/one-shot 워커를 `begin_scope`/`end_scope`(kind="skill"|"run") 하나로 통합. WebRenderer 는 persistent `scope_start`/`scope_end`(+kind) emit + `_thread_to_task` 로 후속 emit 에 task_id 자동 첨부(라우팅은 enclosing scope 복원=중첩 same-thread 안전) → 프론트가 kind별 collapsible card(🪄 skill/🦀 run). ★이전엔 skill 이 프론트 미처리 `group_start` 를 내 `/orchestrate` 가 카드를 안 그리던 버그를 근본 수리. CLI(minimal)는 kind 분기(skill 브래킷/run rich.Live 패널) — 표시만 매체별, 추상화는 동일. teammate begin/end_agent_work 도 scope_* 로 통일.
- 두 renderer 의 lifecycle surface 가 동일 (begin/end 만). 새 renderer 추가 시 이 두 메서드만 override 하면 됨.

### 6.3 run_skill 결과 포맷

`run_skill` 실행 결과에는 스킬 식별 헤더가 포함:

```
STATUS: success
RESULT:
SKILL: summarize(./)
The agent-cli directory contains a ReAct pattern-based agent CLI...
```

- `SKILL: name(arguments)` — 실행된 스킬과 인자
- 스킬은 자체 subdir에 history.jsonl + result.md 저장
- 도구 교집합: skill allowed-tools ∩ parent allowed-tools (빈 교집합 시 거부)
- Role 상속: parent의 Role을 이어받음

### 6.4 Hashline 시스템 (`tools/read_file.py`)

```
원본 파일:             hashline 출력:
def hello():    →    1#VR:def hello():
    return "hi"      2#KT:    return "hi"
                     3#ZZ:

해시 알고리즘: CRC32(line_content, seed) & 0xFF → 2-char 태그
시드: 내용 있는 줄 → 0, 빈 줄 → line_number
알파벳: ZPMQVRWSNKTXJBYH (16자 기반 256 조합)
```

편집 연산:
```json
{"op": "replace", "pos": "2#KT", "lines": ["    return 'hello'"]}
{"op": "replace", "pos": "1#VR", "end": "3#ZZ", "lines": ["def greet():", "    pass"]}
{"op": "append",  "pos": "1#VR", "lines": ["    # 주석"]}
{"op": "prepend", "pos": "1#VR", "lines": ["# 헤더"]}
{"op": "append",  "lines": ["# EOF"]}  // pos 없으면 파일 끝
```

퍼지 매칭 (`edit_file.py`): 해시 불일치 시 공백/따옴표/대시 정규화 후 재매칭. LLM 재호출 없이 비용 제로 보정.

**변이 후 hashline echo (`_change_echo.render_change_echo`, write_file/edit_file 공용).** edit 는 원래 diff 만 반환했다 — diff 는 **무엇이** 바뀌었나는 보여주지만 `LINE#HASH` 태그가 없어, 모델이 방금 편집한 자리를 이어서 편집하려면 `read_file` 재왕복이 필요했다. 이제 성공한 편집은 diff 뒤에 바뀐 영역 ±3줄의 **fresh hashline 블록**을 붙여(절대 줄번호), 후속 edit 을 read 없이 체이닝할 수 있다. **이 조립을 `_change_echo` 로 추출해 `write_file` 의 소량-덮어쓰기 갈래도 동일하게 호출** — 이전엔 edit_file 은 diff+region, write_file 소량-덮어쓰기는 diff-only 라 "같은 diff 인데 출력이 다른" 비대칭이 있었는데, 공용 헬퍼로 **diff 를 내는 모든 관찰이 region refs 를 일관 동반**하도록 일원화했다(write_file 소량-덮어쓰기는 "다음엔 edit_file" 넛지가 붙는 자리라 region refs 와 시너지). 전체 파일 hashline 덤프(=비용 이점 파괴)는 `_MAX_REGION_LINES` 상한으로 막고 나머지는 read_file 로 안내한다. mimicry 우려는 무관 — 이건 **관찰-side**(도구 결과) 변형이라 action 재공급이 아니다(`feedback_refeed_own_output_mimicry` 는 관찰-side 는 안전이라 명시).

**한 op = 한 편집 (flat-native, Step 3).** edit_file 은 `{path, op, pos, end?, lines?}` 단일 편집을 받는다 — 옛 `edits[]` **op-내 중첩** 배열은 제거됐다('op 안에 배열 중첩이 27B 90% 깨뜨림' 실측, DESIGN Exp 8). **같은 파일 다중편집 = 루프 레벨 배치로 부활(다른 형태).** 연속된 같은-path edit_file op 들을 loop(`_dispatch_edit_batch`)이 모아 `apply_edits_batch` 로 처리: 원본 1회 read → 모든 ref 를 그 원본 기준 해석 → 범위 겹침 사전거부(`_find_overlap`) → bottom-up 정렬 적용 → 1회 쓰기, **all-or-nothing**. 옛 안전장치(범위 겹침 거부·bottom-up 정렬)가 **op-내 배열이 아니라 flat op 의 루프 그룹핑**으로 돌아온 셈 — nested-array 함정을 피하면서 "앞 편집이 줄을 밀어 뒤 편집 ref 가 stale" 문제를 원본-기준 해석으로 제거(fuzzy 는 같은 줄번호 정규화 한정이라 드리프트를 못 잡으므로, 애초에 드리프트를 안 만드는 이 방식이 정답 — 외부 검증: NousResearch hashline·5-edit-strategies 벤치마크의 bottom-up). 비연속·다른 파일은 per-op.

### 6.5 Tool Output 전달 방식

Tool output은 **잘림(truncation) 없이 전체를 그대로** LLM에 전달합니다 — 단, 한 관찰이 **`context_window // 10`**(loop `_oversized_cap`)를 넘는 병적 대용량(예: 레포 전체 `find`, 전 심볼 `code_index` 덤프)이면 컨텍스트에 안 들이고 **"좁히라"는 nudge 로 거절**합니다(전체는 어디에도 보존 안 함 — 호출 자체는 성공; 모델이 라인범위/`LIMIT`/`grep`/`tee→read_file` 로 다시 받음). 한 메시지가 윈도우를 넘겨 압축을 깨뜨리는 걸 방지(§5.4 과대 출력 캡 참조). 도구별로 `Tool.render_observation`(결과→관찰 본문)·`Tool.apply_oversized_cap`(기본 True)·`Tool.render_oversized`(캡 초과 시 낼 관찰 — 기본 제네릭 nudge, read_file 은 range/`read_symbols`/run 팬아웃 유도) 표면으로 제어. 이전에는 context window의 3% 비율로 잘랐으나(`tools/truncation.py`, 삭제됨) LLM이 불완전한 정보로 판단하는 성능 열화가 확인되어 제거했고, 그 뒤 청크-spill(history 보존 + `json_extract` 회수)도 거절-nudge 로 대체했습니다(spill 보관-회수 기계 제거 → 단순화). context가 budget의 90%를 넘으면 `context/manager.py`의 compaction이 oldest 절반을 LLM 요약으로 흡수하고, 실패/미충족이면 belt-and-braces로 FIFO drop이 메시지 단위로 떨궈냄.

### 6.5.0b Shell Output: full passthrough (이전 artifact guard 제거됨)

이전엔 shell 출력이 한도(기본 500줄 / 20KB) 초과 시 head/tail 미리보기로
치환하고 전체를 `<session>/shell/`에 저장하는 guard가 있었음. 2026-05-19
제거 — 실사용에서 **head/tail이 중간 디버깅 정보(error trace, 핵심
로그 라인)를 silent하게 누락**시켜 task가 풀리지 않는 사례 두 차례
관찰. 가드의 절약 효과보다 silent loss의 비용이 컸음.

현재 정책: **shell 출력은 잘리지 않고 그대로 LLM observation으로 전달**.
컨텍스트 budget 관리는 messages buffer의 2-tier 관리가 담당
(`context/manager.py`) — 90% 초과 시 oldest 절반을 LLM 요약으로 흡수,
요약 실패/미충족이면 FIFO drop으로 떨궈냄. 의도된 거대 출력
(`find /`, `cat huge.log`)은 모델이 자기 비용 인지하에 호출한 것으로 간주.

LLM이 출력을 좁히고 싶으면 도구 호출 자체를 좁혀야 함 (`tail -n 100`,
`grep ERROR`, `head -c 4096` 등). silent truncation 없음.

관련 환경변수 (`AGENT_CLI_SHELL_OUTPUT_LIMIT_*`, `AGENT_CLI_SHELL_ARTIFACT_*`)
도 함께 제거됨. read_file의 full-read guard 역시 이후 제거됨 — 큰 파일도
bare full read를 허용하고, 컨텍스트 관리는 모델 자율 + downstream
compaction에 맡긴다 (모델이 거부에 헤매던 비용이 더 컸음).

### 6.6 스키마 검증 (`tools/registry.py`)

검증 순서:
1. 도구 존재 확인
2. action_input이 string이면 → dict 자동 변환 시도
3. 필수 필드 존재 확인
4. 타입 검증 + 자동 변환:
   - `"30"` (string) → `30` (integer)
   - `{}` (dict) → `[{}]` (array)
   - `42` (int) → `"42"` (string)

---

## 7. 프로바이더 시스템

### 7.1 LLMProvider 프로토콜 (`providers/base.py`)

```python
class LLMProvider(Protocol):
    def call(
        self,
        messages: list[dict],
        system: str,
        model: str,
        capabilities: ModelCapabilities,
        **kwargs,          # tools, skip_json_format 등
    ) -> LLMResponse: ...
```

### 7.2 프로바이더별 구현

| 프로바이더 | 엔드포인트 | 인증 | 구조화 출력 | Thinking |
|-----------|-----------|------|-----------|---------|
| **Anthropic** | `/messages` | x-api-key | - | budget_tokens |
| **OpenAI Compat** | `/chat/completions` | Bearer token | `response_format={"type":"json_object"}` (basic JSON) | reasoning_effort |

네이티브 tool calling (Anthropic `tool_use`, OpenAI `function calling`)은 **사용하지 않습니다**. 모든 프로바이더가 동일하게 ReAct 텍스트 파싱을 거치므로 provider-specific 코드 경로가 줄고, 프로바이더 편차가 거의 없어집니다.

**구조화 출력 정책**: 두 프로바이더 모두 **basic JSON mode**만 사용하고, **strict JSON Schema는 쓰지 않습니다**. 이는 확장성을 위한 선택이며 다음과 같은 배경이 있습니다:

- strict JSON Schema 강제는 일부 백엔드(예: mlx 엔진으로 패키징된 모델)에서 런타임 에러나 조용한 출력 깨짐을 유발했으므로 미사용 — basic JSON mode만 사용.
- Basic JSON mode(`response_format={"type":"json_object"}`)는 "유효한 JSON을 내라"는 신호만 주고 스키마는 강제하지 않음. 거의 모든 백엔드가 지원.
- ReAct JSON 구조 강제는 대신 시스템 프롬프트의 `FORMAT_RULES`와 3단계 파서(json.loads → json_repair → regex)가 담당. 32B+ 모델에서 신뢰성 충분.
- 7-14B 모델은 schema 없을 때 포맷 drift가 늘지만, 이 사이즈는 README에서 이미 비권장 구간.

향후 특정 백엔드가 strict schema를 반드시 필요로 하면, 현재 기본값을 건드리지 말고 **opt-in 플래그**로 다시 도입할 것. mlx 패키지 모델에서 재발 여지가 있으므로 기본 활성화는 금지.

### 7.3 프로바이더 팩토리 (`providers/__init__.py`)

```python
create_provider("anthropic", base_url, api_key)  → AnthropicProvider
create_provider("openai", base_url, api_key)     → OpenAIProvider
# 그 외 → ValueError("Available: anthropic, openai")
```

OpenAIProvider 하나로 OpenAI, vLLM, LM Studio, mlx-lm을 `--base-url`만 바꿔서 커버.

### 7.4 Thinking Budget 적용

| 프로바이더 | 파라미터 | 동작 | thinking_format |
|-----------|---------|------|----------------|
| Anthropic | `thinking.budget_tokens = budget`, `max_tokens += budget` | Anthropic이 max_tokens에서 thinking 차감 | `""` (API 레벨 처리) |
| OpenAI | `reasoning_effort = low/medium/high` | budget ≤1024→low, ≤8192→medium, >8192→high | `""` (API 레벨 처리) |

Thinking 블록 처리 플로우:
1. thinking 모델(`thinking_format="think"`) → `<think>...</think>` 블록을 텍스트에 출력
2. 각 플러그인 stage 0(`strip_thinking`)이 블록 분리 (thinking_tags 단일 소스)
3. 분리된 thinking 내용은 `ParsedAction.thinking`에 보존
4. 나머지 텍스트(JSON)만 파싱 → Stage 1 직접 성공률 향상

### 7.5 재시도 헬퍼 (`providers/http.py`)

두 프로바이더 모두 동일한 재시도 래퍼 `post_with_retry(requests.post, url, **kwargs)`를 거쳐 HTTP를 발송합니다. 목적은 on-prem LLM 서버(vLLM, LM Studio, omlx)에서 간헐적으로 발생하는 일시적 네트워크 오류 — 서버 재시작 직후의 `ConnectionError`, 첫 호출 시 모델 로딩이 늦어서 발생하는 `Timeout` — 을 사용자 레벨로 노출하지 않고 복구하는 것입니다.

**범위: pre-stream only.** `requests.post()` 호출 자체에서 발생한 예외만 재시도합니다. 스트리밍이 시작된 이후(즉 `requests.post(stream=True)`가 Response를 돌려준 뒤) 청크를 읽다가 발생한 오류는 재시도 대상 아님 — 이미 소비된 청크가 중복되면 LLM 출력이 깨지기 때문.

**재시도 대상 예외:**
- `requests.Timeout` (ConnectTimeout, ReadTimeout 포함)
- `requests.ConnectionError`

**재시도 대상 HTTP 상태 (5.14.1):** 일시적 게이트웨이 5xx — **502/503/504** — 는 재시도 **함**(네트워크 예외와 **독립 예산**, `retry_statuses` kwarg 기본 `{502,503,504}`). on-prem LLM 앞단 리버스 프록시(nginx/caddy)가 업스트림 재시작·과부하 중 내는 오류라, 직접 연결 시의 `ConnectionError` 재시도와 같은 취지로 짧게 재전송하면 대개 회복. 에러 응답은 `r.close()` 후 처음부터 재전송(resume 없음). 나머지(4xx, bare 500)는 재시도 안 함 — `raise_for_status_with_body`가 `post_with_retry` 반환 *뒤에* 호출되어 서버의 거절 응답을 그대로 caller로 전달. **5xx 예산 소진 시엔 raise 하지 않고 마지막 에러 응답을 그대로 반환** → caller가 기존과 동일하게 body 포함해 표면화(context-overflow 400 인식 경로 무영향).

**백오프:** 고정 1초 (지수 아님). on-prem 단일 사용자 전제라 rate-limit / thundering-herd 대책이 필요 없고, `ConnectionError` 직후 서버 부팅 마무리에만 약간의 헤드룸을 주면 충분. `Timeout`은 이미 긴 대기였으므로 추가 대기 효과는 작지만 해롭지도 않음.

**설정:** 고정 상수 (env 조정 불가 — v5.17.0 에서 env 제거, 테스트는 `http` 모듈 상수 monkeypatch).
- `_DEFAULT_ATTEMPTS` (10, 최초 포함 총 시도 횟수) — Timeout / ConnectionError 전용
- `_DEFAULT_STATUS_ATTEMPTS` (3, 최초 포함) — 502/503/504 전용, `_DEFAULT_DELAY` 공용
- `_DEFAULT_DELAY` (1.0초, 네트워크 에러·5xx 공용)
- **Timeout 프로파일 2개** (requests `(connect, read)` 튜플): 비스트리밍 `LLM_API_TIMEOUT=(30,1200)` (post 가 전체 body 읽음 → read=전체생성 idle 상한, 느린 cold 27B 보호). 스트리밍 `LLM_STREAM_TIMEOUT=(30,30)` — post 는 헤더만 읽으므로 read=30 이 **헤더 대기 + 헤더 구간 interrupt 바운드**(broken 서버 ~20분 행 제거). **단일 소켓 timeout 이 헤더·body read 둘 다 지배**(5s post timeout→iter_lines 5s 예외, 실측)하므로, 헤더 수신 후 **`make_stream_patient(r, 1200)`** 가 urllib3 소켓 timeout 을 patient 로 재설정(best-effort, 실패 시 30s 가 body backstop). body stall 은 폴링-루프 idle 감지가 소유: **`interruptible_lines(idle_threshold=30, max_idle_ticks=20, on_idle=...)`** — 30초 무토큰마다 `on_idle`(UI 대기 알림), 20틱(10분) 연속 침묵이면 r.close()+**`StreamIdleTimeout`** raise → 토큰 오면 카운터 리셋. provider(openai)의 스트리밍 콜이 `StreamIdleTimeout` 을 잡아 **재연결+재전송**(생성 재시작, partial 폐기 — 서버 resume 없음), `STREAM_MAX_RECONNECTS=3` 회 후 propagate. interrupt 는 idle 보다 우선(0.2초 폴링) — body 구간 ~8초, 헤더 구간 ≤30초.

**가시성:** 재시도 시 `render_status("running", ...)` 한 줄로 사용자에게 표시(예: `LLM request failed (Timeout) — retrying (2/3)`). spinner는 계속 돌아감. 모두 실패하면 `render_status("error", ...)` 후 마지막 예외를 그대로 raise. verbose 모드에서는 `agent_cli.verbose.debug_log`로 stderr에도 한 줄 남김.

**테스트 호환:** `post_with_retry`는 `post_fn`을 인자로 받고, 각 프로바이더는 자기 네임스페이스의 `requests.post`를 명시적으로 넘깁니다. 덕분에 기존 테스트가 `agent_cli.providers.{name}.requests.post`를 패치하는 패턴이 그대로 동작.

### 7.6 공용 debug 유틸 (`verbose.py`)

`agent_cli/verbose.py`가 verbose 플래그와 `debug_log()`의 단일 소유자입니다. 과거에는 `loop.py` 모듈 안에 `_debug_verbose` / `_debug_log`로 있었으나, `providers/http.py`가 재시도 로그를 찍어야 하면서 provider 레이어가 loop를 역참조하지 않도록 추출했습니다. `loop.py`는 하위 호환을 위해 해당 심볼을 그대로 재-export합니다.

---

## 8. 설정 시스템

### 8.0 config.json (프로바이더/모델 설정)

```json
{
  "provider": "openai",
  "base_url": "http://127.0.0.1:8000/v1",
  "api_key": "",
  "default_model": "gpt-4o"
}
```

**3레이어 병합** (`load_config()`):
```
env vars (AGENT_CLI_*)  →  최저 우선순위
~/.agent-cli/config.json →  사용자 전역
.agent-cli/config.json   →  워크스페이스 (최고)
+ CLI 파라미터             →  임시 오버라이드
```

필드 단위 병합: 상위 레이어가 해당 필드를 가지면 덮어씀, 없으면 하위에서 상속.

**SetupWizard** (`setup.py`): 설정 파일이 없으면 자동 실행.
`agent-cli setup`으로 수동 재설정 가능.

**DIRECTIVE.md** — 프로젝트 지시사항 (`prompts/system_prompt.py`):
```
.agent-cli/DIRECTIVE.md   →  프로젝트별 규칙 (우선 로드)
~/.agent-cli/DIRECTIVE.md →  사용자 전역 규칙
```
- 둘 다 존재하면 모두 로드 (content hash 중복 제거)
- content hash 중복 제거, truncation 없음 (ResourceLoader 기반)
- scope 라벨은 위치 기반(`[project, user]`); cwd == home 이면 두 경로가 같은 파일로 resolve → path-dedup 으로 1회만 로드, project 로 라벨
- 매 세션 시작 시 system prompt 동적 영역에 주입

### 8.1 models.json 구조

```json
{
  "models": {
    "<model_id>": {
      "provider": "anthropic | openai",
      "context_window": 32768,
      "max_output_tokens": 4096,
      "supports_structured_output": true,
      "supports_thinking": true,
      "thinking_budget": 4096,
      "supports_strict_schema": false
    }
  },
  "provider_defaults": {
    "openai": {"base_url": "https://api.openai.com/v1", "default_model": "gpt-4o"},
    "anthropic": {"base_url": "https://api.anthropic.com/v1", "default_model": "claude-sonnet-4-20250514"}
  }
}
```

### 8.2 파일 위치 및 정책

| 우선순위 | 위치 | 역할 | 자동 저장 |
|---------|------|------|----------|
| 1 | `.agent-cli/models.json` | 프로젝트 로컬 오버라이드 | 안 함 (읽기만) |
| 2 | `~/.agent-cli/models.json` | 사용자 전역 설정 | 새 모델 자동 저장 |
| 3 | `agent_cli/default_models.json` | 패키지 기본값 | 안 함 (읽기만) |

### 8.3 설정 로딩 우선순위 (`config.py`)

3개 파일을 병합하되, 높은 우선순위가 낮은 우선순위를 오버라이드:
1. `agent_cli/default_models.json` (패키지) — 먼저 로딩
2. `~/.agent-cli/models.json` (전역) — 동일 키 덮어쓰기
3. `.agent-cli/models.json` (프로젝트 로컬) — 동일 키 덮어쓰기 (최종)
4. 하드코딩 폴백 (모든 파일 없어도 동작)

### 8.4 능력치 조회 우선순위 (`providers/capabilities.py`)

1. `models.json` 정적 설정 (병합된 결과)
2. 런타임 감지 → **`~/.agent-cli/models.json`에 자동 저장**
   - OpenAI 호환: `/v1/models` (`max_model_len`) + overflow probe fallback (context window) + `/chat/completions` (thinking 프로브)
3. `DEFAULT_CAPABILITIES` (context_window=4096, 모든 기능 비활성)

프로브는 진행 콜백을 받아 첫 실행 시 어느 단계가 돌고 있는지 사용자에게 표시 (`set_progress_callback`). 한 번 감지된 결과는 `_auto_detected: true` 마커와 함께 저장되어 재실행 시 프로브 생략.

### 8.6 Thinking 감지 방식

하드코딩 패턴 매칭이 아닌 **프로브 기반 감지**:
1. 모델에 "What is 2+2?" 프롬프트 전송
2. 두 가지 위치에서 thinking 확인:
   - `reasoning_content` 필드 (OpenAI 호환 — vLLM 컨벤션)
   - `<think>`, `<thinking>`, `<reasoning>`, `<reflection>` 태그 in content (DeepSeek-R1 등)
3. 감지되면 → `supports_thinking=True`, `thinking_format=감지방식`
4. 결과를 `~/.agent-cli/models.json`에 저장 (`_auto_detected: true`) → 다음 실행 시 프로브 불필요
5. 모델 업데이트 시 자동 감지 항목은 재감지로 갱신됨 (수동 등록 항목은 보호)

새 모델이 추가되어도 코드 수정 없이 자동 감지됩니다.

OpenAI 호환 서버(vLLM 등)에서는 `/v1/models` API로 context window도 감지합니다 (`max_model_len` 필드). 메타데이터에 없으면 overflow probe fallback으로 결정.

### 8.5 모델 정보 출력

| 상황 | 출력 |
|------|------|
| 새 모델 감지 + 저장 | Rich Panel (상세 — context, thinking, tool calling 등) |
| 기존 모델 로딩 | 한 줄 요약 (`● Model: name (ctx=N, thinking=✓)`) |

---

## 9. 시스템 프롬프트 아키텍처 (`prompts/system_prompt.py`)

LLM attention 패턴에 최적화된 섹션 순서 — Primacy(앞), Middle(중간), Recency(끝):

```
build_system_prompt(capabilities, active_tools, skill_stack, session_id, agent_role, agent_registry, ...)
    │
    │  ── Primacy: 정체성 + 핵심 원칙 (강한 attention) ──
    │
    ├─ ROLE_PROMPT (항상 포함 — 에이전트 역할 정의)
    │
    ├─ CONTEXT_DISCIPLINE (항상 포함 — 컨텍스트 창이 핵심 리소스임을 교육)
    │   └─ "읽을 것만 읽어라 / thought 간결 / 불필요한 덤프 금지"
    │
    ├─ TASK_GUIDELINES (항상 포함 — 코드 작업 원칙 7개)
    │   └─ 코드 읽기 선행, 범위 제한, 보안, 정직한 보고 등
    │
    ├─ FORMAT_RULES (항상 포함 — JSON ReAct 포맷 + 규칙 10개)
    │   └─ 재귀 금지, 단일 액션 강제,
    │      효율적 액션 선택 (batch 필드 활용 / shell 파이프라이닝 / 좁은 read 모드 우선)
    │
    │  ── Middle: 레퍼런스 (필요시 참조) ──
    │
    ├─ Available Tools (active_tools + _ALWAYS_INCLUDE)
    │   └─ 정적 도구 먼저 (KV cache 안정), 조건부 도구 뒤에
    │   └─ 가이드가 해당 도구에 inline (별도 섹션 없음):
    │       - edit_file ← Hashline Guide
    │       - agent ← Delegation Guide (run/spawn)
    │
    ├─ Available Skills (skill_stack에 없는 스킬만, run_skill 사용 안내)
    │
    ├─ Available Agents (depth < max_depth + agent_stack 재귀 방지)
    │   └─ .agent-cli/agents/ + ~/.agent-cli/agents/ + builtin/ 스캔
    │
    │  ── Recency: passive reference → active rules → immediate constraint ──
    │
    ├─ Environment (항상 포함 — CWD, 플랫폼)
    │   └─ 날짜는 의도적으로 제외 — KV prefix cache 안정성 (자정 rollover 방지)
    │
    ├─ Context Recovery Guide (session_dir가 있을 때만)
    │   └─ "이전 대화 내용이 필요하면 read_file({session_dir}/history.jsonl)"
    │
    ├─ Directives (DIRECTIVE.md가 존재할 때만)
    │   └─ .agent-cli/DIRECTIVE.md (프로젝트) + ~/.agent-cli/DIRECTIVE.md (유저 전역)
    │
    └─ Execution Context (skill_stack/agent_stack이 있을 때만 — Recency 마지막)
        ├─ "Call stack: main → agent:coder → skill:plan"
        ├─ "Do not run or invoke: coder, plan (already in call stack)"
        └─ 세션 내 변동 가능한 유일한 Recency 섹션 → 끝에 두어 앞 3개를 안정적
           KV prefix로 보존
    
    Role 선택 (Primacy 영역):
    - main: 기본 ROLE_PROMPT
    - 서브에이전트(run/spawn): 프로파일 본문이 기본 Role을 대체
    - skill: parent의 Role 상속
```

---

## 10. 테스트 아키텍처

### 10.1 테스트 분류

| 분류 | 파일 수 | 테스트 수 | 실행 방법 |
|------|---------|----------|----------|
| 유닛 테스트 | ~69 | ~2030 | `pytest tests/` |
| omlx 통합 (E2E) | 2 | ~25 | `pytest tests/ -m omlx_integration` |

**실브라우저 테스트 (`tests/browser/`, v7.12.0):** playwright + 헤드리스 chromium 으로 jsdom 유닛이 원리적으로 못 잡는 부류 — origin 당 6연결 고갈, secure context, CSS 렌더링(칩 세로 클리핑·flex ellipsis), 실 SSE 타이밍, 서버↔프런트 실계약 — 를 검증 (confirm-starvation 사가의 프런트 버그 4건이 전부 이 층에서만 잡힌 실증). `conftest.py::WebStack`=실 uvicorn 위 WebRenderer+WebServer(포트 0 임시할당) + worker 헬퍼(`start_confirm_loop`/`start_ask`, teardown 이 `push_abort` 로 블록된 worker 를 풀어 모듈-전역 interactive_lock 해제 — 테스트 간 간섭 방지). 시나리오: confirm 클릭·해결·comment·다중뷰어 409 fold, ANSWERING 왕복, 부재 중 pending replay, 헤더 칩 가시성·복사·ctx 팝오버, stall 경고 표출/정리. **옵트인**: `AGENT_CLI_BROWSER_TESTS=1` 아니면 루트 conftest `collect_ignore=["browser"]` 로 **수집조차 안 함**(per-item skip 만으로는 pytest-asyncio 가 수집 단계 이벤트루프를 남겨 이후 async e2e 를 깨뜨림 — 실측 수리). CI 는 별도 `browser` 잡(chromium install). **omlx 통합 테스트:** `tests/test_integration_omlx.py` + `tests/test_integration_omlx_builtin.py`. 실 OpenAI 호환 omlx 서버를 대상으로 `run_loop`(질문/read/shell/write/edit/multi-step), `provider.call` ReAct 파싱, 런타임 capability 감지, 스킬 실행(fork·dynamic injection·allowed_tools·디렉토리 구조·bracket args), 훅(Pre/PostToolUse), agent run(none/fork), code-analyst 프로파일, plan 스킬, @<profile> dispatch를 검증. conftest fixtures(`omlx_provider`, `integration_model`, `model_capabilities`)는 서버 `/v1/models` 프로브로 가용성을 확인하고, 미가용 시 전부 skip → `pytest tests/`는 항상 green. 연결은 env(`OMLX_BASE_URL` 기본 `http://127.0.0.1:8000/v1`, `OMLX_API_KEY`, `INTEGRATION_MODELS` 기본 `Qwen3.6-27B-MLX-8bit`)로 override. 순수 로딩/프롬프트 검증은 유닛(test_builtin_skills/agents)에 있어 통합에서는 제외.

### 10.2 테스트 실행

```bash
# 전체 (유닛; 통합은 서버 미가용 시 자동 skip)
pytest tests/ -v

# 특정 모듈
pytest tests/test_wire_formats_json_fc.py -v

# omlx 통합 E2E (실 서버 필요)
pytest tests/ -m omlx_integration -v
```

---

## 11. CLI 명령어 레퍼런스

### 11.1 `run` — 단발 실행

```bash
agent-cli run "task description" [options]
  -p, --provider    openai | anthropic    (기본: openai)
  -m, --model       모델 ID                       (기본: 프로바이더 기본값)
  --base-url        API 엔드포인트
  --api-key         API 키 (환경 변수 자동 감지)
  -n, --max-turns    최대 턴 (0=무제한)
  --max-depth       서브에이전트 중첩 깊이 (기본: 2)
  --agent-timeout    서브에이전트 타임아웃 초 (기본: 300)
  -v, --verbose     원시 LLM 응답 표시

  /sh <cmd>         LLM 없이 셸 명령 직접 실행
```

`run` 도 `web` 과 동일하게 세션/컨텍스트(compaction + FIFO fallback + history.jsonl + compaction.json)를 관리합니다. 완료 후 세션 ID가 출력되며 `web --resume <id>`로 이어서 작업할 수 있습니다 (compaction state는 `dynamic_start_index`로 복원되어 summarised tail과 중복 없음).

### 11.2 `web` — 대화형 브라우저 UI

```bash
agent-cli web [options]
  (run 옵션 + --host/--port/--token/--no-browser/--resume/--idle-timeout/--trust-local/--base-path). **`--base-path <prefix>`(경로 prefix 라우팅)**: 리버스 프록시가 `/<prefix>/*` → 이 인스턴스(+prefix strip)로 라우팅할 때 — 프론트 URL 전부 **상대경로**(`api/...`/`static/...`)이고 `index()` 라우트가 serve 시 `<base href="<prefix>/">` 주입(기본 `<base href="/">`=루트, 동작 byte-동일). 서버 routes 무변경(프록시 strip→`/api/...` 수신). 회귀 가드 `test_web_base_path.py`(serve 프론트에 절대 `/api`·`/static` 0). **`--trust-local`(loopback 토큰 면제)**: 신뢰된 로컬 게이트웨이(127.0.0.1 바인드 인스턴스를 프록시·인증) 뒤에서 게이트웨이가 토큰을 매 요청 주입 안 하게 — pure-ASGI `_TrustLocalMiddleware` 가 `server.is_trusted_client(host)`(trust_local AND peer∈{127.0.0.1,::1}) 면 `_with_token_query` 로 유효 토큰을 query 에 주입(기존 endpoint 토큰검사 통과). 끈 상태/비-loopback 은 byte-동일(토큰 그대로). 브라우저 자동 오픈은 `_is_local_bind(host)`(loopback/wildcard) 일 때만 — 특정 IP(원격 bind)면 생략하고 URL 만 출력(서버에서 브라우저 무의미). **`--idle-timeout N`(self-reap)**: 외부 오케스트레이터(게시판류)가 인스턴스를 온디맨드로 띄우고 회수 안 하게 — N초 동안 비활성이면 스스로 종료(다음 접속 `--resume` 재기동). 순수 결정 로직 `web/idle.py::IdleMonitor`(clock 주입, 단위테스트) + web() 의 데몬 폴링 스레드가 `tick()` → `server_obj.should_exit=True`(기존 finally 가 teardown+세션저장). **활성(=안 죽임) = `renderer.has_live_connections()` OR `renderer.worker_is_busy()` OR `server.pending_count()>0`** — busy(작업/질문대기) 면 mid-task 회수 안 함. 기본 0=비활성(하위호환). **인스턴스 파일 (`web/instance_file.py`)**: web 시작 시 `.agent-cli/sessions/<id>/web.json`(`{session_id, host, port, token, pid}`)을 기록하고 finally 에서 제거 — 외부 오케스트레이터가 "이 세션 web 떠 있나/어디로" 를 파일 하나로 알아 spawn-or-attach(pid 죽었으면 stale→재spawn). 순수 write/read/remove(서버 의존 0). idle-timeout(self-reap)+제거가 짝이라 오케스트레이터는 프로세스 추적·kill 불필요. **라이브 상태 사이드카 `status.json`(`{busy, awaiting_input, viewers}`)**: web.json(준정적 핸드셰이크)과 분리된 별도 파일로, 오케스트레이터가 `GET /api/health` 를 폴링하는 대신 **파일 read** 로 라이브니스를 읽게 한다. `WebRenderer` 가 viewer 등록/해제·busy 토글(worker_state sticky)·awaiting 토글(input_required sticky) 변화마다 원자적(**유니크 temp**(`tempfile.mkstemp`)+`os.replace`)으로 재기록 — 스냅샷만 lock 으로 잡고 디스크 I/O 는 lock 밖(재진입·블로킹 회피). 에이전트-루프 스레드와 웹 스레드가 동시에 쓰므로 tmp 는 반드시 write 별로 유니크해야 함(고정 tmp 는 `os.replace` 경합→`FileNotFoundError` 크래시, v4.27.1 수정). 시작 시 seed·finally 에서 제거(web.json 과 짝). `_STATUS_STICKY_KEYS={worker_state,input_required}` 만 재발행(무관 sticky 는 스킵).

  # 웹 명령어 (handle_slash_command + 공유 dispatch):
  /help              명령어 안내
  /sh <cmd>          LLM 우회 셸 실행
  /compact           수동 컨텍스트 compaction
  /skills            스킬 목록
  /<skill> <args>    스킬 실행
  @agents            에이전트 목록
  @<agent> <task>    에이전트에 위임
```
다중 뷰어 (모두 동등 — 모두 입력·큐 가능). 상세는 server.py / web.py 엔트리 참조.

---

## 12. 확장 가이드

### 12.1 새 프로바이더 추가

1. `providers/` 디렉토리에 새 파일 생성 (예: `google.py`)
2. `LLMProvider` 프로토콜을 만족하는 클래스 구현:
   ```python
   class GoogleProvider:
       def __init__(self, base_url: str, api_key: str): ...
       def call(self, messages, system, model, capabilities, **kwargs) -> LLMResponse: ...
   ```
3. `providers/__init__.py`의 `create_provider()`에 분기 추가
4. `config.py`의 `_PROVIDER_FALLBACKS`에 기본값 추가
5. `models.json`에 모델 등록
6. `tests/test_providers.py`에 테스트 추가

### 12.2 새 도구 추가

1. `tools/` 디렉토리에 새 파일 생성 (예: `search.py`)
2. `tool_search(args: dict) -> str` 함수 구현
3. `tools/registry.py`의 `TOOL_SCHEMAS`에 스키마 추가
4. `tools/__init__.py`의 `TOOLS` dict에 등록
   - 가상 도구(loop 인터셉트)면 `loop.py`의 if-cascade에 `if parsed.action == "<name>":` 분기 추가
   - 항상 포함되어야 하면 `registry.py`의 `_ALWAYS_INCLUDE`에도 추가
5. `tests/test_registry.py`에 검증 테스트 추가

### 12.3 새 모델 등록

`models.json`에 항목 추가:
```json
"new-model:14b": {
  "provider": "openai",
  "context_window": 16384,
  "max_output_tokens": 4096,
  "supports_structured_output": true,
  "supports_thinking": false,
  "thinking_budget": 0,
  "supports_strict_schema": false
}
```

미등록 모델은 런타임 감지(OpenAI 호환) 또는 보수적 기본값으로 동작합니다.

### 12.4 새 wire format 추가

ReAct 외 새 응답 형식(예: PREFIX-MD 마크다운, OpenAI 스타일 tool call,
실험용 multi-action 등)을 추가하려면 `agent_cli/wire_formats/`에 새 모듈 한 개를
만들면 됩니다. **main code path(loop.py / system_prompt.py / recovery/)는 수정하지
않습니다** — 분기점이 `WireFormat` ABC 안에 격리되어 있기 때문입니다.

ABC가 lifecycle / 식별 hook의 default를 제공하므로 plugin은 **format-specific
abstract method만 구현**하면 됩니다. 나머지는 자동 작동.

1. `agent_cli/wire_formats/<name>.py` 생성:
   ```python
   from agent_cli.wire_formats.base import ParsedAction, WireFormat

   class MyFormat(WireFormat):
       name = "my_format"
       thought_required = True  # thought가 schema 필수 필드면 True

       # ── 필수 abstract (format-specific) ──
       def parse(self, llm_text) -> ParsedAction: ...
       def render_full_example(self, *, thought, action, action_input) -> str: ...
       def format_rules_anchor(self) -> str: ...
       def format_rules_field_specific(self) -> str: ...
       def constraint_reminder_call(self) -> str: ...
       def constraint_reminder_action_required(self) -> str: ...
       def failure_framing_parse_fail(self) -> str: ...
       def failure_framing_no_action(self) -> str: ...
       def static_retry_hint_no_json(self) -> str: ...
       def static_retry_hint_no_action(self) -> str: ...
       def system_user_prefixes(self) -> tuple[str, ...]: ...

       # ── 선택 override (그 plugin이 default와 달라야 할 때만) ──
       # def prefill(self) -> str: ...               # default ""
       # def provider_call_kwargs(self) -> dict: ... # default {}
       # def sanitize_thought(self, thought) -> str | None: ...       # default identity
       # def render_action_input(self, action_input) -> str: ...      # default identity
       # serialize_assistant_for_history / render_assistant_from_history /
       # format_rules도 base default 사용 가능
   ```

2. `agent_cli/wire_formats/__init__.py`의 `_register_builtin_plugins()`에 등록 추가:
   ```python
   from agent_cli.wire_formats.my_format import MyFormat
   register(MyFormat())
   ```

3. `tests/test_wire_formats_<name>.py`에 동작 테스트 추가
4. 사용:
   ```bash
   agent-cli run "task" --response-format my_format
   ```

`thought_required=True`인 plugin은 추가로 `format_no_thought_retry(prior_content=…) -> Intervention` 인스턴스 메서드를 구현해야 합니다 (ABC base 외 — duck typing; loop이 `thought_required` 가드 후 호출). ReActFormat이 참고 구현입니다.

폐기는 폴더에서 파일을 지우고 `_register_builtin_plugins()`에서 등록 줄을 빼면 끝 — main code 변경 없음.

---

## 13. 스킬 시스템 (`skills/`)

### 13.1 개요

프롬프트 스킬은 특정 작업에 최적화된 재사용 가능한 프롬프트 템플릿입니다. Claude Code의 스킬 파일 포맷과 호환되도록 설계되었습니다.

### 13.2 스킬 파일 포맷 (Claude Code 호환)

```markdown
---
name: review-code
description: Review code for bugs and security
allowed-tools: [read_file]
max-turns: 5
argument-hint: "<file_path>"
---

You are a code reviewer. Read $ARGUMENTS and analyze for bugs.
```

| Frontmatter 필드 | 타입 | 설명 |
|-----------------|------|------|
| `name` | string | 슬래시 명령어 이름 |
| `description` | string | 스킬 설명 |
| `allowed-tools` | list[str] | 허용 도구 (미지정 시 전체) |
| `max-turns` | int | 최대 턴 (미지정 시 기본값) |
| `argument-hint` | string | 인자 힌트 |

### 13.3 인자 치환

| 패턴 | 설명 |
|------|------|
| `$ARGUMENTS` | 전체 인자 문자열 |
| `$0`, `$1`, ... | N번째 인자 (0-indexed) |

### 13.4 스킬 검색 경로

1. `.agent-cli/skills/*.md` (프로젝트 로컬, 최우선)
2. `~/.agent-cli/skills/*.md` (사용자 전역)
3. `agent_cli/skills/builtin/*.md` (패키지 내장, 최하위)

동일 name의 스킬이 여러 위치에 있으면 상위 우선순위가 오버라이드합니다.

패키지 내장 스킬:
- `create-skill` — 새 스킬 파일 대화형 생성
- `create-agent` — 새 에이전트 정의 파일 대화형 생성
- `plan` — 기능 요청을 작업 분해 + 의존성 + 범위 추정으로 구조화 (plan/ 저장)

### 13.5 실행 플로우

```
사용자 입력: /review-code src/auth.py
    │
    ▼
load_skills() — 호출 시점마다 디스크 재스캔, 파일 파싱
    │  └─ 캐시 없음. /create-skill로 방금 만든 스킬도 재시작 없이 즉시 인식
    ▼
스킬 매칭: "review-code" → Skill 객체
    │
    ▼
substitute_arguments() — $ARGUMENTS → "src/auth.py" 치환
    │
    ▼
run_loop(query=치환된_프롬프트, allowed_tools=["read_file"], max_turns=5)
    │  └─ loop.py의 기존 인프라 그대로 활용
    ▼
결과 반환
```

### 13.6 스킬 스택 (재귀 방지)

스킬이 `run_skill`로 다른 스킬을 호출할 수 있지만, 재귀는 방지:

```
A→B: 허용 (summarize → optimize)
A→A: 차단 (summarize → summarize)
A→B→A: 차단 (summarize → optimize → summarize)
```

방어 메커니즘 3단계:
1. **skill_stack** — `run_loop`이 `skill_stack: list[str]`를 추적. `_handle_run_skill`이 스택에 같은 이름이 있으면 에러 반환.
2. **시스템 프롬프트** — `build_skill_descriptions(exclude_names=skill_stack)`로 현재 실행 중인 스킬을 Available Skills에서 숨김. LLM이 재귀 시도 자체를 하지 않도록 유도.
3. **프롬프트 규칙** — Rule 7: "NEVER invoke yourself recursively via shell"

### 13.7 커스텀 스킬 작성

`.agent-cli/skills/my-skill.md` 파일을 생성하면 자동으로 `/my-skill` 명령어가 등록됩니다.

### 13.8 기본 내장 스킬

| 스킬 | 도구 | 설명 |
|------|------|------|
| `/review-code <file>` | read_file, shell | 코드 리뷰 (버그, 보안, 성능) |
| `/summarize <path>` | read_file, shell | 파일/디렉토리 요약 |
| `/test <file>` | read_file, write_file, shell | 유닛 테스트 생성 |
| `/optimize <path>` | read_file, shell, write_file | 코드 최적화 분석 → OptimizationToDo.md |

---

## 14. Hook 시스템 (`hooks/`)

### 14.1 개요

Python hook + shell hook 두 가지 방식의 라이프사이클 훅을 지원한다.
- **Python hook**: `.agent-cli/hooks/*.py` — context window 조작, MCP 메모리 접근 가능
- **Shell hook**: `.agent-cli/hooks.json` — 외부 명령 실행 (기존 방식, 하위 호환)
- **Skill-local shell hook**: SKILL.md frontmatter의 `hooks:` 섹션 — 해당 스킬이 실행되는 동안만 적용되는 로컬 matcher. 호출자의 hooks_config와 `merge_hooks_configs(parent, skill.hooks)`로 합쳐져서 부모 훅과 함께 발동.
- **Agent-local shell hook**: 에이전트 정의 파일(`.agent-cli/agents/*.md`) frontmatter의 `hooks:` 섹션 — 해당 프로파일로 run/spawn 되는 동안만 적용되는 로컬 matcher. skill과 동일한 merge 계약: `merge_hooks_configs(parent, agent.hooks)`로 부모 훅 뒤에 덧붙여 fire.
- **agent 전파**: run 엔진(`tool_delegate`)이 `hooks_config`를 subagent `run_loop`에 그대로 전달 (상주 spawn 도 runtime 경유 동일). 즉 전역/프로젝트/스킬 훅은 모두 상속되고, 에이전트 자신의 overlay까지 그 위에 얹힘.

### 14.2 라이프사이클 이벤트 (11개)

| 이벤트 | 시점 | 함수명 |
|--------|------|--------|
| OnSessionStart | 세션 시작 후 | `on_session_start(ctx)` |
| PreLLMCall | LLM 호출 직전 (매 턴) | `pre_llm_call(ctx)` |
| PostLLMCall | LLM 응답 수신 후 | `post_llm_call(ctx)` |
| PreToolUse | 도구 실행 직전 | `pre_tool_use(ctx)` |
| PostToolUse | 도구 실행 직후 | `post_tool_use(ctx)` |
| OnTurnEnd | 턴 종료 후 | `on_turn_end(ctx)` |
| OnAgentStart | agent run 실행 직전 | `on_agent_start(ctx)` |
| OnAgentEnd | agent run 완료 후 | `on_agent_end(ctx)` |
| OnSkillStart | skill 실행 직전 | `on_skill_start(ctx)` |
| OnSkillEnd | skill 완료 후 | `on_skill_end(ctx)` |
| OnSessionEnd | 세션 종료 시 | `on_session_end(ctx)` |

### 14.3 Python Hook 파일 규약

```python
# .agent-cli/hooks/00_memory.py
EVENTS = ["OnSessionStart", "OnTurnEnd"]

def on_session_start(ctx):
    memories = ctx.search_memory("project context")
    if memories:
        ctx.inject_system_section("Memory", format_memories(memories))

def on_turn_end(ctx):
    ctx.store_memory([{"name": "...", "entityType": "decision", "observations": [...]}])
```

- 파일명 숫자 prefix 순서 실행 (`00_` → `10_` → `20_`)
- 프로젝트 hooks → 유저 hooks 순서
- `EVENTS` 리스트로 구독할 이벤트 선언
- 에러 발생 시 해당 hook 건너뜀 (에이전트 루프 중단 없음)

### 14.4 HookContext

hook 함수가 받는 컨텍스트 객체:
- **읽기**: `event`, `messages`, `session_dir`, `turn`, `tool_name`, `tool_input`, `tool_result`, `llm_response`
- **context 조작**: `inject_message()`, `inject_system_section()`, `remove_system_section()`
- **도구 제어** (PreToolUse): `block(reason)`, `modify_input(new_input)`
- **MCP 메모리**: `store_memory()`, `search_memory()`, `read_memory()`

### 14.5 실행 순서

```
이벤트 발생 → HookContext 생성 → Python hooks (파일명 순) → Shell hooks (hooks.json)
```

### 14.6 loop.py 통합

```
AgentLoop.run()
  ├─ _setup() → OnSessionStart
  ├─ _execute_turn()
  │   ├─ PreLLMCall → system_sections 적용
  │   ├─ _call_llm()
  │   ├─ PostLLMCall
  │   ├─ self._dispatch_tool_with_hooks()
  │   │   ├─ PreToolUse (Python) → PreToolUse (Shell)
  │   │   ├─ OnAgentStart / OnSkillStart
  │   │   ├─ 도구 실행
  │   │   ├─ OnAgentEnd / OnSkillEnd
  │   │   └─ PostToolUse (Python) → PostToolUse (Shell)
  │   └─ OnTurnEnd
  └─ OnSessionEnd (finally)
```

---

## 15. 설계 원칙

1. **모델은 commodity, harness가 성패를 결정한다** — 파싱 폴백, 도구 출력 압축, 퍼지 편집 등 harness 레벨 최적화가 핵심
2. **프로바이더별 최선의 방식 자동 선택** — 네이티브 tool calling > basic JSON mode > 텍스트 파싱 (strict JSON Schema는 확장성 이슈로 미사용)
3. **소형 모델 우선 설계** — 보수적 기본값, 적응형 출력 압축, 스키마 자동 변환
4. **비용 제로 보정 우선** — LLM 재호출 없이 harness에서 보정 (퍼지 매칭, 타입 변환)
5. **점진적 기능 저하** — 기능 미지원 시 에러 대신 다음 폴백으로 graceful degradation
6. **순환 의존 없는 단방향 모듈 구조** — config → capabilities → base → adapters → loop → main
