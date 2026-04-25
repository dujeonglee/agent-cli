# Agent-CLI 프로젝트 규칙

## 코드 수정 시 필수 사항

1. **유닛 테스트**: 코드 수정/추가 시 관련 유닛 테스트를 반드시 추가하거나 업데이트한다. `pytest tests/ -m "not ollama_integration"` 전체 통과를 확인한다.

2. **README.md**: 사용자 대면 기능이 변경되면 README.md를 업데이트한다 (CLI 옵션, 사용법, 기능 설명).

3. **docs/ARCHITECTURE.md**: 내부 구조가 변경되면 아키텍처 문서를 업데이트한다 (모듈, 의존성, 데이터 구조, 플로우, LOC 수치).

4. **ruff**: `ruff check agent_cli/ tests/` 와 `ruff format --check agent_cli/ tests/` 모두 통과해야 한다.

5. **regression 없음**: 모든 변경 후 기존 테스트가 깨지지 않는지 확인한다.

6. **커밋/푸쉬**: 위 1~5 항목을 모두 충족한 후, 수정 코드·README.md·docs/ARCHITECTURE.md·tests 를 **하나의 커밋에 함께 포함**하여 커밋하고 푸쉬한다.

7. **기술 부채 금지**: 기술 부채 요인이 있으면 즉각 멈추고 사용자와 의논할 것. 임시 해결책이나 불필요한 추상화를 만들지 않는다.

## 코드 스타일

- Python 3.10+ 호환
- ruff format 적용
- 새 의존성 추가 최소화 (on-premise 배포 고려)

## 프로젝트 구조

- `agent_cli/` — 소스 코드 패키지
- `agent_cli/render/` — 플러그인 렌더러 시스템 (minimal — 커스텀 추가 가능)
- `agent_cli/skills/builtin/` — 패키지 내장 스킬 (create-skill, create-agent, plan, create-team)
- `agent_cli/agents/builtin/` — 패키지 내장 에이전트 (explorer)
- `agent_cli/resource_loader.py` — 공유 파일 로더 (스킬/에이전트/지시사항)
- `tests/` — 유닛 + 통합 테스트
- `docs/` — 아키텍처 문서 + 설계 문서 (`delegate-redesign/` 등)
- `~/.agent-cli/models.json` — 사용자 전역 모델 설정 (자동 저장 대상)
- `~/.agent-cli/DIRECTIVE.md` — 사용자 전역 에이전트 지시사항
- `~/.agent-cli/agents/` — 사용자 전역 에이전트 정의
- `.agent-cli/` — 프로젝트 로컬 설정 + 스킬 (.gitignore 대상)
- `.agent-cli/DIRECTIVE.md` — 프로젝트별 에이전트 지시사항
- `.agent-cli/agents/` — 프로젝트별 에이전트 정의 (delegate agent 파라미터용)
- `agent_cli/default_models.json` — 패키지 기본 모델 정의
