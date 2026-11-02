"""`run_skill` 은 모델이 부를 수 있는 스킬이 있을 때만 광고된다 (v10.100.0).

- 전부 ``disable-model-invocation`` 이면 Skills 절도 도구도 빠진다 (종전엔 목록 없는
  머리 — 제목·호출 예시 — 만 실렸고 도구는 남았다).
- "not found" 안내의 목록은 모델이 부를 수 있는 것만 — 사용자 전용 이름을 보여 주면
  다음 턴에 그 이름으로 다시 불러 거절을 한 번 더 받는다.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.skills.models import Skill
from tests.binding_support import make_binding
from tests.loop_kinds import make_caller


def _skill(name, *, user_only=False, desc="does a thing"):
    return Skill(
        name=name,
        description=desc,
        prompt_template="x",
        disable_model_invocation=user_only,
    )


@pytest.fixture
def skills(monkeypatch):
    """`load_skills` 가 돌려줄 스킬을 테스트가 정한다 — 디스크를 읽지 않는다."""
    box: dict = {}

    def fake():
        return dict(box)

    monkeypatch.setattr("agent_cli.skills.loader.load_skills", fake)
    monkeypatch.setattr("agent_cli.skills.load_skills", fake)
    return box


def _caps():
    return ModelCapabilities(
        context_window=32768, max_output_tokens=4096, supports_thinking=False
    )


def _tools_list():
    from agent_cli.loop import AgentLoop
    from tests.loop_ports import make_ports

    loop = AgentLoop(
        query="Q",
        provider=MagicMock(),
        capabilities=_caps(),
        model="m",
        ctx=None,
        depth=0,
        max_depth=3,
        ports=make_ports(),
    )
    return list(loop.tools_list)


class TestSkillsSection:
    def test_user_only_skills_give_no_section(self, skills):
        from agent_cli.prompts.system_prompt import build_skill_descriptions

        skills["create-agent"] = _skill("create-agent", user_only=True)
        assert build_skill_descriptions() == ""

    def test_section_lists_only_model_skills(self, skills):
        from agent_cli.prompts.system_prompt import build_skill_descriptions

        skills["plan"] = _skill("plan")
        skills["create-agent"] = _skill("create-agent", user_only=True)
        out = build_skill_descriptions()
        assert "`plan`" in out and "create-agent" not in out

    def test_no_encouragement_line(self, skills):
        """권유 문장은 뺐다 — 위임 안내는 비용 우선(v10.26.0)."""
        from agent_cli.prompts.system_prompt import build_skill_descriptions

        skills["plan"] = _skill("plan")
        assert "Consider using skills" not in build_skill_descriptions()


class TestToolOffer:
    def test_run_skill_offered_with_a_model_skill(self, skills):
        skills["plan"] = _skill("plan")
        assert "run_skill" in _tools_list()

    def test_run_skill_hidden_when_all_user_only(self, skills):
        skills["create-agent"] = _skill("create-agent", user_only=True)
        assert "run_skill" not in _tools_list()

    def test_run_skill_hidden_with_no_skills(self, skills):
        assert "run_skill" not in _tools_list()


class TestNotFound:
    def _call(self, name):
        from agent_cli.loop import _handle_run_skill

        return _handle_run_skill(
            {"name": name},
            make_caller(
                binding=make_binding(
                    provider_name="openai",
                    base_url="http://localhost:11434",
                    api_key="",
                    capabilities=_caps(),
                    model="test",
                ),
                ctx=None,
                session=None,
            ),
        )

    def test_lists_only_callable_skills(self, skills):
        skills["plan"] = _skill("plan")
        skills["create-agent"] = _skill("create-agent", user_only=True)
        res = self._call("video-transcribe")
        assert not res.success
        assert res.error == 'Skill "video-transcribe" not found. Available: plan'

    def test_none_callable(self, skills):
        skills["create-agent"] = _skill("create-agent", user_only=True)
        res = self._call("x")
        assert res.error.endswith("Available: (none)")

    def test_user_only_name_still_says_user_only(self, skills):
        skills["create-agent"] = _skill("create-agent", user_only=True)
        res = self._call("create-agent")
        assert "user-only" in res.error


def test_orchestrate_is_for_several_specialists_only():
    from pathlib import Path

    import agent_cli
    from agent_cli.skills.loader import _parse_skill_file

    p = Path(agent_cli.__file__).parent / "skills" / "builtin" / "orchestrate.md"
    desc = _parse_skill_file(p).description
    assert desc.endswith("Use when a task needs more than one specialist.")
    assert "few steps" not in desc


def test_inventory_limit_scenario_matches_a_real_loop():
    """인벤토리의 깊이 상한 시나리오는 루프 조립과 같은 판정 — `run_skill`·`agent` 와
    Skills·Agent Profiles 절이 없다 (상한 줄만 있고 도구는 실리는 모순이 없게)."""
    from agent_cli.prompts.inventory import build_inventory

    for d in build_inventory()["dialects"].values():
        sc = d["scenarios"]["limit"]
        assert "run_skill" not in sc["tools"] and "agent" not in sc["tools"]
        names = [s["name"] for s in sc["sections"]]
        assert "Skills" not in names and "Agent Profiles" not in names
