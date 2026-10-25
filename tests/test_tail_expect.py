"""꼬리 재설계 2단계 (v10.74.0, docs/tail/DESIGN.md §7): 추론 줄이 "계획을 말하라" 에서
"호출마다 기대 결과와 실패 시 행동을 한 줄씩" 으로 바뀌었다 — 산문 지시이고 op 필드가
아니다(문법·native_fc 에 무해)."""

from __future__ import annotations

from agent_cli.prompts.system_prompt import TASK_GUIDELINES

EXPECT_LINE = (
    "Before the calls, say in one line each what result you expect and what you "
    "will do if it fails; then act."
)


def test_reasoning_line_asks_for_expectation_and_fallback():
    lines = [ln for ln in TASK_GUIDELINES.splitlines() if ln.startswith("- Reason in")]
    assert len(lines) == 1
    (ln,) = lines
    assert ln.endswith(EXPECT_LINE)
    assert "State the plan, then act" not in TASK_GUIDELINES
    # 간결 규칙은 그대로 앞에 남는다
    assert "one point per sentence, active voice, no repetition" in ln


def test_it_is_prose_not_a_wire_field():
    """op 모양에 `expect`/`on_fail` 같은 필드를 요구하지 않는다 — 방언·문법이 모르는
    필드를 모델이 보내면 거부되므로, 지시는 글로만 한다."""
    assert "`expect`" not in TASK_GUIDELINES and "`on_fail`" not in TASK_GUIDELINES
    assert "field" not in TASK_GUIDELINES.split("- Reason in", 1)[1].split("\n", 1)[0]
