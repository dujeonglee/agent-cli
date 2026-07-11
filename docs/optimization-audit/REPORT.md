# agent-cli 구조 분석 리포트 — 최적화 · 리팩터링 (2026-07-11)

3-축 병렬 탐색(loop / tools+context / web+render+providers+wire) 후 헤드라인 주장 전수
교차검증(✅=코드로 직접 확인). 전체 상태: 31.6k LOC, 테스트 2773, TODO/FIXME 0,
순환 import 는 지연 import 로 관리, 전역 가변 상태 2곳(shell/_confine 세션 allowlist).

## A. 버그 (최적화 이전에)

| # | 발견 | 위치 | 검증 | 상태 |
|---|---|---|---|---|
| **A1** | **delegate activity-log 조용한 기능 상실** — `_extract_activity_log`/`_extract_last_actions` 가 `json.loads(msg["content"])` 로 파싱하나, wire-format 리팩터(423608e) 이후 assistant 레코드는 `action`/`ops` 최상위 필드 + `content` 키 부재 → 매번 skip. `[Subagent activity]`·`iterations`·`[Last actions before failure]` 항상 빈 값 | delegate.py:111-221, 486-494 | ✅ | ✅ v4.35.1 |
| **A2** | **fetch 가 flat-native 아님** — `fetch_url`/`fetch_depth` prefix 잔존. "모든 builtin flat-native"(base.py:240, registry.py:94, 로드맵 ★완료) 문서와 모순. `claims`/`infer_action` 이 fetch 에만 live | fetch.py:250-263 | ✅ | ✅ v4.38.0 |
| A3 | `_build_review_observation` 프로덕션 dead (테스트만 호출) | loop.py:2472-2520 | ✅ | ✅ v4.35.1 |
| A4 | *(A1 파생, 분석 중 추가 발견)* `_format_tool_calls_for_review` 도 top-level `action` 만 읽어 멀티-op `ops` 레코드 누락 — 현 기본 포맷에서 review "YOUR TOOL CALLS" 섹션 항상 빈 값 | loop.py:2453 | ✅ | ✅ v4.35.1 |

## B. Code Optimization (임팩트 순)

| # | 항목 | 위치 | 비용 | 수리 방향 | 상태 |
|---|---|---|---|---|---|
| **B1** | **`get_messages()` O(n²)** — 매 턴 전체 히스토리를 `_to_natural_language`(assistant 는 wire render 포함)로 전량 재변환, 턴당 3-4회 호출. 유일한 초선형 패턴 ✅ | manager.py:233-276, loop.py:576/623/765/866 | 턴↑수록 악화 | 렌더 결과 증분 캐시(add 시 tail 만 변환, compaction 때만 재빌드) | ✅ v4.37.0 |
| **B2** | **web: async 핸들러 안 블로킹 I/O** — `workspace_tree` 요청마다 재귀 `rglob` 사이징, zip·rmtree·write 도 이벤트루프 위 → 모든 뷰어 SSE 정지 ✅ | server.py:1440,1477,1519,1554 | 요청당, 전 클라이언트 파급 | sync `def` 전환(FastAPI 스레드풀) 또는 `run_in_executor` | ✅ v4.36.0 |
| **B3** | **web: `_event_buffer` 무한 성장** — trim 없는 plain list, 재접속마다 전체 스냅샷 재직렬화 ✅ | render/web.py:117,233,284 | 세션 길이 비례 | `deque(maxlen)` 또는 디스크 폴백 | ✅ v4.36.0 |
| B4 | SSE 페이로드 뷰어당 재직렬화 (`json.dumps` × N viewers) | server.py:926,947 | 이벤트당×뷰어 | `_emit` 1회 직렬화 후 문자열 공유 | ✅ v4.36.0 |
| B5 | react 단일-op 턴 이중 파싱 — 3-stage 파스 후 classic 이면 `super().parse_turn`→`parse_react` 재파싱 ✅ | react.py:558-598→93 | LLM 턴당 1회 | 파싱된 `data` 로 Op 직조 | ✅ v4.39.0 |
| B6 | `ctx.add` 당 `mkdir`+open/close 동기 I/O(턴당 ≥2) + 같은 페이로드 `json.dumps` 2회 | manager.py:645-648,197 | 턴당 | 세션 파일핸들 유지, 직렬화 1회 | ✅(부분) v4.39.0 |
| B7 | 도구 호출마다 `frozenset`+`RunContext` 재생성(oversized 경로 2회) — 입력 불변 | loop.py:_run_ctx | 호출당 소액 | init 1회 캐시 | ✅ v4.39.0 |
| B8 | read_file 전체 읽기 파일 2회 split | read_file.py:259/282 | 읽기당 소액 | `format_hashlines_range` 재사용 | ✅ v4.39.0 |
| 기각 | ~~`estimate_tokens(self.system)` 매 턴~~ — `len//4` 는 O(1), 공짜 | token_estimator.py:6 | — | ❌ 에이전트 주장 기각 | — |

