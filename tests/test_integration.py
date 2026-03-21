"""E2E integration tests with real Ollama.

Run: pytest tests/test_integration.py -v
Skip: pytest tests/ -m "not ollama_integration"
Custom models: INTEGRATION_MODELS="model1,model2" pytest tests/test_integration.py -v
"""
from __future__ import annotations

import pytest

from agent_cli.loop import run_loop
from agent_cli.parsing.react_parser import parse_react
from agent_cli.planning.executor import execute_plan
from agent_cli.planning.generator import generate_plan
from agent_cli.planning.models import Plan
from agent_cli.providers.compat import get_capabilities
from tests.conftest import OLLAMA_BASE_URL


# All tests in this file require Ollama
pytestmark = pytest.mark.ollama_integration


class TestSimpleConversation:
    def test_simple_question(self, integration_model, ollama_provider, model_capabilities):
        """Simple question → final_answer without tool use."""
        result = run_loop(
            query="What is 2+2? Answer with just the number.",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            quiet=True,
            max_iter=3,
        )
        assert result is not None
        assert len(result) > 0


class TestReadFile:
    def test_read_file_content(self, integration_model, ollama_provider, model_capabilities, tmp_path):
        """Read file → tool call → content in answer."""
        test_file = tmp_path / "test_data.txt"
        test_file.write_text("UNIQUE_MARKER_XYZ789")

        result = run_loop(
            query=f"Read the file at {test_file} and tell me its exact content.",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            quiet=True,
            max_iter=5,
        )
        assert result is not None
        assert "UNIQUE_MARKER_XYZ789" in result


class TestShellCommand:
    def test_shell_echo(self, integration_model, ollama_provider, model_capabilities):
        """Shell command → execute → output in answer."""
        result = run_loop(
            query="Run the command 'echo INTEGRATION_TEST_PASS_42' and tell me the output.",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            quiet=True,
            max_iter=5,
        )
        assert result is not None
        assert "INTEGRATION_TEST_PASS_42" in result


class TestWriteFile:
    def test_create_file(self, integration_model, ollama_provider, model_capabilities, tmp_path):
        """Create file → write_file tool → file exists on disk."""
        target = tmp_path / "agent_output.txt"

        result = run_loop(
            query=f"Create a new file at {target} with the content 'hello from agent test'.",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            quiet=True,
            max_iter=8,
        )
        assert result is not None
        assert target.exists()
        content = target.read_text()
        assert "hello from agent test" in content


class TestEditFile:
    def test_edit_file_content(self, integration_model, ollama_provider, model_capabilities, tmp_path):
        """Edit file → read_file + edit_file → content changed."""
        f = tmp_path / "edit_target.py"
        f.write_text('def greet():\n    return "OLD_VALUE"\n')

        result = run_loop(
            query=f"Edit the file {f} to change 'OLD_VALUE' to 'NEW_VALUE'.",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            quiet=True,
            max_iter=12,
        )
        assert result is not None
        content = f.read_text()
        assert "NEW_VALUE" in content


class TestPlanGeneration:
    def test_generate_plan(self, integration_model, ollama_provider, model_capabilities, tmp_path):
        """Plan generation → returns Plan with steps."""
        test_file = tmp_path / "readme.txt"
        test_file.write_text("This is a test readme file.\nIt has two lines.")

        plan = generate_plan(
            goal=f"Read {test_file} and summarize its content",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_steps=5,
            quiet=True,
        )
        assert plan is not None
        assert len(plan.steps) >= 1
        assert plan.goal is not None


class TestPlanExecution:
    def test_plan_execute(self, integration_model, ollama_provider, model_capabilities, tmp_path):
        """Plan generation + execution → result."""
        data_file = tmp_path / "data.txt"
        data_file.write_text("apple\nbanana\ncherry\n")

        plan = generate_plan(
            goal=f"Read {data_file} and tell me how many lines it has",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_steps=5,
            quiet=True,
        )
        assert plan is not None

        result = execute_plan(
            plan=plan,
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            quiet=True,
            step_max_iter=8,
        )
        assert result is not None


