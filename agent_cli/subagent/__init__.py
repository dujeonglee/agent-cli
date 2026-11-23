"""서브에이전트 공용 실행 계층 (teammate P0, docs/teammate/DESIGN.md §4.1).

에이전트(run·spawn)·스킬·하니스 작업 루프가 공유하는 러너와 에이전트 레지스트리.
소비자는 :mod:`agent_cli.subagent.runner` 를 직접 import 한다
(가변 전역 재수출 금지 — C2 교훈).
"""