참고: 토큰 계정(`get_estimated_tokens`)은 O(1) 증분 + 서버 실측 재앵커로 잘 설계됨. B1 만 예외 지점.

## C. Architecture Refactoring (임팩트 순)

| # | 항목 | 근거 | 방향 | 상태 |
|---|---|---|---|---|
| **C1** | loop.py god-module(2671줄) — `_dispatch_op` ~390줄, `__init__` ~190줄, action-card 렌더 5중복, `_append_observation` 8곳 동일 인자. (정정: 순환의 뿌리는 registry eager TOOLS — delegate→run_loop 지연은 유지 필수, lazy registry 재시도 금지) | loop.py:1239-1626 | **Option 3 협력객체 3단 PR 확정** — PR-1 State/Config ✅ v4.40.0 → PR-2 SystemPromptSvc·ToolBridge ✅ v4.41.0 → PR-3 ✅ v4.42.0 → 패키지化 ✅ v4.43.0 | ✅ 완결 |
| **C2** | delegate.py 8책임(851줄) — agent 로딩/추출기/persist/포맷팅/단일·병렬 실행/dispatch/Tool | delegate.py | `delegate/` 패키지 분할 (agents/report/exec/tool) | ✅ v4.44.0 |
| **C3** | web/server.py 전송·비즈니스 뒤엉킴(1727줄, 27라우트) — directive 조작·lesson 학습·slash ~440줄 | server.py:168-408,493-687 | directives/inspector/slash 3모듈 추출 | ✅ v4.45.0 |
| **C4** | main.py `run`/`web` 부트스트랩 중복(web 커맨드 546줄) | main.py:749,1188 | 공용 추출 + 실측서 발견 3결함 수리 | ✅ v4.46.0 |
| **C5** | ContextManager 8관심사(1053줄) — 캐시+압축+요약+2종 영속화+분류+NL렌더 | manager.py | 렌더/분류 → `context/_render.py`, 영속화 store 분리. B1 과 연계 | ☐ |
| C6 | providers 스트리밍 파서 중복 — 양 어댑터 `_handle_stream` 각 ~130줄 (retry 는 공유됨) | anthropic.py:96, openai.py:122 | SSE 이터레이터+델타 누산기 http.py 공유 | ☐ |
| C7 | 이중 검증층 — registry 중앙 + 도구 내부 재검증 | registry.py:293-377 | `Tool.validate(args)` 훅 1-pass | ☐ |
| C8 | Renderer ABC 50메서드/17추상 — fat 아님, 신규 renderer 진입장벽만 | render/base.py | 코어+mixin 분리 (낮은 우선순위) | ☐ |

### C9. Latent seam 재고 (과거 결정 존중)

- `render_action_input_for_context`+`_context_view`: override 0 확인. 단 v3.16.1 mimicry revert 후 의도적 보류 seam — "보류 유지 vs 착수 vs 제거" 결정 사안.
- registry lazy 화: **시도→롤백 이력** (순환 뿌리가 registry→context-tool→context.manager 라 lazy 로 안 닿음). 재시도 금지, C1 이 정공법.
- `_MULTI_OP_DESC_REWRITES={}`·prefix 머신러리: 문서화된 의도적 latent — 유지. 단 A2 가 "latent" 주장을 현재 거짓으로 만듦.

## D. 실행 순서 & 진행 로그

