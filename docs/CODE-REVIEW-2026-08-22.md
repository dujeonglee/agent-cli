# agent-cli 코드 리뷰 리포트 — 2026-08-22

**범위**: `agent_cli/` 전체 (Python ~37.5K LOC + 프론트 ~7.3K LOC), 관점 4종 — 확장성 · 일관성 · 중복성 · 효율성.
**방법**: 서브시스템 6개(loop+main / providers+wire_formats / render+web / tools+subagent / context+지원모듈 / 프론트 정적자산)를 독립 리뷰어가 병렬 정밀 리뷰 → 고심각도 발견 7건을 별도 스팟체크로 **코드 재검증** → 교차 테마 종합. 발견 총 ~120건.

---

## 1. 요약 (Executive Summary)

전반 상태는 **건강한 골격 + 절반에서 멈춘 이행 + 조합-의존적으로 죽어 있는 안전장치**로 요약된다.

**강점** — 리뷰어 전원이 일치:
- 설계 의도가 코드 주석에 이례적으로 잘 문서화됨(결정 근거·실측·버전 이력).
- 인프라 수렴이 잘 된 곳들: `http.run_sse_stream`/`post_with_retry`(프로바이더 공용 스트림 골격), wire_formats의 self-register 레지스트리 + self-contained 불변식, `Tool` 베이스(스키마·디스패치·관찰·over-cap 정책 단일 소유), context 계층의 정책/표현/계약/디스크 분리, 프론트 `team_model.js`의 순수 함수 분리, CSS 토큰 규율(하드코딩 hex 0건).

**최우선 문제** — 설계 논쟁이 아니라 **지금 조용히 오동작하는 것들**(전부 코드로 재검증 완료):

