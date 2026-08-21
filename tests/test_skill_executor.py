"""Tests for skills/executor.py."""

from unittest.mock import MagicMock, patch

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.skills.executor import execute_skill
from agent_cli.skills.models import Skill


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=8000,
        max_output_tokens=2000,
        supports_thinking=False,
    )


@pytest.fixture
def ctx(tmp_path):
    return ContextManager(session_dir=tmp_path / "sessions" / "test")


def _make_skill(name="test", allowed_tools=None, prompt="Do $ARGUMENTS"):
    return Skill(
        name=name,
        description="test skill",
        prompt_template=prompt,
        allowed_tools=allowed_tools or [],
        max_turns=0,
        source_path="",
    )


class TestToolIntersection:
    def test_intersection_filters_tools(self, caps, ctx):
        """Skill tools ∩ parent tools = effective tools."""
        skill = _make_skill(allowed_tools=["read_file", "shell", "write_file"])

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            from agent_cli.tools.result import ToolResult as _TR

            mock_loop.return_value = _TR(True, output="done")
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
                parent_tools=["read_file", "shell"],
            )
            call_kwargs = mock_loop.call_args
            active_tools = call_kwargs.kwargs.get("active_tools") or call_kwargs[1].get(
                "active_tools"
            )
            assert set(active_tools) == {"read_file", "shell"}

    def test_empty_intersection_rejected(self, caps, ctx):
        """Empty intersection → execution rejected with error."""
        skill = _make_skill(allowed_tools=["write_file", "fetch"])

        result = execute_skill(
            skill=skill,
            arguments="test",
            provider=MagicMock(),
            capabilities=caps,
            model="test",
            ctx=ctx,
            parent_tools=["read_file", "shell"],
        )
        assert result is not None
        assert not result.success
        assert "cannot run" in result.error
        assert result.artifact == ""

    def test_no_parent_tools_uses_skill_tools(self, caps, ctx):
        """No parent_tools → use skill's tools as-is."""
        skill = _make_skill(allowed_tools=["read_file", "write_file"])

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            from agent_cli.tools.result import ToolResult as _TR

            mock_loop.return_value = _TR(True, output="done")
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
            )
            call_kwargs = mock_loop.call_args
            active_tools = call_kwargs.kwargs.get("active_tools") or call_kwargs[1].get(
                "active_tools"
            )
            assert set(active_tools) == {"read_file", "write_file"}

    def test_no_skill_tools_uses_parent_tools(self, caps, ctx):
        """Skill has no tool restriction → use parent's tools."""
        skill = _make_skill(allowed_tools=[])

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            from agent_cli.tools.result import ToolResult as _TR

            mock_loop.return_value = _TR(True, output="done")
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
                parent_tools=["read_file", "shell"],
            )
            call_kwargs = mock_loop.call_args
            active_tools = call_kwargs.kwargs.get("active_tools") or call_kwargs[1].get(
                "active_tools"
            )
            assert set(active_tools) == {"read_file", "shell"}


class TestParentRoleInheritance:
    def test_parent_role_passed_to_run_loop(self, caps, ctx):
        """parent_role is forwarded as agent_role to run_loop."""
        skill = _make_skill()

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            from agent_cli.tools.result import ToolResult as _TR

            mock_loop.return_value = _TR(True, output="done")
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
                parent_role="You are an explorer agent.",
            )
            call_kwargs = mock_loop.call_args
            agent_role = call_kwargs.kwargs.get("agent_role") or call_kwargs[1].get(
                "agent_role"
            )
            assert agent_role == "You are an explorer agent."

    def test_no_parent_role_empty(self, caps, ctx):
        """No parent_role → agent_role is empty."""
        skill = _make_skill()

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            from agent_cli.tools.result import ToolResult as _TR

            mock_loop.return_value = _TR(True, output="done")
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
            )
            call_kwargs = mock_loop.call_args
            agent_role = call_kwargs.kwargs.get("agent_role") or call_kwargs[1].get(
                "agent_role"
            )
            assert agent_role == ""

    def test_compaction_disabled_propagates_to_run_loop(self, caps, ctx):
        """Parent's compaction_enabled=False threads into the skill's run_loop
        (the internal flag flowing to subagents)."""
        skill = _make_skill()

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            from agent_cli.tools.result import ToolResult as _TR

            mock_loop.return_value = _TR(True, output="done")
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
                compaction_enabled=False,
            )
            assert mock_loop.call_args.kwargs.get("compaction_enabled") is False

    def test_compaction_defaults_enabled(self, caps, ctx):
        """Backward-compat: omitting compaction_enabled keeps compaction on."""
        skill = _make_skill()

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            from agent_cli.tools.result import ToolResult as _TR

            mock_loop.return_value = _TR(True, output="done")
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
            )
            assert mock_loop.call_args.kwargs.get("compaction_enabled") is True