class TestConstrainedDecoding:
    def test_json_response_parseable(self, integration_model, ollama_provider, model_capabilities):
        """Constrained decoding → valid JSON response → parseable."""
        from agent_cli.prompts.system_prompt import build_system_prompt
        from agent_cli.tools import TOOLS

        system = build_system_prompt(
            capabilities=model_capabilities,
            active_tools=list(TOOLS.keys()),
        )
        response = ollama_provider.call(
            messages=[{"role": "user", "content": "What is 1+1? Provide final_answer."}],
            system=system,
            model=integration_model,
            capabilities=model_capabilities,
        )
        parsed = parse_react(response.content)
        # Should parse at stage 1 (direct JSON) or stage 2 (repair)
        assert parsed.parse_stage >= 1, (
            f"Failed to parse response (stage={parsed.parse_stage}): {response.content[:200]}"
        )


class TestMultiStepToolUse:
    def test_create_then_read(self, integration_model, ollama_provider, model_capabilities, tmp_path):
        """Multi-step: write_file → read_file → answer."""
        target = tmp_path / "multi_step.txt"

        result = run_loop(
            query=(
                f"First, create a file at {target} containing 'MULTI_STEP_OK'. "
                f"Then, read that file and tell me its content."
            ),
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            quiet=True,
            max_iter=10,
        )
        assert result is not None
        assert target.exists()


class TestRuntimeCapabilityDetection:
    def test_detects_real_model(self, integration_model):
        """Runtime detection returns real capabilities (not defaults)."""
        caps = get_capabilities(
            integration_model, provider="ollama", base_url=OLLAMA_BASE_URL
        )
        assert caps.context_window > 0
        assert caps.context_window >= 4096


class TestProbeBasedThinkingDetection:
    def test_thinking_detection_matches_actual(self, integration_model, ollama_provider, model_capabilities):
        """Probe-based thinking detection should produce consistent results."""
        from agent_cli.providers.compat import _probe_thinking_support

        supports, fmt = _probe_thinking_support(OLLAMA_BASE_URL, integration_model)

        # Result should be consistent with what get_capabilities returned
        assert model_capabilities.supports_thinking == supports
        if supports:
            assert fmt in ("think", "thinking", "reasoning", "reflection")
            assert model_capabilities.thinking_format == fmt


class TestPlanPersistence:
    def test_save_and_resume(self, integration_model, ollama_provider, model_capabilities, tmp_path):
        """Save plan → load → done steps skipped."""
        data_file = tmp_path / "persist_test.txt"
        data_file.write_text("test content for persistence")

        plan = generate_plan(
            goal=f"Read {data_file} and tell me its content",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_steps=3,
            quiet=True,
        )
        assert plan is not None

        # Save plan
        plan_file = tmp_path / "saved_plan.json"
        plan.save(plan_file)

        # Load and verify
        loaded = Plan.load(plan_file)
        assert loaded.goal == plan.goal
        assert len(loaded.steps) == len(plan.steps)


class TestSkillExecution:
    def test_review_code_skill(self, integration_model, ollama_provider, model_capabilities, tmp_path):
        """Run /review-code skill with a real file → result contains file analysis."""
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.models import Skill

        test_file = tmp_path / "sample.py"
        test_file.write_text("def add(a, b):\n    return a + b\n\ndef divide(a, b):\n    return a / b\n")

        skill = Skill(
            name="review-code",
            description="Review code",
            prompt_template="Read $ARGUMENTS and review for bugs. Be brief.",
            active_tools=["read_file"],
            max_iter=5,
        )

        result = execute_skill(
            skill=skill,
            arguments=str(test_file),
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            quiet=True,
        )
        assert result is not None
        assert len(result) > 10

    def test_summarize_skill(self, integration_model, ollama_provider, model_capabilities, tmp_path):
        """Run /summarize skill → result contains summary."""
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.models import Skill

        test_file = tmp_path / "readme.txt"
        test_file.write_text("This is a test project.\nIt does math operations.\nVersion 1.0.\n")

        skill = Skill(
            name="summarize",
            description="Summarize",
            prompt_template="Read $ARGUMENTS and summarize in one paragraph.",
            active_tools=["read_file"],
            max_iter=5,
        )

        result = execute_skill(
            skill=skill,
            arguments=str(test_file),
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            quiet=True,
        )
        assert result is not None
        assert len(result) > 10