| # | 문제 | 위치 | 효과 |
|---|------|------|------|
| P0-1 | `stop_reason` 어휘 미정규화 — 루프는 OpenAI `"length"`만 비교, Anthropic은 `"max_tokens"` 원어 통과 | `loop/core.py:818` ↔ `providers/anthropic.py:226` | Anthropic 경로에서 **출력 절단 가드 무발화** → 잘린 write_file/shell이 그대로 디스패치 |
| P0-2 | edit_file 동일파일 배치가 ToolBridge 우회(`apply_edits_batch` 직접 호출) | `loop/dispatch.py:336,500` | 배치 경로에서 **Pre/PostToolUse 훅·recent_tool_history 누락** |
| P0-3 | json_fc가 `Op.truncated`를 절대 설정 안 함(xml_fc만 전파) | `wire_formats/json_fc.py:682-691` | **기본 포맷에서 edit_file 절단 새니타이저 상시 무발화** |
| P0-4 | degeneration 조기종료 게이트가 `"#" in ev.text`(json_fc 시그니처) 하드코딩 | `providers/http.py:456-459` | xml_fc(`<tool_call>` 반복)에서 **러너웨이 조기종료 구조적 무발화** |
| P0-5 | `WebRenderer._pending_thought`가 인스턴스 단일 슬롯 | `render/web.py:192,1280,1301` | 병렬 delegate N개가 서로의 thought를 덮어써 **카드 오귀속**(CLI는 스레드 캡처라 무사) |
| P0-6 | 프론트 `el(tag, cls, html)` 3번째 인자가 무조건 innerHTML인데 실패 카드가 원문 미이스케이프 주입 | `app.js:974,993-995` | 모델 원문의 `<` **마크업 실행/렌더 파손** (같은 파일 606행은 이스케이프 — 규칙 불일치) |
| P0-7 | 파이썬 훅 시스템(`hooks/runner.py`) 프로덕션 미배선 — `HookRunner()` 생성 0, loop의 `_fire_hook` 7곳 전부 죽은 분기 | `hooks/runner.py:14` + `loop/run.py:50` | **광고된 확장점(.agent-cli/hooks/*.py)이 무동작**. 배선 or 제거 결정 필요 |


> **수리 현황**: 1묶음(v8.34.0) — P0-1·4·5·9 ✅ + P0-6 최소패치. 2묶음(v8.35.0) — P0-2·3 ✅. 3묶음(v8.36.0) — P0-8a ✅(캐시↔history 정렬 북키핑 `_cache_hidx` — 리뷰 시나리오를 수렴 테스트로 실증 후 수리; 단, resume 의 fold 재적용은 원래 설계에 있었고 단일 사이클은 수렴했음을 병기)·P0-8b ✅(`_token_scale` 실측/추정 환산)·P0-6② ✅(el=textContent + elHtml 명시 분리, 콜사이트 전수 감사). **P0 전체 완료** — P0-7 만 보류(사용자 결정). 4묶음(v8.37.0) — **T1 잔여 3건 + P1 퀵윈** ✅: 개입 5중복→`_intervene()` 통일+턴 계수 단일화(A4/A5 도 비계수), parallel_safe 크래시 트랩 봉인(`_PARALLEL_BATCH_ENGINES` 게이트→순차 폴백), thinking 오버라이드 공용 정책(`resolve_thinking_policy` — off 시 effort 잔존 수리·상충 조합 동형화), 인라인 `<think>` 격리 Anthropic 동형화(+다중 text/thinking 블록 누산), enable_thinking 재프로브 transport 공통화(Anthropic thinking-블록 재프로브), 부수 5건(`delegate` 문구 2곳·`save_config` 캐시·`validate_tool_input` 사본화·`$N` 두자리 버그·mcp devnull fd 누수). 5묶음(v8.38.0) — **T3 테마 본체** ✅: agent 모드 테이블화(`AGENT_MODES`+`_MODE_HANDLERS` — 5중 나열 소멸, 신·구 출력 20케이스 바이트 동일) + 도구 정책 선언화(`terminal`/`depth_gated`/`requires_handler`/`force_mount` 클래스 속성 — tools_list·턴 종결이 속성 파생, 60조합 등가성 매트릭스로 증명). 6묶음(v8.39.0) — **run/web 조립기** ✅: `agent_cli/runtime.py`(`AgentRuntime` 13키 단일 정의·registry/waker 조립 공용화·`teardown_session` 단일 소유), HEAD 3벌 dict 추출-대조 + CliRunner 실구동 경로 검증. **부수 발견·수리 3건**: ①web 채팅 턴·상주 에이전트의 디스크 훅(hooks.json) 미배선 — run 과 동형 배선으로 수리(디스크→루프 전 체인 기능 테스트 고정) ②skill 조기-반환 경로 registry 미종료+MCP 미해제 ③@agent 경로 MCP 미해제 — 전 종료 경로가 단일 finally 로 수렴.

**부가 P0급**(리뷰어 검증, 스팟체크 미수행이나 코드 인용 명확):
- 컨텍스트 회계 이중 결함 — 실측 재앵커 후 추정치 감산(단위 혼합, `context/manager.py:306` vs `:664` 등) + `fold_resolved_interventions`가 `_dynamic_start_index`를 안 올려 **resume 시 요약된 레코드 재혼입**(`:795`).
- 웹 헤더 중첩 판정 불일치 — delegate 서브루프가 `ready` 스티키를 서브 모델로 덮음(`render/web.py:1255` vs `minimal.py:285`).
- `agents_live.py` 락 규율 — `tm.queued` seq 발급 비원자(`:612`, 파일명·dedup 키 충돌 가능), 표시용 무락 상태를 idle-reap **제어** 판정에 사용(`:441,467`), MailWaker `_armed` check-then-set 레이스(`:1523-1531`).

---

## 2. 교차 테마 (관점별 종합)

개별 발견 ~120건은 6개 테마로 수렴한다.

### T1. "조합-의존 무발화" 안전장치 (일관성 × 최고 위험)
절단 가드·러너웨이 조기종료·훅·새니타이저가 **특정 프로바이더/포맷/경로 조합에서만 동작**한다(P0-1~4). 공통 원인: 경계 계약(stop_reason 어휘, degeneration 시그니처, truncated 신호)이 **명세 없이 기본 조합(OpenAI+json_fc 단건)으로 암묵 고정**된 것. 수리 방향도 공통 — 계약을 명시 어휘/플러그인 속성으로 승격하고 매핑 책임을 경계 소유자(프로바이더/wire format)에게 옮긴다.

> **테마 종결 (v8.37.0)**: P0-1~4(v8.34.0/v8.35.0) + 잔여 3건 — thinking 오버라이드 해석 공용화(`resolve_thinking_policy`), 인라인 `<think>` 격리 프로바이더 동형화, enable_thinking 재프로브 transport 공통 2단계 계약 — 까지 전부 수리됨. "조합에 따라 안전장치/기능이 갈리는" 발견은 남아있지 않다.

### T2. 절반에서 멈춘 리팩터 이행 (확장성)
- `loop/core.py:254-449` — property 34 + setter 12가 순수 위임 shim("PR-2/3에서 소멸" 예고 후 잔존). 파라미터 28개가 run_loop→`__init__`→LoopConfig→property 4중 복제.
- `wire_formats/base.py` — ABC는 `parse` 추상/`parse_turn` 파생인데 내장 플러그인 둘 다 정반대 구현. `_format_rules_builder`는 프로덕션 호출 0에 내용("Exactly ONE action")이 현행 멀티-op 규칙과 모순.
- `hooks/` — 파이썬 훅 미배선(P0-7), 셸 훅은 별도 경로, `_run_shell_hooks`는 빈 스텁.
- `render/base.py` — ABC 추상 8 : no-op 30으로 "웹 기능 적재소"화.
→ 공통 처방: 각 이행을 **끝내거나 걷어낸다**(CLAUDE.md 7항). 남겨두면 새 코드가 사문을 모방한다.

### T3. 선언 없는 정책 하드코딩 (확장성)
- 도구 정책(턴 종결·가용성·병렬)이 Tool 클래스가 아닌 루프 문자열로 산재(`dispatch.py` 7곳+) — `parallel_safe` 선언만 있고 배선 없는 도구는 **런타임 크래시**(`dispatch.py:455-461`).
- agent 도구 모드 목록 5중 선언(스키마 enum·`_MODE_REQUIRED`·validate·브리지·`tool_agent`) — 이미 폴백 에러문에서 `run` 누락.
- 프로바이더 추가 = 5곳 수정(if/elif 2곳+transport+context-window 함수) — 자매 시스템 wire_formats는 self-register.
→ 처방: 정책을 **선언(클래스 속성/테이블)으로 승격**하고 루프·레지스트리는 선언을 읽기만.

### T4. 보일러플레이트 복제 (중복성)
대표 복제군: 개입 처리 20행 블록 5회(`dispatch.py`), 프로바이더 스트림 재연결 28행 2벌, capability 3-tier 탐지 3벌, runtime 13키 dict 3곳, 스킬 스코프 열기/닫기 2벌, sticky 래퍼 6종+GET/POST 4쌍 동형, JSON body 가드 6회(+1곳 누락으로 500), `prompt_user`/`confirm` 대기 블록 2벌, `.agent-cli` 프로젝트/사용자 경로쌍 6곳(순서·병합규칙 제각각), shell/fetch over-cap 흐름 2벌, 프론트 POST fetch 15회·클립보드 3벌·이스케이프 2벌(+미이스케이프 변종)·드로어 CSS 3벌. 각각 헬퍼/테이블/컨텍스트매니저 1개로 수렴 가능 — 개별 난도는 낮고 회귀 리스크도 국소적.

### T5. 상태·회계 규율 (일관성)
토큰 회계 단위 혼합 + fold 오프셋 정렬 상실(context), 무락 공유 카운터(`tm.queued`·`_armed`·`any_activity`), 스레드 공유 단일 슬롯(`_pending_thought`), `validate_tool_input`의 인자 제자리 변경, `save_config` 캐시 미무효화(형제 `save_model_entry`는 함). → "누가 이 상태를 소유하고 어떤 락 아래서 읽는가"를 필드 단위로 명시하는 정리 패스 1회 권장.

### T6. 전량 재계산 (효율성)
- **code_index**: 질의·write/edit마다 루트 전체 read_bytes+sha1(주석은 "한 파일 ~50ms"로 오도, `build()`에 path 인자 없음).
- **프론트**: `stream_chunk`마다 개요 전체 innerHTML 재조립 + 최근 14엔트리 마크다운 파이프라인 재실행; 스윔레인은 이벤트마다 전체 이력 rebuild(세션 누적 O(N²), `_events` 무상한).
- **context**: 압축/FIFO/resume마다 전 레코드 재직렬화(per-record 토큰 미러 부재).
- **agents_live**: 메시지 1건마다 agents.json 전체 원자 재작성; **memory.add**는 append 1건에 전체 JSONL 재작성(fsio에 `append_line`이 이미 있는데 미사용).
- **providers**: `acc.content += ev.text` O(n²) 문자열 누적, degeneration 검사 누적-전체 재스캔.
- **web server**: 디렉토리 나열마다 하위 전체 `rglob` 사이징; SSE 뷰어당 기본 executor 스레드 15초 점유(zip/scan과 풀 공유).

---

## 3. 우선순위 로드맵

**P0 — 실동작 수리 (각각 소규모 패치, 즉시 가치)**
1. `LLMResponse.stop_reason` 정규화 어휘 정의 + 프로바이더 매핑 (P0-1)
2. edit 배치 → 브리지 경유(훅·이력 회복) (P0-2)
3. json_fc `Op.truncated` 전파 (P0-3)
4. degeneration 게이트를 `WireFormat` 속성으로 (P0-4)
5. `_pending_thought` → 스레드 키 dict (P0-5)
6. `el()` textContent 고정 + `elHtml()` 분리 (P0-6)
7. 훅 배선 or 제거 **결정** (P0-7)
8. context fold 오프셋 정렬 + 토큰 회계 단위 통일
9. `agents_live` seq/armed/판정 락 정리

**P1 — 구조 부채 (확장 비용 절감, 중간 규모)**
- ~~도구 정책 선언화(`terminal`/`requires_handler`/`depth_gated` 클래스 속성) + agent 모드 테이블화~~ ✅ v8.38.0 (+`force_mount`; 엔진 바인딩(edit_file 같은-path 배치·agent 병렬 엔진)은 의도적으로 엔진 코드 옆 잔존 — 속성이 배선을 거짓말하면 parallel_safe 트랩의 재판)
- core.py property 브리지 소멸 + 파라미터 4중 복제 해소(LoopConfig 직접 전달)
- ~~개입 처리 5중복 → `_intervene()` 단일 헬퍼(+ 턴 계수 규칙 통일)~~ ✅ v8.37.0
- ~~run/web 부트스트랩·teardown 조립기 추출(runtime dict 3중·종료경로 4갈래 해소)~~ ✅ v8.39.0 (`agent_cli/runtime.py`: `AgentRuntime`+`build_agent_registry`/`wire_agent_mail`+`teardown_session`; **부수 발견·수리 3건** — web 채팅 턴·상주 에이전트 디스크 훅 미배선(run 만 배선돼 있었음), skill 조기-반환 경로 registry/MCP 미정리, @agent 경로 MCP 미해제)
- 프로바이더 self-register + capability transport 흡수; wire ABC `parse_turn` 1차화 + 사문 빌더 제거
- Renderer ABC 코어/옵셔널 프로토콜 분리; sticky+엔드포인트 선언형 레지스트리
- `.agent-cli` 경로쌍 `scoped_paths()` 단일화(+훅 병합 규칙 통일)
- 프론트 ES 모듈 분할(`type="module"` — 빌드 불필요) + `postJson`/`escapeHtml`/클립보드 util 단일화 → 테스트의 소스 스크레이핑도 함께 소멸

**P2 — 효율 (측정 후 순차)**
- code_index 증분 갱신 API(post_hook에 path 전달) — 최대 항목
- 개요 스트리밍 증분 렌더(활동 스트립만 갱신) + 스윔레인 증분 build/버퍼 상한
- context per-record 토큰 미러; agents.json 저장 디바운스; memory.add → `append_line`
- provider content 리스트 누적 + degeneration 윈도우 검사; SSE 대기 asyncio화; 디렉토리 사이징 on-demand

**부수 정리(낮음)**: loop 8파일 동일 docstring·고아 주석, `DEFAULT_TOKEN_BUDGET` 사본 2, `tool_calls`/`prefill` 등 소비자-0 훅, `${SESSION_ID}` 상시 빈값, ~~`$N` 치환 11개+ 버그~~ ✅, ~~mcp devnull fd 누수~~ ✅, `${VAR}` 치환 env만 적용, deprecated `get_event_loop` 혼용, ~~존재하지 않는 도구명 `delegate` 안내 문구 2곳~~ ✅ (v8.37.0).

---

## 4. 서브시스템별 상세

> 표기: [심각도][관점] 위치 — 문제 / 개선안. 전체 발견 중 대표 항목. (P0/요약 중복분 제외)

### 4.1 loop/ + main.py
- [높음][확장성] `run.py:20-105`+`core.py:56-95`+`state.py:36-74` — 파라미터 28개 4중 복제 / LoopConfig 직접 전달로 진입점 축소.
- [높음][확장성] `core.py:254-449` — 위임 property 34+12 잔존 / 호출부 이행 후 브리지 삭제.
- ~~[높음][확장성] `core.py:141-154`+`dispatch.py` 7곳 — 도구 정책 문자열 하드코딩 / Tool 클래스 속성으로 승격.~~ ✅ v8.38.0 (terminal/depth_gated/requires_handler/force_mount — tools_list 구성·턴-종결 flush 가 속성 파생; 등가성은 신·구 알고리즘 60조합 매트릭스 테스트로 증명)
- ~~[높음][중복성] `dispatch.py:237-255,833-852,884-899,911-926,1010-1029` — 개입 블록 5복제 / `_intervene()` 헬퍼.~~ ✅ v8.37.0
- ~~[높음][일관성] 위 5곳 — A7/B1/NO_JSON만 `turn -= 1`, A4/A5는 계수 / "개입 비계수" 단일 규칙.~~ ✅ v8.37.0 (개입 전부 비계수 — B1 detector 가 반복 폭주 상한)
- ~~[중간][확장성] `dispatch.py:455-461` — 미배선 `parallel_safe` 도구 = 런 크래시 / 순차 폴백 or 등록 시 거부.~~ ✅ v8.37.0 (`_PARALLEL_BATCH_ENGINES` 수집 게이트 → 순차 폴백)
- ~~[중간][중복성] `main.py:1178,1976`+`tool_bridge.py:280` — runtime 13키 3중 / `AgentRuntime` dataclass.~~ ✅ v8.39.0 (HEAD 3벌 추출-대조로 키·값 매핑 등가 증명; run/web 의 compaction_enabled 키 부재도 캐노니컬화 — 소비측 기본값과 동일)
- [중간][중복성] `main.py:737-784` vs `skill_invoke.py:107-158` — 스킬 스코프 열닫 2벌 / 컨텍스트매니저 추출.
- ~~[중간][일관성] `main.py:1232,1272,1302,1350` — 종료 경로별 teardown 누락 조합 상이 / `_finalize_run()` 단일화.~~ ✅ v8.39.0 (`teardown_session` 단일 소유 + run 전 경로 단일 finally 수렴 — CliRunner 실구동 테스트로 경로별 완전 teardown 고정; 예외 경로도 세션 저장·MCP 해제)
- [중간][일관성] `core.py:776-828` — `OnTurnEnd`가 실패/RETRY/인터럽트서 미발화 / try-finally 보장.
- [중간][일관성] `core.py:752-759` — directives/memory 리로드 플래그를 서브루프가 선소비 가능 / 소유 루프 가드 통일.
- [중간][중복성] `dispatch.py:1284`+`core.py:715` vs `llm.py:90,199` — messages 직접 append가 ctx 재조립에 전량 폐기(이중 장부) / ctx 파생 뷰로 못박기.
- [중간][확장성] `dispatch.py:176-195` — 무타입 outcome dict 관통 / `TurnOutcome` dataclass.
- [중간][확장성] `dispatch.py:512-559` — 반환 채널 4중 의미 / (센티널|ToolResult) 2종으로 축소.
- [중간][확장성] `main.py` run ~315행·web ~590행 — 커맨드 본문 비대 / `AgentSession` 조립기. (v8.39.0 부분 해소 — registry/waker/teardown 조립은 runtime.py 로 추출; 커맨드 본문 자체의 추가 축소는 잔여)
- [중간][일관성] `main.py:2197` vs `:1895` — 세션 경로 직접 조립 vs `get_session_dir` / 후자 통일.
- [낮음][효율성] `main.py:1266-1269` — 상한 없는 busy-poll / 완료 이벤트+타임아웃.
- [낮음] 8파일 동일 docstring·고아 주석, `_interrupt_check` 2벌, `__all__`에 私유명 10개.

### 4.2 providers/ + wire_formats/
- [높음][일관성] `core.py:818`↔`anthropic.py:226` — stop_reason 정규화 부재 (P0-1).
- [높음][확장성] `http.py:456-459` — degeneration 게이트 json_fc 전용 (P0-4).
- [높음][확장성] `wire_formats/base.py:268-321` — `parse`/`parse_turn` 역전, base 기본 구현 사문 / `parse_turn` 추상 승격.
- [높음][일관성] `json_fc.py:682-691` — `Op.truncated` 미설정 (P0-3).
- [높음][확장성] `_format_rules_builder.py:41` — 호출 0 + 현행 규칙과 모순 / 갱신 or 제거.
- [높음][확장성] `providers/__init__.py:21-32`+`capabilities.py:169-183` — 프로바이더 추가 5곳 수정 / self-register + transport 흡수.
- [중간][중복성] `openai.py:91-118` ≡ `anthropic.py:108-135` — 스트림 재연결 28행 복붙 / `stream_with_reconnect` 공용화.
- [중간][중복성] `capabilities.py:372-526` — 3-tier 탐지 3벌 / transport 확장으로 단일화.
- ~~[중간][일관성] `openai.py:68-80` vs `anthropic.py:89-101` — request_overrides 해석 정책 상이 / 공용 정책 함수.~~ ✅ v8.37.0 (`resolve_thinking_policy`)
- ~~[중간][일관성] `openai.py:149` vs anthropic — 인라인 `<think>` 격리 OpenAI만 / 공용 후처리로 승격.~~ ✅ v8.37.0 (Anthropic 스트림+비스트림 동형 적용)
- [중간][효율성] `http.py:452` — content/thinking O(n²) 누적 / 리스트+join.
- [중간][효율성] `http.py:456-460` — degeneration 누적-전체 재검사 / 윈도우 제한.
- ~~[중간][일관성] `anthropic.py:190-198` — 비스트리밍 다중 text 블록 마지막만 잔존 / 누산으로 동형화.~~ ✅ v8.37.0
- ~~[중간][확장성] `capabilities.py:291-365` — enable_thinking 재프로브 OpenAI만 / transport 공통 계약화.~~ ✅ v8.37.0 (Anthropic 은 thinking 블록 재프로브 — 방언별 스위치, 공통 2단계 계약)
- [중간][일관성] `capabilities.py` — 16K 미만 하드리젝인데 폴백은 4096 + 프로브가 재시도 헬퍼 미사용 / 128K 폴백 통일+재시도.
- [중간][중복성] wire 접두 제거 스캔 2벌(+매 렌더 재정렬), 멀티-op history 직렬화 2벌 / 공용 헬퍼·중간 클래스.
- [낮음] 스트리밍 분기 fall-through 위험(이중 청구), `tool_calls` 등 소비자-0 코드, no-op `max()` 가드·고아 주석.

### 4.3 render/ + web/(Python)
- [높음][확장성] `web.py:137~` — WebRenderer가 렌더러+연결 허브 겸임(계약 외 공개 ~30) / EventHub 분리.
- [높음][일관성] `_pending_thought` 단일 슬롯 (P0-5).
- [높음][일관성] `web.py:1255` vs `minimal.py:285` — 중첩 판정 상이 → ready 스티키 오염 / depth 게이트 통일.
- [높음][일관성] `web.py:703,747` vs `:664` — agent work 라우팅 set/pop vs 스택 복원 불일치 / 단일 스택 규율.
- [중간][확장성] `__init__.py:288` — 렌더러 로더가 알파벳 첫 서브클래스+위치인자(`--style web` 즉사) / `RENDERER` 심볼 규약.
- [중간][확장성] `base.py` — 추상 8 : no-op 30 / 코어+옵셔널 프로토콜 분할.
- [중간][중복성] `__init__.py:49-268` — 위임 함수 25개 기계 반복 / `__getattr__` 위임.
- [중간][중복성] sticky 래퍼 6종+GET/POST 4쌍 동형 / 선언형 세션-설정 레지스트리.
- [중간][중복성] JSON body 가드 6복제 + `workspace_delete` 누락(500) / 공통 의존성.
- [중간][일관성] 오류 계약 2갈래(200+ok:false vs HTTPException) / 상태코드 통일.
- [중간][일관성] `server.py:605,742` — 덕타이핑 getattr + 비공개 `_auto_approve` 직독 / 공개 접근자.
- [중간][효율성] 리플레이 버퍼 건수 상한만(바이트 무제한)+`_JsonReady` 이중 보유 / 바이트 예산.
- [중간][효율성] 청크마다 전역 락+N큐 put / 락 밖 fan-out.
- [중간][효율성] 트리 나열마다 하위 전체 rglob 사이징 / on-demand.
- [중간][효율성] SSE 뷰어당 executor 스레드 15초 점유 / asyncio 대기.
- [중간][중복성] prompt_user/confirm 대기 블록 2벌 / `_await_answer()`.
- [중간][효율성] kill마다 5000 deque 전량 재구축 / 인덱스·tombstone.
- [낮음] `get_event_loop` 혼용, hasattr 지연 생성, render_step만 예외 삼킴.

### 4.4 tools/ + subagent/
- [높음][일관성] edit 배치 훅 우회 (P0-2).
- [높음][중복성] `_handle_request` vs `_handle_human_batch` 몸통 복제 / `_execute_turn(tm, items)` 병합.
- [높음][효율성] code_index 전체 재스캔 + 주석 오도 (P0급 효율) / 증분 API+게이트.
- [중간][일관성] read_context만 프리픽스 키 잔존(flat-native 불변식 위반) / 평탄화.
- ~~[중간][일관성] 존재하지 않는 `delegate` 도구명 안내 2곳 / `agent(mode="run")` 정정.~~ ✅ v8.37.0
- [중간][일관성] `tm.queued` 비원자 seq / 락 하 발급.
- [중간][일관성] 무락 상태로 idle-reap 제어 판정 / 락 하 판독 or 전이 카운터.
- [중간][일관성] MailWaker `_armed` 레이스 / Event/Lock 원자화.
- ~~[중간][확장성] agent 모드 5중 선언(+폴백 문구 `run` 누락) / 선언 테이블.~~ ✅ v8.38.0 (`AGENT_MODES` 단일 테이블 → enum/validate/배칭/브리지 라우팅/`_MODE_HANDLERS` 전부 파생; 신·구 tool_agent 출력 20케이스 바이트 동일 검증)
- [중간][확장성] ScheduleTool env 1회 평가 + conditional 2갈래 / `Tool.available()` 소유.
- [중간][중복성] shell vs _confine confirm 흐름, shell vs fetch over-cap 흐름, `_do_callers/callees`, 단건 vs 배치 edit / 각각 공용 헬퍼.
- [중간][효율성] write_file 1회에 diff 3연산 / opcodes 전달.
- [중간][효율성] 메시지당 agents.json 전체 재작성 / 디바운스·분리.
- [낮음] ~~`validate_tool_input` 제자리 변경~~ ✅ v8.37.0 (사본화 — normalized 반환이 단일 계약), 에러에 스키마 전문 동봉.

### 4.5 context/ + prompts/ + hooks/ + 지원모듈
- [높음][확장성] 파이썬 훅 미배선 (P0-7) + `_run_shell_hooks` 빈 스텁 / 배선 or 제거.
- [높음][효율성] 압축/FIFO/resume마다 전 레코드 재직렬화 / per-record 토큰 미러.
- [중간][일관성] 토큰 회계 단위 혼합 / 이빅션 후 재추정+비율 보정.
- [중간][일관성] fold 오프셋 미갱신 → resume 재혼입 / history 인덱스 동반.
- [중간][확장성] `build_system_prompt` 래퍼 시그니처 이탈 / kwargs 위임 or 삭제.
- [중간][일관성] 스킬 0개 판정 `len<=2` 상시 거짓 / `len==4` 정정.
- ~~[중간][일관성] `save_config` 캐시 미무효화 / `reload_config()` 호출.~~ ✅ v8.37.0
- [중간][중복성] `.agent-cli` 경로쌍 6곳(순서·병합·시점 제각각) / `scoped_paths()` 단일화.
- [중간][일관성] 훅만 파일 대체(타 설정은 병합) / `merge_hooks_configs` 통일.
- [중간][중복성] hooks/shell.py 파싱 24행 중복 / `parse_hooks_config` 재사용.
- [중간][확장성] 훅 오류 전면 무음 / 경고+수집 필드.
- [중간][효율성] memory add마다 시스템 프롬프트 재조립(스킬 glob+md+YAML 전량) / mtime 캐시·섹션 교체.
- [중간][효율성] memory.add 전체 재작성(`append_line` 미사용) / append 전환.
- [중간][확장성] MCP 루프 재진입(병렬 워커) / 직렬화 락 or 전용 스레드.
- [중간][일관성] MCP `${VAR}` env만 치환 / 전 필드 적용. + ~~devnull fd 누수~~ ✅ v8.37.0 (disconnect 에서 close, 연결 실패 경로 포함).
- [낮음] `${SESSION_ID}` 상시 빈값, `$N` 11개+ 치환 버그, `DEFAULT_TOKEN_BUDGET` 사본, 시스템 앵커 사문 분기.

### 4.6 프론트 정적자산
- [높음][확장성] 메인 IIFE 2,744행 + 평평한 가변 상태 30여 개 / ES 모듈 분할(빌드 불필요).
- [높음][확장성] 테스트가 소스를 텍스트 위치로 스크레이핑(불변식 이미 파손, 스크레이퍼 2벌) / 순수 함수 파일 분리 + `module.exports`.
- [높음][일관성] `el()` 미이스케이프 주입 (P0-6) / textContent 고정+`elHtml()` 분리.
- [높음][효율성] 청크마다 개요 전체 innerHTML+마크다운 재실행 / 스트립만 갱신+엔트리 캐시.
- [높음][효율성] 스윔레인 이벤트마다 전체 rebuild(O(N²))+버퍼 무상한 / 증분+상한.
- [중간][확장성] 전역 훅+CustomEvent 브리지 혼재(로드 순서 결합) / 단일 bus 규약.
- [중간][일관성] 브리지 대상/네임스페이스 혼재 + 죽은 이벤트 1건 / window+`agentcli:` 고정.
- [중간][일관성] ask-tray DOM 조립 vs innerHTML+위임 혼재(이중 실행 위험) / 위임 단일화.
- [중간][중복성] POST fetch 15회, 클립보드 3벌, 이스케이프 2벌(미이스케이프 변종), sys-line 4벌, 카드 조회 3+3벌, 트리 재구축 2벌 / util 통합.
- [중간][효율성] ready 재수신마다 리스너 재부착, 카드/청크마다 강제 리플로우+무상한 누적, 인스펙터 필터 매 키 재소문자화 / 1회 바인딩·rAF 병합·캐시.
- [낮음] 테마 목록 2곳, 드로어 CSS 3벌 값 불일치, `scopeParent`/`liveKids` 단조 증가.

---

## 5. 검증 노트

리뷰어 발견 중 고심각도 7건을 메인 리뷰어가 코드로 직접 재확인:
P0-1(`core.py:818` 리터럴 비교 + anthropic 원어 통과) · P0-2(`apply_edits_batch` 직접 호출) · P0-3(json_fc에 truncated 대입 부재) · P0-5(단일 슬롯 3지점) · P0-6(`el()` innerHTML + 미이스케이프 2행) · P0-7(`HookRunner()` 생성 grep 결과 docstring 1건뿐) · code_index(`build()` 시그니처에 path 인자 부재 — 단, sha1 일치 파일은 **재파싱은** 스킵되므로 "전량 재파싱"이 아니라 "전량 read+sha1"이 정확).

나머지 발견은 리뷰어의 file:line 인용을 신뢰하되 개별 수리 착수 시 재확인을 전제로 한다.
