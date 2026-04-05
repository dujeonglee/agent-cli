# Context & Artifact 재설계 — 구현 계획

> 기반 문서: docs/context-redesign/DESIGN.md
> Date: 2026-04-05

## Phase 1: ContextManager 재작성 (FIFO + history.jsonl + 캐시) ✅

- [x] history.jsonl 읽기/쓰기 유틸 (append, 마지막 N개 파싱)
- [x] FIFO 메모리 캐시 (deque N=100, append, 자동 앞 제거)
- [x] 자연어 변환 로직 (JSON → LLM messages)
  - assistant: `{thought}. → {action}({인자})`
  - assistant (complete): `{thought}. {result}`
  - user (도구 결과): `[{tool}] {인자}\n{전문}\n→ {artifact}`
  - user (사용자 입력): 그대로
- [x] get_messages() → 캐시에서 자연어 변환하여 반환
- [x] add() → 캐시 append + history.jsonl append
- [x] 세션 재개: history.jsonl에서 마지막 N개 파싱하여 캐시 초기화
- [x] 기존 압축 로직 제거 (_compress, _summary, _build_scratchpad_block 등)
- [x] 기존 scratchpad 연동 제거 (begin_turn, end_turn, init_task 등)
- [x] fork_history_to() — fork 모드용 history 복사
- [x] 유닛 테스트 (29 passed)
  - FIFO 동작 (N개 초과 시 앞에서 제거)
  - history.jsonl append-only 기록
  - 자연어 변환 (각 메시지 타입별)
  - 세션 재개 시 캐시 복원
  - 빈 세션 시작
  - Fork 복사 + 자식 독립성
  - 통합 시나리오 (대화 흐름 + artifact 경로 보존)

## Phase 2: loop.py 연동 (자연어 변환 + history.jsonl)

### 분석 결과 (Phase 1 완료 시점)

loop.py (~2000 LOC)에서 ctx 사용 패턴 4가지:
1. `ctx.add("role", "content")` → `ctx.add({"role":..., ...})` dict 형태로 변경
2. `ctx.end_turn(...)` → 제거 (scratchpad 연동)
3. `ctx.begin_turn()` / `ctx.init_task()` / `ctx.set_dispatch_context()` / `ctx._step_count` / `ctx._scratchpad_dir` → 제거
4. `ctx.force_compress()` → 제거. `ctx.get_messages()` 유지

주의: 기존 테스트 (~50개 이상)가 old API로 ContextManager를 mock하고 있음.
전략: 기존 코드가 동작하도록 호환 래퍼를 먼저 검토한 뒤, 하나씩 치환.

관련 함수 (loop.py 내 ctx 호출 위치):
- `_setup()`: init_task, set_dispatch_context, append_progress, add("user", query)
- `_on_interrupt()`: add("user", msg), append_progress
- `_begin_iteration()`: begin_turn
- `_execute_iteration()`: force_compress, get_messages
- `_maybe_checkpoint()`: add("user", msg)
- `_call_llm()`: force_compress, get_messages (overflow retry)
- `_handle_native_path()`: end_turn, add("assistant", answer), 각 tool별 observation
- `_handle_text_path()`: end_turn, add("assistant", answer), observation
- `_append_native_observation()`: add("assistant", ...), add("user", ...)
- `_append_text_observation()`: add("assistant", ...), add("user", ...)
- `_handle_run_skill()`: set_dispatch_context, append_progress, _step_count, _scratchpad_dir

### 추가 결정: Native Tool Calling 미사용

Anthropic/OpenAI native tool calling 제거. 모든 provider가 text parsing 방식만 사용.
→ _handle_native_path, _append_native_observation, _format_anthropic/openai_tool_messages 전부 제거
→ _handle_text_path만 유지, 자연어 변환 적용

### 작업 항목

