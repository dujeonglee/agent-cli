"""delegate 패키지 — C2 파일 물리 분할 (loop/ 패키지와 동형 패턴).

기존 단일 모듈 ``agent_cli/tools/delegate.py`` 의 공개 표면을 그대로
재수출한다. 모듈 배치 = 소유권: agents(에이전트 정의 로딩) /
report(실행 결과의 표현 — DelegateResult·활동로그 추출·영속·포맷팅) /
exec(단일·병렬 실행 엔진 + tool_delegate 진입) / tool(DelegateTool).
테스트 monkeypatch 는 사용하는 서브모듈을 patch 해야 한다.
(프로파일 로딩은 5.0.0 에서 ``subagent/profiles.py`` 로 병합 이관 —
테스트는 ``profiles._profile_loader`` 를 교체.)
"""

from agent_cli.tools.delegate.report import (
    DelegateResult,
    _extract_activity_log,
    _extract_last_actions,
    _format_delegate_output,
    _format_parallel_results,
    _generate_delegate_dir_name,
    _persist_delegate_result,
    _summarize_action,
)
from agent_cli.tools.delegate.exec import _run_parallel, _run_single, tool_delegate
from agent_cli.tools.delegate.tool import DelegateTool

__all__ = [
    "DelegateResult",
    "DelegateTool",
    "tool_delegate",
    "_extract_activity_log",
    "_extract_last_actions",
    "_format_delegate_output",
    "_format_parallel_results",
    "_generate_delegate_dir_name",
    "_persist_delegate_result",
    "_run_parallel",
    "_run_single",
    "_summarize_action",
    "_validate_agent_name",
]
