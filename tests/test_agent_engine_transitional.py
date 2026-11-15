"""엔진 통합의 P1 임시물 목록 — P2 가 이 목록을 전부 지워야 끝난다 (docs/agent-engine/DESIGN.md §4).

사용자 결정: P1 의 임시 종류·어댑터는 두되, 지울 때 철저하게 없애 최종 디자인은 단일 모듈
(루프 종류 하나 = `ResidentLoop`, 결과 = 회신)이어야 한다. 그래서 지울 이름을 여기 한 곳에 적는다.

**P2 에서 이 테스트를 '저장소 어디에도 없다' 로 뒤집는다 — 최종 디자인은 단일 모듈(사용자 결정
2026-11-15).** `test_transitional_names_exist`·`test_transitional_strings_exist` 를
"``agent_cli/``·``tests/``·``docs/`` 어디에도 없다(이 파일 제외)" 로 바꾸고, 그 테스트가 통과할
때까지 P2 는 끝나지 않는다. 지금(P1)은 목록이 실제 코드와 맞는지(오타·이미
사라진 이름이 없는지)만 확인한다.
"""

from __future__ import annotations

import importlib

import pytest

#: (모듈, 이름) — 이름이 None 이면 모듈 통째. 각 줄의 대체물은 DESIGN.md §4 표.
TRANSITIONAL: frozenset[tuple[str, str | None]] = frozenset(
    {
        # 결과 어댑터 — 결과는 회신(reply)으로
        ("agent_cli.subagent.run_result", None),
        ("agent_cli.subagent.run_result", "RunReply"),
        ("agent_cli.subagent.run_result", "write_result_md_and_tool_result"),
        ("agent_cli.subagent.run_result", "batch_tool_result"),
        # STATUS/RESULT 포장 — 어댑터만 쓰는 report.py 의 run 포맷터
        ("agent_cli.subagent.report", "DelegateResult"),
        ("agent_cli.subagent.report", "_extract_activity_log"),
        ("agent_cli.subagent.report", "_extract_last_actions"),
        ("agent_cli.subagent.report", "_format_delegate_output"),
        ("agent_cli.subagent.report", "_format_parallel_results"),
        ("agent_cli.subagent.report", "_persist_run_result"),
        ("agent_cli.subagent.report", "extract_result_body"),
        # run 전용 루프 종류 — ResidentLoop(ends_after_task) 하나로
        ("agent_cli.subagent.run_agent", "RunAgentLoop"),
        ("agent_cli.subagent.run_agent", "RUN_ITEM"),
        ("agent_cli.subagent.run_agent", "_wait_for_replies"),
        # run: 주소 접두 — run 에이전트도 agent:<key>
        ("agent_cli.constants", "RUN_OWNER_PREFIX"),
        # run 전용 축소 스키마 — 서브루프도 같은 도구 표면(§1)
        ("agent_cli.tools.agent_tool", "AgentTool.SUBLOOP_PARAMETERS"),
        # 레지스트리의 run 전용 수명·필터 — spawn(ends_after_task) 한 길로
        ("agent_cli.subagent.agents_live", "AgentRegistry.start_run_agent"),
        ("agent_cli.subagent.agents_live", "AgentRegistry._run_agent_worker"),
        ("agent_cli.subagent.agents_live", "AgentRegistry._run_agent_tombstone"),
        ("agent_cli.subagent.agents_live", "AgentRegistry._residents"),
    }
)

#: 이름이 아니라 글로 남는 임시물 — 저장소 grep 대상(P2 의 "없다" 판정에 같이 쓴다).
TRANSITIONAL_STRINGS: frozenset[str] = frozenset(
    {
        "STATUS: error\\nERROR: Subagent did not complete",
        # 모델 대면 명단에서 run 빼기 — P2 는 run 도 명단·주소에 나온다
        'not s.get("ends_after_task")',
        # run 채널 읽기 전용·✕/↻ 없음 — P2 는 직접 메시지, P3 는 채널 ■/✕
        "function ovActiveRun()",
        "This run agent takes no messages",
        'if (key !== "main" && !run)',
        "tests/equivalence/test_run_result_parity.py",  # P1 바이트 기준 — P2 는 바이트가 바뀐다
    }
)


def _resolve(module: str, name: str | None):
    obj = importlib.import_module(module)
    for part in (name or "").split(".") if name else []:
        obj = getattr(obj, part)
    return obj


@pytest.mark.parametrize(("module", "name"), sorted(TRANSITIONAL, key=str))
def test_transitional_names_exist(module, name):
    """P1: 목록이 실제 코드와 맞는다. **P2 에서 "어디에도 없다" 로 뒤집는다.**"""
    assert _resolve(module, name) is not None


def test_transitional_strings_exist():
    """P1: 글로 남는 임시물이 실제로 있다. **P2 에서 "어디에도 없다" 로 뒤집는다.**"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sources = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in (root / "agent_cli").rglob("*")
        if p.suffix in (".py", ".js")
    )
    for item in TRANSITIONAL_STRINGS:
        if item.endswith(".py"):
            assert (root / item).is_file(), item
        else:
            assert item in sources, item
