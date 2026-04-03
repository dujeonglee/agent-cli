# Agent-CLI 프로젝트 규칙

## 코드 수정 시 필수 사항

1. **유닛 테스트**: 코드 수정/추가 시 관련 유닛 테스트를 반드시 추가하거나 업데이트한다. `pytest tests/ -m "not ollama_integration"` 전체 통과를 확인한다.

2. **README.md**: 사용자 대면 기능이 변경되면 README.md를 업데이트한다 (CLI 옵션, 사용법, 기능 설명).

3. **docs/ARCHITECTURE.md**: 내부 구조가 변경되면 아키텍처 문서를 업데이트한다 (모듈, 의존성, 데이터 구조, 플로우, LOC 수치).

4. **ruff**: `ruff check agent_cli/ tests/` 와 `ruff format --check agent_cli/ tests/` 모두 통과해야 한다.

5. **regression 없음**: 모든 변경 후 기존 테스트가 깨지지 않는지 확인한다.

6. **커밋/푸쉬**: 위 1~5 항목을 모두 충족한 후, 수정 코드·README.md·docs/ARCHITECTURE.md·tests 를 **하나의 커밋에 함께 포함**하여 커밋하고 푸쉬한다.

## 코드 스타일

- Python 3.10+ 호환
- ruff format 적용
- 새 의존성 추가 최소화 (on-premise 배포 고려)

## 프로젝트 구조

- `agent_cli/` — 소스 코드 패키지
- `tests/` — 유닛 + 통합 테스트
- `~/.agent-cli/models.json` — 사용자 전역 모델 설정 (자동 저장 대상)
- `.agent-cli/` — 프로젝트 로컬 설정 + 스킬 (.gitignore 대상)
- `agent_cli/default_models.json` — 패키지 기본 모델 정의