class TestSkillSubdir:
    def test_skill_creates_subdir(self, caps, ctx):
        """Skill creates its own subdir with history.jsonl."""
        skill = _make_skill(name="summarize")

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            from agent_cli.tools.result import ToolResult as _TR

            mock_loop.return_value = _TR(True, output="done")
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
            )
            call_kwargs = mock_loop.call_args
            skill_ctx = call_kwargs.kwargs.get("ctx") or call_kwargs[1].get("ctx")
            # Should be a different ContextManager than parent
            assert skill_ctx is not ctx
            assert "skill_summarize" in str(skill_ctx.session_dir)


class TestSkillRegistryInheritance:
    """A skill is the MAIN agent's own workflow, so it inherits the running
    loop's agent_registry and can spawn/manage persistent workers (an
    orchestrate skill's whole point). depth bounds skill→skill recursion;
    spawn permission rides on the registry, NOT depth — so a skill (registry
    present) gets the full agent tool while an ordinary sub-agent (no registry)
    stays run-only. Before this wiring the registry was dropped at the skill
    boundary, so /orchestrate's `spawn` ops silently did nothing."""

    def test_skill_inherits_registry_for_spawn(self, caps, ctx):
        skill = _make_skill(allowed_tools=["read_file", "agent"])
        reg = MagicMock(name="registry")
        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            from agent_cli.tools.result import ToolResult as _TR

            mock_loop.return_value = _TR(True, output="done")
            execute_skill(
                skill=skill,
                arguments="orchestrate this",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
                agent_registry=reg,
            )
            # The skill's run_loop must receive the SAME registry (else spawn
            # ops in the skill body hit no registry and no-op).
            assert mock_loop.call_args.kwargs["agent_registry"] is reg

    def test_skill_subloop_exposes_spawn_but_subagent_does_not(self, caps):
        """Parity: the agent tool advertises spawn when a registry is present
        (skill / main) and stays run-only when it is absent (sub-agent)."""
        from agent_cli.prompts.system_prompt import build_system_prompt_sections

        def _txt(secs):
            return "\n".join(b for _n, b in secs)

        with_reg = _txt(
            build_system_prompt_sections(
                caps, active_tools=["read_file", "agent"], agent_registry=MagicMock()
            )
        )
        without = _txt(
            build_system_prompt_sections(
                caps, active_tools=["read_file", "agent"], agent_registry=None
            )
        )
        # registry present → full agent tool (spawn enum value visible)
        assert "spawn" in with_reg
        # registry absent → SUBLOOP description ("run ... only here")
        assert "only here" in without.lower()
        assert "spawn" not in without or "only here" in without.lower()


class TestSkillSpawnExecution:
    """v7.16.0 통합: skill 서브루프에서 spawn 이 실제로 tool_agent 까지 닿아
    성공하는지 (registry 배선의 end-to-end 증명). 유닛(passthrough)과 달리
    실제 spawn op 를 실행해 워커가 main registry 에 등록되고 'main-session
    only' 거부가 나지 않음을 확인. orchestrate skill 이 워커를 spawn·조율하는
    능력의 회귀 가드."""

    def test_spawn_in_skill_subloop_registers_worker_no_reject(self, caps, ctx):
        import tempfile
        from pathlib import Path

        from agent_cli.providers.base import LLMResponse
        from agent_cli.subagent.agents_live import AgentRegistry

        skill = _make_skill(
            name="orch", allowed_tools=["read_file", "agent"], prompt="do $ARGUMENTS"
        )
        prov = MagicMock()
        prov.call.side_effect = [
            LLMResponse(
                content='Spawn a worker.\n[{"action":"agent","mode":"spawn",'
                '"profile":"code-writer","name":"w1","task":"build"}]'
            ),
            LLMResponse(content='Done.\n[{"action":"complete","result":"ok"}]'),
        ]
        with tempfile.TemporaryDirectory() as d:
            from agent_cli.context.manager import ContextManager

            sctx = ContextManager(session_dir=Path(d))
            reg = AgentRegistry(session_dir=Path(d))
            try:
                execute_skill(
                    skill,
                    "make it",
                    prov,
                    caps,
                    "m",
                    ctx=sctx,
                    agent_registry=reg,
                    max_turns=4,
                )
                hist = Path(d) / "history.jsonl"
                rejected = hist.is_file() and "main-session only" in hist.read_text()
                worker_count = len(reg.roster_snapshot())
            finally:
                # 실워커 스레드를 join 하고 나서 TemporaryDirectory 를 닫는다 —
                # 부팅 중인 워커가 세션 디렉토리에 파일을 쓰는 도중 rmtree 가
                # 돌면 CI 에서 "Directory not empty" 로 간헐 실패 (느린 러너
                # 에서만 재현되는 teardown 레이스).
                reg.shutdown_all()
        # spawn 이 거부되지 않고 워커가 registry 에 등록돼야 (배선 완전)
        assert not rejected, "skill 서브루프 spawn 이 'main-session only' 로 거부됨"
        assert worker_count >= 1, "spawn 된 워커가 registry 에 등록 안 됨"


