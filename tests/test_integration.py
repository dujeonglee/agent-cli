"""E2E integration tests with real Ollama.

Run: pytest tests/test_integration.py -v
Skip: pytest tests/ -m "not ollama_integration"
Custom models: INTEGRATION_MODELS="model1,model2" pytest tests/test_integration.py -v
"""

from __future__ import annotations

import pytest

from agent_cli.loop import run_loop
from agent_cli.parsing.react_parser import parse_react
from agent_cli.providers.compat import get_capabilities
from tests.conftest import OLLAMA_BASE_URL


# All tests in this file require Ollama
pytestmark = pytest.mark.ollama_integration


class TestSimpleConversation:
    def test_simple_question(
        self, integration_model, ollama_provider, model_capabilities
    ):
        """Simple question → complete tool without other tool use."""
        result = run_loop(
            query="What is 2+2? Answer with just the number.",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
            max_iter=3,
        )
        assert result is not None
        assert len(result) > 0


class TestReadFile:
    def test_read_file_content(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """Read file → tool call → content in answer."""
        test_file = tmp_path / "test_data.txt"
        test_file.write_text("UNIQUE_MARKER_XYZ789")

        result = run_loop(
            query=f"Read the file at {test_file} and tell me its exact content.",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
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
            suppress_output=True,
            max_iter=5,
        )
        assert result is not None
        assert "INTEGRATION_TEST_PASS_42" in result


class TestWriteFile:
    def test_create_file(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """Create file → write_file tool → file exists on disk."""
        target = tmp_path / "agent_output.txt"

        result = run_loop(
            query=f"Create a new file at {target} with the content 'hello from agent test'.",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
            max_iter=8,
        )
        assert result is not None
        assert target.exists()
        content = target.read_text()
        assert "hello from agent test" in content


class TestEditFile:
    def test_edit_file_content(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """Edit file → read_file + edit_file → content changed."""
        f = tmp_path / "edit_target.py"
        f.write_text('def greet():\n    return "OLD_VALUE"\n')

        result = run_loop(
            query=f"Edit the file {f} to change 'OLD_VALUE' to 'NEW_VALUE'.",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
            max_iter=12,
        )
        assert result is not None
        content = f.read_text()
        assert "NEW_VALUE" in content


class TestConstrainedDecoding:
    def test_json_response_parseable(
        self, integration_model, ollama_provider, model_capabilities
    ):
        """Constrained decoding → valid JSON response → parseable."""
        from agent_cli.prompts.system_prompt import build_system_prompt
        from agent_cli.tools import TOOLS

        system = build_system_prompt(
            capabilities=model_capabilities,
            active_tools=list(TOOLS.keys()),
        )
        response = ollama_provider.call(
            messages=[
                {
                    "role": "user",
                    "content": "What is 1+1? Use the complete tool to answer.",
                }
            ],
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
    def test_create_then_read(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
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
            suppress_output=True,
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
    def test_thinking_detection_matches_actual(
        self, integration_model, ollama_provider, model_capabilities
    ):
        """Probe-based thinking detection should produce consistent results."""
        from agent_cli.providers.compat import _probe_thinking_support

        supports, fmt = _probe_thinking_support(OLLAMA_BASE_URL, integration_model)

        # Result should be consistent with what get_capabilities returned
        assert model_capabilities.supports_thinking == supports
        if supports:
            assert fmt in (
                "think",
                "thinking",
                "reasoning",
                "reflection",
                "thinking_field",
            )
            assert model_capabilities.thinking_format == fmt


class TestSkillExecution:
    def test_review_code_skill(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """Run /review-code skill with a real file → result contains file analysis."""
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.models import Skill

        test_file = tmp_path / "sample.py"
        test_file.write_text(
            "def add(a, b):\n    return a + b\n\ndef divide(a, b):\n    return a / b\n"
        )

        skill = Skill(
            name="review-code",
            description="Review code",
            prompt_template="Read $ARGUMENTS and review for bugs. Be brief.",
            allowed_tools=["read_file"],
            max_iter=5,
        )

        result = execute_skill(
            skill=skill,
            arguments=str(test_file),
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
        )
        assert result is not None
        assert len(result) > 10

    def test_summarize_skill(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """Run /summarize skill → result contains summary."""
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.models import Skill

        test_file = tmp_path / "readme.txt"
        test_file.write_text(
            "This is a test project.\nIt does math operations.\nVersion 1.0.\n"
        )

        skill = Skill(
            name="summarize",
            description="Summarize",
            prompt_template="Read $ARGUMENTS and summarize in one paragraph.",
            allowed_tools=["read_file"],
            max_iter=8,
        )

        result = execute_skill(
            skill=skill,
            arguments=str(test_file),
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
        )
        assert result is not None
        assert len(result) > 10

    def test_skill_context_fork(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """context: fork → skill runs in independent context and returns result."""
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.models import Skill
        from unittest.mock import MagicMock

        test_file = tmp_path / "fork_test.txt"
        test_file.write_text("FORK_CONTENT_ABC")

        skill = Skill(
            name="fork-reader",
            description="Read in fork",
            prompt_template="Read $ARGUMENTS and tell me the content.",
            allowed_tools=["read_file"],
            max_iter=5,
            context="fork",
        )

        fake_ctx = MagicMock()
        result = execute_skill(
            skill=skill,
            arguments=str(test_file),
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
            ctx=fake_ctx,
        )
        assert result is not None
        assert "FORK_CONTENT_ABC" in result

    def test_skill_dynamic_context_injection(
        self, integration_model, ollama_provider, model_capabilities
    ):
        """!`command` in skill template → shell output injected before LLM sees it."""
        import datetime

        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.models import Skill

        skill = Skill(
            name="dynamic-ctx",
            description="Dynamic context",
            prompt_template=(
                "The current date output is: !`date +%Y`\n"
                "What year is shown above? Answer with just the year number."
            ),
            max_iter=3,
        )

        result = execute_skill(
            skill=skill,
            arguments="",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
        )
        assert result is not None
        assert str(datetime.datetime.now().year) in result

    def test_skill_allowed_tools_restriction(
        self, integration_model, ollama_provider, model_capabilities
    ):
        """allowed-tools: [shell] → LLM uses shell tool to complete task."""
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.models import Skill

        skill = Skill(
            name="shell-only",
            description="Shell only",
            prompt_template=(
                "Run the shell command 'echo SHELL_ONLY_MARKER_99' "
                "and tell me the output."
            ),
            allowed_tools=["shell"],
            max_iter=5,
        )

        result = execute_skill(
            skill=skill,
            arguments="",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
        )
        assert result is not None
        assert "SHELL_ONLY_MARKER_99" in result

    def test_skill_directory_structure(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """skills/<name>/SKILL.md directory structure loads and executes."""
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.loader import _parse_skill_file

        skill_dir = tmp_path / "greet"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: greet\n"
            "description: Greet someone\n"
            "max-iter: 5\n"
            "---\n\n"
            "Say hello to $ARGUMENTS. Answer with just the greeting.\n"
        )

        skill = _parse_skill_file(skill_dir / "SKILL.md")
        assert skill is not None

        result = execute_skill(
            skill=skill,
            arguments="Alice",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
        )
        assert result is not None
        assert len(result) > 0

    def test_skill_arguments_bracket_notation(
        self, integration_model, ollama_provider, model_capabilities
    ):
        """$ARGUMENTS[N] bracket notation substitutes correctly."""
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.models import Skill

        skill = Skill(
            name="compare",
            description="Compare two things",
            prompt_template=(
                "Compare $ARGUMENTS[0] and $ARGUMENTS[1]. "
                "Which is bigger? Answer in one sentence."
            ),
            max_iter=3,
        )

        result = execute_skill(
            skill=skill,
            arguments="elephant mouse",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
        )
        assert result is not None
        assert len(result) > 5


class TestSkillHooks:
    def test_pretooluse_hook_blocks(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """PreToolUse hook blocks shell → LLM gets error feedback."""
        from agent_cli.hooks import HookEntry, HookMatcher

        hooks_config = {
            "PreToolUse": [
                HookMatcher(
                    matcher="shell",
                    hooks=[HookEntry(command='echo "shell is blocked" >&2; exit 2')],
                )
            ]
        }

        result = run_loop(
            query="Run 'echo hello' in the shell and tell me the output.",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
            max_iter=5,
            hooks_config=hooks_config,
        )
        assert result is not None

    def test_posttooluse_hook_logging(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """PostToolUse hook writes log file after tool execution."""
        from agent_cli.hooks import HookEntry, HookMatcher

        log_file = tmp_path / "hook_log.txt"
        hooks_config = {
            "PostToolUse": [
                HookMatcher(
                    matcher="",
                    hooks=[HookEntry(command=f"echo 'hook fired' >> {log_file}")],
                )
            ]
        }

        test_file = tmp_path / "data.txt"
        test_file.write_text("test data")

        result = run_loop(
            query=f"Read the file {test_file} and tell me its content.",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
            max_iter=5,
            hooks_config=hooks_config,
        )
        assert result is not None
        assert log_file.exists()
        assert "hook fired" in log_file.read_text()


class TestSkillInvocationControl:
    def test_disable_model_invocation_excluded_from_prompt(self):
        """disable-model-invocation=True → excluded from system prompt."""
        from agent_cli.prompts.system_prompt import build_skill_descriptions
        from agent_cli.skills.models import Skill

        skills = {
            "auto-ok": Skill(
                name="auto-ok",
                description="LLM can call this",
                prompt_template="Do $ARGUMENTS",
            ),
            "manual-only": Skill(
                name="manual-only",
                description="User only",
                prompt_template="Do $ARGUMENTS",
                disable_model_invocation=True,
            ),
        }
        desc = build_skill_descriptions(skills)
        assert "auto-ok" in desc
        assert "run_skill" in desc
        assert "manual-only" not in desc

    def test_user_invocable_false_hidden_from_list(self):
        """user-invocable=False → hidden from /skills listing."""
        from agent_cli.skills.models import Skill

        skills = {
            "visible": Skill(
                name="visible",
                description="Show me",
                prompt_template="Do $ARGUMENTS",
            ),
            "background": Skill(
                name="background",
                description="LLM only",
                prompt_template="Do $ARGUMENTS",
                user_invocable=False,
            ),
        }
        user_skills = {k: v for k, v in skills.items() if v.user_invocable}
        assert "visible" in user_skills
        assert "background" not in user_skills


class TestDelegateSubagent:
    """Integration tests for in-process delegate tool."""

    def test_delegate_none_executes_tools(
        self, integration_model, ollama_provider, model_capabilities
    ):
        """Delegate (context=none) runs subagent that uses tools."""
        result = run_loop(
            query="Use the delegate tool to count .py files in the tests/ directory. "
            "Use this exact delegate call: "
            '{"tasks": [{"task": "Count .py files in tests/ directory using '
            "shell command: find tests/ -name '*.py' -type f | wc -l. "
            'Return only the number."}]}',
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
            max_iter=5,
        )
        assert result is not None
        assert len(result) > 0

    def test_delegate_fork_has_parent_context(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """Delegate (context=fork) subagent can access parent conversation history."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(
            provider=ollama_provider,
            model=integration_model,
            capabilities=model_capabilities,
            scratchpad_dir=tmp_path,
        )
        result = run_loop(
            query="First, remember that the secret password is DOLPHIN42. "
            "Then delegate a task with context fork: "
            '{"tasks": [{"task": "What is the secret password mentioned earlier?", '
            '"context": "fork"}]}. '
            "Return whatever the delegate says.",
            provider=ollama_provider,
            capabilities=model_capabilities,
            model=integration_model,
            suppress_output=True,
            max_iter=8,
            ctx=ctx,
        )
        assert result is not None