1. ✅ **A1 delegate 추출기 수리** (+A3 dead code 제거, +A4) — 버그 수리 (v4.35.1)
2. ✅ **B2+B3+B4 web 안정성 묶음** — 사용자 체감 직결 (v4.36.0)
3. ✅ **B1 get_messages 증분 캐시** — 유일한 O(n²) (v4.37.0)
4. ✅ **A2 fetch flatten** — 로드맵 완결 (v4.38.0)
5. ✅ **B5+B6(부분)+B7+B8 잔여 perf 소품 묶음** (v4.39.0)
6. ☐ C1/C2/C3 구조 분할 — 각각 독립 PR, 필요 시

### 진행 로그

- **2026-07-11 · v4.47.0 · C5 완료 (+fsio 저장 패턴 통일)**: manager
  1,130→787줄 — records(121, shape 계약)/render(219, 무상태 실측 확인)/
  store(95, I/O primitive). store 경계는 A/B 상세 비교로 함수형 확정
  (_dynamic_start_index 소유권 역전 → 상태-소유 클래스 성립 불가).
  **fsio.py 신설**로 전 저장 지점 3패턴 수렴: 원자 교체(compaction 고정
  tmp 교정·web.json 비원자 결함 발견-수리·status 수렴·config/memory/
  meta/DIRECTIVE/presets) + 가드 append(turns.jsonl 사전 mkdir 제거) +
  직접 쓰기(도구 산출물, 의도적 비수렴). 동시 4-writer 레이스 회귀
  테스트 포함 fsio 9종 + 분리 증명. 전체 2835 passed.

- **2026-07-11 · v4.46.0 · C4 완료**: 실측이 드러낸 결함 3건 수리 —
  ① web MCP 미배선(단순 누락 확정) → run 동형 배선(_setup_mcp/TOOLS.update/
  run_loop·capture_startup mcp_manager/shutdown disconnect), ② 예산 폴백
  run·web 상이 → `compute_token_budget`=(context×7)//10 단일 출처 통일,
  ③ run 에 --resume 부재 → 추가(명시 플래그만; unknown fail-fast).
  그 위에 공용 추출: `SessionBootstrap`+`_bootstrap_provider`+
  `_load_resume_session`+`_build_context` — 3결함 수리로 두 경로가 동형이
  되어 추출이 봉합-추상화가 아닌 진짜 공통부 명명. 유닛 6종 + 실기동
  e2e(run 생성→run --resume 이전 대화 참조 정답, web 기동+health).
  전체 회귀 2826 passed.

- **2026-07-11 · v4.45.0 · C3 완료**: server.py(1772) → 전송 전용(1228) +
  directives(280 — FastAPI 무의존, 테스트 가드)/inspector(91)/slash(222).
  재수출 셔임 없이 import 전부 이관(소비자 전부 repo 내부 — main 2곳 +
  테스트 43곳 소유권 지도 기반 기계 재작성, 문자열 patch 0 실측).
  F821→F401 2단 검증. 분리 증명 테스트 3종(단방향·무의존·단독 왕복).
  전체 회귀 2820 passed.

- **2026-07-11 · v4.44.0 · C2 완료**: delegate.py(838줄) → `tools/delegate/`
  4모듈(agents 67/report 249/exec 474/tool 114 + __init__ 재수출).
  F821→F401 2단 검증. `_reset_agent_loader` prod 삭제(테스트는
  `agents._agent_loader` 직접 교체 + conftest autouse 복원 — 구식부터
  있던 순서-의존 누수 클래스 근본 수리). `_BUILTIN_AGENTS_DIR` __file__
  깊이 보정. patch 재타게팅: _run_single×3·_load_agent×3→exec,
  `_agent_loader` prod 소비 2곳(main/system_prompt)→agents 모듈 직접
  (재수출 alias 는 stale-바인딩 함정이라 의도적 미제공). 패키지 표면
  테스트 4종. 전체 회귀 2817 passed.