- [ ] Native tool calling 관련 코드 제거
  - _handle_native_path() 전체 제거
  - _append_native_observation() 제거
  - _format_tool_call_messages / _format_anthropic_tool_messages / _format_openai_tool_messages 제거
  - convert_to_anthropic_tools / convert_to_openai_tools import 제거
  - supports_tool_calling 분기 제거
- [ ] ctx.add() 호출 변경 — `ctx.add("role", "content")` → `ctx.add(dict)`
- [ ] ctx.end_turn() 호출 제거
- [ ] scratchpad 관련 호출 제거: begin_turn, init_task, set_dispatch_context, _step_count, _scratchpad_dir, append_progress
- [ ] ctx.force_compress() 제거 + overflow retry 로직 제거
- [ ] _append_text_observation → 자연어 변환 적용
- [ ] thought 프롬프트 강화 (Format Rules에 목적+이유 지침 추가)
- [ ] main.py 내 ctx 호출 변경 (_dispatch_agent, _dispatch_skill, /compact, /ctx_window)
- [ ] ContextManager 생성부 변경 (main.py, delegate.py, skills/executor.py)
- [ ] 유닛 테스트
  - text path만으로 동작 확인
  - history.jsonl 기록 확인
  - 기존 테스트 중 native tool calling / scratchpad 관련 제거/수정

## Phase 3: system_prompt.py 변경 ✅

- [x] Role 상속 로직 (agent_role > parent_role > ROLE_PROMPT)
- [x] Git Context 섹션 제거
- [x] Session ID 섹션 제거
- [x] Context Recovery Guide 섹션 추가 (session_dir 파라미터)
- [x] DIRECTIVE.md를 Environment 앞으로 이동
- [x] thought 지침 강화 (purpose + reason 필수)
- [x] 유닛 테스트 (11 passed)

## Phase 4: delegate.py 변경 ✅ (partial)

- [x] inherit 모드 제거 (treat as none)
- [x] fork 모드 재정의: parent history.jsonl 복사 → delegate history.jsonl
- [x] delegate subdir 구조 생성: `delegate_{name}_{hash}_{ts}/`
- [x] result.md 저장 (replaces scratchpad save_artifact)
- [x] 결과 반환에 delegate 디렉토리 경로 포함
- [x] files_touched 제거
- [x] set_dispatch_context 제거
- [ ] agent_stack 재귀 방지 (별도 작업)
- [ ] 병렬 delegate 호출 순서 append 검증
- [ ] 유닛 테스트 업데이트 (old API 테스트 10개 실패 중)

## Phase 5: skills/executor.py 변경

- [ ] 도구 교집합 로직: skill allowed-tools ∩ parent allowed-tools
- [ ] 빈 교집합 시 실행 거부 (에러 반환)
- [ ] parent Role 상속: parent_role 파라미터 전달
- [ ] skill subdir 구조 생성: `skill_{name}_{hash}_{ts}/`
  - history.jsonl
  - result.md
- [ ] parent history.jsonl에 observation 기록 (결과 + artifact 경로)
- [ ] 유닛 테스트
  - 도구 교집합 계산
  - 빈 교집합 실행 거부
  - parent Role 상속 (main → 기본, delegate → Agent Role)
  - skill subdir 구조 확인
  - skill이 delegate 호출 시 재귀 중첩 디렉토리

## Phase 6: 정리

- [ ] agent_cli/context/scratchpad.py 삭제
- [ ] agent_cli/prompts/compression_prompt.py 삭제
- [ ] scratchpad 관련 import 제거 (manager.py, loop.py 등)
- [ ] 기존 테스트 정리 (scratchpad, compression 관련 테스트 삭제)
- [ ] ruff check + ruff format 통과
- [ ] pytest tests/ -m "not ollama_integration" 전체 통과
- [ ] 사용하지 않는 함수/클래스/import 정리 (전체 codebase grep)
- [ ] docs/ARCHITECTURE.md 업데이트
- [ ] README.md 업데이트 (필요 시)
