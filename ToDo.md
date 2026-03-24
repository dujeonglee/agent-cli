# Agent-CLI v2 — 남은 작업

> 최종 업데이트: 2026-03-24
> 현재 상태: v2 완성 (348 유닛 + 42 통합 = 390개 테스트)

---

## 완료된 작업

- v2 모듈형 패키지 재설계 (M1~M4)
- 네이티브 Tool Calling (Anthropic, OpenAI)
- Constrained Decoding (Ollama JSON Schema)
- Thinking Budget + 프로브 기반 thinking 감지 (message.thinking 필드 + `<think>` 태그)
- 도구 출력 압축 (context 3% 비례) + 퍼지 편집 + 스키마 타입 검증
- 컨텍스트 압축 (95% 임계값, 구조화 요약 + 증분 업데이트)
- Planning Mode (생성→검토→실행, 영속화, --plan-model, 재시도)
- Skill 시스템 (Claude Code 호환 필드명, .agent-cli/skills/)
- Dynamic Tool RAG + 런타임 compat 감지 (Ollama + OpenAI 호환)
- models.json 마이그레이션 (~/.agent-cli/ + 자동 저장 + 패키지 기본값)
- complete 가상 도구 (final_answer 대체 → 루프 무한반복 해결)
- ask 도구 (배열 지원, chat 모드 전용)
- read_file 부분 읽기 (line_start/line_end)
- echo-as-final-answer 변환
- 반복 호출 3회 감지 → 자동 중단
- 체크포인트 시스템 (50회 + 매 20회)
- 파일 기반 세션 context (JSONL 로깅 + ctx_window 요약 + --resume + sessions 명령)
- chat readline 히스토리 (화살표 키 + 영속화)
- 문서 (README.md, docs/ARCHITECTURE.md)

---

## 남은 작업

### 1. Claude Code 스킬 완벽 호환

현재 지원하는 스킬 프론트매터 필드:
- [x] `name` — 슬래시 명령어 이름
- [x] `description` — 스킬 설명
- [x] `argument-hint` — 인자 힌트
- [x] `allowed-tools` — 허용 도구 리스트
- [x] `max-iter` — 최대 이터레이션 (agent-cli 전용)

아직 미지원인 Claude Code 필드:
- [ ] `disable-model-invocation` (bool) — true면 사용자만 호출 가능 (LLM 자동 호출 금지)
- [ ] `user-invocable` (bool) — false면 LLM만 호출 가능 (사용자 /메뉴에서 숨김)
- [ ] `model` (string) — 스킬 실행 시 모델 오버라이드
- [ ] `effort` (string) — low / medium / high / max
- [ ] `context` (string) — "fork"이면 독립 subagent context에서 실행
- [ ] `agent` (string) — context: fork 시 사용할 에이전트 타입
- [ ] `hooks` (object) — 스킬 스코프 lifecycle hooks (PreToolUse, PostToolUse)

아직 미지원인 디렉토리 구조:
- [ ] `skills/<name>/SKILL.md` 디렉토리 구조 지원 (현재: `skills/*.md` 플랫 구조만)
- [ ] 스킬 디렉토리 내 supporting files 참조 (reference.md, scripts/ 등)

아직 미지원인 동적 기능:
- [ ] `$ARGUMENTS[N]` — 0-based 인덱스 접근 (현재: `$0`, `$1` 지원)
- [ ] `${CLAUDE_SKILL_DIR}` — 스킬 디렉토리 경로 변수
- [ ] `${CLAUDE_SESSION_ID}` — 세션 ID 변수 (→ `${SESSION_ID}`로 대체 가능)
- [ ] `` !`command` `` — 동적 컨텍스트 주입 (셸 명령 실행 후 결과 주입)

구현 우선순위 제안:
1. `model` 오버라이드 — 가장 실용적, executor에 model 파라미터 전달만 하면 됨
2. `context: fork` — delegate와 유사, 독립 context에서 실행
3. 디렉토리 구조 — loader.py에 SKILL.md 탐색 추가
4. `${CLAUDE_SKILL_DIR}` / `${SESSION_ID}` — substitute_arguments()에 추가
5. `` !`command` `` 동적 주입 — 스킬 로딩 시 셸 명령 실행
6. hooks — 가장 복잡, 별도 hook 시스템 필요
7. `disable-model-invocation` / `user-invocable` — LLM 자동 호출 기능 구현 후 의미 있음

### 2. PyPI 배포
- 버전을 2.0.0-dev → 2.0.0으로 변경
- `pip install agent-cli`로 설치 가능하도록 PyPI 업로드

### 3. 실사용 피드백 반영
- 실제 사용 중 발견되는 이슈 수정
- 프롬프트 튜닝 (모델별 최적화)
- 새 모델/프로바이더 추가

### 4. Iteration 기반 중단 + 요약 (검토 중)
- max_iter 도달 시 작업 요약 생성 → 사용자에게 계속/중단 선택
- 요약을 context에 넣고 iteration 카운터 리셋하여 이어가기