class TestMainRegistrySlotInheritance:
    """배선 통일 (v7.17.0): execute_skill 의 agent_registry 해석 3케이스.
    미지정(_INHERIT) → main 슬롯 자동 상속(사용자 /skill dispatch 등 main 의
    모든 skill 실행 경로가 배선 없이 spawn 가능), 명시 registry → 그대로,
    명시 None → 상속 안 함(서브에이전트 run-only 경계 보존 — dispatch 가
    cfg.agent_registry=None 을 명시 전달하는 경로)."""

    def _run_and_capture(self, caps, ctx, **kw):
        skill = _make_skill(allowed_tools=["read_file", "agent"])
        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            from agent_cli.tools.result import ToolResult as _TR

            mock_loop.return_value = _TR(True, output="done")
            execute_skill(
                skill=skill,
                arguments="t",
                provider=MagicMock(),
                capabilities=caps,
                model="m",
                ctx=ctx,
                **kw,
            )
            return mock_loop.call_args.kwargs["agent_registry"]

    def test_unspecified_inherits_main_slot(self, caps, ctx):
        from agent_cli.subagent.agents_live import set_main_registry

        reg = MagicMock(name="main-reg")
        set_main_registry(reg)
        # 미지정 → 슬롯 자동 상속 (사용자 슬래시 경로의 계약)
        assert self._run_and_capture(caps, ctx) is reg

    def test_explicit_registry_passes_through(self, caps, ctx):
        from agent_cli.subagent.agents_live import set_main_registry

        set_main_registry(MagicMock(name="slot"))
        explicit = MagicMock(name="explicit")
        assert self._run_and_capture(caps, ctx, agent_registry=explicit) is explicit

    def test_explicit_none_does_not_inherit(self, caps, ctx):
        """경계 핵심: 서브에이전트 루프의 dispatch 는 None 을 **명시** 전달 —
        슬롯이 차 있어도 상속하지 않아야 run-only 가 유지된다."""
        from agent_cli.subagent.agents_live import set_main_registry

        set_main_registry(MagicMock(name="slot"))
        assert self._run_and_capture(caps, ctx, agent_registry=None) is None

    def test_slot_empty_unspecified_is_none(self, caps, ctx):
        # 슬롯 미등록(예: headless) — 미지정이어도 None (거부 경로 그대로)
        assert self._run_and_capture(caps, ctx) is None


class TestPromptScopeUnification:
    """Inspector redesign: a skill's prompt snapshot must be keyed under the
    SAME scope id the timeline card carries (its ``data-task-id``), so clicking
    🔍 on the skill card resolves ``GET /api/debug/prompt?task_id=<card id>``.

    The scope-opening caller (main.py / loop.skill_invoke) passes ``scope_id``;
    ``execute_skill`` must then NOT open a second, divergent prompt scope — it
    registers under the caller's scope. Direct callers (no ``scope_id``) keep the
    self-managed fallback scope."""

    def _spy_renderer(self):
        r = MagicMock()
        r.begin_prompt_scope = MagicMock()
        r.end_prompt_scope = MagicMock()
        r.note_scope_ctx = MagicMock()
        return r

    def _run(self, caps, ctx, *, scope_id):
        from agent_cli.tools.result import ToolResult as _TR

        skill = _make_skill(name="plan")
        spy = self._spy_renderer()
        with (
            patch("agent_cli.skills.executor.run_loop") as mock_loop,
            patch("agent_cli.render.get_renderer", return_value=spy),
        ):
            mock_loop.return_value = _TR(True, output="ok")
            execute_skill(
                skill=skill,
                arguments="",
                provider=MagicMock(),
                capabilities=caps,
                model="m",
                ctx=ctx,
                scope_id=scope_id,
            )
        return spy

    def test_caller_scope_is_reused_no_second_scope(self, caps, ctx):
        # scope_id passed → execute_skill must NOT push its own prompt scope,
        # but MUST register the skill ctx (under the caller's top scope = card id).
        spy = self._run(caps, ctx, scope_id="skill-plan-card1")
        spy.begin_prompt_scope.assert_not_called()
        spy.end_prompt_scope.assert_not_called()
        spy.note_scope_ctx.assert_called_once()

    def test_direct_call_opens_fallback_scope(self, caps, ctx):
        # No scope_id (direct/test caller) → self-managed fallback scope so the
        # skill is still inspectable independently.
        spy = self._run(caps, ctx, scope_id="")
        spy.begin_prompt_scope.assert_called_once()
        spy.end_prompt_scope.assert_called_once()
        # begin/end use the SAME minted id (balanced push/pop).
        assert (
            spy.begin_prompt_scope.call_args.args[0]
            == spy.end_prompt_scope.call_args.args[0]
        )