- **2026-07-11 · v4.43.0 · C1 패키지化 (완결)**: loop.py(2954줄) →
  `agent_cli/loop/` 9모듈(state/prompt/tool_bridge/llm/dispatch/
  skill_invoke/core/run/__init__, 최대 dispatch 1156줄). 전 모듈 동일
  import 헤더 후 F821(누락 검출)→F401 --fix(미사용 464건 정리) 2단 검증.
  `_normalize_input` 은 소비자가 bridge 뿐이라 이주(순환 회피).
  __init__ 재수출로 외부 표면 무변경; 테스트 patch 재타게팅 12곳
  (render_step→dispatch/core 사용-모듈 구분 포함). 전체 회귀 2813 passed.

- **2026-07-11 · v4.42.0 · C1 PR-3 (3/3)**: `TurnDispatcher`(790줄 디스패치
  클러스터 + `loop_detector` 전유·`_task_text` 이주) · `LLMCaller`(_call_llm/
  압축요약콜 + `overflow_retries` 전유) 승격 — 재배선 leftover 0 검증.
  `_dispatch_op` 389줄 → 라우터+4헬퍼(`_op_complete`/`_op_ask`[무질문
  `_NOT_HANDLED` 폴스루]/`_op_run_skill`/`_op_execute_tool`) 분해.
  `_CONTINUE`/`_RETRY` 모듈 상수화. review 헬퍼 → review.py 이관(지연
  import 1개 소멸; `_ECHO_FINAL_RE` 동반 이동 사고는 회귀로 즉시 검출·복원).
  협력자 단독 unit 6종 + 전체 회귀 2813 passed. **파일 물리 분할(패키지化)
  잔여** — 5클래스 단일 모듈이라 patch 표면 무변경 이점, 분할은 별도 후속.

- **2026-07-11 · v4.41.0 · C1 PR-2 (2/3)**: 교차호출-0 실측 클러스터 2종 승격 —
  `SystemPromptSvc`(sections/system 소유, rebuild/apply_hook_sections)·
  `ToolBridge`(hooks·invoke·RunContext·관찰 seam + `_oversized_cap`/
  `_run_ctx_cache`/`recent_tool_history` 전유). 의존은 cfg/state/ctx/provider
  명시 주입 — 이동 블록 321줄의 self 참조를 필드 지도 기반 기계 재배선(잔여 0
  확인). AgentLoop 는 thin 위임+property 로 표면 유지(호출부·테스트 patch
  무변경 — `agent_cli.loop._execute_tool` 등 모듈 전역이 같은 모듈이라 유효).
  단독 생성 단위테스트 2종(승격 보상 증명). bare 헬퍼 1곳 브리지 조립 이관.

- **2026-07-11 · v4.40.0 · C1 PR-1 (1/3)**: Option 3(협력 객체) 확정 후 1단계 —
  `LoopConfig`(frozen, 불변 배선 ~24필드)·`LoopState`(공유 가변 7필드) 도입,
  `__init__` 이 두 객체를 조립하고 기존 `self.X` 표면은 property 브리지
  (config=읽기전용/state=RW)로 유지 → 메서드 본문 무변경, 테스트 이관은
  bare-`__new__` 헬퍼 1곳뿐. 상태 실측 지도(공유 6+2 / 전유 / 불변)가 근거.
  다음: PR-2 교차호출-0 클러스터(SystemPromptSvc·ToolBridge) 승격.

- **2026-07-11 · v4.39.0 · B5+B6(부분)+B7+B8 완료**: (B5) react classic 경로가
  parse_turn 자신의 3-stage 파스 결과 dict 로 Op 직조 — 같은 텍스트 2회 파싱
  제거, repair `truncated` 플래그 Op 보존, old-path 동등성 테스트(정상·drift·
  repair·완전실패 케이스) 고정. (B6 부분) history append 의 세션dir 가드
  `mkdir` 를 실패 시에만(FileNotFoundError→mkdir+재시도)으로 — add 당 stat 1회
  절약, 외부 wipe 복구 의미 보존. **파일핸들 유지·dumps 공유는 의도적 비채택**:
  fd 유지는 unlink 후 소리 없는 유실(가드 목적 훼손), estimate 와 record 는
  서로 다른 payload 라 직렬화 공유가 추정 왜곡 — 잔여분 수용하고 종결.
  (B7) `_run_ctx` lazy 1회 캐시(세 입력 init 후 불변 확인; per-call 가변 필드
  등장 시 캐시 제거 주석). (B8) full read 가 기존 split 라인 리스트 재사용 —
  출력 바이트 동일 테스트(엣지 5종) 고정.
