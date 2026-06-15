# Changelog

이 프로젝트의 주요 변경 사항을 기록합니다. 형식은
[Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르며,
버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따릅니다.

버전 증가 규칙 (이 프로젝트 기준):

- **MAJOR** — CLI 플래그/설정 스키마 호환 깨짐, 기본 wire format 전환 등 하위호환 파괴
- **MINOR** — 하위호환 기능 추가 (새 도구·CLI 옵션·wire format)
- **PATCH** — 버그 픽스·문서·내부 정리

## [Unreleased]

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

[Unreleased]: https://github.com/dujeonglee/agent-cli/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/dujeonglee/agent-cli/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/dujeonglee/agent-cli/releases/tag/v2.0.0
