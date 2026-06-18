# Changelog

이 프로젝트의 주요 변경 사항을 기록합니다. 형식은
[Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르며,
버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따릅니다.

버전 증가 규칙 (이 프로젝트 기준):

- **MAJOR** — CLI 플래그/설정 스키마 호환 깨짐, 기본 wire format 전환 등 하위호환 파괴
- **MINOR** — 하위호환 기능 추가 (새 도구·CLI 옵션·wire format)
- **PATCH** — 버그 픽스·문서·내부 정리

## [Unreleased]

## [3.9.3] - 2026-06-18

### Fixed

- **토큰 추정이 멀티-op(`ops`) assistant 레코드 내용을 안 세던 버그 수리** —
  `_estimate_message_tokens` 가 content/thought/top-level action_input 만 세고
  md_array(기본 포맷)의 `ops` 안에 든 action·action_input·complete result 를
  전부 누락했음 → **모든 assistant 턴이 thought 만 카운트**(예: write_file 의 큰
  content 인자나 긴 complete 결과가 예산 추정에서 통째로 빠짐). 결과적으로
  `ensure_within` 의 예방적 압축 판단이 과소추정돼 늦게 트리거(서버 reconcile/
  force_fit 로 사후 보정은 됐으나 부정확). 이제 `ops` 를 순회해 각 op 의
  action+action_input 을 카운트. (md_array 가 기본이 될 때 `_to_summary_text`·
  `_file_extract` 는 ops 처리로 고쳤으나 `_estimate_message_tokens` 는 누락됐던 것.)

### Fixed

- **세션 요약/리스팅이 spill 레코드에서 크래시하던 버그 수리** (`AttributeError:
  'dict' object has no attribute 'startswith'`). spill 레코드의 `content` 가 dict
  인데, raw history 를 읽는 일부 소비자가 여전히 문자열로 가정했음:
  `recent_exchanges`(resume 미리보기·`sessions` 목록 — 실제 크래시 지점),
  `_session_title`(read_context 세션 목록), delegate `_extract_last_actions`(서브
  에이전트 관찰 스크랩). 셋 다 `_spill_view` 로 guide 문자열을 보도록 수리. (이전
  라운드에서 manager 소비자·web replay 는 처리했으나 이 세 경로를 놓쳤음.)

### Fixed

- **spill 의 guide/청크가 긴 줄(1MB 단일-줄 파일)에서 윈도우를 초과하던 회귀 수리** —
  v3.9.0 의 spill 은 head preview(30줄)와 청크를 **라인 기준**으로 잘라서, 줄바꿈이
  거의 없는 출력(예: `read_file` hashline 으로 받은 1MB 단일 줄 파일)에서는 (a) guide
  head 가 그 한 줄을 통째로 포함해 **guide 자체가 262K 토큰**(윈도우 초과)이 되고
  (b) 청크 하나가 1MB(262K 토큰)가 되어, `tokens_after=0` 캐시 비움이 재발했음.
  이제 **청크·guide head 모두 문자(char) 기준**: `_chunk_text_by_tokens` 는 ~50K
  토큰(±200K자) 창에서 줄경계를 선호하되 **긴 줄은 하드분할**하고, guide head 는
  `_SPILL_HEAD_TOKENS`(500)로 하드캡. 실측: 1MB 단일-줄 → spill 레코드 추정 599토큰
  (이전 262K), 청크 6개 각 ≤50K, 무손실.

## [3.9.0] - 2026-06-18

### Fixed

- **단일 도구 출력이 컨텍스트 윈도우를 넘겨 압축을 깨뜨리던 버그 수리** — `find` /
  `code_index` 같은 한 번의 거대 출력(실측 800K 토큰)이 (a) 캐시 토큰을 윈도우
  이상으로 부풀리고, (b) 압축 evict 가 dynamic 전체를 먹어 `tokens_after=0`(캐시
  비움·최신 메시지 손실), (c) 요약 호출 자체를 윈도우 초과로 밀어넣던 문제.

### Added

- **과대 도구 출력 spill (무손실 청크 + 회수)** — 관찰 출력이 50K 토큰을 넘으면
  `ctx.add` 단일 지점에서 청크 분할해 `content = {"spill": true, "output":
  [guide, chunk1, ...]}` 로 저장. **컨텍스트엔 guide(output[0])만** 들어가고(토큰
  회계·LLM·요약·분류 모두 guide 기준) 전체 청크는 history.jsonl 에 **무손실 보존**.
  회수는 read_context 의 새 `content`(JSON) 컬럼 + `json_extract`:
  `SELECT json_extract(content,'$.output[N]') FROM history WHERE turn=T`.
- 어떤 단일 메시지도 윈도우를 넘지 않으므로 압축 evict·요약 호출이 항상 윈도우 안.

### Changed

- **관찰 렌더를 단일 지점(`_append_observation`)으로 통합 — "저장한 것을 보여준다"**.
  관찰 카드는 이제 ctx 에 저장된(spill 적용) 값으로 렌더되어 라이브·재접속·resume
  이 일관(거대 출력은 guide 카드). 부수적으로 multi-op 턴의 관찰 *결과*는 turn 끝에
  합본 1카드로 표시(action 카드는 op별 라이브 유지) — resume 이 이미 보여주던 모습과
  동일. 회복(recovery) 경로는 `render_recovery` 가 이미 렌더하므로 중복 안 함.

### Changed

- **compaction 요약 프롬프트를 agentic-resume 지향으로 정교화** — 기존 4-clause
  (intent/actions/decisions/outcomes) 대신 **구조화 섹션**(TASK / STATE / DONE /
  PENDING / DECISIONS / FAILURES / FACTS, 빈 섹션 생략)을 요청. 에이전트가 요약만
  으로 작업을 이어가야 하므로 **남은 작업(PENDING)·실패한 시도(FAILURES)·verbatim
  식별자(FACTS: 경로/명령/에러 문자열)** 보존 + "transcript 에 있는 것만, 지어내지
  말 것" 규칙을 추가. 재귀 병합은 "same section headings" 로 구조 유지.
  실세션 검증(Qwen3.6-27B): 구조 준수 + 실제 `AttributeError` 실패를 verbatim
  포착, 6.4K→2.3K자 압축.

## [3.7.1] - 2026-06-17

### Fixed

- **카드 시각이 본문 글씨와 겹치던 문제 수리** — absolute 배치된 모서리 시각이
  전폭 텍스트(assistant thought/final, error, user bubble)의 첫 줄 위에
  올라타던 것을, 해당 카드 상단에 거터(padding-top)를 확보해 본문이 시각
  아래에서 시작하도록 조정. (v3.7.0 CSS 회귀.)

## [3.7.0] - 2026-06-17

### Added

- **웹 카드에 시각 표시** — 각 대화 카드(user/assistant/observation/error)
  모서리에 발생 시각을 `YYMMDD HH:MM:SS` 로 표시(마우스 hover 시 전체 날짜+밀리초
  tooltip). `_emit` 단일 fan-out 지점에서 모든 이벤트에 server-stamp `ts` 를
  부착하므로 **delegate/skill 내부 카드까지 자동 커버**.
  - **resume 시각 보존**: `--resume` 로 재생되는 카드는 `replay_from_history` 가
    history record 의 원본 `ts` 를 통과시켜 **실제 발생 시각**으로 표시(재접속
    시점이 아님). history `ts`(ISO)·live `ts`(epoch) 둘 다 프론트가 수용,
    레거시 pre-ts 기록은 wall-clock fallback.

## [3.6.0] - 2026-06-17

### Added

- **웹에서 컨텍스트 압축(compaction) 가시화** — 두 군데로 노출:
  - **대화창 인라인 시스템 라인**: 압축 진행이 `⊙ 컨텍스트 압축 중… → 압축됨
    X→Y tok`(실패 시 warning) 으로 대화 타임라인에 표시. 전용 `compaction`
    SSE 이벤트(구조화 payload)를 프론트가 렌더(`.card-sys`). 이전엔 백엔드가
    generic `status` SSE 를 쐈으나 프론트 리스너가 없어 웹에선 안 보였음.
  - **Prompt Inspector**: 압축으로 흡수된 요약·파일 목록을 `⊙ Compaction
    summary / Files touched (user-injected)` 섹션으로 표시. 이들은
    `get_messages()` 가 시스템 프롬프트 직후 `role=user` 로 주입하는 내용이라
    `self.system` 엔 없지만 컨텍스트를 점유하므로 검사 가능하게 노출.

### Changed

- `render_compaction_progress` 가 `_renderer.compaction(phase, ...)` 으로 위임 —
  base 기본 구현은 `status` 한 줄(CLI 무변경), `WebRenderer` 는 전용 SSE 이벤트로
  override. (포맷 텍스트는 base 로 이동, CLI 출력 바이트 동일.)

## [3.5.1] - 2026-06-17

### Fixed

- **`--no-compaction` 플래그가 delegate/skill 서브에이전트에 전파되도록 수리** —
  부모 `AgentLoop` 의 `compaction_enabled` 가 `tool_delegate`/`_run_single`/
  `_run_parallel`(delegate)과 `_handle_run_skill`→`execute_skill`(skill)의
  `run_loop` 호출까지 스레딩됨. 이전엔 `AGENT_CLI_COMPACTION=off`(env)는 각 loop 의
  `_compaction_enabled()` per-loop 체크로 전파되었지만 `--no-compaction`(CLI 플래그)는
  main loop 만 끄고 서브에이전트는 여전히 압축하는 비대칭이 있었음. 이제 양쪽 일관.

## [3.5.0] - 2026-06-17

### Changed

- **`ask` 도구 flat-native 화 + 비-terminal 배치** — op 안의 `questions:[...]`
  배치 배열 대신 **`{action:"ask", question:"하나"}` 단수**(flat-native 불변식의
  마지막 holdout 제거). 여러 질문은 **ask op 여러 개로 배치**(read_file 처럼).
  또한 `ask` 가 더 이상 턴을 끝내지 않고 — 사용자 응답을 observation 으로 내고
  일반 도구처럼 accumulate — 여러 ask 가 순차 프롬프트 후 합성 observation 1개로
  묶입니다. 단일 질문 동작은 무변경, legacy `questions[]` 도 계속 관용(하위 안전).

## [3.4.2] - 2026-06-17

### Fixed

- **NO_JSON 회복 — 문자열 따옴표 하나 누락(앞/뒤) 일반화 수리** — `"path": mgt.c"`
  (여는 따옴표 누락)·`"path": "mgt.c}`(닫는 따옴표 누락)처럼 따옴표 한쪽이 빠진
  경우를 파서 에러-위치 가이드로 복구(`repair_value_quotes`). bail-if-invalid
  (재파싱 valid 일 때만 채택), bare `true`/`42`·EOF-truncation 은 안 건드림,
  미닫힘 `]` 와 합성. 한 페이로드의 여러 누락도 처리.
- **`## Thought` 헤더 누락 보정 (md_array)** — 모델이 `## Thought` 헤더 없이
  reasoning 을 내고 `## Action` 을 뒤따르게 하면, 앞 prose 가 drop 되던 것을 이제
  thought 로 회수(오타 헤더도 구제). 정상/헤더없는-answer/Action-맨앞 경로 무변경.

## [3.4.1] - 2026-06-17

### Fixed

- **sqlite 없는 환경에서 read_context 가 앱 기동을 깨뜨리던 버그** — `read_context`
  가 `import sqlite3` 를 모듈 최상단에 둬, stdlib `sqlite3` 확장이 없는 Python
  (locked-down/custom 빌드)에서 `No module named '_sqlite3'` 로 **코어 도구
  로드 실패 → 앱 기동 불가**. 이제 `code_index._sqlite` shim(stdlib→`pysqlite3`
  폴백)을 lazy 로 거쳐 — code_index 가 도는 환경이면 read_context 도 동작하고,
  sqlite 가 정말 없으면 쿼리 시 친절한 에러(크래시 아님). authorizer 도
  pysqlite3 가 상수를 안 노출하면 skip(안전은 SELECT prefix + ephemeral DB).

## [3.4.0] - 2026-06-17

### Changed

- **`read_context` 를 SQL 질의로 전환** — 필터 파라미터(`mode`/`kind`/`tool`/
  `author`/`turn`) 더미 대신 **단일 `query`(SQL SELECT)** 프리미티브. history
  를 인메모리 `history` 테이블(컬럼 `session/loc/seq/kind/turn/ts/tools/files/
  author/text`)로 적재하고 LLM이 SELECT 작성. 읽기전용(SELECT/READ 외 거부),
  결과 50행 cap, `query` 생략 시 스키마+예시+세션 목록. (도구 입력 스키마 변경 —
  프롬프트-구동이라 모델이 노출된 스키마로 적응.) `mode=list/search/fetch` 제거,
  context.py 736→362 LOC.

### Added

- **`files` 검색 컬럼** — 각 history 레코드에 **툴이 조작한 파일 경로**를 enrich
  (`extract_file_paths` 재사용). "auth.py 를 건드린 레코드 전부" 같은 조회 가능
  (`... WHERE files LIKE '%auth.py%'`).

## [3.3.0] - 2026-06-17

### Added

- **`read_context` 구조화 JSON 쿼리** — 세션 이력 검색이 키워드 substring +
  필드 필터(`kind`/`tool`/`author`/`turn`)를 자유 조합하는 구조화 쿼리로
  확장. `kind`(query/action/observation/final/raw), `tool`(툴명), `author`
  (웹 멀티유저 닉네임), `turn`(int 또는 {from,to} 범위)로 "정말 필요한
  레코드만" 회상. 구 `scope`(reasoning/tool/observation/query) 폐기.
- **history.jsonl 검색 enrich** — 각 레코드에 `kind`/`turn`/`ts`/`tools`/
  `text`(+`author`) 검색 키를 가산 기록(round-trip/LLM 경로는 무변경). 외부
  `jq` 로도 구조화 조회 가능. (검색 분류 단일 출처 `_classify_record` — 구
  tool-scope 가 ops-모양 액션을 놓치던 버그도 해소.)

## [3.2.0] - 2026-06-16

### Added

- **웹 큐 메시지 단일 라우팅** — 실행 중 큐에 넣은 메시지도 run 시작 메시지와
  **동일하게 라우팅**됩니다. 이제 중간에 보낸 `/sh`·`/compact`·`@agent`·`/skill`
  이 리터럴 chat 텍스트로 새지 않고 **실제로 실행**됩니다(이전엔 run 시작 시에만
  동작). `@agent` 중간 주입은 모델의 `delegate` 와 동일한 경로로 수렴. `/sh`·
  `/help` 은 종전처럼 display-only, `/compact`·`@agent`·`/skill` 은 컨텍스트에
  반영. 평문 메시지는 기존대로 스티어링 주입.

### Changed

- (내부) 사용자 메시지 intake 통합 — run-starter/injected 의 라벨링·라우팅
  중복 제거(`_add_user_message` 단일 헬퍼, `query_label`→`query_author`). 동작
  하위호환(CLI 무변경). 설계 `docs/intake-unification/DESIGN.md`.

## [3.1.1] - 2026-06-16

### Fixed

- **웹 세션 resume 시 assistant 카드 누락** — `replay_from_history` 가 단수
  `{action}` 모양만 디코드해, 두 wire format 이 실제로 저장하는 `ops` 모양
  assistant 턴(`complete` 최종답 포함)이 전부 렌더 누락되던 버그 수정. 이제
  `ops` 분기로 op마다 thought+action(complete=final) 재방출, 레거시 단수 모양·
  raw content-only(final 카드)도 호환.

## [3.1.0] - 2026-06-16

### Added

- **Jira Server/DC 의 평문 `http://` URL 지원** — 웹 UI 에서 사용자가 직접
  입력한(=config 미등록) base_url 에 `http://` 도 허용(기존 `https` 전용 →
  `http`/`https`). 사내 평문 HTTP Jira 를 대상으로 쓸 수 있습니다. 평문 자격증명
  전송 위험은 차단이 아니라 입력 시 UI 경고로 표시하며, `http`/`https` 외 scheme
  은 거부합니다. config 에 등록된 URL 의 내부 http 허용은 종전과 동일.
- **닉네임 중간 변경** — 웹 로스터 옆 ✎ 버튼으로 접속 후에도 닉네임을 언제든
  재설정(현재 닉 prefill → 즉시 로스터 반영). 첫 연결 닉네임 바를 재사용하며
  서버는 ephemeral 유지(영속화 없음).

## [3.0.0] - 2026-06-15

### Changed

- **Jira 코멘트를 프론트엔드 사용자 본인 명의로 게시** — 코멘트 작성자가 백엔드
  config 계정이 아니라, 웹 UI 에서 자격증명을 입력한 그 사용자가 됩니다.
  - **(호환 깨짐)** config `jira.instances` 에서 `email`/`api_token` 제거 —
    이제 `base_url`(+ 선택 `deployment`)만 둡니다. 자격증명은 서버에 저장하지
    않고 사용자가 웹 UI 에서 입력(브라우저 localStorage 기억, POST 한 번에만
    transient 사용).
  - **Jira Cloud + Server/Data Center 모두 지원** — `{base_url}/rest/api/2/
    serverInfo` 프로브로 deployment 자동 판별(또는 config 명시/UI 토글). Cloud=
    `/rest/api/3`+ADF, Server/DC=`/rest/api/2`+wiki 마크업으로 코멘트 본문 전송.
  - **config 선택화(zero-config)** — config 없이도 웹 UI 에서 base_url 을 직접
    입력해 게시 가능. config 인스턴스는 드롭다운+URL prefill 로 제공. config 에
    없는 사용자 입력 URL 은 `https://` 만 허용(서버가 미검증 호스트로 자격증명을
    보내므로 TLS 강제; config 등록 URL 은 내부 http 허용).

## [2.1.0] - 2026-06-14

### Added

- **`agent-cli update`** — GitHub 최신 릴리스 확인 후 업데이트(`gh` + `pip`).
  `--check`(확인만)·`-y`(확인 생략)·`--force`(dev 설치 강행). private repo
  인증은 `gh` 로그인이 처리(토큰 불필요), 릴리스 첨부 wheel 을 설치.
- **웹 워크스페이스 다운로드(📥)** — 우측 드로어의 lazy 파일 트리에서 파일/
  디렉토리를 골라 zip 다운로드(디렉토리=재귀, All=전체). 파일·디렉토리 크기 표시.
- **웹 멀티유저** — 접속자 수·닉네임 로스터(`👁 N · …`), 접속 시 닉네임 입력
  (재미있는 기본값 20개 풀에서 배정, localStorage 기억), 사용자 메시지 큐:
  실행 중 보낸 메시지가 큐에 쌓여 실시간 표시되고 매 턴 종료 시 하나씩 대화에
  주입(steering), 자기 큐 메시지 취소 가능. 모든 사용자 요청은 `[닉네임]:`
  라벨로 LLM 에 노출 + task 로그 누적.

### Changed

- **웹 제어 모델 단순화** — controller/observer 권한 시스템(권한 요청/승인)
  제거, 모든 연결이 동등하게 입력·큐 가능. `role` 이벤트 → `identity`.

### Fixed

- `pysqlite3-binary` 의존성 marker 를 x86_64 Linux 로 한정 — **arm64 Linux 에서
  agent-cli 설치 불가** 버그 수정(arm64 wheel 부재).
- `complete` 턴 history 직렬화를 포맷 동질 모양(`ops`)으로 — 단수 `{action}`
  으로 새던 불일치 수정.
- `read_context {mode:list}` 크래시(제거된 `SessionMeta.query` 참조) 수정 —
  세션 제목을 history 첫 메시지에서 유도.
- 웹 다운로드 All 후 재오픈 시 트리 비활성 잔존 수정.

### Bench (제품 외)

- `bench/swebench/` — SWE-bench 어댑터(호스트 인퍼런스 A + 컨테이너 인-에이전트
  B, 네이티브 arm64), report. django-10914 검증, B5-django resolved 4/5.

## [2.0.0] - 2026-06-14

첫 공개 릴리스. on-premise LLM을 위한 ReAct 패턴 에이전트 CLI.

### Added

**에이전트 루프**
- ReAct(Reason–Act) 루프 — Thought → Action → Observation. 단일 실행(`agent-cli run`)과 대화형 웹 UI(`agent-cli web`) 지원.
- 멀티 프로바이더 — OpenAI 호환(OpenAI, vLLM, omlx, LM Studio 등)과 Anthropic.
- `agent-cli --version` / `-V` 플래그.

**Wire format (모델 응답 형식 추상화)**
- 플러그인형 wire format 시스템. 두 멀티-op 포맷 내장: `md_array`(기본, 마크다운 envelope + flat op JSON 배열)와 `react`(JSON 쌍둥이).
- 전 builtin 도구 flat-native — `{action, ...params}` (wire-key prefix·batch 배열 제거). prefix 머신러리는 미래 prefixed 도구용 latent seam으로 보존.
- 한 턴에 여러 독립 op 디스패치. `delegate`는 연속 op를 병렬 실행(`parallel_safe`).

**Robust recovery 하니스**
- 3단계 파싱 폴백 + 형식 실패 회복(NO_JSON / NO_ACTION / NO_THOUGHT / UNKNOWN_TOOL / SCHEMA_MISMATCH / ACTION_LOOP / DEGENERATE 등 라벨링).
- failure-grounding 재시도(모델 자기 출력 echo), 액션 루프 감지, degeneration 조기 중단.
- JSON 구문 진단 — 파싱 실패 시 line/column + 캐럿으로 *어디가* 깨졌는지 모델에 제시.
- 미닫힘 op 배열 EOF 닫기 수리(문자열-인식, bail-if-invalid).
- 세션별 관측성 로그(`turns.jsonl`).

**도구**
- `read_file`(심볼/라인 범위/배치), `write_file`, `edit_file`(hashline), `shell`, `code_index`(tree-sitter 기반 심볼 인덱싱), `delegate`(서브에이전트, 병렬), `ask`, `complete`, `run_skill`, `ready_for_review`.
- MCP 도구 어댑터.

**컨텍스트 관리**
- token-budget 기반 LLM 요약 compaction + FIFO 폴백. 세션 저장·resume.

**확장성**
- 스킬 시스템(내장: create-skill, create-agent, plan, create-team) + 사용자/프로젝트 스킬.
- 에이전트 정의(내장: explorer) + 사용자/프로젝트 에이전트.
- 플러그인 렌더러 시스템.
- 라이프사이클 훅(PreLLMCall, PostLLMCall, PreToolUse, PostToolUse).

**웹 UI** (`agent-cli web`, `pip install agent-cli[web]`)
- FastAPI + SSE 기반 LAN UI, 스트리밍·중단·resume.
- 대화 내보내기(📤) — HTML 파일 또는 Jira 코멘트(다중 인스턴스).
- Prompt Inspector(⚡) — 시스템 프롬프트 디버그 드로어.

**배포**
- 순수 파이썬 패키지(`py3-none-any` wheel), Python 3.10+.
- on-prem 친화 — 의존성 최소화, locked-down 서버용 `pysqlite3-binary` 폴백(Linux).

[Unreleased]: https://github.com/dujeonglee/agent-cli/compare/v3.9.3...HEAD
[3.9.3]: https://github.com/dujeonglee/agent-cli/compare/v3.9.2...v3.9.3
[3.9.2]: https://github.com/dujeonglee/agent-cli/compare/v3.9.1...v3.9.2
[3.9.1]: https://github.com/dujeonglee/agent-cli/compare/v3.9.0...v3.9.1
[3.9.0]: https://github.com/dujeonglee/agent-cli/compare/v3.8.0...v3.9.0
[3.8.0]: https://github.com/dujeonglee/agent-cli/compare/v3.7.1...v3.8.0
[3.7.1]: https://github.com/dujeonglee/agent-cli/compare/v3.7.0...v3.7.1
[3.7.0]: https://github.com/dujeonglee/agent-cli/compare/v3.6.0...v3.7.0
[3.6.0]: https://github.com/dujeonglee/agent-cli/compare/v3.5.1...v3.6.0
[3.5.1]: https://github.com/dujeonglee/agent-cli/compare/v3.5.0...v3.5.1
[3.5.0]: https://github.com/dujeonglee/agent-cli/compare/v3.4.2...v3.5.0
[3.4.2]: https://github.com/dujeonglee/agent-cli/compare/v3.4.1...v3.4.2
[3.4.1]: https://github.com/dujeonglee/agent-cli/compare/v3.4.0...v3.4.1
[3.4.0]: https://github.com/dujeonglee/agent-cli/compare/v3.3.0...v3.4.0
[3.3.0]: https://github.com/dujeonglee/agent-cli/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/dujeonglee/agent-cli/compare/v3.1.1...v3.2.0
[3.1.1]: https://github.com/dujeonglee/agent-cli/compare/v3.1.0...v3.1.1
[3.1.0]: https://github.com/dujeonglee/agent-cli/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/dujeonglee/agent-cli/compare/v2.1.0...v3.0.0
[2.1.0]: https://github.com/dujeonglee/agent-cli/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/dujeonglee/agent-cli/releases/tag/v2.0.0
