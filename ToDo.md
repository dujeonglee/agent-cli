# Agent-CLI v2 — 남은 작업

> 최종 업데이트: 2026-03-22
> 현재 상태: v2 재설계 완료 (4,117 LOC, 215 유닛 + 39 통합 = 254개 테스트)

---

## 완료된 작업

- M1: 모듈형 패키지 구조 + 모델 레지스트리 + 3개 프로바이더 + 3단계 파서
- M2: 도구 출력 압축 + 퍼지 편집 + 스키마 검증 + 오버플로 감지 + 구조화 요약
- M3: 조건부 시스템 프롬프트 + 에이전트 루프 + CLI 진입점
- M4: Planning Mode (생성→검토→실행, 영속화, --plan-model, step-max-iter)
- P0: pyproject.toml + Anthropic/OpenAI 네이티브 tool calling
- P1: Thinking budget + 스키마 타입 검증 + 유니코드 세정
- P2: 계획 영속화 + Dynamic Tool RAG + 런타임 compat 감지 + --plan-model
- E2E: 실제 Ollama 3개 모델 통합 테스트
- Thinking 블록 감지/분리 (<think>, <reasoning> 등)
- Skill 시스템 (Claude Code 호환, .agent-cli/skills/)
- 기술 부채 제거 (중복 코드, 상수화, import 정리)
- models.json 마이그레이션 (~/.agent-cli/ + 런타임 감지 자동 저장)
- 모델 정보 출력 (감지 시 Rich Panel, 로딩 시 한 줄 요약)
- 문서 (README.md, docs/ARCHITECTURE.md)

---

## 남은 작업

### 1. CI/CD 파이프라인
- GitHub Actions: pytest (유닛만), ruff (린팅)
- PR 자동 테스트
- 통합 테스트는 로컬 전용 (Ollama 필요)

### 2. PyPI 배포
- 버전을 2.0.0-dev → 2.0.0으로 변경
- `pip install agent-cli`로 설치 가능하도록 PyPI 업로드

### 3. 실사용 피드백 반영
- 실제 사용 중 발견되는 이슈 수정
- 프롬프트 튜닝 (모델별 최적화)
- 새 모델 추가 시 models.json 업데이트
