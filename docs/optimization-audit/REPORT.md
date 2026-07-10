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
| B5 | react 단일-op 턴 이중 파싱 — 3-stage 파스 후 classic 이면 `super().parse_turn`→`parse_react` 재파싱 ✅ | react.py:558-598→93 | LLM 턴당 1회 | 파싱된 `data` 로 Op 직조 | ☐ |
| B6 | `ctx.add` 당 `mkdir`+open/close 동기 I/O(턴당 ≥2) + 같은 페이로드 `json.dumps` 2회 | manager.py:645-648,197 | 턴당 | 세션 파일핸들 유지, 직렬화 1회 | ☐ |
| B7 | 도구 호출마다 `frozenset`+`RunContext` 재생성(oversized 경로 2회) — 입력 불변 | loop.py:_run_ctx | 호출당 소액 | init 1회 캐시 | ☐ |
| B8 | read_file 전체 읽기 파일 2회 split | read_file.py:259/282 | 읽기당 소액 | `format_hashlines_range` 재사용 | ☐ |
| 기각 | ~~`estimate_tokens(self.system)` 매 턴~~ — `len//4` 는 O(1), 공짜 | token_estimator.py:6 | — | ❌ 에이전트 주장 기각 | — |

참고: 토큰 계정(`get_estimated_tokens`)은 O(1) 증분 + 서버 실측 재앵커로 잘 설계됨. B1 만 예외 지점.

## C. Architecture Refactoring (임팩트 순)

| # | 항목 | 근거 | 방향 | 상태 |
|---|---|---|---|---|
| **C1** | loop.py god-module(2671줄) — 순환 import 뿌리(지연 9곳 + delegate/skills/review 역-import), `_dispatch_op` ~390줄, `__init__` ~190줄, action-card 렌더 5중복, `_append_observation` 8곳 동일 인자 | loop.py:1239-1626 | 턴 실행 코어 분리; 헬퍼 추출은 저위험 선행 | ☐ |
| **C2** | delegate.py 8책임(851줄) — agent 로딩/추출기/persist/포맷팅/단일·병렬 실행/dispatch/Tool | delegate.py | `delegate/` 패키지 분할 | ☐ |
| **C3** | web/server.py 전송·비즈니스 뒤엉킴(1727줄, 27라우트) — directive 조작·lesson 학습·slash ~440줄 | server.py:168-408,493-687 | `directives_service.py`/`slash.py` 추출 | ☐ |
| **C4** | main.py `run`/`web` 부트스트랩 중복(web 커맨드 546줄) | main.py:749,1188 | `bootstrap_session()` 공용 추출 | ☐ |
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
5. ☐ C1/C2/C3 구조 분할 — 각각 독립 PR, 필요 시

### 진행 로그

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
