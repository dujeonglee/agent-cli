"""실패한 `run_skill` 의 관찰에 사유가 보인다 (v10.121.1).

종전엔 두 군데서 사유가 사라졌다:

- `_handle_run_skill` 이 실패한 스킬 결과도 ``output`` 에 "STATUS: success" 틀로 담았다 —
  관찰은 실패 결과의 ``error`` 를 그리므로 본문이 비었다.
- `SkillLoop.finish` 가 출력이 없을 때 엔진의 사유("Max turns (1) reached" 등)를 버리고
  "Skill returned no result" 만 남겼다.

엔진(`SkillLoop._engine`)만 바꾼다 — 결과 포장(`finish`)과 `_handle_run_skill` 은 진짜다.
"""

from __future__ import annotations

from unittest.mock import patch

from agent_cli.tools import TOOLS
from agent_cli.tools.result import ToolResult
from tests.loop_kinds import make_caller


def _engine_returns(result):
    def engine(kind, start, ctx):
        return result, 0.1

    return engine


def _invoke(engine_result, arguments="x"):
    from agent_cli.loop.skill_invoke import _handle_run_skill

    with patch(
        "agent_cli.skills.executor.SkillLoop._engine",
        lambda self: _engine_returns(engine_result),
    ):
        return _handle_run_skill(
            {"name": "plan", "arguments": arguments},
            make_caller(binding=None, ctx=None, session=None),
        )


def _skill_loop():
    from agent_cli.skills.executor import SkillLoop
    from agent_cli.skills.models import Skill

    return SkillLoop(
        Skill(name="s", description="d", prompt_template="t"),
        "",
        make_caller(binding=None, ctx=None, session=None),
    )


class TestHandleRunSkillFailure:
    def test_failure_is_an_error_with_the_reason(self):
        res = _invoke(ToolResult(False, error="Max turns (3) reached"))
        assert res.success is False
        assert res.error == "SKILL: plan(x)\nMax turns (3) reached"
        assert "STATUS: success" not in (res.output or "")

    def test_observation_body_shows_the_reason(self):
        res = _invoke(ToolResult(False, error="Max turns (3) reached"))
        body = TOOLS["run_skill"].render_observation(res, {})
        assert "Max turns (3) reached" in body
        assert "SKILL: plan(x)" in body
        assert "STATUS: success" not in body

    def test_failure_without_arguments_has_bare_header(self):
        res = _invoke(ToolResult(False, error="boom"), arguments="")
        assert res.success is False
        assert res.error == "SKILL: plan\nboom"

    def test_success_path_unchanged(self):
        res = _invoke(ToolResult(True, output="the plan"))
        assert res.success is True
        assert res.output == "STATUS: success\nRESULT:\nSKILL: plan(x)\nthe plan"
        assert not res.error


class TestSkillLoopFinish:
    def test_engine_reason_is_kept(self):
        res = _skill_loop().finish(
            ToolResult(False, error="Max turns (1) reached"), 0.0, ""
        )
        assert res.success is False
        assert res.error == "Max turns (1) reached"

    def test_empty_failure_still_says_no_result(self):
        loop = _skill_loop()
        assert loop.finish(ToolResult(False, error=""), 0.0, "").error == (
            "Skill returned no result"
        )
        assert loop.finish(ToolResult(True, output=""), 0.0, "").error == (
            "Skill returned no result"
        )

    def test_success_output_passes_through(self):
        res = _skill_loop().finish(ToolResult(True, output="done"), 0.0, "")
        assert res.success is True and res.output == "done"
