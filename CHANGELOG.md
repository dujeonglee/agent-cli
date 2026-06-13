# Changelog

이 프로젝트의 주요 변경 사항을 기록합니다. 형식은
[Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르며,
버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따릅니다.

버전 증가 규칙 (이 프로젝트 기준):

- **MAJOR** — CLI 플래그/설정 스키마 호환 깨짐, 기본 wire format 전환 등 하위호환 파괴
- **MINOR** — 하위호환 기능 추가 (새 도구·CLI 옵션·wire format)
- **PATCH** — 버그 픽스·문서·내부 정리

## [Unreleased]

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

[Unreleased]: https://github.com/dujeonglee/agent-cli/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/dujeonglee/agent-cli/releases/tag/v2.0.0
