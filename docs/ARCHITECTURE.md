# Agent-CLI v2 아키텍처 문서

> **이 문서는 코드와 함께 유지보수되어야 합니다.**
> 코드 수정 시 관련 섹션을 반드시 업데이트하세요.
>
> 최종 업데이트: 2026-05-25
> 버전: 2.0.0-dev
> 총 소스: ~22,800 LOC (89 Python 파일) + ~27,200 LOC 테스트 (70 파일)
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

**시스템 패키지**: C/C++ 인덱싱의 `unifdef` 단계는 번들된 pure-Python (`_unifdef.py`) 이 기본 처리 — 설치 불필요. 시스템 `unifdef` 바이너리가 있으면 자동 우선 사용 (battle-tested C). `AGENT_CLI_UNIFDEF=pure|system|auto` 환경변수로 명시적 강제 가능.

표준 라이브러리: json, re, dataclasses, pathlib, os, sys, zlib, textwrap, unicodedata, copy, tempfile, threading, sqlite3 (code_index — stdlib 우선, 미존재 시 `_sqlite.py` shim 이 `pysqlite3-binary` 로 폴백)

---

## 2. 디렉토리 구조

```
agent_cli/
├── __init__.py              (3)    패키지 버전 (__version__ = "2.0.0-dev")
├── __main__.py              (5)    python -m agent_cli 진입점
├── main.py                  (~1450) CLI 명령어: run, web, setup, sessions, update, --style, --response-format, resume preview. **`update`**: `gh release view`로 최신 릴리스 태그 확인 → `_parse_version` 비교 → 새 버전이면 `gh release download`(wheel)+`pip install --upgrade`(`--check` 확인만, `-y` 확인생략). editable/dev 설치는 `_is_editable_install` 감지해 거부+`git pull` 안내(`--force` 우회). **세션 표시 공유 헬퍼**: `_print_session(meta)` 가 id·시각 + `session_summary` 기반 `↳ 마지막 요청` / `→ 마지막 결과`(또는 `(in progress)`)를 한 블록으로 출력 — `sessions` 명령과 resume 프롬프트가 동일 포맷 공유. **`_maybe_resume_recent(workspace, response_format, prompt_fn)`**: `--resume` 없이 `web` 진입 시 가장 최근 세션을 보여주고 `[y/N]` 질의 — `y` 면 `load_session` 으로 이어가고(`is_resume=True`) 그 외엔 `create_session` 으로 새로 시작. `prompt_fn` 은 TTY 면 `input`, 비대화(파이프/cron)면 `None` → 묻지 않고 항상 새 세션 (안전 기본값). **`DispatchOutput` Protocol + `_ConsoleDispatchOutput` + `try_dispatch_agent_or_skill`** — `@<agent>`/`/<skill>` 접두사 처리 (listing, invocation, not-found) 공유 dispatcher. `run` 은 `_ConsoleDispatchOutput`(Rich 색상), `agent-cli web` worker 는 `web.server.WebDispatchOutput`(observation 이벤트) 어댑터 주입. unknown `@`/`/` 명령은 LLM으로 통과하지 않고 error observation 발사 (오타로 인한 사고성 LLM round-trip 방지). **`web` 명령**: `--resume <id>` 지원 — provider 핸드셰이크 전에 `load_session` pre-check로 unknown ID fail-fast, `ContextManager(resume=True)` 로 캐시 복구 후 `renderer.replay_from_history(ctx)` 한 번 호출해 persistent event buffer를 재구성 → 이후 새 SSE 연결의 snapshot replay로 이전 turn이 그대로 UI에 복원. **graceful shutdown**: `uvicorn.Server(config).run()` 직접 호출 + `KeyboardInterrupt` swallow + `finally` 블록에서 `renderer.shutdown_all_connections()` → `server.shutdown()` → `worker.join(timeout=2s)` → `finalize_session(...)` 순서로 정리 (lifespan shutdown 훅이 SSE generator를 먼저 닫아도 idempotent).
├── resource_loader.py       (144)  ResourceLoader — 파일 검색/우선순위 (스킬/에이전트/지시사항)
├── config.py                (217)  config.json 3레이어 로딩 + models.json 레지스트리
├── setup.py                 (267)  SetupWizard (Rich TUI, 첫 실행 설정 마법사 — 기존 config 노출 + 프로브 진행 표시). 모델 선택: OpenAI 호환·Anthropic 둘 다 `/v1/models`(`_list_models(provider)` — OpenAI=`Bearer`, Anthropic=`x-api-key`+`anthropic-version` 헤더; 응답 `data[].id` 동형)로 목록 표시 후 선택(`_select_model_from_list`), 실패 시 수동 입력(OpenAI 기본 `gpt-4o`, Anthropic 기본 `claude-sonnet-4-20250514`). omlx 가 두 API 를 같은 모델로 서빙 + 실 Anthropic 도 GET /v1/models 지원이라 양쪽 동작
├── constants.py             (~25)  공유 상수 (timeout, observation 템플릿, INTERRUPT_NOTICE). 외부 모듈 의존 없음 — 저층 레이어. wire-format-specific 상수 (FORMAT_RULES, RETRY_HINT_*, SYSTEM_USER_PREFIXES) 는 ``wire_formats/`` 의 plugin이 소유
├── wire_formats/                   Wire format 플러그인 시스템 — 모델 응답 형식 추상화
│   ├── __init__.py          (138)  Registry (`register` / `get(name=None)` / `list_names`) + `all_system_user_prefixes()` (format-agnostic + plugin prefix 통합 entry point). builtin plugin (react, md_array) 자동 등록. **`DEFAULT_WIRE_FORMAT = "md_array"`** — 기본 wire format 의 single source of truth: `get(None)`/unspecified fallback, CLI `--response-format` 기본, 새 세션 default 가 모두 여기로 resolve (변경은 1곳). **2026-06-11 prefix_md→md_array 전환**: Phase-2 풀루프 95.2%(=react) + 실전 150턴 형식실패 0.7%(prefix_md 동급) 검증 후. md_array 는 prefix_md 의 기능적 상위집합(단일-op + 멀티-op). prefix_md 는 등록된 선택지로 유지(deprecate 아님 — 검증된 fallback 한 사이클 더). 멀티-op 자발 활용률은 아직 낮음(~0.7%) — 다음 거리는 자발 배칭 유도 프롬프트.
│   ├── base.py              (632)  `WireFormat` ABC + `ParsedAction` dataclass. Plugin 베이스 클래스 — abstract method (format-specific 부분만, plugin이 반드시 구현)와 concrete default (lifecycle / 식별 hook, 보통 그대로 상속) 분리. **멀티-op 추상화 (additive, 멀티-op wire format 1단계 — docs/inputs-array-schema/DESIGN.md)**: `Op`(action+action_input+truncated 1개) + `ParsedTurn`(thought + ops 리스트 + terminal + parse_stage) dataclass, 그리고 concrete **`parse_turn(text)->ParsedTurn`** = 기본적으로 기존 `parse()`를 감싸 단수 action을 1-op turn으로 매핑(action 없어도 action_input 있으면 Op 보존 → infer 복구 전제 유지; terminal 항상 False — 단수 포맷은 `complete` op로 종료). **단수 포맷(react)은 무변경**, 멀티-op 포맷만 `parse_turn` override. 루프는 아직 `parse()`를 쓰므로 현재 inert(동작 0 변화); 루프의 `parse_turn` 전환은 후속 단계. **`is_degenerate(text)`** (default False): emission 이 wire shape 을 반복(format runaway)했는지 — 두 용도: (1) loop 이 `provider.call(degeneration_check=...)` 로 넘겨 **streaming 중 조기 break**(토큰 절약), (2) loop 이 최종 emission 을 `FAILURE_DEGENERATE` 로 라벨·raw 캡처. runaway 가능 shape 만 override(prefix_md). **`sanitize_thought(thought)`** (default identity): 모델이 thought 에 흘린 wire sentinel(줄단독 `## 헤더`)을 제거 — raw 가 prior 로 재주입되면 `## Thought … ## Thought` 중복이 self-reinforcement→mimicry→runaway 의 **근본 원인**이라 save-time 에 두 곳에서 정제: `parse`(structured thought) + `serialize_assistant_for_history`(bare content, action 무효 turn). 정제된 record 가 history→prior(render)+화면 일괄로 흐름. react 는 thought 가 JSON string(이스케이프)이라 무관(identity 상속). Abstract: render_full_example / format_rules_anchor / format_rules_field_specific / parse / 6개 recovery wording / system_user_prefixes. Default: format_rules = `build_format_rules(self)`, render_action_input = dict→JSON via json.dumps (wire가 직렬화 — 호출자는 dict만 전달, JSON 가정은 이 hook 한 곳에; render_full_example/history round-trip도 이 hook 경유), provider_call_kwargs = `{}`, prefill = `""`, serialize_assistant_for_history = `self.parse()` + 구조화 필드 추출(+ bare content `sanitize_thought` save-time 정제), render_assistant_from_history = `self.render_full_example()` 호출로 wire shape 재방출 (live + resume prior 둘 다 이 경로 — `normalize_assistant_for_messages` 는 제거됨, 매 턴 prior 가 record 에서 render 됨). **대칭 플래그 `thought_required`/`action_required`** (기본 True): 각각 thought/action 누락 시 **recovery(LLM 재발화) vs 관용·infer** 를 게이트 — loop(복구 측)과 프롬프트(`_gated_rule`)가 같은 플래그를 읽음. **프롬프트 게이트 플래그 `multi_op`/`exposes_complete`** (기본 False/True): multi_op=한 턴 여러 op — 프롬프트 레이어(registry `get_tool_descriptions` + system_prompt 인라인 빌더)가 per-tool 배치 prose 생략·prefix strip; exposes_complete=False — `complete` 를 도구 목록에서 제외(thought-only terminal 로 종료하는 포맷용). **`_gated_rule(required, strong, soft=None)`** = Format-Rules clause 강도를 플래그로 약화/생략하는 hook (현재 soft 미제공이라 inert — 출력 불변, 미래에 soft 채우면 plugin·loop 무변경으로 완화). **parse 계약**: action 슬롯이 무효/없음이어도 식별된 action_input 은 **보존**한다 (infer_action 복구 / NO_ACTION echo 의 전제 — 드롭한 action 을 wire-key prefix 로 복원하려면 파서가 input 을 남겨야 함). 모듈 docstring에 assistant turn lifecycle (A → B → C; render 가 live+resume prior 둘 다 빌드) 표 포함. **`diagnose_syntax_error(prior_content)->str|None`** (concrete default `None`): NO_JSON 회복 시 JSON 이 *어디서* 깨졌는지(line/col + 캐럿) 짚는 opt-in seam — base 는 None(JSON 없는 포맷·미구현 포맷은 generic 힌트 유지), JSON 포맷이 자기 후보 추출 후 `_json_diag.describe_json_error` 에 위임. **`serialize_terminal_for_history(thought, result)`** (concrete default 단수 `{action:complete, action_input}`): loop 의 complete 핸들러가 (nested-envelope 언랩된) result 를 들고 있어 `serialize_assistant_for_history`(raw 입력) 를 못 타므로, terminal turn 을 **이 포맷이 다른 op 와 같은 모양으로** 기록하는 병렬 진입점 — 멀티-op 포맷(md_array·react)은 `{ops:[{complete}]}` 로 override 해 history 동질성 유지(과거 complete 핸들러가 직접 단수 dict 를 `ctx.add` 해 73개 op 와 다른 모양으로 새던 불일치 수리; render·summary 는 양쪽 관용이라 무해했으나 shape 읽는 외부 도구를 속임). plugin 추가 = WireFormat 상속한 새 파일 1개, main code 0 변경.
│   ├── react.py             (882)  ReActFormat — **multi-op JSON wire format (md_array 의 JSON 쌍둥이 — 엔벨로프만 다름; wire-format consolidation roadmap Step 2, 2026-06-13)**. 셰이프: `{"thought": ..., "actions": [{"action": tool, ...flat params}, ...]}` — 한 턴 여러 op, op 셰이프는 md_array 와 **바이트 동일**(cross-format parity 테스트로 고정, 코드는 self-contained 복제). **`parse_turn` override (가산·backward-compat)**: `actions` 배열 → N-op ParsedTurn(`_ops_from_items`); 없으면 classic 단일-op `{thought, action, action_input}` 를 `super().parse_turn()` 으로 1-op 처리 — classic ReAct 는 가장 많이 학습된 prior 라 수용=견고성(기존 233 react JSON 테스트 무수정 통과). **`multi_op=True`** → `_multi_op_flat_params`(프롬프트 flat 노출) + `wrap_single_op`(dispatch flat→canonical) 공유 게이트 자동 합류(md_array 와 같은 경로). 자체 multi-op `_FORMAT_RULES`(공유 `build_format_rules` 의 "ONE action per turn" 단일-op tail 회피), `render_action_input`(prefixed dict→flat op `{action, ...}`)·`render_full_example`(`{thought, actions:[op]}`)·`serialize/render_assistant`(history `{role,thought,ops}` 저장→JSON 재방출) 모두 override(self-contained). **json_mode 유지** — JSON object 라 `json_object` 모드 호환(structured-output 모델용; md_array 는 markdown 이라 json_mode off). 완료=명시 `complete` op(rfr-게이트 문구 없음, md_array 와 수렴). recovery wording + 3-stage fallback parser (`parse_react`) + stage-2 JSON repair helper (`repair_json`) 모두 self-contained. **`diagnose_syntax_error` override**: `strip_markdown_fences`+`_extract_json_block` 로 JSON 후보 뽑아 `describe_json_error` 위임(NO_JSON 힌트에 line/col+캐럿). **`_fix_missing_brackets` 는 `_json_repair.close_unbalanced` 의 thin alias**(repair_json 호출처 보존, 괄호-밸런서는 md_array 와 공유·중복 0). **stage-2a lenient parse (`_try_json_parse(strict=False)`, md_array 와 동일 class — 1781213377)**: 모델이 대용량 `content`/`result` 를 `\n` 이스케이프 없이 리터럴 개행/탭으로 내면 strict `json.loads` 가 거부 → 예전엔 stage-3 regex 로 떨어져 action_input 이 dict 가 아닌 raw 문자열(도구가 못 씀)이 됨. stage 1(strict) 실패 후 `strict=False` 재파스를 stage-2a 로 시도해 dict 복구(repair_json 보다 먼저). WireFormat ABC 상속해 lifecycle default 사용 — format-specific 메서드만 정의. **`thought_required=False`·`action_required=False`**: `_normalize_action_input` 이 action 없어도 non-reserved sibling 을 action_input 으로 bundle 해 dropped-action 을 infer 로 복구 (Layer 1 virtual-tool alias 는 action 있을 때만). (이전 EnvelopeFormat은 2026-05-10 측정 후 폐기 — Phase 1 bakeoff에서 mistral 0% / qwen thought 9.5%로 wire-shape 결정성 약점 확인)
│   ├── _json_diag.py        (76)   **JSON 구문 진단 (recovery 표면)** — `describe_json_error(text)`: `json.loads(strict=False)` 의 `JSONDecodeError`(msg/lineno/colno/pos)를 `메시지 (line L, column C)` + 오류 문자 아래 캐럿 스니펫으로 렌더. 순수 JSON-레이어 유틸(react/md_array 모름) — wire-format *행위* 가 아니라 `json.loads` 류라 WireFormat base 가 아닌 sibling 모듈에 둠(캐럿 포매터 2벌 복제 회피, "base 공유 금지" 규칙 무관). `{`/`[` 로 시작하는 **실제 JSON 시도** 일 때만 진단(프로즈는 None → 기존 generic 힌트 유지). 후보 추출(어느 부분이 JSON인지)은 포맷별 `diagnose_syntax_error` 가 담당. `repair_json`+strict=False 폴백이 모두 실패해 NO_JSON 까지 떨어진 잔여 케이스에만 발화.
│   ├── _json_repair.py      (144)  **JSON 구조 수리 (순수 string→string, bail-if-invalid)** — 둘 다 호출처가 재파싱해 valid 일 때만 채택(아니면 None→진단+재시도, 가짜 op 강제 안 함). `_json_diag` 와 동성격(포맷 모르는 순수 JSON 유틸 → base 아닌 sibling). **(1) `close_unbalanced(text)`**: 문자열-인식 깊이 스택으로 EOF 미닫힘 `]`/`}` 만 **추가**. 실전 NO_JSON 지배 shape(세션 1781336790: 6-op 배치 `]` 누락, 캡처 3/3). react `_fix_missing_brackets` 도 수렴. **(2) `repair_value_quotes(text)`**: 문자열 값/키가 **따옴표 하나(앞 OR 뒤) 누락** — `"path": mgt.c"`(여는 따옴표 누락→`Expecting value`) / `"path": "mgt.c}`(닫는 따옴표 누락→`Unterminated string`). 파서 에러-위치 가이드 + bounded loop(한 페이로드 여러 개 수리). **명확한 신호일 때만 발화**: stray 따옴표(없으면 bare `true`/`42` 안 건드림) 또는 EOF 전 구분자 있는 미종료 문자열(EOF까지 미종료=진짜 truncation→bail). close_unbalanced 와 합성(따옴표+미닫힘 동시 복구).
│   └── md_array.py          (617)  **MdArrayFormat (기본 wire format — 멀티-op, docs/inputs-array-schema/DESIGN.md)** — 마크다운 envelope(`## Thought`/`## Action`) + `## Action` 본문 = flat `{action, ...params}` op 들의 JSON 배열. 한 턴 여러 INDEPENDENT op(배열 원소), plain 키(no prefix), **op 하나=대상 하나(per-tool 배치 금지 — 배치 중첩이 27B 90% 깨뜨린 실측)**. bare 객체=1-op 관용. **`_repair_anonymous_op_objects`/`_extract_op_json` (DESIGN Exp 8 — 실전 1781208482 5/6 실패)**: 27B 가 대용량 param op 배치 시 `{"action":X, {params}}`(params 를 익명 중첩 객체로 = invalid JSON)를 내면 strict 파싱 실패 후 **익명 `{` 를 제거해 복구** → parse_stage 2(drift). 두 shape: A `{"action":X, {params}}`(균형 `}}`, 27B)·B `{"action":X, {params}`(닫는 `}` 하나, 35B — array 에 N개 `{` 불균형) — `_extract_op_json` 가 양쪽 시도해 valid 채택. 컨텍스트+문자열 인식 단일 패스(키-자리 `{`만 unwrap; array 원소·`:`-값·문자열 내 brace 무시 — C코드 content 안전). **strict=False 폴백 (실전 1781213377 `complete` 거부)**: 모델이 대용량 `result`/`content` 마크다운을 `\n` 이스케이프 없이 **리터럴 개행/탭(control char)** 으로 내면 strict `json.loads` 가 "Invalid control character" 로 거부(echo/터미널엔 `\n`과 리터럴 개행이 동일 렌더라 안 보임) — `_extract_op_json` 의 **마지막 폴백으로 `_extract_first_json(strict=False)` 재파스**해 구제(parse_stage 2). brace 스캐너는 이미 문자열-인식이라 무관, `json.loads` 의 strict 만 완화 → valid/escaped JSON 은 stage 1 그대로, control char 만 stage 2 로 복구(신호 유지). strict=False 는 control char 만 허용 — 진짜 깨진 JSON(값 누락 등)은 여전히 None(가짜 op 강제 안 함). 헤더-없는 경로·`## Action` 본문 둘 다 적용. **미닫힘 괄호 EOF 닫기 (실전 NO_JSON 지배 shape — 세션 1781336790, 캡처 3/3)**: 모델이 멀티-op 배열을 완결해 놓고 닫는 `]` 만 빠뜨리면 `_extract_first_json` 이 depth 0 복귀 못 해 None → 최후 폴백으로 `_json_repair.close_unbalanced`(문자열-인식 깊이 스택)로 EOF에 닫고 strict→strict=False 재파싱, valid 면 채택(parse_stage 2)·6 op 전부 보존. truncation(미종료 문자열 등 더 깊은 깨짐)은 괄호만 닫아선 파싱 실패 → None 유지(진단+재시도). react `repair_json` 재사용 안 함(그건 첫 `{}`만 잡아 5/6 op 유실 — 배열-인식 닫기가 정답). **따옴표 하나 누락 수리(`repair_value_quotes`)**: `"path": mgt.c"`(앞)·`"path": "mgt.c}`(뒤) 같이 문자열 따옴표 한쪽이 빠진 경우 `_extract_op_json` 최후 폴백에서 에러-위치 가이드로 복구(close_unbalanced 와 합성, bail-if-invalid; bare keyword·EOF-truncation 은 안 건드림). **종료=명시적 `complete` op** (`{"action":"complete","result":...}`, 검증된 prefix_md/react 모델). `multi_op=True`/`exposes_complete=True`/`thought_required=False`/`action_required=False` — prefix_md 패리티 + multi_op. thought-only/빈/no-op 턴은 **완료가 아니라** 0 ops → loop NO_ACTION 넌지(complete 부르거나 op emit). **`parse_turn` override**가 본 경로(ParsedTurn: N ops, terminal 항상 False); `## Input` 잔재 strip·bare-object=1op·actionless-op 보존(infer)·헤더없는 op JSON=work 유지. **`## Thought` 헤더 누락 보정(`_split_sections`)**: `## Action` 은 있는데 `## Thought` 헤더가 없으면 그 앞 prose 를 thought 로 회수(예전엔 drop→None) — 헤더 빠뜨린 reasoning·오타 헤더 구제. `parse` 는 1st-op 단수 투영(ABC). **history 직렬화 override**: ops 레코드 `{role, thought, ops:[{action, action_input}]}` — render 가 wire 모양 재방출(round-trip). **`serialize_terminal_for_history` override** 로 complete 턴도 `{ops:[{complete}]}` 동질 모양(과거 complete 만 단수 `{action}` 으로 새던 불일치 수리). sanitize_thought 는 prefix_md 동형 + **외톨이 thinking 태그 strip(`_ORPHAN_THINK_TAG`)**: thinking 학습 모델이 visible thought 에 흘린 짝없는 `</thinking>`(여는 태그는 reasoning 채널이 소비; 세션 1782027249 NO_JSON 동반 지배)를 save-time 에 제거 — 파싱은 `## Action` JSON 따로라 무영향, prior 재주입·렌더 cosmetic 청소. md_array origin 한정(react 미적용 — 발생 origin 에만). is_degenerate 는 prefix_md 동형. **`diagnose_syntax_error` override**: `_split_sections` 로 `## Action` 본문(op 배열) 추출 후 `describe_json_error` 위임(헤더 없으면 전체 텍스트 — 미닫힘 배열도 위치 표시). **2026-06-11 기본 전환** (Phase-2 95.2%=react + 실전 150턴 0.7% 검증) — `DEFAULT_WIRE_FORMAT`. **format_rules 능동 배치 유도(B, DESIGN §6)**: 독립 op 를 한 턴에 묶도록 결정 휴리스틱+3-op read 예시로 steering — 의존-분리·중첩 금지 두 가드레일 동등 비중 유지. read_file 인라인 가이드에도 same-turn 배치 힌트. **종료 모델 변경 (DESIGN Exp 8)**: 원래 thought-only 종료 + ready_for_review 게이트였으나 마무리 버그 class(false-terminate, NO_JSON 종료-전환, 빈 `[]`, 리뷰지시문 불일치로 deliverable 폐기)를 누적 → `complete` 부활로 origin 수리 + lenient-terminal·게이트(`_finish_terminal_turn`/`_terminal_reviewed`) 제거. ready_for_review 도구는 이후 v4.4.0 에서 제거(사용률 0).
├── recovery/                       Robust Harness Recovery Layer (docs/robust-harness/DESIGN.md)
│   ├── __init__.py                 primitive·detector·observability 재export (common_recovery / wf_recovery는 호출처가 import — 패키지 자체 format-agnostic 보존)
│   ├── common_recovery.py   (~65)  WF-agnostic Intervention factory — `format_action_loop_intervention` (B1). 모든 plugin이 같은 텍스트를 봄. 새 wire-format plugin 추가 시 0 변경
│   ├── wf_recovery.py       (~108) WF-aware Intervention factory — `format_no_json_retry` (A1a), `format_no_action_retry` (A3). plugin의 framing/reminder/static fallback 사용. WF 의존이 한 파일에 모여 audit 용이. **`format_no_json_retry(syntax_error=…)`**: 옵셔널 구문 진단(`diagnose_syntax_error` 결과)을 framing 다음 줄에 끼워 *어디서* 깨졌는지 노출 + primitives 에 `diagnose_json_error` 추가. 미지정(기본 None)이면 메시지·primitives bit-for-bit 불변(기존 호출/테스트 보존). ReAct-only NO_THOUGHT recovery는 `ReActFormat.format_no_thought_retry` 메서드 (plugin = boundary)
│   ├── detectors.py         (~250) 감지기 모음. stateful: `ActionLoopDetector` (B1, turn 간 (action, args) 추적). stateless: `detect_unknown_tool` (A4), `detect_schema_mismatch` (A5, `validate_tool_input` wrap), `detect_nested_envelope` (A6, complete 결과의 이중 래핑 감지 — 관찰 전용), `detect_thought_missing` (A7, action 있고 thought 없음 — mimicry-strengthening loop trigger; loop이 `wire_format.thought_required` 가드 후 호출. `complete` 액션은 제외 — 최종 답이라 next-turn 의무 없음, Phase 2 bakeoff 2026-05-18에서 27b prefix_md complete_direct 5/5 recovery loop 해소 측정).
│   ├── intervention.py      (~30)  `Intervention` dataclass — primitive 합성 결과 (message + 적용된 primitive 이름)
│   ├── observability.py     (207)  `TurnRecorder` — 세션별 `turns.jsonl` 추가-only writer; `TurnRecord` 스키마(model, timestamp, parse_stage, failure_signal, primitives_applied — timestamp 가 row 정렬·`raw_failures.jsonl` join 키; 구 `seq` 는 run-local 충돌로 제거). FAILURE_* 라벨 9종 (NO_JSON / NO_OUTPUT / NO_ACTION / NO_THOUGHT / UNKNOWN_TOOL / SCHEMA_MISMATCH / NESTED_ENVELOPE / ACTION_LOOP / **DEGENERATE**=wire shape 반복 runaway, 라벨+raw 캡처만·dispatch 진행). 디버그 시 `record_raw=True`(env `AGENT_CLI_RECORD_RAW_FAILURES`)면 실패 턴의 raw LLM 응답을 별도 `raw_failures.jsonl`에 캡처 (turns.jsonl 의 메타-only 계약은 불변)
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
├── loop.py                  (~2633) AgentLoop 클래스 + 에이전트 루프 (wire_format plugin 통합 — parse_turn / system prompt / recovery builders / NO_THOUGHT 가드 / messages 버퍼·history.jsonl 저장의 assistant 표현, token-budget compaction + FIFO fallback, hook, streaming, nested depth rendering, failure-grounding retry). **과대 출력 캡 (`_tool_observation`, `_render_oversized_nudge`, `_oversized_cap=context_window//10`)**: 도구 결과→관찰 seam 에서 `Tool.render_observation`(본문 렌더)+`Tool.apply_oversized_cap`(기본 True)을 consult, cap 초과면 좁히라 nudge 로 치환(messages·ctx 양쪽 전에 1회 — 일관). 1508/1124 두 디스패치 지점이 이 헬퍼 경유. **시스템 프롬프트 단일 소스**: `_system_sections`(이름 붙은 섹션 리스트)가 진실이고 `self.system`은 항상 join 파생 — hook 섹션 적용(`_apply_system_sections`, `Hook: <title>` 항목으로 교체-적용) 후에도 Inspector 뷰와 LLM 수신 문자열이 구조적으로 일치. `_call_llm`이 매 턴 `render_system_prompt_snapshot(build_inspector_sections(self._system_sections, self.ctx), turn)` 으로 renderer 에 전달(CLI no-op, web 은 저장만). **`build_inspector_sections`** 는 system 섹션 뒤에 compaction 주입 컨텍스트(`ctx.summary`·`ctx.file_list`)를 "⊙ Compaction summary / Files touched (user-injected)" 라벨 섹션으로 덧붙임 — 이들은 `get_messages()`가 시스템 프롬프트 직후 `role=user` 로 주입하는 내용이라 `self.system`(=`_system_sections` join) 에는 없지만 컨텍스트 윈도우를 점유하므로 Inspector 가 가시화. **새 list 반환 — `_system_sections` 불변**(self.system 파생원 보호). **통일 turn 디스패치 (멀티-op 3a — docs/inputs-array-schema/DESIGN.md §6)**: `_handle_text_path` 가 `wire_format.parse_turn()`(ParsedTurn) 기반 — per-op `infer_action`, turn-level 라벨링(terminal 은 실패 아님). **턴경계 메시지 주입(web 멀티유저 — 단일 라우팅)**: `run()` while 상단에서 `_inject_queued_messages()`가 `dequeue_user_message` 로 큐된 user 메시지 1개를 꺼내 user 카드 echo 후 — **run-starter 와 동일하게** `route_message(text)` 콜백으로 라우팅: 명령(`/sh`·`/compact`·`@agent`·`/skill`)이면 실행(결과는 공유 ctx 반영; `@agent` 는 모델 `delegate` 와 동일한 `tool_delegate` 기계장치에 수렴), 명령이 아니면 `_add_user_message(text, author)` 로 스티어링 주입. `_add_user_message` 는 setup(run-starter)과 **공유하는 단일 헬퍼** — `[author]: text` 라벨 + `task_log` 누적 + ctx.add. CLI 는 두 콜백 None=무동작. (`query_author`=run-starter 닉네임; 과거 `query_label` 별도 인자·라벨링 중복·injected-무라우팅 비대칭은 제거 — 설계 `docs/intake-unification/DESIGN.md`. 이전엔 중간 주입 `/sh`·`@agent` 가 라우팅 없이 리터럴 chat 으로 샜음.) `restate_task`(B1)·`_build_review_observation`(auto-review 컨텍스트)은 `self.query` 대신 **`_task_text()`(첫 쿼리+주입 전체)** 인용. 디스패치는 `_dispatch_turn`(turn 가드: NO_THOUGHT → thought 렌더 → no-ops → ops 배열 순회) → `_dispatch_op`(per-op: complete/ask/run_skill/B1/A4/A5/tool 실행 — 기존 단수 본문 그대로, 모든 분기가 return 이라 1-op 에서 종전과 동일 동작) → `_recover_unparsed`(NO_ACTION/NO_JSON 공용 recovery, no-ops 와 action-없는-op fall-through 두 곳에서 호출). **종료는 명시적 `complete` op**(`_dispatch_op` 의 complete 분기, result=최종 답변) — md_array 도 동일(prefix_md/react 모델). thought-only/0-op 턴은 `not turn.ops` → `_recover_unparsed`(NO_ACTION 넌지), 완료 아님. (DESIGN Exp 8: 원래 thought-only 종료 + `_finish_terminal_turn`/`_terminal_reviewed` ready_for_review 게이트였으나 마무리 버그 class 누적으로 제거 — complete 부활로 origin 수리; ready_for_review 도구는 v4.4.0 에서 제거.) B1 loop detector 는 N-op 에서도 per-op observe — 같은 (action,args) 3연속(턴 경계 무관)이면 발화, 한 턴 내 2중복은 무발화(threshold 3 의미론 그대로). **N-op 실행 (3b)**: 1-op 은 legacy 경로(자체 observation append — 종전과 동일), N-op 은 순차 실행+축적 → `_flush_op_results` 가 per-op `[i/N] tool — OK/FAILED` 헤더의 **합성 observation 1개** append (any-fail ⇒ success=False, 모델이 실패 op 재시도; 합본 `tool` 라벨은 `_combined_tool_label` 가 연속 동-도구를 `tool×N` 으로 run-length 압축 — `shell+write_file×12`, 줄넘침 방지·순서 보존). 턴-종료성 op(complete/run_skill)는 분기 전에 축적분 flush(시간순 보존); 가드(B1/A4/A5) 발화 시엔 intervention 후 flush. **`ask` 는 턴-종료가 아님 — 사용자 응답을 observation 으로 내고 일반 도구처럼 accumulate** 하므로 ask op 여러 개가 read/shell 배치처럼 묶임(각자 순차 프롬프트 → 합성 observation 1개). 단일-op ask 는 자기 observation 직접 append(종전 동일). **병렬 batch (`_dispatch_parallel_batch`, Step 3 delegate)**: 연속된 같은 `Tool.parallel_safe=True` 도구 op 런(≥2)은 순차 대신 **동시 실행**으로 묶임 — 현재 유일한 parallel_safe 도구 delegate 는 각 flat op 입력을 `{tasks:[...]}` 로 조립해 `tool_delegate` 한 번 호출→`_run_parallel`(스레딩). 이게 프롬프트의 "여러 delegate op = 병렬" 약속을 실제로 참으로 만듦(N-op 루프는 본래 순차이므로). lone parallel_safe op(런 길이 1)은 normal per-op 경로(B1/A4/A5 가드 유지). mutating 도구(parallel_safe=False: write/edit/shell)는 항상 순차 — 순서가 정확성 보장(write→edit 같은 파일, mkdir→touch). 내부엔진 없는 미래 read-only parallel_safe 도구용 generic thread-pool 슬롯은 `NotImplementedError`(미배선 — delegate 만 opt-in). **같은 파일 edit 배치 (`_dispatch_edit_batch`, parallel batch 의 형제 경로)**: 연속된 같은-path `edit_file` op 런(≥2)은 `apply_edits_batch(path, edits)`(순수함수) 로 묶여 **원본 1회 read 기준 bottom-up 적용·overlap 사전거부·all-or-nothing** — 뒤 op 의 ref 가 앞 op 의 줄 이동으로 stale 되는 것을 제거(두정 보고 "Hash mismatch at line N"). parallel batch(동시) 와 달리 **순차 의미를 유지하되 단일 base** 라 mutating 이어도 안전. 결과는 누적의 한 unit(`{tool_name, observation, success}`) → `_flush_op_results` 합본. 단방향 호출(loop→edit_file)이라 강결합 없음. 런 길이 1·다른 path·비연속은 per-op. tool-exec 직전 `wire_format.multi_op` 면 `Tool.wrap_single_op` 호출 — **모든 builtin 도구가 flat-native(Step 3)라 wrap=identity**(과거 batch 도구의 flat→캐노니컬 변환은 소멸). **action 카드 렌더는 `op.action_input`(모델 실제 emission)을 표시** — 모든 wrap 이 identity 라 dispatch 입력과 동일하지만, 렌더는 명시적으로 pre-wrap 값을 써서 미래 prefixed/batch 도구가 다시 생겨도 history.jsonl/resume-replay(raw 저장)와 일치 유지. dispatch 는 `tool_input`, 카드는 pre-wrap. 생성 시 `ctx.set_compactor(self._llm_compact_summarize)` + `ctx.set_recorder(self.recorder)`로 compaction 진입점을 ContextManager에 주입; `--no-compaction` / `AGENT_CLI_COMPACTION=off`면 미주입 → FIFO만 동작. **Tool dispatch safety net**: `_dispatch_tool_with_hooks` 가 invoke 단계 (`_invoke_regular` / `_invoke_delegate`) 를 try/except Exception 으로 감싸 unhandled exception 을 `ToolResult(False, error="Tool 'X' raised … retry or different approach")` 로 변환 → post-hooks + observation 정상 흐름, LLM 이 다음 turn 에서 retry 결정 가능. `KeyboardInterrupt` / `SystemExit` 는 의도적으로 통과시켜 Ctrl+C 종료 보장. 전체 traceback 은 `_debug_log` 로 보존, LLM observation 은 짧게 유지. **Output-truncation guard**: `_execute_turn` 가 `response.stop_reason == "length"`(모델 출력 한도 도달) 면 그 응답의 action 을 **dispatch 하지 않고** `_on_output_truncated` 로 `OUTPUT_TRUNCATED_NOTICE` observation 기록 → 잘린 content(write_file)·명령(shell)·답(complete)이 불완전 실행되는 것 방지, 모델이 다음 turn 에 더 작은 단위로 재시도. (이어쓰기 continuation 은 후속.) **Mid-generation interrupt**: `_call_llm` 이 provider 에 `interrupt_check=self._interrupt_check`(= `stop_event.is_set()`) 를 넘겨, Ctrl+C/web stop 이 **생성 스트리밍 도중**이면 provider 가 즉시 stream 을 끊고 `stop_reason="interrupted"` 반환 → `_execute_turn` 가 parse/dispatch **전에** 이를 감지해 `_on_interrupt()` 로 직행(미완성 partial 은 ctx 에 안 들어갔으므로 폐기, interrupt notice 만 기록). 생성이 이미 끝난 뒤(스트림 완료)면 `"interrupted"` 가 아니라 정상 흐름 → 도구는 부작용 보호를 위해 끝까지 실행되고 turn 경계에서 멈춤(graceful "finish current step"). `stop_event` 는 skill(`_handle_run_skill`)·delegate(`tool_delegate`, 병렬 worker 스레드 포함)로 그대로 전파되고 interrupt 로직은 공유 `AgentLoop` 에 있어, 한 번의 interrupt 가 모든 중첩 loop 의 in-flight 생성을 끊음(각 병렬 worker 는 자기 스레드에서 자기 스트림을 닫음). **Unified call-depth ceiling**: `__init__` 가 `depth >= max_depth` 시 `delegate` AND `run_skill` 둘 다 tools_list 에서 제거 (대칭). `execute_skill` 이 `parent_depth + 1` 전달 → skill 체인도 depth 카운트. cycle (`skill_stack` / `agent_stack` 검사) + depth 한계 위반 시 `recovery/recursion.py` 의 actionable helper (3가지 recovery option) 로 응답. dispatch 단계 belt-and-suspenders check 가 직접 caller 도 보호. 시스템 프롬프트 `## Execution Context` 가 `depth N/M` 표시 + 한계 도달 시 명시 (KV cache: section 위치 그대로 — 한 loop 내 depth 불변이라 영향 0).
├── render/                         플러그인 가능 렌더링 + 사용자 입력 시스템
│   ├── __init__.py          (~270) 렌더러 디스패치 + load_renderer_by_name + render crash 방어 + observation success 전달
│   ├── base.py              (~522) Renderer ABC + `ConfirmOption` dataclass. 출력 메서드 19개 (depth, capture, group, thread_status, thinking 등) + 입력 메서드 2개 (`prompt_user` 자유 입력 — optional `context` kwarg로 pre-input 안내(예: ask 도구의 질문 블록)을 전달, `confirm` 선택지+코멘트) + **`can_prompt()` (기본 True)** — "지금 사용자에게 프롬프트(confirm y/n/a 또는 ask 자유입력)를 띄울 수 있나"를 렌더러가 선언; 호출자가 블로킹 전에 확인해 못 띄우면 hang 대신 refuse/기본값. **인터랙티브 읽기 추상화**: 모듈 레벨 `interactive_lock`(RLock, 모든 사용자 읽기 직렬화) + `_prompt_display_guard()`(읽기 중 출력 정리 — 기본 no-op) + `_guarded_read(read)`(락+가드로 감싼 블로킹 읽기). `confirm`·`prompt_user` 둘 다 `_guarded_read`로 읽어 서로 직렬화(한 번에 하나 → 응답 오라우팅 방지) + 표시정책 공유. **프롬프트 출처(provenance)**: thread별 `set_thread_agent`(delegate 라벨)·`note_thought`·`note_action` 기록 → `prompt_meta()`가 `{agent, reasoning, action}` 반환. confirm/ask가 "어느 에이전트가 왜(confirm은 무엇을)" 묻는지 표시. `_format_prompt_meta`는 agent 라벨이 있을 때(=delegate)만 CLI 헤더 생성(메인 에이전트는 thought/action이 인라인이라 중복 회피). 입력도 추상화에 포함해 web UI 같은 비-CLI renderer가 SSE+POST로 같은 인터페이스 만족할 수 있게. **`begin_delegate_task` / `end_delegate_task`** concrete no-op lifecycle 메서드 — CLI 렌더러는 그대로 무시(rich.Live가 자체 처리), WebRenderer만 override해서 thread→task_id 매핑 + SSE 마커 발사. `delegate.py::_run_parallel` 워커는 둘을 무조건 호출 → 렌더러 타입 분기 없음.
│   ├── minimal.py           (~950) MinimalRenderer — 유일한 번들 렌더러. **`token_usage(stats, turn, verbose)`**: 매 turn `in/out(+speed) · ctx: used/window(%) · Σout(누적) · cache` 한 줄 (`_format_token_stats`, K 단위 축약; non-verbose면 `--verbose` 힌트). **출력**: nested depth, markdown, ASCII-art talking-face streaming progress with token counter + 시간 기반 프레임 throttle + 폭 통일 패딩 + 좁은 터미널 안전망 + resize-recovery, ASCII-art thinking spinner, `FrameClock` 공유 (delegate 병렬 패널이 동일 cadence로 reuse), write_file/edit_file unified-diff 렌더링 (plain diff 를 `_colorize_diff_line` 으로 라인 첫 char 별 색상 — diff 데이터 자체는 plain), ToolResult.success 직접 전달로 정확한 ✓/✗ 표시, capture, group blocks, CJK+Ambiguous width, verbose에서 provider thinking 블록 표시. **입력**: `prompt_user`는 multiline 시 `input_history.read_rich_input` (paste + `"""..."""` 블록 지원), 단일 줄은 stdin `input()`; EOF/Ctrl+C는 호출자 정책 분기를 위해 전파. `confirm`은 첫 토큰 매칭 (key + aliases, case-insensitive), EOF/empty/unrecognized는 `default_key` 반환. **`confirm`·`prompt_user` 둘 다 `_guarded_read`** 경유 → `_prompt_display_guard` override가 활성 Live(spinner/parallel-delegate 패널)를 정지 후 읽고 재개 (워커 스레드에서 호출돼도 Rich 리페인트가 프롬프트를 안 덮음; 공유 락이 동시 stdin 읽기를 막아 워커 스레드 읽기도 안전). delegate 프롬프트면 읽기 직전 `_emit_prompt_meta_header`로 `↳ from [agent] · 💭 reasoning · ⚡ action`(ask는 action 생략) 출력. begin/end_delegate_task가 `set_thread_agent`로 라벨 설정/해제. **`can_prompt()` = stdin·stdout 둘 다 TTY** (Live/스레드 상태는 가드가 처리하므로 게이트엔 미반영). 커스텀은 `render/{name}.py`에 Renderer 서브클래스를 두면 `--style {name}`으로 로드됨
│   └── web.py               (1094) WebRenderer — `agent-cli web` 전용. **`note_system_prompt(sections, turn)` override + `prompt_snapshot(scope)` + `prompt_scopes()` + `delete_prompt_scope(scope)`**: 매 LLM 콜의 시스템 프롬프트(이름 붙은 섹션)를 **스코프별 슬롯**(`_prompt_snapshots: dict[scope, snapshot]`)에 저장만(SSE 미발사 — ~16KB는 on-demand) — 섹션별 chars/est_tokens(estimate_tokens 단일 출처) 계산 포함. **스코프는 호출 스레드에서 해소** — `note_system_prompt` 가 `_thread_to_task.get(get_ident())`(`_emit` 과 동일 맵)로 자기가 main(`_MAIN_SCOPE=""`)인지 delegate 서브에이전트(task_id)인지 스스로 판별 → loop 이 identity 를 안 내려줘도 에이전트별 프롬프트가 분리 저장(loop 변경 0). `begin_delegate_task` 가 `_prompt_scope_labels[task_id]={agent,index}` 도 기록(칩 라벨 "explorer·1"). 서브에이전트 스냅샷은 task 종료 후에도 잔존(사후 검사) — 프런트가 `delete_prompt_scope`(✕)로만 제거, main 은 삭제 불가. `prompt_scopes()` 는 스냅샷 있는 스코프만 main-우선 나열(`GET /api/debug/prompt/scopes`); `prompt_snapshot(scope)`/`delete_prompt_scope` 가 `GET`/`DELETE /api/debug/prompt?task_id=` 의 공개 표면. 모든 Renderer emit이 (1) `_event_buffer`에 (persistent만) 누적 + (2) 활성 SSE connection의 queue에 push. `thought()` 는 즉시 emit 안 하고 다음 `action()` / `final()` 에서 `assistant_turn` 한 이벤트로 묶음 (LLM 한 emission = 프런트 카드 한 개). `prompt_user` / `confirm` 은 `input_required` 이벤트 push 후 worker thread에서 `_input_queue.get()` blocking, POST /api/input 이 도착하면 깨움 (emit+wait를 `_guarded_read`의 공유 락 안에서 실행 → 동시 prompt/confirm이 단일 큐에서 섞이지 않음). `input_required` 이벤트에 `agent`/`reasoning`(+confirm은 `action`) 필드 첨부 → 프런트가 어느 delegate 에이전트가 묻는지 표시. begin/end_delegate_task가 `set_thread_agent` 호출. **`can_prompt()` = 활성 connection 존재** (TTY 불필요 — SSE+/api/input 채널이 운반; 클라이언트 미접속이면 False → 위험 셸 명령·ask는 refuse/기본값). `prompt_user(context=...)` 는 ask 도구의 질문 텍스트를 `input_required.context` 필드로 그대로 전달 → 프런트가 ANSWERING 칩 옆 패널로 렌더 (스크롤 없이 질문 즉시 노출). **Sticky state registry (`_sticky` + `set_sticky(name, event, payload, position=)`)**: "단일 서버 값을 라이브 브로드캐스트 + 새 connection snapshot 재생"을 한 표면으로 통합 (옛 `_latest_ready`/`_latest_worker_state`/`_latest_token_usage`/`_latest_queue` 4슬롯 + 반복 if 를 흡수). 멤버: `ready`(세션정보 top-bar, position=**prepend** — buffer 분리로 AgentLoop 재진입 시 누적 방지, 첫 turn 전 새로고침에도 top-bar 즉시) / `worker_state`(`worker_busy`/`worker_idle` send 버튼 게이팅, `_worker_loop` 가 pop 전 idle·후 busy, SHUTDOWN 제외) / `token_usage`(raw 카운트, 프런트 포맷) / `queue`(대기 메시지) / **`auto_review`**(`auto_review_state(enabled)` — 🔍 토글 **모든 브라우저 동기화**: 한 클라이언트가 켜면 broadcast 로 나머지 버튼 갱신 + 재접속 snapshot 복원; 서버 상태 `WebServer._auto_review`, `set_auto_review` 가 이 메서드로 미러; 프런트는 메인 SSE 핸들러가 `document` CustomEvent 로 토글 IIFE 에 중계). `register_connection` 이 `_sticky` 순회로 position 별 prepend/append 재조립(전부 non-persistent — buffer=history, slot=latest). NOT sticky: `viewers`(연결집합 파생)·prompt_snapshots(스코프별 on-demand). `_build_token_stats` 가 `in=usage.total_input_tokens`(전체 점유량 → Anthropic 캐시 적중 시에도 ctx% 정확)·context_window·누적 out 을 render-agnostic dict 로 전달; CLI/web 공통. `in_speed` 만 bare input_tokens(prefill 비캐시분), cache_read/write 별도 내역. 중첩 AgentLoop(`skill_name`/`skill_args` 세팅)에서의 header()는 무시 (sub-flow가 top-bar를 클로버하지 않도록). **다중 뷰어 (모두 동등)**: `register_connection` 은 연결을 `_connections` 에 append + 스냅샷 맨 앞에 `identity` 이벤트(conn_id) prepend — controller/observer 구분 없이 모두 입력·큐 가능. `unregister_connection` 은 `__close__` sentinel push(SSE generator 즉시 깸). **접속자 수(`viewers`)**: join/leave 시 열린 연결 수를 브로드캐스트 — 참여 conn 은 자기 snapshot 으로(큐 오염 방지), 기존 conn 들은 큐로 받음. 프런트 헤더 `#viewers` 에 `👁 N` + 닉네임 로스터. **`queue_state(pending)`**: 대기 메시지 큐를 `queue` 이벤트로 브로드캐스트(+`_latest_queue` slot 으로 재접속 복원). **`nickname_for(conn_id)`**: 큐 메시지 닉네임 attribution. **`set_nickname(conn_id, name)`**: 사용자 지정 닉네임(trim·24자, 빈값 거부) → 로스터 재브로드캐스트. **`shutdown_all_connections()`** — 모든 active connection에 `__close__` sentinel을 일괄 push하고 리스트를 비움; FastAPI lifespan shutdown 훅과 main.py `finally` 양쪽에서 호출되며 idempotent (두 번째 호출은 빈 리스트 위에서 no-op). **`replay_from_history(ctx)`** — `--resume` 시 worker 시작 + SSE 연결 이전에 한 번 호출, `ctx.get_raw_messages()`를 walk해 user/tool 메시지는 `push_user_message` / `observation` 으로, assistant 메시지는 **`ops` 모양**(두 wire format 이 `complete` 포함 모든 턴을 저장하는 형태)을 walk 해 op마다 `thought+action`(complete면 `thought+final`)으로 재방출(`_replay_assistant_op` 헬퍼; thought 는 1회 held → 첫 op 카드에 실림). 레거시 단수 `{action,action_input}` 모양 + raw content-only(final 카드)도 호환. → 새 클라이언트의 snapshot replay가 자연스럽게 이전 turn을 복원 (transient stream_chunk/status/spinner는 on-disk 기록 없음 = 재생 안 함). **(버그픽스)** 과거 단수 모양만 처리해 `ops`-모양 assistant 턴(=실제 저장 형태)을 전부 누락 → resume 시 complete 최종답 포함 assistant 카드 전체가 안 보이던 문제 수정. `__init__(workspace=...)` 로 workspace 경로 받아 ready 이벤트에 포함. **Card timestamps**: `_emit` 가 단일 fan-out 지점에서 모든 이벤트에 server-stamp `ts`(epoch 초, emit 시각) 부착 → 프런트(`stampCard`)가 카드 모서리에 로컬시각(YYMMDD HH:MM:SS, hover=전체 날짜+ms) 표시. delegate/skill 내부 카드도 같은 `_emit` 경유라 자동 커버. **resume 시각 보존**: `replay_from_history` 가 각 history record 의 원본 `ts` 를 `_replay_ts` 에 실어 `_emit` 의 `if 'ts' not in data` 가드가 그대로 통과 → 재생 카드가 resume 시점이 아닌 실제 발생 시각 표시. history `ts` 는 ISO 문자열(`_now_iso`), live 는 epoch — 프런트 `tsToDate` 가 둘 다 수용(레거시 pre-ts record 는 None→wall-clock fallback). **Parallel delegate visibility**: `_thread_to_task` dict + `_emit` 자동 task_id 첨부 + `begin_delegate_task` / `end_delegate_task` / `set_thread_status` override로 worker thread별 SSE 이벤트 라우팅. 프런트는 task_id 보고 collapsible group 카드로 격리 표시 → 두 parallel worker 출력이 인터리브하지 않음. **Recovery lifecycle (`recovery(raw, intervention, reason, turn)`)**: parse/validate 실패 경로가 base default(status+observation)를 override해 `failed_turn`(live streaming 카드를 실패 카드로 finalize — 안 하면 다음 턴 stream이 같은 카드에 누적되던 버그)+`observation`(LLM에 되먹인 intervention) 두 persistent 이벤트 emit.
├── web/                            agent-cli web 서버 + 정적 UI (optional dep, `pip install agent-cli[web]`)
│   ├── __init__.py
│   ├── server.py            (987) FastAPI app. **`WebServer(renderer, token, ctx=None)`** — `ctx`(live ContextManager, worker 공유)를 받아 Prompt Inspector 가 **동적 컨텍스트**(대화+관찰)도 보여줌. **`_dynamic_context_sections(ctx)`** = `ctx.get_messages()`(system 제외)를 시스템 섹션과 **같은 shape** 의 섹션 리스트로 변환(`kind="dynamic"`, 메시지당 1섹션) — 프론트가 동일 아코디언으로 렌더(새 렌더 경로 0). `list(...)` 복사로 worker append 레이스 방어(읽기 전용 디버그 뷰, 락 없음). **첫 LLM 콜 전에도 채움**: 엔드포인트가 시스템 스냅샷 없어도(메인 스코프) ctx 메시지 있으면 동적 섹션만이라도 반환(`ok=False` 게이트 완화) → resume 즉시 대화 표시. **`capture_startup_system_prompt(renderer, capabilities, wire_format, session_dir, max_depth)`** — web 시작 시 `build_system_prompt_sections(active_tools=list(TOOLS.keys()), mcp_manager=None, depth=0)` 로 정적 시스템 프롬프트를 미리 빌드·캡처(첫 메시지 전 인스펙터 채움; `Hook:` 섹션은 PreLLMCall 후라 미포함, 첫 콜이 덮어씀). best-effort. `pick_port(host, preferred)` — `--port` 생략 시 main.py가 호출. preferred(8080) 에 **라이브 리스너 없으면**(`_port_has_live_listener` connect 프로브) bind 후 그대로, 있으면 `bind((host, 0))` 으로 OS ephemeral 할당. **connect 프로브가 핵심**: bind 프로브의 `SO_REUSEADDR`(TIME_WAIT 재시작 친화) 만으로는 macOS/BSD 에서 특정-IP bind 가 다른 프로세스의 `0.0.0.0:port` 리스너와 **조용히 공존**해 false-positive(두 서버가 같은 포트 경합) — `--host <ip>` 새 인스턴스가 이미 도는 `0.0.0.0:8080` 위에 또 8080 을 잡던 버그. connect 는 실제 클라이언트처럼 라이브 리스너(점유)와 TIME_WAIT 잔재(재사용 가능)를 구분. 명시한 `--port N` 은 probe 없이 그대로 uvicorn에 전달 (충돌 시 uvicorn이 에러). `_NoCacheStaticFiles` + `_NO_CACHE_HEADERS` — `/static/*` 와 `/` 응답에 `Cache-Control: no-cache, must-revalidate` 자동 stamp. editable install로 CSS/JS 수정해도 사용자가 hard-refresh(Cmd+Shift+R) 안 해도 서버 재기동만으로 반영됨 — `no-store` 가 아닌 `no-cache` 라 변경 없으면 304 fast path 유지. 엔드포인트: `GET /` (정적 index.html), `GET /static/*` (앱 JS/CSS), `GET /api/health` (auth 없음), `GET /api/debug/prompt?task_id=` (토큰 인증 — Prompt Inspector: `task_id` 로 delegate 서브에이전트 스코프 선택, 생략 시 main loop. 해당 스코프 최신 시스템 프롬프트 스냅샷(`kind="system"`)을 섹션·사이즈와 함께 반환; **메인 스코프면 `_dynamic_context_sections(server.ctx)`(`kind="dynamic"`)를 덧붙여 동적 컨텍스트도 포함**(서브에이전트 ctx 는 미도달이라 system-only). total_chars/est_tokens 는 합쳐서 재계산. 시스템 스냅샷이 없어도 메인 스코프 ctx 에 메시지가 있으면 동적만이라도 반환(resume); 시스템·동적 둘 다 비면 ok=False)·`GET /api/debug/prompt/scopes` (스냅샷 있는 스코프 목록 — main + 서브에이전트, 칩 라벨용)·`DELETE /api/debug/prompt?task_id=` (서브에이전트 스냅샷 제거 ✕; main 불가), **`GET /api/export/jira/targets`** (토큰 인증 — 설정된 Jira 인스턴스 name+base_url+deployment; 자격증명 없음. config 에 deployment 미지정 시 `detect_deployment` 프로브로 채움; export 드롭다운·필드 선택용)·**`POST /api/export/html`** (토큰 인증 — 선택 entry들을 self-contained HTML attachment 로; 읽기전용이라 controller 게이트 없음)·**`POST /api/export/jira`** (토큰 인증 — `{target?, base_url?, issue_key, deployment?, entries, auth:{user,secret}}` → `jira.resolve_target`[body base_url 우선, config 미일치 URL 은 `http`/`https` 허용(그 외 scheme 거부), base_url 없으면 config 해석] + deployment 결정[body→config→probe→cloud] → cloud=ADF/server=wiki 변환 → 사용자 자격증명으로 `post_comment`. config 없이도 동작(zero-config). 자격증명 누락·잘못된 scheme URL·config/issue 오류 400. 자격증명은 로그·세션에 안 남김), **`GET /api/workspace/tree?path=`** (토큰 인증 — 워크스페이스(서버 cwd) 한 레벨 디렉토리 목록: dirs-first, `{name, rel, type, size}`; 디렉토리도 rglob 합산 size; lazy 트리 펼침용)·**`POST /api/workspace/download`** (토큰 인증 — `{paths:[rel...], all?}` → 선택 경로를 임시 zip 압축[dir 재귀·file 단일·중복 dedup]→`FileResponse` + `BackgroundTask(os.unlink)` 로 전송 후 삭제)·**`POST /api/workspace/upload?name=&path=`** (토큰 인증 — body=raw 파일 바이트, multipart 의존성 0. `name`=대상 `path` 기준 상대경로[단일파일 `a.txt` 또는 디렉토리 업로드 `mydir/sub/a.c`]; 세그먼트별 `..`/빈/절대/백슬래시 거부+`_safe_workspace_path` 최종 재검증, 중간 디렉토리 자동 mkdir. 대상 `path` 는 기존 dir, `_MAX_UPLOAD_BYTES`=50MB 초과 413, 덮어쓰기 허용+`{name,rel,size,overwritten}` 보고. WRITE 라 download 보다 가드 강함. 프런트 📁 통합 드로어=파일/폴더 드래그-드롭[`webkitGetAsEntry` 재귀 walk] 또는 파일/폴더 선택[`webkitdirectory`/`webkitRelativePath`]→파일별 1요청, 트리에서 클릭한 폴더로 업로드)·**`POST /api/workspace/delete`** (토큰 인증 — `{paths:[rel...]}` → 파일 unlink·디렉토리 `shutil.rmtree` 재귀 삭제. **WRITE+DESTRUCTIVE 라 가드 최강**: under-workspace+traversal 거부 + **워크스페이스 루트 자체 삭제 거부**, per-path 오류는 `{deleted, errors}` 로 보고(한 경로 실패가 나머지 중단 X), 빈 목록 400. 프런트 dl-foot 의 `🗑 Delete` 가 체크 선택분을 `confirm()` 후 삭제→트리 갱신). 트리 `size`=파일 stat·디렉토리 rglob 재귀합산, 프런트 루트 행은 최상위 entries 합으로 **워크스페이스 총 크기** 표시. 세 엔드포인트 모두 `_safe_workspace_path(rel)` 로 workspace(=`Path.cwd()` 시작 시 resolve, `self.workspace`) 하위만 허용 — traversal/심볼릭 escape 차단. `GET /api/stream` (SSE, 토큰 인증, 다중 뷰어), `POST /api/input` (chat/prompt/confirm 통합 — 모든 연결 입력 가능; **chat 은 즉시 echo 없이 큐에 enqueue** → 디큐 시점에 카드 렌더), `POST /api/queue/cancel`(`{conn_id, id}` — 소유자·미디큐만 취소), `POST /api/nickname`(`{conn_id, name}` — 사용자 닉네임 설정, trim+24자), `POST /api/abort` (`prompt_user`/`confirm` 인터럽트), `POST /api/stop` (진행 중 chat/skill turn 중단 — `trigger_stop` → worker `stop_event`). **`set_stop_handle(event)` / `trigger_stop()`** — worker 가 turn 마다 등록한 `stop_event` 를 `/api/stop` 이 set; lock 으로 worker·request thread 간 보호, 미등록이면 `trigger_stop` 이 False 반환. **다중 뷰어 (모두 동등)**: 모든 인증 연결이 스트림을 받고 모두 입력 가능(controller/observer 구분 삭제). 각 연결은 `identity` 이벤트로 conn_id 취득(로스터 "(you)"·큐 소유). takeover 없음. **메시지 큐**: `_pending`(deque+Condition) — `enqueue`(누구나)·`dequeue_blocking`(worker idle, pending 우선 후 SHUTDOWN)·`dequeue_nowait`(loop 턴경계 주입)·`cancel_pending`(소유자·미디큐만)·`queue_snapshot`. 변경마다 `renderer.queue_state`로 `queue` 이벤트 브로드캐스트. 토큰은 `secrets.compare_digest` 상수시간 비교. `stream_events` async generator가 snapshot replay → live loop 순서로 yield. **`handle_slash_command(message, renderer, ctx=None)`** — 웹 명령어: `/help`, `/sh <cmd>`, `/compact`(`ctx.compact_now()` → before/after `observation` 카드; ctx 없으면 unavailable). **`WebDispatchOutput`** — `main.try_dispatch_agent_or_skill` 에 넘기는 `DispatchOutput` 어댑터: `/skills`/`@agents` 리스트, `@<name> <task>`/`/<skill> <args>` invocation, not-found 에러를 전부 `observation` 이벤트로 변환. `run` 과 dispatcher 공유. **`SHUTDOWN` sentinel + `shutdown()` 메서드** — `_pending` 큐의 shutdown 플래그를 set + condition notify 해 worker thread의 blocking `dequeue_blocking()`을 깨움(pending 은 먼저 drain); worker는 `item is server.SHUTDOWN` 분기로 루프를 빠져나간다. **lifespan shutdown 훅** (`@asynccontextmanager async def _lifespan`) — uvicorn SIGINT 경로에서 `server.renderer.shutdown_all_connections()` 호출 → sse-starlette ping coroutine이 CancelledError 트레이스 없이 조용히 종료; main.py finally 블록과 idempotent하게 페어링. **`suppress_incomplete_response_log()`** — Ctrl+C 시 SSE 클라이언트가 연결돼 있으면 sse-starlette가 `_stream_response` task를 final body chunk 전에 cancel해 uvicorn이 "ASGI callable returned without completing response"를 logger.error로 남긴다. 세션은 정상 finalize되는 cosmetic noise이고 shutdown 시에만 발생(정상 운영 중엔 없음)하므로, `uvicorn.error` 로거에 그 메시지만 거르는 `_IncompleteResponseLogFilter`를 main.py `web()`가 idempotent하게 부착.
│   └── static/                     Vanilla JS 프런트엔드 (의존성 0)
│       ├── index.html       (127)  단일 HTML 셸 — `<head>` 의 인라인 테마 스크립트(첫 페인트 전 `data-theme` 설정 = localStorage `agentcli_theme` 의 테마 id, 미지정 시 amber[시스템 light 선호면 light] — FOUC 방지), header(🎨 테마 피커[`#theme-wrap` > 버튼 + `#theme-menu` 드롭다운] / ⚡ Inspector / 📤 Export / 📁 Files 버튼 — 공통 borderless-emoji 스타일 + 접속자 로스터 옆 ✎ `#rename-btn` 닉네임 재설정 버튼[기본 hidden, 로스터 합류 시 노출]) / name-bar(첫 접속·✎ 공용 닉네임 입력바) / messages / export-bar(선택 모드 액션바 — Jira 폼에 `#export-jira-http-warn` 평문 경고 span 포함) / download-drawer(우측: 파일트리·count·⬇ zip + 업로드 드롭존) / footer + textarea. JS가 URL ``?token=…``에서 토큰 추출, SSE 연결.
│       ├── app.js           (2421) SSE 이벤트 디스패치 + DOM 렌더링. event_buffer (snapshot) replay → live. 카드 종류: user_message (우측 파란 bubble), assistant_turn (thought + final OR action), observation (✓/✗ + tool_name), error, streaming (점선, 토큰 누적). prune 이벤트 시 가장 오래된 N개 카드 DOM에서 제거. input mode 3개 (chat / prompt / confirm). confirm 모드는 ConfirmOption.label 버튼 + 코멘트 텍스트. confirm/ask에 `input_required.agent/reasoning/action` 가 있으면 `buildPromptMetaEl`이 `↳ from <agent> · 💭 reasoning · ⚡ action`(.prompt-meta) 블록을 버튼/답변영역 위에 렌더 — delegate 출처 표시. **identity/roster**: `identity` 이벤트로 자기 conn_id 수신(접속자 로스터 "(you)"·큐 소유). 모든 연결 동등하게 입력 가능. 입력 POST 에 `conn_id` 동봉(큐 소유 식별). **닉네임 입력**: 접속 시 `#name-bar`(기본값 채워진 입력)로 이름 설정(localStorage 기억; 저장값 있으면 자동 적용·바 미표시) → POST /api/nickname. **닉네임 중간 변경(✎)**: `openNameBar(current)` 공유 헬퍼가 first-connect 프롬프트와 ✎ 진입점 양쪽에서 name-bar 를 prefill·포커스 재노출 — 헤더 `#rename-btn`(✎)은 `viewers` 이벤트에서 내가 로스터에 있을 때만 노출(`myNickname` 도 갱신해 prefill), 클릭 시 현재 닉네임으로 바 재오픈 → 기존 `applyNickname` 경로 재사용(POST + localStorage 갱신 + 바 숨김). 백엔드 무변경(`set_nickname` 이 로스터 재브로드캐스트, ephemeral·미영속). **메시지 큐 UI**: `queue` 이벤트 → `#queue-list` 에 대기 메시지(닉네임)·자기 항목 ✕(POST /api/queue/cancel). send 는 항상 큐잉(busy 면 대기); Stop 은 별도 `#chat-stop` 버튼(busy 시 노출, POST /api/stop). **markdown 헬퍼 (`escapeAndFormat` → `extractCodeFences` → `markdownInline` → `restoreCodeFences`)** — 의존성 0의 자체 미니 파서: 헤더(`#`/`##`/`###` → `<h1>`/`<h2>`/`<h3>`), GFM 파이프 표(헤더 행 + `---` separator + body), 순서/비순서 리스트 (`-`/`*`/`1.` 연속 라인 ↔ `<ul>`/`<ol>`), `**bold**`/`*italic*`, 인라인 코드, 펜스 코드(```` ``` ````). **XSS 안전(NFR-MD-2)**: `escapeHtml`이 가장 먼저 실행되어 `<`를 `&lt;`로 치환, 펜스를 placeholder로 빼낸 후 markdown 패스를 stripped body에 적용, 마지막에 pre-rendered `<pre><code>`로 복원 — markdown 패스가 사용자 입력 HTML을 실행 가능 토큰으로 되돌릴 경로가 없음. write_file/edit_file 의 plain diff 는 `colorizeDiffBody` 가 observation 본문에서 `--- a/` 블록 이후 라인을 첫 char 별 `rich-*` span 으로 색상 (diff 데이터는 plain — 색상은 렌더 시점). **`failed_turn` 핸들러**는 `finalizeStreamingAsFailed`로 live streaming 카드를 `.card-failed`로 마감(제거 X)+streamingText 리셋 → 잘못된 응답 / intervention(observation) / 재발화가 **3개 카드로 분리**(이전엔 하나의 카드에 재발화까지 누적되다 정상 응답에서야 교체). **Export 기능(별도 IIFE — main 렌더 루프 무수정)**: 📤 버튼이 선택 모드 토글 → `#messages > .card` 를 class 로 분류(user/assistant/observation/error/delegate; streaming·failed 제외)해 per-card 체크박스 부착(MutationObserver 로 실행 중 도착 카드도). 선택 entry(`{kind,label,body=innerText,mono}`)를 `POST /api/export/html`(Blob 다운로드) 또는 `POST /api/export/jira`(인스턴스 드롭다운은 `GET …/targets`로 채움[deployment 포함, 0개여도 폼 표시=zero-config] + **편집 가능한 base_url 필드**[config target 선택 시 prefill, 직접 타이핑 가능, localStorage `agentcli_jira_url` 마지막 URL 기억; `updateJiraHttpWarn` 가 `input`/`change` 마다 URL 이 `http://` 면 `#export-jira-http-warn` 평문 경고를 인라인 표시(차단 아님, https/빈값이면 숨김)] + Cloud/Server 토글 + 본인 계정·토큰[localStorage `agentcli_jira_cred_{base_url}` — URL 별 prefill] + issue key 폼, body 에 `base_url`+`auth:{user,secret}`+deployment 동봉)로 전송. Inspector 와 동일한 "헤더 버튼 → 별도 IIFE" 패턴. **Download 기능(별도 IIFE)**: 📥 버튼이 우측 드로어 토글 → `GET /api/workspace/tree`로 루트 목록을 받아 lazy 트리 렌더(디렉토리 ▶ 클릭 시 하위 fetch·펼침, 노드별 체크박스, 파일·디렉토리 size 표시). 선택 경로(또는 All) → `POST /api/workspace/download` → 응답 zip Blob 을 anchor click 으로 저장. **테마 피커(별도 IIFE)**: 🎨 `#theme-btn` 이 `#theme-menu` 드롭다운을 토글 — 5개 테마 목록(`THEMES` = id/name/swatch[bg+accent] 단일 출처)에서 항목별 스와치+이름+현재 ✓ 를 렌더, 클릭 시 `<html data-theme>` 설정 + localStorage `agentcli_theme` 저장 + 메뉴 닫기(외부 클릭/Esc 도 닫음). 기본 amber. 초기 테마는 `<head>` 인라인 스크립트가 이미 설정(FOUC 0); CSS 토큰만 갈리고 렌더 로직 무관 — 메인 IIFE 와 격리.
│       └── style.css        (1066) chat UI 스타일 — 가독성 우선, 모바일 폴백 단일 컬럼. **멀티-테마 디자인 토큰**: 파일 상단 `:root`(공유 다크 베이스 = "slate" ~55 토큰: surface/text/accent/status/glass/shadow) + 큐레이션 테마별 `[data-theme="midnight|terminal|amber|light"]` 오버라이드 블록(다크 변형은 surface+accent 만, light 는 전체 오버라이드). **다크 테두리=반투명 헤어라인**(`--border: rgba(255,255,255,.07)`) — 단단한 회색 선 대신 부드러운 경계(프리미엄 다크 룩의 핵심). 본문 전체가 raw hex 없이 `var(--…)` 로 파생돼 테마 추가=토큰 블록 하나(회귀가드 `test_theme_tokens_and_picker_wired`: body raw-hex 0·토큰 self-ref 0·테마 블록 존재). 테마 피커 드롭다운(`#theme-menu` + `.theme-item`/`.theme-swatch`). **폼/버튼 color 토큰화**: input/textarea/select/button 은 `color` 미상속이라 다크에서 검은 글씨가 되던 것 → 기본 `var(--text)` + placeholder `var(--muted)`. 메시지/카드 색상, 입력창 sticky, 닉네임 바·✎ `#rename-btn`, Prompt Inspector 드로어(스코프 칩 row), Export 선택 모드, Download 드로어, 헤더 아이콘 버튼(`#theme-btn`/`#inspector-btn`/`#export-btn`/`#files-btn` 공통 borderless-emoji) 등.
├── integrations/                   외부 서비스 연동 (export 타깃 등) — web Export 기능이 사용
│   ├── export.py            (149)  대화 export 렌더링 — 선택 transcript entry(`{kind,label,body,mono}`) → **`entries_to_html`**(self-contained HTML, inline CSS, escape + pre-wrap; mono body는 `<pre>`) / **`entries_to_adf`**(Jira **Cloud** 코멘트용 ADF doc — label은 strong paragraph, body는 mono면 codeBlock 아니면 paragraph; 빈 body는 skip해 ADF 빈-텍스트노드 거부 회피) / **`entries_to_wiki`**(Jira **Server·DC** 코멘트용 wiki 마크업 STRING — `*label*` 굵게, mono body는 `{code}…{code}`, 빈 body skip; v2 코멘트 body는 ADF 가 아닌 문자열). 셋 다 순수함수 → 브라우저·라이브 Jira 없이 단위테스트
│   └── jira.py              (236)  Jira 코멘트 POST — **프론트엔드 사용자 본인 명의**. config 는 선택(zero-config 가능); 자격증명은 서버 미저장(`jira.instances` 는 `base_url` + 선택 `deployment` 만 + `default`). `list_targets`(name+base_url+config-pinned deployment, 순수·네트워크 없음)·`detect_deployment(base_url)`(`{base_url}/rest/api/2/serverInfo` 의 `deploymentType` 무인증 GET → `"cloud"|"server"|None`, 성공만 프로세스 캐시)·`resolve_instance`(target/default/단일 해석, `base_url` 만 필수)·**`resolve_target(config, target, base_url)`**(어디로 POST 할지 결정 — body base_url 우선. config 인스턴스와 일치하면 신뢰(내부 http 허용), 미일치=사용자 입력이면 **`http://`·`https://` 둘 다 허용**(그 외 scheme/scheme 없는 값은 `JiraError`) — `http` 평문 위험은 여기서 차단하지 않고 UI 경고로 surface; base_url 없으면 `resolve_instance` 폴백 → 트러스트 정책의 단일 origin)·`post_comment(base_url, deployment, auth_user, auth_secret, key, body)` → `deployment=="server"`면 `/rest/api/2`+문자열 body, 아니면 `/rest/api/3`+ADF dict, 둘 다 `requests.post(auth=(user,secret))` Basic — 자격증명은 그 요청에만 쓰고 저장 안 함. base_url 이 인자라 테스트는 로컬 mock 으로(유료 Jira 불요). 실패는 `JiraError`. `requests` 재사용(새 의존성 0)
├── providers/                      LLM 프로바이더 어댑터
│   ├── __init__.py          (33)   create_provider() 팩토리
│   ├── base.py              (50)   LLMProvider 프로토콜, LLMResponse(+thinking), TokenUsage(+cache_creation/cache_read tokens)
│   ├── capabilities.py      (620)  ModelCapabilities + 프로브 감지 + 진행 콜백 + 자동 저장. **공유 오케스트레이터 `_detect_capabilities(model, transport)`** — context_window/thinking-태그/structured/reject/`max_output` 로직은 provider 무관 1곳, **transport 만 provider별**(`_OpenAITransport`=`/chat/completions`·Bearer, `_AnthropicTransport`=`/messages`·`x-api-key`+`anthropic-version`·`content[].text`). OpenAI 는 기존 helper 위임(parity), Anthropic 은 `_detect_anthropic_context_window`(/models 메타→/messages overflow→128K) + 프롬프트-only JSON structured probe(strict 항상 False) + `<think>` 태그 thinking 탐지. (omlx 가 두 API 동일모델 서빙·실 Anthropic 도 GET /v1/models 지원이라 양쪽 동작; 실 Anthropic 은 /v1/models 에 window 메타 없어 overflow/fallback/registry 로.) OpenAI 호환 context window는 `/v1/models` 메타 → overflow probe → 128K fallback 3-tier. **auto-detect 시 `max_output_tokens = context_window // 4`** (예: 256K→64K, 16K→4K; 기존 4096 cap 제거). context window가 `MIN_CONTEXT_WINDOW`(16K) 미만이면 `UnsupportedModelError` raise → CLI(`_setup_provider`)가 잡아 fail-fast (registry/models.json 저장값은 이 규칙 미적용 — 저장값 그대로). **structured-output 감지**(`_probe_structured_output`): context window 수용 후 `response_format={"type":"json_object"}` → `supports_structured_output`, 이어 strict `json_schema` → `supports_strict_schema` 프로브. 산문 자연스러운 프롬프트의 반환값이 유효 JSON(스키마 준수)일 때만 인정(서버가 `response_format` 무시 시 오탐 방지), 실패 시 보수적 False
│   ├── http.py              (307)  post_with_retry (Timeout/ConnectionError 재시도, pre-stream only + 스트리밍 StreamIdleTimeout 1회 재전송은 provider 가 소유, 고정 1초 백오프). **`raise_for_status_with_body(r)`** — provider 가 `r.raise_for_status()` 대신 사용: 표준 메시지는 본문 없는 `400 Client Error: ... for url` 이라 omlx overflow 400 의 상한 본문(`...exceeds max context window of N`)이 떨어져나가 `is_context_overflow` 가 못 잡고 flow 2 복구가 발화 못 함 → `raise_for_status()` 를 감싸 에러 분기에서만 `r.text`(≤1000자)를 메시지에 덧붙임(success/스트리밍 200 경로 무손상). + **`interruptible_lines(r, interrupt_check, poll=0.2, idle_threshold=, max_idle_ticks=, on_idle=)`** — 스트리밍 read 를 no-data gap 에도 중단 가능하게 + stall 감지: `r.iter_lines()` 는 다음 바이트까지 블로킹이라 "chunk 마다 flag 확인"으로는 **첫 토큰 전 TTFT 창**을 못 깬다. 블로킹 read 를 데몬 reader 스레드가 돌려 queue 로 넘기고, 제너레이터가 `poll` 주기로 폴링하며 빈 폴링마다 (1) `interrupt_check` → True 면 `r.close()` 후 중단, (2) **idle 측정** — `idle_threshold` 초마다 `on_idle(tick, secs)`(UI 대기 알림), 토큰 오면 리셋, `max_idle_ticks` 도달 시 r.close()+**`StreamIdleTimeout`** raise(interrupt 우선). interrupt_check·idle_threshold 둘 다 없으면 plain pass-through. **`make_stream_patient(r, read_timeout)`** — 스트리밍 post 가 헤더 받은 뒤 urllib3 소켓(`r.raw._connection.sock`) timeout 을 patient 로 재설정(짧은 post read 가 body 까지 죽이는 걸 회피; best-effort, 실패 시 debug_log + post timeout 이 backstop). 양 provider 공유(둘 다 requests 기반).
│   ├── anthropic.py         (258)  Anthropic Messages API (tool_use + thinking blocks + streaming + TTFT + prompt cache via cache_control). **`degeneration_check`**(= `wire.is_degenerate`, provider-독립)·**`interrupt_check`**(zero-arg) 두 predicate 를 `_handle_stream` 가 처리. line read 는 `interruptible_lines` 경유라 interrupt 는 TTFT 포함 no-data gap 에서도 깨지고, 루프 뒤 `interrupt_check()` 재확인으로 `stop_reason="interrupted"`(loop 이 partial 폐기). degeneration 은 content chunk 별 `'#'` 게이팅 후 True 면 `stop_reason="degenerate_runaway"`(loop 이 라벨·복구). openai 와 동작 동일 — loop 이 두 provider 에 같은 predicate 를 넘기므로 대칭(이전엔 anthropic 이 degeneration_check 를 받고도 버리는 비대칭 부채였음).
│   └── openai.py            (296)  OpenAI 호환 API (function calling + reasoning_content + streaming + TTFT). **스트리밍 콜은 재연결 루프**: post `(30,30)` timeout(헤더 바운드) → `make_stream_patient` 로 소켓 patient 리셋 → `_handle_stream`(idle 파라미터 + `on_idle`=render_status 대기 알림 전달). `StreamIdleTimeout`(10분 침묵) 잡으면 재연결 알림 렌더 후 재전송, `STREAM_MAX_RECONNECTS=3` 회 후 raise. 비스트리밍은 `(30,1200)`. **`degeneration_check`** kwarg(= `wire.is_degenerate`)가 있으면 `_handle_stream` 이 누적 텍스트에 적용 → True 면 stream 을 닫고 break(format-runaway 조기 중단, `'#'` 포함 chunk 에서만 검사해 O(headers)). truncated content 는 `stop_reason="degenerate_runaway"` 로 반환돼 downstream 에서 parse·라벨. **`interrupt_check`** kwarg(zero-arg, = loop `_interrupt_check` → `stop_event.is_set()`): line read 가 `interruptible_lines` 경유라 TTFT 포함 no-data gap 에서도 interrupt 가 깨지고(블로킹 read 를 시그널핸들러/타스레드에서 직접 닫는 reentrant/race 회피 — loop 은 flag 만, 닫기는 reader 소유 측), 루프 뒤 `interrupt_check()` 재확인으로 `stop_reason="interrupted"`. degeneration partial 과 달리 loop 이 이 partial 을 **파싱·기록 없이 폐기**(사용자가 방향 전환). **`response_format={"type":"json_object"}` 는 `kwargs["json_mode"]` 가 True 일 때만 전송 — provider 는 capability 를 직접 안 본다.** `json_mode` 는 **`WireFormat.provider_call_kwargs(capabilities)`** 가 결정(wire ⨯ capability 단일 결정점): JSON-shaped wire(react)는 `capabilities.supports_structured_output`, md_array(markdown)는 capability 무관 항상 `False`. 이전엔 provider 가 capability 와 wire 의 `skip_json_format` 을 직접 조합하다 prefix_md 에 JSON 강제 → omlx/mlx degenerate(`[2025]`/`[1000,1000]`)하는 버그가 있었음 (prefix_md 기본 전환이 노출 — bakeoff 는 provider 우회라 못 잡음). 이제 새 wire plugin 은 `provider_call_kwargs` 만 정의하면 provider 가 잘못 조합할 여지가 없음. **`response_format={"type":"json_object"}` 는 `kwargs["json_mode"]` 가 True 일 때만 전송 — provider 는 capability 를 직접 안 본다.** `json_mode` 는 **`WireFormat.provider_call_kwargs(capabilities)`** 가 결정(wire ⨯ capability 단일 결정점): JSON-shaped wire(react)는 `capabilities.supports_structured_output`, md_array(markdown)는 capability 무관 항상 `False`. 이전엔 provider 가 capability 와 wire 의 `skip_json_format` 을 직접 조합하다 prefix_md 에 JSON 강제 → omlx/mlx degenerate(`[2025]`/`[1000,1000]`)하는 버그가 있었음 (prefix_md 기본 전환이 노출 — bakeoff 는 provider 우회라 못 잡음). 이제 새 wire plugin 은 `provider_call_kwargs` 만 정의하면 provider 가 잘못 조합할 여지가 없음.
│
├── tools/                          도구 시스템
│   ├── __init__.py          (30)   registry re-export (TOOLS / TOOL_SCHEMAS / _execute_tool / infer_action / validate / get_descriptions) — 기존 `from agent_cli.tools import ...` 호환
│   ├── base.py              (193)  `Tool` ABC — schema(name/description/parameters) + dispatch(`_run`) + wire-key prefix(`key_prefix`/`strip_prefix`/`add_prefix`) + `claims`(prefix 매칭) + **`touched_paths`/`summary_arg`**(compaction 시 file-list 기여 + action 라벨 — 각 도구가 `strip_prefix`로 표준 키 읽음; base 기본=빈 list / 첫 string fallback, path·command·agent 도구가 override). **과대 출력 표면 2개**: `render_observation(result, args)`(결과→관찰 본문 렌더, 기본=성공 `output`·실패 `error` — write/edit 가 echo 트림 등으로 override 할 seam) + `apply_oversized_cap: bool = True`(이 도구 관찰에 `context_window//10` 캡 적용 여부 — 도구별 opt-out). loop `_tool_observation` 이 결과→관찰 seam 에서 둘 다 consult. **`render_action_input_for_context(action_input)->dict`** (관찰의 대칭 — action 측): 어시스턴트 turn 재공급 시 이 도구의 action_input 표현(**기본 identity**). manager `_context_view` 가 render+estimate 양쪽에서 consult. **현재 어떤 도구도 override 안 함 → 무영향**(seam 은 미래용 latent). write_file(`content`)·edit_file(`lines`) 본문 elide 를 켰었으나(v3.16.0) **모델이 재공급된 `<…elided…>` 마커를 본문으로 모방(mimicry)해 파일을 실제 손상**(모델은 `shell` heredoc 으로 우회 복구) → **v3.16.1 revert**. 교훈: **모델 자신의 출력(action)을 가짜로 재공급하면 모방 위험** — 관찰(=도구 결과)은 안전, action(=자기 출력)은 위험. 본문 bloat 는 미해결로 둠. **`parallel_safe: bool = False`** (Step 3): 한 턴의 연속 동-도구 op 들을 loop 이 동시 실행해도 안전한가 — 부작용/순서 의존 도구(write/edit/shell)는 False(순차가 정확성 보장), 독립 도구만 True. 현재 delegate 만 opt-in(독립 서브에이전트 = 병렬이 안전+가치). loop `_dispatch_parallel_batch` 가 읽음. **`wrap_single_op(flat)`** (멀티-op 3b): 멀티-op 포맷의 flat 단일-대상 op 을 자기 캐노니컬 입력으로 재포장 — 기존 validate→strip→run 파이프라인을 무변경으로 재사용하는 전제. 기본=add_prefix(미래 prefixed 도구용 — 현재 어떤 도구도 base 기본을 안 씀); **모든 builtin 도구가 flat-native(write_file/read_file/edit_file/code_index/delegate, consolidation Step 3)라 identity override**(스키마 자체가 flat). **`McpTool` 도 identity override**(MCP 는 prefix-less — base add_prefix 면 bare 키 `{query}`→`{srv.tool_query}` 로 손상돼 validate 실패; Step 4 발견·수정). 멀티-op 디스패치 경로에서만 호출(단수 포맷 우회). `run()`이 strip_prefix 후 `_run` 호출. 각 도구는 `name`만 정하면 prefix/strip/claims 자동(현재 latent — flat 키엔 무작동)
│   ├── virtual.py           (121)  가상 도구 Tool 서브클래스 (complete/ask/run_skill) — loop이 인터셉트, **표준 키 유지** (prefix/추론 대상 아님). **`ask` 는 flat 단수 `{question}`**(질문 하나=op 하나; 여러 질문은 ask op 여러 개=read_file 식 배치) — 비-terminal 이라 응답이 observation 으로 accumulate. (legacy `questions[]` 도 `_extract_questions` 가 관용.)
│   ├── result.py            (15)   ToolResult 데이터클래스 (success, output, error, artifact)
│   ├── registry.py          (416)  12개 Tool 인스턴스 수집 → `TOOLS`(= `TOOL_SCHEMAS` alias), `_execute_tool`(tool.run), **`infer_action`**(action_input 키 prefix → 정확히 1개 도구가 claims 하면 복원, 0/2+는 None), `validate_tool_input`(3-tuple), `get_tool_descriptions(..., wire_format=None)` — **2단 레이아웃(attention)**: 전 도구 기본 소개(`- name:`+Input JSON) ROSTER 먼저, 그 다음 상세 GUIDES(prose+예시) — 모든 도구 소개가 어떤 참조 예시보다 앞섬(cross-tool 참조: code_index fetch 가이드가 edit_file 을 가리키나 옛 단일-tier 는 edit_file 을 그 뒤에 소개). 텍스트 동일(재그룹만; 가이드 첫 문장이 자기 도구 명시). **format-aware**: wire 가 `multi_op` 면 각 도구 description·param 키에서 자기 prefix(`{tool}_`)를 strip (flat `{action, params}` 컨벤션). **`_multi_op_flat_params`**: multi_op 일 때 배치 배열 param 을 **item-object 필드로 unwrap** 해 Input JSON 을 flat 단일-op 모양으로 노출 — `Tool.wrap_single_op` 의 flat→batch 매핑과 정확히 대칭. **모든 builtin 도구가 flat-native(Step 3)라 현재 전부 else-분기(배열 없음)로 그대로 통과** — unwrap 메커니즘은 MCP/미래 배치 도구용으로 유지. item 스키마는 schema 의 `items.properties` 에 이미 존재(per-tool 선언 불필요). **이걸 안 했더니 27B 가 광고된 배열을 그대로 베껴 옛 wrapper 를 md_array 에서 emit 했음(DESIGN Exp 8 — root cause; inline 가이드만 flat 로 고치고 스키마 렌더를 안 고친 누락)**. **`_MULTI_OP_DESC_REWRITES`**: 배치 문장("Provide … as a list" 등)을 multi_op 에서 중립화하는 메커니즘 — 현재 **빈 dict**(모든 builtin 도구가 flat-native 라 description 이 native 단일-op). 추상화 표면으로 유지(미래 배치 도구용). 테스트가 잔존 배치 표현 0 단언. `exposes_complete=False` 면 `_ALWAYS_INCLUDE` 의 `complete` 를 생략. 기본(None/단수 포맷)은 바이트-동일 (스냅샷 테스트 가드). **`render_param_value`**(JSON-Schema property → `Input JSON` 값: type + required 마커 + 중첩 `array<object{k1, k2?, ...}>` 항목 키. MCP adapter 와 공유 — 두 도구 표면 렌더 일관). tool 모듈을 import하므로 `detectors`는 `validate_tool_input`을 lazy import (순환 회피)
│   ├── _diff.py             (68)   write_file/edit_file 공용 unified-diff 포매터 — **plain 표준 unified diff** (git diff 텍스트 형태, colour markup·gutter 없음). LLM observation 에 깨끗한 diff 가 들어가도록(=`[green]` 태그로 토큰 낭비/노이즈 없음); 색상은 렌더러가 라인 첫 char 보고 입힘 (CLI `_colorize_diff_line`, web `colorizeDiffBody`). 100줄 cap (`MAX_DIFF_LINES`) + `DIFF_TRUNCATION_PREFIX` summary
│   ├── read_file.py         (279)  파일 읽기 + hashline 포맷팅. `_read_one`(단일 파일: 부분/검색/stat 모드 dispatch) → ToolResult. **flat-native (consolidation Step 3)**: `ReadFileTool` 스키마 = flat 단일파일 `{path, line_start?, line_end?, search?, context?, stat?}` (required `path`) — `read_file_reads` 배치 배열·`read_file_` prefix 제거. `wrap_single_op`=identity, `_run`→`_read_one` 직결. 한 op=한 파일; 여러 파일은 멀티-op 포맷이 read_file op 을 여러 개 emit (op 배열이 곧 배치). (이전 batch `tool_read_file`/`_format_batch` 는 제거 — op-배열이 배치를 대신.) `key_prefix` 는 유지(latent: flat 키엔 strip no-op, `claims`=False)
│   ├── write_file.py        (67)   파일 생성/덮어쓰기. 작성 content 를 hashline(LINE#HASH:content) 포맷으로 반환 → LLM 이 read_file 없이 방금 쓴 파일을 바로 edit_file 가능 (write→edit 마찰 제거; 기존 colored diff 를 대체 — 작은 변경 시 전체 재작성 대신 부분 edit 유도) → ToolResult (+ WriteFileTool)
│   ├── edit_file.py         (369)  파일 편집 (hashline + 퍼지 매칭 + colored diff). ops: replace / append / prepend / delete (delete = pos..end 범위 제거, lines 없음 = replace+lines=[] 의 명시 형태) → ToolResult. **flat-native (consolidation Step 3)**: `EditFileTool` 스키마 = flat 단일편집 `{path, op, pos, end?, lines?}` (required `path,op,pos`) — `edit_file_edits` 배치 배열·`edit_file_` prefix 제거, `wrap_single_op`=identity. 한 op=한 편집. **같은 파일 다중편집 = 루프 레벨 배치 (`apply_edits_batch`)**: 연속된 같은-path edit_file op 들을 loop 이 묶어 이 순수함수로 라우팅 — 원본 1회 read → 모든 ref 를 그 원본 기준 해석(`_op_to_span`: op→half-open `(lo,hi,repl)` span) → overlap 사전거부(`_find_overlap`: 범위끼리 진짜 겹침·insert 가 범위 내부일 때만 거부, 인접 OK) → 줄번호 내림차순 **bottom-up** 적용(낮은 인덱스 안 밀림) → **1회 쓰기**. **all-or-nothing**: 한 op 라도 ref 실패/overlap 이면 무변경(부분쓰기 시 드리프트 재발 방지). 이로써 앞 편집이 줄을 밀어도 뒤 편집 ref 가 stale 안 됨 — 모델은 ref 를 **마지막 read 기준 그대로**(줄번호 미보정) emit. `fuzzy_verify_ref`/`format_diff`/`post_hook` 재사용, 단일 `tool_edit_file` 경로는 무변경(자기 메시지 보존). 다른 파일·비연속 편집은 per-op. `key_prefix` 유지(latent). (옛 `edits` 배열 머신러리는 read_file `_format_batch` 와 함께 제거됐고, 이 배치는 **중첩 배열이 아니라 flat op 의 루프 그룹핑** — 27B 깨뜨린 nested-array 함정 회피.)
│   ├── shell.py             (208)  셸 명령 실행 (**flat-native, Step 3**: 스키마 `{command, timeout?}` — `shell_` prefix 제거, `wrap_single_op`=identity; shell 이 마지막 flat 전환이라 이로써 **모든 builtin 도구 flat**) + 위험 명령 (rm/rmdir/mv) y/n/a 확인 (decision + 선택적 코멘트, env `AGENT_CLI_DANGEROUS_SHELL_CONFIRM=0`로 비활성) → ToolResult. **프롬프트 가능 여부는 `get_renderer().can_prompt()` 로 판정** (구 `_is_tty()` 대체) — CLI는 TTY, web은 연결된 클라이언트(SSE+/api/input, TTY 불필요). 못 물어보면 hang 대신 명확한 refuse 에러. **확인 직렬화는 렌더 레이어의 공유 `interactive_lock`(RLock)** 사용 (confirm·ask 공통) — parallel delegate가 task별 워커 스레드로 돌기에 "한 번에 하나의 outstanding 프롬프트"를 보장해 응답이 물어본 워커로만 라우팅. shell은 락을 잡고 `_session_allowlist` 재확인 후 `renderer.confirm`을 호출(같은 스레드 RLock 재진입). `ask`(`_handle_ask`)도 동일 `can_prompt` 게이트 — 못 띄우면 `"(no response)"` 치환. 위험 확인 별칭: `y`(+yes/ok/okay/yep/yeah/sure), `a`(+always/**allow**), `n`(+no/nope) — 긍정 의도가 안전 기본값 deny로 오인되지 않게 확장(특히 프롬프트 라벨이 "always allow"라 `allow`→a). 출력은 잘리지 않고 그대로 LLM observation으로 전달 (이전 shell_artifact 가드는 2026-05-19 제거 — head/tail 미리보기가 중간 디버깅 정보를 silent하게 누락시키는 사례 발견, 컨텍스트 budget은 compaction/FIFO가 처리)
│   ├── fetch.py             (258)  웹 페이지 fetch → 마크다운 변환 → ToolResult (+ FetchTool)
│   ├── delegate.py          (812)  in-process 서브에이전트 (fork/none, 병렬 + Live 상태 패널은 render.minimal `FrameClock` reuse, subdir, agent_stack, stop_event). **flat-native (consolidation Step 3)**: `DelegateTool` 스키마 = flat 단일 task `{task, context?, tools?, agent?}` (required `task`) — `delegate_tasks` 배치 배열·prefix 제거, `wrap_single_op`=identity. **`parallel_safe=True`** (유일) → loop `_dispatch_parallel_batch` 가 한 턴의 연속 delegate op 들을 모아 `{tasks:[...]}` 로 조립→`tool_delegate`→`_run_parallel`(병렬). 병렬 엔진(`tool_delegate`/`_run_parallel`/`_run_single`, `{tasks:[...]}` 소비)은 **기능적이라 보존**(read/code_index 의 batch 와 달리 삭제 안 함). `_invoke_delegate` 가 단일 flat op 을 `{tasks:[op]}` 로 정규화(agent 등 전 필드 보존). ContextManager는 순환 회피 위해 dispatch 시점 런타임 import
│   ├── context.py           (405)  read_context 도구 — **history 를 SQL 로 질의**. 필터 파라미터 더미 대신 **단일 `query`(SQL SELECT)** 프리미티브: history.jsonl 을 인메모리 sqlite `history` 테이블로 온-디맨드 적재(컬럼 `session/loc/seq/kind/turn/ts/tools/files/author/text`)하고 LLM 이 SELECT 작성. `text` 컬럼이 각 레코드의 **전체 내용**(검색·읽기 표면). 컬럼은 **읽기 시점에 `manager._classify_record`(kind/tools/text) + `extract_file_paths`(files) 로 유도** → 어떤 레코드 shape 든 동작, prefix-관습 재추측 없음. `turn`/`ts`/`author` 는 레코드에서. SQLite 는 `code_index._sqlite` shim 경유 **lazy 로드**(stdlib→pysqlite3 폴백; **코어 도구라 sqlite 부재여도 모듈 import 안 깨짐** — 쿼리 시 친절 에러). **읽기전용**: 비-SELECT prefix 거부 + 인메모리 DB 콜마다 재빌드·폐기(쓰기 무해)가 1차 가드, sqlite authorizer(SELECT/READ 외 거부)는 belt-and-suspenders(상수 없는 pysqlite3 빌드면 skip). **결과는 행/셀 캡 없이 VERBATIM 반환**(이전 50행 cap + 200자 셀 절단·공백 collapse 제거 — read_context 가 청크 회수 시 내용을 망가뜨리던 버그 수정) — 결과 크기는 loop 의 과대 출력 캡(좁히라 nudge)이 관장하고, 모델은 `LIMIT`/`substr` projection 으로 작게 유지. `query` 생략 시 스키마+예시+세션목록(discovery). `sessions`(current/all/<id>) = 테이블에 적재할 데이터 범위. `files` 컬럼은 다음 BM25(FTS5)와 같은 sqlite 기반.
│   └── code_index.py        (738)  code_index 도구 — `agent_cli.code_index` 패키지의 native-tool wrapper. **flat-native (consolidation Step 3)**: `CodeIndexTool` 스키마 = flat 단일쿼리 `{mode, path?, name?, symbol_kind?, ref_kind?, search?, with_*?, depth?, max_bytes?}` (required `mode`) — `code_index_queries` 배치 배열·`code_index_` prefix 제거, `wrap_single_op`=identity. 한 op=한 쿼리; 여러 쿼리는 멀티-op 으로 code_index op 을 여러 개 emit(읽기전용이라 순서/상태 의존 없음 — read_file `reads[]` 와 동형). `_run(args)→_dispatch_one(args)` 직결. (옛 batch `tool_code_index`/`_format_batch` 는 read_file `_format_batch` 와 함께 제거.) `key_prefix` 유지(latent). `_dispatch_one`(per-query mode dispatch)는 유지: 10 mode dispatch (list/fetch/lookup/kind/file/refs/callers/callees/slice/build). 인덱스 root 자동 해석 (cwd 또는 가장 가까운 조상 `.agent-cli/`), lazy build + per-query incremental refresh. list/fetch는 root 바깥 path에 대해 on-demand parse fallback (DB 갱신 없음); 나머지 모드는 index-scoped (out-of-root 명시적 거부). fetch 결과는 hashline 포맷 → edit_file 직결. `post_hook(path)`는 edit_file/write_file 성공 직후 호출되어 자동 incremental refresh — 모든 예외 swallow (인덱싱 hiccup이 user-facing op 막지 않음). `_resolve_defs_path(root)`가 `<root>/.agent-cli/defconfig` 존재 시 `build(defs_path=...)`로 전달 — kernel/driver처럼 `#ifdef CONFIG_*` 가 함수 시그니처를 분기하는 코드에서 tree-sitter 파싱이 ERROR로 떨어져 정의가 누락되는 케이스를 unifdef 사전 분기 제거로 살림. 파일 부재 시 `None`이 그대로 통과해 기존 무전처리 동작 유지. 모듈 레벨 `_BUILD_LOCK` (threading.Lock) 이 `_ensure_index` / `post_hook` / `_do_build` 의 `build()` 호출을 직렬화 — 병렬 delegate worker 가 동시 진입해도 중복 빌드 없음. (atomic write 가 correctness 를 책임지고, 락은 효율 + SQLite 락 경합 회피 책임).
│
├── code_index/                     code_index 패키지 — tree-sitter SQLite 코드 인덱서 (`minish.ai/Agent-tools tsindex.py` Apache 2.0 port — NOTICE 참조). 총 ~5,000 LOC. `_sqlite.py` shim 이 stdlib `sqlite3` 우선 / 미존재(`--without-sqlite` CPython) 시 `pysqlite3-binary` 폴백 — Linux 잠금 서버에서도 무설정 동작.
│   ├── __init__.py          (56)   public API: build / load_index / build_callgraph / cmd_slice / IndexStore / Symbol / Ref / NAME_KINDS / CODE_NAME_KINDS / REF_KINDS / SCHEMA_VERSION
│   ├── schema.py            (~140) SCHEMA_VERSION=2 (v2: `qualified_name` 컬럼 추가, walker가 emit 시 full display form 산출 — Python/JS/TS/Java/Go/Rust/Markdown은 `.`, C++는 `::`, C는 flat=name; tool handler가 qualified_name 우선 lookup + bare-leaf fallback). Symbol/Ref dataclass, NAME_KINDS(5-vocab: function/type/variable/constant/section), CODE_NAME_KINDS(=NAME_KINDS-{section}, cross-file ref name resolution 전용 4-vocab), REF_KINDS(call/name/type). `section`은 markdown heading 5번째 vocab으로 추가됨 (upstream 4-vocab → 5-vocab).
│   ├── preproc.py           (498)  C/C++ 전처리: unifdef 드라이버 + rewriter chain (foreach/decl_macro/bare_attribute/variadic/ifdef_zero/define_comments/pp_trailing_ws/consecutive_attr/pp_continuation/type_arg). `_apply_unifdef` 헬퍼가 백엔드 선택 (시스템 `UNIFDEF_BIN` 우선, 없거나 `AGENT_CLI_UNIFDEF=pure` 면 `_unifdef.run_unifdef` 사용). `compute_preproc`이 fingerprint 산출 — defs file 내용 변경 시 인덱스 자동 invalidate. 백엔드 선택 정보 `preproc_info["backend"]` 에 노출.
│   ├── _unifdef.py          (653)  Pure-Python `unifdef -b` 구현 — Pratt-style 표현식 parser (defined/논리/비교/산술/비트), UNKNOWN 전파 + short-circuit 평가, directive walker (TAKEN/NOT_TAKEN/PASS_THROUGH 상태 스택). `-b` 라인 보존 contract 준수. 시스템 unifdef 와 byte-identical parity (parity 테스트로 8 케이스 보장). preproc.py 가 백엔드 fallback 으로 사용 — 시스템 binary 없는 잠금 서버에서도 무설정 동작.
│   ├── store.py             (257)  IndexStore (SQLite reader). find_symbols/find_refs/find_refs_in_range, normalize_file_path (exact/absolute/basename/suffix), kind_counts/ref_kind_counts/top_ref_names. dict-style 접근(`idx['symbols']`)도 호환 유지.
│   ├── builder.py           (492)  build() — Pass-1(definitions) + Pass-2(refs) + sha1 incremental + Option-B re-Pass2 (변경 파일의 새 이름을 mention하는 unchanged 파일 자동 re-walk). `iter_source_files`가 `_SKIP_DIRS` (.git/.agent-cli/.claude/.venv/node_modules/build/dist 등) prune → 인덱스 폭주 방지. 무효화 트리거 3개: schema_version mismatch / meta.root 변경 / preproc_fingerprint 변경. `write_sqlite_index` 는 atomic tmp + `os.replace` 패턴 — 활성 DB 파일을 절대 unlink/truncate 하지 않고 옆에 새 tmp 파일을 만든 뒤 한 번의 rename 으로 swap. 병렬 delegate worker 가 같은 인덱스에 동시 접근해도 `sqlite3.OperationalError: disk I/O error` race 안 남.
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
│   ├── manager.py           (1046) ContextManager (토큰 budget 압축 + FIFO fallback + history.jsonl + 자연어 변환). **`_context_view(message)`**: 어시스턴트 turn 의 재공급 표현 — op별 `Tool.render_action_input_for_context`(기본 identity) 적용한 **복사본**(원본 record 불변). render(`_to_natural_language`)+estimate(`_estimate_message_tokens`) 양쪽에서 호출 → 재공급=카운트 일관(큰 write/edit 본문 elide 대비 seam; 현재 identity 라 무영향). **history.jsonl retrieval enrich**: `_append_to_history` 가 round-trip 메시지에 **검색 키를 가산**해 파일에 기록(`_enrich_record`) — `kind`/`turn`/`ts`/`tools`/`files`(`extract_file_paths` 재사용 — 조작 파일 경로)/`text`(+`author` passthrough). `kind`/`tools`/`text` 는 `_classify_record`(레코드 shape → query/action/observation/final/raw/system, `[author]:`·`Observation:` prefix 벗긴 평탄 text)로 유도, `turn` 은 loop 이 매 턴 경계 `set_turn`. **파일만 enrich** — `_cache`/`get_messages`(LLM 경로)는 무변경(round-trip 필드 그대로, extra 키 무시). read_context(JSON 쿼리)와 외부 jq 가 이 키들을 쓴다. (하위호환 무시 — 구 세션은 키 없어 쿼리에서 자연 제외.) **`ensure_within(target)`** (flow 1 예방형): loop이 매 호출 직전 `target=(C−S−O)×0.8`(S=system 실측)로 호출 — `_cache_tokens > target`면 LLM 요약 compaction 시도 (system anchor만 보존 → oldest 절반 evict → 단일 호출로 요약, 이전 summary가 있으면 같은 호출에 prepend하여 recursive 갱신 → `_file_extract`로 touched paths 누적 dedup → `[system][summary][file_list][retained]`로 캐시 재구성 → `compaction.json` atomic write). **요약 입력은 `_to_summary_text`로 만든 자연어 transcript를 user 메시지 하나로 감싼 형태** — `get_messages`의 `_to_natural_language`(assistant를 ReAct JSON으로 round-trip)와 달리, 요약 경로에선 assistant를 산문으로 풀고 action_input을 owning `Tool`의 **`Tool.summary_arg`**(`touched_paths`의 sibling — `strip_prefix`로 표준 키를 읽어 prefix/배열 셰이프 흡수; registry lazy import로 순환 회피)로 축약(파일 본문 제거)해 모델이 "transcript를 요약"하게 함(이전엔 ReAct JSON 대화로 보여 소형 모델이 요약 대신 다음 `write_file` 액션을 생성하던 버그). **(이전 버그: 구 `summarize_tool_args`가 bare `args.get("path")`를 읽어 wire-key prefix 도입 후 모든 실 레코드에서 빈 라벨 — `write_file()`처럼 인자 누락. tool-result 레코드는 args가 없으므로(=`{role,tool,success,content}`) 라벨은 assistant 액션 레코드에서만 나옴. 테스트가 가짜 `args:{path}`·bare `action_input:{path}` shape을 써서 미검출 — `_file_extract`와 동일하게 `serialize_assistant_for_history` 실제 출력 기반 회귀가드 추가) **멀티-op record 처리(`_file_extract` 동형)**: 멀티-op 포맷은 `{ops:[...]}` 저장 → op 순회로 각 op 라벨, flat op 은 `wrap_single_op` 으로 캐노니컬 정규화 후 `summary_arg`. (이전엔 top-level `action` 만 읽어 md_array 기본값 요약이 thought-only 였음 — 도구 호출 기록 증발; md_array 회귀가드 추가) dangling assistant 턴 없음 → 연속 유인 제거. 요약 실패하거나 재구성된 캐시가 여전히 target 초과면 belt-and-braces로 `_evict_fifo(target)` 발동 — 무한 트리거 루프 방지. `add()`는 compaction 트리거 안 함(append만). **`compact_now()`** (수동 `/compact`): `_compact` 1회 실행 후 `(before, after)` 토큰 반환 — disabled/compactor 없음/evict 대상 없으면 no-op(equal), 실패 시 warning만(강제 FIFO 안 함). **`reconcile_actual_tokens(actual, system_tokens)`**: 호출 직후 서버 실측(`usage.input_tokens`+cache)으로 `_cache_tokens = actual − system`으로 re-anchor → chars/4의 CJK 과소평가가 턴 간 누적되지 않음(drift 1턴치). **`force_fit(target, actual_tokens)`** (flow 2 반응형): 서버가 400(prompt too long)으로 거부하면 loop이 호출 — 로컬 추정(chars/4, CJK 과소)을 못 믿으므로 서버가 알려준 `actual_tokens`로 reconcile 후 compact→FIFO로 비율 축소. keep_ratio=target/actual로 줄여 추정 과소배율이 분자분모에서 상쇄(추정 절대정확도 불필요); progress 보장(매 호출 최소 1개 evict, anchor=최신 1개 보존). `actual_tokens` 없으면 ~25% trim fallback. `compaction_enabled=False` 또는 `AGENT_CLI_COMPACTION=off`로 끄면 기존 FIFO만 동작. Resume: `compaction.json`의 `dynamic_start_index`로 history.jsonl 후방 슬라이스만 cache 복원해 summarised tail과 중복 방지. 인스턴스마다 wire_format plugin attach (`__init__(wire_format=...)`, default fallback="react"). `get_messages()`는 system은 verbatim, user/tool branch만 자체 처리하고 assistant branch는 `wire_format.render_assistant_from_history`에 위임 — 한 세션 = 한 wire_format으로 격리. Compactor 콜백(`set_compactor`)과 `TurnRecorder`(`set_recorder`)는 `AgentLoop`가 후입식으로 주입 — unit-test 경로는 미주입 상태로 즉시 사용 가능.
│   ├── _file_extract.py     (74)   `extract_file_paths(messages)` — evict 된 assistant record 의 `action`→owning `Tool` 의 **`Tool.touched_paths(action_input)`** 에 위임. path/prefix 키 지식을 각 도구에 둠(=`strip_prefix` 재사용; write/edit/read/code_index=flat `{path}`, delegate=flat `{agent}` placeholder — 전부 flat-native Step 3) → 도구가 입력 셰이프를 바꿔도(예: read_file 의 flat-native 전환) extract 가 자동 추적. **멀티-op record 처리**: 멀티-op 포맷(md_array·react)은 `{ops:[...]}` 로 저장하므로 op 리스트 순회(single-op `{action,action_input}` 은 `[msg]` 로 정규화); 저장 op 은 flat(모델 emission)이라 `touched_paths` 전에 **`Tool.wrap_single_op` 으로 정규화**(현재 모든 builtin 도구 identity 라 사실상 no-op — MCP/미래 도구용 단계). registry 는 함수 내 lazy import 로 module-load 순환(registry→context-tool→manager→_file_extract) 회피. 입력 순서 dedup. compaction 시 file_list 단일 진입점. **(이전 버그: ① bare `path`/`tool-result args` 가정으로 wire-key prefix 도입 후 file_list 빈 채 — 회귀가드 추가. ② top-level `action` 만 읽어 멀티-op `{ops}` record 의 경로를 전부 놓침 — md_array 기본값(2026-06-11~) file_list 가 줄곧 비었음; ops 순회+wrap 정규화로 수정, md_array 회귀가드 추가)**
│   └── session.py           (211)  세션 메타데이터 (session.jsonl — id/workspace/updated_at/`response_format`. response_format 은 세션이 돈 wire format 을 기록 — 디버깅·resume 시 형식 복원용. default 는 `DEFAULT_WIRE_FORMAT`(md_array); response_format 키가 없는 옛 세션도 현재 default(md_array) 로 로드 — backward-compat to 이전 default(react→prefix_md) 는 의도적으로 미보존) + resume용 user↔assistant 페어 추출 (recent_exchanges) + `session_summary(meta)` = 마지막 (user 요청, 결과) 한 쌍을 history 에서 읽어 반환 (제거된 `query` 메타 필드의 대체 — sessions 목록/resume 프롬프트가 공유). System-injected user 메시지 필터는 `wire_formats.all_system_user_prefixes()` (format-agnostic 프리픽스 + 등록된 모든 plugin의 framing prefix) 단일 진입점 사용 — 새 wire format plugin 추가가 자동 반영
│
├── prompts/                        프롬프트 템플릿
│   ├── __init__.py          (1)
│   └── system_prompt.py     (969)  Attention 최적화 시스템 프롬프트 빌더. **`build_system_prompt_sections()`** = 단일 조립 지점 — (이름, 텍스트) 섹션 리스트 반환(Role/Context Discipline/Task Guidelines/Response Format/Available Tools/[MCP Tools]/[Skills]/[Agents]/Environment/[Context Recovery]/[Directives]/[Execution Context]); `build_system_prompt()`는 그 join wrapper(바이트-동일). 섹션 이름은 조립 시점에 부여 — 합쳐진 문자열의 `##` 재파싱은 본문 헤딩(도구 가이드·format_rules 예시) 때문에 불가하므로 구조를 원천에서 노출(Prompt Inspector 소비) (Primacy/Middle/Recency, Role 상속, Context Recovery Guide). **format-aware 툴 가이드 (멀티-op 2단계 — DESIGN §5)**: 4개 인라인 빌더(read_file/edit_file/code_index/delegate)가 `wire_format.multi_op` 분기 — multi_op 면 per-tool 배치 prose·예시를 생략하고 단일-대상(op 하나=파일/edit/query/task 하나) 예시를 `render_action_input` 경유 flat 렌더 (op 배열이 곧 배치 — 배치 중첩이 27B 90% 깨뜨린 실측 근거). **read_file 빌더는 flat-native(Step 3) 이후 예시가 항상 flat 단일파일** — `multi_op` 분기는 intro 문구(멀티-op: "한 턴에 read_file op 여러 개" / 단수: "한 호출=한 파일")만 가르고, `{reads:[...]}`·"5. Batch" prose 는 제거(read_file 에 batch 셰이프 자체가 없어짐). `_ASK_INLINE_NO_COMPLETE` = `exposes_complete=False` 포맷용 ask 가이드 변형 (`complete` 호출 대신 thought-only finish 로 표현). **react 도 Step 2(2026-06-13)부터 multi_op=True** 라 multi_op 분기를 탐 — 출하 포맷 둘 다(react/md_array) multi-op 이고, else(단수) 경로는 synthetic 포맷만 도달(정리는 roadmap Step 3/4). 스냅샷 가드(tests/snapshots/tools_section_react.txt)는 이제 react 의 **multi-op flat** 렌더를 고정. `build_system_prompt(wire_format=…)` — Response Format 섹션은 `wire_format.format_rules()`, 스킬·에이전트 호출 예시는 `wire_format.render_full_example(thought=None, ...)`, 도구 inline 가이드의 action_input 단편은 표준 키 dict로 작성되어 `wire_format.render_action_input(dict)`이 wire별 직렬화 (react·md_array 둘 다 prefixed dict→flat op `{action, ...plain params}` 로 변환; 비-JSON plugin이 swap), 도구별 `{tool}_` prefix는 `Tool.add_prefix`로 적용 — 가이드는 표준 키로 쓰고 prefix·직렬화는 단일 출처(`_rai_prefixed`)에서. 인라인 예시는 wire 셰이프로 감싸지 않음 — 와이어 셰이프 학습은 Format Rules + skill/agent 예시(각 1번)에서 일어나고, 인라인은 mode 분기 / 의미론 학습. Recency 순서: Environment → Recovery → Directives → Execution Context (passive→active, persistent→immediate; Execution Context만 동적이라 끝에 배치 → 앞 3개 KV cache 안정). Tool inline 가이드는 `_build_tool_inline_guides(active_tools, wire_format)` 가 매 호출마다 빌드 — `read_file` 가이드의 Flow 문장이 `code_index` 활성 여부에 따라 분기 (활성 시 supported 확장자 파일은 `code_index mode='list'`로 우회 — 확장자 목록은 `code_index.languages.get_supported_extensions()` 단일 출처에서 가져와 walker 추가가 자동 전파). code_index 가이드는 per-file (list/fetch) vs index-wide (lookup/kind/file/refs/callers/callees/slice) scope 경계를 명시, on-demand parse fallback 위치도 안내. **code_index 도 flat-native(Step 3)**: `rai` 헬퍼가 항상 flat 단일쿼리 렌더, `{queries:[...]}` batch 예시·"LIST" prose 제거(read_file 와 동형). edit_file 가이드(`_build_edit_file_inline` — op 시맨틱·hashline·constraints 는 wire 공통 텍스트, flat 단일편집 예시만 `render_action_input`으로 wire별 렌더)는 (1) 편집 직전에 CURRENT turn에서 read 하도록 요구(code_index mode='fetch'도 fresh read로 카운트) (2) hash mismatch를 failure가 아닌 guardrail로 reframe해 모델이 panic 없이 re-read/retry 하도록 톤 조정 (3) **파일 CONTENT 작성은 write_file/edit_file — shell heredoc(`cat <<EOF`) 금지** nudge: 코드가 shell+JSON 이중 escape 돼 NO_JSON 빈발(세션 1782027249 실측 지배)이라, write_file 는 본문을 전용 필드에 담아 escape 한 겹. **edit_file flat-native(Step 3)**: 한 op=한 편집(`edits` 배열 제거), `multi_op` 분기는 framing 문구만 가름. **same-file 배치 안내(multi_op 분기)**: "같은 파일 다중편집은 한 턴에 연속 edit_file op 으로 — ref 는 마지막 read 기준 그대로(줄번호 미보정), 함께 bottom-up 적용되어 stale 안 됨, overlap 은 배치 거부, 같은-파일 op 은 인접 유지"로 **허용**을 안내(루프 `_dispatch_edit_batch` 가 실제 처리). 옛 "턴을 나눠 re-read" 문구를 대체. 단수(react 아닌 synthetic) 분기는 한 턴 1-op 이라 여전히 턴 분리 안내.
│
├── skills/                         프롬프트 스킬 시스템
│   ├── __init__.py          (7)    re-export
│   ├── models.py            (21)   Skill 데이터 모델 (model/context/hooks/invocation)
│   ├── loader.py            (95)   스킬 파일 검색/파싱 (ResourceLoader 기반, 캐싱)
│   ├── executor.py          (209)  인자 치환 + 도구 교집합 + Role 상속 + skill subdir + stop_event
│   └── builtin/                    패키지 내장 스킬
│       ├── create-skill.md         스킬 생성 메타 스킬
│       ├── create-agent.md         에이전트 생성 메타 스킬
│       ├── plan.md                 구현 계획 생성 (plan/ 디렉토리에 저장)
│       └── create-team/            에이전트 팀 구성 메타 스킬
│           ├── SKILL.md            6단계 워크플로 (분석→설계→에이전트→스킬→오케스트레이터→검증)
│           └── references/         단계별 가이드 (design-patterns, agent-writing, skill-writing)
│
├── agents/                         에이전트 정의 패키지
│   ├── __init__.py          (1)
│   └── builtin/                    패키지 내장 에이전트
│       └── explorer.md             읽기 전용 코드베이스 탐색 에이전트
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
│openai_   ││try    ││overflow││prompt  ││react     │
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
wire_formats/react  → recovery/intervention, recovery/primitives,
                      wire_formats/base, wire_formats/_json_diag,
                      wire_formats/_json_repair
wire_formats/md_array → recovery/intervention, recovery/primitives,
                      wire_formats/base, wire_formats/_json_diag,
                      wire_formats/_json_repair
wire_formats/__init.→ wire_formats/base, wire_formats/react, wire_formats/md_array
                      (builtin 등록)
tools/result.py     → (외부만: dataclasses, 순수 데이터 타입)
tools/read_file.py  → tools/result, (외부만: re, zlib, pathlib)
tools/edit_file.py  → tools/read_file, tools/result
tools/shell.py      → tools/result
tools/write_file.py → tools/result, tools/read_file (format_hashlines)
tools/context.py    → tools/result, context/session
tools/delegate.py   → tools/result, context/manager, resource_loader, loop (lazy import)
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
                      render, tools, tools/delegate, tools/registry,
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
- `<think>...</think>` 태그가 content 안에 있는 경우는 별도 — `parse_react`가 `ParsedAction.thinking`으로 분리 추출

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

모든 wire-format plugin이 반환하는 boundary 데이터타입. ReActFormat의 `parse_react`(같은 plugin 안)는 이 타입을 *직접* 반환하며, 미래 plugin도 같은 타입을 사용해 loop이 plugin과 무관하게 동작.

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
    def run(self, args, *, session_dir=None):   # strip_prefix → _run
    @abstractmethod
    def _run(self, args, *, session_dir=None) -> ToolResult: ...
```

**Wire-key prefix** (★ Step 3 완료로 **모든 builtin 도구가 flat-native** — 어떤 builtin 도 prefix 안 씀; MCP 도 prefix-less. 아래 dropped-action 복구 메커니즘은 **미래 prefixed 도구/포맷용 latent seam** — Step 4 에서 "삭제 대신 seam 보존" 결정): action_input 의 최상위 키를 `{tool}_{param}` 으로 네임스페이스한다 (예시는 가상의 prefixed 도구 `xtool_param`; 중첩 키 등은 그대로). 모델이 `## Action` 의 tool 이름을 누락해도(parse_stage 3) 키 모양으로 도구를 복원할 수 있다 — loop 이 parse 직후 (`wire_format.action_required=False` 일 때만) `registry.infer_action(action_input)` 을 호출, 각 `Tool.claims`(prefix 매칭)가 투표해 **정확히 1개**가 소유하면 그 도구로 보정(0/2+는 NO_ACTION recovery로; `action_required=True` plugin 은 infer 를 건너뛰고 바로 NO_ACTION). 이 복구의 전제는 파서가 action 무효 시에도 action_input 을 보존하는 것(WireFormat.parse 계약). 보정에 성공하면 `_append_observation` 이 next-turn prior(messages)와 history record 를 **보정된 wire shape** 으로 재기록한다 — raw drift 를 prior 로 다시 먹이면 mimicry 가 강화되기 때문(다음 턴이, 또는 resume 시 복원된 prior 가 "action 이름을 빠뜨려도 된다"를 학습; NO_THOUGHT retry 가 피하는 것과 같은 실패). 보정 자체는 `TurnRecorder`(parse_stage=3 + `action_inferred` primitive)로 추적되므로 형식 실패 분석 신호는 보존된다. prefix 는 **wire 표면에만** 존재: `Tool.run()` 이 dispatch 직전 `strip_prefix` 로 표준 키로 되돌려 `tool_*` 함수·virtual 처리·validate·기존 dispatch 가 전부 표준 키를 받는다(prefix 없는 키는 no-op → 모델이 표준 키를 보내도 동작). 키 prefix 가 변별을 구조적으로 보장하므로 claims 충돌(`{content, edits}` 류)이 원천 소멸 — 각 도구는 `name` 만 정하면 prefix/strip/claims 가 자동(override 0). **실측 근거**: omlx 27B/35B 에서 prefix 키 compliance 60/60(std-leak 0) — 표준 키와 동일하게 따름.

```text
# 실제 도구 (각 모듈에 Tool 서브클래스): read_file, write_file, edit_file, shell,
#   code_index, read_context, fetch, delegate
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
[system]   Role (main/delegate/skill별 상이)
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

스킬/delegate 실행 시 출력을 시각적으로 감싸기 위해 `group_start`/`group_end`와
depth 기반 prefix(`│ `)를 사용. 병렬 delegate는 worker별 capture 후 Live 패널로
실시간 상태 표시, 완료 후 block replay.

| 시점 | 호출 | 출력 |
|------|------|------|
| 스킬/delegate 시작 | `render_group_start(label, icon)` | `┌─ 🪄 skill:plan` |
| 내부 턴 | `push_depth` 상태에서 `_p()` | `│ 💭 thought...` |
| 스킬/delegate 종료 | `render_group_end(label, success, dur)` | `└─ ✓ skill:plan (5.2s)` |

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

### 5.3 3단계 파싱 폴백 (`wire_formats/react.py`)

```
LLM 텍스트 응답
    │
    ▼
유니코드 서로게이트 제거 (_sanitize_surrogates)
    │
    ▼
Thinking 블록 분리 (_strip_thinking_blocks)
    │  ├─ <think>...</think> 제거 → thinking 필드에 보존
    │  ├─ <thinking>...</thinking> 제거
    │  ├─ <reasoning>...</reasoning> 제거
    │  └─ <reflection>...</reflection> 제거
    │
    ▼
Stage 1: 마크다운 펜스 제거 → json.loads()
    ├─ 성공 → ParsedAction (parse_stage=1)
    │
    ▼ 실패
Stage 2: repair_json() — 6단계 복구 파이프라인 (같은 모듈 안 helper)
    │  ├─ JSON 블록 추출 (brace depth tracking)
    │  ├─ 작은따옴표 → 큰따옴표
    │  ├─ 따옴표 없는 키 수정
    │  ├─ trailing comma 제거
    │  ├─ 닫히지 않은 문자열 닫기
    │  └─ 누락된 괄호 추가
    ├─ 성공 → ParsedAction (parse_stage=2)
    │
    ▼ 실패
Stage 3: regex 필드 추출
    │  ├─ "thought": "..." 추출
    │  ├─ "action": "..." 추출
    │  └─ "action_input": {...} 추출
    ├─ 성공 → ParsedAction (parse_stage=3)
    │
    ▼ 실패
ParsedAction (parse_stage=0, 모든 필드 None)
```

`repair_json` 등 stage 2/3 helper는 모두 ReAct plugin 모듈(`wire_formats/react.py`) 안에 함께 산다. `parsing/` 별도 패키지를 두지 않음으로써 plugin이 *폴더 째 삭제 가능*한 boundary를 유지 — 다른 plugin은 자기만의 recovery 전략을 정의 (의존 X). 같은 algorithm을 재사용해야 할 plugin이 등장하면 그 시점에 공통화 결정.

#### 형제 키 정규화 (action_input hoist, 2-레이어)

일부 소형 모델은 action 인자를 `action_input` 안에 **중첩하지 않고 top-level 형제 키로** 뱉는 드리프트를 보입니다:

```json
// 드리프트 A: 가상 툴 payload가 top-level
{"thought": "done", "action": "complete", "result": "final answer"}

// 드리프트 B: 실제 툴 인자가 top-level (pcie_scsc 세션에서 관찰)
{"thought": "find files", "action": "shell", "command": "ls"}

// 둘 다의 기대 형태
{"thought": "...", "action": "...", "action_input": {...}}
```

JSON 자체는 valid하고 action 이름도 올바른데, loop이 `action_input.X`를 찾기 때문에 조용히 실패 (가상 툴은 "Completed without result", 실제 툴은 "Missing required field" → repeated-call guard). strict JSON Schema로도 막히지 않음 — 과거 schema가 `thought`만 required로 두고 `additionalProperties` 제한이 없었기 때문.

`_normalize_action_input()`이 파싱 직후 두 레이어로 정규화합니다 (`wire_formats/react.py`):

**Layer 1 — 가상 툴 별칭 매핑.** `complete` / `ask`에 대해 정해진 후보 키를 canonical target 키로 매핑:

| action | target key | top-level fallback 순위 |
|---|---|---|
| `complete` | `action_input.result` | `result` > `answer` > `response` > `final` > `output` |
| `ask` | `action_input.questions` | `questions` > `question` (`_extract_questions`가 str→list 처리) |

알려진 가상 툴인데 후보가 하나도 없으면 **fall-through 안 함** — `action_input=None` 유지해서 downstream이 "no payload" 경로로 처리 (복귀 가능).

**Layer 2 — 실제 툴 / 미지의 action의 형제 키 번들링.** 가상 툴이 아니고 `action_input`이 없으면, 예약되지 않은 top-level 키 전부를 `action_input`으로 모아줌:

```json
// 입력
{"thought":"...", "action":"shell", "command":"ls", "timeout":10}
// 정규화 후
{"thought":"...", "action":"shell", "action_input":{"command":"ls","timeout":10}}
```

MCP 제공 툴처럼 `action` 이름이 레지스트리에 없어도 같은 룰 적용.

**예약어 블랙리스트 (`_REACT_RESERVED`).** 다음 키들은 형제로 나타나도 `action_input`에 담기지 않습니다:

- `thought` / `action` / `action_input` — ReAct 프로토콜 필드
- `observation` — 시스템 프롬프트가 금지하지만 드리프트 시 혼입 가능
- `reasoning` / `reflection` — thinking 태그 변종 (태그로 나타나면 `_strip_thinking_blocks`가 잡지만 top-level 키 형태로도 등장 가능)
- `role` / `_meta` — 저장/세션 계층 메타 필드

**우선순위 규칙.** `action_input`이 이미 있고 truthy면 Layer 1, 2 모두 skip — 모델이 명시적으로 nested를 선택했다고 보고 형제 키는 무시. `action_input`이 `None` 또는 `{}`면 레이어 로직 발동.

이 정규화는 strict JSON Schema 도입 없이 작동하며, flat form을 정식 canonical로 승격하는 미래 변경(`plan/schema-flatten.md` 참조)의 파서 기반이 됩니다.

#### Failure Grounding Retry (`recovery/primitives.py` + `constants.py` + `loop.py`)

> 설계 문서: `docs/robust-harness/DESIGN.md` (4-layer 디자인, primitive 도구함, playbook)

3단계 파싱이 모두 실패하거나(JSON 깨짐 — `parse_stage=0`) JSON은 파싱됐는데 `action`이 없으면 (`parse_stage>0` & `action=None`), `loop.py`가 user role 메시지를 한 개 주입하고 같은 turn을 재시도합니다 (`turn -= 1`로 카운트 제외). 메시지는 `recovery/primitives.py`의 순수 함수들을 합성한 결과:

```
Your response was not valid JSON.
Expecting ',' delimiter (line 1, column 39)   ← diagnose_syntax_error (있을 때만)
    [{"action":"shell","command":"ls -la"}
                                          ^

Your prior output:               ← echo_prior_output: head 400자 (구조 마커 보존)
---
{LLM이 방금 토출한 content}
---

Honor that. Output ONLY a JSON object: {...}.   ← constrain_format_json
```

**JSON 구문 진단 (NO_JSON 한정, opt-in).** parse_stage=0 회복 시 `loop._recover_unparsed` 가 `wire_format.diagnose_syntax_error(llm_text)` 를 호출 — 결과(메시지 + line/column + 캐럿)가 있으면 `format_no_json_retry(syntax_error=…)` 가 framing 바로 다음에 끼워 모델에게 *정확히 어디서* 깨졌는지 알려줌(`primitives_applied` 에 `diagnose_json_error` 기록). 표준 `json.JSONDecodeError` 가 이미 가진 위치 정보를 살리는 것(라이브러리 추가 0). `{`/`[` 로 시작하는 실제 JSON 시도일 때만 발화 — 프로즈/빈 출력은 None 이라 generic 힌트·`primitives_applied=[]` 경로 그대로(A1b 무영향). `repair_json`+strict=False 폴백이 상당수를 이미 자동 수리하므로 효과는 그 잔여 NO_JSON 케이스에 한정. 캐럿 line/col 은 추출된 JSON 후보 기준(펜스/프로즈 없는 단일라인 flat 케이스는 원문과 일치, 펜스 있으면 절대 line 에 오프셋이나 국소 캐럿이 자가-위치확인). 진단 포매팅은 `wire_formats/_json_diag.describe_json_error`(순수 JSON 유틸), 후보 추출은 각 포맷의 `diagnose_syntax_error`.

**v1 design — content-only echo.** thinking 채널 echo는 격리 측정값 없이 runtime 의존성만 유발하므로 v1에서 제외. Step 2 observability (TurnRecord JSONL) 데이터로 필요성이 검증되면 별도 primitive로 추가. (자세한 결정 배경은 `docs/robust-harness/DESIGN.md` §2.2.)

`prior_content`가 비면 정적 fallback (`RETRY_HINT_NO_JSON` / `RETRY_HINT_NO_ACTION`) — graceful path.

**라벨 우선순위 — DEGENERATE 가 parse_stage 보다 먼저.** degeneration(wire shape 반복 runaway)은 **생성-레벨 병리**(스트림이 안 끝남)라 파싱 실패라는 *증상*보다 논리적으로 앞선다. 그래서 `_handle_text_path` 는 `wire_format.is_degenerate(llm_text)` 를 **parse_stage 검사 이전에** 평가 → 마크다운 runaway(`## Thought/## Action` 빈 블록 반복)는 JSON 이 없어 parse_stage 0 이지만 `FAILURE_NO_JSON` 이 아니라 **`FAILURE_DEGENERATE`** 로 정확히 집계된다(더 구체적 원인). 빈 출력은 `is_degenerate("")=False` 라 아래 NO_OUTPUT 로 떨어진다. **회복 경로는 `turn.parse_stage` 로 구동되므로**(`_recover_unparsed`) 라벨 재배치는 telemetry(turns.jsonl)만 정확하게 할 뿐 동작 불변. (이전엔 is_degenerate 가 parse_stage 0 *뒤* 에 있어 md_array runaway 가 NO_JSON 으로 가려졌음 — react 는 `is_degenerate=False` 라 무영향.)

**A1a (NO_JSON) vs A1b (NO_OUTPUT) 라벨 분리.** parse stage 0 실패(=degenerate 아님)는 두 가지 운영 모드가 섞여 있음 — (a) 모델이 *내용은 있는데* JSON 형식에서 드리프트 (YAML 키, prose, code fence 등), (b) 모델이 *아무것도* 안 뱉음 (whitespace-only). `loop.py`의 `_handle_text_path`가 `llm_text.strip()` 검사로 둘을 분리해 `failure_signal` 을 `FAILURE_NO_JSON` 또는 `FAILURE_NO_OUTPUT` 으로 기록. 회복 경로는 동일(둘 다 `format_no_json_retry`) — A1b는 echo 대상이 없어 자연스럽게 정적 fallback path로 떨어지고 `primitives_applied=[]` 가 됨. 라벨 분리의 목적은 *관찰성*이며, 두 모드가 회복률 분포에서 어떻게 갈리는지 데이터를 모은 뒤 별도 primitive 도입 여부를 결정 (DESIGN.md §1, A1a/A1b).

**근거 (failure grounding):** 추상적 *"your response was invalid"*는 모델이 무엇을 위반했는지 모르게 함 — 같은 출력을 반복할 가능성 높음. retry에 자기 출력을 인용해 보여주면 모델이 자기 드리프트(YAML-style 키, 함수-호출 신택스, bare prose 등)를 직접 보고 self-diagnose 가능. 구조 마커가 보통 출력 시작 부분이라 head-truncate.

**Primitive 계약 (누더기 방지):** primitive는 provider/모델/채널 이름을 절대 참조하지 않음. 새 실패 모드는 *primitive 합성과 매핑 한 줄*로 처리 — `if "anthropic"`, `response.thinking` 같은 분기를 primitive 시그니처에 두면 invariant 위반.

**Prefix 호환성:** retry 메시지 시작은 항상 정적 템플릿과 같은 문장 (`"Your response was not valid JSON."` / `"Your JSON was parsed but has no action."` / `"Your JSON was missing the 'thought' field."`)으로 시작하므로 `SYSTEM_USER_PREFIXES` 매칭이 그대로 유지됨 → resume 시 자연어 변환에서 noise로 표시되지 않음.

**A7 (NO_THOUGHT) — mimicry-strengthening loop 차단.** parser 가 성공해 `action`은 있지만 `thought`가 비어 있으면(또는 `None`/whitespace-only) `_dispatch_text_path` 가 dispatch 직전에 차단하고 `format_no_thought_retry`로 retry. drift-shaped 응답 1건이 transcript에 들어가면 in-context learning 으로 이어지는 turn 들이 같은 구조를 mimicry해 thought-drop 이 연쇄로 번지는 패턴(일부 소형 모델)을 끊는 것이 목적. 정적 fallback 메시지 + echo path 모두 "Your JSON was missing the 'thought' field." 로 시작 — `SYSTEM_USER_PREFIXES` 에 동일 prefix 등록. constraint 메시지("must include 'thought' stating purpose / reason")는 builder 내부에 inline — primitive로 승격하면 v1 단일-caller 상황에서 anti-patchwork invariant ("primitive reused by ≥2 failures") 위반이라 두 번째 caller 등장 시점까지 보류. **예외: `complete` 액션은 검사에서 제외** — 최종 답 액션이라 reasoning slot이 next-turn 의무를 지지 않음. Phase 2 bakeoff (2026-05-18) 측정: 27b prefix_md `complete_direct`에서 5/5 unnecessary recovery + 평균 +3.1s latency, 35b는 영향 없음 (이미 thought 100% 채움).

#### Per-Turn Observability (`recovery/observability.py`)

`format_no_*_retry`는 단순 문자열이 아니라 `Intervention` (message + primitives 이름) 을 반환합니다. `_handle_text_path`는 try/finally로 매 턴 한 번씩 `TurnRecorder.record()`를 호출 — 성공/실패/예외 모든 경로에서 정확히 한 줄이 기록됩니다.

**스키마 (`TurnRecord`, `{session_dir}/turns.jsonl` 한 줄당 한 row):**
- `model` — 어떤 모델이 응답했는지 (분석 시 그룹 키)
- `timestamp` — ISO 8601 UTC. row 정렬 + `raw_failures.jsonl` 과의 join 키. (이전엔 `seq` 가 있었으나 제거 — TurnRecorder 인스턴스별 카운터라 web 이 run_loop 마다 새 recorder 를 만들면 세션 중간에 0 으로 리셋·충돌. 정렬/매칭은 append 순서 + timestamp 로.)
- `parse_stage` — 0(실패), 1(json.loads), 2(json_repair), 3(regex)
- `failure_signal` — `"NO_JSON"` / `"NO_OUTPUT"` / `"NO_ACTION"` / `"NO_THOUGHT"` / `"UNKNOWN_TOOL"` / `"SCHEMA_MISMATCH"` / `"NESTED_ENVELOPE"` / `"ACTION_LOOP"` / `null`
- `primitives_applied` — 합성된 primitive 이름 list (실패 retry 시에만 채워짐)

**프라이버시 계약:** `turns.jsonl`에는 사용자 prompt나 LLM 응답 본문이 절대 기록되지 않음 — 구조 메타만. (예외: 아래 raw_failures 디버그 캡처는 *별도* 파일로만.) 회복률은 *저장하지 않고* 분석 시 walk-forward로 계산 (실패 row 다음 row의 failure_signal을 봐서 회복 여부 판단). retrospective 쓰기 회피.

**활성화 조건:**
- `ctx is not None` (in-process subagent 의 일부 헬퍼 경로에선 ctx 미주입 → 비활성)
- `record_turns=True` (CLI: `--record-turns/--no-record-turns`, 기본 켜짐)

**raw_failures 캡처 (디버그 한정):** `AGENT_CLI_RECORD_RAW_FAILURES=on` (env only — CLI 플래그 없이 run/chat/web 모든 진입점 일괄, `AgentLoop.__init__`가 env fallback) 이면 **실패 턴**(`failure_signal≠null`)의 raw LLM 응답을 별도 `{session_dir}/raw_failures.jsonl` 에 기록 (`{timestamp, parse_stage, failure_signal, raw}`). **timestamp 는 같은 턴의 turns.jsonl row 와 정확히 동일** — `record()` 가 now() 를 1회만 호출해 양쪽에 공유하므로 두 로그를 timestamp 로 join 할 수 있다 (이 join 키가 이전 `seq` 를 대체). 기본 off. turns.jsonl의 메타-only 계약은 유지 — raw는 이 분리된 파일에만. 복구 규칙 강화 전 "어떤 형태로 드리프트했나"(마크다운/코드펜스/부분 JSON 등)를 보기 위한 분석용. intervention(재시도 요청)은 이미 history.jsonl의 observation entry에 남으므로 여기엔 raw 응답만.

**활용:** Step 3·4의 playbook 튜닝 데이터 누적이 주 목적. 분석은 별도 스크립트 (`jq`로도 충분). 자세한 설계는 `docs/robust-harness/DESIGN.md` §3.3.

#### B1 — Action Loop 감지 + 회복 (`recovery/detectors.py` + B1 playbook)

같은 `(action, args)` 호출이 연속 2회 이상이면 모델이 막힌 상태로 보고 단계적 개입을 발동합니다. 기존 hard-fail (`_detect_repeated_calls`)을 대체.

**Detector (`ActionLoopDetector`):** turn 간 stateful — `_last_signature`, `_consecutive_count`, `_fire_count` 보유. `observe(action, args, prev_was_error=False)`가 호출될 때마다 escalation level 반환:
- 0 — 임계값 미만 또는 error retry로 카운터 리셋
- 1 — 첫 발동 (probe_progress)
- 2 — 두 번째 발동 (restate_task)
- 3+ — 회복 소진, hard-fail

`prev_was_error=True`면 카운터 리셋 — 정당한 재시도 false-positive 방지. 다른 action이 끼면 카운터 리셋. Args canonicalization은 `json.dumps(sort_keys=True)` (dict 키 순서 무시).

**Playbook (`format_action_loop_intervention`):**
- Level 1: `probe_progress` — 가벼운 nudge ("이미 가진 응답을 다시 봐, complete 또는 다른 action 선택")
- Level 2: `restate_task` — 원본 task 재고정 + 진단 질문 ("task가 이 호출을 왜 필요로 하나? 못 얻고 있는 정보가 뭔가?")
- Level 3+: `None` 반환 — caller가 hard-fail (어떤 primitive를 시도했는지 에러 메시지에 포함)

**Temperature-down 컬럼 의도적 누락:** DESIGN.md §2.3에 명시된 escalation 컬럼 중 "+temp↓"은 v1에서 제외. provider별 temperature 처리가 다양해 primitive 계약(provider-agnostic) 위반 위험. Step 4에서 데이터 보고 재검토.

**감지 시점:** dispatch *전*. tool은 실행되지 않고, 모델은 다음 turn에 새 prompt(`probe_progress` 또는 `restate_task`)와 함께 같은 결정을 다시 내림. 중복 비용 0.

#### A4 / A5 — Pre-dispatch Detection (`recovery/detectors.py`)

LLM이 emit한 action·input이 도구 레지스트리·스키마와 안 맞을 때:

- **A4 (Unknown tool)** — `detect_unknown_tool(action, tools_list)` → `action not in tools_list`
- **A5 (Schema mismatch)** — `detect_schema_mismatch(action, action_input)` → `validate_tool_input` wrap, `(mismatched, error_message, normalized_input)` 반환

**감지 위치:** `_dispatch_text_path` 안, B1 detector → render_action 직후, `_dispatch_tool_with_hooks` 호출 직전. 모든 *pre-dispatch* 검사가 한 자리에 모임 (DESIGN.md §3.1 detection layer).

**처리:** 라벨링 + observation 주입 + dispatch 우회.
```
A4: outcome["failure_signal"] = FAILURE_UNKNOWN_TOOL
    Observation: "Unknown tool 'X'. Available: ..."
A5: outcome["failure_signal"] = FAILURE_SCHEMA_MISMATCH
    Observation: "Missing required field(s) for 'X': ... Expected: {...} Fix action_input and retry."
```

**v1은 라벨링만 — 별도 primitive 없음.** 이유: 현재 메시지(레지스트리·스키마 정보 포함)가 이미 grounding 역할 수행 중. 별도 primitive(`probe_tool_name`, `echo_diff` 등)가 *측정 효과*를 내는지는 TurnRecord 통계 보고 결정 (Step 4b).

**B1 detector와의 순서:** B1이 먼저 (A4·A5 무관 *반복 자체*가 더 큰 신호). 같은 unknown tool을 2번 emit하면 B1이 잡아 `probe_progress`를 줌 — A4 메시지가 무한 반복되지 않음.

**`_dispatch_tool_with_hooks` 내부 검증 제거됨:** Step 4a 전엔 `_execute_single_tool` 안에서도 `validate_tool_input` 호출 + `tool_name in tools_list` 체크가 있었음. 이젠 recovery 레이어가 단일 진실 원천 — 중복 제거. 2026-05-03: `tools/__init__.py:execute_tool`의 boundary 방어도 제거 + `_execute_tool`로 internal rename (REMAINING_DEBT.md #2/#3 청산).

**남은 부채:** 이 작업 중 *알면서 남긴* 부채는 `docs/robust-harness/REMAINING_DEBT.md`에 명시 기록.

#### A6 — Nested Envelope Detection (관찰 전용)

`complete` action의 결과 페이로드가 다시 `{"result": "..."}` JSON 객체로 래핑되어 들어오는 경우 — 일부 소형 모델에서 산발적으로 관찰됨. 사용자에게 `✅ {"result": "..."}` 같은 문자열이 그대로 표시되는 UX 회귀로 이어짐.

- **감지** — `detect_nested_envelope(result_value)` → `str` 인지 확인 → `lstrip().startswith('{"result"')` → `json.loads` 성공 → 결과 dict의 top-level `result` 키 존재. 한 단계라도 실패하면 false (오탐 방지).
- **위치** — `loop.py`의 `complete` 분기, `answer` 결정 *직후*. 라벨링만 하고 출력은 그대로 둠.
- **라벨** — `outcome["failure_signal"] = FAILURE_NESTED_ENVELOPE` (TurnRecord에 기록).
- **자동 unwrap 안 함 (의도적)** — 빈도·재현 모델 분포 측정 후 4b에서 결정. v1에서 unwrap을 하면 (a) 모델이 의도적으로 그렇게 답한 경우 데이터를 잃고 (b) anti-patchwork 원칙(측정 후 결정) 위반.

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

- **압축 비활성화**: `--no-compaction` 또는 `AGENT_CLI_COMPACTION=off` → 플레인 FIFO만 동작 (env가 flag보다 우선; 운영자 kill switch). **서브에이전트 전파**: `compaction_enabled` 플래그가 부모 `AgentLoop`→`tool_delegate`/`_run_single`/`_run_parallel`(delegate)과 `_handle_run_skill`→`execute_skill`(skill)의 `run_loop` 호출까지 스레딩됨 → `--no-compaction` 이 delegate/skill 서브에이전트에도 적용(이전엔 env 만 per-loop 체크로 전파되고 플래그는 main 만 끄던 비대칭을 제거). env kill switch 는 각 loop 의 `_compaction_enabled()` 가 독립 체크하므로 양쪽 다 일관
- **과대 도구 출력 캡 + nudge (loop `_tool_observation`, 결과→관찰 seam)**: 도구 관찰 토큰이 **`context_window // 10`**(loop `_oversized_cap`) 초과면 전체 출력을 컨텍스트·history 어디에도 안 넣고 **"좁히라"는 nudge**(`_render_oversized_nudge`)로 치환 — 호출 자체는 성공. 거대 출력은 추론 공간을 잠식해 품질↓ 이라 모델을 라인범위/심볼/`LIMIT`/`grep`/`tee→read_file` 같은 surgical 회수로 유도. 두 **도구별 추상화 표면**(`tools/base.py`)이 여기서 만남: `Tool.render_observation(result, args)`(결과→관찰 본문, 기본=성공 output·실패 error)와 `Tool.apply_oversized_cap`(기본 True). loop 가 `messages.append`·`ctx.add` **양쪽 전에** 최종 본문을 만들므로 둘이 일관 → **`add` 는 순수 저장**(spill 변환 없음). 비-도구 관찰(개입·unknown-tool)·사용자/어시스턴트 메시지는 캡 대상 아님. 관찰 렌더는 `_append_observation` 단일 지점(`render` 플래그; recovery 는 `render_recovery` 가 이미 렌더해 False), multi-op 은 flush 합본 1카드. (이전의 청크-spill 레코드 `{spill,output:[guide,chunk...]}` + read_context `json_extract` 회수 + read_file 3% 절단은 모두 제거 — 후속 분리 항목: 큰 write/edit action_input 본문의 매-턴 재공급 elide.)
- **요약 프롬프트 (agentic resume 지향)**: `_llm_compact_summarize` 의 system 프롬프트가 단순 4-clause 가 아니라 **구조화 섹션**(TASK/STATE/DONE/PENDING/DECISIONS/FAILURES/FACTS, 빈 섹션 생략)을 요청 — 에이전트가 요약만으로 작업을 이어가야 하므로 남은 작업(PENDING)·실패한 시도(FAILURES)·verbatim 식별자(FACTS: 경로/명령/에러문자열) 보존 + "transcript 에 있는 것만, 지어내지 말 것" 규칙 포함. 재귀 병합(이전 요약 + 신규 transcript)은 "same section headings" 로 구조 유지. (실세션 검증: Qwen3.6-27B 가 구조 준수 + 실제 `AttributeError` 실패를 verbatim 포착, 6.4K→2.3K자.)
- **Belt-and-braces**: LLM 요약 실패(`CompactionError`)나 재구성 후 캐시 미충족 모두 같은 FIFO 경로로 수렴 → 무한 트리거 루프 없음
- **Observability**: `TurnRecorder.record_compaction(tokens_before/after, evicted_count, fallback_used, failure_signal, duration_ms)` → `turns.jsonl`에 `event: "compaction"` 기록
- **UI**: `render_compaction_progress(phase, ...)` 단일 helper가 `_renderer.compaction(phase, ...)` 으로 위임 — CLI-vs-web 라우팅은 renderer 가 담당. **base `Renderer.compaction` 기본 구현**은 `status` 한 줄(start/done/warning)로 출력(CLI/minimal 그대로). **`WebRenderer.compaction` 은 override** 하여 전용 `compaction` SSE 이벤트(구조화 payload: phase/old_tokens/new_tokens/evicted_count/reason)를 emit → 프론트(app.js `compaction` 리스너)가 **대화창 인라인 시스템 라인**(`.card-sys`: start "압축 중…" → done "압축됨 X→Y tok" 갱신, warning) 으로 렌더. transient(재접속 시 미재생). (이전엔 helper 가 generic `status` SSE 를 쐈으나 프론트 리스너가 없어 웹에선 안 보였음 — 전용 이벤트로 수리.)
- **Scratchpad 없음.** history.jsonl이 대화 기록이자 artifact 인덱스
- **Context inject 없음.** LLM이 필요할 때 read_file로 pull
- System prompt에 Context Recovery Guide 포함
- 스킬/delegate는 부모 budget 상속

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
├── delegate_coder_f1a9_20260405T143230456/    ← delegate subdir
│   ├── history.jsonl                          ← delegate 내부 대화
│   └── result.md                              ← delegate 최종 결과
│
└── skill_summarize_d4e1_20260405T143200100/   ← skill subdir
    ├── history.jsonl                          ← skill 내부 대화
    └── result.md                              ← skill 최종 결과
```

- main: root에 flat artifact
- delegate/skill: subdir에 history.jsonl + result.md (재귀 중첩 가능)
- fork 모드: parent history.jsonl 복사 → delegate가 이어서 append

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
| `delegate` | in-process 서브에이전트 위임 (flat-native — 한 op=한 task, parallel_safe) | `task` 필수, `context?`/`tools?`/`agent?` | 구조화된 결과 (output + activity log + duration) + delegate subdir 경로. 여러 delegate op = 병렬 |
| `read_context` | 세션 이력 조회 | `mode`, `keyword`, `scope?`, `sessions?`, `loc?`, `range?` | **list**: 전체 세션 목록. **search**: 기본 현재 세션, `sessions="all"` 또는 ID로 확장; `scope`로 필드 필터 (reasoning/tool/observation/query); 결과 턴 블록 + preview 200자 cap + 50건 truncation + fetch hint footer. **fetch**: `loc='{session}/{path}:{line}'` (search 결과 그대로) 로 전체 턴 회상; `loc` 단일/배열 (max 10), `range` 0-5 (앞뒤 N턴). multi-line 보존, action_input compact JSON, all-or-nothing 시멘틱. |
| `fetch` | 웹 페이지 fetch → 마크다운 변환 | `url` | 재귀 링크 추출, 에러 힌트 |

**가상 도구** — loop.py if-cascade가 인터셉트해 직접 처리 (실제 tool dispatch 우회). LLM에게는 일반 도구처럼 노출 (시스템 프롬프트의 ``## Available Tools`` 섹션 포함):

| 도구 | 설명 | 필수 입력 | 비고 |
|------|------|----------|------|
| `complete` | 작업 완료 신호 | `result` | 루프 종료 |
| `ask` | 사용자에게 질문 | `questions` | 대화형 전용 (ctx 없으면 제거) |
| `run_skill` | 스킬 실행 | `name` | loop 레벨 인터셉트, skill subdir 생성 |

### 6.2 delegate agent 로딩

`delegate` 도구의 `agent` 파라미터로 사전 정의된 에이전트 역할을 로드할 수 있습니다:

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

**핵심 함수** (`tools/delegate.py`):
- `_validate_agent_name(name)` — 이름 검증 (`[a-zA-Z0-9_-]`만 허용)
- `_load_agent(name)` — 파일 탐색 + YAML frontmatter 파싱 → `(role_prompt, config, error)`
- `_extract_activity_log(messages)` — 컨텍스트 메시지에서 per-turn 액션 요약 추출
- `_summarize_action(action, action_input)` — 단일 액션을 한 줄 요약으로 포맷
- `_extract_last_actions(messages, n)` — 마지막 N개 액션 + 에러 observation 추출
- `_persist_delegate_result(formatted, delegate_dir)` — result.md를 delegate subdir에 저장
- `_format_delegate_output(result)` — DelegateResult를 구조화된 observation 문자열로 포맷
- `_AGENT_SEARCH_PATHS` — 검색 경로 리스트
- `_FRONTMATTER_PATTERN` — `---` frontmatter 정규식

**DelegateResult 필드**: `output`, `duration_secs`, `activity_log`, `last_actions`, `iterations`

**산출물 구조**: delegate 실행 결과는 다음 섹션을 포함:
1. 서브에이전트 출력 (output 또는 "(subagent returned no result)")
2. `[Subagent activity]` — per-turn 액션 로그 (최대 20개)
3. `[Last actions before failure]` — 실패 시 마지막 5개 액션 + 에러 힌트
4. `[Duration: Ns]` + `[Subagent used N turns]` — 실행 메타데이터
5. `→ delegate_{name}_{hash}_{ts}/` — delegate subdir 경로 (history.jsonl + result.md)

**적용 우선순위**: task에 명시된 `tools`/`model`이 agent 파일 설정보다 우선합니다.

**병렬 delegate lifecycle 통합 인터페이스** (`_run_parallel`):
- 각 worker thread 는 `renderer.begin_delegate_task(task_id, ...)` → `_run_single` → `renderer.end_delegate_task(task_id, ...)` 만 호출. 그 외 panel/capture 오케스트레이션 전부 renderer 책임.
- `task_id` 는 `delegate-{index}-{uuid4().hex}` (single 경로는 `delegate-single-{uuid4().hex}`). **thread id 가 아니라 uuid4** — `threading.get_ident()` 는 worker thread 종료 후 재사용되므로, 나중 delegate 호출의 worker 가 이전 호출과 동일한 id 를 받아 web 프론트 `ensureTaskGroup` 이 stale 항목에 short-circuit → 새 카드 미생성 버그가 있었음. uuid4 로 호출-간 유일성 보장. 프론트는 `delegate_task_end` 수신 시 `taskGroups[taskId]` 항목을 삭제(DOM 카드는 유지) → 전역 누적 방지 + stale 충돌 원천 차단.
- MinimalRenderer 가 첫 begin 에서 Live 영역 띄움 (`is_terminal` 체크 — non-tty 면 skip), 자체적으로 thread → task slot → capture buffer 매핑. emit (`thought`/`action`/`observation`) 마다 `set_thread_status` 로 카드 상태 업데이트, `_capture_line` 으로 버퍼 누적.
- 마지막 end 에서 Live 종료 + 각 task 의 captured 출력을 `┌─ 🦀 [N] agent: task` group 으로 wrapping 해서 replay (등록 순서).
- WebRenderer 는 같은 begin/end 마커로 SSE `delegate_task_start` / `delegate_task_end` 이벤트 emit, `_thread_to_task` 로 후속 emit 들에 task_id 자동 첨부 → 프론트가 collapsible card 로 routing.
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

**한 op = 한 편집 (flat-native, Step 3).** edit_file 은 `{path, op, pos, end?, lines?}` 단일 편집을 받는다 — 옛 `edits[]` **op-내 중첩** 배열은 제거됐다('op 안에 배열 중첩이 27B 90% 깨뜨림' 실측, DESIGN Exp 8). **같은 파일 다중편집 = 루프 레벨 배치로 부활(다른 형태).** 연속된 같은-path edit_file op 들을 loop(`_dispatch_edit_batch`)이 모아 `apply_edits_batch` 로 처리: 원본 1회 read → 모든 ref 를 그 원본 기준 해석 → 범위 겹침 사전거부(`_find_overlap`) → bottom-up 정렬 적용 → 1회 쓰기, **all-or-nothing**. 옛 안전장치(범위 겹침 거부·bottom-up 정렬)가 **op-내 배열이 아니라 flat op 의 루프 그룹핑**으로 돌아온 셈 — nested-array 함정을 피하면서 "앞 편집이 줄을 밀어 뒤 편집 ref 가 stale" 문제를 원본-기준 해석으로 제거(fuzzy 는 같은 줄번호 정규화 한정이라 드리프트를 못 잡으므로, 애초에 드리프트를 안 만드는 이 방식이 정답 — 외부 검증: NousResearch hashline·5-edit-strategies 벤치마크의 bottom-up). 비연속·다른 파일은 per-op.

### 6.5 Tool Output 전달 방식

Tool output은 **잘림(truncation) 없이 전체를 그대로** LLM에 전달합니다 — 단, 한 관찰이 **`context_window // 10`**(loop `_oversized_cap`)를 넘는 병적 대용량(예: 레포 전체 `find`, 전 심볼 `code_index` 덤프)이면 컨텍스트에 안 들이고 **"좁히라"는 nudge 로 거절**합니다(전체는 어디에도 보존 안 함 — 호출 자체는 성공; 모델이 라인범위/`LIMIT`/`grep`/`tee→read_file` 로 다시 받음). 한 메시지가 윈도우를 넘겨 압축을 깨뜨리는 걸 방지(§5.4 과대 출력 캡 참조). 도구별로 `Tool.render_observation`(결과→관찰 본문)·`Tool.apply_oversized_cap`(기본 True) 표면으로 제어. 이전에는 context window의 3% 비율로 잘랐으나(`tools/truncation.py`, 삭제됨) LLM이 불완전한 정보로 판단하는 성능 열화가 확인되어 제거했고, 그 뒤 청크-spill(history 보존 + `json_extract` 회수)도 거절-nudge 로 대체했습니다(spill 보관-회수 기계 제거 → 단순화). context가 budget의 90%를 넘으면 `context/manager.py`의 compaction이 oldest 절반을 LLM 요약으로 흡수하고, 실패/미충족이면 belt-and-braces로 FIFO drop이 메시지 단위로 떨궈냄.

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

### 6.5.1 Review-context builder (`_build_review_observation`)

> **NOTE (v4.4.0):** `ready_for_review` 가상 도구는 **제거**됐습니다 — 모델이
> complete 전에 자발적으로 호출하라는 self-review 도구였으나 실전 사용률 0
> (297레코드 세션 0회). 아래 컨텍스트 빌더(`_build_review_observation` /
> `_format_tool_calls_for_review`)는 **보존** — auto-review(v4.5.0, web `🔍 Review`
> 토글)가 reviewer 컨텍스트로 재활용(`review.py::build_reviewer_task`).

리뷰 컨텍스트 블록은 `_build_review_observation` (loop.py)이 합성합니다:
`--- ORIGINAL REQUEST ---` / `--- YOUR SUMMARY ---` / *(옵션)* `--- YOUR TOOL CALLS ---` /
`--- REVIEW INSTRUCTIONS ---` / `Format your review like this:`.

마지막 섹션은 모델이 자유 텍스트로 "Done" 한 줄 응답하지 못하도록
`Requirement N: ... → [DONE | MISSING]: ...` / `Decision: complete | continue` 출력 템플릿을
강제합니다. self-review가 *생성* 되어야 reasoning이 따라오는 작은 모델 특성에 맞춘 디자인.

`--- YOUR TOOL CALLS ---` 섹션은 `_format_tool_calls_for_review(ctx)`가 ctx의 raw
messages에서 assistant tool calls만 추출해 컴팩트하게 렌더 (`tool(k=v, ...)`)합니다.
virtual tools(`complete` / `ask`)는 제외. 30개 초과 시 최근 30개만
유지하고 `(last 30 of N)` 표기. 긴 string 인자는 40자로, 비스칼라 인자는 `<list>`/`<dict>`
타입 마커로 축약. 이 섹션이 도구 호출 사실 목록을 모델에 명시 노출해, Observation 본문이
context FIFO로 evict되어도 "내가 무슨 도구를 불렀는지"는 review 시점에 보존됩니다.
ctx가 None이거나 실제 도구 호출이 없으면 섹션은 생성되지 않습니다.

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
2. `parse_react()`가 `_strip_thinking_blocks()`로 블록 분리
3. 분리된 thinking 내용은 `ParsedAction.thinking`에 보존
4. 나머지 텍스트(JSON)만 파싱 → Stage 1 직접 성공률 향상

### 7.5 재시도 헬퍼 (`providers/http.py`)

두 프로바이더 모두 동일한 재시도 래퍼 `post_with_retry(requests.post, url, **kwargs)`를 거쳐 HTTP를 발송합니다. 목적은 on-prem LLM 서버(vLLM, LM Studio, omlx)에서 간헐적으로 발생하는 일시적 네트워크 오류 — 서버 재시작 직후의 `ConnectionError`, 첫 호출 시 모델 로딩이 늦어서 발생하는 `Timeout` — 을 사용자 레벨로 노출하지 않고 복구하는 것입니다.

**범위: pre-stream only.** `requests.post()` 호출 자체에서 발생한 예외만 재시도합니다. 스트리밍이 시작된 이후(즉 `requests.post(stream=True)`가 Response를 돌려준 뒤) 청크를 읽다가 발생한 오류는 재시도 대상 아님 — 이미 소비된 청크가 중복되면 LLM 출력이 깨지기 때문.

**재시도 대상 예외:**
- `requests.Timeout` (ConnectTimeout, ReadTimeout 포함)
- `requests.ConnectionError`
- HTTP 4xx/5xx는 재시도 **안 함**. `raise_for_status()`는 `post_with_retry` 반환 *뒤에* 호출되어 서버의 거절 응답을 그대로 caller로 전달.

**백오프:** 고정 1초 (지수 아님). on-prem 단일 사용자 전제라 rate-limit / thundering-herd 대책이 필요 없고, `ConnectionError` 직후 서버 부팅 마무리에만 약간의 헤드룸을 주면 충분. `Timeout`은 이미 긴 대기였으므로 추가 대기 효과는 작지만 해롭지도 않음.

**설정:**
- `AGENT_CLI_LLM_RETRY_ATTEMPTS` (기본 10, 최초 포함 총 시도 횟수; 0/음수는 1로 clamp)
- `AGENT_CLI_LLM_RETRY_DELAY` (기본 1.0초)
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
build_system_prompt(capabilities, active_tools, include_delegate, skill_stack, session_id, agent_role)
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
    │       - delegate ← Delegation Guide
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
        ├─ "Call stack: main → agent:reviewer → skill:plan"
        ├─ "Do not delegate to or invoke: reviewer, plan (already in call stack)"
        └─ 세션 내 변동 가능한 유일한 Recency 섹션 → 끝에 두어 앞 3개를 안정적
           KV prefix로 보존
    
    Role 선택 (Primacy 영역):
    - main: 기본 ROLE_PROMPT
    - delegate: Agent Role이 기본 Role을 대체
    - skill: parent의 Role 상속
```

---

## 10. 테스트 아키텍처

### 10.1 테스트 분류

| 분류 | 파일 수 | 테스트 수 | 실행 방법 |
|------|---------|----------|----------|
| 유닛 테스트 | ~69 | ~2030 | `pytest tests/` |
| omlx 통합 (E2E) | 2 | ~25 | `pytest tests/ -m omlx_integration` |

**omlx 통합 테스트:** `tests/test_integration_omlx.py` + `tests/test_integration_omlx_builtin.py`. 실 OpenAI 호환 omlx 서버를 대상으로 `run_loop`(질문/read/shell/write/edit/multi-step), `provider.call` ReAct 파싱, 런타임 capability 감지, 스킬 실행(fork·dynamic injection·allowed_tools·디렉토리 구조·bracket args), 훅(Pre/PostToolUse), delegate(none/fork), explorer 에이전트, plan 스킬, @agent dispatch를 검증. conftest fixtures(`omlx_provider`, `integration_model`, `model_capabilities`)는 서버 `/v1/models` 프로브로 가용성을 확인하고, 미가용 시 전부 skip → `pytest tests/`는 항상 green. 연결은 env(`OMLX_BASE_URL` 기본 `http://127.0.0.1:8000/v1`, `OMLX_API_KEY`, `INTEGRATION_MODELS` 기본 `Qwen3.6-27B-MLX-8bit`)로 override. 순수 로딩/프롬프트 검증은 유닛(test_builtin_skills/agents)에 있어 통합에서는 제외.

### 10.2 테스트 실행

```bash
# 전체 (유닛; 통합은 서버 미가용 시 자동 skip)
pytest tests/ -v

# 특정 모듈
pytest tests/test_react_parser.py -v

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
  --delegate-timeout 서브에이전트 타임아웃 초 (기본: 300)
  -v, --verbose     원시 LLM 응답 표시

  /sh <cmd>         LLM 없이 셸 명령 직접 실행
```

`run` 도 `web` 과 동일하게 세션/컨텍스트(compaction + FIFO fallback + history.jsonl + compaction.json)를 관리합니다. 완료 후 세션 ID가 출력되며 `web --resume <id>`로 이어서 작업할 수 있습니다 (compaction state는 `dynamic_start_index`로 복원되어 summarised tail과 중복 없음).

### 11.2 `web` — 대화형 브라우저 UI

```bash
agent-cli web [options]
  (run 옵션 + --host/--port/--token/--no-browser/--resume/--idle-timeout/--trust-local/--base-path). **`--base-path <prefix>`(경로 prefix 라우팅)**: 리버스 프록시가 `/<prefix>/*` → 이 인스턴스(+prefix strip)로 라우팅할 때 — 프론트 URL 전부 **상대경로**(`api/...`/`static/...`)이고 `index()` 라우트가 serve 시 `<base href="<prefix>/">` 주입(기본 `<base href="/">`=루트, 동작 byte-동일). 서버 routes 무변경(프록시 strip→`/api/...` 수신). 회귀 가드 `test_web_base_path.py`(serve 프론트에 절대 `/api`·`/static` 0). **`--trust-local`(loopback 토큰 면제)**: 신뢰된 로컬 게이트웨이(127.0.0.1 바인드 인스턴스를 프록시·인증) 뒤에서 게이트웨이가 토큰을 매 요청 주입 안 하게 — pure-ASGI `_TrustLocalMiddleware` 가 `server.is_trusted_client(host)`(trust_local AND peer∈{127.0.0.1,::1}) 면 `_with_token_query` 로 유효 토큰을 query 에 주입(기존 endpoint 토큰검사 통과). 끈 상태/비-loopback 은 byte-동일(토큰 그대로). 브라우저 자동 오픈은 `_is_local_bind(host)`(loopback/wildcard) 일 때만 — 특정 IP(원격 bind)면 생략하고 URL 만 출력(서버에서 브라우저 무의미). **`--idle-timeout N`(self-reap)**: 외부 오케스트레이터(게시판류)가 인스턴스를 온디맨드로 띄우고 회수 안 하게 — N초 동안 비활성이면 스스로 종료(다음 접속 `--resume` 재기동). 순수 결정 로직 `web/idle.py::IdleMonitor`(clock 주입, 단위테스트) + web() 의 데몬 폴링 스레드가 `tick()` → `server_obj.should_exit=True`(기존 finally 가 teardown+세션저장). **활성(=안 죽임) = `renderer.has_live_connections()` OR `renderer.worker_is_busy()` OR `server.pending_count()>0`** — busy(작업/질문대기) 면 mid-task 회수 안 함. 기본 0=비활성(하위호환). **인스턴스 파일 (`web/instance_file.py`)**: web 시작 시 `.agent-cli/sessions/<id>/web.json`(`{session_id, host, port, token, pid}`)을 기록하고 finally 에서 제거 — 외부 오케스트레이터가 "이 세션 web 떠 있나/어디로" 를 파일 하나로 알아 spawn-or-attach(pid 죽었으면 stale→재spawn). 순수 write/read/remove(서버 의존 0). idle-timeout(self-reap)+제거가 짝이라 오케스트레이터는 프로세스 추적·kill 불필요.

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
- **Agent-local shell hook**: 에이전트 정의 파일(`.agent-cli/agents/*.md`) frontmatter의 `hooks:` 섹션 — 해당 에이전트로 delegate 되는 동안만 적용되는 로컬 matcher. skill과 동일한 merge 계약: `merge_hooks_configs(parent, agent.hooks)`로 부모 훅 뒤에 덧붙여 fire.
- **Delegate 전파**: `tool_delegate`가 `hooks_config`를 subagent `run_loop`에 그대로 전달. 즉 전역/프로젝트/스킬 훅은 모두 상속되고, 에이전트 자신의 overlay까지 그 위에 얹힘.

### 14.2 라이프사이클 이벤트 (11개)

| 이벤트 | 시점 | 함수명 |
|--------|------|--------|
| OnSessionStart | 세션 시작 후 | `on_session_start(ctx)` |
| PreLLMCall | LLM 호출 직전 (매 턴) | `pre_llm_call(ctx)` |
| PostLLMCall | LLM 응답 수신 후 | `post_llm_call(ctx)` |
| PreToolUse | 도구 실행 직전 | `pre_tool_use(ctx)` |
| PostToolUse | 도구 실행 직후 | `post_tool_use(ctx)` |
| OnTurnEnd | 턴 종료 후 | `on_turn_end(ctx)` |
| OnDelegateStart | delegate 실행 직전 | `on_delegate_start(ctx)` |
| OnDelegateEnd | delegate 완료 후 | `on_delegate_end(ctx)` |
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
  │   │   ├─ OnDelegateStart / OnSkillStart
  │   │   ├─ 도구 실행
  │   │   ├─ OnDelegateEnd / OnSkillEnd
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