- **2026-07-11 · v4.38.0 · A2 완료**: fetch 스키마 flat `{url, depth?}` 전환 —
  마지막 prefixed builtin 제거로 "모든 builtin flat-native" 불변식이 실제로
  참이 됨(`claims` 전 builtin False, prefix 머신러리 완전 latent).
  `wrap_single_op`=identity 추가; `strip_prefix` 가 레거시 `fetch_url`
  emission(구 세션 재공급 prior) 계속 관용 — 구 세션 resume 무해(재검증은
  신규 emission 만 대상). over-cap nudge 문구 `fetch_depth`→`depth`.
  테스트: flat 스키마/identity/claims False(전 builtin 불변식)/레거시
  strip 관용/legacy-key render_oversized 관용 5종 + oversized 픽스처 flat 화.
- **2026-07-11 · v4.37.0 · B1 완료**: `_nl_cache` 증분 렌더 미러 —
  `add` 가 record 렌더를 1회 수행해 append, `get_messages` 는 head(system/
  summary/file_list) 재조립 + 미러 포인터 복사. 벌크 변형 4곳(`_compact`
  재할당·`_evict_fifo` pop·`force_fit` pop·`_restore_cache`) `None` 무효화 +
  길이 불일치 backstop. 호출자(변형 없음 사전 전수 확인: loop 은 append 만,
  provider 는 읽기 전용) 무변경 — 투명 최적화. 렌더=record 순수함수 전제를
  코드 주석에 명시(미래 턴-의존 context view 는 무효화 필요). 테스트:
  전량 재렌더 동등성/렌더 1회 카운팅(3×get 에 추가 렌더 0)/FIFO 무효화/
  resume 복원/반환 리스트 독립성 5종.
- **2026-07-11 · v4.36.0 · B2+B3+B4 완료**: (B2) `workspace_tree`(재귀 rglob
  사이징)·`download`(zip)·`delete`(rmtree, 경로검증은 사전)·`upload`(쓰기)·
  `export_html`(렌더) 블로킹 본문을 `run_in_executor` 오프로드 — async 핸들러가
  이벤트루프를 막아 전 뷰어 SSE 가 정지하던 문제 해소(`_gen_directive` 선례 패턴).
  (B3) `_event_buffer` → `deque(maxlen=_EVENT_BUFFER_MAX=5000)`; 재접속 재생이
  넘치면 snapshot 에 `transcript_truncated {omitted:N}` + app.js 노티스 카드
  (전체 기록은 history.jsonl 보존, `_persistent_count` 는 총계 유지). (B4)
  `_emit` 이 payload 를 1회 직렬화해 dict 서브클래스 `_JsonReady.json_str` 에
  캐시 — SSE generator·재접속 replay 가 재사용, queue/buffer 의 (event, dict)
  shape 은 보존이라 테스트 churn 0(합성 identity/sticky/viewers 는 fallback
  dumps). 테스트: 버퍼 캡+notice/캡 미만 무notice/캐시 일치/replay 캐시/합성
  plain-dict 5종 신규, deque 비교 단언 1건 조정.
- **2026-07-11 · v4.35.1 · A1+A3+A4 완료**: `manager.iter_record_ops(record)` 신설
  (멀티-op `ops` + 단수 legacy `action` 양쪽 shape 의 단일 reader, `_classify_record`
  옆) → delegate 추출기 2종과 loop `_format_tool_calls_for_review` 가 소비.
  `json.loads(content)` 파싱 전면 제거(+`import json` 정리). `_build_review_observation`
  49줄 + 테스트 2클래스 삭제(실 리뷰 경로는 `review.build_reviewer_task`).
  테스트: 픽스처를 실제 저장 shape 으로 교체(구식 JSON-in-content 픽스처가 버그를
  가려온 원인), 멀티-op 조인/단수 legacy/drift skip/actionless stub/terminal complete
  케이스 + `iter_record_ops` 단위 7종 + review ops-shape 회귀 2종 신규.
