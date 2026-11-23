"""엔진 통합의 P1 임시물 목록 — P2 가 전부 지웠고, 이 테스트가 그것을 지킨다 (docs/agent-engine/DESIGN.md §4).

사용자 결정(2026-11-15): P1 의 임시 종류·어댑터는 두되, 지울 때 철저하게 없애 최종 디자인은
단일 모듈(루프 종류 하나 = `ResidentLoop`, 결과 = 회신)이어야 한다. 지울 이름을 여기 한 곳에
적었고(P1 에선 "있다" 를 확인했다), **P2(v10.141.0)에서 "저장소 어디에도 없다" 로 뒤집었다** —
``agent_cli/``·``tests/``·``docs/`` 어디에도(이 파일 제외) 이 이름·글이 다시 나타나면 실패한다.

이름은 그 자리에서 import 되지 않아야 하고(모듈·속성 모두), 낱말로도 소스·테스트·문서에 없어야
한다. 문서의 이력(설계 문서가 "무엇을 지웠다" 를 적는 자리)도 이 이름들을 쓰지 않는다 — 지운
것은 이 파일 한 곳이 기억한다.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

#: (모듈, 이름) — 이름이 None 이면 모듈 통째. 각 줄의 대체물은 DESIGN.md §4 표.
TRANSITIONAL: frozenset[tuple[str, str | None]] = frozenset(
    {
        # 결과 어댑터 — 결과는 회신(reply)으로
        ("agent_cli.subagent.run_result", None),
        ("agent_cli.subagent.run_result", "RunReply"),
        ("agent_cli.subagent.run_result", "write_result_md_and_tool_result"),
        ("agent_cli.subagent.run_result", "batch_tool_result"),
        # STATUS/RESULT 포장 — 어댑터만 쓰던 run 포맷터와 그 모듈(통째로 삭제)
        ("agent_cli.subagent.report", None),
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
        # P2 에서 찾아 더한 것 — 표에 없던 P1 짝들
        ("agent_cli.tools.agent_tool", "AgentTool.SUBLOOP_DESCRIPTION"),
        ("agent_cli.subagent.run_agent", "run_agents"),
        ("agent_cli.subagent.agents_live", "AgentRegistry._make_message_handler"),
    }
)

#: 이름이 아니라 글로 남는 임시물 — 저장소 grep 대상.
TRANSITIONAL_STRINGS: frozenset[str] = frozenset(
    {
        "STATUS: error\\nERROR: Subagent did not complete",
        # 모델 대면 명단에서 run 빼기 — P2 는 run 도 명단·주소에 나온다
        'not s.get("ends_after_task")',
        # run 채널 읽기 전용·✕/↻ 없음 — P2 는 직접 메시지, P3 는 채널 ■/✕
        "function ovActiveRun()",
        "This run agent takes no messages",
        'if (key !== "main" && !run)',
        "tests/equivalence/test_run_result_parity.py",  # P1 바이트 기준
        # P2 에서 찾아 더한 것
        "tests/equivalence/run_corpus.py",  # P1 코퍼스 구동기
        "tests/test_run_result.py",  # 옛 일회성 결과 포맷터의 테스트
        "has_agent_registry",  # 서브루프 축소 스키마의 분기 인자
    }
)

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()


def _repo_text() -> str:
    """``agent_cli/``·``tests/``·``docs/`` 의 소스·문서 전부(이 파일 제외)."""
    parts: list[str] = []
    for top in ("agent_cli", "tests", "docs"):
        for p in (ROOT / top).rglob("*"):
            if p.resolve() == SELF or p.suffix not in (".py", ".js", ".md", ".html"):
                continue
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _importable(module: str, name: str | None) -> bool:
    try:
        obj = importlib.import_module(module)
    except ModuleNotFoundError:
        return False
    for part in name.split(".") if name else []:
        if not hasattr(obj, part):
            return False
        obj = getattr(obj, part)
    return True


@pytest.mark.parametrize(("module", "name"), sorted(TRANSITIONAL, key=str))
def test_transitional_names_are_gone(module, name):
    """P2: 임시 이름은 그 자리에서 import 되지 않는다."""
    assert not _importable(module, name), f"{module}.{name or ''} still exists"


def test_transitional_names_appear_nowhere():
    """P2: 임시 이름(모듈은 마지막 마디, 속성은 이름)이 소스·테스트·문서 어디에도 없다."""
    text = _repo_text()
    left = []
    for module, name in TRANSITIONAL:
        word = (name or module).split(".")[-1]
        if name is None:
            word = module  # 모듈은 점 경로 그대로 찾는다("run_result" 는 흔한 낱말이다)
        if word in text:
            left.append(word)
    assert not left, f"transitional names still in the repo: {sorted(left)}"


def test_transitional_strings_are_gone():
    """P2: 글로 남는 임시물이 없다 — 파일 경로는 파일이 없고, 글은 어디에도 없다."""
    text = _repo_text()
    for item in TRANSITIONAL_STRINGS:
        if item.endswith(".py"):
            assert not (ROOT / item).exists(), item
        assert item not in text, item
