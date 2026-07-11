"""서브에이전트 공용 실행 계층 (teammate P0, docs/teammate/DESIGN.md §4.1).

delegate(일회성 파견)와 teammate(상주 팀원, P1+)가 공유하는 러너.
소비자는 :mod:`agent_cli.subagent.runner` 를 직접 import 한다
(가변 전역 재수출 금지 — C2 교훈).
"""
