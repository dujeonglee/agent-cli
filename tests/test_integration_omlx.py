"""E2E integration tests against a live OpenAI-compatible omlx server.

Run: pytest tests/test_integration_omlx.py -m omlx_integration -v
Custom: OMLX_BASE_URL=... INTEGRATION_MODELS="m1,m2" pytest ... -m omlx_integration

Skips automatically when the server is unreachable (see conftest), so a
plain ``pytest tests/`` stays green.
"""

from __future__ import annotations

import pytest

from agent_cli.loop import run_loop
from agent_cli.providers.capabilities import get_capabilities
from agent_cli.wire_formats.react import parse_react
from tests.conftest import OMLX_BASE_URL

# All tests in this file require a live omlx server.
pytestmark = pytest.mark.omlx_integration


class TestSimpleConversation:
    def test_simple_question(
        self, integration_model, omlx_provider, model_capabilities
    ):
        """Simple question → complete tool without other tool use."""
        result = run_loop(
            query="What is 2+2? Answer with just the number.",
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_turns=3,
        )
        assert result.success
        assert len(result.output) > 0


class TestReadFile:
    def test_read_file_content(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """Read file → tool call → content in answer."""
        test_file = tmp_path / "test_data.txt"
        test_file.write_text("UNIQUE_MARKER_XYZ789")

        result = run_loop(
            query=f"Read the file at {test_file} and tell me its exact content.",
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_turns=5,
        )
        assert result.success
        assert "UNIQUE_MARKER_XYZ789" in result.output


class TestShellCommand:
    def test_shell_echo(self, integration_model, omlx_provider, model_capabilities):
        """Shell command → execute → output in answer."""
        result = run_loop(
            query="Run the command 'echo INTEGRATION_TEST_PASS_42' and tell me the output.",
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_turns=5,
        )
        assert result.success
        assert "INTEGRATION_TEST_PASS_42" in result.output


class TestWriteFile:
    def test_create_file(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """Create file → write_file tool → file exists on disk."""
        target = tmp_path / "agent_output.txt"

        result = run_loop(
            query=f"Create a new file at {target} with the content 'hello from agent test'.",
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_turns=8,
        )
        assert result.success
        assert target.exists()
        assert "hello from agent test" in target.read_text()


class TestEditFile:
    def test_edit_file_content(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """Edit file → read_file + edit_file → content changed."""
        f = tmp_path / "edit_target.py"
        f.write_text('def greet():\n    return "OLD_VALUE"\n')

        result = run_loop(
            query=f"Edit the file {f} to change 'OLD_VALUE' to 'NEW_VALUE'.",
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_turns=12,
        )
        assert result.success
        assert "NEW_VALUE" in f.read_text()


class TestConstrainedDecoding:
    def test_json_response_parseable(
        self, integration_model, omlx_provider, model_capabilities
    ):
        """A real provider.call should return a ReAct-parseable response."""
        from agent_cli.prompts.system_prompt import build_system_prompt
        from agent_cli.tools import TOOLS

        system = build_system_prompt(
            capabilities=model_capabilities,
            active_tools=list(TOOLS.keys()),
        )
        response = omlx_provider.call(
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
        # Should parse at stage 1 (direct JSON) or stage 2 (repair).
        assert parsed.parse_stage >= 1, (
            f"Failed to parse (stage={parsed.parse_stage}): {response.content[:200]}"
        )


class TestMultiStepToolUse:
    def test_create_then_read(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """Multi-step: write_file → read_file → answer."""
        target = tmp_path / "multi_step.txt"

        result = run_loop(
            query=(
                f"First, create a file at {target} containing 'MULTI_STEP_OK'. "
                f"Then, read that file and tell me its content."
            ),
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_turns=10,
        )
        assert result.success
        assert target.exists()


class TestRuntimeCapabilityDetection:
    def test_detects_real_model(self, integration_model, omlx_available):
        """Runtime detection returns real capabilities (not defaults)."""
        caps = get_capabilities(
            integration_model, provider="openai", base_url=OMLX_BASE_URL
        )
        assert caps.context_window >= 4096


class TestSkillExecution:
    def test_review_code_skill(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """Run a review skill with a real file → result contains analysis."""
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
            max_turns=5,
        )

        result = execute_skill(
            skill=skill,
            arguments=str(test_file),
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
        )
        assert result.success
        assert len(result.output) > 10

    def test_summarize_skill(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """Run a summarize skill → result contains summary."""
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
            max_turns=8,
        )

        result = execute_skill(
            skill=skill,
            arguments=str(test_file),
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
        )
        assert result.success
        assert len(result.output) > 10

    def test_skill_context_fork(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """context: fork → skill runs in independent context and returns result."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.models import Skill

        test_file = tmp_path / "fork_test.txt"
        test_file.write_text("FORK_CONTENT_ABC")

        skill = Skill(
            name="fork-reader",
            description="Read in fork",
            prompt_template="Read $ARGUMENTS and tell me the content.",
            allowed_tools=["read_file"],
            max_turns=5,
            context="fork",
        )

        # fork serializes the parent conversation into the child context, so
        # the parent must be a real ContextManager (a MagicMock yields
        # non-serializable message history).
        ctx = ContextManager(
            session_dir=tmp_path,
            max_context_tokens=model_capabilities.context_window,
        )
        result = execute_skill(
            skill=skill,
            arguments=str(test_file),
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
            ctx=ctx,
        )
        assert result.success
        assert "FORK_CONTENT_ABC" in result.output

    def test_skill_dynamic_context_injection(
        self, integration_model, omlx_provider, model_capabilities
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
            max_turns=3,
        )

        result = execute_skill(
            skill=skill,
            arguments="",
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
        )
        assert result.success
        assert str(datetime.datetime.now().year) in result.output

    def test_skill_allowed_tools_restriction(
        self, integration_model, omlx_provider, model_capabilities
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
            max_turns=5,
        )

        result = execute_skill(
            skill=skill,
            arguments="",
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
        )
        assert result.success
        assert "SHELL_ONLY_MARKER_99" in result.output

    def test_skill_directory_structure(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
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
            "max-turns: 5\n"
            "---\n\n"
            "Say hello to $ARGUMENTS. Answer with just the greeting.\n"
        )

        skill = _parse_skill_file(skill_dir / "SKILL.md")
        assert skill is not None

        result = execute_skill(
            skill=skill,
            arguments="Alice",
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
        )
        assert result.success
        assert len(result.output) > 0

    def test_skill_arguments_bracket_notation(
        self, integration_model, omlx_provider, model_capabilities
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
            max_turns=3,
        )

        result = execute_skill(
            skill=skill,
            arguments="elephant mouse",
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
        )
        assert result.success
        assert len(result.output) > 5


class TestSkillHooks:
    def test_pretooluse_hook_blocks(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
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
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_turns=5,
            hooks_config=hooks_config,
        )
        assert result.success

    def test_posttooluse_hook_logging(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
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
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_turns=5,
            hooks_config=hooks_config,
        )
        assert result.success
        assert log_file.exists()
        assert "hook fired" in log_file.read_text()


class TestDelegateSubagent:
    """Integration tests for the in-process delegate tool."""

    def test_delegate_none_executes_tools(
        self, integration_model, omlx_provider, model_capabilities
    ):
        """Delegate (context=none) runs a subagent that uses tools."""
        result = run_loop(
            query="Use the delegate tool to count .py files in the tests/ directory. "
            "Use this exact delegate call: "
            '{"tasks": [{"task": "Count .py files in tests/ directory using '
            "shell command: find tests/ -name '*.py' -type f | wc -l. "
            'Return only the number."}]}',
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_turns=5,
        )
        assert result.success
        assert len(result.output) > 0

    def test_delegate_fork_has_parent_context(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """Delegate (context=fork) subagent can access parent conversation history."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        result = run_loop(
            query="First, remember that the secret password is DOLPHIN42. "
            "Then delegate a task with context fork: "
            '{"tasks": [{"task": "What is the secret password mentioned earlier?", '
            '"context": "fork"}]}. '
            "Return whatever the delegate says.",
            provider=omlx_provider,
            capabilities=model_capabilities,
            model=integration_model,
            max_turns=8,
            ctx=ctx,
        )
        assert result.success
