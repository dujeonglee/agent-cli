# Agent-CLI v2 — 남은 작업

> 최종 업데이트: 2026-03-22
> 현재 상태: v2 완성 (221 유닛 + 42 통합 = 263개 테스트, CI/CD 구축)

---

## 완료된 작업

- v2 모듈형 패키지 재설계 (M1~M4)
- 네이티브 Tool Calling (Anthropic, OpenAI)
- Constrained Decoding (Ollama JSON Schema)
- Thinking Budget + 프로브 기반 thinking 감지 (message.thinking 필드 + `<think>` 태그)
- 도구 출력 압축 + 퍼지 편집 + 스키마 타입 검증
- 컨텍스트 압축 (구조화 요약 + 증분 업데이트)
- Planning Mode (생성→검토→실행, 영속화, --plan-model, 재시도)
- Skill 시스템 (Claude Code 호환, .agent-cli/skills/)
- Dynamic Tool RAG + 런타임 compat 감지 (Ollama + OpenAI 호환)
- models.json 마이그레이션 (~/.agent-cli/ + 자동 저장)
- 모델 정보 출력 (Rich Panel / 한 줄 요약)
- 기술 부채 제거 (중복 코드, 상수화, import 정리)
- CI/CD (GitHub Actions: pytest + ruff)
- E2E 통합 테스트 (3개 모델)
- 문서 (README.md, docs/ARCHITECTURE.md)

---

## 남은 작업

### 1. PyPI 배포
- 버전을 2.0.0-dev → 2.0.0으로 변경
- `pip install agent-cli`로 설치 가능하도록 PyPI 업로드

### 2. 실사용 피드백 반영
- 실제 사용 중 발견되는 이슈 수정
- 프롬프트 튜닝 (모델별 최적화)
- 새 모델/프로바이더 추가
